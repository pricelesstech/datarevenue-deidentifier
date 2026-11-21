"""Column classification and value transforms for HIPAA Safe Harbor (45 CFR 164.514(b)(2)).

Safe Harbor lists 18 identifier types. How each is handled here:

  names .......................... NAME   (column dropped, names also scrubbed from free text)
  geography smaller than state ... REMOVE (street, city, county), ZIP3 (zip code)
  dates (except year) ............ YEAR   (keep year only), AGE (ages over 89 become 90+)
  phone, fax, email, URL, IP ..... REMOVE, and pattern-scrubbed from every kept column
  SSN, account, plan, license,
  vehicle, device, cert numbers .. REMOVE
  medical record numbers ......... CODE   (replaced with a random study code, see crosswalk.py)
  photos, biometrics ............. handled by images.py
  any other unique number ........ REMOVE by default; long digit runs scrubbed from text
"""
from __future__ import annotations

import datetime as _dt
import re

KEEP = "KEEP"
REMOVE = "REMOVE"
NAME = "NAME"
CODE = "CODE"
YEAR = "YEAR"
ZIP3 = "ZIP3"
AGE = "AGE"
TEXT = "TEXT"

ACTIONS = [KEEP, REMOVE, NAME, CODE, YEAR, ZIP3, AGE, TEXT]

ACTION_LABELS = {
    KEEP: "Keep (still checked for emails, phones, SSNs)",
    REMOVE: "Remove column",
    NAME: "Remove column + erase these names from notes",
    CODE: "Replace with study code (links images)",
    YEAR: "Keep year only",
    ZIP3: "Keep first 3 digits of ZIP",
    AGE: "Keep age, 90+ for over 89",
    TEXT: "Free text: erase names, dates, numbers",
}

# ZIP3 prefixes whose population is 20,000 or fewer (2000 Census), which Safe Harbor
# requires to be reported as 000.
RESTRICTED_ZIP3 = frozenset({
    "036", "059", "063", "102", "203", "556", "692", "790", "821",
    "823", "830", "831", "878", "879", "884", "890", "893",
})


def _tokens(header: str) -> list[str]:
    h = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(header or ""))  # camelCase -> camel Case
    return [t for t in re.split(r"[^a-z0-9]+", h.lower()) if t]


def _joined(header: str) -> str:
    return "".join(_tokens(header))


_NAME_TOKENS = {"name", "names", "firstname", "lastname", "fname", "lname", "surname",
                "middlename", "fullname", "nickname", "maiden", "guarantor", "responsible",
                "parent", "guardian", "spouse", "mother", "father", "emergency", "kin"}
_ID_TOKENS = {"mrn", "patientid", "patid", "ptid", "chart", "chartno", "chartnumber",
              "medicalrecord", "recordnumber", "patientnumber", "patientno", "subjectid",
              "caseid", "personid", "pid"}
_REMOVE_TOKENS = {
    "address", "addr", "street", "st1", "line1", "line2", "apt", "suite", "city", "town",
    "county", "township", "village", "geo", "lat", "latitude", "lon", "lng", "longitude",
    "gps", "email", "mail", "e", "phone", "tel", "telephone", "mobile", "cell", "fax",
    "pager", "ssn", "social", "sin", "license", "licence", "dl", "passport", "plate",
    "vin", "vehicle", "serial", "device", "imei", "mac", "url", "website", "web", "ip",
    "account", "acct", "member", "subscriber", "policy", "insured", "beneficiary",
    "certificate", "cert", "npi", "dea", "medicaid", "medicare", "mbi", "photo", "picture",
    "image", "fingerprint", "biometric", "signature", "username", "login", "password",
    "employer", "occupation", "school",
}
# Header words that by themselves mean "this is a date". Words like "visit", "service" or
# "procedure" are NOT here: "Procedure" usually holds a procedure name. Date-valued columns
# with such names are caught by their values instead (classify_values + the KEEP safety net).
_DATE_TOKENS = {"date", "dates", "dt", "dob", "birth", "birthday", "birthdate", "born", "dod",
                "death", "dos", "created", "updated", "modified", "timestamp", "datetime",
                "scheduled", "when"}
