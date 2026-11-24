"""Bring your own data: load a CSV into SQLite so the same agent can analyse it.

    from analyst.datasets import import_csv
    info = import_csv("sales.csv", "uploads/sales.db")
    async with DataAnalyst(db_path=info.db_path) as a: ...

The import cleans what usually breaks text-to-SQL on real files:

* delimiter and encoding are detected (``,`` ``;`` ``\\t`` ``|``; UTF-8 or Latin-1);
* headers become safe snake_case identifiers (``"Unit Price ($)"`` -> ``unit_price``);
* ``$1,234.50`` / ``12%`` / ``1.2k``-free numeric text becomes numbers;
* date columns in any common format become ISO text so ``strftime`` works.

It also writes a ``<db>.notes.md`` sidecar ("dataset notes"). The MCP server returns
it with the schema, so every SQL model sees it: the Claude/local prompts read it as
rules and the fine-tuned model gets it as BIRD-style evidence.

``profile()`` and ``suggest_questions()`` give an instant overview computed in code,
so the "summary of my file" never contains a made-up number.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analyst.llm import _additive

MAX_ROWS = 1_000_000
_ID_NAME = re.compile(r"(^|_)(id|uuid|key|code|zip|postcode|phone)$")


@dataclass
class DatasetInfo:
    db_path: str
    table: str
    rows: int
    columns: dict[str, str]                     # column -> "number" | "date" | "text"
    renamed: dict[str, str] = field(default_factory=dict)   # original header -> column
    notes: str = ""


def notes_path(db_path: str | Path) -> Path:
    return Path(db_path).with_suffix(".notes.md")


def safe_identifier(name: str, taken: set[str], fallback: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_").lower() or fallback
    if s[0].isdigit():
        s = f"c_{s}"
    if s in {"select", "from", "where", "group", "order", "table", "index", "limit", "by", "and", "or", "not"}:
        s = f"{s}_"
    base, i = s, 2
    while s in taken:
        s, i = f"{base}_{i}", i + 1
    taken.add(s)
    return s


def _read(path: Path):
    import pandas as pd

    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        sep = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|").delimiter
    except csv.Error:
        sep = ","
    return pd.read_csv(io.StringIO(text), sep=sep, nrows=MAX_ROWS, skipinitialspace=True,
                       dtype=str, keep_default_na=True, na_values=["", "NA", "N/A", "null", "NULL", "-"])


_NUM_CLEAN = re.compile(r"[\s$€£¥,]")


def _as_number(s):
    """Numeric text ("$1,234.50", "12%", "(300)") -> numbers, if >=95% of values parse."""
    import pandas as pd

    v = s.dropna().astype(str).str.strip()
    if v.empty:
        return None, False
    text = s.astype(str).str.strip()
    cleaned = text.str.replace(_NUM_CLEAN, "", regex=True).str.rstrip("%").str.strip("()")
    num = pd.to_numeric(cleaned.where(s.notna()), errors="coerce")
    if num.notna().sum() < 0.95 * len(v):
        return None, False
    neg = text.str.fullmatch(r"\(.*\)") & num.notna()
    num[neg] = -num[neg]
    return num, v.str.endswith("%").mean() > 0.9


def _as_date(s):
    import warnings

    import pandas as pd

    v = s.dropna().astype(str).str.strip()
    if v.empty or not v.str.contains(r"\d").all() or v.str.fullmatch(r"-?\d+(\.\d+)?").mean() > 0.5:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for kw in ({"format": "ISO8601"}, {"format": "mixed", "dayfirst": False}):
            try:
                d = pd.to_datetime(s, errors="coerce", **kw)
            except (ValueError, TypeError):
                continue
            if d.notna().sum() >= 0.9 * len(v):
                has_time = ((d.dt.hour != 0) | (d.dt.minute != 0)).fillna(False).any()
                return d.dt.strftime("%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d")
    return None


def import_csv(src: str | Path, db_path: str | Path, table: str | None = None,
               user_notes: str = "", display_name: str | None = None) -> DatasetInfo:
    """Load ``src`` into table ``table`` (default: from the file name) of a new SQLite DB."""
    src, db_path = Path(src), Path(db_path)
    name = display_name or src.name
    df = _read(src)
    if df.empty or len(df.columns) == 0:
        raise ValueError(f"{name} has no rows.")
    taken: set[str] = set()
    renamed, kinds, pct_cols = {}, {}, []
    out = {}
    for i, col in enumerate(df.columns):
        new = safe_identifier(col, taken, f"col_{i + 1}")
        if str(col) != new:
            renamed[str(col)] = new
        s = df[col]
        num, pct = _as_number(s)
        if num is not None:
            out[new], kinds[new] = num, "number"
            if pct:
                pct_cols.append(new)
            continue
        d = _as_date(s)
        if d is not None:
            out[new], kinds[new] = d, "date"
            continue
        out[new], kinds[new] = s.where(s.isna(), s.astype(str).str.strip()), "text"

    import pandas as pd

    clean = pd.DataFrame(out)
    tname = safe_identifier(table or Path(name).stem, set(), "data")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    # Percentages are stored as REAL even when whole ("15%"): in SQLite, discount / 100 on an
    # INTEGER column is integer division and silently returns 0.
    dtypes = {c: ("REAL" if k == "number" and (c in pct_cols or not (clean[c].dropna() % 1 == 0).all()) else
                  "INTEGER" if k == "number" else "TEXT") for c, k in kinds.items()}
    clean.to_sql(tname, con, index=False, dtype=dtypes)
    con.commit()
    con.close()

    dates = [c for c, k in kinds.items() if k == "date"]
    lines = [f"Table {tname} was uploaded from the file {name} ({len(clean):,} rows, one row per record)."]
    if dates:
        lines.append(f"Date columns ({', '.join(dates)}) are ISO text 'YYYY-MM-DD': use strftime('%Y-%m', col) "
                     "for months and strftime('%Y', col) for years.")
    if pct_cols:
        lines.append(f"Columns {', '.join(pct_cols)} were percentages: 12.5 means 12.5%.")
    if renamed:
        shown = list(renamed.items())[:12]
        lines.append("Original headers: " + "; ".join(f"'{o}' is {n}" for o, n in shown) + ".")
    if user_notes.strip():
        lines.append("User definitions: " + " ".join(user_notes.split())[:1500])
    notes = "\n".join(lines)
    notes_path(db_path).write_text(notes)
    return DatasetInfo(str(db_path), tname, len(clean), kinds, renamed, notes)


# ------------------------------------------------------------------ overview

def _columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(r[1], r[2].upper()) for r in con.execute(f'PRAGMA table_info("{table}")')]


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def classify(db_path: str | Path, table: str) -> dict[str, list[str]]:
    """Split columns into measures, categories, dates and identifiers for overviews/questions."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] or 1
    kinds: dict[str, list[str]] = {"measure": [], "category": [], "date": [], "id": [], "text": []}
    for col, typ in _columns(con, table):
        q = f'"{col}"'
        distinct = con.execute(f"SELECT COUNT(DISTINCT {q}) FROM \"{table}\"").fetchone()[0]
        if typ in ("INTEGER", "REAL"):
            unique_ints = typ == "INTEGER" and distinct > 0.9 * n and n > 20
            kinds["id" if _ID_NAME.search(col) or unique_ints else "measure"].append(col)
        else:
            sample = con.execute(f"SELECT {q} FROM \"{table}\" WHERE {q} IS NOT NULL LIMIT 50").fetchall()
            if sample and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?", str(r[0])) for r in sample):
                kinds["date"].append(col)
            elif _ID_NAME.search(col) or distinct > 0.9 * n and n > 20:
                kinds["id"].append(col)
            elif distinct <= max(50, int(0.05 * n)):
                kinds["category"].append(col)
            else:
                kinds["text"].append(col)
    con.close()
    return kinds


