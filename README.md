# Data Revenue De-Identifier

**Browser version (main path for clients):** `web/` + `deid/web.py`. The same Python engine
runs in the browser via Pyodide in a Web Worker; X-ray text detection runs with ONNX Runtime
Web (`web/ocr.js`, same PP-OCRv4 detection model). Files never leave the browser. Build with
`python tools/build_web.py` (output `web/dist`, a static site); `.github/workflows/pages.yml`
publishes it to GitHub Pages on push to main. Sample mode only (max 200 images).

**Desktop version:** the same engine packaged for Windows/macOS (below).

Free desktop app (Windows + macOS) that Data Revenue clients download to make a safe data
sample in minutes. Runs fully offline: drag in spreadsheets (CSV, XLSX) and images (DICOM,
JPG, PNG, TIFF, BMP), it removes identifiers under HIPAA Safe Harbor, asks the 15 Data
Snapshot questions while it works, and produces one zip to send us:

- a random sample of de-identified rows and images (or everything, if chosen)
- `data_summary.html` / `.json`: a profile of the WHOLE data set (fill rates, ranges, common
  values with small-cell suppression) plus the client's answers (Snapshot keys)
- `DEIDENTIFICATION_REPORT.txt` with the owner attestation

The study-code key stays on the client's computer in `Documents/Data Revenue/Private - keep
on this computer`.

## Code

- `deid/rules.py`: column classification (header + values) and Safe Harbor transforms
- `deid/tables.py`: two-pass spreadsheet processing, sampling, profiling
- `deid/images.py`: DICOM allowlist header rebuild, metadata strip, OCR text redaction
- `deid/profile.py`: whole-table column profile on de-identified values
- `deid/package.py`: summary page + zip; questions in `deid/ui/buyer_questions.json` (what
  AI and data buyers ask first; keys shared with the Data Snapshot use its option keys)
- `deid/pipeline.py`: scan files/folders, run sample or full
- `deid/ui/index.html` + `app.py`: the window (pywebview: Edge WebView2 / macOS WebKit)

## Develop

```powershell
.\.venv\Scripts\python -m pytest -q tests
```

```powershell
.\.venv\Scripts\python app.py
```

`tools/ui_smoke.py <input> <shots>` clicks through the real window and saves screenshots.
`tools/make_samples.py` makes FAKE test data. `app --cli <in> <out>` runs headless.

## Release

Push a tag like `v1.1.0` to GitHub: `.github/workflows/build.yml` tests, builds the Windows
installer (Inno Setup, per-user, no admin) and two Mac disk images (Apple chip, Intel),
smoke-tests each packaged app on fake data, and attaches them to a GitHub Release with
stable names (`DataRevenue-DeIdentifier-Setup.exe`, `DataRevenue-DeIdentifier-mac-*.dmg`)
so the website links never change. Signing runs only when these repo secrets exist:
Windows `WIN_CERT_PFX`, `WIN_CERT_PASSWORD`; Mac `MAC_CERT_P12`, `MAC_CERT_PASSWORD`,
`MAC_SIGN_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`.

Website page: `LOVABLE_DEIDENTIFIER_DOWNLOAD_PAGE.md` (paste into the datarevenue.io project).

Known limits: unsigned builds warn on first open; face photos are not blurred; names in free
text are caught only when they appear in a name column or after a title; first launch on
Windows takes several seconds while the system web engine starts.
