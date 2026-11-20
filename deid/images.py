"""Image de-identification: DICOM, JPEG, PNG, TIFF, BMP.

Two kinds of identifiers live in an X-ray file:

1. Metadata. DICOM headers carry patient name, ID, birth date, dates, institution,
   referring doctor, private vendor tags. JPEG/PNG/TIFF carry EXIF, comments, GPS.
   DICOM: rebuilt from an ALLOWLIST of technical tags, so anything not on the list
   (including every private tag) is dropped. Other formats: all metadata dropped.
2. Text printed into the pixels ("burned in"): panoramic units usually stamp the patient
   name, ID, date and clinic in a corner. Found with an offline OCR model bundled in the
   program and painted over with solid black.

Fail closed: if a file's pixels cannot be decoded, or text detection fails, the file is
NOT written to the shareable folder; it is listed for manual review instead.
"""
from __future__ import annotations

import io
import os
import struct

import numpy as np

IMAGE_EXTS = {".dcm", ".dicom", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

_OCR = None


def _ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    return _OCR


def is_dicom(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".dcm", ".dicom"):
        return True
    if ext in IMAGE_EXTS:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


def dicom_patient_id(path: str) -> str | None:
    import pydicom

    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
        pid = getattr(ds, "PatientID", None)
        return str(pid) if pid not in (None, "") else None
    except Exception:
        return None


# ── text detection ──────────────────────────────────────────────────────────────
def _to_uint8(arr: np.ndarray, invert: bool = False) -> np.ndarray:
    a = arr.astype(np.float32)
    lo, hi = np.percentile(a, 0.5), np.percentile(a, 99.5)
    if hi <= lo:
        lo, hi = float(a.min()), float(a.max()) or 1.0
    a = np.clip((a - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255).astype(np.uint8)
    return 255 - a if invert else a


def _as_bgr(img8: np.ndarray) -> np.ndarray:
    if img8.ndim == 2:
        return np.stack([img8] * 3, axis=-1)
    if img8.shape[2] == 4:
        img8 = img8[:, :, :3]
    return img8[:, :, ::-1].copy()  # RGB -> BGR


def _run(engine, img: np.ndarray, ox: int, oy: int, boxes: list):
    res, _ = engine(img, text_score=0.3, box_thresh=0.4)
    for item in res or []:
        pts, text, score = item[0], item[1], item[2]
        if not any(ch.isalnum() for ch in str(text)):
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        boxes.append((int(min(xs)) + ox, int(min(ys)) + oy,
                      int(max(xs)) + ox, int(max(ys)) + oy, str(text)))


def find_text(img8: np.ndarray) -> list[tuple[int, int, int, int, str]]:
    """Boxes (x0, y0, x1, y1, text) of printed text. Large images are also scanned in
    overlapping tiles so small corner labels are not lost to downscaling."""
    engine = _ocr()
    bgr = _as_bgr(img8)
    h, w = bgr.shape[:2]
    boxes: list = []
    _run(engine, bgr, 0, 0, boxes)
    tile, step = 1024, 864
    if max(h, w) > 1400:
        for y in range(0, max(h - tile, 0) + step, step):
            for x in range(0, max(w - tile, 0) + step, step):
                y0, x0 = min(y, max(h - tile, 0)), min(x, max(w - tile, 0))
                _run(engine, bgr[y0:y0 + tile, x0:x0 + tile], x0, y0, boxes)
    return boxes


def _paint(arr: np.ndarray, boxes, fill, pad: int = 6) -> None:
    h, w = arr.shape[:2]
    for x0, y0, x1, y1, _t in boxes:
        arr[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)] = fill


# ── lossless metadata stripping when no pixels change ───────────────────────────
def _strip_jpeg(data: bytes) -> bytes | None:
    """Drop APP1-APP13, APP15 and COM segments (EXIF, XMP, IPTC, comments). Keeps APP0
    (JFIF) and APP14 (Adobe color transform). Pixels are untouched."""
    if data[:2] != b"\xff\xd8":
        return None
    out = bytearray(b"\xff\xd8")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: rest is image data
            out += data[i:]
            return bytes(out)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        seg = data[i:i + 2 + seglen]
        drop = (0xE1 <= marker <= 0xED) or marker == 0xEF or marker == 0xFE
        if not drop:
            out += seg
        i += 2 + seglen
    return None


_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
             b"sBIT", b"pHYs"}


