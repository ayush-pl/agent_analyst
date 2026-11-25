"""Web demo for the AI Data Analyst (Gradio). Runs locally or as a Hugging Face Space.

    python app.py                       # http://127.0.0.1:7860
    ANALYST_LLM=mock python app.py      # instant UI check, no model download
    python app.py --build-cache         # re-run the example questions, save to demo/cache/

Two tabs:

* **Demo shop**: a seeded e-commerce database with instant, pre-computed example answers.
* **Your CSV**: upload a CSV; it is cleaned into a SQLite table (analyst/datasets.py), you get
  an overview computed from the data plus suggested questions, then ask anything about it.

Both tabs run the same LangGraph agent (SQL review, self-correction, chart, checked summary).
The model backend comes from ``ANALYST_LLM`` (default here: ``specialist``, the fine-tuned
text-to-SQL model + a small generalist, both in-process via llama.cpp).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("ANALYST_LLM", "specialist")
DB_PATH = Path(os.environ.get("ANALYST_DB", ROOT / "demo" / "shop.db"))
CACHE_DIR = ROOT / "demo" / "cache"
SAMPLE_CSV = ROOT / "demo" / "sample_coffee_sales.csv"
SAMPLE_NOTES = "revenue = qty * unit_price * (1 - discount / 100). discount is a percentage."
OUT_DIR = Path(tempfile.gettempdir()) / "analyst-demo"
UPLOAD_DIR = OUT_DIR / "uploads"
UPLOAD_TTL_S = 6 * 3600
MAX_AGENTS = 3          # live datasets kept open (each holds an MCP server subprocess)
REPO_URL = os.environ.get("ANALYST_REPO_URL", "")

# Taken from the evaluation set (eval/dataset.py) and verified correct with the default model,
# so the instant answers are ones the agent really gets right (with SQLForge-3B). Rebuild with --build-cache.
EXAMPLES = [
    "What is total revenue by region?",
    "What is the monthly revenue trend?",
    "What is revenue by sales channel?",
    "What is the return rate (percentage of orders returned) for each sales channel?",
    "Which product has the highest return rate (share of its order lines in returned orders)? Show the top 3 with their rates as percentages.",
    "How much did we spend on marketing last quarter?",
    "Delete all cancelled orders.",
]

_llm = None
_agents: "OrderedDict[str, object]" = OrderedDict()
_lock: asyncio.Lock | None = None


def ensure_db() -> None:
    if not DB_PATH.exists():
        from demo.seed_db import seed
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        seed(DB_PATH)


def schema_markdown(db: Path = DB_PATH) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    lines = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cols = ", ".join(r[1] for r in con.execute(f'PRAGMA table_info("{t}")'))
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        lines.append(f"- **{t}** ({n:,} rows): {cols}")
    con.close()
    return "\n".join(lines)


def prefetch_models() -> None:
    """Download the GGUF files in the background so the first visitor does not wait for ~4 GB."""
    try:
        from analyst.specialist import DEFAULT_GENERAL_MODEL, resolve_model

        for spec in (os.environ.get("ANALYST_GENERAL_MODEL", DEFAULT_GENERAL_MODEL),
                     os.environ.get("ANALYST_SQL_MODEL", ""), os.environ.get("ANALYST_SQL_LORA", "")):
            if spec:
                print(f"model ready: {resolve_model(spec)}", flush=True)
    except Exception as e:  # noqa: BLE001 - the first question will retry and show the error
        print(f"model prefetch failed: {e}", flush=True)


async def get_analyst(db: str):
    """One agent per dataset (each owns an MCP server subprocess); the models are shared.

    Created inside Gradio's event loop; the least recently used agent is closed when more
    than MAX_AGENTS datasets are open.
    """
    global _llm, _lock
    from analyst.agent import DataAnalyst
    from analyst.llm import default_llm

    if _lock is None:
        _lock = asyncio.Lock()
    if _llm is None:
        _llm = default_llm()
    if db in _agents:
        _agents.move_to_end(db)
        return _agents[db]
    agent = DataAnalyst(db_path=db, llm=_llm, require_approval=True, output_dir=str(OUT_DIR / uuid.uuid4().hex[:8]))
    await agent.__aenter__()
    _agents[db] = agent
    while len(_agents) > MAX_AGENTS:   # close the least recently used upload; keep the shop warm
        victim = next(k for k in _agents if k not in (str(DB_PATH), db))
        await _agents.pop(victim).__aexit__(None, None, None)
    return agent


# ---------------------------------------------------------------- rendering

def display_columns(cols: list[str]) -> list[str]:
    """Readable headers for unaliased SQL expressions (Spider-style models rarely alias columns)."""
    import re

    out, seen = [], set()
    for c in cols:
        if re.fullmatch(r"[\w ]+", c):
            name = c
        else:
            low = c.lower()
            fn = re.match(r"\s*(sum|avg|count|min|max|round|cast)\s*\(", low)
            name = ("month" if "%y-%m" in low and "strftime" in low else
                    "year" if "'%y'" in low and "strftime" in low else
                    {"sum": "total", "avg": "average", "count": "count", "min": "minimum",
                     "max": "maximum"}.get(fn.group(1) if fn else "", "value"))
            if re.search(r"\b100(\.0)?\s*\*|\*\s*100(\.0)?\b", low) and "/" in low and name in ("value", "total"):
                name = "percent"
        base, i = name, 2
        while name in seen:
            name, i = f"{base}_{i}", i + 1
        seen.add(name)
        out.append(name)
    return out


def render(state: dict, note: str = ""):
    """State -> (status, summary, chart, table, sql, trace)."""
    import gradio as gr
    import pandas as pd

    status = state.get("status", "")
    res = state.get("result") or {}
    cols = display_columns(res.get("columns") or [])
    table = pd.DataFrame(res.get("rows") or [], columns=cols or None) if res else None
    if status == "unanswerable":
        summary = ("**Declined, nothing was run.** Either the data doesn't contain what this question needs, "
                   "or it asks to change the database, and this agent is read-only.\n\n"
                   f"_Model's reason: {state.get('rationale') or 'not given'}_")
    else:
        summary = state.get("summary") or ""
    badge = {"done": "✅ Answered", "unanswerable": "🛑 Declined (nothing was run)",
             "rejected": "↩️ Rejected by reviewer", "failed": "⚠️ Failed"}.get(status, status)
    attempts = state.get("attempts") or []
    if attempts:
        badge += f" · self-corrected after {len(attempts)} failed quer{'y' if len(attempts) == 1 else 'ies'}"
    trace = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(state.get("events") or []))
    if attempts:
        trace += "\n\n**Failed attempts**\n" + "\n".join(f"- `{a['error']}`" for a in attempts)
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


class _Empty:
    """Hidden chart + hidden table + no SQL + no trace (lazy so gradio imports stay inside handlers)."""

    def __iter__(self):
        import gradio as gr
        return iter((gr.update(value=None, visible=False), gr.update(value=None, visible=False), "", ""))


_EMPTY = _Empty()


# ---------------------------------------------------------------- agent handlers

async def ask(question: str, require_approval: bool, db: str | None):
    import gradio as gr

    question = (question or "").strip()
    if not db:
        raise gr.Error("Load a CSV file first.")
    if not question:
        raise gr.Error("Type a question first.")
    analyst = await get_analyst(db)
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


async def decide(thread_id: str | None, sql: str, action: str, proposed: str | None, db: str | None):
    import gradio as gr
    from analyst.agent import AnalystRun

    if not thread_id or not db:
        raise gr.Error("Nothing is waiting for approval. Ask a question first.")
    analyst = await get_analyst(db)
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


# ---------------------------------------------------------------- CSV handlers

N_SUGGEST = 4


def _cleanup_uploads() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    open_dbs = set(_agents)
    for f in UPLOAD_DIR.iterdir():
        db_twin = str(f.with_suffix(".db"))
        if now - f.stat().st_mtime > UPLOAD_TTL_S and db_twin not in open_dbs:
            f.unlink(missing_ok=True)


def load_csv(file_path: str | None, notes: str, display_name: str | None = None):
    """Upload -> cleaned SQLite table + overview + suggested questions."""
    import gradio as gr

    from analyst.datasets import import_csv, profile, suggest_questions

    if not file_path:
        raise gr.Error("Choose a CSV file first.")
    _cleanup_uploads()
    db = UPLOAD_DIR / f"{uuid.uuid4().hex}.db"
    try:
        info = import_csv(file_path, db, user_notes=notes or "", display_name=display_name or Path(file_path).name)
    except Exception as e:  # noqa: BLE001 - show parsing problems to the user
        raise gr.Error(f"Could not read that file as CSV: {e}") from e
    overview = profile(info.db_path, info.table)
    if info.renamed:
        overview += "\n\n_Column names were cleaned for SQL: " + ", ".join(
            f"`{o}` → `{n}`" for o, n in list(info.renamed.items())[:10]) + "._"
    qs = suggest_questions(info.db_path, info.table, N_SUGGEST, notes or "")
    btns = [gr.update(value=q, visible=True) for q in qs] + [gr.update(visible=False)] * (N_SUGGEST - len(qs))
    status = (f"Loaded **{info.table}**: {info.rows:,} rows, {len(info.columns)} columns. "
              "Ask a question below or pick a suggestion.")
    return (info.db_path, status, gr.update(value=overview, visible=True), *btns)


def load_sample():
    return (str(SAMPLE_CSV), SAMPLE_NOTES, *load_csv(str(SAMPLE_CSV), SAMPLE_NOTES, SAMPLE_CSV.name))


# ---------------------------------------------------------------- UI

CSS = """
.gradio-container {max-width: 1180px !important; margin: auto}
.status-line {font-weight: 600}
footer {display: none !important}
"""


def results_column(gr):
    """Status, SQL approval box and results; returns the components in handler output order."""
    status = gr.Markdown(elem_classes="status-line")
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
    return dict(status=status, approve_box=approve_box, sql_edit=sql_edit, rationale=rationale,
                run_btn=run_btn, rej_btn=rej_btn, summary=summary, chart=chart, table=table,
                final_sql=final_sql, trace=trace)


def wire(gr, q, approval, ask_btn, r, db_state):
    """Connect ask / approve / reject for one tab."""
    thread, proposed = gr.State(None), gr.State(None)
    outs = [thread, r["sql_edit"], r["approve_box"], r["status"], r["rationale"], r["summary"],
            r["chart"], r["table"], r["final_sql"], r["trace"], proposed]

    async def _ask(question, req, db):
        res = await ask(question, req, db)
        return (*res, res[1] or None)

    async def _approve(t, sql, prop, db):
        res = await decide(t, sql, "approve", prop, db)
        return (*res, res[1] or None)

    async def _reject(t, sql, prop, db):
        res = await decide(t, sql, "reject", prop, db)
        return (*res, None)

    ask_btn.click(_ask, [q, approval, db_state], outs)
    q.submit(_ask, [q, approval, db_state], outs)
    r["run_btn"].click(_approve, [thread, r["sql_edit"], proposed, db_state], outs)
    r["rej_btn"].click(_reject, [thread, r["sql_edit"], proposed, db_state], outs)
    return _ask, outs


def build_ui():
    import gradio as gr

    sql_model = os.environ.get("ANALYST_SQL_MODEL", "")
    if "sqlforge" in sql_model.lower():
        model_line = ("SQL model: [SQLForge-3B](https://huggingface.co/ay2r/sqlforge-3b) "
                      "(Qwen2.5-Coder-3B fine-tuned with QLoRA on Spider + BIRD, 4-bit)")
    elif sql_model:
        model_line = f"SQL model: `{Path(sql_model).name}`"
    else:
        model_line = "SQL model: Qwen2.5-Coder-3B (set `ANALYST_SQL_MODEL` for the fine-tuned weights)"
    with gr.Blocks(title="AI Data Analyst") as ui:
        gr.Markdown(
            "# AI Data Analyst\n"
            "Ask a business question in plain English. A **LangGraph** agent writes SQL with a "
            "**fine-tuned text-to-SQL model**, waits for **your approval**, runs it through **MCP tools** "
            "on a read-only database, corrects its own failed queries, then charts and summarizes the result.  \n"
            f"{model_line} · runs on a free 2-CPU server, so a live question takes 1–2 minutes "
            "(the examples are instant)." + (f" · [Source code]({REPO_URL})" if REPO_URL else ""))

        with gr.Tabs():
            # ------------------------------------------------ demo shop
            with gr.Tab("Demo shop"):
                shop_db = gr.State(str(DB_PATH))
                with gr.Row():
                    with gr.Column(scale=2):
                        q = gr.Textbox(label="Question", lines=2,
                                       placeholder="e.g. What is revenue by product category?")
                        with gr.Row():
                            approval = gr.Checkbox(value=True, label="Review SQL before it runs")
                            ask_btn = gr.Button("Ask", variant="primary")
                        gr.Markdown("**Try an example** (instant, pre-computed):")
                        ex_btns = [gr.Button(e, size="sm") for e in EXAMPLES]
                        with gr.Accordion("Database: a demo e-commerce shop", open=False):
                            gr.Markdown(schema_markdown())
                    with gr.Column(scale=3):
                        r = results_column(gr)
                wire(gr, q, approval, ask_btn, r, shop_db)
                for b, e in zip(ex_btns, EXAMPLES):
                    b.click(lambda e=e: show_example(e), None,
                            [q, r["approve_box"], r["status"], r["summary"], r["chart"], r["table"],
                             r["final_sql"], r["trace"]])

            # ------------------------------------------------ your CSV
            with gr.Tab("Analyze your CSV"):
                csv_db = gr.State(None)
                with gr.Row():
                    with gr.Column(scale=2):
                        upload = gr.File(label="CSV file (max 25 MB)", file_types=[".csv", ".tsv", ".txt"],
                                         type="filepath")
                        notes = gr.Textbox(label="Definitions the agent should know (optional)", lines=2,
                                           placeholder="e.g. revenue = qty * unit_price; a churned customer has status 'closed'")
                        with gr.Row():
                            load_btn = gr.Button("Load file", variant="primary")
                            sample_btn = gr.Button("Try a sample file")
                        gr.Markdown("_Files are converted to a private, read-only SQLite table and deleted "
                                    "after a few hours. Do not upload sensitive data to a public demo._")
                        q2 = gr.Textbox(label="Question about your data", lines=2)
                        with gr.Row():
                            approval2 = gr.Checkbox(value=True, label="Review SQL before it runs")
                            ask_btn2 = gr.Button("Ask", variant="primary")
                        gr.Markdown("**Suggested questions**")
                        sug = [gr.Button(visible=False, size="sm") for _ in range(N_SUGGEST)]
                    with gr.Column(scale=3):
                        load_status = gr.Markdown(elem_classes="status-line")
                        with gr.Accordion("Data overview", open=True):
                            overview = gr.Markdown(visible=False)
                        r2 = results_column(gr)
                ask2, outs2 = wire(gr, q2, approval2, ask_btn2, r2, csv_db)
                load_outs = [csv_db, load_status, overview, *sug]
                load_btn.click(load_csv, [upload, notes], load_outs)
                sample_btn.click(load_sample, None, [upload, notes, *load_outs])
                for b in sug:
                    b.click(lambda text: text, b, q2).then(ask2, [q2, approval2, csv_db], outs2)
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
        if os.environ.get("ANALYST_LLM") == "specialist":
            threading.Thread(target=prefetch_models, daemon=True).start()
        build_ui().queue(default_concurrency_limit=1, max_size=20).launch(
            server_name=os.environ.get("HOST", "0.0.0.0"), server_port=args.port, css=CSS,
            theme=gr.themes.Soft(primary_hue="blue"), max_file_size="25mb",
            allowed_paths=[str(CACHE_DIR), str(OUT_DIR)])
