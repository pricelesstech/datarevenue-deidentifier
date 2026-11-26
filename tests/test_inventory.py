"""Inventory mode: counts, record counts, personal-data signals, duplicates, name hiding."""
import csv
import json
import os
import sqlite3
import threading
import zipfile

import pytest

from deid import inventory, inventory_report


@pytest.fixture()
def drive(tmp_path):
    root = tmp_path / "Drive"
    (root / "John Smith" / "2019").mkdir(parents=True)
    (root / "Accounting").mkdir()
    (root / "$RECYCLE.BIN").mkdir()
    (root / ".hidden").mkdir()
    with open(root / "Accounting" / "customers.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Customer Name", "Email", "Phone", "Order Total", "Order Date"])
        for i in range(250):
            w.writerow([f"Person {i}", f"p{i}@x.com", "555-111-2222", i * 3, "2021-03-04"])
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Job No", "Project Name", "Amount"])
    for i in range(40):
        ws.append([i, f"Project {i}", i * 10])
    wb.save(root / "Accounting" / "jobs.xlsx")
    con = sqlite3.connect(root / "Accounting" / "crm.sqlite")
    con.execute("CREATE TABLE contacts (id INTEGER, first_name TEXT, ssn TEXT)")
    con.executemany("INSERT INTO contacts VALUES (?,?,?)", [(i, "a", "b") for i in range(77)])
    con.commit()
    con.close()
    big = os.urandom(1 << 20) + b"x" * 1000
    (root / "John Smith" / "scan.pdf").write_bytes(big)
    (root / "John Smith" / "2019" / "scan copy.pdf").write_bytes(big)
    (root / "John Smith" / "photo.jpg").write_bytes(b"\xff\xd8" + b"0" * 5000)
    (root / "empty.txt").write_bytes(b"")
    (root / "$RECYCLE.BIN" / "junk.pdf").write_bytes(b"x" * 999)
    (root / ".hidden" / "secret.pdf").write_bytes(b"x" * 999)
    (root / "Thumbs.db").write_bytes(b"x")
    with zipfile.ZipFile(root / "Accounting" / "old.zip", "w") as z:
        z.writestr("a/notes.docx", b"y" * 3000)
        z.writestr("b/clip.mp4", b"z" * 7000)
    return root


def test_counts_and_types(drive, tmp_path):
    lst = tmp_path / "private" / "files.csv"
    inv = inventory.run_inventory([str(drive)], private_list=str(lst))
    assert inv["complete"]
    # 8 files counted: csv, xlsx, sqlite, 2 pdf, jpg, empty.txt, zip. Recycle bin, hidden
    # folder and Thumbs.db are skipped.
    assert inv["total"]["files"] == 8
    assert inv["skipped"]["folders"] == 2 and inv["skipped"]["system_files"] == 1
    types = {r["type"]: r["files"] for r in inv["by_type"]}
    assert types["Documents"] == 2
    assert types["Spreadsheets and data tables"] == 2
    assert types["Databases and database backups"] == 1
    assert types["Photos and images"] == 1
    assert inv["total"]["empty_files"] == 1
    # records: 250 CSV rows + 40 xlsx rows; sqlite 77
    assert inv["tables"]["records"] == 290
    assert inv["databases"]["records"] == 77
    kinds = {p["kind"] for p in inv["personal_data"]}
    assert {"Names of people", "Email addresses", "Phone numbers",
            "Social Security or tax ID numbers"} <= kinds
    # duplicates: the two identical 1 MB scans
    assert inv["duplicates"]["extra_copies"] == 1
    assert inv["duplicates"]["extra_bytes"] == (1 << 20) + 1000
    # zip listed, not unpacked
    assert inv["zips"]["files"] == 2 and inv["zips"]["bytes"] == 10000
    assert {f["name"] for f in inv["folders"]} >= {"John Smith", "Accounting"}
    # private file list has every counted file
    rows = list(csv.reader(open(lst, encoding="utf-8-sig")))
    assert len(rows) == 1 + 8


def test_package_hides_names(drive, tmp_path):
    inv = inventory.run_inventory([str(drive)])
    answers = {"contact": {"company": "Acme Co"}, "datasets": [{"name": "All files"}],
               "company": {}}
    out = tmp_path / "out"
    r = inventory_report.build_inventory_package(inv, answers, str(out), show_names=False)
    with zipfile.ZipFile(r["zip"]) as z:
        names = set(z.namelist())
        assert "data_inventory.html" in names and "data_inventory.json" in names
        blob = b"".join(z.read(n) for n in names).decode("utf-8")
    for secret in ("John Smith", "customers.csv", "scan.pdf", str(tmp_path), "Accounting"):
        assert secret not in blob, secret
    assert "Folder 1" in blob and "Spreadsheet 1 (.csv)" in blob
    assert "—" not in blob  # no em dashes
    j = json.loads(open(out / "Inventory contents" / "data_inventory.json",
                        encoding="utf-8").read())
    assert j["kind"] == "inventory" and j["inventory"]["total"]["files"] == 8


def test_package_shows_names_when_asked(drive, tmp_path):
    inv = inventory.run_inventory([str(drive)], find_duplicates=False)
    r = inventory_report.build_inventory_package(
        inv, {"contact": {"company": "Acme"}}, str(tmp_path / "o"), show_names=True)
    html = open(r["html"], encoding="utf-8").read()
    assert "Accounting" in html and "customers.csv" in html
    assert str(tmp_path) not in html  # never the absolute path
    assert "not checked" in html


def test_cancel_stops(drive):
    ev = threading.Event()
    ev.set()
    inv = inventory.run_inventory([str(drive)], cancel=ev)
    assert not inv["complete"]


def test_single_file_and_estimate(tmp_path, monkeypatch):
    p = tmp_path / "big.csv"
    with open(p, "w") as f:
        f.write("a,b\n")
        for i in range(5000):
            f.write(f"{i},{i}\n")
    monkeypatch.setattr(inventory, "MAX_EXACT_CSV_BYTES", 100)
    inv = inventory.run_inventory([str(p)])
    assert inv["total"]["files"] == 1
    assert inv["tables"]["records_estimated"]
    assert 4000 < inv["tables"]["records"] < 6000


def test_categories_cover_common_types():
    assert inventory.category_of("x.PST") == "Emails and mailboxes"
    assert inventory.category_of("x.dwg") == "Design, CAD and 3D"
    assert inventory.category_of("x.mov") == "Video"
    assert inventory.category_of("x.dcm") == "Medical images (DICOM)"
    assert inventory.category_of("x.weird") == inventory.OTHER
    assert inventory.pii_kinds("Date of Birth") == ["Dates of birth"]
    assert inventory.pii_kinds("Project Name") == []