_ZIP_TOKENS = {"zip", "zipcode", "postal", "postcode", "zip5", "zip9"}
_TEXT_TOKENS = {"note", "notes", "comment", "comments", "remark", "remarks", "memo",
                "narrative", "description", "desc", "history", "hx", "summary",
                "findings", "impression", "text", "message", "complaint", "reason",
                "assessment", "plan", "observation", "observations"}


# Everyday English and business words: never harvested as names from a spreadsheet name
# column, and never erased on their own as if they were a person's name.
COMMON_WORDS = set("""
a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing done down during each else few
for from further had has have having he her here hers him his how i if in into is it its
just me more most my no nor not now of off on once only or other our out over own same she
should so some such than that the their them then there these they this those through to
too under until up upon very was we were what when where which while who whom why will with
would you your yours yes per via etc inc llc co corp ltd company construction contractor
contract contracts subcontract subcontractor owner owners client project projects job jobs
site work works new old north south east west street road avenue drive suite office
building park state city county parish department dept division unit group team services
service management manager engineer engineering architect design general repair repairs
total amount date dates time day days week month year phase change order orders request
requests report reports daily note notes item items number no each page section clause
general supply supplies material materials labor equipment cost costs price bid bids
schedule submittal submittals invoice payment payments pay application final letter
document documents file files attached attachment see per approved pending complete
completed open closed yes none na tbd various misc other info information contact
""".split())

_THING_NAME = {"project", "company", "business", "vendor", "supplier", "product", "item",
               "file", "job", "site", "building", "model", "brand", "store", "location",
               "facility", "plan", "program", "course", "drug", "medication", "part",
               "material", "equipment", "task", "event", "organization", "org", "firm",
               "practice", "sheet", "tab", "folder", "document", "account", "subcontractor",
               "sub", "contractor", "manufacturer", "carrier", "insurer", "payer", "bank",
               "agency", "department", "dept", "street", "city", "county", "state",
               "country", "product", "service", "category", "class", "type", "trade",
               "phase", "activity", "cost", "code", "field", "column", "table", "variable"}


def classify(header: str) -> str:
    """Suggest an action for a column from its header alone. Values get a second check
    (see scan_value) whatever the action."""
    toks = set(_tokens(header))
    joined = _joined(header)
    if not toks:
        return KEEP
    if joined in _ID_TOKENS or toks & {"mrn", "chart"} or (
        "patient" in toks and toks & {"id", "number", "no", "num", "code"}
    ) or joined in {"recordid", "recordno"}:
        return CODE
    if toks & _ZIP_TOKENS:
        return ZIP3
    if toks & {"age"} and not toks & _DATE_TOKENS:
        return AGE
    if "name" in toks and toks & _THING_NAME:
        return KEEP  # "Project Name", "Vendor Name": the name of a thing, not a person
    if toks & _NAME_TOKENS or joined in _NAME_TOKENS or (
        "patient" in toks and len(toks) == 1
    ) or toks & {"first", "last", "middle", "given", "family"} or joined in {
        "provider", "doctor", "physician", "dentist", "orthodontist", "referring",
        "referredby", "contact", "relative",
    }:
        return NAME
    if toks & _REMOVE_TOKENS or joined in _REMOVE_TOKENS:
        return REMOVE
    if toks & {"id", "number", "no", "num"} and not toks & {"tooth", "teeth", "count",
                                                             "visits", "visit", "of"}:
        # an unrecognized identifier-like column ("Ref No", "Claim ID"): fail closed
        return REMOVE
    if toks & _DATE_TOKENS:
        return YEAR
    if toks & _TEXT_TOKENS:
        return TEXT
    return KEEP


