"""Assemble the browser version into web/dist (a static site, no server code).

  index.html app.js worker.js ocr.js icon.png
  py/deid/*.py + py/deid/ui/buyer_questions.json   the same engine as the desktop app
  py/wheels/*.whl                                      pydicom, openpyxl (pure Python)
  py/manifest.json
  models/det.onnx                                      PP-OCRv4 text detection (Apache-2.0)

python tools/build_web.py
"""
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DIST = os.path.join(WEB, "dist")
WHEELS = ["pydicom==3.0.2", "openpyxl==3.1.5", "et-xmlfile==2.0.0", "olefile==0.47",
          "xlrd==2.0.2", "pypdf==7.1.1"]


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    # exist_ok: a synced folder (OneDrive) can keep an emptied directory around briefly
    os.makedirs(os.path.join(DIST, "py", "deid", "ui"), exist_ok=True)
    os.makedirs(os.path.join(DIST, "py", "wheels"), exist_ok=True)
    os.makedirs(os.path.join(DIST, "models"), exist_ok=True)
    for f in ("index.html", "app.js", "worker.js", "ocr.js"):
        shutil.copy(os.path.join(WEB, f), DIST)
    shutil.copy(os.path.join(ROOT, "assets", "icon.png"), DIST)

    files = []
    for p in sorted(glob.glob(os.path.join(ROOT, "deid", "*.py"))):
        shutil.copy(p, os.path.join(DIST, "py", "deid"))
        files.append("deid/" + os.path.basename(p))
    shutil.copy(os.path.join(ROOT, "deid", "ui", "buyer_questions.json"),
                os.path.join(DIST, "py", "deid", "ui"))
    files.append("deid/ui/buyer_questions.json")

    subprocess.check_call([sys.executable, "-m", "pip", "download", "--no-deps",
                           "--only-binary", ":all:", "--python-version", "3.12",
                           "--platform", "any", "-q", "-d", os.path.join(DIST, "py", "wheels"),
                           *WHEELS])
    wheels = ["py/wheels/" + os.path.basename(w)
              for w in sorted(glob.glob(os.path.join(DIST, "py", "wheels", "*.whl")))]

    import rapidocr_onnxruntime

    model = os.path.join(os.path.dirname(rapidocr_onnxruntime.__file__), "models",
                         "ch_PP-OCRv4_det_infer.onnx")
    shutil.copy(model, os.path.join(DIST, "models", "det.onnx"))
    with open(os.path.join(DIST, "models", "NOTICE.txt"), "w", encoding="utf-8") as f:
        f.write("det.onnx: PaddleOCR PP-OCRv4 text detection model, Apache License 2.0, "
                "https://github.com/PaddlePaddle/PaddleOCR (ONNX export via RapidOCR).\n")

    with open(os.path.join(DIST, "py", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"files": files, "wheels": wheels}, f, indent=1)
    with open(os.path.join(DIST, ".nojekyll"), "w") as f:
        f.write("")
    print("web build in", DIST, "|", len(files), "engine files,", len(wheels), "wheels")


if __name__ == "__main__":
    main()
