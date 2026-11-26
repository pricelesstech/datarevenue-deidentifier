"""Turn an inventory (deid.inventory.run_inventory) + the client's answers into one file:

  DataRevenue_Inventory_<Company>_<date>.zip
     data_inventory.html     visual report: quote inputs, types, years, folders, answers
     data_inventory.json     the same, machine readable (answers use the Snapshot keys)
     inventory_by_type.csv, inventory_by_extension.csv, inventory_by_year.csv,
     inventory_by_folder.csv, inventory_spreadsheets.csv

The full file list (with every file name) is written separately to the private folder and
is never put in the zip. With show_names=False, folder and file names are replaced with
neutral labels (Folder 1, Spreadsheet 3) because names can identify people.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import os
import zipfile

from . import __version__
from .inventory import VALUE_CATEGORIES
from .package import _CSS, _answer_rows, _e, load_questions, safe_company


def gb(n: int) -> str:
    if n >= 1e12:
        return f"{n / 1e12:,.2f} TB"
    if n >= 1e9:
        return f"{n / 1e9:,.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:,.1f} MB"
    if n >= 1e3:
        return f"{n / 1e3:,.0f} KB"
    return f"{n:,} bytes"


def _rel(path: str, roots: list[str]) -> str:
    for r in roots:
        if path == r:
            return os.path.basename(path)
        base = r.rstrip("\\/")
        if path.startswith(base + os.sep) or path.startswith(base + "/"):
            label = os.path.basename(base) or base.rstrip(":")
            return label + "/" + os.path.relpath(path, r).replace("\\", "/")
    return os.path.basename(path)


def shareable(inv: dict, show_names: bool) -> dict:
    """A copy of the inventory that is safe to send: no absolute paths, and with
    show_names=False no folder or file names at all."""
    roots = inv["roots"]
    out = json.loads(json.dumps(inv, default=str))
    ext = lambda p: os.path.splitext(p)[1].lower() or "no extension"  # noqa: E731
    if show_names:
        out["roots"] = [os.path.basename(r.rstrip("\\/")) or r for r in roots]
        for t in out["tables"]["list"] + out["databases"]["list"] + out["largest"]:
            t["path"] = _rel(t["path"], roots)
        return out
    out["roots"] = [f"Location {i}" for i in range(1, len(roots) + 1)]
    names = {}
    for i, f in enumerate(out["folders"], 1):
        names[f["name"]] = f["name"] if f["name"].startswith("(") else f"Folder {i}"
        f["name"] = names[f["name"]]
    counters: dict[str, int] = {}
    for f in out["subfolders"]:
        parent = names.get(f["parent"])
        if parent is None:  # its top-level folder was not in the top list
            parent = names.setdefault(f["parent"], f"Folder {len(names) + 1}")
        counters[parent] = counters.get(parent, 0) + 1
        f["name"] = f"{parent} / Subfolder {counters[parent]}"
        f["parent"] = parent
    for i, t in enumerate(out["tables"]["list"], 1):
        t["path"] = f"Spreadsheet {i} ({ext(t['path'])})"
    for i, t in enumerate(out["databases"]["list"], 1):
        t["path"] = f"Database {i} ({ext(t['path'])})"
    for i, t in enumerate(out["largest"], 1):
        t["path"] = f"File {i} ({ext(t['path'])})"
    return out


def quote_inputs(inv: dict) -> list[tuple[str, str]]:
    """The handful of numbers a quote is built from."""
    bt = {r["type"]: r for r in inv["by_type"]}

    def files(c):
        return bt.get(c, {}).get("files", 0)

    def size(c):
        return bt.get(c, {}).get("bytes", 0)

    value_bytes = sum(r["bytes"] for r in inv["by_type"] if r["type"] in VALUE_CATEGORIES)
    years = [y for y, _n, _b in inv["by_year"]]
    recs = inv["tables"]["records"] + inv["databases"]["records"]
    rows = [
        ("Total size scanned", gb(inv["total"]["bytes"])),
        ("Total files", f"{inv['total']['files']:,}"),
        ("Size of business data (excludes programs, system files, code, backups)",
         gb(value_bytes)),
        ("Records in spreadsheets and databases" + (" (estimated)" if
                                                    inv["tables"]["records_estimated"] else ""),
         f"{recs:,}"),
        ("Spreadsheet and data-table files", f"{files('Spreadsheets and data tables'):,} "
                                             f"({gb(size('Spreadsheets and data tables'))})"),
        ("Database files and backups", f"{files('Databases and database backups'):,} "
                                       f"({gb(size('Databases and database backups'))})"),
        ("Emails and mailboxes", f"{files('Emails and mailboxes'):,} files "
                                 f"({gb(size('Emails and mailboxes'))})"),
        ("Documents", f"{files('Documents'):,} ({gb(size('Documents'))})"),
        ("Photos and images", f"{files('Photos and images'):,} ({gb(size('Photos and images'))})"),
        ("Medical images (DICOM)", f"{files('Medical images (DICOM)'):,} "
                                   f"({gb(size('Medical images (DICOM)'))})"),
        ("Video", f"{files('Video'):,} files ({gb(size('Video'))})"),
        ("Audio and call recordings", f"{files('Audio and call recordings'):,} files "
                                      f"({gb(size('Audio and call recordings'))})"),
        ("Design, CAD and 3D files", f"{files('Design, CAD and 3D'):,} "
                                     f"({gb(size('Design, CAD and 3D'))})"),
        ("Years covered (by last-modified date)",
         f"{min(years)} to {max(years)}" if years else "n/a"),
        ("Likely duplicate copies", (f"{inv['duplicates']['extra_copies']:,} files "
                                     f"({gb(inv['duplicates']['extra_bytes'])})")
         if inv["duplicates"]["checked"] else "not checked"),
        ("Personal data present (from column names)",
         ", ".join(p["kind"] for p in inv["personal_data"]) or "none found in column names"),
    ]
    return [(k, v) for k, v in rows if not v.startswith("0 (") and not v.startswith("0 files")]


def _table(head, rows) -> str:
    h = "<table><tr>" + "".join(f"<th>{_e(x)}</th>" for x in head) + "</tr>"
    for r in rows:
        h += "<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>"
    return h + "</table>"


def _bar(frac: float) -> str:
    pct = max(0.0, min(100.0, frac * 100))
    return f"<div class='bar' style='width:140px'><i style='width:{pct:.1f}%'></i></div>"


def build_html(inv: dict, answers: dict, cat: dict, created: _dt.datetime,
               show_names: bool) -> str:
    company = answers.get("contact", {}).get("company") or "Client"
    tot = inv["total"]["bytes"] or 1
    H = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{_e(company)} data inventory</title><style>{_CSS}</style></head><body>"]
    H.append(f"<header><div class='wrap'><h1>{_e(company)}: Data Inventory</h1>"
             f"<p>Prepared {created:%B %d, %Y} with the Data Revenue De-Identifier. A count "
             f"of everything on the chosen drive or folders. No file contents were copied "
             f"or sent.</p></div></header><div class='wrap'>")
    if not inv["complete"]:
        H.append("<div class='note' style='border-color:#b3403f'><b>Stopped early.</b> "
                 "This inventory covers only part of the data.</div>")

    H.append("<h2>At a glance</h2><div class='cards'>")
    years = [y for y, _n, _b in inv["by_year"]]
    recs = inv["tables"]["records"] + inv["databases"]["records"]
    for big, small in [
        (gb(inv["total"]["bytes"]), "total size"),
        (f"{inv['total']['files']:,}", "files"),
        (f"{recs:,}", "records in spreadsheets and databases"
         + (" (estimated)" if inv["tables"]["records_estimated"] else "")),
        (f"{min(years)} to {max(years)}" if years else "n/a", "years covered"),
        (f"{len(inv['personal_data'])}", "kinds of personal data found"),
    ]:
        H.append(f"<div class='card'><b>{_e(big)}</b><span>{_e(small)}</span></div>")
    H.append("</div>")

    H.append("<h2>Quote inputs</h2>")
    H.append(_table(["Item", "Amount"], [(f"<span class='mute'>{_e(k)}</span>", _e(v))
                                         for k, v in quote_inputs(inv)]))

    H.append("<h2>What kind of data is here</h2>")
    H.append(_table(["Type", "Files", "Size", "Share of total", "Years"], [
        (f"<b>{_e(r['type'])}</b>", f"{r['files']:,}", gb(r["bytes"]),
         f"{r['bytes'] / tot:.1%}{_bar(r['bytes'] / tot)}",
         _e(f"{r['oldest_year']} to {r['newest_year']}" if r["oldest_year"] else "n/a"))
        for r in inv["by_type"]]))

    if inv["personal_data"]:
        H.append("<h2>Personal data in spreadsheets and databases</h2><p class='mute small'>"
                 "Judged from column names only. No values were read or reported.</p>")
        H.append(_table(["Kind of personal data", "Columns", "Files"], [
            (_e(p["kind"]), f"{p['columns']:,}", f"{p['files']:,}")
            for p in inv["personal_data"]]))

    ys = inv["by_year"]
    if ys:
        mx = max(b for _y, _n, b in ys) or 1
        H.append("<h2>Data by year</h2><p class='mute small'>By each file's last-modified "
                 "date. Shows how much data is added each year.</p>")
        H.append(_table(["Year", "Files", "Size", ""], [
            (str(y), f"{n:,}", gb(b), _bar(b / mx)) for y, n, b in reversed(ys)]))

    if inv["folders"]:
        label = "" if show_names else " (names hidden)"
        H.append(f"<h2>Largest folders{label}</h2>")
        H.append(_table(["Folder", "Files", "Size", "Main type", "Years"], [
            (_e(f["name"]), f"{f['files']:,}", gb(f["bytes"]), _e(f["main_type"] or ""),
             _e(f"{f['oldest_year']} to {f['newest_year']}" if f["oldest_year"] else "n/a"))
            for f in inv["folders"][:25]]))
        if inv["folders_count"] > 25:
            H.append(f"<p class='mute small'>{inv['folders_count']:,} top-level folders in "
                     f"all; the full list is in inventory_by_folder.csv.</p>")

    tl = inv["tables"]["list"]
    if tl:
        H.append("<h2>Largest spreadsheets by records</h2>")
        H.append(_table(["Spreadsheet", "Records", "Columns", "Size", "Personal data"], [
            (_e(t["path"]), (f"{t.get('records', 0):,}" + (" est." if t.get("estimated") else ""))
             if not t.get("error") else "could not open",
             f"{t.get('columns', 0):,}", gb(t["bytes"]),
             _e(", ".join(t.get("personal_data") or []) or "none found"))
            for t in tl[:25]]))
        if inv["tables"]["files"] > inv["tables"]["opened"]:
            H.append(f"<p class='mute small'>{inv['tables']['opened']:,} of "
                     f"{inv['tables']['files']:,} spreadsheets were opened (the largest); "
                     f"records in the rest are estimated from their size.</p>")

    if inv["databases"]["list"]:
        H.append("<h2>Databases</h2>")
        H.append(_table(["Database", "Tables", "Records", "Size", "Personal data"], [
            (_e(d["path"]), _e(d["tables"] if d["tables"] is not None else "n/a"),
             f"{d['records']:,}", gb(d["bytes"]),
             _e(", ".join(d.get("personal_data") or []) or "none found"))
            for d in inv["databases"]["list"][:25]]))

    z = inv["zips"]
    if z["zips"]:
        H.append("<h2>Inside zip files</h2><p class='mute small'>Read from each zip's index; "
                 "nothing was unpacked. Not added to the totals above.</p>")
        H.append(_table(["Item", "Amount"], [
            ("Zip files listed", f"{z['zips']:,}"),
            ("Files inside", f"{z['files']:,}"),
            ("Size when unpacked", gb(z["bytes"])),
            ("Main types inside", _e(", ".join(f"{k} ({gb(v)})" for k, v in
                                               list(z["by_type"].items())[:5]))),
        ]))

    if inv["largest"]:
        H.append("<h2>Largest files</h2>")
        H.append(_table(["File", "Type", "Size", "Year"], [
            (_e(f["path"]), _e(f["type"]), gb(f["bytes"]), _e(f["year"] or "n/a"))
            for f in inv["largest"][:15]]))

    rows = _answer_rows(answers, cat)
    if rows:
        H.append("<h2>Buyer questions (answered by the data owner)</h2>")
        H.append(_table(["Question", "Answer"], [(f"<span class='mute'>{_e(q)}</span>", _e(a))
                                                 for q, a in rows]))

    sk = inv["skipped"]
    errs = ", ".join(f"{v:,} {k}" for k, v in sk["errors"].items()) or "none"
    H.append("<h2>How this inventory was made</h2><div class='note small'>Run on the data "
             "owner's own computer. The program read each file's name, size, type and date "
             "from the folder listing; it opened spreadsheets and SQLite databases only to "
             "count records and read column names, and read zip indexes without unpacking. "
             "No file contents were copied or sent. "
             + ("Folder and file names are shown as given. " if show_names else
                "Folder and file names are hidden because they can identify people. ")
             + (f"{inv.get('cloud_only_files', 0):,} files are stored online only (OneDrive, "
                "Dropbox or similar); they are counted but were not opened, so their records "
                "are not counted. " if inv.get("cloud_only_files") else "")
             + f"Skipped: {sk['folders']:,} system or hidden folders, "
             f"{sk['system_files']:,} system files. Could not read: {_e(errs)}. "
             "Duplicates are likely copies (same size and same content at the start, middle "
             "and end). Live databases on a server (SQL Server, MySQL, PostgreSQL and "
             f"others) are measured with the Data Revenue Scanner. Scan time: "
             f"{inv['seconds'] / 60:,.1f} minutes.</div>")
    H.append(f"<footer>Data Revenue De-Identifier v{_e(__version__)}, inventory mode"
             f"</footer></div></body></html>")
    return "".join(H)


def _csv(rows, head) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return buf.getvalue()


def build_inventory_package(inv: dict, answers: dict, out_base: str,
                            show_names: bool = False) -> dict:
    cat = load_questions()
    created = _dt.datetime.now()
    safe = shareable(inv, show_names)
    share = os.path.join(out_base, "Inventory contents")
    os.makedirs(share, exist_ok=True)
    files = {
        "data_inventory.html": build_html(safe, answers, cat, created, show_names),
        "data_inventory.json": json.dumps({
            "tool_version": __version__, "kind": "inventory",
            "created": created.isoformat(timespec="seconds"),
            "question_set": cat.get("version"), "answers": answers,
            "names_shown": show_names, "inventory": safe}, indent=1, default=str),
        "inventory_by_type.csv": _csv([[r["type"], r["files"], r["bytes"], r["oldest_year"],
                                        r["newest_year"]] for r in safe["by_type"]],
                                      ["type", "files", "bytes", "oldest_year", "newest_year"]),
        "inventory_by_extension.csv": _csv(safe["by_extension"], ["extension", "files", "bytes"]),
        "inventory_by_year.csv": _csv(safe["by_year"], ["year", "files", "bytes"]),
        "inventory_by_folder.csv": _csv(
            [[f["name"], f["files"], f["bytes"], f["main_type"], f["oldest_year"],
              f["newest_year"]] for f in safe["folders"] + safe["subfolders"]],
            ["folder", "files", "bytes", "main_type", "oldest_year", "newest_year"]),
        "inventory_spreadsheets.csv": _csv(
            [[t["path"], t.get("records"), t.get("estimated"), t.get("columns"), t["bytes"],
              "; ".join(t.get("personal_data") or [])] for t in safe["tables"]["list"]],
            ["spreadsheet", "records", "estimated", "columns", "bytes", "personal_data"]),
    }
    for name, text in files.items():
        with open(os.path.join(share, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    company = safe_company(answers.get("contact", {}).get("company", ""))
    zip_path = os.path.join(out_base,
                            f"DataRevenue_Inventory_{company}_{created:%Y-%m-%d_%H%M}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(os.path.join(share, name), name)
    return {"zip": zip_path, "html": os.path.join(share, "data_inventory.html"),
            "size_mb": round(os.path.getsize(zip_path) / 1e6, 2)}
