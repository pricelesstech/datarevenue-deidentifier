"""Spreadsheet de-identification (CSV, XLSX, XLSM). Two passes over each file:

1. collect: names from NAME columns (to erase them from free text anywhere) and patient
   IDs from CODE columns (so images processed later get the same study code);
2. write: transform every cell according to its column's action.
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import re
from dataclasses import dataclass, field

import warnings

from . import rules
from .crosswalk import Crosswalk

# workbook page headers/footers we never read; openpyxl warns about odd ones
warnings.filterwarnings("ignore", message="Cannot parse header or footer", module="openpyxl")
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

TABLE_EXTS = {".csv", ".xlsx", ".xlsm", ".xls", ".txt", ".tsv"}

# words that appear in name fields but are not names
_NAME_STOP = {"jr", "sr", "ii", "iii", "iv", "dr", "md", "dds", "dmd", "do", "rn", "np",
              "pa", "phd", "mr", "mrs", "ms", "miss", "the", "and", "of", "de", "la", "van",
              "von", "del", "da", "di", "le", "st", "unknown", "none", "null", "na", "n",
              "test", "patient", "baby", "boy", "girl"}


def _read_text_file(path: str):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                sample = f.read(65536)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if path.lower().endswith(".tsv") else csv.excel
    return enc, dialect


def iter_sheets(path: str):
    """Yield (sheet_name, header, rows) where rows is a list of lists."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                header, body = _split_header(rows)
                if header is not None:
                    yield ws.title, header, body
        finally:
            wb.close()
    elif ext == ".xls":  # Excel 97-2003
        import xlrd

        book = xlrd.open_workbook(path, on_demand=True)
        try:
            for si in range(book.nsheets):
                sh = book.sheet_by_index(si)
                rows = []
                for r in range(sh.nrows):
                    row = []
                    for c in range(sh.ncols):
                        cell = sh.cell(r, c)
                        v = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            try:
                                v = xlrd.xldate.xldate_as_datetime(v, book.datemode)
                            except Exception:  # noqa: BLE001
                                pass
                        elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                            v = None
                        elif cell.ctype == xlrd.XL_CELL_NUMBER and float(v).is_integer():
                            v = int(v)
                        row.append(v)
                    rows.append(row)
                header, body = _split_header(rows)
                if header is not None:
                    yield sh.name, header, body
                book.unload_sheet(si)
        finally:
            book.release_resources()
    else:
        enc, dialect = _read_text_file(path)
        with open(path, newline="", encoding=enc) as f:
            rows = [r for r in csv.reader(f, dialect)]
        header, body = _split_header(rows)
        if header is not None:
            yield None, header, body


def _split_header(rows):
    """Find the header row. Business spreadsheets often start with a title ("RFI LOG",
    "Job 2201") or a few form fields above the real column headers, so the header is the
    first of the top 20 rows that is at least half as wide as the widest row and mostly
    text; if none qualifies, the first non-empty row."""
    def filled(r):
        return [c for c in r if c not in (None, "")]

    top = rows[:20]
    widest = max((len(filled(r)) for r in top), default=0)
    pick = None
    for i, r in enumerate(top):
        f = filled(r)
        if widest >= 2 and len(f) >= max(2, widest / 2) and \
                sum(isinstance(c, str) for c in f) >= 0.6 * len(f):
            pick = i
            break
    if pick is None:
        pick = next((i for i, r in enumerate(rows) if filled(r)), None)
    if pick is None:
        return None, []
    r = rows[pick]
    last = max((j for j, c in enumerate(r) if c not in (None, "")), default=-1)
    header = ["" if c is None else str(c).strip() for c in r[:last + 1]]
    width = len(header)
    body = []
    for row in rows[pick + 1:]:
        row = list(row) + [None] * (width - len(row))
        body.append(row[:width] if len(row) > width else row)
    return header, body


