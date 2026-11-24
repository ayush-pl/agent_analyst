"""Web demo for the AI Data Analyst (Gradio). Runs locally or as a Hugging Face Space.

    python app.py                       # http://127.0.0.1:7860
    ANALYST_LLM=mock python app.py      # instant UI check, no model download
    python app.py --build-cache         # re-run the example questions, save to demo/cache/

The model backend comes from ``ANALYST_LLM`` (default here: ``specialist``, the
fine-tuned text-to-SQL model + a small generalist, both in-process via llama.cpp;
see analyst/specialist.py). Example questions show pre-computed answers instantly
so visitors are not kept waiting on a free CPU; "Ask" always runs the agent live.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("ANALYST_LLM", "specialist")
DB_PATH = Path(os.environ.get("ANALYST_DB", ROOT / "demo" / "shop.db"))
CACHE_DIR = ROOT / "demo" / "cache"
OUT_DIR = Path(tempfile.gettempdir()) / "analyst-demo"
REPO_URL = os.environ.get("ANALYST_REPO_URL", "")

# Taken from the evaluation set (eval/dataset.py) and verified correct with the default model,
# so the instant answers are ones the agent really gets right. Rebuild with --build-cache.
EXAMPLES = [
    "What is total revenue by region?",
    "What is the monthly revenue trend?",
    "What is the monthly revenue for each sales channel in 2026?",
    "What is the gross profit (revenue minus unit cost times quantity) by product category for completed orders?",
    "What are the top 5 products by revenue?",
    "What is our Net Promoter Score?",
    "Delete all cancelled orders.",
]

_analyst = None
_lock: asyncio.Lock | None = None


def ensure_db() -> None:
    if not DB_PATH.exists():
        from demo.seed_db import seed
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        seed(DB_PATH)


def schema_markdown() -> str:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    lines = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cols = ", ".join(r[1] for r in con.execute(f"PRAGMA table_info({t})"))
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        lines.append(f"- **{t}** ({n:,} rows): {cols}")
    con.close()
    return "\n".join(lines)


async def get_analyst():
    """One shared agent (MCP server subprocess + models), created inside Gradio's event loop."""
    global _analyst, _lock
    if _analyst is None:
        from analyst.agent import DataAnalyst
        _lock = asyncio.Lock()
        _analyst = DataAnalyst(db_path=str(DB_PATH), require_approval=True, output_dir=str(OUT_DIR))
        await _analyst.__aenter__()
    return _analyst


# ---------------------------------------------------------------- rendering

def render(state: dict, note: str = ""):
    """State -> (status, summary, chart, table, sql, trace)."""
    import gradio as gr
    import pandas as pd

    status = state.get("status", "")
    res = state.get("result") or {}
    table = pd.DataFrame(res.get("rows") or [], columns=res.get("columns") or None) if res else None
    if status == "unanswerable":
        summary = f"**Declined.** {state.get('rationale') or 'The data cannot answer this question.'}"
    else:
        summary = state.get("summary") or ""
    badge = {"done": "✅ Answered", "unanswerable": "🛑 Declined (nothing was run)",
             "rejected": "↩️ Rejected by reviewer", "failed": "⚠️ Failed"}.get(status, status)
    attempts = state.get("attempts") or []
    if attempts:
        badge += f" · self-corrected after {len(attempts)} failed quer{'y' if len(attempts) == 1 else 'ies'}"
    trace = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(state.get("events") or []))
    if attempts:
        trace += "\n\n**Failed attempts**\n" + "\n".join(
            f"- `{a['error']}`" for a in attempts)
    chart = state.get("chart_path")
    has_rows = table is not None and len(table) > 0
    return (f"{badge}{'  ·  ' + note if note else ''}", summary,
            gr.update(value=chart, visible=bool(chart)), gr.update(value=table, visible=has_rows),
            state.get("sql") or "", trace)


