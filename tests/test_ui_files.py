"""The app's screens are plain HTML/JS files that no other test opens. Guard against them
being damaged (a bad find-and-replace once shipped a broken desktop screen)."""
import os
import re
import subprocess
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
PAGES = [os.path.join(ROOT, "deid", "ui", "index.html"), os.path.join(ROOT, "web", "index.html")]
SCRIPTS = [os.path.join(ROOT, "web", f) for f in ("app.js", "worker.js", "ocr.js")]


@pytest.mark.parametrize("path", PAGES)
def test_pages_intact(path):
    s = open(path, encoding="utf-8").read()
    assert s.lower().startswith("<!doctype html>")
    for needle in ("Share a safe sample of your data in minutes", "s-welcome", "s-details",
                   "s-data", "s-questions", "s-done", "<script", "Get started"):
        assert needle in s, needle
    words = re.findall(r"[a-z]{4,}", s)
    assert sum(w.count("e") for w in words) / max(1, len(words)) > 0.3  # English still has e's
    assert "—" not in s  # no em dashes in client-facing text
    assert "Leeds" not in s


def test_buyer_questions():
    import json

    q = json.load(open(os.path.join(ROOT, "deid", "ui", "buyer_questions.json"), encoding="utf-8"))
    qs = q["dataset_card"]["questions"] + q["company_questions"]
    keys = [x["key"] for x in qs]
    assert len(keys) == len(set(keys))
    # the client is making the sample right now: never ask how fast they could export one
    assert "export_speed" not in keys
    assert not any("sample" in x["prompt"].lower() for x in qs)
    for k in ("data_formats", "industry", "ownership", "contracts", "ai_terms", "sensitive",
              "exclusivity", "prior_sharing"):
        assert k in keys, k
    industries = next(x for x in qs if x["key"] == "industry")["options"]
    assert len(industries) >= 10  # all industries, not one sector
    for x in qs:
        assert x["type"] in ("single", "multi") and len(x["options"]) >= 2
        assert len({o["key"] for o in x["options"]}) == len(x["options"])
    text = json.dumps(q)
    assert "—" not in text


@pytest.mark.parametrize("path", SCRIPTS)
def test_scripts_parse(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