def scan_headers(paths: list[str]) -> dict[str, dict]:
    """Return {header: {"action": suggested, "files": n}} across all tables."""
    seen: dict[str, dict] = {}
    values: dict[str, list] = {}
    for p in paths:
        try:
            for _sheet, header, rows in iter_sheets(p):
                for i, h in enumerate(header):
                    d = seen.setdefault(h, {"action": rules.KEEP, "files": 0})
                    d["files"] += 1
                    vs = values.setdefault(h, [])
                    for r in rows[:300]:
                        if len(vs) >= 300:
                            break
                        vs.append(r[i])
        except Exception:  # unreadable file is reported at run time
            continue
    for h, d in seen.items():
        d["action"] = rules.classify_values(h, values.get(h, []))
        d["action_business"] = rules.for_business(h, d["action"])
    return seen


@dataclass
class TableResult:
    source: str
    output: str
    sheets: int = 0
    rows: int = 0  # rows written
    source_rows: int = 0  # rows in the original
    columns: dict = field(default_factory=dict)  # header -> action
    profiles: list = field(default_factory=list)  # one {"sheet", "rows", "columns"} per sheet
    hits: dict = field(default_factory=dict)  # header -> {pattern: count}
    error: str | None = None


def collect(paths, actions: dict, crosswalk: Crosswalk) -> set[str]:
    names: set[str] = set()
    for p in paths:
        try:
            for _sheet, header, rows in iter_sheets(p):
                idx_name = [i for i, h in enumerate(header) if actions.get(h) == rules.NAME]
                idx_code = [i for i, h in enumerate(header) if actions.get(h) == rules.CODE]
                for row in rows:
                    for i in idx_name:
                        v = row[i]
                        if v in (None, ""):
                            continue
                        words = rules._WORD.findall(str(v))
                        if len(words) > 5:  # a sentence, not a name: harvest nothing
                            continue
                        for w in words:
                            lw = w.lower().strip("'-")
                            if len(lw) >= 2 and lw not in _NAME_STOP \
                                    and lw not in rules.COMMON_WORDS:
                                names.add(lw)
                    for i in idx_code:
                        crosswalk.code_for(row[i])
        except Exception:
            continue
    return names


def _cell(value, action: str, header: str, crosswalk: Crosswalk, names: set, hits: dict):
    if action == rules.CODE:
        return crosswalk.code_for(value)
    if action == rules.YEAR:
        return rules.to_year(value, header)
    if action == rules.ZIP3:
        return rules.to_zip3(value)
    if action == rules.AGE:
        return rules.to_age(value)
    if value is None or value == "":
        return value
    keep_dates = rules.KEEP_DATES and not rules.is_birth_header(header)
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        if keep_dates:  # business mode: a business date keeps its value
            return value
        # health mode: Safe Harbor allows only the year
        hits["date"] = hits.get("date", 0) + 1
        return value.year if not isinstance(value, _dt.time) else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if action == rules.TEXT and rules.RE_LONGNUM.fullmatch(str(int(value))):
            hits["long number"] = hits.get("long number", 0) + 1
            return "[NUMBER]"
        if (isinstance(value, int) or float(value).is_integer()) and 9 <= len(
            str(abs(int(value)))
        ) <= 11:
            # 9 to 11 digit whole numbers are how Excel stores SSNs and phone numbers
            hits["number like SSN/phone"] = hits.get("number like SSN/phone", 0) + 1
            return "[NUMBER]"
        return value
    s = str(value)
    if action == rules.TEXT:
        s = rules.scrub_patterns(s, rules.text_patterns(), hits)
        return rules.scrub_names(s, names, hits)
    if rules.RE_DATE.fullmatch(s.strip()):
        if keep_dates:
            return s
        # a lone date in a kept column: keep the year, as Safe Harbor allows
        hits["date"] = hits.get("date", 0) + 1
        return rules.to_year(s, header)
    s = rules.scrub_patterns(s, rules.safety_patterns(), hits)
    return rules.scrub_names(s, names, hits) if names else s


def _clean_header(h: str, names: set) -> str:
    s = rules.scrub_patterns(str(h), rules.text_patterns(), {})
    if names:  # capitalized name words only: "Mr. Okafor" yes, "Contact" no
        s = re.sub(r"[A-Z][A-Za-z'\-]+",
                   lambda m: "[NAME]" if m.group(0).lower() in names else m.group(0), s)
    return s


