"""Sample mode + the one-file package a client sends."""
import csv
import json
import os
import re
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import make_samples  # noqa: E402
from deid import package, pipeline  # noqa: E402

PHI = ["smithfield", "quintero", "pakulski", "hallworth", "4482913", "117730", "558201",
       "oak lane", "555-0142", "example.com", "1961-03-14", "84604"]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    src = tmp_path_factory.mktemp("in")
    base = tmp_path_factory.mktemp("base")
    make_samples.main(str(src))
    # a bigger table so sampling actually samples
    with open(os.path.join(src, "big.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Patient ID", "Last Name", "Visit Date", "Treatment", "Fee"])
        for i in range(1200):
            w.writerow([900000 + i, f"Zzname{i % 7}", f"2019-0{1 + i % 9}-15",
                        ["Braces", "Aligners", "Retainer"][i % 3], 100 + i % 50])
    # pass a folder AND a file, as a drag and drop would
    inv = pipeline.scan([str(src), os.path.join(src, "visits.xlsx")], str(base))
    actions = {h: d["action"] for h, d in inv.headers.items()}
    share = os.path.join(base, "run", "Sample contents")
    s = pipeline.run(inv, None, actions, ocr=True, workers=2, sample_rows=100,
                     sample_images=2, share_dir=share, private_dir=os.path.join(base, "priv"),
                     seed=7)
    answers = {"contact": {"company": "Maple Ortho, PC", "name": "Test", "email": "t@x.io"},
               "datasets": [{"name": "Ortho records", "category": "customer",
                             "record_volume": "under_100k", "history_years": "3_10",
                             "update_frequency": "daily_weekly", "record_type": "person"}],
               "company": {"ownership": "own_outright", "work_data": ["none"]},
               "notes": "Pano X-rays for every patient"}
    pkg = package.build_package(s, answers, os.path.join(base, "run"))
    return s, pkg, inv


def test_file_counted_once(built):
    _s, _pkg, inv = built
    assert sum(1 for p in inv.tables if p.endswith("visits.xlsx")) == 1


def test_sample_sizes(built):
    s, _pkg, _inv = built
    assert s["sampling"] is True
    big = next(t for t in s["table_profiles"] if t["file"].startswith("big"))
    assert big["source_rows"] == 1200 and big["sample_rows"] == 100
    assert s["images_total"] == 4 and s["images_ok"] == 2


def test_profile_covers_full_table(built):
    s, _pkg, _inv = built
    big = next(t for t in s["table_profiles"] if t["file"].startswith("big"))
    cols = {c["name"]: c for c in big["sheets"][0]["columns"]}
    assert cols["Last Name"]["kind"] == "removed" and "top_values" not in cols["Last Name"]
    assert cols["Visit Date"]["min"] == 2019 and cols["Visit Date"]["max"] == 2019
    assert cols["Fee"]["min"] == 100 and cols["Fee"]["max"] == 149
    assert dict(cols["Treatment"]["top_values"]) == {"Braces": 400, "Aligners": 400,
                                                     "Retainer": 400}
    assert cols["Patient ID"]["kind"] == "study code" and "top_values" not in cols["Patient ID"]


def test_small_cells_suppressed(built):
    s, _pkg, _inv = built
    pat = next(t for t in s["table_profiles"] if t["file"].startswith("patients"))
    for c in pat["sheets"][0]["columns"]:
        for _v, n in c.get("top_values", []):
            assert n >= 5


def test_zip_contents_and_no_phi(built):
    _s, pkg, _inv = built
    assert os.path.basename(pkg["zip"]).startswith("DataRevenue_Sample_Maple_Ortho_PC_")
    with zipfile.ZipFile(pkg["zip"]) as z:
        names = z.namelist()
        assert "data_summary.html" in names and "data_summary.json" in names
        assert "DEIDENTIFICATION_REPORT.txt" in names
        assert any(n.startswith("tables/") for n in names)
        assert sum(1 for n in names if n.startswith("images/")) == 2
        data = json.loads(z.read("data_summary.json"))
        assert data["answers"]["datasets"][0]["record_volume"] == "under_100k"
        assert "share_dir" not in data["summary"]
        for n in names:
            if n.endswith((".html", ".json", ".txt", ".csv")):
                blob = z.read(n).decode("utf-8", "ignore").lower()
                # random study codes (P + 8 digits) can contain any digit run by chance
                blob = re.sub(r"p\d{8}", "<code>", blob)
                for term in PHI:
                    assert term not in blob, f"{term!r} in {n}"


def test_html_shows_answers_as_labels(built):
    _s, pkg, _inv = built
    html = open(pkg["html"], encoding="utf-8").read()
    assert "Maple Ortho, PC" in html
    assert "Under 100 thousand" in html  # label, not the key
    assert "data:image/jpeg;base64," in html  # thumbnails
    assert "—" not in html  # no em dashes
