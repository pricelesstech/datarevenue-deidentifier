"""Inventory mode: a fast, read-only count of everything on a drive or folder, for a quote.

Nothing is copied, cleaned or changed. The walk reads file sizes and dates from the folder
listing; a few file types are opened to count records (spreadsheets, SQLite databases) or
list contents (zips, read from the zip's own index, never unpacked). Spreadsheet column
NAMES are classified to show what kinds of personal data exist; values are never read past
the header rows and never reported.

    inv = run_inventory(["D:\\"], progress=fn, cancel=event)
    -> dict (JSON-safe) consumed by deid.inventory_report

Built for very large drives (terabytes, millions of files): totals are kept as running
aggregates, the full file list is streamed to a private CSV, and anything slow is capped.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import heapq
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from collections import Counter, defaultdict

from . import rules

# ── file types ────────────────────────────────────────────────────────────────────
# (category, extensions). Order is the order shown in the report.
CATEGORIES: list[tuple[str, set[str]]] = [
    ("Spreadsheets and data tables", {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".xlsb",
                                      ".ods", ".parquet", ".avro", ".orc", ".feather",
                                      ".arrow", ".sav", ".dta", ".sas7bdat", ".numbers"}),
    ("Databases and database backups", {".sqlite", ".sqlite3", ".db", ".db3", ".mdb",
                                        ".accdb", ".mdf", ".ndf", ".ldf", ".bak", ".dbf",
                                        ".sql", ".dump", ".fdb", ".gdb", ".frm", ".ibd",
                                        ".myd", ".qbw", ".qbb", ".tlg"}),
    ("Emails and mailboxes", {".msg", ".eml", ".pst", ".ost", ".mbox", ".emlx", ".olm"}),
    ("Documents", {".pdf", ".doc", ".docx", ".docm", ".odt", ".rtf", ".pages", ".ppt",
                   ".pptx", ".odp", ".key", ".wpd", ".xps", ".one"}),
    ("Text, logs and structured text", {".txt", ".log", ".json", ".jsonl", ".ndjson",
                                        ".xml", ".yaml", ".yml", ".md", ".html", ".htm",
                                        ".ini", ".cfg", ".hl7", ".edi", ".x12", ".dat"}),
    ("Medical images (DICOM)", {".dcm", ".dicom"}),
    ("Photos and images", {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
                           ".heic", ".heif", ".webp", ".raw", ".cr2", ".cr3", ".nef",
                           ".arw", ".dng", ".orf", ".rw2", ".svg", ".ico", ".jp2"}),
    ("Video", {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".flv",
               ".webm", ".mts", ".m2ts", ".3gp", ".vob", ".ts"}),
    ("Audio and call recordings", {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma",
                                   ".aiff", ".aif", ".opus", ".amr"}),
    ("Design, CAD and 3D", {".dwg", ".dxf", ".rvt", ".rfa", ".skp", ".step", ".stp",
                            ".iges", ".igs", ".stl", ".obj", ".fbx", ".3ds", ".max",
                            ".blend", ".psd", ".ai", ".indd", ".eps", ".sldprt", ".sldasm",
                            ".ipt", ".iam", ".nwd", ".ifc", ".dgn", ".pln"}),
    ("Maps and GIS", {".shp", ".shx", ".kml", ".kmz", ".geojson", ".gpx", ".gpkg", ".mxd",
                      ".aprx", ".las", ".laz"}),
    ("Zip and compressed files", {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2",
                                  ".xz", ".zst", ".cab"}),
    ("Disk images and system backups", {".iso", ".vhd", ".vhdx", ".vmdk", ".img", ".dmg",
                                        ".tib", ".gho", ".wim", ".sparsebundle", ".qcow2"}),
    ("Source code and scripts", {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs",
                                 ".go", ".rb", ".php", ".r", ".ipynb", ".m", ".swift",
                                 ".kt", ".scala", ".sh", ".ps1", ".bat", ".vb", ".css"}),
    ("Programs and system files", {".exe", ".dll", ".msi", ".sys", ".so", ".dylib", ".app",
                                   ".pkg", ".deb", ".rpm", ".jar", ".bin", ".tmp", ".lnk",
                                   ".cache", ".pyc", ".class", ".o", ".lib"}),
]
OTHER = "Other files"
CATEGORY_NAMES = [c for c, _ in CATEGORIES] + [OTHER]
_EXT_CAT = {e: c for c, exts in CATEGORIES for e in exts}

# Categories that usually carry the value a buyer pays for (shown first in the summary).
VALUE_CATEGORIES = {"Spreadsheets and data tables", "Databases and database backups",
                    "Emails and mailboxes", "Documents", "Text, logs and structured text",
                    "Medical images (DICOM)", "Photos and images", "Video",
                    "Audio and call recordings", "Design, CAD and 3D", "Maps and GIS"}

_SKIP_DIRS = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
              "recycler", "found.000", ".trashes", ".spotlight-v100", ".fseventsd",
              ".temporaryitems", ".documentrevisions-v100", "node_modules", "__pycache__"}
# Skipped only when the walk starts at a drive root (someone pointed us at C:\).
_SYSTEM_ROOT_DIRS = {"windows", "program files", "program files (x86)", "programdata",
                     "$sysreset", "recovery", "perflogs", "msocache", "system", "library",
                     "applications", "private", "usr", "bin", "sbin", "etc", "var", "opt",
                     "cores", "dev", "proc"}
_SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store", "pagefile.sys", "hiberfil.sys",
               "swapfile.sys"}

TABLE_HEADER_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}
MAX_TABLES_OPENED = 3000          # spreadsheets opened for headers + record counts
MAX_EXACT_CSV_BYTES = 256 << 20   # CSVs up to this size get an exact line count
MAX_XLS_BYTES = 60 << 20          # old .xls files are read whole by xlrd; skip giants
MAX_SQLITE_BYTES = 8 << 30
MAX_ZIPS_LISTED = 20000
DUP_MIN_BYTES = 1 << 20           # duplicate check only for files of 1 MB or more
FINGERPRINT_BYTES = 64 << 10
TOP_FOLDERS = 60
LARGEST_FILES = 25


def category_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _EXT_CAT.get(ext, OTHER)


def _year(ts: float) -> int | None:
    try:
        y = _dt.datetime.fromtimestamp(ts).year
    except (OverflowError, OSError, ValueError):
        return None
    return y if 1980 <= y <= _dt.date.today().year + 1 else None


def is_drive_root(path: str) -> bool:
    p = os.path.abspath(path)
    return os.path.dirname(p) == p or os.path.ismount(p)


def list_drives() -> list[dict]:
    """Drives and volumes the person can pick, with used / total space."""
    out = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                try:
                    u = shutil.disk_usage(root)
                except OSError:
                    continue
                out.append({"path": root, "label": f"{letter}: drive",
                            "used": u.used, "total": u.total})
    else:
        roots = ["/"]
        vols = "/Volumes" if os.path.isdir("/Volumes") else "/media"
        if os.path.isdir(vols):
            for d in sorted(os.listdir(vols)):
                p = os.path.join(vols, d)
                if os.path.isdir(p) and not os.path.islink(p):
                    roots.append(p)
        for r in roots:
            try:
                u = shutil.disk_usage(r)
            except OSError:
                continue
            out.append({"path": r, "label": os.path.basename(r) or "Main disk",
                        "used": u.used, "total": u.total})
    return out


# ── personal-data signals from column names only ──────────────────────────────────
_PII_RULES = [
    ("Social Security or tax ID numbers", {"ssn", "social", "sin", "tin", "ein", "taxid"}),
    ("Dates of birth", {"dob", "birth", "birthday", "birthdate", "born"}),
    ("Email addresses", {"email", "mail"}),
    ("Phone numbers", {"phone", "tel", "telephone", "mobile", "cell", "fax"}),
    ("Street addresses", {"address", "addr", "street", "line1", "line2", "apt", "suite"}),
    ("Financial account or card numbers", {"account", "acct", "card", "iban", "routing",
                                           "bank", "credit"}),
    ("Driver's license, passport or other ID numbers", {"license", "licence", "dl",
                                                        "passport", "plate", "vin"}),
    ("Health plan or medical IDs", {"mrn", "medicaid", "medicare", "mbi", "npi", "insured",
                                    "subscriber", "policy", "member", "chart", "patient"}),
]


def pii_kinds(header: str) -> list[str]:
    """What kind of personal data a column holds, judged from its NAME only."""
    if not header:
        return []
    toks = set(rules._tokens(header))
    joined = rules._joined(header)
    found = [label for label, words in _PII_RULES if toks & words or joined in words]
    action = rules.classify(header)
    if action == rules.NAME:
        found.append("Names of people")
    elif action == rules.ZIP3:
        found.append("ZIP or postal codes")
    elif action == rules.TEXT:
        found.append("Free-text notes (may mention people)")
    elif action == rules.CODE and "Health plan or medical IDs" not in found:
        found.append("Health plan or medical IDs")
    return list(dict.fromkeys(found))


# ── counting records ──────────────────────────────────────────────────────────────
def _count_lines(path: str, size: int) -> tuple[int, bool]:
    """(lines, estimated). Exact up to MAX_EXACT_CSV_BYTES, else extrapolated."""
    with open(path, "rb") as f:
        if size <= MAX_EXACT_CSV_BYTES:
            n, last = 0, b""
            while True:
                chunk = f.read(1 << 22)
                if not chunk:
                    break
                n += chunk.count(b"\n")
                last = chunk
            if last and not last.endswith(b"\n"):
                n += 1
            return n, False
        head = f.read(8 << 20)
    lines = head.count(b"\n")
    if not lines:
        return 0, True
    return int(size / (len(head) / lines)), True


def _csv_header(path: str) -> list[str]:
    from .tables import _read_text_file, _split_header

    enc, dialect = _read_text_file(path)
    rows = []
    with open(path, newline="", encoding=enc, errors="replace") as f:
        for i, r in enumerate(csv.reader(f, dialect)):
            rows.append(r)
            if i >= 20:
                break
    header, _ = _split_header(rows)
    return header or []


def table_facts(path: str, size: int) -> dict:
    """{"sheets": n, "records": n, "estimated": bool, "headers": [...]}, header rows only."""
    from .tables import _split_header

    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".tsv"):
        header = _csv_header(path)
        lines, est = _count_lines(path, size)
        top = 1 if header else 0
        return {"sheets": 1, "records": max(0, lines - top), "estimated": est,
                "headers": header}
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            recs, headers, sheets = 0, [], 0
            for ws in wb.worksheets:
                sheets += 1
                top = [list(r) for r in ws.iter_rows(min_row=1, max_row=20, values_only=True)]
                h, body = _split_header(top)
                if h is None:
                    continue
                headers += h
                mr = ws.max_row
                if mr is None:  # no stored size: count rows (still cheap, values only)
                    mr = sum(1 for _ in ws.iter_rows(values_only=True))
                recs += max(0, mr - (len(top) - len(body)))
            return {"sheets": sheets, "records": recs, "estimated": False, "headers": headers}
        finally:
            wb.close()
    if ext == ".xls":
        if size > MAX_XLS_BYTES:
            return {"sheets": 0, "records": 0, "estimated": True, "headers": [],
                    "note": "too large to open quickly"}
        import xlrd

        book = xlrd.open_workbook(path, on_demand=True)
        try:
            recs, headers = 0, []
            for si in range(book.nsheets):
                sh = book.sheet_by_index(si)
                top = [[c.value if c.value != "" else None for c in sh.row(r)]
                       for r in range(min(20, sh.nrows))]
                h, body = _split_header(top)
                if h is not None:
                    headers += h
                    recs += max(0, sh.nrows - (len(top) - len(body)))
                book.unload_sheet(si)
            return {"sheets": book.nsheets, "records": recs, "estimated": False,
                    "headers": headers}
        finally:
            book.release_resources()
    return {"sheets": 0, "records": 0, "estimated": False, "headers": []}


def sqlite_facts(path: str, size: int) -> dict | None:
    """Tables and row counts of a SQLite file, opened read-only. None if not SQLite."""
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return None
    except OSError:
        return None
    if size > MAX_SQLITE_BYTES:
        return {"tables": None, "records": 0, "headers": [], "note": "too large to count"}
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f").replace("#", "%23") + \
          "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        recs, headers = 0, []
        for n in names[:500]:
            q = n.replace('"', '""')
            try:
                recs += con.execute(f'SELECT COUNT(*) FROM "{q}"').fetchone()[0]
                headers += [r[1] for r in con.execute(f'PRAGMA table_info("{q}")')]
            except sqlite3.DatabaseError:
                continue
        return {"tables": len(names), "records": recs, "headers": headers}
    finally:
        con.close()


# Windows: files kept only in OneDrive / Dropbox / iCloud ("online-only"). Opening one
# downloads it, so such files are counted from the listing but never opened.
_CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


def is_cloud_only(st) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_ATTRS)


def _fingerprint(path: str, size: int) -> str | None:
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            h.update(f.read(FINGERPRINT_BYTES))
            if size > 2 * FINGERPRINT_BYTES:
                f.seek(size // 2)
                h.update(f.read(FINGERPRINT_BYTES))
                f.seek(-FINGERPRINT_BYTES, os.SEEK_END)
            h.update(f.read(FINGERPRINT_BYTES))
    except OSError:
        return None
    return h.hexdigest()


# ── the walk ──────────────────────────────────────────────────────────────────────
class _Agg:
    __slots__ = ("files", "bytes", "oldest", "newest", "cats")

    def __init__(self):
        self.files = 0
        self.bytes = 0
        self.oldest = None
        self.newest = None
        self.cats = Counter()

    def add(self, size, mtime, cat):
        self.files += 1
        self.bytes += size
        if mtime:
            self.oldest = mtime if self.oldest is None else min(self.oldest, mtime)
            self.newest = mtime if self.newest is None else max(self.newest, mtime)
        self.cats[cat] += size

    def out(self):
        top = self.cats.most_common(1)
        return {"files": self.files, "bytes": self.bytes,
                "oldest_year": _year(self.oldest) if self.oldest else None,
                "newest_year": _year(self.newest) if self.newest else None,
                "main_type": top[0][0] if top else None}


def run_inventory(paths, progress=None, cancel: threading.Event | None = None,
                  private_list: str | None = None, find_duplicates: bool = True,
                  count_records: bool = True) -> dict:
    """Walk every path and return the inventory. private_list = CSV path for the full
    file list (it holds file names, so it stays on the client's computer)."""
    if isinstance(paths, str):
        paths = [paths]
    progress = progress or (lambda *_a, **_k: None)
    cancel = cancel or threading.Event()
    roots = [os.path.abspath(p) for p in paths]
    t0 = time.time()

    # Expected total for the progress bar: used space when a whole drive was chosen.
    expect = 0
    for r in roots:
        if is_drive_root(r):
            try:
                expect += shutil.disk_usage(r).used
            except OSError:
                pass

    cats = {c: _Agg() for c in CATEGORY_NAMES}
    exts = defaultdict(lambda: [0, 0])                 # ext -> [files, bytes]
    years = defaultdict(lambda: [0, 0])                # year -> [files, bytes]
    folders1, folders2 = defaultdict(_Agg), defaultdict(_Agg)
    largest: list = []                                 # heap of (size, path, cat, mtime)
    by_size: dict[int, list] = defaultdict(list)       # duplicate candidates
    tables_seen: list = []                             # (path, size)
    dbs_seen: list = []
    zips_seen: list = []
    total = _Agg()
    errors = Counter()
    skipped_dirs = 0
    hidden_files = 0
    cloud_only = 0
    empty_files = 0
    max_depth = 0

    writer = fh = None
    if private_list:
        os.makedirs(os.path.dirname(private_list), exist_ok=True)
        fh = open(private_list, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(fh)
        writer.writerow(["path", "size_bytes", "modified", "type", "extension"])

    last_tick = 0.0
    try:
        for root in roots:
            if os.path.isfile(root):
                entries = [(root, os.path.basename(root), None, 0)]
                stack = []
            else:
                entries = []
                stack = [(root, None, None, 0)]
            # skip Windows / Program Files etc. only on the computer's own system drive
            at_drive_root = is_drive_root(root) and (
                root == "/" or os.path.isdir(os.path.join(root, "Windows")))
            root_label = os.path.basename(root.rstrip("\\/")) or root.rstrip("\\/") or root
            prefix = (root_label + "/") if len(roots) > 1 else ""

            def handle(fp, name, st, f1, f2, depth):
                nonlocal hidden_files, cloud_only, empty_files, max_depth, last_tick
                if name.lower() in _SKIP_FILES or name.startswith("~$"):
                    hidden_files += 1
                    return
                size, mtime = st.st_size, st.st_mtime
                remote = is_cloud_only(st)
                if remote:
                    cloud_only += 1
                cat = category_of(name)
                ext = os.path.splitext(name)[1].lower() or "(none)"
                total.add(size, mtime, cat)
                cats[cat].add(size, mtime, cat)
                e = exts[ext]
                e[0] += 1
                e[1] += size
                y = _year(mtime)
                if y:
                    years[y][0] += 1
                    years[y][1] += size
                folders1[f1 or "(files at the top level)"].add(size, mtime, cat)
                if f2:
                    folders2[f2].add(size, mtime, cat)
                max_depth = max(max_depth, depth)
                if size == 0:
                    empty_files += 1
                if len(largest) < LARGEST_FILES:
                    heapq.heappush(largest, (size, fp, cat, mtime))
                elif size > largest[0][0]:
                    heapq.heapreplace(largest, (size, fp, cat, mtime))
                if not remote:  # never open a cloud-only file: that would download it
                    if find_duplicates and size >= DUP_MIN_BYTES:
                        by_size[size].append(fp)
                    if ext in TABLE_HEADER_EXTS:
                        tables_seen.append((fp, size))
                    elif ext in (".sqlite", ".sqlite3", ".db", ".db3"):
                        dbs_seen.append((fp, size))
                    elif ext == ".zip":
                        zips_seen.append((fp, size))
                if writer:
                    writer.writerow([fp, size, _dt.datetime.fromtimestamp(mtime).isoformat(
                        timespec="seconds") if y else "", cat, ext])
                now = time.time()
                if now - last_tick > 0.4:
                    last_tick = now
                    msg = (f"Counted {total.files:,} files, "
                           f"{total.bytes / 1e9:,.1f} GB")
                    progress(msg, total.bytes, expect or 0)

            for fp, name, _x, _d in entries:
                try:
                    handle(fp, name, os.stat(fp), None, None, 0)
                except OSError:
                    errors["unreadable file"] += 1

            while stack:
                if cancel.is_set():
                    break
                d, f1, f2, depth = stack.pop()
                try:
                    it = os.scandir(d)
                except PermissionError:
                    errors["folder access denied"] += 1
                    continue
                except OSError:
                    errors["folder could not be opened"] += 1
                    continue
                subdirs = []
                with it:
                    for ent in it:
                        try:
                            if ent.is_symlink() or (hasattr(ent, "is_junction")
                                                    and ent.is_junction()):
                                continue
                            if ent.is_dir(follow_symlinks=False):
                                low = ent.name.lower()
                                if low in _SKIP_DIRS or (depth == 0 and at_drive_root and
                                                         low in _SYSTEM_ROOT_DIRS):
                                    skipped_dirs += 1
                                    continue
                                if ent.name.startswith("."):
                                    skipped_dirs += 1
                                    continue
                                if f1 is None:   # a top-level folder
                                    n1, n2 = prefix + ent.name, None
                                elif f2 is None:  # a folder one level down
                                    n1, n2 = f1, f1 + "/" + ent.name
                                else:
                                    n1, n2 = f1, f2
                                subdirs.append((ent.path, n1, n2, depth + 1))
                            elif ent.is_file(follow_symlinks=False):
                                handle(ent.path, ent.name, ent.stat(follow_symlinks=False),
                                       f1, f2, depth)
                        except PermissionError:
                            errors["file access denied"] += 1
                        except OSError:
                            errors["unreadable file"] += 1
                stack.extend(reversed(sorted(subdirs)))
            if cancel.is_set():
                break
    finally:
        if fh:
            fh.close()
    walk_seconds = time.time() - t0

    # ── second pass: open a few kinds of files (all capped) ──
    tables = []
    pii = Counter()            # kind -> number of columns
    pii_files = Counter()      # kind -> number of files with it
    table_records = 0
    table_records_est = False
    fields = 0
    if count_records and not cancel.is_set():
        # biggest first: they hold most of the records
        chosen = sorted(tables_seen, key=lambda t: -t[1])[:MAX_TABLES_OPENED]
        for i, (fp, size) in enumerate(chosen, 1):
            if cancel.is_set():
                break
            if i % 5 == 1:
                progress(f"Counting records in spreadsheet {i:,} of {len(chosen):,}",
                         i, len(chosen))
            try:
                f = table_facts(fp, size)
            except Exception as e:  # noqa: BLE001
                errors["spreadsheet could not be opened"] += 1
                tables.append({"path": fp, "bytes": size, "error": type(e).__name__})
                continue
            kinds = Counter()
            for h in f["headers"]:
                for k in pii_kinds(h):
                    kinds[k] += 1
            pii.update(kinds)
            pii_files.update(kinds.keys())
            table_records += f["records"]
            table_records_est |= f.get("estimated", False)
            fields += len(f["headers"])
            tables.append({"path": fp, "bytes": size, "sheets": f["sheets"],
                           "records": f["records"], "estimated": f.get("estimated", False),
                           "columns": len(f["headers"]), "personal_data": sorted(kinds),
                           "note": f.get("note")})
        not_opened = len(tables_seen) - len(chosen)
        if not_opened > 0 and tables:
            # estimate the rest from bytes per record of the ones we opened
            ok = [t for t in tables if t.get("records")]
            bpr = (sum(t["bytes"] for t in ok) / max(1, sum(t["records"] for t in ok))) if ok else 0
            rest = sum(s for _p, s in sorted(tables_seen, key=lambda t: -t[1])[MAX_TABLES_OPENED:])
            if bpr:
                table_records += int(rest / bpr)
                table_records_est = True

    databases = []
    db_records = 0
    if count_records and not cancel.is_set():
        for i, (fp, size) in enumerate(dbs_seen[:500], 1):
            if cancel.is_set():
                break
            progress(f"Reading database {i:,} of {min(500, len(dbs_seen)):,}", i,
                     min(500, len(dbs_seen)))
            try:
                f = sqlite_facts(fp, size)
            except Exception:  # noqa: BLE001
                errors["database could not be opened"] += 1
                continue
            if f is None:
                continue
            kinds = Counter()
            for h in f["headers"]:
                for k in pii_kinds(h):
                    kinds[k] += 1
            pii.update(kinds)
            pii_files.update(kinds.keys())
            db_records += f["records"]
            databases.append({"path": fp, "bytes": size, "tables": f["tables"],
                              "records": f["records"], "personal_data": sorted(kinds),
                              "note": f.get("note")})

    zip_inside = {"zips": 0, "files": 0, "bytes": 0, "by_type": Counter(),
                  "unreadable": 0}
    if not cancel.is_set():
        for i, (fp, _size) in enumerate(zips_seen[:MAX_ZIPS_LISTED], 1):
            if cancel.is_set():
                break
            if i % 20 == 1:
                progress(f"Listing zip file {i:,} of {min(len(zips_seen), MAX_ZIPS_LISTED):,}",
                         i, min(len(zips_seen), MAX_ZIPS_LISTED))
            try:
                with zipfile.ZipFile(fp) as z:
                    zip_inside["zips"] += 1
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        zip_inside["files"] += 1
                        zip_inside["bytes"] += info.file_size
                        zip_inside["by_type"][category_of(info.filename)] += info.file_size
            except Exception:  # noqa: BLE001
                zip_inside["unreadable"] += 1
    zip_inside["by_type"] = dict(zip_inside["by_type"].most_common())

    dup = {"checked": find_duplicates, "groups": 0, "extra_copies": 0, "extra_bytes": 0}
    if find_duplicates and not cancel.is_set():
        groups = [(s, ps) for s, ps in by_size.items() if len(ps) > 1]
        n = sum(len(ps) for _s, ps in groups)
        done = 0
        for size, ps in groups:
            if cancel.is_set():
                break
            fps = defaultdict(int)
            for p in ps:
                done += 1
                if done % 50 == 1:
                    progress(f"Checking for duplicate files {done:,} of {n:,}", done, n)
                h = _fingerprint(p, size)
                if h:
                    fps[h] += 1
            for c in fps.values():
                if c > 1:
                    dup["groups"] += 1
                    dup["extra_copies"] += c - 1
                    dup["extra_bytes"] += (c - 1) * size

    by_cat = []
    for c in CATEGORY_NAMES:
        a = cats[c]
        if a.files:
            o = a.out()
            o.pop("main_type")
            o["type"] = c
            by_cat.append(o)
    by_cat.sort(key=lambda r: -r["bytes"])

    def folder_rows(d):
        rows = [dict(name=k, **v.out()) for k, v in d.items()]
        rows.sort(key=lambda r: -r["bytes"])
        return rows[:TOP_FOLDERS], len(rows)

    f1_rows, f1_n = folder_rows(folders1)
    f2_rows, f2_n = folder_rows(folders2)
    for r in f2_rows:  # the top-level folder it sits in (sub names never contain "/")
        r["parent"] = r["name"].rsplit("/", 1)[0]

    return {
        "roots": roots,
        "complete": not cancel.is_set(),
        "seconds": round(time.time() - t0, 1),
        "walk_seconds": round(walk_seconds, 1),
        "total": total.out() | {"empty_files": empty_files, "max_depth": max_depth},
        "by_type": by_cat,
        "by_extension": sorted(([k, v[0], v[1]] for k, v in exts.items()),
                               key=lambda r: -r[2]),
        "by_year": sorted([y, v[0], v[1]] for y, v in years.items()),
        "folders": f1_rows, "folders_count": f1_n,
        "subfolders": f2_rows, "subfolders_count": f2_n,
        "largest": [{"path": p, "bytes": s, "type": c, "year": _year(m)}
                    for s, p, c, m in sorted(largest, reverse=True)],
        "tables": {"files": len(tables_seen), "opened": len(tables), "records": table_records,
                   "records_estimated": table_records_est, "fields": fields,
                   "list": sorted(tables, key=lambda t: -(t.get("records") or 0))[:200]},
        "databases": {"sqlite_files": len(databases), "records": db_records,
                      "list": databases[:100]},
        "personal_data": [{"kind": k, "columns": v, "files": pii_files[k]}
                          for k, v in pii.most_common()],
        "zips": zip_inside,
        "duplicates": dup,
        "cloud_only_files": cloud_only,
        "skipped": {"folders": skipped_dirs, "system_files": hidden_files,
                    "errors": dict(errors)},
    }
