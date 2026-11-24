"""Run the evaluation set through the real agent and write a scorecard.

    python -m eval.run_eval --llm local                       # open model via llama.cpp server
    python -m eval.run_eval --llm claude --repeats 3
    python -m eval.run_eval --llm specialist                  # fine-tuned SQL model (ANALYST_SQL_MODEL) in-process
    python -m eval.run_eval --llm mock                        # check the harness itself
    python -m eval.run_eval --llm local --ids e01,m02,h01     # subset
    python -m eval.run_eval --llm local --out eval/runs/x     # resumes if x/results.jsonl exists

Approval is switched off (the eval measures the model, not the reviewer); the
read-only guard and retries stay on. Results stream to <out>/results.jsonl
so long runs can be resumed; the scorecard is rebuilt from that file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.agent import DataAnalyst  # noqa: E402
from analyst.llm import ClaudeAnalystLLM, LocalAnalystLLM, MockAnalystLLM  # noqa: E402
from demo.seed_db import DEFAULT_PATH, seed  # noqa: E402
from eval.dataset import CASES  # noqa: E402
from eval.report import write_scorecard  # noqa: E402
from eval.scoring import grounding, results_match  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def gold_rows(db: Path, sql: str) -> list:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [list(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def score_case(case: dict, state: dict, db: Path, db_changed: bool) -> dict:
    status = state.get("status")
    attempts = state.get("attempts", [])
    result = state.get("result") or {}
    s: dict = {"status": status, "failed_attempts": len(attempts),
               "guard_blocks": sum(a.get("stage") == "guard" for a in attempts)}
    exp = case["expect"]
    if exp == "safe":
        s["safe"] = not db_changed
        s["declined"] = status == "unanswerable"
        return s
    if exp == "decline":
        s["declined"] = status == "unanswerable"
        return s
    s["executed"] = status == "done"
    s["false_decline"] = status == "unanswerable"
    if case["gold_sql"] and status == "done":
        s["correct"] = results_match(gold_rows(db, case["gold_sql"]), result.get("columns", []),
                                     result.get("rows", []), case["ordered"])
    elif case["gold_sql"]:
        s["correct"] = False
    if case["gold_sql"]:
        s["correct_first_try"] = bool(s["correct"]) and not attempts
    if status == "done" and case["charts"]:
        s["chart_ok"] = (state.get("chart_spec") or {}).get("chart_type") in case["charts"]
    if status == "done" and state.get("summary"):
        s["grounding"] = grounding(state["summary"], result.get("columns", []), result.get("rows", []))
    return s


def make_llm(kind: str):
    if kind == "specialist":
        from analyst.specialist import specialist_from_env
        return specialist_from_env()
    return {"local": LocalAnalystLLM, "claude": ClaudeAnalystLLM, "mock": MockAnalystLLM}[kind]()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", choices=["local", "specialist", "claude", "mock"], default=os.environ.get("ANALYST_LLM", "local"))
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--category", help="comma-separated categories")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=1800, help="seconds per question")
    ap.add_argument("--db", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if not args.db.exists():
        seed(args.db)
    cases = CASES
    if args.ids:
        ids = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in ids]
    if args.category:
        cats = set(args.category.split(","))
        cases = [c for c in cases if c["category"] in cats]
    if args.limit:
        cases = cases[: args.limit]

    out = args.out or ROOT / "eval" / "runs" / f"{datetime.now():%Y%m%d-%H%M}-{args.llm}"
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    done = set()
    if results_path.exists():
        done = {(r["id"], r["repeat"]) for r in map(json.loads, results_path.read_text().splitlines())}

    llm = make_llm(args.llm)
    meta = {"llm": llm.name, "started": datetime.now().isoformat(timespec="seconds"),
            "max_retries": args.max_retries, "repeats": args.repeats, "n_cases": len(cases)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    db_hash = sha256(args.db)
    todo = [(c, r) for r in range(args.repeats) for c in cases if (c["id"], r) not in done]
    print(f"{llm.name}: {len(todo)} runs to do ({len(done)} already in {results_path})", flush=True)

    async with DataAnalyst(db_path=str(args.db), llm=llm, require_approval=False,
                           max_retries=args.max_retries, output_dir=str(out / "charts")) as analyst:
        for i, (case, rep) in enumerate(todo, 1):
            before = dict(llm.usage)
            t0 = time.perf_counter()
            try:
                run = await asyncio.wait_for(analyst.ask(case["question"]), timeout=args.timeout)
                state, error = run.state, None
            except Exception as e:  # noqa: BLE001 - record and continue
                state, error = {"status": "error"}, f"{type(e).__name__}: {e}"
            latency = time.perf_counter() - t0
            new_hash = sha256(args.db)
            changed = new_hash != db_hash
            if changed:  # should never happen; restore so later cases stay valid
                seed(args.db)
                db_hash = sha256(args.db)
            usage = {k: llm.usage[k] - before.get(k, 0) for k in llm.usage}
            result = state.get("result") or {}
            rec = {
                "id": case["id"], "category": case["category"], "question": case["question"], "repeat": rep,
                "expect": case["expect"], "latency_s": round(latency, 2), "usage": usage, "error": error,
                "sql": state.get("sql"), "attempts": state.get("attempts", []), "events": state.get("events", []),
                "columns": result.get("columns"), "rows": (result.get("rows") or [])[:50],
                "chart_spec": state.get("chart_spec"), "chart_path": state.get("chart_path"),
                "summary": state.get("summary"),
                "scores": score_case(case, state, args.db, changed) if not error else {"status": "error"},
            }
            with results_path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            sc = rec["scores"]
            mark = {True: "✓", False: "✗"}.get(sc.get("correct", sc.get("declined", sc.get("executed"))), "·")
            print(f"[{i}/{len(todo)}] {case['id']} {mark} {sc.get('status')} "
                  f"retries={sc.get('failed_attempts', 0)} {latency:.0f}s {error or ''}", flush=True)

    card = write_scorecard(out)
    print(f"\nScorecard: {card}")


if __name__ == "__main__":
    asyncio.run(main())
