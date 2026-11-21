"""Documents, emails, old Excel and zips: fake construction records in, nothing personal out."""
import glob
import os
import re
import shutil
import sys

import pytest
from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")

from deid import documents, pipeline, rules  # noqa: E402
from deid.people import PeopleBook  # noqa: E402

PERSONAL = ["darnell", "okafor", "marisol", "vega", "tobias", "hale", "priya", "raman",
            "555-0187", "555-0110", "555-0122", "72-1234567", "4410 magazine", "70115",
            "darnell.okafor@", "marisol.vega@", "t.hale@"]


def _run(tmp_path, keep_dates):
    src = tmp_path / "in" / "Job 2207 Riverbend"
    shutil.copytree(FIX, src)
    inv = pipeline.scan([str(src)], str(tmp_path / "out"))
    key = "action_business" if keep_dates else "action"
    actions = {h: d[key] for h, d in inv.headers.items()}
    s = pipeline.run(inv, str(tmp_path / "out"), actions, ocr=False, keep_dates=keep_dates,
                     sample_rows=None, sample_images=None, sample_docs=None)
    share = os.path.join(tmp_path / "out", pipeline.SHARE_DIR)
    return inv, s, share


def _all_text(share):
    out = {}
    for p in glob.glob(os.path.join(share, "**", "*"), recursive=True):
        if os.path.isdir(p):
            continue
        if p.endswith(".xlsx"):
            wb = load_workbook(p)
            t = " ".join(str(c) for ws in wb for r in ws.iter_rows(values_only=True) for c in r)
        else:
            t = open(p, "rb").read().decode("utf-8", "ignore")
        out[os.path.relpath(p, share)] = t + " " + os.path.basename(p)
    return out


@pytest.fixture(scope="module")
def business(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("biz"), keep_dates=True)


@pytest.fixture(scope="module")
def health(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("health"), keep_dates=False)


def test_everything_found(business):
    inv, s, _share = business
    kinds = sorted(os.path.splitext(p)[1] for p in inv.documents)
    # .docx twice: one at the top and one inside the zip
    assert kinds == [".docx", ".docx", ".eml", ".odt", ".pdf"]
    assert any(p.endswith("po_log.xls") for p in inv.tables)
    assert s["documents_written"] == 5
    assert s["document_profile"]["people"] >= 4
    assert s["document_profile"]["attachments_read"] == 1  # the PDF inside the email


def test_zip_slip_blocked(business, tmp_path_factory):
    inv, _s, _share = business
    # "Photos Week 1/../../escape.txt" lands inside the unzip folder, never above it
    esc = [p for p in inv.other + inv.documents + inv.tables if p.endswith("escape.txt")]
    assert esc and all("deid_unzip_" in p for p in esc)
    assert inv.display[esc[0]].endswith("job_2207_extras.zip/Photos Week 1/escape.txt")
    for d in inv.temp_dirs:  # cleaned up after the run
        assert not os.path.exists(d)


def test_no_personal_details_in_share(business, health):
    for _inv, _s, share in (business, health):
        for name, text in _all_text(share).items():
            low = text.lower()
            for term in PERSONAL:
                assert term not in low, f"{term!r} left in {name}"


def test_labels_consistent_across_documents(business):
    _inv, _s, share = business
    texts = _all_text(share)
    email = next(t for n, t in texts.items() if n.endswith("punch_list_eml.txt"))
    report = next(t for n, t in texts.items() if n.endswith("daily_report_pdf.txt"))
    sender = re.search(r"From: (Person \d+)", email).group(1)
    assert f"[{sender}]" in report  # the same superintendent in both files
    # the sender's company domain is kept (who they work for), never their address
    assert f"From: {sender} (riverbend-builders.example)" in email
    assert "Delta Glass LLC" in report  # company names are kept


def test_dates_by_mode(business, health):
    biz = " ".join(_all_text(business[2]).values())
    hl = " ".join(_all_text(health[2]).values())
    assert "07/18/2023" in biz and "2023-08-01" in biz  # business dates kept
    assert "07/18/2023" not in hl and "2023-08-01" not in hl  # health: year only
    assert "[DATE]" in hl


def test_old_excel(business):
    _inv, _s, share = business
    p = glob.glob(os.path.join(share, "tables", "po_log*.xlsx"))[0]
    rows = list(load_workbook(p).active.iter_rows(values_only=True))
    assert rows[0] == ("PO Number", "Vendor", "Amount", "Order Date", "Description")
    assert rows[1][1] == "Delta Glass LLC" and rows[1][2] == 18250
    assert str(rows[1][3]).startswith("2023-03-02")  # business mode keeps the order date


def test_report_and_key(business):
    _inv, s, share = business
    rep = open(os.path.join(share, "DEIDENTIFICATION_REPORT.txt"), encoding="utf-8").read()
    assert "business data" in rep and "Documents and emails" in rep
    key = open(os.path.join(s["private_dir"], "people_key.csv"), encoding="utf-8-sig").read()
    assert "Darnell Okafor" in key  # the key stays private, with the real names


def test_people_rules():
    pb = PeopleBook()
    pb.add_header("Crescent Lumber <orders@crescentlumber.example>")
    assert not pb.by_name  # a company display name is not a person
    pb.learn_text("We used white paint. Inspector Priya Raman approved.\n\nThanks,\nKwame Mensah")
    out = pb.scrub("White paint approved by Priya Raman and Kwame Mensah; white trim.")
    assert "Priya" not in out and "Mensah" not in out
    assert "white trim" in out  # lowercase words are never treated as names


def test_thing_names_are_not_people():
    for h in ("PROJECT NAME:", "Vendor Name", "Company Name", "Job Name", "Product name"):
        assert rules.classify(h) == rules.KEEP, h
    for h in ("Customer Name", "Name", "Contact Name", "Patient"):
        assert rules.classify(h) == rules.NAME, h


def test_sentences_in_name_columns_are_not_harvested(tmp_path):
    from deid import tables
    from deid.crosswalk import Crosswalk

    p = tmp_path / "t.csv"
    p.write_text("Name,Notes\nAnn Zzlee,x\n\"the owner and contractor shall be paid by the bank\",y\n",
                 encoding="utf-8")
    names = tables.collect([str(p)], {"Name": rules.NAME, "Notes": rules.KEEP},
                           Crosswalk(str(tmp_path / "priv")))
    assert "zzlee" in names and "ann" in names
    assert not names & {"the", "owner", "and", "contractor", "paid", "bank"}


def test_mode_resets():
    rules.set_mode(False)
    assert any(p[0] == "date" for p in rules.safety_patterns())
    rules.set_mode(True)
    assert not any(p[0] == "date" for p in rules.safety_patterns())
    assert any(p[0] == "date of birth" for p in rules.safety_patterns())
    rules.set_mode(False)


def test_birth_dates_removed_even_in_business_mode():
    rules.set_mode(True)
    hits = {}
    out = rules.scrub_patterns("Client DOB: 03/14/1961, visit 07/18/2023",
                               rules.text_patterns(), hits)
    rules.set_mode(False)
    assert "1961" not in out and "07/18/2023" in out


def test_extract_each_format():
    for name in ("daily_report.pdf", "daily_report.docx", "daily_report.odt"):
        d = documents.extract(os.path.join(FIX, name))
        assert "Riverbend Clinic" in d.text, name
    e = documents.extract(os.path.join(FIX, "punch_list.eml"))
    assert e.kind == "Email" and "Okafor" in e.sender and e.attachments
