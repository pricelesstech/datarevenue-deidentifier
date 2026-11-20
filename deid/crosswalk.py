"""Study codes for patient IDs.

Codes are random (P followed by digits), not derived from the original ID, which is what
Safe Harbor 164.514(c) requires of a re-identification code. The table that maps codes
back to original IDs is written ONLY to the private folder and must never be shared.

Re-running on new files with the same output folder reuses the same table, so a patient
keeps the same code across batches, and an X-ray gets the same code as the spreadsheet row.
"""
from __future__ import annotations

import csv
import os
import secrets
import threading

CROSSWALK_FILE = "patient_code_crosswalk.csv"
KEY_FILE = "project_secret.txt"


def normalize_id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value).strip().upper()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


class Crosswalk:
    def __init__(self, private_dir: str):
        self.private_dir = private_dir
        self.path = os.path.join(private_dir, CROSSWALK_FILE)
        self._map: dict[str, str] = {}
        self._used: set[str] = set()
        self._lock = threading.Lock()
        if os.path.exists(self.path):
            with open(self.path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self._map[row["original_id"]] = row["study_code"]
                    self._used.add(row["study_code"])
        key_path = os.path.join(private_dir, KEY_FILE)
        if os.path.exists(key_path):
            with open(key_path, encoding="utf-8") as f:
                self.secret = f.read().strip()
        else:
            self.secret = secrets.token_hex(32)
            os.makedirs(private_dir, exist_ok=True)
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(self.secret)

    def code_for(self, original) -> str:
        key = normalize_id(original)
        if not key:
            return ""
        with self._lock:
            code = self._map.get(key)
            if code is None:
                while True:
                    code = "P" + "".join(secrets.choice("0123456789") for _ in range(8))
                    if code not in self._used:
                        break
                self._map[key] = code
                self._used.add(code)
            return code

    def lookup(self, original) -> str | None:
        return self._map.get(normalize_id(original))

    def keys(self):
        return self._map.keys()

    def __len__(self):
        return len(self._map)

    def save(self) -> None:
        os.makedirs(self.private_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["original_id", "study_code"])
            for k, v in sorted(self._map.items(), key=lambda kv: kv[1]):
                w.writerow([k, v])
        os.replace(tmp, self.path)
