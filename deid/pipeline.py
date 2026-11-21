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

from . import __version__, documents, images, rules, tables
from .crosswalk import Crosswalk
from .people import PeopleBook

SHARE_DIR = "SHARE_THIS_deidentified"
PRIVATE_DIR = "KEEP_PRIVATE_do_not_share"
_SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "icon\r"}
ARCHIVE_EXTS = {".zip"}


@dataclass
class Inventory:
    roots: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    images: list = field(default_factory=list)  # (path, kind)
    documents: list = field(default_factory=list)
    archives: list = field(default_factory=list)
    other: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    display: dict = field(default_factory=dict)  # path -> path shown (for unzipped files)
    temp_dirs: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # (path, note) e.g. archive size limit hit

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
    elif ext in documents.DOC_EXTS:
        inv.documents.append(p)
    elif ext in ARCHIVE_EXTS:
        inv.archives.append(p)
    elif ext in images.IMAGE_EXTS or images.is_dicom(p):
        inv.images.append((p, "dicom" if images.is_dicom(p) else "image"))
    else:
        inv.other.append(p)


def _safe_parts(member: str) -> list[str]:
    """Zip member name -> safe path parts (no absolute paths, drives or '..')."""
    parts = []
    for part in member.replace("\\", "/").split("/"):
        part = part.strip()
        if part in ("", ".", "..") or ":" in part:
            continue
        parts.append(part)
    return parts