def _measures(kinds: dict[str, list[str]]) -> list[str]:
    """Measures with additive ones (amounts, counts) first: those are the ones worth summing."""
    return sorted(kinds["measure"], key=lambda c: not _additive(c))


def profile(db_path: str | Path, table: str) -> str:
    """Markdown overview of a table: size, every column's type/missing/range, headline facts."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    cols = _columns(con, table)
    kinds = classify(db_path, table)
    role = {c: k for k, cs in kinds.items() for c in cs}
    rows = []
    for col, _ in cols:
        q = f'"{col}"'
        missing, distinct = con.execute(
            f'SELECT SUM({q} IS NULL), COUNT(DISTINCT {q}) FROM "{table}"').fetchone()
        r = role.get(col, "text")
        if r == "measure":
            lo, avg, hi, tot = con.execute(f'SELECT MIN({q}), AVG({q}), MAX({q}), SUM({q}) FROM "{table}"').fetchone()
            detail = f"min {_fmt(lo)} · avg {_fmt(avg)} · max {_fmt(hi)}" + (
                f" · total {_fmt(tot)}" if _additive(col) else "")
        elif r == "date":
            lo, hi = con.execute(f'SELECT MIN({q}), MAX({q}) FROM "{table}"').fetchone()
            detail = f"{lo} → {hi}"
        elif r == "category":
            top = con.execute(f'SELECT {q}, COUNT(*) c FROM "{table}" WHERE {q} IS NOT NULL '
                              f'GROUP BY {q} ORDER BY c DESC LIMIT 3').fetchall()
            detail = "most common: " + ", ".join(f"{v} ({c:,})" for v, c in top)
        else:
            detail = f"{distinct:,} distinct values"
        miss = f"{100 * (missing or 0) / n:.0f}%" if n else "–"
        rows.append(f"| `{col}` | {r} | {miss} | {detail} |")

    facts = []
    m, c, d = _measures(kinds), kinds["category"], kinds["date"]
    if m and c:
        top = con.execute(f'SELECT "{c[0]}", SUM("{m[0]}") s FROM "{table}" GROUP BY 1 ORDER BY s DESC LIMIT 1'
                          ).fetchone()
        total = con.execute(f'SELECT SUM("{m[0]}") FROM "{table}"').fetchone()[0]
        if top and total:
            facts.append(f"**{top[0]}** has the largest total `{m[0]}` among `{c[0]}` values: "
                         f"{_fmt(top[1])} ({100 * top[1] / total:.1f}% of {_fmt(total)}).")
    if m and d:
        per = con.execute(f'SELECT strftime(\'%Y-%m\', "{d[0]}") p, SUM("{m[0]}") FROM "{table}" '
                          f'WHERE "{d[0]}" IS NOT NULL GROUP BY p ORDER BY p').fetchall()
        if len(per) >= 2:
            best = max(per, key=lambda r: r[1] or 0)
            facts.append(f"`{m[0]}` by month runs from {per[0][0]} to {per[-1][0]} ({len(per)} months); "
                         f"the highest month is **{best[0]}** at {_fmt(best[1])}.")
    con.close()
    head = f"**{table}**: {n:,} rows × {len(cols)} columns"
    out = [head, ""]
    if facts:
        out += ["**Headline facts** (computed directly from the data)", *[f"- {f}" for f in facts], ""]
    out += ["| Column | Role | Missing | Summary |", "|---|---|---|---|", *rows]
    return "\n".join(out)


def suggest_questions(db_path: str | Path, table: str, limit: int = 4, user_notes: str = "") -> list[str]:
    """Questions that fit the columns, so first-time users know what to ask.

    Measures the user defined in their notes ("revenue = qty * price") come first.
    """
    k = classify(db_path, table)
    defined = [n.lower() for n in re.findall(r"\b([A-Za-z][A-Za-z ]{1,30}?)\s*=", user_notes)]
    m, c, d = [*defined, *_measures(k)], k["category"], k["date"]
    words = lambda s: s.replace("_", " ")  # noqa: E731
    plural = lambda s: words(s) if s.endswith("s") else words(s) + "s"  # noqa: E731
    qs = []
    if m and c:
        qs.append(f"What is the total {words(m[0])} by {words(c[0])}?")
    if m and d:
        qs.append(f"How does total {words(m[0])} change by month?")
    if m and len(c) > 1:
        qs.append(f"What are the top 5 {plural(c[1])} by total {words(m[0])}?")
    elif m and c:
        qs.append(f"What are the top 5 {plural(c[0])} by total {words(m[0])}?")
    rates = [x for x in m if not _additive(x)]
    if rates and c:
        qs.append(f"What is the average {words(rates[0])} for each {words(c[0])}?")
    if c and len(qs) < limit:
        qs.append(f"How many records are there for each {words(c[0])}?")
    if not qs:
        qs.append(f"How many rows are in {table}?")
    return qs[:limit]