_PERSONAL_ID = {"customer", "client", "member", "patient", "employee", "account", "acct",
                "ssn", "social", "license", "licence", "policy", "subscriber", "insured",
                "phone", "tax", "ein", "tin", "driver", "passport", "card", "bank", "routing",
                "user", "student", "person", "guest", "resident", "tenant", "borrower"}


def for_business(header: str, action: str) -> str:
    """The suggestion for business data. Two differences from health data:
    business dates are kept (dates of birth are not), and ID-like columns that are not
    about a person ("PO Number", "Job No", "Invoice ID") are kept instead of dropped."""
    toks = set(_tokens(header))
    if action == YEAR and not is_birth_header(header):
        return KEEP
    if action == REMOVE and toks & {"id", "number", "no", "num"} \
            and not toks & (_REMOVE_TOKENS | _PERSONAL_ID):
        return KEEP
    return action


def classify_values(header: str, values: list) -> str:
    """Header-based suggestion, refined by a look at the actual values: a column the
    header calls KEEP but whose values are mostly dates becomes YEAR; mostly long free
    text becomes TEXT."""
    a = classify(header)
    if a != KEEP:
        return a
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return a
    dates = sum(1 for v in vals if isinstance(v, (_dt.date, _dt.datetime))
                or (isinstance(v, str) and RE_DATE.fullmatch(v.strip())))
    if dates >= 0.6 * len(vals):
        return YEAR
    long_text = sum(1 for v in vals if isinstance(v, str) and len(v) > 60 and " " in v)
    if long_text >= 0.3 * len(vals):
        return TEXT
    return a


# ── value patterns ──────────────────────────────────────────────────────────────
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
RE_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
RE_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
RE_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
RE_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")
RE_ZIP9 = re.compile(r"\b\d{5}-\d{4}\b")
RE_DATE = re.compile(
    r"\b(?:\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}|"
    r"\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+\d{2,4})\b",
    re.I,
)
RE_LONGNUM = re.compile(r"(?<!\d)(?<!\d\.)\d{6,}(?!\d)(?!\.\d)")
RE_BARE_ID = re.compile(r"(?<!\d)(?<!\d\.)\d{9,11}(?!\d)(?!\.\d)")
RE_TITLE_NAME = re.compile(
    r"\b(?:Dr|Mr|Mrs|Ms|Miss|Mx|Prof)\.?\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?"
)

RE_EIN = re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])")  # US employer / tax ID
_STREET_TYPES = (r"St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|"
                 r"Hwy|Highway|Pkwy|Parkway|Way|Pl|Place|Cir|Circle|Ter|Terrace|Trl|Trail|"
                 r"Sq|Square|Loop|Row")
RE_STREET = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][A-Za-z'\-]*\.?\s+){1,4}(?:" + _STREET_TYPES +
    r")\b\.?(?:,?\s+(?:Suite|Ste|Apt|Unit|#)\.?\s*[\w\-]+)?")
RE_POBOX = re.compile(r"\bP\.?\s?O\.?\s+Box\s+\d+\b", re.I)
_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
           "MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY")
RE_STATE_ZIP = re.compile(r"\b(" + _STATES + r")\.?,?\s+\d{5}(?:-\d{4})?\b")

# ── mode ──
# HEALTH (default, HIPAA Safe Harbor): no date more specific than a year anywhere.
# BUSINESS: business dates (orders, reports, meetings) keep their value; dates of birth are
# still cut to the year and every other identifier is removed the same way.
KEEP_DATES = False


def set_mode(keep_dates: bool) -> None:
    global KEEP_DATES
    KEEP_DATES = bool(keep_dates)


RE_BIRTH_DATE = re.compile(
    r"(?i)\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date|born(?:\s+on)?)\s*[:#\-]?\s*"
    r"(?:" + RE_DATE.pattern + r")", re.I)


