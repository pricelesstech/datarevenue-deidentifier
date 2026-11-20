"""Drive the real app window end to end and save screenshots. Developer use only.

python tools/ui_smoke.py <input_folder> <screenshot_dir>
"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import webview  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import app  # noqa: E402


def main(src, shots):
    os.makedirs(shots, exist_ok=True)
    api = app.Api()
    api._base = os.path.join(shots, "Data Revenue")
    api._private = os.path.join(api._base, "Private - keep on this computer")
    w = webview.create_window("Data Revenue De-Identifier", url=app.resource("deid", "ui", "index.html"),
                              js_api=api, width=980, height=760, x=40, y=40)
    api._window = w
    log = []

    def snap(name):
        time.sleep(0.8)
        ImageGrab.grab(all_screens=False).save(os.path.join(shots, name + ".png"))

    def js(code):
        return w.evaluate_js(code)

    def drive():
        try:
            time.sleep(2.5)
            w.on_top = True
            snap("1_welcome")
            js("go('details')")
            js("document.getElementById('company').value='Maple Street Dental';"
               "document.getElementById('person').value='Test User';"
               "document.getElementById('email').value='doc@example.com'")
            snap("2_details")
            js("saveDetails()")
            js(f"addPaths({json.dumps([src])})")
            for _ in range(60):
                if js("document.getElementById('scan-out').style.display") == "block":
                    break
                time.sleep(0.5)
            js("document.querySelector('details').open=true")
            snap("3_data")
            js("document.querySelector('details').open=false")
            js("startRun()")
            time.sleep(1.5)
            snap("4_questions")
            js("document.getElementById('ds-name').value='Orthodontic patient records';"
               "document.querySelectorAll('#qs .q[data-key]').forEach(q=>choose(q.querySelector('.opt')))")
            js("submitAnswers()")
            for _ in range(240):
                if js("document.getElementById('s-done').classList.contains('on')"):
                    break
                time.sleep(0.5)
            snap("5_done")
            log.append(js("JSON.stringify({zip:state.zip, title:document.getElementById('done-title').textContent})"))
        except Exception as e:  # noqa: BLE001
            log.append(f"ERROR {e!r}")
        finally:
            with open(os.path.join(shots, "log.txt"), "w") as f:
                f.write("\n".join(map(str, log)))
            w.destroy()

    threading.Thread(target=drive, daemon=True).start()
    webview.start(private_mode=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
