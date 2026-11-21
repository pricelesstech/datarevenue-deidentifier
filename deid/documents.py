"""Documents and emails: read the text out of each file, then write a de-identified text
version. Pure Python only (it also runs in the browser):

  .msg  Outlook email (olefile)          .eml  standard email (stdlib)
  .pdf  text layer (pypdf)               .docx .pptx .odt  Office/OpenDocument XML
  .doc  Word 97-2003 (olefile)           .rtf  Rich Text
  .htm/.html

Originals are never copied: only their scrubbed text goes out, so hidden metadata
(authors, tracked changes, comments, embedded images) never leaves. Attachments inside
emails are read too and added to the email's text. PDFs with no text layer (scans,
drawings) are not output: they are listed for review, because their text cannot be
checked.
"""
from __future__ import annotations

import datetime as _dt
import email
import email.policy
import email.utils
import html
import io
import os
import re
import struct
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser

import logging

logging.getLogger("pypdf").setLevel(logging.ERROR)  # damaged PDFs log a lot; we only need text

DOC_EXTS = {".msg", ".eml", ".pdf", ".docx", ".docm", ".doc", ".odt", ".pptx", ".rtf",
            ".htm", ".html"}
MAX_PDF_PAGES = 300
MAX_TEXT = 2_000_000  # characters kept per document


@dataclass
class Doc:
    kind: str                       # "Email", "PDF", "Word document", ...
    text: str = ""
    subject: str = ""
    sender: str = ""
    to: str = ""
    cc: str = ""
    date: _dt.datetime | None = None
    attachments: list = field(default_factory=list)  # [(name, Doc | None, note)]
    pages: int = 0
    note: str = ""                  # why text is missing or cut
    scanned: bool = False


# ── helpers ──
class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table",
             "blockquote", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "td":
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_text(s: str) -> str:
    p = _HTMLText()
    try:
        p.feed(s)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", s)
    return _tidy("".join(p.out))


def _tidy(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:MAX_TEXT]


def rtf_to_text(rtf: str) -> str:
    """Plain text from RTF, good enough for de-identification and reading."""
    if "\\fromhtml" in rtf[:500]:  # HTML wrapped in RTF (Outlook): keep the HTML parts
        parts = re.findall(r"\{\\\*\\htmltag\d*\s?([^{}]*)\}|([^{}\\]+)", rtf)
        return html_to_text("".join(a or b for a, b in parts))
    out, i, depth, skip_at = [], 0, 0, None
    while i < len(rtf):
        c = rtf[i]
        if c == "{":
            depth += 1
            if rtf.startswith("{\\*", i) and skip_at is None:
                skip_at = depth
            i += 1
        elif c == "}":
            if skip_at == depth:
                skip_at = None
            depth -= 1
            i += 1
        elif c == "\\":
            m = re.match(r"\\([a-z]+)(-?\d+)? ?|\\'([0-9a-f]{2})|\\(.)", rtf[i:], re.I)
            if not m:
                i += 1
                continue
            i += m.end()
            if skip_at is not None:
                continue
            word, num, hexc, sym = m.groups()
            if hexc:
                out.append(bytes([int(hexc, 16)]).decode("cp1252", "replace"))
            elif sym:
                out.append(sym if sym in "\\{}" else ("\n" if sym == "\n" else ""))
            elif word in ("par", "line", "row"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
            elif word == "u" and num:
                out.append(chr(int(num) % 65536))
                if i < len(rtf) and rtf[i] == "?":
                    i += 1
            elif word in ("fonttbl", "colortbl", "stylesheet", "info", "pict", "header",
                          "footer"):
                skip_at = depth
        else:
            if skip_at is None and c not in "\r\n":
                out.append(c)
            i += 1
    return _tidy("".join(out))


def _lzfu(data: bytes) -> bytes:
    """Decompress Outlook compressed RTF (MS-OXRTFCP)."""
    if len(data) < 16:
        return b""
    comp_size, raw_size, magic = struct.unpack_from("<III", data, 0)
    body = data[16:]
    if magic == 0x414C454D:  # "MELA": stored uncompressed
        return body[:raw_size]
    pre = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss "
           b"\\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New RomanCourier"
           b"{\\colortbl\\red0\\green0\\blue0\r\n\\par \\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx")
    dic = bytearray(4096)
    dic[:len(pre)] = pre
    w = len(pre)
    out = bytearray()
    i = 0
    while i < len(body):
        flags = body[i]
        i += 1
        for bit in range(8):
            if i >= len(body):
                break
            if flags & (1 << bit):
                if i + 1 >= len(body):
                    return bytes(out)
                ref = (body[i] << 8) | body[i + 1]
                i += 2
                off, ln = ref >> 4, (ref & 0xF) + 2
                if off == w % 4096:
                    return bytes(out)
                for k in range(ln):
                    b = dic[(off + k) % 4096]
                    out.append(b)
                    dic[w % 4096] = b
                    w += 1
            else:
                b = body[i]
                i += 1
                out.append(b)
                dic[w % 4096] = b
                w += 1
    return bytes(out[:raw_size])