def safety_patterns():
    """Patterns that must never survive in a kept column or a document."""
    p = [
        ("date of birth", RE_BIRTH_DATE, "[DATE OF BIRTH]"),
        ("email", RE_EMAIL, "[EMAIL]"),
        ("url", RE_URL, "[URL]"),
        ("ssn", RE_SSN, "[SSN]"),
        ("tax ID", RE_EIN, "[TAX ID]"),
        ("phone", RE_PHONE, "[PHONE]"),
        ("ip", RE_IP, "[IP]"),
        ("zip+4", RE_ZIP9, "[ZIP]"),
        ("state + ZIP", RE_STATE_ZIP, r"\1 [ZIP]"),
        ("street address", RE_STREET, "[ADDRESS]"),
        ("PO box", RE_POBOX, "[ADDRESS]"),
        ("number like SSN/phone", RE_BARE_ID, "[NUMBER]"),
    ]
    if not KEEP_DATES:
        p.append(("date", RE_DATE, "[DATE]"))  # Safe Harbor: no date more specific than a year
    return p


def text_patterns():
    return safety_patterns() + [
        ("long number", RE_LONGNUM, "[NUMBER]"),
        ("titled name", RE_TITLE_NAME, "[NAME]"),
    ]


# kept for callers that read them as constants (health mode values)
SAFETY_PATTERNS = safety_patterns()
TEXT_PATTERNS = text_patterns()

_BIRTH = re.compile(r"(?i)\b(dob|d\.o\.b\.?|birth|born|birthday|birthdate)\b")


def is_birth_header(header: str) -> bool:
    return bool(set(_tokens(header)) & {"dob", "birth", "birthday", "birthdate", "born"})


def scrub_patterns(value: str, patterns, hits: dict) -> str:
    for label, rx, repl in patterns:
        value, n = rx.subn(repl, value)
        if n:
            hits[label] = hits.get(label, 0) + n
    return value


_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def scrub_names(value: str, name_tokens: set[str], hits: dict) -> str:
    if not name_tokens:
        return value
    n = 0

    def _sub(m):
        nonlocal n
        w = m.group(0)
        if w.lower().strip("'-") in name_tokens:
            n += 1
            return "[NAME]"
        return w

    out = _WORD.sub(_sub, value)
    if n:
        hits["name"] = hits.get("name", 0) + n
    return out


# ── transforms ──────────────────────────────────────────────────────────────────
_RX_YMD = re.compile(r"^\s*(\d{4})[/\-.]?(\d{1,2})[/\-.]?(\d{1,2})")
_RX_MDY = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
_RX_YEAR_ANY = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


def year_of(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.year
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = int(value)
        if 1800 <= v <= 2100:
            return v
        if 19000101 <= v <= 21001231:  # 19610314
            return v // 10000
        if 1 <= v <= 80000:  # Excel serial date
            return (_dt.date(1899, 12, 30) + _dt.timedelta(days=v)).year
        return None
    s = str(value)
    m = _RX_YMD.match(s)
    if m and 1800 <= int(m.group(1)) <= 2100:
        return int(m.group(1))
    m = _RX_MDY.match(s)
    if m:
        y = int(m.group(3))
        if y < 100:
            pivot = _dt.date.today().year % 100
            y += 2000 if y <= pivot else 1900
        return y
    m = _RX_YEAR_ANY.search(s)
    return int(m.group(1)) if m else None


def to_year(value, header: str = "") -> str:
    y = year_of(value)
    if y is None:
        return ""
    toks = set(_tokens(header))
    if toks & {"dob", "birth", "birthday", "birthdate", "born"}:
        if _dt.date.today().year - y >= 90:
            return f"{_dt.date.today().year - 90} or earlier"
    return str(y)


def to_zip3(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return ""
    if len(digits) in (3, 4, 8):  # Excel dropped leading zeros (e.g. 2134 = 02134)
        digits = digits.zfill(5 if len(digits) < 5 else 9)
    z3 = digits[:3]
    return "000" if z3 in RESTRICTED_ZIP3 else z3


def to_age(value) -> str:
    if value is None or value == "":
        return ""
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    if not m:
        return ""
    a = float(m.group(0))
    if a >= 90:
        return "90+"
    return str(int(a)) if a.is_integer() else str(a)
