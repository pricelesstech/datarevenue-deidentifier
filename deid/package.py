"""Turn a finished run + the client's answers into one file to send:

  DataRevenue_Sample_<Company>_<date>.zip
     data_summary.html   visual summary: answers, data at a glance, every field, images
     data_summary.json   the same, machine readable (answers use the Snapshot keys)
     tables/  images/    the de-identified sample
     DEIDENTIFICATION_REPORT.txt
"""
from __future__ import annotations

import base64
import datetime as _dt
import html
import io
import json
import os
import re
import zipfile

from . import __version__

# The questions AI and data buyers ask first (see "source" in the file). Keys shared with
# the Data Snapshot use the same option keys.
QUESTIONS_PATH = os.path.join(os.path.dirname(__file__), "ui", "buyer_questions.json")


def load_questions() -> dict:
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _label_maps(cat: dict):
    """key -> (prompt, {option_key: label}) for every question."""
    out = {}
    card = cat["dataset_card"]
    for q in [card.get("category_question"), *card["questions"], *cat["company_questions"]]:
        if not q:
            continue
        out[q["key"]] = (q["prompt"], {o["key"]: o["label"] for o in q.get("options", [])})
    out["name"] = (card["name_question"]["prompt"], {})
    return out


def _answer_rows(answers: dict, cat: dict) -> list[tuple[str, str]]:
    labels = _label_maps(cat)
    rows = []

    def fmt(key, val):
        _prompt, opts = labels.get(key, (key, {}))
        if isinstance(val, list):
            return ", ".join(opts.get(v, v) for v in val) or "(none)"
        return opts.get(val, val) if val not in (None, "") else "(not answered)"

    for ds in answers.get("datasets", []):
        for k in ["name", "category", *[q["key"] for q in cat["dataset_card"]["questions"]]]:
            if k in ds:
                rows.append((labels.get(k, (k,))[0], fmt(k, ds[k])))
    for q in cat["company_questions"]:
        if q["key"] in answers.get("company", {}):
            rows.append((q["prompt"], fmt(q["key"], answers["company"][q["key"]])))
    if answers.get("notes"):
        rows.append(("Anything else we should know", answers["notes"]))
    return rows


def safe_company(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
    return s[:40] or "Client"


def _thumbs(share_dir: str, n: int = 6) -> list[str]:
    from PIL import Image

    out = []
    img_dir = os.path.join(share_dir, "images")
    paths = []
    for root, _d, files in os.walk(img_dir):
        paths += [os.path.join(root, f) for f in sorted(files)]
    for p in paths[:n]:
        try:
            if p.lower().endswith(".dcm"):
                import numpy as np
                import pydicom

                from .images import _to_uint8

                ds = pydicom.dcmread(p)
                arr = ds.pixel_array
                if int(getattr(ds, "NumberOfFrames", 1) or 1) > 1:
                    arr = arr[0]
                inv = str(getattr(ds, "PhotometricInterpretation", "")) == "MONOCHROME1"
                im = Image.fromarray(_to_uint8(arr, invert=inv) if arr.ndim == 2
                                     else np.asarray(arr, dtype=np.uint8))
            else:
                im = Image.open(p)
                im = im.convert("L" if im.mode.startswith(("I", "L", "1")) else "RGB")
            im.thumbnail((420, 260))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=80)
            out.append(base64.b64encode(buf.getvalue()).decode())
        except Exception:  # noqa: BLE001
            continue
    return out


_CSS = """
:root{--navy:#0B2A4A;--teal:#0E8C8C;--ink:#1d2733;--mute:#5f6b78;--line:#e3e8ee;--bg:#f6f8fa}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
.wrap{max-width:1060px;margin:0 auto;padding:32px 20px 60px}
header{background:var(--navy);color:#fff;padding:28px 0}header .wrap{padding-top:0;padding-bottom:0}
h1{margin:0;font-size:26px}header p{margin:4px 0 0;color:#b9c7d6}
h2{color:var(--navy);font-size:19px;margin:34px 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card b{display:block;font-size:24px;color:var(--navy)}.card span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
th{background:#eef3f7;color:var(--navy);font-weight:600}
tr:last-child td{border-bottom:none}
.bar{height:6px;background:#e6edf2;border-radius:3px;margin-top:4px;width:90px}.bar i{display:block;height:6px;background:var(--teal);border-radius:3px}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:#e7f4f4;color:var(--teal)}
.tag.rm{background:#f3eded;color:#9a3b3b}
.mute{color:var(--mute)}.small{font-size:13px}
.thumbs{display:flex;flex-wrap:wrap;gap:10px}.thumbs img{border-radius:8px;border:1px solid var(--line);max-width:100%}
.note{background:#fff;border-left:4px solid var(--teal);padding:12px 16px;border-radius:6px}
footer{color:var(--mute);font-size:12px;margin-top:40px}
"""


