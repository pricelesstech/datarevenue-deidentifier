"""End-to-end: fake PHI in, nothing identifying out of the SHARE folder."""
import csv
import os
import re
import sys

import numpy as np
import pydicom
import pytest
from openpyxl import load_workbook
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

import make_samples  # noqa: E402
from deid import images, pipeline, rules  # noqa: E402

PHI = ["smithfield", "quintero", "pakulski", "hallworth", "004482913", "4482913", "117730",
       "558201", "oak lane", "elm st", "provo", "orem", "lehi", "555-0142", "5550199",
       "example.com", "example.org", "1961-03-14", "19321102", "123456789", "88231944",
       "84604", "84057", "acc99812"]


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    src = tmp_path_factory.mktemp("in")
    out = tmp_path_factory.mktemp("out")
    make_samples.main(str(src))
    inv = pipeline.scan(str(src), str(out))
    actions = {h: d["action"] for h, d in inv.headers.items()}
    s = pipeline.run(inv, str(out), actions, ocr=True, workers=2)
    return src, out, s, actions


def _share_files(out):
    root = os.path.join(out, pipeline.SHARE_DIR)
    for r, _d, fs in os.walk(root):
        for f in fs:
            yield os.path.join(r, f)


def test_classification(result):
    _src, _out, _s, a = result
    assert a["Patient Name"] == rules.NAME
    assert a["First Name"] == rules.NAME and a["Last Name"] == rules.NAME
    assert a["MRN"] == rules.CODE and a["Patient ID"] == rules.CODE
    assert a["DOB"] == rules.YEAR and a["Visit Date"] == rules.YEAR
    assert a["Date of Service"] == rules.YEAR
    assert a["Zip"] == rules.ZIP3 and a["Age"] == rules.AGE
    for h in ("Address", "City", "Phone", "Email", "SSN", "Insurance Member ID"):
        assert a[h] == rules.REMOVE, h
    assert a["Airway Notes"] == rules.TEXT and a["Comments"] == rules.TEXT
    assert a["Diagnosis"] == rules.KEEP and a["State"] == rules.KEEP
    assert a["Teeth Count"] == rules.KEEP
    assert a["Procedure"] == rules.KEEP  # a procedure name, not a date


def test_values_refine_classification():
    assert rules.classify_values("Appt", ["03/14/2023", "2023-04-01", "5/6/22"]) == rules.YEAR
    assert rules.classify_values("Procedure", ["Pano", "Ceph"]) == rules.KEEP
    long = ["Patient reports snoring and daytime fatigue, referred for a sleep study."] * 3
    assert rules.classify_values("Misc", long) == rules.TEXT


def test_no_phi_bytes_in_share_folder(result):
    _src, out, _s, _a = result
    for p in _share_files(out):
        if p.endswith(".xlsx"):
            wb = load_workbook(p)
            blob = " ".join(str(c) for ws in wb for row in ws.iter_rows(values_only=True)
                            for c in row).lower()
        elif p.endswith(".dcm"):
            # header only; printed text in pixels is covered by the OCR test below
            ds = pydicom.dcmread(p, stop_before_pixels=True)
            blob = (str(ds) + str(ds.file_meta)).lower()
        else:
            blob = open(p, "rb").read().decode("latin-1").lower()
        blob += " " + os.path.basename(p).lower()
        # random study codes (P + 8 digits) can contain any digit run by chance
        blob = re.sub(r"p\d{8}", "<code>", blob)
        for term in PHI:
            assert term not in blob, f"{term!r} leaked in {p}"


def test_tables_values(result):
    _src, out, _s, _a = result
    t = os.path.join(out, pipeline.SHARE_DIR, "tables")
    with open(os.path.join(t, "patients_deidentified.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert set(rows[0]) == {"MRN", "DOB", "State", "Zip", "Age", "Visit Date", "Diagnosis",
                            "Airway Notes", "Teeth Count", "Extra"}
    assert rows[0]["DOB"] == "1961"
    assert rows[1]["DOB"].endswith("or earlier")  # born 1932, 90+
    assert rows[1]["Age"] == "90+"
    assert rows[0]["Zip"] == "846" and rows[2]["Zip"] == "000"  # 036 is restricted
    assert rows[0]["MRN"].startswith("P") and len(rows[0]["MRN"]) == 9
    assert "[NAME]" in rows[0]["Airway Notes"] and "[PHONE]" in rows[0]["Airway Notes"]
    assert "[DATE]" in rows[0]["Airway Notes"]
    assert "[PHONE]" in rows[0]["Extra"]
    wb = load_workbook(os.path.join(t, "visits_deidentified.xlsx"))
    vrows = list(wb["Visits"].iter_rows(values_only=True))
    assert vrows[0] == ("Patient ID", "Date of Service", "Procedure", "Comments")
    assert vrows[1][2] == "Pano"
    # same patient, same code across files (MRN 004482913 == Patient ID 4482913)
    assert vrows[1][0] == rows[0]["MRN"]
    assert vrows[1][1] == "2022"
    assert vrows[1][3] == "[NAME] tolerated well"


def test_images(result):
    _src, out, s, _a = result
    assert s["images_ok"] == 4 and s["review"] == 1  # the pdf
    imgs = [p for p in _share_files(out) if "images" in p]
    assert len(imgs) == 4
    dcm = [p for p in imgs if p.endswith(".dcm")
           and pydicom.dcmread(p).get("PatientAge") == "090Y"][0]
    ds = pydicom.dcmread(dcm)
    assert str(ds.PatientName).startswith("P") and ds.PatientID == str(ds.PatientName)
    assert ds.PatientAge == "090Y" and ds.StudyDate == "20230101"
    for kw in ("PatientBirthDate", "PatientAddress", "InstitutionName",
               "ReferringPhysicianName", "AccessionNumber", "StudyDescription", "StudyTime"):
        assert kw not in ds, kw
    assert not any(el.tag.is_private for el in ds)
    assert ds.pixel_array.shape == (1300, 2600)
    # the code in the DICOM matches the MRN 117730 code from the spreadsheet
    t = os.path.join(out, pipeline.SHARE_DIR, "tables", "patients_deidentified.csv")
    with open(t, encoding="utf-8-sig") as f:
        assert next(r for i, r in enumerate(csv.DictReader(f)) if i == 1)["MRN"] == ds.PatientID
    # JPEG found under the patient folder got linked to that patient's code
    assert any(os.path.basename(p).startswith("P") and p.endswith(".jpg") for p in imgs)
    for p in imgs:
        if p.endswith(".jpg"):
            assert not Image.open(p).getexif(), p


def test_no_readable_text_left_in_images(result):
    _src, out, _s, _a = result
    for p in _share_files(out):
        if "images" not in p:
            continue
        if p.endswith(".dcm"):
            arr = images._to_uint8(pydicom.dcmread(p).pixel_array)
        else:
            arr = np.array(Image.open(p).convert("L"))
        left = " ".join(b[4] for b in images.find_text(arr)).lower()
        for term in ("smithfield", "quintero", "pakulski", "4482913", "117730", "558201",
                     "hallworth", "1961", "1932", "2011", "orem"):
            assert term not in left, f"{term!r} still readable in {p}: {left}"


def test_private_folder_has_key(result):
    _src, out, _s, _a = result
    priv = os.path.join(out, pipeline.PRIVATE_DIR)
    with open(os.path.join(priv, "patient_code_crosswalk.csv"), encoding="utf-8") as f:
        ids = {r["original_id"] for r in csv.DictReader(f)}
    assert {"4482913", "117730", "558201"} <= ids
