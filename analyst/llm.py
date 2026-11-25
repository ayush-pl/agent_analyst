"""Model layer: the SQL model, chart planner and summary writer.

``ClaudeAnalystLLM`` calls Claude through the official Anthropic SDK with
structured outputs. ``MockAnalystLLM`` is a deterministic, offline stand-in so
the demo and tests run without an API key (it deliberately writes a broken
query for one demo question so you can watch the retry loop work).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, Field

DEFAULT_MODEL = os.environ.get("ANALYST_MODEL", "claude-opus-5-5")


# --------------------------------------------------------------------------- schemas
class SQLProposal(BaseModel):
    answerable: bool = Field(default=True, description=(
        "False if the database cannot answer the question (data not present) or the request asks to "
        "modify data; then leave sql empty and explain why in rationale."))
    sql: str = Field(description="A single read-only SQLite SELECT/WITH query (empty if not answerable).")
    rationale: str = Field(description="One or two sentences explaining the approach, or why it cannot be answered.")


class ChartSpec(BaseModel):
    chart_type: Literal["bar", "hbar", "line", "none"]
    x: Optional[str] = Field(default=None, description="Result column for categories / time.")
    y: Optional[str] = Field(default=None, description="Result column holding the numeric measure.")
    series: Optional[str] = Field(default=None, description="Optional column that splits the data into series.")
    title: str = ""
    y_label: Optional[str] = None


class AnalystLLM(Protocol):
    name: str

    async def generate_sql(self, question: str, schema: str, attempts: Sequence[dict]) -> SQLProposal: ...
    async def plan_chart(self, question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> ChartSpec: ...
    async def summarize(self, question: str, sql: str, columns: Sequence[str],
                        rows: Sequence[Sequence[Any]], chart: ChartSpec | None, truncated: bool) -> str: ...


# --------------------------------------------------------------------------- helpers
def _preview(columns: Sequence[str], rows: Sequence[Sequence[Any]], limit: int = 60) -> str:
    head = [list(columns)] + [list(r) for r in rows[:limit]]
    body = "\n".join(json.dumps(r, default=str) for r in head)
    more = f"\n... ({len(rows) - limit} more rows)" if len(rows) > limit else ""
    return body + more


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_DATEISH = re.compile(r"^\d{4}(-\d{2}){0,2}$|^\d{4}-Q[1-4]$")


def heuristic_chart(question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> ChartSpec:
    """Pick a sensible chart from the shape of the result (no model call)."""
    cols = list(columns)
    if not rows or len(cols) < 2 or len(rows) == 1:
        return ChartSpec(chart_type="none", title=question)
    first = rows[0]
    num_idx = [i for i, v in enumerate(first) if _is_number(v)]
    cat_idx = [i for i, v in enumerate(first) if not _is_number(v)]
    if not num_idx:
        return ChartSpec(chart_type="none", title=question)
    y = cols[num_idx[0]]
    if not cat_idx:
        x = cols[0]
        series = None
    else:
        x = cols[cat_idx[0]]
        series = cols[cat_idx[1]] if len(cat_idx) > 1 else None
    timeish = all(_DATEISH.match(str(r[cols.index(x)])) for r in rows[:20])
    if timeish:
        kind = "line"
    elif series:
        kind = "bar"
    else:
        kind = "hbar" if len(rows) > 6 or max(len(str(r[cols.index(x)])) for r in rows) > 12 else "bar"
    return ChartSpec(chart_type=kind, x=x, y=y, series=series, title=question.rstrip("?"),
                     y_label=y.replace("_", " "))


# --------------------------------------------------------------------------- Claude
SQL_SYSTEM = """You are the SQL model inside an AI data-analyst agent.
Translate the business question into ONE read-only SQLite query against the schema provided.

Rules:
- SQLite dialect only (strftime for dates, no ILIKE, no DATE_TRUNC).
- A single SELECT or WITH statement. Never write, alter or create anything.
- Give every computed column a clear snake_case alias; round money to 2 decimals.
- Return chart-friendly shapes: a category/time column first, then numeric measures; for
  multi-series questions return long format (x, series, value).
- Order results meaningfully (time ascending, rankings descending) and LIMIT rankings to a
  reasonable top N (default 10) unless the question asks otherwise.
