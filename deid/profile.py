"""Column profile of a whole table, computed on DE-IDENTIFIED values only.

A buyer-facing summary: how full each field is, how varied, ranges, common values.
Small-cell suppression: a value is only listed if at least MIN_CELL rows share it, so a
rare value cannot point at one person.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from . import rules

_NUM = re.compile(r"^\s*-?\$?\d{1,3}(,\d{3})*(\.\d+)?\s*$|^\s*-?\$?\d+(\.\d+)?\s*$")

MIN_CELL = 5
DISTINCT_CAP = 50_000
COUNTER_CAP = 2_000
TOP_N = 8


@dataclass
class ColumnProfile:
    name: str
    action: str
    filled: int = 0
    total: int = 0
    numbers: int = 0
    nmin: float | None = None
    nmax: float | None = None
    nsum: float = 0.0
    text_len: int = 0
    distinct: set = field(default_factory=set)
    distinct_overflow: bool = False
    counter: Counter = field(default_factory=Counter)
    counter_overflow: bool = False

    def add(self, v) -> None:
        self.total += 1
        if v is None or v == "":
            return
        self.filled += 1
        if self.action in (rules.REMOVE, rules.NAME):
            return  # removed: only fill rate is reported, values are never touched
        num = None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            num = float(v)
        elif isinstance(v, str) and _NUM.match(v) and self.action not in (rules.CODE,
                                                                          rules.YEAR,
                                                                          rules.ZIP3):
            num = float(v.replace(",", "").replace("$", ""))
        if num is not None:
            self.numbers += 1
            f = num
            self.nmin = f if self.nmin is None else min(self.nmin, f)
            self.nmax = f if self.nmax is None else max(self.nmax, f)
            self.nsum += f
        else:
            s = str(v)
            self.text_len += len(s)
            if self.action == rules.YEAR and s[:4].isdigit():
                y = float(s[:4])
                self.nmin = y if self.nmin is None else min(self.nmin, y)
                self.nmax = y if self.nmax is None else max(self.nmax, y)
        if not self.distinct_overflow:
            self.distinct.add(v)
            if len(self.distinct) > DISTINCT_CAP:
                self.distinct_overflow = True
                self.distinct = set()
        if not self.counter_overflow and self.action not in (rules.CODE, rules.TEXT):
            self.counter[v] += 1
            if len(self.counter) > COUNTER_CAP:
                self.counter_overflow = True
                self.counter = Counter()

    def kind(self) -> str:
        if self.action in (rules.REMOVE, rules.NAME):
            return "removed"
        if self.action == rules.CODE:
            return "study code"
        if self.action == rules.YEAR:
            return "year"
        if self.action == rules.TEXT:
            return "free text"
        if self.action == rules.ZIP3:
            return "ZIP3 area"
        if self.filled and self.numbers == self.filled:
            return "number"
        if not self.counter_overflow and 0 < len(self.counter) <= 50:
            return "category"
        return "text"

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "handling": rules.ACTION_LABELS[self.action],
            "kind": self.kind(),
            "filled_pct": round(100.0 * self.filled / self.total, 1) if self.total else 0.0,
        }
        if self.action in (rules.REMOVE, rules.NAME):
            return d
        d["distinct"] = f"over {DISTINCT_CAP:,}" if self.distinct_overflow else len(self.distinct)
        k = d["kind"]
        if k == "number" and self.nmin is not None:
            d["min"], d["max"] = _num(self.nmin), _num(self.nmax)
            d["mean"] = _num(self.nsum / self.numbers)
        if k == "year" and self.nmin is not None:
            d["min"], d["max"] = int(self.nmin), int(self.nmax)
        if k == "free text" and self.filled:
            d["avg_length"] = round(self.text_len / self.filled)
        if k in ("category", "ZIP3 area", "year", "text") and not self.counter_overflow:
            top = [(str(v), c) for v, c in self.counter.most_common(TOP_N) if c >= MIN_CELL]
            if top:
                d["top_values"] = top
        return d


def _num(x: float):
    return int(x) if float(x).is_integer() else round(x, 2)
