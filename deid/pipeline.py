"""Scan files/folders, then de-identify them into two folders:

  share   : de-identified tables/ and images/, plus DEIDENTIFICATION_REPORT.txt
  private : code crosswalk, project secret, file log, review list (never shared)

Two modes: a SAMPLE (random rows per table, random images, plus a profile of the whole
data set) for a quick look, or ALL files. Nothing identifying is written to the share
folder: file names are study codes or scrubbed, reports hold counts and column names only.
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import random
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

from . import __version__, images, rules, tables
from .crosswalk import Crosswalk

SHARE_DIR = "SHARE_THIS_deidentified"
PRIVATE_DIR = "KEEP_PRIVATE_do_not_share"
_SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "icon\r"}


@dataclass
class Inventory:
    roots: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    images: list = field(default_factory=list)  # (path, kind)
    other: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)

    @property
    def input_dir(self) -> str:  # backward compatible single-root name
        return self.roots[0] if self.roots else ""


def _add_file(inv: Inventory, p: str) -> None:
    name = os.path.basename(p)
    if name.startswith(("~$", "._")) or name.lower() in _SKIP_NAMES:
        return
    ext = os.path.splitext(name)[1].lower()
    if ext in tables.TABLE_EXTS:
        inv.tables.append(p)
    elif ext in images.IMAGE_EXTS or images.is_dicom(p):
        inv.images.append((p, "dicom" if images.is_dicom(p) else "image"))
    else:
        inv.other.append(p)


def scan(paths, output_dir: str | None = None) -> Inventory:
    """paths: one folder, or a list of files and folders (from a drop or a picker)."""
    if isinstance(paths, str):
        paths = [paths]
    inv = Inventory(roots=[os.path.abspath(p) for p in paths])
    skip = os.path.abspath(output_dir) if output_dir else None
    seen = set()
    for p in inv.roots:
        if os.path.isfile(p):
            if p not in seen:
                seen.add(p)
                _add_file(inv, p)
            continue
        for root, dirs, files in os.walk(p):
            if skip and os.path.abspath(root).startswith(skip):
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                fp = os.path.join(root, name)
                if fp not in seen:
                    seen.add(fp)
                    _add_file(inv, fp)
    inv.headers = tables.scan_headers(inv.tables)
    return inv


def _link_code(path: str, crosswalk: Crosswalk) -> str | None:
    """Find a known patient ID in the file name or its two parent folder names."""
    parts = [os.path.splitext(os.path.basename(path))[0]]
    parent = os.path.dirname(path)
    for _ in range(2):
        parts.append(os.path.basename(parent))
        parent = os.path.dirname(parent)
    for part in parts:
        for tok in re.split(r"[^A-Za-z0-9]+", part):
            if len(tok) >= 3 and any(c.isdigit() for c in tok):
                code = crosswalk.lookup(tok)
                if code:
                    return code
    return None


class Run:
    """One de-identification run, in steps, so any front end can drive it:

        r = Run(...); r.tables(); chosen = r.choose_images(); jobs = r.image_jobs(chosen)
        for job in jobs: r.take_image(images.process(job))
        summary = r.finish()

    The desktop app runs images in a process pool (run() below); the browser runs them
    one at a time with its own text detector (deid/web.py)."""

    def __init__(self, inv: Inventory, actions: dict, ocr: bool = True,
                 share_dir: str | None = None, private_dir: str | None = None,
                 output_dir: str | None = None, sample_rows: int | None = None,
                 sample_images: int | None = None, seed: int | None = None,
                 progress=None, cancel: threading.Event | None = None):
        self.inv, self.actions, self.ocr = inv, actions, ocr
        self.progress = progress or (lambda *_a, **_k: None)
        self.cancel = cancel or threading.Event()
        self.share = share_dir or os.path.join(output_dir, SHARE_DIR)
        self.private = private_dir or os.path.join(output_dir, PRIVATE_DIR)
        self.t_dir = os.path.join(self.share, "tables")
        self.i_dir = os.path.join(self.share, "images")
        for d in (self.share, self.private, self.t_dir, self.i_dir):
            os.makedirs(d, exist_ok=True)
        self.cw = Crosswalk(self.private)
        self.started = _dt.datetime.now()
        self.seed = seed if seed is not None else int(self.started.timestamp())
        self.sample_rows, self.sample_images = sample_rows, sample_images
        self.file_log: list = []  # (original, output, status, note)
        self.review: list = []
        self.table_results: list = []
        self.jobs: list = []
        self.img_stats = Counter()
        self.img_info: list = []
        self.text_log: list = []
        self.done_images = 0

    def tables(self) -> None:
        self.progress("Reading spreadsheets", 0, 1)
        names = tables.collect(self.inv.tables, self.actions, self.cw)
        used: set[str] = set()
        n_tables = len(self.inv.tables)
        for n, p in enumerate(self.inv.tables, 1):
            if self.cancel.is_set():
                break
            self.progress(f"Spreadsheet {n} of {n_tables}", n, max(n_tables, 1))
            r = tables.write_one(p, self.t_dir, self.actions, self.cw, names, used,
                                 sample_rows=self.sample_rows, seed=self.seed)
            self.table_results.append(r)
            if r.error:
                self.review.append((p, f"spreadsheet could not be processed: {r.error}"))
                self.file_log.append((p, "", "review", r.error))
            else:
                self.file_log.append((p, r.output, "ok", f"{r.rows} of {r.source_rows} rows"))
        self.cw.save()

    def choose_images(self) -> list:
        chosen = list(self.inv.images)
        if self.sample_images is not None and len(chosen) > self.sample_images:
            chosen = random.Random(self.seed).sample(chosen, self.sample_images)
            chosen.sort()
        return chosen

    def image_jobs(self, chosen: list) -> list:
        for seq, (p, kind) in enumerate(chosen, 1):
            code = None
            if kind == "dicom":
                pid = images.dicom_patient_id(p)
                code = self.cw.code_for(pid) if pid else None
            if not code:
                code = _link_code(p, self.cw)
            sub = os.path.join(self.i_dir, f"{(seq - 1) // 1000 + 1:04d}")
            os.makedirs(sub, exist_ok=True)
            stem = f"{code}_{seq:07d}" if code else f"IMG_{seq:07d}"
            self.jobs.append({"src": p, "kind": kind, "code": code, "dst_dir": sub,
                              "out_stem": stem, "secret": self.cw.secret, "ocr": self.ocr})
        self.cw.save()
        return self.jobs

    def take_image(self, res: dict) -> None:
        self.done_images += 1
        if res["status"] == "ok":
            self.img_stats["ok"] += 1
            if res.get("info"):
                self.img_info.append(res["info"])
            if res.get("text_boxes"):
                self.img_stats["with_text"] += 1
                self.img_stats["text_boxes"] += res["text_boxes"]
                self.text_log.append((res["src"], res["out"], res["text_boxes"],
                                      " | ".join(t for t in res.get("texts", []) if t)[:500]))
            if res.get("code"):
                self.img_stats["linked"] += 1
            self.file_log.append((res["src"], res["out"], "ok",
                                  f"{res.get('text_boxes', 0)} text areas blacked out"))
        else:
            self.img_stats["review"] += 1
            self.review.append((res["src"], res.get("reason", "")))
            self.file_log.append((res["src"], "", "review", res.get("reason", "")))
        self.progress(f"Image {self.done_images} of {len(self.jobs)}",
                      self.done_images, len(self.jobs))

    def finish(self) -> dict:
        for p in self.inv.other:
            self.review.append((p, "not a spreadsheet or image; not copied. "
                                   "Review by hand if needed."))
            self.file_log.append((p, "", "skipped", "unsupported file type"))

        def _w(name, header, rows):
            with open(os.path.join(self.private, name), "w", newline="",
                      encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)

        stamp = self.started.strftime("%Y%m%d_%H%M%S")
        _w(f"file_log_{stamp}.csv", ["original_file", "output_file", "status", "note"],
           self.file_log)
        _w(f"needs_review_{stamp}.csv", ["original_file", "reason"], self.review)
        _w(f"text_blacked_out_{stamp}.csv",
           ["original_file", "output_file", "areas", "text_that_was_read"], self.text_log)

        tr = self.table_results
        summary = {
            "cancelled": self.cancel.is_set(),
            "sampling": self.sample_rows is not None or self.sample_images is not None,
            "tables": len(tr),
            "table_rows": sum(r.rows for r in tr),
            "table_source_rows": sum(r.source_rows for r in tr),
            "images_total": len(self.inv.images),
            "images_selected": len(self.jobs),
            "images_ok": self.img_stats["ok"],
            "images_with_text": self.img_stats["with_text"],
            "text_boxes": self.img_stats["text_boxes"],
            "images_linked": self.img_stats["linked"],
            "review": len(self.review),
            "other_files": len(self.inv.other),
            "patients_coded": len(self.cw),
            "share_dir": self.share,
            "private_dir": self.private,
            "seconds": int((_dt.datetime.now() - self.started).total_seconds()),
            "table_profiles": [
                {"file": os.path.basename(r.output), "source_rows": r.source_rows,
                 "sample_rows": r.rows, "sheets": r.profiles}
                for r in tr if not r.error
            ],
            "image_profile": _image_profile(self.inv, self.img_info),
        }
        _report(os.path.join(self.share, "DEIDENTIFICATION_REPORT.txt"), summary, tr,
                self.ocr, self.started)
        return summary


def run(inv: Inventory, output_dir: str | None, actions: dict, ocr: bool = True,
        workers: int | None = None, progress=None, cancel: threading.Event | None = None,
        sample_rows: int | None = None, sample_images: int | None = None,
        share_dir: str | None = None, private_dir: str | None = None,
        seed: int | None = None) -> dict:
    r = Run(inv, actions, ocr=ocr, share_dir=share_dir, private_dir=private_dir,
            output_dir=output_dir, sample_rows=sample_rows, sample_images=sample_images,
            seed=seed, progress=progress, cancel=cancel)
    r.tables()
    jobs = r.image_jobs(r.choose_images())
    cancel = r.cancel
    if jobs and not cancel.is_set():
        workers = workers or max(1, min(8, (os.cpu_count() or 2) // 2))
        r.progress("Loading the text-detection model (a few seconds)", 0, len(jobs))
        if len(jobs) <= 6 or workers == 1:
            for job in jobs:
                if cancel.is_set():
                    break
                r.take_image(images.process(job))
        else:
            # imported here: the browser build (Pyodide) has no process pools
            from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

            with ProcessPoolExecutor(max_workers=workers) as ex:
                pending = set()
                it = iter(jobs)
                exhausted = False
                while True:
                    while not exhausted and len(pending) < workers * 3 and not cancel.is_set():
                        try:
                            pending.add(ex.submit(images.process, next(it)))
                        except StopIteration:
                            exhausted = True
                    if not pending:
                        break
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        r.take_image(fut.result())
                    if cancel.is_set():
                        for f in pending:
                            f.cancel()
                        pending = {f for f in pending if not f.cancelled()}
                        exhausted = True
    return r.finish()

def _image_profile(inv: Inventory, info: list) -> dict:
    fmt_all = Counter()
    for p, kind in inv.images:
        fmt_all["DICOM" if kind == "dicom" else
                os.path.splitext(p)[1].lstrip(".").upper().replace("JPEG", "JPG")] += 1
    years = [i["year"] for i in info if i.get("year")]
    widths = sorted(i["w"] for i in info)
    heights = sorted(i["h"] for i in info)
    return {
        "total_by_format": dict(fmt_all.most_common()),
        "processed": len(info),
        "modalities": dict(Counter(i["modality"] for i in info if i["modality"]).most_common()),
        "manufacturers": dict(Counter(i["manufacturer"] for i in info
                                      if i["manufacturer"]).most_common(5)),
        "typical_size": f"{widths[len(widths) // 2]} x {heights[len(heights) // 2]} px"
        if widths else None,
        "bit_depths": dict(Counter(f"{i['bits']}-bit" for i in info).most_common()),
        "years": [min(years), max(years)] if years else None,
    }


def _report(path, s, table_results, ocr, started):
    L = []
    L.append("DE-IDENTIFICATION REPORT")
    L.append("=" * 60)
    L.append(f"Prepared: {started:%Y-%m-%d %H:%M}   Tool: Data Revenue De-Identifier "
             f"v{__version__} (offline)")
    L.append("Method: HIPAA Safe Harbor, 45 CFR 164.514(b)(2)")
    if s["cancelled"]:
        L.append("STATUS: STOPPED BEFORE FINISHING. This output is incomplete.")
    if s["sampling"]:
        L.append("SCOPE: a random SAMPLE of the data set, not every record.")
    L.append("")
    L.append("SUMMARY")
    L.append(f"  Spreadsheets ...................... {s['tables']} "
             f"({s['table_rows']:,} rows written of {s['table_source_rows']:,})")
    L.append(f"  Images de-identified .............. {s['images_ok']:,} "
             f"(of {s['images_total']:,} found)")
    L.append(f"  Images with printed text removed .. {s['images_with_text']:,}")
    L.append(f"  Images linked to a study code ..... {s['images_linked']:,}")
    L.append(f"  Files held back for review ........ {s['review']:,} (not included)")
    L.append("")
    L.append("HOW EACH COLUMN WAS HANDLED")
    by_header: dict = {}
    for r in table_results:
        for h, a in r.columns.items():
            by_header[h] = a
    for h in sorted(by_header, key=str.lower):
        L.append(f"  {h[:40]:<40}  {rules.ACTION_LABELS[by_header[h]]}")
    hits_total: dict = {}
    for r in table_results:
        for h, hd in r.hits.items():
            for k, v in hd.items():
                hits_total.setdefault(h, Counter())[k] += v
    if hits_total:
        L.append("")
        L.append("IDENTIFIERS FOUND AND ERASED INSIDE KEPT OR FREE-TEXT COLUMNS")
        for h, c in sorted(hits_total.items()):
            parts = ", ".join(f"{v:,} {k}" for k, v in c.most_common())
            L.append(f"  {h[:40]:<40}  {parts}")
    L.append("")
    L.append("WHAT WAS DONE")
    L.append("  - Names, street address, city, county, phone, fax, email, SSN, account,")
    L.append("    insurance, license, device and vehicle numbers, URLs, IPs: removed.")
    L.append("  - Customer, patient, member or record IDs: replaced with random study codes")
    L.append("    (P + 8 digits) not derived from the original number. The code key is")
    L.append("    held only by the data owner and is not included here.")
    L.append("  - Dates: year only. Birth years for people 90 or older are grouped.")
    L.append("  - Ages over 89: shown as 90+.")
    L.append("  - ZIP codes: first 3 digits; low-population areas shown as 000.")
    L.append("  - Free-text columns: names, dates, phone numbers, emails and long numbers")
    L.append("    replaced with [NAME], [DATE], [PHONE], [EMAIL], [NUMBER].")
    L.append("  - Data profile: values shared by fewer than 5 records are never listed.")
    L.append("  - DICOM images: header rebuilt from an allowlist of technical fields; all")
    L.append("    patient, provider, institution and private vendor fields dropped; new")
    L.append("    UIDs; dates reduced to year.")
    L.append("  - Other images: all metadata (EXIF, GPS, comments) removed.")
    if ocr:
        L.append("  - Text printed inside images (names, IDs, dates, labels) was")
        L.append("    detected by an offline text-recognition model and blacked out.")
    else:
        L.append("  - Printed text inside images was NOT checked in this run.")
    L.append("")
    L.append("DATA OWNER ATTESTATION (Safe Harbor also requires that the owner has no")
    L.append("actual knowledge that the remaining data could identify a person):")
    L.append("")
    L.append("  I reviewed these files and have no actual knowledge that the remaining")
    L.append("  information could be used to identify any individual.")
    L.append("")
    L.append("  Name: ______________________   Signature: ______________   Date: ________")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