def _strip_png(data: bytes) -> bytes | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    out = bytearray(data[:8])
    i = 8
    while i < len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        if ctype in _PNG_KEEP:
            out += chunk
        i += 12 + length
        if ctype == b"IEND":
            break
    return bytes(out)


# ── DICOM ───────────────────────────────────────────────────────────────────────
_KEEP_TAGS = [
    "SOPClassUID", "Modality", "ImageType", "BodyPartExamined", "ViewPosition",
    "PatientSex", "PatientOrientation", "ImageLaterality", "Laterality",
    "Manufacturer", "ManufacturerModelName", "KVP", "ExposureTime", "XRayTubeCurrent",
    "Exposure", "ExposureInuAs", "DistanceSourceToDetector", "DistanceSourceToPatient",
    "EstimatedRadiographicMagnificationFactor", "FieldOfViewShape", "SliceThickness",
    "SamplesPerPixel", "PhotometricInterpretation", "Rows", "Columns", "BitsAllocated",
    "BitsStored", "HighBit", "PixelRepresentation", "NumberOfFrames", "PixelSpacing",
    "ImagerPixelSpacing", "NominalScannedPixelSpacing", "PixelAspectRatio",
    "RescaleIntercept", "RescaleSlope", "RescaleType", "WindowCenter", "WindowWidth",
    "VOILUTFunction", "PresentationLUTShape", "LossyImageCompression",
    "LossyImageCompressionRatio", "LossyImageCompressionMethod", "PresentationIntentType",
]
_DATE_TAGS = ["StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"]
_UID_TAGS = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "FrameOfReferenceUID"]


def _cap_age(age: str) -> str | None:
    try:
        n, unit = int(age[:3]), age[3:4].upper()
    except (ValueError, TypeError, IndexError):
        return None
    if unit == "Y" and n >= 90:
        return "090Y"
    return age


def _dicom(job: dict) -> dict:
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID, generate_uid

    src, secret = job["src"], job["secret"]
    ds = pydicom.dcmread(src, force=True)
    if "PixelData" not in ds:
        return {"status": "review", "reason": "DICOM file has no image (report or DICOMDIR)"}
    photometric = str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")).upper()
    if photometric == "PALETTE COLOR":
        return {"status": "review", "reason": "palette-color DICOM not supported"}
    try:
        arr = ds.pixel_array.copy()
    except Exception as e:  # noqa: BLE001
        return {"status": "review", "reason": f"could not decode image ({type(e).__name__})"}

    frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    spp = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    found = 0
    texts: list[str] = []
    if job["ocr"]:
        frame_list = [arr] if frames == 1 else [arr[i] for i in range(frames)]
        for fr in frame_list:
            if spp == 1:
                img8 = _to_uint8(fr, invert=(photometric == "MONOCHROME1"))
                fill = fr.max() if photometric == "MONOCHROME1" else fr.min()
            else:
                img8 = _to_uint8(fr)
                fill = 0
            boxes = find_text(img8)
            _paint(fr, boxes, fill)
            found += len(boxes)
            texts += [b[4] for b in boxes]
        if frames > 1:
            arr = np.stack(frame_list)

    new = Dataset()
    for kw in _KEEP_TAGS:
        if kw in ds:
            new.add(ds[kw])
    if "PatientAge" in ds:
        capped = _cap_age(str(ds.PatientAge))
        if capped:
            new.PatientAge = capped
    for kw in _DATE_TAGS:
        v = str(getattr(ds, kw, "") or "")
        if len(v) >= 4 and v[:4].isdigit():
            setattr(new, kw, v[:4] + "0101")
    for kw in _UID_TAGS:
        v = getattr(ds, kw, None)
        if v:
            setattr(new, kw, generate_uid(entropy_srcs=[secret, str(v)]))
    if "SOPInstanceUID" not in new:
        new.SOPInstanceUID = generate_uid(entropy_srcs=[secret, src])
    if "SOPClassUID" not in new:
        new.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"  # secondary capture
    code = job.get("code") or ""
    new.PatientName = code or "ANONYMOUS"
    new.PatientID = code or "ANONYMOUS"
    new.PatientIdentityRemoved = "YES"
    new.DeidentificationMethod = (  # LO: 64 characters max
        "HIPAA Safe Harbor; header allowlist"
        + ("; burned-in text removed" if job["ocr"] else "")
    )
    if job["ocr"]:
        new.BurnedInAnnotation = "NO"
    if photometric.startswith("YBR"):
        new.PhotometricInterpretation = "RGB"  # pydicom decoded to RGB
    if spp > 1:
        new.PlanarConfiguration = 0
    new.PixelData = arr.tobytes()
    new["PixelData"].VR = "OB" if int(ds.BitsAllocated) <= 8 else "OW"

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = new.SOPClassUID
    meta.MediaStorageSOPInstanceUID = new.SOPInstanceUID
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    new.file_meta = meta
    new.preamble = b"\x00" * 128
    out = os.path.join(job["dst_dir"], job["out_stem"] + ".dcm")
    new.save_as(out, enforce_file_format=True)
    year = str(getattr(new, "StudyDate", "") or "")[:4]
    return {"status": "ok", "out": out, "text_boxes": found, "texts": texts,
            "info": {"format": "DICOM", "w": int(ds.Columns), "h": int(ds.Rows),
                     "modality": str(getattr(ds, "Modality", "") or ""),
                     "manufacturer": str(getattr(ds, "Manufacturer", "") or ""),
                     "bits": int(getattr(ds, "BitsStored", 8) or 8),
                     "year": int(year) if year.isdigit() else None}}


