// Page logic. All heavy work happens in worker.js; this file only drives the screens.
"use strict";
const state = {inv: null, actions: {}, answers: {datasets: [{}], company: {}}, ready: false,
               runDone: false, wantFinish: false, items: []};
const order = ["welcome", "details", "data", "questions", "working", "done"];
let Q = null;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"}[c]));

// ── worker RPC ──
const worker = new Worker("worker.js");
let nextId = 1;
const pending = new Map();
function call(method, args) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, {resolve, reject});
    worker.postMessage({id, method, args});
  });
}
worker.onmessage = (ev) => {
  const m = ev.data;
  if (m.id) {
    const p = pending.get(m.id); pending.delete(m.id);
    m.error ? p.reject(new Error(m.error)) : p.resolve(m.result);
  } else if (m.type === "boot") {
    if (m.error) { $("boot-msg").textContent = "Could not load: " + m.error + ". Please refresh the page."; return; }
    $("boot-msg").textContent = m.ready ? "Ready. Everything runs in this tab." : m.msg + "... (" + m.pct + "%)";
    if (m.ready) { state.ready = true; $("boot").classList.add("ok"); refreshStart(); }
  } else if (m.type === "progress") {
    showProgress(m.msg, m.n, m.total);
  }
};
fetch("py/deid/ui/snapshot_questions.json").then((r) => r.json()).then((q) => { Q = q; });

function go(name) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.toggle("on", s.id === "s-" + name));
  const idx = Math.min(order.indexOf(name), 4);
  document.querySelectorAll("#steps div").forEach((d, i) => d.classList.toggle("on", i <= idx));
  window.scrollTo(0, 0);
}

function saveDetails() {
  const c = $("company").value.trim(), e = $("email").value.trim();
  if (!c) { $("details-err").textContent = "Please enter the company or practice name."; return; }
  if (e && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) { $("details-err").textContent = "That email does not look right."; return; }
  $("details-err").textContent = "";
  state.answers.contact = {company: c, name: $("person").value.trim(), email: e};
  go("data");
}

// ── adding files ──
async function readAllEntries(dirEntry) {
  const reader = dirEntry.createReader(), all = [];
  for (;;) {
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) return all;
    all.push(...batch);
  }
}
async function walk(entry, out) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    out.push({path: entry.fullPath, file});
  } else if (entry.isDirectory) {
    for (const e of await readAllEntries(entry)) await walk(e, out);
  }
}
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", async (e) => {
  e.preventDefault();
  if (!$("s-data").classList.contains("on")) return;
  const entries = [...e.dataTransfer.items].map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  const out = [];
  if (entries.length) { for (const en of entries) await walk(en, out); }
  else { for (const f of e.dataTransfer.files) out.push({path: f.name, file: f}); }
  addItems(out);
});
$("pick-folder").addEventListener("change", (e) => addItems([...e.target.files].map((f) => ({path: f.webkitRelativePath || f.name, file: f}))));
$("pick-files").addEventListener("change", (e) => addItems([...e.target.files].map((f) => ({path: f.name, file: f}))));

async function addItems(items) {
  if (!items.length) return;
  state.items = state.items.concat(items);
  $("scan-busy").style.display = "block";
  $("scan-busy").textContent = state.ready ? "Looking through your files..." : "Finishing setup, then looking through your files...";
  $("data-err").textContent = "";
  try {
    onScanned(await call("scan", {items: state.items}));
  } catch (err) {
    $("scan-busy").style.display = "none";
    $("data-err").textContent = "Could not read those files: " + err.message;
  }
}

function onScanned(r) {
  $("scan-busy").style.display = "none";
  state.inv = r;
  const pl = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
  let h = `<span class="chip"><b>${pl(r.tables, "spreadsheet", "spreadsheets")}</b></span><span class="chip"><b>${pl(r.images, "image", "images")}</b></span>`;
  if (r.dicom) h += `<span class="chip"><b>${r.dicom.toLocaleString()}</b> of them DICOM</span>`;
  if (r.other) h += `<span class="chip">${pl(r.other, "other file", "other files")} (like PDFs) will be skipped</span>`;
  h += `<button class="btn link" onclick="clearData()">Start over</button>`;
  $("chips").innerHTML = h;
  state.actions = {};
  let t = "<tr><th>Column</th><th>What happens</th></tr>";
  r.columns.forEach((c, i) => {
    state.actions[c.name] = c.action;
    const opts = r.labels.map(([k, l]) => `<option value="${k}" ${k === c.action ? "selected" : ""}>${esc(l)}</option>`).join("");
    t += `<tr><td>${esc(c.name || "(blank)")}</td><td><select data-i="${i}">${opts}</select></td></tr>`;
  });
  $("cols").innerHTML = r.columns.length ? t : "<tr><td class='hint'>No spreadsheets added.</td></tr>";
  $("cols").querySelectorAll("select").forEach((s) => s.addEventListener("change", () => { state.actions[state.inv.columns[s.dataset.i].name] = s.value; }));
  $("scan-out").style.display = "block";
  $("drop").style.display = "none";
  refreshStart();
}
function refreshStart() {
  const ok = state.inv && state.inv.tables + state.inv.images > 0;
  $("start-btn").disabled = !(ok && state.ready);
  if (state.inv && !ok) $("data-err").textContent = "No spreadsheets or images found. Try another folder.";
}
function clearData() {
  state.inv = null; state.items = [];
  $("scan-out").style.display = "none"; $("drop").style.display = "block";
  $("pick-folder").value = ""; $("pick-files").value = "";
  refreshStart();
}