def expand_archive(inv: Inventory, path: str, dest: str, shown: str, depth: int = 0,
                   budget: list | None = None) -> None:
    """Unzip into dest (safely) and add what is inside to the inventory. Zips inside zips
    are opened too, up to 3 levels. budget = [bytes left] caps the total unzipped size."""
    import zipfile

    budget = budget if budget is not None else [20 * 1024 ** 3]
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        inv.other.append(path)
        inv.notes.append((path, f"could not open zip ({type(e).__name__})"))
        return
    with z:
        for info in z.infolist():
            if info.is_dir() or info.flag_bits & 0x1:  # folders, password-protected files
                if info.flag_bits & 0x1:
                    inv.notes.append((path, "password-protected files inside were skipped"))
                continue
            parts = _safe_parts(info.filename)
            if not parts:
                continue
            if info.file_size > budget[0]:
                inv.notes.append((path, "size limit reached; the rest of this zip was skipped"))
                break
            target = os.path.join(dest, *parts)
            real = os.path.realpath(target)
            if not real.startswith(os.path.realpath(dest) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            budget[0] -= info.file_size
            inv.display[target] = shown + "/" + "/".join(parts)
            if os.path.splitext(target)[1].lower() in ARCHIVE_EXTS and depth < 3:
                sub = target + "_unzipped"
                expand_archive(inv, target, sub, inv.display[target], depth + 1, budget)
            else:
                _add_file(inv, target)


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
    if inv.archives:
        import tempfile

        budget = [20 * 1024 ** 3]
        for a in list(inv.archives):
            tmp = tempfile.mkdtemp(prefix="deid_unzip_")
            inv.temp_dirs.append(tmp)
            expand_archive(inv, a, tmp, _shown(inv, a), budget=budget)
    inv.headers = tables.scan_headers(inv.tables)
    return inv


def _shown(inv: Inventory, p: str) -> str:
    """The path to show for a file: relative to what was added, or inside its zip."""
    if p in inv.display:
        return inv.display[p]
    for r in inv.roots:
        if p == r:
            return os.path.basename(p)
        if p.startswith(r.rstrip("/\\") + os.sep) or p.startswith(r.rstrip("/") + "/"):
            return os.path.basename(r) + "/" + os.path.relpath(p, r).replace("\\", "/")
    return os.path.basename(p)


def cleanup(inv: Inventory) -> None:
    import shutil

    for d in inv.temp_dirs:
        shutil.rmtree(d, ignore_errors=True)
    inv.temp_dirs.clear()


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
                 progress=None, cancel: threading.Event | None = None,
                 keep_dates: bool = False, sample_docs: int | None = None):
        rules.set_mode(keep_dates)
        self.keep_dates = keep_dates
        if keep_dates:  # business data: business dates keep their value, birth dates do not
            actions = {h: (rules.KEEP if a == rules.YEAR and not rules.is_birth_header(h)
                           else a) for h, a in actions.items()}
        self.inv, self.actions, self.ocr = inv, actions, ocr
        self.sample_docs = sample_docs
        self.people = PeopleBook()
        self.names: set[str] = set()
        self._docs: dict = {}       # path -> extracted Doc (pass 1)
        self.chosen_docs: list = []
        self.doc_stats = Counter()
        self.doc_kinds = Counter()
        self.doc_years: list = []
        self.doc_excerpts: list = []
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

    # ── documents, pass 1: read them and learn every person they name ──
    def choose_documents(self) -> list:
        chosen = list(self.inv.documents)
        if self.sample_docs is not None and len(chosen) > self.sample_docs:
            chosen = sorted(random.Random(f"{self.seed}:docs").sample(chosen, self.sample_docs))
        self.chosen_docs = chosen
        return chosen

    def learn_people(self) -> None:
        if not self.chosen_docs and self.inv.documents:
            self.choose_documents()
        n = len(self.chosen_docs)
        for i, p in enumerate(self.chosen_docs, 1):
            if self.cancel.is_set():
                break
            self.progress(f"Reading document {i} of {n}", i, max(n, 1))
            try:
                d = documents.extract(p)
                documents.learn(d, self.people)
                self._docs[p] = d
            except Exception as e:  # noqa: BLE001
                self._docs[p] = f"{type(e).__name__}: {str(e)[:150]}"

    def tables(self) -> None:
        self.progress("Reading spreadsheets", 0, 1)
        names = tables.collect(self.inv.tables, self.actions, self.cw)
        # people found in documents are erased from spreadsheet text too, and names from
        # spreadsheet name columns are erased from documents
        self.people.extra_tokens |= names
        names = names | {t for t in self.people.tokens}
        self.names = names
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

    # ── documents, pass 2: write the de-identified text versions ──
    def _out_name(self, p: str, used: set) -> str:
        shown = _shown(self.inv, p)
        parts = shown.split("/")
        clean = []
        for idx, part in enumerate(parts):
            last = idx == len(parts) - 1
            stem, ext = os.path.splitext(part) if last else (part, "")
            s = self.people.scrub(stem)
            s = rules.scrub_patterns(s, rules.text_patterns(), {})
            s = re.sub(r"[\[\]]", "", s)
            s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip(" .") or "document"
            clean.append(s[:80] + (ext.replace(".", "_") if last else ""))
        rel = os.path.join(*clean) + ".txt"
        base, n = rel, 2
        while rel.lower() in used:
            rel = base[:-4] + f"_{n}.txt"
            n += 1
        used.add(rel.lower())
        return rel

    def documents(self) -> None:
        if not self.chosen_docs:
            return
        d_dir = os.path.join(self.share, "documents")
        used: set = set()
        n = len(self.chosen_docs)
        for i, p in enumerate(self.chosen_docs, 1):
            if self.cancel.is_set():
                break
            self.progress(f"Cleaning document {i} of {n}", i, max(n, 1))
            d = self._docs.get(p)
            if d is None:
                continue
            if isinstance(d, str):
                self.review.append((p, f"document could not be read: {d}"))
                self.file_log.append((p, "", "review", d))
                self.doc_stats["unreadable"] += 1
                continue
            self.doc_kinds[d.kind] += 1
            if d.scanned or (not d.text.strip() and not d.attachments):
                self.review.append((p, d.note or "no readable text: not included"))
                self.file_log.append((p, "", "review", d.note or "no text"))
                self.doc_stats["no text"] += 1
                continue
            hits: dict = {}
            rel = self._out_name(p, used)
            title = os.path.basename(rel)[:-4]
            text = documents.render(d, self.people, self.names, self.keep_dates, hits, title)
            out = os.path.join(d_dir, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            self.doc_stats["written"] += 1
            self.doc_stats["words"] += documents.word_count(text)
            self.doc_stats["pages"] += d.pages
            self.doc_stats["emails"] += d.kind == "Email"
            self.doc_stats["attachments read"] += sum(1 for a in d.attachments if a[1])
            for k, v in hits.items():
                self.doc_stats[f"removed: {k}"] += v
            if d.date:
                self.doc_years.append(d.date.year)
            if len(self.doc_excerpts) < 3 and documents.word_count(d.text) > 40:
                self.doc_excerpts.append((title, d.kind, text[:900]))
            self.file_log.append((p, out, "ok", f"{d.kind}; {sum(hits.values())} removed"))
        self._docs.clear()

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
            self.review.append((p, "not a supported file type; not copied. "
                                   "Review by hand if needed."))
            self.file_log.append((p, "", "skipped", "unsupported file type"))
        for p, note in self.inv.notes:
            self.review.append((p, note))
        if len(self.people):
            self.people.save(self.private)

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
            "keep_dates": self.keep_dates,
            "documents_total": len(self.inv.documents),
            "documents_written": self.doc_stats["written"],
            "document_profile": {
                "total_by_type": dict(Counter(
                    os.path.splitext(p)[1].lstrip(".").upper() for p in self.inv.documents
                ).most_common()),
                "kinds_in_sample": dict(self.doc_kinds.most_common()),
                "written": self.doc_stats["written"],
                "emails": self.doc_stats["emails"],
                "attachments_read": self.doc_stats["attachments read"],
                "words": self.doc_stats["words"],
                "pages": self.doc_stats["pages"],
                "no_text": self.doc_stats["no text"],
                "unreadable": self.doc_stats["unreadable"],
                "people": len(self.people),
                "years": [min(self.doc_years), max(self.doc_years)] if self.doc_years else None,
                "removed": {k[9:]: v for k, v in self.doc_stats.items()
                            if k.startswith("removed: ")},
                "excerpts": self.doc_excerpts,
            },
        }
        _report(os.path.join(self.share, "DEIDENTIFICATION_REPORT.txt"), summary, tr,
                self.ocr, self.started)
        cleanup(self.inv)
        return summary