def cached(question: str):
    f = CACHE_DIR / "examples.json"
    if not f.exists():
        return None
    for item in json.loads(f.read_text()):
        if item["question"] == question:
            st = dict(item["state"])
            if st.get("chart_path"):
                st["chart_path"] = str(CACHE_DIR / st["chart_path"])
            return st
    return None


# ---------------------------------------------------------------- handlers

class _Empty:
    """Hidden chart + hidden table + no SQL + no trace (lazy so gradio imports stay inside handlers)."""

    def __iter__(self):
        import gradio as gr
        return iter((gr.update(value=None, visible=False), gr.update(value=None, visible=False), "", ""))


_EMPTY = _Empty()


async def ask(question: str, require_approval: bool):
    import gradio as gr

    question = (question or "").strip()
    if not question:
        raise gr.Error("Type a question first.")
    analyst = await get_analyst()
    async with _lock:
        analyst.require_approval = require_approval
        run = await analyst.ask(question[:500])
    if run.pending_approval:
        p = run.pending_approval
        status = "⏸️ Waiting for your approval. Review or edit the SQL, then run it."
        return (run.thread_id, p["sql"], gr.update(visible=True), status,
                f"_{p.get('rationale', '')}_", None, *_EMPTY)
    s = render(run.state)
    return (None, "", gr.update(visible=False), *s[:1], "", *s[1:])


async def decide(thread_id: str | None, sql: str, action: str, proposed: str | None = None):
    import gradio as gr
    from analyst.agent import AnalystRun

    if not thread_id:
        raise gr.Error("Nothing is waiting for approval. Ask a question first.")
    analyst = await get_analyst()
    run = AnalystRun(thread_id, {}, {"pending": True})
    async with _lock:
        if action == "reject":
            run = await analyst.resume(run, "reject", reason="Rejected in the web demo")
        elif proposed is not None and " ".join(sql.split()) != " ".join(proposed.split()):
            run = await analyst.resume(run, "edit", sql=sql)
        else:
            run = await analyst.resume(run, "approve")
    if run.pending_approval:   # a failed query was corrected and needs approval again
        p = run.pending_approval
        errs = "; ".join(p.get("previous_errors", []))
        return (run.thread_id, p["sql"], gr.update(visible=True),
                f"🔁 The query failed ({errs}). The SQL model corrected it. Approve the new version?",
                f"_{p.get('rationale', '')}_", None, *_EMPTY)
    s = render(run.state)
    return (None, "", gr.update(visible=False), s[0], "", *s[1:])


def show_example(question: str):
    import gradio as gr

    st = cached(question)
    if st is None:
        return question, gr.update(visible=False), "Press **Ask** to run this question live.", "", *_EMPTY
    s = render(st, note="pre-computed by this agent; press **Ask** to run it live")
    return question, gr.update(visible=False), *s


# ---------------------------------------------------------------- UI

CSS = """
.gradio-container {max-width: 1180px !important; margin: auto}
#status {font-weight: 600}
footer {display: none !important}
"""