// ── run ──
function showProgress(msg, n, total) {
  const pct = total ? Math.round((100 * n) / total) : 0;
  const done = state.runDone;
  ["pbar", "pbar2"].forEach((id) => { $(id).style.width = (done ? 100 : pct) + "%"; });
  $("pmsg").textContent = done ? "Your data is de-identified." : msg;
  $("ppct").textContent = done ? "Done" : total > 1 ? pct + "%" : "";
  $("w-msg").textContent = done ? "Packaging..." : msg;
}

async function startRun() {
  const rows = parseInt($("n-rows").value, 10), imgs = parseInt($("n-imgs").value, 10);
  const opts = {rows: isNaN(rows) ? 500 : Math.max(10, rows), images: isNaN(imgs) ? 25 : Math.min(200, Math.max(0, imgs)),
                ocr: $("ocr").checked, actions: state.actions};
  buildQuestions();
  go("questions");
  state.runDone = false;
  call("start", opts).then((s) => {
    state.runDone = true; state.summary = s;
    showProgress("", 1, 1);
    if (state.wantFinish) finish();
  }).catch((err) => { alert("Something went wrong: " + err.message); go("data"); });
}

function buildQuestions() {
  if (!Q) return;
  const card = Q.dataset_card;
  let h = `<div class="q" id="q-name"><h3>${esc(card.name_question.prompt)}</h3><div class="hint">${esc(card.name_question.help || "")}</div><input type="text" id="ds-name" maxlength="${card.name_question.max_length || 120}" style="margin-top:10px"></div>`;
  const all = [[card.category_question, "ds"], ...card.questions.map((q) => [q, "ds"]), ...Q.company_questions.map((q) => [q, "co"])];
  for (const [q, scope] of all) {
    const multi = q.type === "multi";
    h += `<div class="q" data-key="${q.key}" data-scope="${scope}" data-multi="${multi}"><h3>${esc(q.prompt)}</h3>`;
    if (q.help) h += `<div class="hint">${esc(q.help)}</div>`;
    if (multi) h += `<div class="hint">Choose all that apply.</div>`;
    h += `<div class="opts">` + q.options.map((o) => `<div class="opt ${multi ? "multi" : ""}" data-v="${o.key}"><span class="dot"></span><div>${esc(o.label)}${o.hint ? `<small>${esc(o.hint)}</small>` : ""}</div></div>`).join("") + `</div></div>`;
  }
  $("qs").innerHTML = h;
  $("qs").querySelectorAll(".opt").forEach((el) => el.addEventListener("click", () => choose(el)));
}
function choose(el) {
  const q = el.closest(".q");
  if (q.dataset.multi === "true") el.classList.toggle("sel");
  else q.querySelectorAll(".opt").forEach((o) => o.classList.toggle("sel", o === el));
  q.classList.remove("missing");
}
function submitAnswers() {
  const ds = {name: $("ds-name").value.trim()}, co = {};
  let missing = null;
  if (!ds.name) { $("q-name").classList.add("missing"); missing = $("q-name"); }
  document.querySelectorAll("#qs .q[data-key]").forEach((q) => {
    const sel = [...q.querySelectorAll(".opt.sel")].map((o) => o.dataset.v);
    const multi = q.dataset.multi === "true", val = multi ? sel : sel[0];
    if (!multi && !val) { q.classList.add("missing"); missing = missing || q; }
    (q.dataset.scope === "ds" ? ds : co)[q.dataset.key] = val === undefined ? [] : val;
  });
  if (missing) { $("q-err").textContent = "Please answer the questions outlined in red."; missing.scrollIntoView({behavior: "smooth", block: "center"}); return; }
  $("q-err").textContent = "";
  state.answers.datasets = [ds]; state.answers.company = co; state.answers.notes = $("notes").value.trim();
  state.wantFinish = true;
  go("working");
  if (state.runDone) finish();
}

async function finish() {
  state.wantFinish = false;
  try {
    const r = await call("package", {answers: state.answers});
    $("dl-zip").href = URL.createObjectURL(new Blob([r.zip], {type: "application/zip"}));
    $("dl-zip").download = r.name;
    $("view-html").href = URL.createObjectURL(new Blob([r.html], {type: "text/html"}));
    if (r.key) {
      $("dl-key").href = URL.createObjectURL(new Blob([r.key], {type: "text/csv"}));
      $("dl-key").download = "PRIVATE_study_code_key_" + r.name.replace(/^DataRevenue_Sample_/, "").replace(/\.zip$/, "") + ".csv";
    } else { $("key-box").style.display = "none"; }
    $("zip-name").textContent = r.name;
    $("zip-meta").textContent = r.size_mb + " MB";
    $("done-warn").innerHTML = r.review ? `<div class="warn">${r.review.toLocaleString()} ${r.review === 1 ? "file was" : "files were"} left out because ${r.review === 1 ? "it" : "they"} could not be processed safely.</div>` : "";
    go("done");
  } catch (err) { alert("Could not build the file: " + err.message); go("questions"); }
}
window.addEventListener("beforeunload", (e) => { if (state.items.length && !$("s-done").classList.contains("on")) { e.preventDefault(); e.returnValue = ""; } });
