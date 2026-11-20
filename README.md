# AI Data Analyst

A **LangGraph** agent that takes a business question, calls a **SQL model as a tool**,
runs the query through **MCP tools** after a **human-approval** step, **retries on SQL
errors**, renders a **chart**, and writes a **summary**. It uses **Claude**
(`claude-opus-5-5`) through the official Anthropic SDK.

```
          ┌──────────────┐
 START ──►│ load_schema  │  MCP: describe_schema
          └──────┬───────┘
                 ▼
          ┌──────────────┐   read-only guard fails
     ┌───►│ generate_sql │◄──────────────┐ (retry)
     │    └──────┬───────┘───────────────┘
     │           │  SQL model tool (question + schema + past errors → SQL)
     │           ▼
     │    ┌──────────────┐   reject
     │    │human_approval│─────────► END (status = rejected)
     │    └──────┬───────┘  interrupt(): approve / edit / reject
     │           ▼
     │    ┌──────────────┐
     └────│ execute_sql  │  MCP: run_query (read-only, row-capped)
 SQL error└──────┬───────┘  (retries exhausted → fail → END)
 + retries       ▼
 left     ┌──────────────┐
          │  make_chart  │  Claude picks chart spec → MCP: render_chart (PNG)
          └──────┬───────┘
                 ▼
          ┌──────────────┐
          │  summarize   │  Claude writes a stakeholder summary
          └──────┬───────┘
                 ▼
                END
```

## Features

| Requirement | How it works |
|---|---|
| **SQL model as a tool** | `generate_sql` calls `sql_model_tool`, a wrapper around the SQL model (Claude with structured output → `{sql, rationale}`). It gets the question, the live schema with sample rows, and every earlier failed attempt with its error. |
| **Retries on SQL errors** | When the database returns an error (e.g. `no such column`), the query and the error go back to the SQL model, which writes a corrected query. Retries are capped by `max_retries` (default 3). After that the run ends with `status="failed"` and gives the last error. |
| **Human approval** | `human_approval` uses LangGraph's `interrupt()` with a checkpointer. The run pauses with the SQL and the model's reasoning. You resume with `approve`, `edit` (your own SQL) or `reject` (with a reason). **Every** query is approved before it runs, retries included. Pass `require_approval=False` to skip approval in batch jobs. |
| **MCP tools** | `analyst/mcp_server.py` is a standalone MCP server (stdio) with `list_tables`, `describe_schema`, `run_query` and `render_chart`. The agent connects to it as an MCP client (`analyst/mcp_client.py`). To use another database, point the agent at any MCP server that offers the same tool names. |
| **Safety** | Read-only checks at three levels: a SQL guard that allows one `SELECT`/`WITH` statement only, a read-only SQLite connection (`mode=ro`), and `PRAGMA query_only`. Unsafe SQL never reaches the person approving it. It goes back to the model as a retry. |
| **Chart** | Claude picks `line` / `bar` / `hbar` / `none` and the columns. If that fails, a heuristic based on the result's shape picks instead. Charts are drawn with matplotlib in a consistent style: fixed colour order, one y-axis, light gridlines. |
| **Summary** | A short Markdown answer with the key numbers first, then supporting points. |

## Quick start

```bash
pip install -r requirements.txt
python demo/seed_db.py                # sample e-commerce DB → demo/shop.db
python demo/run_demo.py               # interactive: approve / edit / reject each query
```

* With `ANTHROPIC_API_KEY` set, the demo uses **Claude**.
* Without a key it uses a **deterministic offline mock model**, so you can see the whole
  pipeline (MCP, approval, retries, charts) anyway. The mock deliberately writes a broken
  query for the first question, so you can watch the retry loop recover.

```bash
python demo/run_demo.py --auto-approve                     # no prompts
python demo/run_demo.py -q "Who are our top 5 customers by lifetime revenue?"
python demo/run_demo.py --mock                             # force the offline model
```

Charts and a `report.md` are written to `outputs/`. A sample run is in
[`docs/demo/report.md`](docs/demo/report.md).

![monthly revenue](docs/demo/what-is-the-monthly-revenue-trend.png)

## Using it from code

```python
import asyncio
from analyst.agent import DataAnalyst

async def main():
    async with DataAnalyst(db_path="demo/shop.db") as analyst:
        run = await analyst.ask("Which regions generated the most revenue in 2026?")
        while not run.done:                          # paused for human approval
            print(run.pending_approval["sql"])
            run = await analyst.resume(run, "approve")   # or "edit", sql=... / "reject", reason=...
        print(run.state["summary"], run.state["chart_path"])

asyncio.run(main())
```

