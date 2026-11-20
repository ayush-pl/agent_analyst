"""The LangGraph workflow.

    START → load_schema → generate_sql ─┬─(unsafe SQL, retries left)──┐
                              ▲          ▼                             │
                              │    human_approval ──reject──► END      │
                              │          │ approve / edit              │
                              │          ▼                             │
                              └──── execute_sql ◄──────────────────────┘
                     (SQL error,         │ ok
                      retries left)      ▼
                                     make_chart → summarize → END
                         (retries exhausted → fail → END)

* ``generate_sql`` calls the **SQL model as a tool** (``sql_model_tool``) with the
  question, the schema and every previous failed attempt + its error.
* ``human_approval`` pauses the graph with ``interrupt()``; resume it with
  ``Command(resume={"action": "approve" | "edit" | "reject", ...})``.
* ``execute_sql`` / ``load_schema`` / ``make_chart`` use **MCP tools**.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from analyst.llm import AnalystLLM, ChartSpec, heuristic_chart
from analyst.mcp_client import MCPToolbox
from analyst.sql_safety import UnsafeSQLError, check_read_only


class AnalystState(TypedDict, total=False):
    question: str
    require_approval: bool
    max_retries: int
    output_dir: str
    schema: str
    sql: str
    rationale: str
    attempts: list[dict]          # [{"sql":..., "error":..., "stage": "guard"|"database"}]
    approval: dict
    result: dict                  # {"columns": [...], "rows": [...], "truncated": bool}
    chart_spec: dict
    chart_path: Optional[str]
    summary: str
    status: Literal["running", "done", "rejected", "failed"]
    events: list[str]             # human-readable trace


def _log(state: AnalystState, msg: str) -> list[str]:
    return [*state.get("events", []), msg]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "chart"


def _format_schema(payload: dict) -> str:
    blocks = []
    for t in payload.get("tables", []):
        samples = "\n".join(f"  {row}" for row in t.get("sample_rows", []))
        blocks.append(f"{t['ddl']}\n-- sample rows ({', '.join(t['columns'])}):\n{samples}")
    return f"-- dialect: {payload.get('dialect', 'sqlite')}\n\n" + "\n\n".join(blocks)


def build_graph(llm: AnalystLLM, tools: MCPToolbox, checkpointer: Any = None):
    """Compile the analyst graph. A checkpointer is required for human approval."""

    async def sql_model_tool(question: str, schema: str, attempts: list[dict]) -> dict:
        """Tool wrapper around the SQL model: question + schema + failures -> SQL."""
        proposal = await llm.generate_sql(question, schema, attempts)
        return proposal.model_dump()

    # ------------------------------------------------------------------ nodes
    async def load_schema(state: AnalystState) -> dict:
        payload = await tools.call("describe_schema")
        if not payload.get("ok"):
            return {"status": "failed", "summary": f"Could not load schema: {payload.get('error')}",
                    "events": _log(state, f"schema load failed: {payload.get('error')}")}
        return {"schema": _format_schema(payload), "attempts": [], "status": "running",
                "events": _log(state, f"MCP describe_schema → {len(payload['tables'])} tables")}

    async def generate_sql(state: AnalystState) -> Command:
        attempts = state.get("attempts", [])
        out = await sql_model_tool(state["question"], state["schema"], attempts)
        events = _log(state, f"SQL model proposed query (attempt {len(attempts) + 1})")
        try:
            sql = check_read_only(out["sql"])
        except UnsafeSQLError as e:
            attempts = [*attempts, {"sql": out["sql"], "error": str(e), "stage": "guard"}]
            events = [*events, f"read-only guard rejected it: {e}"]
            nxt = "generate_sql" if len(attempts) <= state.get("max_retries", 3) else "fail"
            return Command(goto=nxt, update={"attempts": attempts, "events": events})
        update = {"sql": sql, "rationale": out["rationale"], "events": events}
        return Command(goto="human_approval" if state.get("require_approval", True) else "execute_sql",
                       update=update)

    async def human_approval(state: AnalystState) -> Command:
        # Everything before interrupt() re-runs on resume, so keep it side-effect free.
        decision = interrupt({
            "type": "sql_approval",
            "question": state["question"],
            "sql": state["sql"],
            "rationale": state.get("rationale", ""),
            "attempt": len(state.get("attempts", [])) + 1,
            "previous_errors": [a["error"] for a in state.get("attempts", [])],
        })
        if isinstance(decision, bool):
            decision = {"action": "approve" if decision else "reject"}
        action = (decision or {}).get("action", "reject")
        if action == "edit":
            try:
                sql = check_read_only(decision.get("sql", ""))
            except UnsafeSQLError as e:
                return Command(goto="human_approval", update={
                    "events": _log(state, f"edited SQL rejected by guard: {e}")})
            return Command(goto="execute_sql", update={
                "sql": sql, "approval": decision, "events": _log(state, "human edited & approved the SQL")})
        if action == "approve":
            return Command(goto="execute_sql", update={
                "approval": decision, "events": _log(state, "human approved the SQL")})
        reason = decision.get("reason") or "no reason given"
        return Command(goto=END, update={
            "approval": decision, "status": "rejected",
            "summary": f"Query was not run: the reviewer rejected it ({reason}).",
            "events": _log(state, f"human rejected the SQL: {reason}")})

    async def execute_sql(state: AnalystState) -> Command:
        res = await tools.call("run_query", {"sql": state["sql"]})
        if res.get("ok"):
            return Command(goto="make_chart", update={
                "result": {"columns": res["columns"], "rows": res["rows"], "truncated": res.get("truncated", False)},
                "events": _log(state, f"MCP run_query → {res['row_count']} rows")})
        attempts = [*state.get("attempts", []), {"sql": state["sql"], "error": res.get("error"), "stage": "database"}]
        retries_left = len(attempts) <= state.get("max_retries", 3)
        return Command(goto="generate_sql" if retries_left else "fail", update={
            "attempts": attempts,
            "events": _log(state, f"SQL error: {res.get('error')}"
                                  + (f" → retry {len(attempts)}/{state.get('max_retries', 3)}" if retries_left else ""))})

    async def make_chart(state: AnalystState) -> dict:
        cols, rows = state["result"]["columns"], state["result"]["rows"]
        events = list(state.get("events", []))
        try:
            spec = await llm.plan_chart(state["question"], cols, rows)
            needed = [c for c in (spec.x, spec.y, spec.series) if c]
            if spec.chart_type != "none" and (not spec.x or not spec.y or any(c not in cols for c in needed)):
                raise ValueError(f"chart spec references unknown columns {needed}")
        except Exception as e:  # noqa: BLE001 - a chart problem should never sink the answer
            events.append(f"chart planner failed ({e}); using heuristic")
            spec = heuristic_chart(state["question"], cols, rows)
        path = None
        if spec.chart_type != "none":
            out = str(Path(state.get("output_dir", "outputs")).resolve() / f"{_slug(state['question'])}.png")
            res = await tools.call("render_chart", {
                "spec": spec.model_dump(), "columns": cols, "rows": rows, "out_path": out})
            if res.get("ok"):
                path = res.get("path")
                events.append(f"MCP render_chart → {spec.chart_type} chart")
            else:
                events.append(f"chart render failed: {res.get('error')}")
        else:
            events.append("no chart for this result shape")
        return {"chart_spec": spec.model_dump(), "chart_path": path, "events": events}

    async def summarize(state: AnalystState) -> dict:
        r = state["result"]
        spec = ChartSpec(**state["chart_spec"]) if state.get("chart_spec") else None
        text = await llm.summarize(state["question"], state["sql"], r["columns"], r["rows"], spec, r["truncated"])
        return {"summary": text, "status": "done", "events": _log(state, "summary written")}

    async def fail(state: AnalystState) -> dict:
        last = state.get("attempts", [{}])[-1]
        return {"status": "failed",
                "summary": (f"Could not produce a working query after {len(state.get('attempts', []))} attempts. "
                            f"Last error: {last.get('error')}"),
                "events": _log(state, "giving up: retries exhausted")}

    # ------------------------------------------------------------------ wiring
    g = StateGraph(AnalystState)
    g.add_node("load_schema", load_schema)
    g.add_node("generate_sql", generate_sql, destinations=("human_approval", "execute_sql", "generate_sql", "fail"))
    g.add_node("human_approval", human_approval, destinations=("execute_sql", "human_approval", END))
    g.add_node("execute_sql", execute_sql, destinations=("make_chart", "generate_sql", "fail"))
    g.add_node("make_chart", make_chart)
    g.add_node("summarize", summarize)
    g.add_node("fail", fail)

    g.add_edge(START, "load_schema")
    g.add_conditional_edges("load_schema", lambda s: END if s.get("status") == "failed" else "generate_sql")
    g.add_edge("make_chart", "summarize")
    g.add_edge("summarize", END)
    g.add_edge("fail", END)
    return g.compile(checkpointer=checkpointer)