# ── Office XML (docx, pptx, odt) ──
def _xml_text(data: bytes, part_rx: str, para_tags: tuple, text_tags: tuple) -> str:
    import xml.etree.ElementTree as ET

    z = zipfile.ZipFile(io.BytesIO(data))
    parts = sorted((n for n in z.namelist() if re.fullmatch(part_rx, n)),
                   key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    out = []
    for name in parts:
        root = ET.fromstring(z.read(name))
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag in para_tags:
                out.append("\n")
            elif tag in ("tab",):
                out.append("\t")
            if tag in text_tags and el.text:
                out.append(el.text)
            if tag in ("s",) and el.tail is None:
                out.append(" ")
            if tag in ("span", "a", "s", "tab", "line-break") and el.tail:
                out.append(el.tail)
        out.append("\n\n")
    return _tidy("".join(out))


def _docx(data):
    return Doc("Word document", _xml_text(
        data, r"word/(document|footnotes|endnotes|header\d*|footer\d*)\.xml", ("p", "br", "tr"),
        ("t",)))


def _pptx(data):
    return Doc("PowerPoint", _xml_text(data, r"ppt/slides/slide\d+\.xml", ("p",), ("t",)))


def _odt(data):
    return Doc("OpenDocument", _xml_text(data, r"content\.xml", ("p", "h", "line-break"),
                                         ("p", "h", "span", "a")))


# ── Word 97-2003 ──
def _doc(data):
    import olefile

    ole = olefile.OleFileIO(io.BytesIO(data))
    wd = ole.openstream("WordDocument").read()
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    if flags & 0x0100:
        return Doc("Word document (old format)", "", note="password protected")
    tbl = ole.openstream("1Table" if flags & 0x0200 else "0Table").read()
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
    clx = tbl[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:  # Prc entries come first
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ValueError("no piece table")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    out = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
        count = cps[k + 1] - cps[k]
        if fc & 0x40000000:
            start = (fc & 0x3FFFFFFF) // 2
            out.append(wd[start:start + count].decode("cp1252", "replace"))
        else:
            out.append(wd[fc:fc + 2 * count].decode("utf-16-le", "replace"))
    t = "".join(out)
    t = re.sub("\x13[^\x13\x14\x15]*\x14?", "", t).replace("\x15", "")  # field codes
    t = t.replace("\x07", "\t").replace("\x0b", "\n").replace("\x0c", "\n").replace("\r", "\n")
    t = re.sub(r"[\x00-\x08\x0e-\x1f]", "", t)
    return Doc("Word document (old format)", _tidy(t))


# ── PDF ──
def _pdf(data):
    from pypdf import PdfReader

    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        try:
            r.decrypt("")
        except Exception:  # noqa: BLE001
            return Doc("PDF", "", note="password protected")
    pages = len(r.pages)
    out = []
    for i, p in enumerate(r.pages[:MAX_PDF_PAGES]):
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
        out.append(f"\n\n[page {i + 2}]\n" if i + 1 < min(pages, MAX_PDF_PAGES) else "")
    text = _tidy("".join(out))
    words = len(re.findall(r"[A-Za-z]{2,}", text))
    d = Doc("PDF", text, pages=pages)
    if words < max(15, 5 * min(pages, MAX_PDF_PAGES)):
        d.scanned, d.text = True, ""
        d.note = "no text layer (a scan or a drawing): not included, check by hand"
    elif pages > MAX_PDF_PAGES:
        d.note = f"first {MAX_PDF_PAGES} of {pages} pages"
    return d


# ── email ──
def _filetime(v: int):
    try:
        return _dt.datetime(1601, 1, 1) + _dt.timedelta(microseconds=v // 10)
    except (OverflowError, ValueError):
        return None


def _msg(data, depth=0):
    import olefile

    ole = olefile.OleFileIO(io.BytesIO(data))

    def prop(prefix, pid):
        for typ, dec in (("001F", "utf-16-le"), ("001E", "cp1252"), ("0102", None)):
            path = f"{prefix}__substg1.0_{pid}{typ}"
            if ole.exists(path):
                raw = ole.openstream(path).read()
                return raw if dec is None else raw.decode(dec, "replace").rstrip("\x00")
        return None

    d = Doc("Email")
    d.subject = prop("", "0037") or ""
    name, addr = prop("", "0C1A"), prop("", "5D01") or prop("", "0C1F")
    d.sender = f"{name or ''} <{addr}>" if addr and "@" in addr else (name or "")
    to, cc = [], []
    for entry in ole.listdir(storages=True, streams=False):
        top = entry[0]
        if top.startswith("__recip_version1.0_") and len(entry) == 1:
            pre = top + "/"
            rname = prop(pre, "3001") or ""
            raddr = prop(pre, "39FE") or prop(pre, "3003") or ""
            rtype = 1
            pstream = pre + "__properties_version1.0"
            if ole.exists(pstream):
                pdata = ole.openstream(pstream).read()
                for off in range(8, len(pdata) - 15, 16):
                    tag = struct.unpack_from("<I", pdata, off)[0]
                    if tag >> 16 == 0x0C15:
                        rtype = struct.unpack_from("<I", pdata, off + 8)[0]
            entry_s = f"{rname} <{raddr}>" if "@" in raddr else rname
            (cc if rtype == 2 else to).append(entry_s)
    d.to = "; ".join(to) or (prop("", "0E04") or "")
    d.cc = "; ".join(cc) or (prop("", "0E03") or "")
    if ole.exists("__properties_version1.0"):
        pdata = ole.openstream("__properties_version1.0").read()
        for off in range(32, len(pdata) - 15, 16):
            tag = struct.unpack_from("<I", pdata, off)[0]
            if tag in (0x00390040, 0x0E060040):
                d.date = _filetime(struct.unpack_from("<Q", pdata, off + 8)[0])
                if tag == 0x00390040:
                    break
    body = prop("", "1000")
    if not body:
        h = prop("", "1013")
        if isinstance(h, bytes):
            h = h.decode("utf-8", "replace")
        body = html_to_text(h) if h else ""
    if not body:
        rtf = prop("", "1009")
        if isinstance(rtf, bytes):
            body = rtf_to_text(_lzfu(rtf).decode("cp1252", "replace"))
    d.text = _tidy(body if isinstance(body, str) else "")
    for entry in ole.listdir(storages=True, streams=False):
        top = entry[0]
        if top.startswith("__attach_version1.0_") and len(entry) == 1:
            pre = top + "/"
            fname = prop(pre, "3707") or prop(pre, "3704") or "attachment"
            if isinstance(fname, bytes):
                fname = fname.decode("utf-8", "replace")
            blob = prop(pre, "3701")
            if isinstance(blob, bytes):
                d.attachments.append(_attachment(fname, blob, depth))
            else:
                d.attachments.append((fname, None, "embedded item, not read"))
    return d


def _eml(data, depth=0):
    m = email.message_from_bytes(data, policy=email.policy.default)
    d = Doc("Email", subject=str(m["subject"] or ""), sender=str(m["from"] or ""),
            to=str(m["to"] or ""), cc=str(m["cc"] or ""))
    try:
        d.date = email.utils.parsedate_to_datetime(str(m["date"])) if m["date"] else None
    except (TypeError, ValueError):
        d.date = None
    body = m.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        d.text = _tidy(html_to_text(content) if body.get_content_type() == "text/html"
                       else content)
    for part in m.iter_attachments():
        fname = part.get_filename() or "attachment"
        blob = part.get_payload(decode=True)
        if blob:
            d.attachments.append(_attachment(fname, blob, depth))
    return d


def _attachment(fname, blob, depth):
    ext = os.path.splitext(fname)[1].lower()
    if ext in DOC_EXTS and depth < 2:
        try:
            return (fname, extract_bytes(blob, ext, depth + 1), "")
        except Exception as e:  # noqa: BLE001
            return (fname, None, f"could not read ({type(e).__name__})")
    return (fname, None, "not read (images and other files inside emails are listed only)")


# ── entry points ──
def extract_bytes(data: bytes, ext: str, depth: int = 0) -> Doc:
    ext = ext.lower()
    if ext == ".msg":
        return _msg(data, depth)
    if ext == ".eml":
        return _eml(data, depth)
    if ext == ".pdf":
        return _pdf(data)
    if ext in (".docx", ".docm"):
        return _docx(data)
    if ext == ".pptx":
        return _pptx(data)
    if ext == ".odt":
        return _odt(data)
    if ext == ".doc":
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # sometimes RTF or HTML named .doc
            head = data[:200].lstrip().lower()
            if head.startswith(b"{\\rtf"):
                return Doc("Word document (RTF)", rtf_to_text(data.decode("cp1252", "replace")))
            return Doc("Word document (HTML)", html_to_text(data.decode("utf-8", "replace")))
        return _doc(data)
    if ext == ".rtf":
        return Doc("Rich Text document", rtf_to_text(data.decode("cp1252", "replace")))
    if ext in (".htm", ".html"):
        return Doc("Web page", html_to_text(data.decode("utf-8", "replace")))
    raise ValueError(f"unsupported document type {ext}")


def extract(path: str) -> Doc:
    with open(path, "rb") as f:
        data = f.read()
    return extract_bytes(data, os.path.splitext(path)[1])


def learn(doc: Doc, people) -> None:
    """Teach the people book every person this document names."""
    for v in (doc.sender, doc.to, doc.cc):
        people.add_header(v)
    people.learn_text(doc.text)
    for _n, sub, _note in doc.attachments:
        if sub is not None:
            learn(sub, people)


# ── writing ──
def _date_str(d: _dt.datetime | None, keep_dates: bool) -> str:
    if not d:
        return ""
    return d.strftime("%Y-%m-%d %H:%M") if keep_dates else str(d.year)


def _scrub(text: str, people, names: set, hits: dict) -> str:
    from . import rules

    # names from spreadsheet name columns reach documents through people.extra_tokens,
    # where only capitalized words count, so ordinary words are never erased
    text = people.scrub(text, hits)
    return rules.scrub_patterns(text, rules.text_patterns(), hits)


def render(doc: Doc, people, names: set, keep_dates: bool, hits: dict, title: str) -> str:
    """The de-identified text version of a document, ready to write."""
    L = [f"Document type: {doc.kind}", f"File: {title}"]
    if doc.kind == "Email":
        L.append(f"Subject: {_scrub(doc.subject, people, names, hits)}")
        if doc.date:
            L.append(f"Date: {_date_str(doc.date, keep_dates)}")
        for label, v in (("From", doc.sender), ("To", doc.to), ("Cc", doc.cc)):
            if v:
                L.append(f"{label}: {_header_labels(v, people)}")
    if doc.pages:
        L.append(f"Pages: {doc.pages}")
    if doc.note:
        L.append(f"Note: {doc.note}")
    L.append("")
    L.append(_scrub(doc.text, people, names, hits))
    for fname, sub, note in doc.attachments:
        safe = _scrub(fname, people, names, hits)
        L.append("")
        L.append(f"=== Attachment: {safe} ===")
        if sub is None or (not sub.text and sub.kind != "Email"):
            L.append(f"({note or (sub.note if sub else '') or 'no readable text'})")
        else:
            L.append(render(sub, people, names, keep_dates, hits, safe))
    return "\n".join(L).strip() + "\n"


def _header_labels(value: str, people) -> str:
    """'Darnell Okafor <d@riverbend.example>' -> 'Person 1 (riverbend.example)'. The
    domain stays: it shows which company someone is at, not who they are, except
    personal mail domains, which are hidden."""
    out = []
    from .people import RE_ADDR

    for part in re.split(r"[;\n]|,(?=[^>]*(?:<|$))", value):
        part = part.strip()
        if not part:
            continue
        m = RE_ADDR.search(part)
        if m:
            key = people.by_email.get(m.group(2).lower())
            label = people.labels[key] if key else "[EMAIL]"
            dom = m.group(2).split("@")[1].lower()
            personal = dom in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                               "aol.com", "icloud.com", "msn.com", "live.com", "me.com",
                               "att.net", "comcast.net", "bellsouth.net", "cox.net"}
            out.append(f"{label} ({'personal email' if personal else dom})")
        else:
            out.append(people.scrub(part))
    return "; ".join(out)


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))