def build_ui():
    import gradio as gr

    sql_model = os.environ.get("ANALYST_SQL_MODEL", "")
    model_line = (f"SQL model: `{Path(sql_model).name}`" if sql_model
                  else "SQL model: Qwen2.5-Coder-3B (set `ANALYST_SQL_MODEL` for the fine-tuned weights)")
    with gr.Blocks(title="AI Data Analyst") as ui:
        gr.Markdown(
            "# AI Data Analyst\n"
            "Ask a business question in plain English. A **LangGraph** agent writes SQL with a "
            "**fine-tuned text-to-SQL model**, waits for **your approval**, runs it through **MCP tools** "
            "on a read-only database, corrects its own failed queries, then charts and summarizes the result.  \n"
            f"{model_line} · runs on CPU, so a live question takes about a minute."
            + (f" · [Source code]({REPO_URL})" if REPO_URL else ""))
        thread = gr.State(None)
        proposed = gr.State(None)
        with gr.Row():
            with gr.Column(scale=2):
                q = gr.Textbox(label="Question", placeholder="e.g. What is revenue by product category?", lines=2)
                with gr.Row():
                    approval = gr.Checkbox(value=True, label="Review SQL before it runs")
                    ask_btn = gr.Button("Ask", variant="primary")
                gr.Markdown("**Try an example** (instant, pre-computed):")
                ex_btns = [gr.Button(e, size="sm") for e in EXAMPLES]
                with gr.Accordion("Database: a demo e-commerce shop", open=False):
                    gr.Markdown(schema_markdown())
            with gr.Column(scale=3):
                status = gr.Markdown(elem_id="status")
                with gr.Group(visible=False) as approve_box:
                    sql_edit = gr.Code(label="Proposed SQL (you can edit it)", language="sql", interactive=True)
                    rationale = gr.Markdown()
                    with gr.Row():
                        run_btn = gr.Button("Approve & run", variant="primary")
                        rej_btn = gr.Button("Reject", variant="stop")
                summary = gr.Markdown()
                chart = gr.Image(label="Chart", type="filepath", show_label=False, visible=False)
                table = gr.Dataframe(label="Result", wrap=True, max_height=320, visible=False)
                with gr.Accordion("What the agent did", open=False):
                    final_sql = gr.Code(label="SQL that ran", language="sql", interactive=False)
                    trace = gr.Markdown()

        outs = [thread, sql_edit, approve_box, status, rationale, summary, chart, table, final_sql, trace]

        async def _ask(question, req):
            r = await ask(question, req)
            return (*r, r[1] or None)

        async def _approve(t, sql, prop):
            r = await decide(t, sql, "approve", prop)
            return (*r, r[1] or None)

        async def _reject(t, sql, prop):
            r = await decide(t, sql, "reject", prop)
            return (*r, None)

        ask_btn.click(_ask, [q, approval], outs + [proposed])
        q.submit(_ask, [q, approval], outs + [proposed])
        run_btn.click(_approve, [thread, sql_edit, proposed], outs + [proposed])
        rej_btn.click(_reject, [thread, sql_edit, proposed], outs + [proposed])
        for b, e in zip(ex_btns, EXAMPLES):
            b.click(lambda e=e: show_example(e), None,
                    [q, approve_box, status, summary, chart, table, final_sql, trace])
    return ui


# ---------------------------------------------------------------- example cache

async def build_cache() -> None:
    from analyst.agent import DataAnalyst

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    async with DataAnalyst(db_path=str(DB_PATH), require_approval=False, output_dir=str(OUT_DIR)) as a:
        for qtext in EXAMPLES:
            run = await a.ask(qtext)
            st = {k: run.state.get(k) for k in
                  ("question", "status", "sql", "rationale", "attempts", "summary", "events", "chart_path")}
            res = run.state.get("result") or {}
            st["result"] = {"columns": res.get("columns"), "rows": (res.get("rows") or [])[:100]} if res else None
            if st.get("chart_path"):
                name = f"example-{len(items) + 1}.png"
                shutil.copy(st["chart_path"], CACHE_DIR / name)
                st["chart_path"] = name
            items.append({"question": qtext, "model": a.llm.name, "state": st})
            print(f"{st['status']:>12}  {qtext}", flush=True)
    (CACHE_DIR / "examples.json").write_text(json.dumps(items, indent=1, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7860)))
    args = ap.parse_args()
    ensure_db()
    if args.build_cache:
        asyncio.run(build_cache())
    else:
        import gradio as gr
        build_ui().queue(default_concurrency_limit=1, max_size=20).launch(
            server_name=os.environ.get("HOST", "0.0.0.0"), server_port=args.port, css=CSS,
            theme=gr.themes.Soft(primary_hue="blue"), allowed_paths=[str(CACHE_DIR), str(OUT_DIR)])
