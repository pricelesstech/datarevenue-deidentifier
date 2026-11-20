// In-browser text detection for burned-in labels on images (names, IDs, dates).
// Same detection model as the desktop app (PaddleOCR PP-OCRv4 DBNet "det", Apache-2.0),
// run with ONNX Runtime Web inside this worker. Pixels never leave the browser.
//
// Detection only: every detected text region is blacked out (fail closed). Large images
// are also scanned in overlapping 1024 px tiles so small corner labels are not lost to
// downscaling, matching deid/images.py find_text().

const DET = {limitSide: 736, maxSide: 2000, thresh: 0.3, boxThresh: 0.4, unclip: 1.6,
             tile: 1024, step: 864, tileAbove: 1400, maxBoxFrac: 0.2};
let detSession = null;

async function initOCR(modelUrl) {
  detSession = await ort.InferenceSession.create(modelUrl, {
    executionProviders: ["wasm"], graphOptimizationLevel: "all"});
}

function frameToCanvas(w, h, c, bytes) {
  const rgba = new Uint8ClampedArray(w * h * 4);
  if (c === 1) {
    for (let i = 0, j = 0; i < w * h; i++, j += 4) {
      const v = bytes[i]; rgba[j] = v; rgba[j + 1] = v; rgba[j + 2] = v; rgba[j + 3] = 255;
    }
  } else {
    for (let i = 0, j = 0, k = 0; i < w * h; i++, j += 4, k += c) {
      rgba[j] = bytes[k]; rgba[j + 1] = bytes[k + 1]; rgba[j + 2] = bytes[k + 2]; rgba[j + 3] = 255;
    }
  }
  const cv = new OffscreenCanvas(w, h);
  cv.getContext("2d").putImageData(new ImageData(rgba, w, h), 0, 0);
  return cv;
}

async function runDet(src, sx, sy, sw, sh) {
  let ratio = 1;
  const mn = Math.min(sw, sh), mx = Math.max(sw, sh);
  if (mn < DET.limitSide) ratio = DET.limitSide / mn;
  if (mx * ratio > DET.maxSide) ratio = DET.maxSide / mx;
  const rw = Math.max(32, Math.round((sw * ratio) / 32) * 32);
  const rh = Math.max(32, Math.round((sh * ratio) / 32) * 32);
  const cv = new OffscreenCanvas(rw, rh);
  const ctx = cv.getContext("2d");
  ctx.drawImage(src, sx, sy, sw, sh, 0, 0, rw, rh);
  const px = ctx.getImageData(0, 0, rw, rh).data;
  const n = rw * rh, t = new Float32Array(3 * n);
  for (let i = 0, j = 0; i < n; i++, j += 4) {  // BGR planes, (v/255 - 0.5) / 0.5
    t[i] = px[j + 2] / 127.5 - 1; t[n + i] = px[j + 1] / 127.5 - 1; t[2 * n + i] = px[j] / 127.5 - 1;
  }
  const feeds = {[detSession.inputNames[0]]: new ort.Tensor("float32", t, [1, 3, rh, rw])};
  const out = await detSession.run(feeds);
  const prob = out[detSession.outputNames[0]].data;

  // binarize + 2x2 dilation
  const bm = new Uint8Array(n);
  for (let y = 0; y < rh; y++) {
    for (let x = 0; x < rw; x++) {
      const i = y * rw + x;
      if (prob[i] > DET.thresh) {
        bm[i] = 1;
        if (x + 1 < rw) bm[i + 1] = 1;
        if (y + 1 < rh) { bm[i + rw] = 1; if (x + 1 < rw) bm[i + rw + 1] = 1; }
      }
    }
  }
  // connected components (8-neighbour), axis-aligned box per component
  const lab = new Int32Array(n), stack = new Int32Array(n);
  const boxes = [];
  let next = 0;
  for (let s = 0; s < n; s++) {
    if (!bm[s] || lab[s]) continue;
    next++;
    let sp = 0; stack[sp++] = s; lab[s] = next;
    let x0 = rw, y0 = rh, x1 = 0, y1 = 0;
    while (sp) {
      const p = stack[--sp], py = (p / rw) | 0, pxx = p - py * rw;
      if (pxx < x0) x0 = pxx; if (pxx > x1) x1 = pxx; if (py < y0) y0 = py; if (py > y1) y1 = py;
      for (let dy = -1; dy <= 1; dy++) {
        const ny = py + dy; if (ny < 0 || ny >= rh) continue;
        for (let dx = -1; dx <= 1; dx++) {
          const nx = pxx + dx; if (nx < 0 || nx >= rw) continue;
          const q = ny * rw + nx;
          if (bm[q] && !lab[q]) { lab[q] = next; stack[sp++] = q; }
        }
      }
    }
    const bw = x1 - x0 + 1, bh = y1 - y0 + 1;
    if (Math.min(bw, bh) < 3) continue;
    if (bh > DET.maxBoxFrac * rh && bh > 40) continue;  // anatomy, not a text line
    let sum = 0;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) sum += prob[y * rw + x];
    if (sum / (bw * bh) < DET.boxThresh) continue;
    const d = (bw * bh * DET.unclip) / (2 * (bw + bh));
    if (Math.min(bw, bh) + 2 * d < 5) continue;
    const kx = sw / rw, ky = sh / rh;
    boxes.push([
      Math.max(0, Math.floor(sx + (x0 - d) * kx)), Math.max(0, Math.floor(sy + (y0 - d) * ky)),
      Math.min(sx + sw, Math.ceil(sx + (x1 + 1 + d) * kx)),
      Math.min(sy + sh, Math.ceil(sy + (y1 + 1 + d) * ky))]);
  }
  return boxes;
}

// boxes [[x0, y0, x1, y1], ...] in image pixels for one 8-bit frame
async function detectText(w, h, c, bytes) {
  const src = frameToCanvas(w, h, c, bytes);
  let boxes = await runDet(src, 0, 0, w, h);
  if (Math.max(w, h) > DET.tileAbove) {
    const T = DET.tile, S = DET.step;
    for (let y = 0; y < Math.max(h - T, 0) + S; y += S) {
      for (let x = 0; x < Math.max(w - T, 0) + S; x += S) {
        const y0 = Math.min(y, Math.max(h - T, 0)), x0 = Math.min(x, Math.max(w - T, 0));
        boxes = boxes.concat(await runDet(src, x0, y0, Math.min(T, w), Math.min(T, h)));
      }
    }
  }
  return boxes;
}