def _e(x) -> str:
    return html.escape(str(x))


def build_html(summary: dict, answers: dict, cat: dict, created: _dt.datetime) -> str:
    contact = answers.get("contact", {})
    company = contact.get("company") or "Client"
    ip = summary["image_profile"]
    years = set()
    fields = 0
    for t in summary["table_profiles"]:
        for sh in t["sheets"]:
            for c in sh["columns"]:
                fields += 1
                birth = re.search(r"(?i)birth|dob|born", c["name"])
                if c.get("kind") == "year" and "min" in c and not birth:
                    years.update([c["min"], c["max"]])
    if ip.get("years"):
        years.update(ip["years"])
    dp = summary.get("document_profile") or {}
    if dp.get("years"):
        years.update(dp["years"])
    span = f"{min(years)} to {max(years)}" if years else "n/a"

    H = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{_e(company)} data summary</title><style>{_CSS}</style></head><body>"]
    H.append(f"<header><div class='wrap'><h1>{_e(company)}: Data Sample Summary</h1>"
             f"<p>Prepared {created:%B %d, %Y} with the Data Revenue De-Identifier. "
             f"All personal identifiers removed (HIPAA Safe Harbor method).</p>"
             f"</div></header><div class='wrap'>")
    H.append("<h2>At a glance</h2><div class='cards'>")
    glance = [
        (f"{summary['table_source_rows']:,}", "spreadsheet records in the full data set"),
        (f"{summary['table_rows']:,}", "spreadsheet records in this sample"),
        (f"{fields:,}", "spreadsheet fields"),
    ]
    if summary.get("documents_total"):
        glance += [(f"{summary['documents_total']:,}", "documents and emails in the full set"),
                   (f"{summary.get('documents_written', 0):,}", "documents and emails in this sample")]
    if summary["images_total"]:
        glance += [(f"{summary['images_total']:,}", "images in the full data set"),
                   (f"{summary['images_ok']:,}", "images in this sample")]
    glance.append((span, "years covered"))
    for big, small in glance:
        if big in ("0", "") and "years" not in small:
            continue
        H.append(f"<div class='card'><b>{_e(big)}</b><span>{_e(small)}</span></div>")
    H.append("</div>")

    rows = _answer_rows(answers, cat)
    if rows:
        H.append("<h2>Buyer questions (answered by the data owner)</h2><table>")
        for q, a in rows:
            H.append(f"<tr><td class='mute' style='width:45%'>{_e(q)}</td><td>{_e(a)}</td></tr>")
        H.append("</table>")

    for t in summary["table_profiles"]:
        for sh in t["sheets"]:
            title = t["file"] + (f" / {sh['sheet']}" if sh.get("sheet") else "")
            H.append(f"<h2>Fields in {_e(title)}</h2>"
                     f"<p class='mute small'>{sh['rows']:,} records. Values shared by fewer "
                     f"than 5 records are never shown.</p>")
            H.append("<table><tr><th>Field</th><th>Type</th><th>Filled</th><th>Distinct</th>"
                     "<th>Range / common values</th></tr>")
            for c in sh["columns"]:
                rm = c["kind"] == "removed"
                kind = ("<span class='tag rm'>removed</span>" if rm
                        else f"<span class='tag'>{_e(c['kind'])}</span>")
                detail = ""
                if "min" in c:
                    detail = f"{_e(c['min'])} to {_e(c['max'])}"
                    if "mean" in c:
                        detail += f" <span class='mute'>(avg {_e(c['mean'])})</span>"
                elif c.get("top_values"):
                    detail = ", ".join(f"{_e(v)} <span class='mute'>({n:,})</span>"
                                       for v, n in c["top_values"][:5])
                elif "avg_length" in c:
                    detail = f"<span class='mute'>about {c['avg_length']} characters</span>"
                elif rm:
                    detail = "<span class='mute'>identifier: present in source, removed</span>"
                pct = c["filled_pct"]
                H.append(f"<tr><td><b>{_e(c['name'])}</b></td><td>{kind}</td>"
                         f"<td>{pct}%<div class='bar'><i style='width:{min(pct, 100)}%'></i>"
                         f"</div></td><td>{_e(c.get('distinct', ''))}</td><td>{detail}</td></tr>")
            H.append("</table>")

    if summary.get("documents_total"):
        H.append("<h2>Documents and emails</h2><table>")
        removed = dp.get("removed") or {}
        for k, v in [
            ("File types (full data set)", ", ".join(f"{a}: {b:,}" for a, b in
                                                     (dp.get("total_by_type") or {}).items())),
            ("In this sample", ", ".join(f"{a}: {b:,}" for a, b in
                                         (dp.get("kinds_in_sample") or {}).items()) or "n/a"),
            ("Emails in sample", f"{dp.get('emails', 0):,} "
                                 f"(attachments read: {dp.get('attachments_read', 0):,})"),
            ("Words of text in sample", f"{dp.get('words', 0):,}"),
            ("People replaced with labels", f"{dp.get('people', 0):,} (Person 1, Person 2, ... "
                                            f"used the same way in every document)"),
            ("Personal details removed", ", ".join(f"{b:,} {a}" for a, b in sorted(
                removed.items(), key=lambda kv: -kv[1])) or "none found"),
            ("Scans or drawings with no text layer", f"{dp.get('no_text', 0):,} "
                                                     f"(not included)"),
        ]:
            H.append(f"<tr><td class='mute' style='width:45%'>{_e(k)}</td><td>{_e(v)}</td></tr>")
        H.append("</table>")
        for title, kind, text in dp.get("excerpts") or []:
            H.append(f"<h2 style='font-size:16px'>Excerpt: {_e(title)} "
                     f"<span class='tag'>{_e(kind)}</span></h2>"
                     f"<pre style='white-space:pre-wrap;background:#fff;border:1px solid "
                     f"var(--line);border-radius:10px;padding:14px;font-size:13px'>"
                     f"{_e(text)}{'...' if len(text) >= 900 else ''}</pre>")

    if summary["images_total"]:
        H.append("<h2>Images</h2><table>")
        for k, v in [
            ("Formats (full data set)", ", ".join(f"{a}: {b:,}" for a, b in
                                                  ip["total_by_format"].items())),
            ("Imaging type (sample)", ", ".join(f"{a} ({b})" for a, b in
                                                ip["modalities"].items()) or "n/a"),
            ("Equipment makers (sample)", ", ".join(ip["manufacturers"]) or "n/a"),
            ("Typical size (sample)", ip.get("typical_size") or "n/a"),
            ("Bit depth (sample)", ", ".join(ip["bit_depths"]) or "n/a"),
            ("Printed labels removed", f"{summary['images_with_text']:,} of "
                                       f"{summary['images_ok']:,} sample images"),
        ]:
            H.append(f"<tr><td class='mute' style='width:45%'>{_e(k)}</td><td>{_e(v)}</td></tr>")
        H.append("</table>")
        th = _thumbs(summary["share_dir"])
        if th:
            H.append("<h2>Sample images (de-identified)</h2><div class='thumbs'>")
            H += [f"<img alt='sample image' src='data:image/jpeg;base64,{b}'>" for b in th]
            H.append("</div>")

    H.append("<h2>How this sample was made</h2><div class='note small'>Created on the data "
             "owner's own computer; the original files were never uploaded. Names, "
             "addresses, phone "
             "numbers, emails and ID numbers were removed; customer, patient or account "
             "numbers were replaced with random study codes; people in documents and emails "
             "were replaced with labels (Person 1, Person 2); "
             + ("business dates were kept and dates of birth cut to the year; "
                if summary.get("keep_dates") else "dates were reduced to the year; ")
             + "ZIP codes to 3 digits; text printed on images was found and blacked out; all "
             "image metadata was removed. The key that links study codes back to real people "
             "stays with the data owner. Full details are in DEIDENTIFICATION_REPORT.txt.</div>")
    H.append(f"<footer>Data Revenue De-Identifier v{_e(__version__)}</footer></div></body></html>")
    return "".join(H)


def build_package(summary: dict, answers: dict, out_base: str) -> dict:
    cat = load_questions()
    created = _dt.datetime.now()
    share = summary["share_dir"]
    html_text = build_html(summary, answers, cat, created)
    with open(os.path.join(share, "data_summary.html"), "w", encoding="utf-8") as f:
        f.write(html_text)
    payload = {
        "tool_version": __version__,
        "created": created.isoformat(timespec="seconds"),
        "question_set": cat.get("version"),
        "answers": answers,
        "summary": {k: v for k, v in summary.items()
                    if k not in ("share_dir", "private_dir")},
        "mode": "business (business dates kept)" if summary.get("keep_dates")
        else "health (dates cut to the year)",
    }
    with open(os.path.join(share, "data_summary.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, default=str)
    company = safe_company(answers.get("contact", {}).get("company", ""))
    zip_path = os.path.join(out_base, f"DataRevenue_Sample_{company}_{created:%Y-%m-%d_%H%M}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _d, files in os.walk(share):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, share))
    return {"zip": zip_path, "html": os.path.join(share, "data_summary.html"),
            "size_mb": round(os.path.getsize(zip_path) / 1e6, 1)}