# ── ordinary images ─────────────────────────────────────────────────────────────
def _plain(job: dict) -> dict:
    from PIL import Image, ImageSequence

    src = job["src"]
    ext = os.path.splitext(src)[1].lower()
    with open(src, "rb") as f:
        data = f.read()
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # noqa: BLE001
        return {"status": "review", "reason": f"could not open image ({type(e).__name__})"}

    frames = [fr.copy() for fr in ImageSequence.Iterator(im)]
    found = 0
    texts: list[str] = []
    new_frames = []
    for fr in frames:
        mode = fr.mode
        if mode in ("P", "PA", "LA", "CMYK", "YCbCr", "1"):
            fr = fr.convert("RGB" if mode != "1" and mode != "LA" else "L")
            mode = fr.mode
        arr = np.array(fr)
        if job["ocr"]:
            img8 = arr if arr.dtype == np.uint8 else _to_uint8(arr)
            boxes = find_text(img8)
            if boxes:
                arr = arr.copy()
                _paint(arr, boxes, 0)
            found += len(boxes)
            texts += [b[4] for b in boxes]
        new_frames.append(Image.fromarray(arr, mode=mode if mode != "I;16" else None))

    out_ext = {".jpeg": ".jpg", ".tif": ".tiff"}.get(ext, ext)
    out = os.path.join(job["dst_dir"], job["out_stem"] + out_ext)
    info = {"format": out_ext.lstrip(".").upper(), "w": im.width, "h": im.height,
            "modality": "", "manufacturer": "", "bits": 16 if "16" in im.mode else 8,
            "year": None}
    ok = {"status": "ok", "out": out, "text_boxes": found, "texts": texts, "info": info}
    if found == 0 and ext in (".jpg", ".jpeg"):
        stripped = _strip_jpeg(data)
        if stripped is not None:
            with open(out, "wb") as f:
                f.write(stripped)
            return ok
    if found == 0 and ext == ".png":
        stripped = _strip_png(data)
        if stripped is not None:
            with open(out, "wb") as f:
                f.write(stripped)
            return ok
    first = new_frames[0]
    if out_ext == ".jpg":
        first.convert("RGB" if first.mode not in ("L", "RGB") else first.mode).save(
            out, "JPEG", quality=95, subsampling=0)
    elif out_ext == ".tiff":
        first.save(out, "TIFF", compression="tiff_lzw", save_all=len(new_frames) > 1,
                   append_images=new_frames[1:])
    elif out_ext == ".png":
        first.save(out, "PNG", optimize=False)
    else:
        first.save(out, "BMP")
    return ok


def process(job: dict) -> dict:
    """Worker entry point. Never raises: every failure becomes a review item."""
    try:
        res = _dicom(job) if job["kind"] == "dicom" else _plain(job)
    except Exception as e:  # noqa: BLE001
        res = {"status": "review", "reason": f"{type(e).__name__}: {e}"[:300]}
    res["src"] = job["src"]
    res["code"] = job.get("code")
    return res
