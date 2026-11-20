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
    for it in json.loads(listing_json):
        k, p = it["kind"], it["path"]
        if k == "table":
            inv.tables.append(p)
        elif k in ("dicom", "image"):
            inv.images.append((p, k))
        else:
            inv.other.append(p)
    inv.tables.sort()
    inv.images.sort()
    inv.headers = tables.scan_headers(inv.tables)
    R.clear()
    R["inv"] = inv
    return json.dumps({
        "tables": len(inv.tables),
        "images": len(inv.images),
        "dicom": sum(1 for _p, k in inv.images if k == "dicom"),
        "other": len(inv.other),
        "columns": [{"name": h, "action": d["action"]}
                    for h, d in sorted(inv.headers.items(), key=lambda kv: kv[0].lower())],
        "labels": [[a, rules.ACTION_LABELS[a]] for a in rules.ACTIONS],
    })


def start(opts_json: str) -> str:
    """Process all spreadsheets, then pick the images for the sample. Returns the image
    paths the page must write to the file system before calling make_jobs()."""
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
                       sample_images=int(o.get("images", 25)), progress=_progress)
    R["run"] = run
    run.tables()
    R["chosen"] = run.choose_images()
    return json.dumps([p for p, _k in R["chosen"]])


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
                                         "cancelled")})


def package_files(answers_json: str) -> str:
    """Build the zip. Returns paths of the zip, the summary page and the private key."""
    os.makedirs(os.path.join(WORK, "out"), exist_ok=True)
    for f in os.listdir(os.path.join(WORK, "out")):
        os.remove(os.path.join(WORK, "out", f))
    r = package.build_package(R["summary"], json.loads(answers_json),
                              os.path.join(WORK, "out"))
    key = os.path.join(R["run"].private, "patient_code_crosswalk.csv")
    r["key"] = key if os.path.exists(key) else None
    r["review_files"] = R["summary"]["review"] - R["summary"]["other_files"]
    return json.dumps(r)