def safe_filename(stem: str, names: set) -> str:
    stem = rules.scrub_patterns(stem, rules.text_patterns(), {})
    stem = rules.scrub_names(stem, names, {})
    stem = re.sub(r"[\[\]]", "", stem)
    stem = re.sub(r"[^A-Za-z0-9 _\-.]+", "_", stem).strip(" ._") or "table"
    return stem[:80]


def write_one(path: str, out_dir: str, actions: dict, crosswalk: Crosswalk, names: set,
              used_names: set, sample_rows: int | None = None, seed: int = 0
              ) -> TableResult:
    """De-identify one file. With sample_rows, every row is still transformed (so the
    profile covers the whole table) but only a random sample of rows is written."""
    import random

    from .profile import ColumnProfile

    ext = os.path.splitext(path)[1].lower()
    stem = safe_filename(os.path.splitext(os.path.basename(path))[0], names)
    out_ext = ".xlsx" if ext in (".xlsx", ".xlsm", ".xls") else ".csv"
    out_name = stem + "_deidentified" + out_ext
    n = 2
    while out_name.lower() in used_names:
        out_name = f"{stem}_deidentified_{n}{out_ext}"
        n += 1
    used_names.add(out_name.lower())
    out_path = os.path.join(out_dir, out_name)
    res = TableResult(source=path, output=out_path)
    try:
        sheets = list(iter_sheets(path))
        if out_ext == ".xlsx":
            from openpyxl import Workbook

            wb = Workbook(write_only=True)
        for si, (sheet, header, rows) in enumerate(sheets):
            keep_idx = [i for i, h in enumerate(header)
                        if actions.get(h, rules.KEEP) not in (rules.REMOVE, rules.NAME)]
            # headers go out too (in the file, the report and the summary), so they are
            # cleaned like any text: a form's "Attention: Mr. Smith" row can be the header
            clean = {h: _clean_header(h, names) for h in header}
            out_header = [clean[header[i]] for i in keep_idx]
            rows = [r for r in rows if not all(c in (None, "") for c in r)]
            keep_rows = None
            if sample_rows is not None and len(rows) > sample_rows:
                keep_rows = set(random.Random(f"{seed}:{path}:{si}").sample(
                    range(len(rows)), sample_rows))
            profs = [ColumnProfile(clean[h], actions.get(h, rules.KEEP)) for h in header]
            out_rows = []
            for ri, row in enumerate(rows):
                o = []
                for i, h in enumerate(header):
                    a = actions.get(h, rules.KEEP)
                    if a in (rules.REMOVE, rules.NAME):
                        profs[i].add(row[i])
                        continue
                    hh = res.hits.setdefault(clean[h], {})
                    v = _cell(row[i], a, h, crosswalk, names, hh)
                    profs[i].add(v)
                    o.append(v)
                if keep_rows is None or ri in keep_rows:
                    out_rows.append(o)
            for h in header:
                res.columns[clean[h]] = actions.get(h, rules.KEEP)
            res.rows += len(out_rows)
            res.source_rows += len(rows)
            res.sheets += 1
            res.profiles.append({"sheet": safe_filename(sheet, names) if sheet else None,
                                 "rows": len(rows),
                                 "columns": [p.to_dict() for p in profs]})
            if out_ext == ".xlsx":
                title = safe_filename(sheet or f"Sheet{si + 1}", names)[:31] or f"Sheet{si + 1}"
                ws = wb.create_sheet(title=title)
                ws.append(out_header)
                for r in out_rows:
                    ws.append(r)
            else:
                with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(out_header)
                    w.writerows(out_rows)
        if out_ext == ".xlsx":
            if not sheets:
                wb.create_sheet("Sheet1")
            wb.save(out_path)
        res.hits = {h: v for h, v in res.hits.items() if v}
    except Exception as e:  # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"
        if os.path.exists(out_path):
            os.remove(out_path)
    return res
