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

from . import rules
from .crosswalk import Crosswalk

TABLE_EXTS = {".csv", ".xlsx", ".xlsm", ".txt", ".tsv"}

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
    else:
        enc, dialect = _read_text_file(path)
        with open(path, newline="", encoding=enc) as f:
            rows = [r for r in csv.reader(f, dialect)]
        header, body = _split_header(rows)
        if header is not None:
            yield None, header, body


def _split_header(rows):
    for i, r in enumerate(rows):
        if any(c not in (None, "") for c in r):
            header = ["" if c is None else str(c).strip() for c in r]
            width = len(header)
            body = []
            for row in rows[i + 1:]:
                row = list(row) + [None] * (width - len(row))
                body.append(row[:width] if len(row) > width else row)
            return header, body
    return None, []


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
                        if v not in (None, ""):
                            for w in rules._WORD.findall(str(v)):
                                w = w.lower().strip("'-")
                                if len(w) >= 2 and w not in _NAME_STOP:
                                    names.add(w)
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
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        # a date sitting in a column marked keep: Safe Harbor still allows only the year
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
        s = rules.scrub_patterns(s, rules.TEXT_PATTERNS, hits)
        return rules.scrub_names(s, names, hits)
    if rules.RE_DATE.fullmatch(s.strip()):
        # a lone date in a kept column: keep the year, as Safe Harbor allows
        hits["date"] = hits.get("date", 0) + 1
        return rules.to_year(s, header)
    return rules.scrub_patterns(s, rules.SAFETY_PATTERNS, hits)


def safe_filename(stem: str, names: set) -> str:
    stem = rules.scrub_patterns(stem, rules.TEXT_PATTERNS, {})
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
    out_ext = ".xlsx" if ext in (".xlsx", ".xlsm") else ".csv"
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
            out_header = [header[i] for i in keep_idx]
            rows = [r for r in rows if not all(c in (None, "") for c in r)]
            keep_rows = None
            if sample_rows is not None and len(rows) > sample_rows:
                keep_rows = set(random.Random(f"{seed}:{path}:{si}").sample(
                    range(len(rows)), sample_rows))
            profs = [ColumnProfile(h, actions.get(h, rules.KEEP)) for h in header]
            out_rows = []
            for ri, row in enumerate(rows):
                o = []
                for i, h in enumerate(header):
                    a = actions.get(h, rules.KEEP)
                    if a in (rules.REMOVE, rules.NAME):
                        profs[i].add(row[i])
                        continue
                    hh = res.hits.setdefault(h, {})
                    v = _cell(row[i], a, h, crosswalk, names, hh)
                    profs[i].add(v)
                    o.append(v)
                if keep_rows is None or ri in keep_rows:
                    out_rows.append(o)
            for h in header:
                res.columns[h] = actions.get(h, rules.KEEP)
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