def run(inv: Inventory, output_dir: str | None, actions: dict, ocr: bool = True,
        workers: int | None = None, progress=None, cancel: threading.Event | None = None,
        sample_rows: int | None = None, sample_images: int | None = None,
        share_dir: str | None = None, private_dir: str | None = None,
        seed: int | None = None, keep_dates: bool = False,
        sample_docs: int | None = None) -> dict:
    r = Run(inv, actions, ocr=ocr, share_dir=share_dir, private_dir=private_dir,
            output_dir=output_dir, sample_rows=sample_rows, sample_images=sample_images,
            seed=seed, progress=progress, cancel=cancel, keep_dates=keep_dates,
            sample_docs=sample_docs)
    r.choose_documents()
    r.learn_people()
    r.tables()
    r.documents()
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
    if s.get("keep_dates"):
        L.append("DATA TYPE: business data. Business dates (orders, reports, emails) were")
        L.append("kept; dates of birth were reduced to the year. Every other identifier was")
        L.append("removed as Safe Harbor requires.")
    else:
        L.append("DATA TYPE: health data (strict). No date more specific than the year.")
    if s["cancelled"]:
        L.append("STATUS: STOPPED BEFORE FINISHING. This output is incomplete.")
    if s["sampling"]:
        L.append("SCOPE: a random SAMPLE of the data set, not every record.")
    L.append("")
    L.append("SUMMARY")
    L.append(f"  Spreadsheets ...................... {s['tables']} "
             f"({s['table_rows']:,} rows written of {s['table_source_rows']:,})")
    dp = s.get("document_profile") or {}
    if s.get("documents_total"):
        L.append(f"  Documents and emails .............. {s['documents_written']:,} written "
                 f"(of {s['documents_total']:,} found)")
        L.append(f"  People replaced with labels ....... {dp.get('people', 0):,}")
        L.append(f"  Documents with no text layer ...... {dp.get('no_text', 0):,} (not included)")
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
    if s.get("keep_dates"):
        L.append("  - Dates of birth: year only. Business dates kept.")
    else:
        L.append("  - Dates: year only. Birth years for people 90 or older are grouped.")
    if s.get("documents_total"):
        L.append("  - Documents and emails: only a text version is included (no original")
        L.append("    files, so no hidden authors, comments or tracked changes). People are")
        L.append("    replaced with labels (Person 1, Person 2) used the same way in every")
        L.append("    document; emails, phone numbers, street addresses, tax IDs and long")
        L.append("    numbers are replaced. Company names are kept. PDFs that are scans or")
        L.append("    drawings with no text layer are not included.")
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
