"""Aggregate results.jsonl into scorecard.json + scorecard.md.

    python -m eval.report eval/runs/<run-dir>
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PRICES = {"claude-opus-5-5": (4.0, 20.0)}  # $ per 1M input / output tokens


def _rate(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(map(bool, xs)) / len(xs), 3) if xs else None


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def aggregate(records: list[dict], meta: dict) -> dict:
    gold = [r for r in records if "correct" in r["scores"]]
    ans = [r for r in records if r["expect"] == "answer"]
    by_cat: dict[str, list] = defaultdict(list)
    for r in gold:
        by_cat[r["category"]].append(r)
    retried = [r for r in ans if r["scores"].get("failed_attempts", 0) > 0]
    grounding = [r["scores"]["grounding"] for r in ans if r["scores"].get("grounding", {}).get("rate") is not None]
    lat = [r["latency_s"] for r in records]
    usage = [r["usage"] for r in records]

    # per-repeat accuracy, for spread across repeats
    reps = defaultdict(list)
    for r in gold:
        reps[r["repeat"]].append(r["scores"]["correct"])
    rep_acc = [sum(v) / len(v) for v in reps.values()]

    m = {
        "model": meta.get("llm"),
        "runs": len(records),
        "answer_accuracy": _rate([r["scores"]["correct"] for r in gold]),
        "answer_accuracy_first_try": _rate([r["scores"]["correct_first_try"] for r in gold]),
        "accuracy_by_category": {c: _rate([r["scores"]["correct"] for r in v]) for c, v in sorted(by_cat.items())},
        "accuracy_spread_across_repeats": (round(min(rep_acc), 3), round(max(rep_acc), 3)) if len(rep_acc) > 1 else None,
        "execution_success": _rate([r["scores"].get("executed") for r in ans]),
        "false_decline_rate": _rate([r["scores"].get("false_decline") for r in ans]),
        "retry": {
            "runs_with_sql_errors": len(retried),
            "recovered": _rate([r["scores"]["status"] == "done" for r in retried]) if retried else None,
            "avg_attempts_when_retried": round(statistics.mean(r["scores"]["failed_attempts"] + 1 for r in retried), 2) if retried else None,
        },
        "safety": {
            "db_unchanged": _rate([r["scores"].get("safe") for r in records if r["expect"] == "safe"]),
            "declined_write_requests": _rate([r["scores"].get("declined") for r in records if r["expect"] == "safe"]),
            "write_sql_caught_by_guard": sum(r["scores"].get("guard_blocks", 0) for r in records),
        },
        "unanswerable_declined": _rate([r["scores"].get("declined") for r in records if r["expect"] == "decline"]),
        "chart_appropriate": _rate([r["scores"].get("chart_ok") for r in ans]),
        "summary_grounding": {
            "avg_grounded_number_rate": round(statistics.mean(g["rate"] for g in grounding), 3) if grounding else None,
            "fully_grounded_summaries": _rate([g["rate"] == 1 for g in grounding]),
        },
        "latency_s": {"p50": _quantile(lat, 0.5), "p95": _quantile(lat, 0.95), "total": round(sum(lat), 1)},
        "tokens_per_question": {
            "input": round(statistics.mean(u.get("input_tokens", 0) for u in usage)) if usage else None,
            "output": round(statistics.mean(u.get("output_tokens", 0) for u in usage)) if usage else None,
            "model_calls": round(statistics.mean(u.get("calls", 0) for u in usage), 2) if usage else None,
        },
        "errors": sum(1 for r in records if r.get("error")),
    }
    model_id = (meta.get("llm") or "").split(":", 1)[-1]
    if model_id in PRICES and usage:
        pi, po = PRICES[model_id]
        cost = sum(u.get("input_tokens", 0) * pi + u.get("output_tokens", 0) * po for u in usage) / 1e6
        m["cost_usd"] = {"total": round(cost, 2), "per_question": round(cost / len(usage), 4)}
    else:
        m["cost_usd"] = {"total": 0.0, "per_question": 0.0, "note": "local / mock model: no API cost"}
    return m


def write_scorecard(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    records = [json.loads(l) for l in (run_dir / "results.jsonl").read_text().splitlines() if l.strip()]
    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    m = aggregate(records, meta)
    (run_dir / "scorecard.json").write_text(json.dumps(m, indent=2))

    L = [f"# Evaluation scorecard — {m['model']}", "",
         f"{m['runs']} runs · {meta.get('repeats', 1)} repeat(s) · max {meta.get('max_retries')} SQL retries · "
         f"started {meta.get('started', '?')}", "",
         "| Metric | Value |", "|---|---|",
         f"| **Answer accuracy** (result matches reference) | **{_pct(m['answer_accuracy'])}** |",
         f"| Correct on first try (no retry needed) | {_pct(m['answer_accuracy_first_try'])} |"]
    for c, v in m["accuracy_by_category"].items():
        L.append(f"| &nbsp;&nbsp;accuracy · {c} | {_pct(v)} |")
    if m["accuracy_spread_across_repeats"]:
        lo, hi = m["accuracy_spread_across_repeats"]
        L.append(f"| Accuracy range across repeats | {_pct(lo)} – {_pct(hi)} |")
    rt, sf, sg = m["retry"], m["safety"], m["summary_grounding"]
    L += [f"| Query ran successfully (answerable questions) | {_pct(m['execution_success'])} |",
          f"| Wrongly declined an answerable question | {_pct(m['false_decline_rate'])} |",
          f"| Runs that hit a SQL error | {rt['runs_with_sql_errors']} |",
          f"| &nbsp;&nbsp;recovered after retry | {_pct(rt['recovered'])} |",
          f"| &nbsp;&nbsp;avg attempts when retried | {rt['avg_attempts_when_retried'] or '–'} |",
          f"| **Safety: database unchanged** (adversarial) | **{_pct(sf['db_unchanged'])}** |",
          f"| &nbsp;&nbsp;model declined the write request itself | {_pct(sf['declined_write_requests'])} |",
          f"| &nbsp;&nbsp;write SQL caught by the read-only guard | {sf['write_sql_caught_by_guard']} |",
          f"| Unanswerable questions correctly declined | {_pct(m['unanswerable_declined'])} |",
          f"| Chart type appropriate | {_pct(m['chart_appropriate'])} |",
          f"| Summary numbers traceable to the data (avg) | {_pct(sg['avg_grounded_number_rate'])} |",
          f"| Summaries with every number traceable | {_pct(sg['fully_grounded_summaries'])} |",
          f"| Latency per question p50 / p95 | {m['latency_s']['p50']:.0f}s / {m['latency_s']['p95']:.0f}s |",
          f"| Tokens per question (in / out), model calls | {m['tokens_per_question']['input']} / "
          f"{m['tokens_per_question']['output']}, {m['tokens_per_question']['model_calls']} |",
          f"| Cost | ${m['cost_usd']['total']:.2f} total (${m['cost_usd']['per_question']:.4f}/question) |",
          f"| Harness errors (timeouts, crashes) | {m['errors']} |", ""]

    L += ["## Per-question results", "", "| id | category | question | outcome | retries | time |", "|---|---|---|---|---|---|"]
    for r in records:
        s = r["scores"]
        if "correct" in s:
            out = "✅ correct" if s["correct"] else f"❌ {'wrong result' if s['status'] == 'done' else s['status']}"
        elif r["expect"] == "safe":
            out = ("✅ safe, declined" if s.get("declined") else "✅ safe") if s.get("safe") else "❌ DATABASE CHANGED"
        elif r["expect"] == "decline":
            out = "✅ declined" if s.get("declined") else f"❌ {s['status']}"
        else:
            out = "✅ ran" if s.get("executed") else f"❌ {s['status']}"
        L.append(f"| {r['id']} | {r['category']} | {r['question']} | {out} | {s.get('failed_attempts', 0)} | {r['latency_s']:.0f}s |")

    misses = [r for r in records if r["scores"].get("correct") is False]
    if misses:
        L += ["", "## Wrong answers (for error analysis)", ""]
        for r in misses:
            L += [f"### {r['id']} — {r['question']}", f"status: `{r['scores']['status']}`", "",
                  f"```sql\n{r.get('sql') or '(none)'}\n```"]
            if r.get("attempts"):
                L.append(f"last error: `{r['attempts'][-1]['error']}`")
            if r.get("rows"):
                L.append(f"first rows: `{r['rows'][:3]}`")
            L.append("")
    path = run_dir / "scorecard.md"
    path.write_text("\n".join(L))
    return path


if __name__ == "__main__":
    print(write_scorecard(Path(sys.argv[1])))
