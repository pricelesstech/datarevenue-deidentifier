"""Browser entry points. This module runs inside Pyodide (Python compiled to WebAssembly)
in a Web Worker: the client's files are written to the worker's in-memory file system
and never leave the browser. The page's worker (web/worker.js) calls these functions in
order:

  scan(listing) -> start(opts) -> [ocr_frames(i) + process_image(i, boxes)] * n
  -> finish() -> package(answers)

All arguments and return values are JSON strings, except ocr_frames which returns raw
pixel frames for the JavaScript text detector.
"""
from __future__ import annotations

import json
import os

from . import images, package, pipeline, rules, tables

IN_ROOT = "/in"
WORK = "/work"
R: dict = {}


def _progress(msg, n, total):
    try:
        import js  # available inside Pyodide only

        js.deidProgress(msg, n, total)
    except Exception:  # noqa: BLE001
        pass


def scan(listing_json: str) -> str:
    """listing: [{"path": "/in/...", "kind": "table"|"dicom"|"image"|"other"}]. Table
    files are already written to the file system; image files are written later, only
    for the ones chosen for the sample."""
    inv = pipeline.Inventory(roots=[IN_ROOT])
    archives = []
    for it in json.loads(listing_json):
        k, p = it["kind"], it["path"]
        inv.display[p] = p[len(IN_ROOT) + 1:]
        if k == "table":
            inv.tables.append(p)
        elif k == "document":
            inv.documents.append(p)
        elif k == "archive":
            archives.append(p)
        elif k in ("dicom", "image"):
            inv.images.append((p, k))
        else:
            inv.other.append(p)
    # zips were written by the page; open them here (inside the browser's memory)
    budget = [1536 * 1024 ** 2]
    for a in archives:
        pipeline.expand_archive(inv, a, a + "_unzipped", inv.display[a], budget=budget)
        try:
            os.remove(a)
        except OSError:
            pass
    inv.tables.sort()
    inv.images.sort()
    inv.documents.sort()
    inv.headers = tables.scan_headers(inv.tables)
    R.clear()
    R["inv"] = inv
    return json.dumps({
        "tables": len(inv.tables),
        "images": len(inv.images),
        "dicom": sum(1 for _p, k in inv.images if k == "dicom"),
        "documents": len(inv.documents),
        "archives": len(archives),
        "other": len(inv.other),
        "columns": [{"name": h, "action": d["action"], "action_business": d["action_business"]}
                    for h, d in sorted(inv.headers.items(), key=lambda kv: kv[0].lower())],
        "labels": [[a, rules.ACTION_LABELS[a]] for a in rules.ACTIONS],
    })


def start(opts_json: str) -> str:
    """Pick the documents and images for the sample. Returns the paths the page must
    write to the file system before calling prepare()."""
    o = json.loads(opts_json)
    inv = R["inv"]
    for d in ("share", "private"):
        p = os.path.join(WORK, d)
        if os.path.isdir(p):
            import shutil

            shutil.rmtree(p)
    actions = {h: o.get("actions", {}).get(h, d["action"]) for h, d in inv.headers.items()}
    run = pipeline.Run(inv, actions, ocr=bool(o.get("ocr", True)),
                       share_dir=os.path.join(WORK, "share"),
                       private_dir=os.path.join(WORK, "private"),
                       sample_rows=int(o.get("rows", 500)),
                       sample_images=int(o.get("images", 25)),
                       sample_docs=int(o.get("docs", 100)),
                       keep_dates=bool(o.get("keep_dates", False)), progress=_progress)
    R["run"] = run
    docs = run.choose_documents()
    R["chosen"] = run.choose_images()
    return json.dumps(docs + [p for p, _k in R["chosen"]])


def prepare() -> None:
    """Documents (read, learn people), spreadsheets, then documents (write)."""
    run = R["run"]
    run.learn_people()
    run.tables()
    run.documents()
    for p in run.chosen_docs:
        try:
            os.remove(p)  # free browser memory as we go
        except OSError:
            pass


def make_jobs() -> int:
    R["jobs"] = R["run"].image_jobs(R["chosen"])
    return len(R["jobs"])


def ocr_frames(i: int):
    """8-bit frames of image i for the JavaScript text detector: [(w, h, channels, bytes)]."""
    job = R["jobs"][i]
    if not job["ocr"]:
        return []
    out = []
    for f in images.ocr_inputs(job):
        h, w = f.shape[:2]
        c = 1 if f.ndim == 2 else f.shape[2]
        out.append((int(w), int(h), int(c), f.tobytes()))
    return out


def process_image(i: int, boxes_json: str) -> str:
    job = dict(R["jobs"][i])
    if job["ocr"]:
        job["boxes"] = json.loads(boxes_json)
    res = images.process(job)
    R["run"].take_image(res)
    try:
        os.remove(job["src"])  # free browser memory as we go
    except OSError:
        pass
    return json.dumps({"status": res["status"], "text_boxes": res.get("text_boxes", 0)})


def finish() -> str:
    R["summary"] = R["run"].finish()
    s = R["summary"]
    return json.dumps({k: s[k] for k in ("images_ok", "images_with_text", "review",
                                         "other_files", "tables", "table_rows",
                                         "documents_written", "cancelled")})


def package_files(answers_json: str) -> str:
    """Build the zip. Returns paths of the zip, the summary page and the private key."""
    os.makedirs(os.path.join(WORK, "out"), exist_ok=True)
    for f in os.listdir(os.path.join(WORK, "out")):
        os.remove(os.path.join(WORK, "out", f))
    r = package.build_package(R["summary"], json.loads(answers_json),
                              os.path.join(WORK, "out"))
    # one private key file: study codes, plus the people labels when documents were used
    import csv
    import io

    priv = R["run"].private
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["kind", "code_or_label", "original"])
    cw = os.path.join(priv, "patient_code_crosswalk.csv")
    if os.path.exists(cw):
        with open(cw, encoding="utf-8") as f:
            for row in list(csv.reader(f))[1:]:
                w.writerow(["study code", row[1], row[0]])
    pk = os.path.join(priv, "people_key.csv")
    if os.path.exists(pk):
        with open(pk, encoding="utf-8-sig") as f:
            for row in list(csv.reader(f))[1:]:
                w.writerow(["person", row[0], f"{row[1]} | {row[2]}".strip(" |")])
    key = os.path.join(WORK, "out", "private_key.csv")
    with open(key, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    r["key"] = key if buf.getvalue().count("\n") > 1 else None
    r["review_files"] = R["summary"]["review"] - R["summary"]["other_files"]
    return json.dumps(r)
