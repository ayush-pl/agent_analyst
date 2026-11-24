"""Scoring functions for the evaluation (pure functions, no model calls)."""

from __future__ import annotations

import itertools
import math
from typing import Any, Sequence

# ------------------------------------------------------------------ result-set comparison
REL_TOL, ABS_TOL = 1e-3, 0.051  # tolerate rounding differences (e.g. ROUND(x,1) vs ROUND(x,2))


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        return s.lower()


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return a == b


def _key(v: Any) -> tuple:
    # sortable across types; None first, numbers, then strings
    if v is None:
        return (0, 0)
    if isinstance(v, float):
        return (1, v)
    return (2, v)


def _col_matches(g: list, p: list) -> bool:
    gs, ps = sorted(g, key=_key), sorted(p, key=_key)
    return len(gs) == len(ps) and all(_eq(a, b) for a, b in zip(gs, ps))


def results_match(gold_rows: Sequence[Sequence[Any]], pred_cols: Sequence[str],
                  pred_rows: Sequence[Sequence[Any]], ordered: bool) -> bool:
    """True if every gold column appears (by values) in the prediction with the same rows.

    Column names are ignored; extra predicted columns are allowed; row order is
    checked only when ``ordered``.
    """
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    g = [[_norm(v) for v in r] for r in gold_rows]
    p = [[_norm(v) for v in r] for r in pred_rows]
    k, n = len(g[0]), len(p[0]) if p else 0
    gcols = [[r[i] for r in g] for i in range(k)]
    pcols = [[r[j] for r in p] for j in range(n)]
    candidates = [[j for j in range(n) if _col_matches(gcols[i], pcols[j])] for i in range(k)]
    if any(not c for c in candidates):
        return False
    for combo in itertools.islice(itertools.product(*candidates), 2000):
        if len(set(combo)) != len(combo):
            continue
        gp = [tuple(r) for r in g]
        pp = [tuple(r[j] for j in combo) for r in p]
        if not ordered:
            gp, pp = sorted(gp, key=lambda t: [_key(x) for x in t]), sorted(pp, key=lambda t: [_key(x) for x in t])
        if all(all(_eq(a, b) for a, b in zip(x, y)) for x, y in zip(gp, pp)):
            return True
    return False


# ------------------------------------------------------------------ summary grounding
from analyst.grounding import _candidates, extract_numbers, grounding  # noqa: E402,F401
