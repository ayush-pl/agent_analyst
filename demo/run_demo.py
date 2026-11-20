"""Interactive demo of the AI Data Analyst.

    python demo/run_demo.py                      # scripted demo questions, you approve each query
    python demo/run_demo.py --auto-approve       # no prompts
    python demo/run_demo.py -q "Top 5 customers by revenue"
    python demo/run_demo.py --mock               # force the offline mock model

Uses Claude when ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is set, otherwise an
offline mock model so the whole pipeline (MCP tools, approval, retries, charts)
can be seen without credentials. Writes charts and a Markdown report to outputs/.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.agent import AnalystRun, DataAnalyst  # noqa: E402
from analyst.llm import ClaudeAnalystLLM, MockAnalystLLM, default_llm  # noqa: E402
from demo.seed_db import DEFAULT_PATH, seed  # noqa: E402

DEMO_QUESTIONS = [
    "What is the monthly revenue trend?",              # mock model fails once here → retry loop
    "Which regions generated the most revenue in 2026?",
    "What are our top 10 products by revenue?",
    "How does revenue by sales channel compare each quarter?",
    "What is the return rate by product category?",
]

BOLD, DIM, CYAN, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = CYAN = GREEN = RED = YELLOW = RESET = ""


def banner(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'━' * 78}\n{text}\n{'━' * 78}{RESET}")


def ask_human(p: dict) -> tuple[str, str | None]:
    print(f"\n{YELLOW}{BOLD}⏸  Human approval required{RESET} (attempt {p['attempt']})")
    if p["previous_errors"]:
        print(f"{DIM}   previous error: {p['previous_errors'][-1]}{RESET}")
    print(f"{DIM}   rationale: {p['rationale']}{RESET}")
    print(textwrap.indent(p["sql"], "   │ "))
    while True:
        choice = input(f"   {BOLD}[a]pprove / [e]dit / [r]eject?{RESET} ").strip().lower() or "a"
        if choice.startswith("a"):
            return "approve", None
        if choice.startswith("r"):
            return "reject", input("   reason: ").strip() or None
        if choice.startswith("e"):
            print("   Enter the new SQL; finish with an empty line:")
            lines = []
            while (line := input("   > ")).strip():
                lines.append(line)
            if lines:
                return "edit", "\n".join(lines)


async def run_question(analyst: DataAnalyst, q: str, auto: bool) -> AnalystRun:
    banner(f"❓ {q}")
    run = await analyst.ask(q)
    while not run.done:
        p = run.pending_approval
        if auto:
            print(f"\n{DIM}auto-approving attempt {p['attempt']}:{RESET}")
            print(textwrap.indent(p["sql"], "   │ "))
            run = await analyst.resume(run, "approve")
        else:
            action, extra = ask_human(p)
            run = await analyst.resume(run, action, sql=extra if action == "edit" else None,
                                       reason=extra if action == "reject" else None)
    s = run.state
    print(f"\n{BOLD}Trace{RESET}")
    for e in s.get("events", []):
        colour = RED if "error" in e.lower() or "reject" in e.lower() else DIM
        print(f"  {colour}• {e}{RESET}")
    colour = GREEN if s.get("status") == "done" else RED
    print(f"\n{BOLD}{colour}Summary ({s.get('status')}){RESET}\n{s.get('summary', '')}")
    if s.get("chart_path"):
        print(f"\n📊 chart: {Path(s['chart_path']).relative_to(ROOT) if ROOT in Path(s['chart_path']).parents else s['chart_path']}")
    return run


def write_report(runs: list[AnalystRun], out_dir: Path, model: str) -> Path:
    lines = [f"# AI Data Analyst — demo report",
             f"_Generated {datetime.now():%Y-%m-%d %H:%M} · model: {model}_\n"]
    for r in runs:
        s = r.state
        lines.append(f"## {s['question']}\n")
        lines.append(f"**Status:** {s.get('status')} · **SQL attempts:** {len(s.get('attempts', [])) + (1 if s.get('status') == 'done' else 0)}\n")
        if s.get("chart_path"):
            lines.append(f"![chart]({Path(s['chart_path']).name})\n")
        lines.append(s.get("summary", "") + "\n")
        if s.get("sql"):
            lines.append(f"<details><summary>SQL</summary>\n\n```sql\n{s['sql']}\n```\n</details>\n")
        if s.get("attempts"):
            lines.append("<details><summary>Failed attempts (auto-retried)</summary>\n")
            for a in s["attempts"]:
                lines.append(f"```sql\n{a['sql']}\n```\n> {a['error']}\n")
            lines.append("</details>\n")
    path = out_dir / "report.md"
    path.write_text("\n".join(lines))
    return path


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-q", "--question", action="append", help="question to ask (repeatable)")
    ap.add_argument("--auto-approve", action="store_true", help="skip the human approval prompts")
    ap.add_argument("--mock", action="store_true", help="use the offline mock model even if an API key is set")
    ap.add_argument("--db", type=Path, default=DEFAULT_PATH, help="SQLite database (seeded if missing)")
    ap.add_argument("--out", type=Path, default=ROOT / "outputs")
    ap.add_argument("--max-retries", type=int, default=3)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Seeding demo database at {args.db} …")
        seed(args.db)

    llm = default_llm(force_mock=args.mock)
    if isinstance(llm, MockAnalystLLM) and not args.mock:
        print(f"{YELLOW}No ANTHROPIC_API_KEY found — running with the offline mock model.{RESET}")
    model = llm.model if isinstance(llm, ClaudeAnalystLLM) else llm.name

    async with DataAnalyst(db_path=str(args.db), llm=llm, require_approval=True,
                           max_retries=args.max_retries, output_dir=str(args.out)) as analyst:
        print(f"{BOLD}AI Data Analyst{RESET} · model: {model} · MCP tools: {', '.join(analyst.mcp_tools)}")
        runs = [await run_question(analyst, q, args.auto_approve) for q in (args.question or DEMO_QUESTIONS)]

    report = write_report(runs, args.out, model)
    banner(f"✅ Done — report written to {report}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nbye")