Because approval is a LangGraph `interrupt`, the pause can last as long as you need.
Swap `InMemorySaver` for a persistent checkpointer (SQLite/Postgres) to approve from
a web UI or Slack and resume later with the same `thread_id`.

## Using the MCP server elsewhere

The server works on its own, e.g. in Claude Desktop / Claude Code:

```json
{
  "mcpServers": {
    "analyst-sql": {
      "command": "python",
      "args": ["-m", "analyst.mcp_server"],
      "cwd": "/path/to/agent_analyst",
      "env": { "ANALYST_DB": "/path/to/agent_analyst/demo/shop.db" }
    }
  }
}
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Turns on Claude (otherwise the mock model is used) |
| `ANALYST_MODEL` | `claude-opus-5-5` | Model for the SQL model, chart planner and summary |
| `ANALYST_DB` | `demo/shop.db` | SQLite database the MCP server opens (read-only) |
| `ANALYST_MAX_ROWS` | `500` | Row cap returned by `run_query` |

Claude calls use structured outputs (Pydantic), an explicit `effort` setting, SDK retries
for transient API errors, and server-side refusal fallback (`fallbacks: "default"`).

## Evaluation

`eval/` holds a 52-question test set over the demo database plus a runner that scores the
real agent (approval switched off; the read-only guard and retries stay on).

| Category | # | What's checked |
|---|---|---|
| easy / medium / hard | 12 / 12 / 10 | The query result matches a hand-written reference query. Column names are ignored, small rounding differences are allowed, and row order is only checked for rankings and time series. |
| ambiguous | 6 | The query runs and the chart type is sensible (more than one reading is fine) |
| adversarial | 6 | The database is unchanged (its hash is compared before and after), and whether the model declined or the guard caught it |
| unanswerable | 6 | The agent declines instead of guessing |

Also reported: correct on first try, retry success rate, chart appropriateness,
**summary grounding** (every number in the summary must trace to a result cell or a simple
total, difference or percentage of them), latency p50/p95, tokens and cost per question.

```bash
# open-source model, no API key (llama.cpp on CPU)
pip install "llama-cpp-python[server]"
huggingface-cli download Qwen/Qwen2.5-Coder-7B-Instruct-GGUF qwen2.5-coder-7b-instruct-q4_k_m.gguf --local-dir models
python -m llama_cpp.server --model models/qwen2.5-coder-7b-instruct-q4_k_m.gguf \
    --model_alias qwen2.5-coder-7b-instruct --n_ctx 8192 --port 8000 &
python -m eval.run_eval --llm local

python -m eval.run_eval --llm claude --repeats 3      # Claude, 3 repeats for variance
python -m eval.run_eval --llm local --ids e01,h01     # subset; --category hard; --limit 5
python -m eval.report eval/runs/<run>                 # rebuild a scorecard
```

Each run writes `results.jsonl` (one line per question: SQL, attempts, result, summary and scores),
`scorecard.md` / `scorecard.json`, and the charts. Re-running with the same `--out`
resumes where it stopped. `tests/test_eval.py` checks the harness itself: a model that
returns the reference SQL must score 100%.

The agent can run on any backend: set `ANALYST_LLM=claude|local|mock` (`local` = any
OpenAI-compatible server, configured with `ANALYST_LOCAL_URL` / `ANALYST_LOCAL_MODEL`).

## Project layout

```
analyst/
  graph.py        LangGraph workflow (nodes, retry routing, interrupt)
  agent.py        DataAnalyst: owns MCP connection, graph, checkpointer
  llm.py          Claude SQL model / chart planner / summarizer + offline mock
  mcp_server.py   MCP server: list_tables, describe_schema, run_query, render_chart
  mcp_client.py   MCP stdio client
  sql_safety.py   read-only SQL guard
  charts.py       matplotlib rendering
demo/
  seed_db.py      deterministic sample e-commerce database
  run_demo.py     interactive CLI demo + Markdown report
eval/
  dataset.py      52 questions with reference SQL
  run_eval.py     runs the agent over the set, scores each case
  scoring.py      result-set matching, summary grounding
  report.py       scorecard.md / scorecard.json
tests/            end-to-end tests (approve / edit / reject / retry / guard), no API key needed
```

```bash
pytest -q
```
