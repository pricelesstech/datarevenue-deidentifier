"""Data Revenue De-Identifier: desktop app (Windows + macOS). Runs fully offline.

The window is a local HTML page (deid/ui/index.html) shown in the operating system's own
web view; it loads nothing from the internet. All work happens in Python below.

Headless test mode for packaged builds:  <app> --cli <input> <output>
"""
from __future__ import annotations

import datetime as _dt
import json
import multiprocessing
import os
import subprocess
import sys
import threading

from deid import __version__, package, pipeline, rules


def resource(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def documents_dir() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    d = os.path.join(os.path.expanduser("~"), "Documents")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def reveal(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


class Api:
    """Methods callable from the page as window.pywebview.api.<name>()."""

    def __init__(self):
        self._window = None
        self._paths: list[str] = []
        self._inv = None
        self._lock = threading.Lock()
        self._status = {"msg": "", "n": 0, "total": 0, "done": False, "error": None}
        self._summary = None
        self._cancel = threading.Event()
        self._run_dir = None
        self._base = os.path.join(documents_dir(), "Data Revenue")
        self._private = os.path.join(self._base, "Private - keep on this computer")

    # ── page helpers ──
    def ready(self):
        """Called by the page once it is shown; DEID_TIMING=<file> records launch time."""
        t = os.environ.get("DEID_TIMING")
        if t:
            import time

            with open(t, "w") as f:
                f.write(str(time.time()))

    def questions(self):
        return package.load_questions()

    def pick(self, kind):
        import webview

        if kind == "folder":
            r = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        else:
            r = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
        return list(r) if r else []

    def clear(self):
        self._paths, self._inv = [], None

    def scan(self, paths):
        try:
            for p in paths or []:
                if p and p not in self._paths:
                    self._paths.append(p)
            inv = pipeline.scan(self._paths, self._base)
            self._inv = inv
            return {
                "tables": len(inv.tables),
                "images": len(inv.images),
                "dicom": sum(1 for _p, k in inv.images if k == "dicom"),
                "other": len(inv.other),
                "columns": [{"name": h, "action": d["action"]}
                            for h, d in sorted(inv.headers.items(), key=lambda kv: kv[0].lower())],
                "labels": [[a, rules.ACTION_LABELS[a]] for a in rules.ACTIONS],
            }
        except Exception as e:  # noqa: BLE001
            return {"error": f"Could not read those files: {e}"}

    def _on_drop(self, event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        r = self.scan(paths) if paths else {"error": "Could not read the dropped items. "
                                                      "Try Choose a folder instead."}
        self._window.evaluate_js(f"window.onScanned({json.dumps(r)})")

    # ── run ──
    def start(self, opts):
        if not self._inv:
            return {"error": "Add some files first."}
        company = package.safe_company(opts.get("company", ""))
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H%M")
        self._run_dir = os.path.join(self._base, f"{company} {stamp}")
        share = os.path.join(self._run_dir, "Sample contents")
        sample = opts.get("mode", "sample") == "sample"
        actions = {h: opts.get("actions", {}).get(h, d["action"])
                   for h, d in self._inv.headers.items()}
        self._cancel.clear()
        self._summary = None
        self._status = {"msg": "Starting", "n": 0, "total": 1, "done": False, "error": None}

        def prog(msg, n, total):
            self._status.update(msg=msg, n=n, total=total)

        def work():
            try:
                self._summary = pipeline.run(
                    self._inv, None, actions, ocr=bool(opts.get("ocr", True)), progress=prog,
                    cancel=self._cancel,
                    sample_rows=int(opts.get("rows", 500)) if sample else None,
                    sample_images=int(opts.get("images", 25)) if sample else None,
                    share_dir=share, private_dir=self._private)
                self._status["done"] = True
            except Exception as e:  # noqa: BLE001
                self._status["error"] = f"{type(e).__name__}: {e}"

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def status(self):
        return dict(self._status)

    def cancel(self):
        self._cancel.set()

    def finish(self, answers):
        if not self._summary:
            return {"error": "Still working. Please wait a moment."}
        try:
            r = package.build_package(self._summary, answers, self._run_dir)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Could not build the file: {e}"}
        r.update(folder=self._run_dir, private_dir=self._private,
                 review=self._summary["review"] - self._summary["other_files"],
                 cancelled=self._summary["cancelled"])
        return r

    def reveal(self, path):
        if path and os.path.exists(path):
            reveal(path)

    def open_path(self, path):
        if path and os.path.exists(path):
            open_path(path)


def _cli(src: str, out: str) -> None:
    """Headless run for testing a packaged build. Writes <out>/cli_summary.json."""
    inv = pipeline.scan(src, out)
    s = pipeline.run(inv, out, {h: d["action"] for h, d in inv.headers.items()}, ocr=True,
                     sample_rows=50, sample_images=10)
    answers = {"contact": {"company": "CLI Test"}, "datasets": [{"name": "test"}],
               "company": {}}
    s["package"] = package.build_package(s, answers, out)
    with open(os.path.join(out, "cli_summary.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, default=str)


def main():
    multiprocessing.freeze_support()
    if len(sys.argv) == 4 and sys.argv[1] == "--cli":
        _cli(sys.argv[2], sys.argv[3])
        return
    import webview
    from webview.dom import DOMEventHandler

    api = Api()
    window = webview.create_window(
        f"Data Revenue De-Identifier {__version__}", url=resource("deid", "ui", "index.html"),
        js_api=api, width=980, height=760, min_size=(760, 600), background_color="#F4F7F9")
    api._window = window

    def on_loaded():
        window.dom.document.events.drop += DOMEventHandler(api._on_drop, True, True)

    window.events.loaded += on_loaded
    # A persistent web-view profile starts much faster than a fresh temporary one per launch.
    # It only holds the web view's own cache; no client data is ever stored there.
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.path.expanduser("~/.local/share")
    storage = os.path.join(root, "Data Revenue De-Identifier", "webview")
    os.makedirs(storage, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage,
                  debug=bool(os.environ.get("DEID_DEBUG")))


if __name__ == "__main__":
    main()
