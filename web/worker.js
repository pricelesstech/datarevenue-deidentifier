// Background worker: runs the de-identification engine (Python via Pyodide) and the
// text detector (ONNX Runtime Web). Files arrive as File objects from the page, are
// copied into this worker's in-memory file system, processed, and only the finished
// zip goes back to the page as a download. Nothing is sent over the network.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
const ORT = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/";
importScripts(PYODIDE + "pyodide.js", ORT + "ort.min.js", "ocr.js");

const TABLE_EXT = new Set(["csv", "xlsx", "xlsm", "xls", "txt", "tsv"]);
const IMAGE_EXT = new Set(["jpg", "jpeg", "png", "tif", "tiff", "bmp"]);
const DICOM_EXT = new Set(["dcm", "dicom"]);
const DOC_EXT = new Set(["msg", "eml", "pdf", "docx", "docm", "doc", "odt", "pptx", "rtf", "htm", "html"]);
const ARCHIVE_EXT = new Set(["zip"]);
const SKIP = new Set(["thumbs.db", "desktop.ini", ".ds_store"]);

let py = null, web = null, files = new Map();
const here = (p) => new URL(p, self.location.href).href;
const post = (m, transfer) => self.postMessage(m, transfer || []);
self.deidProgress = (msg, n, total) => post({type: "progress", msg, n, total});

const ready = (async () => {
  post({type: "boot", msg: "Loading the secure workspace", pct: 10});
  ort.env.wasm.wasmPaths = ORT;
  ort.env.wasm.numThreads = self.crossOriginIsolated ? Math.min(4, navigator.hardwareConcurrency || 2) : 1;
  py = await loadPyodide({indexURL: PYODIDE});
  post({type: "boot", msg: "Loading data tools", pct: 40});
  await py.loadPackage(["numpy", "pillow", "micropip"]);
  const manifest = await (await fetch(here("py/manifest.json"))).json();
  const micropip = py.pyimport("micropip");
  await micropip.install(manifest.wheels.map(here), {deps: false});
  py.FS.mkdirTree("/app/deid/ui");
  for (const f of manifest.files) {
    const buf = new Uint8Array(await (await fetch(here("py/" + f))).arrayBuffer());
    py.FS.writeFile("/app/" + f, buf);
  }
  py.runPython("import sys; sys.path.insert(0, '/app')");
  web = py.pyimport("deid.web");
  post({type: "boot", msg: "Loading the text detector", pct: 75});
  await initOCR(here("models/det.onnx"));
  post({type: "boot", msg: "Ready", pct: 100, ready: true});
})().catch((e) => post({type: "boot", error: String(e && e.message || e)}));

function safePath(p) {
  const parts = String(p).replace(/\\/g, "/").split("/").filter((s) => s && s !== "." && s !== "..");
  return "/in/" + parts.join("/");
}

async function kindOf(name, file) {
  const base = name.split("/").pop().toLowerCase();
  if (base.startsWith("~$") || base.startsWith("._") || base.startsWith(".") || SKIP.has(base)) return null;
  const ext = base.includes(".") ? base.split(".").pop() : "";
  if (TABLE_EXT.has(ext)) return "table";
  if (DOC_EXT.has(ext)) return "document";
  if (ARCHIVE_EXT.has(ext)) return "archive";
  if (DICOM_EXT.has(ext)) return "dicom";
  if (IMAGE_EXT.has(ext)) return "image";
  if (file.size > 132) {
    const head = new Uint8Array(await file.slice(128, 132).arrayBuffer());
    if (head[0] === 68 && head[1] === 73 && head[2] === 67 && head[3] === 77) return "dicom";  // "DICM"
  }
  return "other";
}

function writeFile(path, buf) {
  py.FS.mkdirTree(path.substring(0, path.lastIndexOf("/")) || "/");
  py.FS.writeFile(path, new Uint8Array(buf));
}

const handlers = {
  async scan({items}) {  // items: [{path, file}]
    await ready;
    files = new Map();
    try { py.runPython("import shutil, os; shutil.rmtree('/in', ignore_errors=True)"); } catch (e) {}
    const listing = [];
    let n = 0;
    for (const {path, file} of items) {
      n++;
      if (n % 200 === 0) post({type: "progress", msg: `Looking through files (${n})`, n, total: items.length});
      const kind = await kindOf(path, file);
      if (!kind) continue;
      let p = safePath(path);
      while (files.has(p)) p = p.replace(/(\.[^./]*)?$/, "_" + n + "$1");
      files.set(p, file);
      if (kind === "table" || kind === "archive") writeFile(p, await file.arrayBuffer());
      listing.push({path: p, kind});
    }
    return JSON.parse(web.scan(JSON.stringify(listing)));
  },

  async start(opts) {
    await ready;
    post({type: "progress", msg: "Getting your files ready", n: 0, total: 1});
    const chosen = JSON.parse(web.start(JSON.stringify(opts)));
    // files from inside zips are already in memory; the rest are copied in now
    for (const p of chosen) { const f = files.get(p); if (f) writeFile(p, await f.arrayBuffer()); }
    web.prepare();
    const total = web.make_jobs();
    for (let i = 0; i < total; i++) {
      post({type: "progress", msg: `Checking image ${i + 1} of ${total} for printed text`, n: i, total});
      let boxes = [];
      if (opts.ocr) {
        const fp = web.ocr_frames(i);
        const frames = fp.toJs();
        fp.destroy();
        for (const [w, h, c, bytes] of frames) boxes.push(await detectText(w, h, c, bytes));
      }
      web.process_image(i, JSON.stringify(boxes));
    }
    return JSON.parse(web.finish());
  },

  async package({answers}) {
    const r = JSON.parse(web.package_files(JSON.stringify(answers)));
    const zip = py.FS.readFile(r.zip).buffer;
    const html = py.FS.readFile(r.html).buffer;
    const key = r.key ? py.FS.readFile(r.key).buffer : null;
    return {__transfer: [zip, html].concat(key ? [key] : []),
            name: r.zip.split("/").pop(), size_mb: r.size_mb, zip, html, key,
            review: r.review_files};
  },
};

self.onmessage = async (ev) => {
  const {id, method, args} = ev.data;
  try {
    const result = await handlers[method](args || {});
    const transfer = (result && result.__transfer) || [];
    if (result) delete result.__transfer;
    post({id, result}, transfer);
  } catch (e) {
    post({id, error: String(e && e.message || e)});
  }
};
