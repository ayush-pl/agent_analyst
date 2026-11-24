"""Check that numbers in a written summary come from the query result.

Used by the agent (to reject a small model's summary that invents numbers) and by the
evaluation (to score summaries).
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Sequence

_DATE = re.compile(r"\b\d{4}-\d{2}(-\d{2})?\b|\b\d{4}-?Q[1-4]\b|\bQ[1-4]\b", re.I)
_NUM = re.compile(r"(?<![\w.])[-−]?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kmb%])?(?![\w])", re.I)


def extract_numbers(text: str) -> list[float]:
    text = _DATE.sub(" ", text)
    out = []
    for m in _NUM.finditer(text):
        val = float((m.group(1) + (m.group(2) or "")).replace(",", ""))
        suffix = (m.group(3) or "").lower()
        if suffix in ("k", "m", "b"):
            val *= {"k": 1e3, "m": 1e6, "b": 1e9}[suffix]
        if not suffix and val.is_integer() and (val <= 10 or 2000 <= val <= 2100):
            continue  # small counts ("3 regions") and years are not claims about the data
        out.append(val)
    return out


def _candidates(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[float]:
    cands = [float(len(rows))]
    cols = list(zip(*rows)) if rows else []
    for col in cols:
        nums = [float(v) for v in col if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums:
            continue
        cands += nums
        total = sum(nums)
        cands += [total, total / len(nums), max(nums) - min(nums)]
        sub = nums[:40]
        for a, b in itertools.permutations(sub, 2):
            cands.append(a - b)
            if b:
                cands += [100 * a / b, 100 * (a - b) / b, a / b]
        if total:
            cands += [100 * x / total for x in sub]
    return cands


def grounding(summary: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict:
    """Share of numbers in the summary traceable to the result (cells or simple derivations)."""
    nums = extract_numbers(summary)
    if not nums:
        return {"numbers": 0, "grounded": 0, "rate": None, "ungrounded": []}
    cands = _candidates(columns, rows)
    bad = []
    for x in nums:
        tol = max(abs(x) * 0.006, 0.051)  # displayed precision: "1.09M", "42.4%"
        if not any(abs(abs(x) - abs(c)) <= tol for c in cands):
            bad.append(x)
    return {"numbers": len(nums), "grounded": len(nums) - len(bad), "rate": 1 - len(bad) / len(nums),
            "ungrounded": bad}