- Follow the "-- note:" lines in the schema: they are the dataset's business definitions
  (e.g. how revenue is calculated) and override your own assumptions.
- If previous attempts failed, read the database error carefully and fix the cause.
- If the schema does not contain the data needed (e.g. marketing spend, NPS, employees), or the
  request asks to insert/update/delete/create/drop anything, set answerable=false, leave sql
  empty and explain briefly. Do not guess with unrelated columns."""

CHART_SYSTEM = """You choose the chart for a query result in an analytics report.
Choose chart_type from: line (change over time), bar (compare a few categories or grouped
series), hbar (rankings or long category labels), none (a single number or data that does
not chart well). Never propose a second y-axis. x, y and series must be exact column names
from the result. Use series only for long-format results with a category column that splits
lines/bars (max 8 series). Write a short, specific title (no trailing period)."""

SUMMARY_SYSTEM = """You are a senior data analyst writing for a business stakeholder.
Given the question, the SQL that was run and its result, write a concise summary in Markdown:
- Lead with a one-sentence direct answer that includes the key number(s).
- Then 2-4 bullet points of supporting insights (comparisons, trends, outliers, shares).
- Only state facts supported by the data shown; note caveats (e.g. truncated results,
  partial periods) briefly. Do not restate the SQL. Keep it under 150 words."""


_CREATE_RE = re.compile(r"CREATE TABLE\s+\"?(\w+)\"?\s*\((.*?)\n\)", re.S | re.I)


def schema_columns(schema: str) -> dict[str, list[str]]:
    """Parse ``CREATE TABLE`` blocks in a formatted schema into {table: [columns]}."""
    out: dict[str, list[str]] = {}
    for table, body in _CREATE_RE.findall(schema):
        cols = []
        for line in body.split(","):
            m = re.match(r"\s*\"?(\w+)\"?\s+\w", line)
            if m and m.group(1).upper() not in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}:
                cols.append(m.group(1))
        out[table] = cols
    return out


def sql_aliases(sql: str, tables: Sequence[str]) -> dict[str, str]:
    """{alias_or_table: table} for the FROM/JOIN clauses of a query."""
    out: dict[str, str] = {}
    names = {t.lower(): t for t in tables}
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?(\w+))?", sql, re.I):
        t = names.get(m.group(1).lower())
        if not t:
            continue
        out[t] = t
        alias = m.group(2)
        if alias and alias.upper() not in {"ON", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "GROUP",
                                           "ORDER", "LIMIT", "USING", "NATURAL", "OUTER", "FULL"}:
            out[alias] = t
    return out


def closest_columns(name: str, tables: dict[str, list[str]], n: int = 3) -> list[str]:
    """``table.column`` candidates for a column name that does not exist (case, underscores,
    prefixes like customer_region -> customers.region, then fuzzy matching)."""
    import difflib

    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9]", "", x.lower())

    pairs = [(t, c) for t, cols in tables.items() for c in cols]
    target = norm(name)
    exact = [(t, c) for t, c in pairs if norm(c) == target]
    parts = [(t, c) for t, c in pairs if len(norm(c)) >= 4 and (norm(c) in target or target in norm(c))]
    names = sorted({c for _, c in pairs})
    fuzzy = difflib.get_close_matches(name.lower(), [c.lower() for c in names], n=n, cutoff=0.6)
    ranked = exact or parts or [(t, c) for f in fuzzy for t, c in pairs if c.lower() == f]
    out: list[str] = []
    for t, c in ranked:
        if f"{t}.{c}" not in out:
            out.append(f"{t}.{c}")
    return out[:n]


def error_hint(error: str, schema: str, sql: str = "") -> str:
    """Turn a raw database error into a concrete fix hint using the schema.

    Small models often re-send the same broken query when they only see the raw
    error; naming the table that actually holds a column fixes most retries.
    """
    tables = schema_columns(schema)
    m = re.search(r"no such column: (?:(\w+)\.)?(\w+)", error)
    if m:
        alias, col = m.group(1), m.group(2)
        owners = [t for t, cols in tables.items() if col in cols]
        aliases = sql_aliases(sql, list(tables)) if sql else {}
        if alias and alias in aliases and owners:
            used = [a for a, t in aliases.items() if t in owners and a != t] or owners
            return (f"In your query `{alias}` is table {aliases[alias]}, which has no column `{col}`. "
                    f"`{col}` is in {', '.join(owners)}: write it as {used[0]}.{col}"
                    + ("" if any(t in aliases.values() for t in owners) else f" and JOIN {owners[0]}") + ".")
        if owners:
            return (f"Column `{col}` exists only in table(s) {', '.join(owners)}. JOIN that table "
                    f"(via its id column) and reference it as <table_or_alias>.{col}; make sure every "
                    "alias you use is defined in FROM/JOIN.")
        # Fine-tuned models often invent near-miss names (pet_type for PetType): suggest the closest.
        close = closest_columns(col, tables)
        if close:
            return f"No table has a column `{col}`. Did you mean {', '.join(close)}?"
        return f"No table has a column `{col}`. Use only columns listed in the schema."
    m = re.search(r"ambiguous column name: (?:\w+\.)?(\w+)", error)
    if m:
        col = m.group(1)
        owners = [t for t, cols in tables.items() if col in cols]
        return (f"Column `{col}` exists in several joined tables ({', '.join(owners)}). Give every table an "
                f"alias and prefix EVERY column with it (e.g. oi.{col}).")
    m = re.search(r"no such table: (\w+)", error)
    if m:
        return f"There is no table `{m.group(1)}`. Available tables: {', '.join(tables)}."
    return ""


def retry_feedback(attempts: Sequence[dict], schema: str) -> str:
    """Prompt block describing failed attempts, with fix hints, for the SQL model."""
    lines = []
    for i, a in enumerate(attempts):
        hint = error_hint(str(a.get("error", "")), schema, a.get("sql", ""))
        lines.append(f"Attempt {i + 1}:\n```sql\n{a['sql']}\n```\nError: {a['error']}"
                     + (f"\nHint: {hint}" if hint else ""))
    return (f"<previous_failed_attempts>\n" + "\n\n".join(lines) + "\n</previous_failed_attempts>\n"
            "These queries FAILED. Do not repeat them: write a different, corrected query that fixes the cause.")


def _norm_sql(sql: str) -> str:
    return " ".join(sql.lower().replace(";", " ").split())


class ClaudeAnalystLLM:
    """Claude-backed SQL model, chart planner and summary writer."""

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium", client: Any = None):
        import anthropic

        self.anthropic = anthropic
        self.client = client or anthropic.AsyncAnthropic(max_retries=3)
        self.model = model
        self.effort = effort
        self.name = f"claude:{model}"
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def _track(self, resp: Any) -> None:
        u = resp.usage
        self.usage["calls"] += 1
        self.usage["input_tokens"] += (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.usage["output_tokens"] += u.output_tokens or 0

    def _common(self) -> dict[str, Any]:
        # Server-side refusal fallback: if a safety classifier declines, the API
        # re-runs the request on a fallback model inside the same call.
        return {
            "model": self.model,
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
            "output_config": {"effort": self.effort},
        }

    async def _parse(self, system: str, user: str, schema: type[BaseModel], max_tokens: int = 16000):
        resp = await self.client.beta.messages.parse(
            **self._common(), max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], output_format=schema,
        )
        self._track(resp)
        if resp.stop_reason == "refusal":
            raise RuntimeError("The model declined this request.")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise RuntimeError(f"Model returned no structured output (stop_reason={resp.stop_reason}).")
        return resp.parsed_output

    async def generate_sql(self, question: str, schema: str, attempts: Sequence[dict]) -> SQLProposal:
        parts = [f"<schema>\n{schema}\n</schema>", f"<question>{question}</question>"]
        if attempts:
            parts.append(retry_feedback(attempts, schema))
        return await self._parse(SQL_SYSTEM, "\n\n".join(parts), SQLProposal)

    async def plan_chart(self, question, columns, rows) -> ChartSpec:
        user = f"Question: {question}\n\nResult (first row is the header):\n{_preview(columns, rows, 25)}"
        return await self._parse(CHART_SYSTEM, user, ChartSpec, max_tokens=4000)

    async def summarize(self, question, sql, columns, rows, chart, truncated) -> str:
        user = (f"Question: {question}\n\nSQL:\n```sql\n{sql}\n```\n\n"
                f"Result ({len(rows)} rows{', truncated' if truncated else ''}; first row is the header):\n"
                f"{_preview(columns, rows)}\n\n"
                f"Chart shown: {chart.model_dump_json() if chart else 'none'}")
        resp = await self.client.beta.messages.create(
            **self._common(), max_tokens=16000, system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        self._track(resp)
        if resp.stop_reason == "refusal":
            return "_The model declined to summarise this result._"
        return "".join(b.text for b in resp.content if b.type == "text").strip()


# --------------------------------------------------------------------------- Mock
_REVENUE = "ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)), 2)"

_MOCK_SQL: list[tuple[re.Pattern, list[str], str]] = [
    # (pattern, [sql attempts in order], rationale)
    (re.compile(r"monthly|per month|by month|trend|over time", re.I), [
        # First attempt is intentionally wrong to demonstrate the retry loop.
        """SELECT strftime('%Y-%m', o.order_date) AS month, ROUND(SUM(o.total_amount), 2) AS revenue
