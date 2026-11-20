"""Make FAKE patient files for testing. Every name, number and date here is invented."""
from __future__ import annotations

import csv
import datetime as dt
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

PATIENTS = [
    ("John", "Smithfield", "004482913", "1961-03-14", "12 Oak Lane", "Provo", "UT", "84604",
     "(801) 555-0142", "john.smithfield@example.com", 63),
    ("Maria", "Quintero", "117730", "1932-11-02", "9 Elm St", "Orem", "UT", "84057-1234",
     "801.555.0199", "mq@example.org", 93),
    ("Aiden", "Pakulski", "558201", "2011-07-21", "400 Center", "Lehi", "UT", "03601",
     "8015550123", "aiden.p@example.net", 13),
]


def _font(size):
    for f in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def pano(w=2600, h=1300, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 60 + 40 * np.exp(-(((xx - w / 2) / (w / 2.4)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2))
    arch = np.exp(-((yy - (h * 0.55 + 0.00025 * (xx - w / 2) ** 2)) / 60.0) ** 2) * 140
    img = np.clip(base + arch + rng.normal(0, 8, (h, w)), 0, 255).astype(np.uint8)
    return img


def burn(img8, lines, corner="tl"):
    im = Image.fromarray(img8)
    d = ImageDraw.Draw(im)
    f = _font(34)
    x, y = (30, 24) if corner == "tl" else (img8.shape[1] - 760, img8.shape[0] - 150)
    for ln in lines:
        d.text((x, y), ln, fill=255, font=f)
        y += 44
    return np.array(im)


def main(out):
    os.makedirs(out, exist_ok=True)
    os.makedirs(os.path.join(out, "xrays", "Smithfield John"), exist_ok=True)
    rows = []
    for i, (fn, ln, mrn, dob, addr, city, st, z, ph, em, age) in enumerate(PATIENTS):
        rows.append({
            "Patient Name": f"{ln}, {fn}", "MRN": mrn, "DOB": dob, "Address": addr,
            "City": city, "State": st, "Zip": z, "Phone": ph, "Email": em, "Age": age,
            "Visit Date": f"0{i + 3}/1{i}/2023", "Diagnosis": "Class II malocclusion",
            "Airway Notes": f"Pt {fn} {ln} seen by Dr. Hallworth on 3/14/2023, call {ph}. "
                            f"Mother {fn}a reports snoring. Ref #88231944.",
            "Teeth Count": 28, "Insurance Member ID": f"XYZ{mrn}",
            "Extra": "contact at 801-555-0000",
        })
    with open(os.path.join(out, "patients.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Visits"
    ws.append(["Patient ID", "First Name", "Last Name", "Date of Service", "Procedure",
               "Comments", "SSN"])
    for fn, ln, mrn, *_ in PATIENTS:
        ws.append([int(mrn), fn, ln, dt.datetime(2022, 5, 9, 10, 30), "Pano",
                   f"{fn} tolerated well", 123456789])
    wb.save(os.path.join(out, "visits.xlsx"))

    # JPEG pano with burned-in text and EXIF, filed under the patient's name + MRN
    img = burn(pano(seed=1), ["SMITHFIELD, JOHN", "ID 004482913   DOB 03/14/1961",
                              "Hallworth Orthodontics"])
    im = Image.fromarray(img)
    exif = Image.Exif()
    exif[0x010E] = "John Smithfield pano"  # ImageDescription
    exif[0x013B] = "Dr Hallworth"  # Artist
    im.save(os.path.join(out, "xrays", "Smithfield John", "pano_004482913.jpg"), quality=92,
            exif=exif)

    # clean JPEG with no text, only EXIF (tests the lossless path)
    Image.fromarray(pano(seed=2)).save(os.path.join(out, "xrays", "clean_117730.jpg"),
                                       quality=92, exif=exif)

    # DICOM, 16-bit, burned-in text bottom right, full PHI header
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    img16 = burn(pano(seed=3), ["QUINTERO, MARIA  117730", "02-NOV-1932  Orem UT"],
                 corner="br").astype(np.uint16) * 16
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.1.3"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "Quintero^Maria"
    ds.PatientID = "117730"
    ds.PatientBirthDate = "19321102"
    ds.PatientAge = "093Y"
    ds.PatientSex = "F"
    ds.PatientAddress = "9 Elm St, Orem UT"
    ds.InstitutionName = "Hallworth Orthodontics"
    ds.ReferringPhysicianName = "Hallworth^Rex"
    ds.StudyDate = "20230314"
    ds.StudyTime = "101500"
    ds.AccessionNumber = "ACC99812"
    ds.StudyDescription = "Pano for Maria Quintero"
    ds.Modality = "PX"
    ds.Manufacturer = "FakeVendor"
    ds.add_new(0x00091010, "LO", "private Quintero note")
    ds.Rows, ds.Columns = img16.shape
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.PixelData = img16.tobytes()
    ds.save_as(os.path.join(out, "xrays", "IMG0001"), enforce_file_format=True)

    # JPEG-compressed DICOM (how many X-ray units export), text top left
    import io

    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEGBaseline8Bit

    img8 = burn(pano(seed=4), ["PAKULSKI, AIDEN", "558201  07/21/2011"])
    buf = io.BytesIO()
    Image.fromarray(img8).save(buf, "JPEG", quality=90)
    meta2 = FileMetaDataset()
    meta2.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.1.3"
    meta2.MediaStorageSOPInstanceUID = generate_uid()
    meta2.TransferSyntaxUID = JPEGBaseline8Bit
    d2 = FileDataset(None, {}, file_meta=meta2, preamble=b"\x00" * 128)
    d2.SOPClassUID = meta2.MediaStorageSOPClassUID
    d2.SOPInstanceUID = meta2.MediaStorageSOPInstanceUID
    d2.PatientName, d2.PatientID, d2.Modality = "Pakulski^Aiden", "558201", "PX"
    d2.Rows, d2.Columns = img8.shape
    d2.SamplesPerPixel, d2.PhotometricInterpretation = 1, "MONOCHROME2"
    d2.BitsAllocated, d2.BitsStored, d2.HighBit, d2.PixelRepresentation = 8, 8, 7, 0
    d2.PixelData = encapsulate([buf.getvalue()])
    d2["PixelData"].VR = "OB"
    d2.save_as(os.path.join(out, "xrays", "pakulski.dcm"), enforce_file_format=True)

    with open(os.path.join(out, "readme.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 fake")
    print("samples written to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples")