FROM orders o WHERE o.status = 'completed' GROUP BY month ORDER BY month""",
        f"""SELECT strftime('%Y-%m', o.order_date) AS month, {_REVENUE} AS revenue
FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status = 'completed' GROUP BY month ORDER BY month""",
    ], "Sum completed-order line revenue per calendar month."),
    (re.compile(r"region", re.I), [
        f"""SELECT c.region, {_REVENUE} AS revenue, COUNT(DISTINCT o.order_id) AS orders
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status = 'completed' AND o.order_date >= '2026-01-01'
GROUP BY c.region ORDER BY revenue DESC""",
    ], "Join orders to customers for region; 2026 completed orders only."),
    (re.compile(r"return", re.I), [
        """SELECT p.category,
       ROUND(100.0 * SUM(CASE WHEN o.status = 'returned' THEN 1 ELSE 0 END) / COUNT(*), 2) AS return_rate_pct
FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
JOIN products p ON p.product_id = oi.product_id
GROUP BY p.category ORDER BY return_rate_pct DESC""",
    ], "Share of order lines whose order was returned, per category."),
    (re.compile(r"top|best[- ]selling|products?", re.I), [
        f"""SELECT p.name AS product, {_REVENUE} AS revenue
FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.status = 'completed' GROUP BY p.name ORDER BY revenue DESC LIMIT 10""",
    ], "Rank products by completed revenue, top 10."),
    (re.compile(r"channel", re.I), [
        f"""SELECT strftime('%Y-Q', o.order_date) || ((CAST(strftime('%m', o.order_date) AS INTEGER) + 2) / 3) AS quarter,
       o.channel, {_REVENUE} AS revenue
FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status = 'completed' GROUP BY quarter, o.channel ORDER BY quarter, o.channel""",
    ], "Quarterly revenue in long format, one series per sales channel."),
]


def _additive(measure: str) -> bool:
    """Totals and shares only make sense for additive measures (revenue, counts), not rates."""
    return not re.search(r"rate|pct|percent|avg|average|mean|ratio|share|margin|growth|price|discount|score|age", measure, re.I)


def _label(col: str) -> str:
    """Readable measure name; unaliased expressions (common in Spider-style SQL) become "value"."""
    return col.replace("_", " ") if re.fullmatch(r"\w+", col) else "value"


def _additive_col(col: str) -> bool:
    if re.fullmatch(r"\w+", col):
        return _additive(col)
    c = col.upper().strip()
    return c.startswith(("SUM(", "COUNT(")) and "/" not in c and "AVG(" not in c


def fact_summary(question: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
                 chart: ChartSpec | None = None) -> str:
    """Deterministic summary computed from the result: every number is exact."""
    if not rows or all(v is None for r in rows for v in r):
        return ("The query returned no data, so the database probably does not contain the "
                "information this question needs. Check the SQL in \"What the agent did\".")
    spec = chart or heuristic_chart(question, columns, rows)
    cols = list(columns)
    if spec.chart_type == "none" or not spec.x or not spec.y or len(rows) == 1:
        def fmt(v: Any) -> str:
            return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}" if isinstance(v, int) else str(v)
        parts = [f"{_label(c)}: **{fmt(v)}**" for c, v in zip(cols, rows[0])]
        extra = f"\n\n({len(rows)} rows; showing the first)" if len(rows) > 1 else ""
        return "**Answer:** " + " · ".join(parts) + extra
    xi, yi = cols.index(spec.x), cols.index(spec.y)
    if spec.series:
        si = cols.index(spec.series)
        totals: dict[str, float] = {}
        for r in rows:
            totals[str(r[si])] = totals.get(str(r[si]), 0) + float(r[yi])
        if not _additive_col(spec.y):   # rates/averages: compare the mean per series, never sum
            counts = {k: sum(1 for r in rows if str(r[si]) == k) for k in totals}
            avgs = sorted(((k, totals[k] / counts[k]) for k in totals), key=lambda kv: -kv[1])
            return (f"**{avgs[0][0]}** has the highest average {_label(spec.y)} "
                    f"({avgs[0][1]:,.2f}).\n\n" + "\n".join(f"- **{k}**: {v:,.2f} on average" for k, v in avgs))
        grand = sum(totals.values()) or 1
        ranked = sorted(totals.items(), key=lambda kv: -kv[1])
        lines = [f"- **{k}**: {v:,.2f} ({100 * v / grand:.1f}% of total)" for k, v in ranked]
        return (f"**{ranked[0][0]}** leads on {_label(spec.y)} with {ranked[0][1]:,.2f} "
                f"across the period.\n\n" + "\n".join(lines))
    vals = [(str(r[xi]), float(r[yi])) for r in rows]
    total = sum(v for _, v in vals)
    measure = _label(spec.y)
    if spec.chart_type == "line":
        first, last = vals[0], vals[-1]
        peak = max(vals, key=lambda kv: kv[1])
        change = (last[1] - first[1]) / first[1] * 100 if first[1] else 0
        return (f"{measure.capitalize()} moved from {first[1]:,.2f} in {first[0]} to {last[1]:,.2f} in "
                f"{last[0]} ({change:+.1f}%).\n\n- Peak: **{peak[0]}** at {peak[1]:,.2f}\n"
                f"- Total over {len(vals)} periods: {total:,.2f}\n"
                f"- Average per period: {total / len(vals):,.2f}")
    top, bottom = max(vals, key=lambda kv: kv[1]), min(vals, key=lambda kv: kv[1])
    share = f" ({100 * top[1] / total:.1f}% of the total)" if total and _additive_col(spec.y) else ""
    return (f"**{top[0]}** is highest on {measure} at {top[1]:,.2f}{share}.\n\n"
            f"- Lowest: {bottom[0]} at {bottom[1]:,.2f}\n"
            f"- Gap between highest and lowest: {top[1] - bottom[1]:,.2f}\n"
            f"- {len(vals)} groups compared")


class MockAnalystLLM:
    """Deterministic offline stand-in for Claude (demo / tests)."""

    name = "mock (offline)"
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def generate_sql(self, question: str, schema: str, attempts: Sequence[dict]) -> SQLProposal:
        for pattern, sqls, why in _MOCK_SQL:
            if pattern.search(question):
                return SQLProposal(sql=sqls[min(len(attempts), len(sqls) - 1)], rationale=why)
        return SQLProposal(sql="SELECT status, COUNT(*) AS orders FROM orders GROUP BY status ORDER BY orders DESC",
                           rationale="Fallback: order counts by status (mock model has no rule for this question).")

    async def plan_chart(self, question, columns, rows) -> ChartSpec:
        return heuristic_chart(question, columns, rows)

    async def summarize(self, question, sql, columns, rows, chart, truncated) -> str:
        return fact_summary(question, columns, rows, chart)


# --------------------------------------------------------------------------- Local open-source model
# Constrained decoding must force the model to state answerable explicitly.
_SQL_SCHEMA = {**SQLProposal.model_json_schema(), "required": ["answerable", "sql", "rationale"]}


class LocalAnalystLLM:
    """Open-source model served locally behind an OpenAI-compatible endpoint.

    Tested with ``llama-cpp-python[server]`` running Qwen2.5-Coder-7B-Instruct (GGUF):

        python -m llama_cpp.server --model models/qwen2.5-coder-7b-instruct-q4_k_m.gguf --n_ctx 8192

    Structured outputs use the server's JSON-schema constrained decoding
    (``response_format={"type": "json_object", "schema": ...}``), so replies always
    parse into the same Pydantic models the Claude backend returns. Also works
    with Ollama / vLLM / any server that honours that ``response_format``.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float = 900):
        import httpx

        self.base_url = (base_url or os.environ.get("ANALYST_LOCAL_URL", "http://127.0.0.1:8000/v1")).rstrip("/")
        self.model = model or os.environ.get("ANALYST_LOCAL_MODEL", "qwen2.5-coder-7b-instruct")
        self.name = f"local:{self.model}"
        self.http = httpx.AsyncClient(timeout=timeout)
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def _chat(self, system: str, user: str, schema: type[BaseModel] | dict | None, max_tokens: int,
                    temperature: float = 0) -> str:
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "temperature": temperature, "seed": 7,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if schema is not None:
            js = schema if isinstance(schema, dict) else schema.model_json_schema()
            body["response_format"] = {"type": "json_object", "schema": js}
        r = await self.http.post(f"{self.base_url}/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        u = data.get("usage") or {}
        self.usage["calls"] += 1
        self.usage["input_tokens"] += u.get("prompt_tokens", 0)
        self.usage["output_tokens"] += u.get("completion_tokens", 0)
        return data["choices"][0]["message"]["content"] or ""

    async def generate_sql(self, question: str, schema: str, attempts: Sequence[dict]) -> SQLProposal:
        # Schema goes in the system prompt so the prefix is identical across SQL calls (KV-cache reuse).
        system = f"{SQL_SYSTEM}\n\n<schema>\n{schema}\n</schema>"
        user = f"<question>{question}</question>"
        if attempts:
            user += "\n\n" + retry_feedback(attempts, schema)
        user += "\n\nReply with JSON: {\"answerable\": bool, \"sql\": str, \"rationale\": str}."
        proposal = SQLProposal.model_validate_json(await self._chat(system, user, _SQL_SCHEMA, 700))
        # Greedy decoding tends to reproduce a failed query verbatim; resample once if it did.
        tried = {_norm_sql(a["sql"]) for a in attempts}
        if proposal.sql and _norm_sql(proposal.sql) in tried:
            proposal = SQLProposal.model_validate_json(
                await self._chat(system, user, _SQL_SCHEMA, 700, temperature=0.7))
        return proposal

    async def plan_chart(self, question, columns, rows) -> ChartSpec:
        user = (f"Question: {question}\n\nResult (first row is the header):\n{_preview(columns, rows, 25)}\n\n"
                "Reply with JSON: {\"chart_type\": \"bar|hbar|line|none\", \"x\": col, \"y\": col, "
                "\"series\": col or null, \"title\": str, \"y_label\": str}.")
        return ChartSpec.model_validate_json(await self._chat(CHART_SYSTEM, user, ChartSpec, 300))

    async def summarize(self, question, sql, columns, rows, chart, truncated) -> str:
        user = (f"Question: {question}\n\nSQL:\n{sql}\n\n"
                f"Result ({len(rows)} rows{', truncated' if truncated else ''}; first row is the header):\n"
                f"{_preview(columns, rows, 40)}")
        return (await self._chat(SUMMARY_SYSTEM, user, None, 350)).strip()


def default_llm(force_mock: bool = False) -> AnalystLLM:
    """Pick the backend: ANALYST_LLM=claude|local|specialist|mock, else Claude if credentials exist, else mock."""
    choice = os.environ.get("ANALYST_LLM", "").lower()
    if force_mock or choice == "mock":
        return MockAnalystLLM()
    if choice == "local":
        return LocalAnalystLLM()
    if choice == "specialist":
        from analyst.specialist import specialist_from_env
        return specialist_from_env()
    if choice == "claude":
        return ClaudeAnalystLLM()
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return ClaudeAnalystLLM()
    return MockAnalystLLM()
