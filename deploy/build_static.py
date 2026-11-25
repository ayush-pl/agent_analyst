"""Build the static demo site: replays of real recorded agent runs, a live-run video and the
evaluation results. Free to host anywhere static (Hugging Face static Space, GitHub Pages).

    python app.py --build-cache          # record runs (demo/cache/*.json), needs the models
    python deploy/build_static.py        # -> site/index.html + site/assets/
    python -m http.server -d site 8000   # preview

Nothing here runs a model: every answer on the page was produced by the agent beforehand and
the page says so. The live app is app.py (see the README for hosting it).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import display_columns, pretty_sql  # noqa: E402  (no gradio import at module level)

CACHE = ROOT / "demo" / "cache"
RUNS = ROOT / "eval" / "runs"
MODEL_URL = "https://huggingface.co/ay2r/sqlforge-3b"


def _run(item: dict, asset_prefix: str) -> dict:
    st = item["state"]
    res = st.get("result") or {}
    chart = st.get("chart_path")
    return {
        "question": item["question"],
        "status": st.get("status"),
        "sql": pretty_sql(st.get("sql") or ""),
        "rationale": st.get("rationale") or "",
        "attempts": [{"sql": pretty_sql(a.get("sql", "")), "error": a.get("error", "")} for a in st.get("attempts") or []],
        "summary": st.get("summary") or "",
        "events": st.get("events") or [],
        "chart": f"assets/{chart}" if chart else None,
        "columns": display_columns(res.get("columns") or []),
        "rows": (res.get("rows") or [])[:25],
        "total_rows": len(res.get("rows") or []),
        "seconds": item.get("seconds"),
    }


def _score(run: str) -> dict:
    f = RUNS / run / "scorecard.json"
    return json.loads(f.read_text()) if f.exists() else {}


def collect() -> dict:
    shop = json.loads((CACHE / "examples.json").read_text())
    csv = json.loads((CACHE / "csv_examples.json").read_text())
    s_ft, s_base, s_7b = _score("sqlforge-3b"), _score("qwen3b-sqlforge-prompt"), _score("qwen2.5-coder-7b-retryfix")
    c_ft, c_base = _score("csv-sqlforge-3b"), _score("csv-qwen3b-sqlforge-prompt")

    def pct(d, *keys):
        for k in keys:
            d = (d or {}).get(k) if isinstance(d, dict) else None
        return None if d is None else round(100 * d, 1)

    def csv_correct(d):
        return None if not d else round(9 * d.get("answer_accuracy", 0))

    evals = {
        "columns": ["Qwen2.5-Coder-7B", "Qwen2.5-Coder-3B (stock)", "SQLForge-3B (fine-tuned)"],
        "rows": [
            ["Shop: answer accuracy (34 questions)", pct(s_7b, "answer_accuracy"), pct(s_base, "answer_accuracy"),
             pct(s_ft, "answer_accuracy"), "%"],
            ["Unseen CSV: correct answers (of 9)", None, csv_correct(c_base), csv_correct(c_ft), ""],
            ["Safety: database unchanged (6 attacks)", pct(s_7b, "safety", "db_unchanged"),
             pct(s_base, "safety", "db_unchanged"), pct(s_ft, "safety", "db_unchanged"), "%"],
            ["Summary numbers traceable to the data", pct(s_7b, "summary_grounding", "avg_grounded_number_rate"),
             pct(s_base, "summary_grounding", "avg_grounded_number_rate"),
             pct(s_ft, "summary_grounding", "avg_grounded_number_rate"), "%"],
            ["Unanswerable questions declined (shop)", pct(s_7b, "unanswerable_declined"),
             pct(s_base, "unanswerable_declined"), pct(s_ft, "unanswerable_declined"), "%"],
            ["Median seconds per question (4 CPU cores)", (s_7b.get("latency_s") or {}).get("p50"),
             (s_base.get("latency_s") or {}).get("p50"), (s_ft.get("latency_s") or {}).get("p50"), "s"],
        ],
    }
    return {
        "model_url": MODEL_URL,
        "shop": [_run(i, "") for i in shop],
        "csv": {"file": csv["file"], "rows": csv["rows"], "table": csv["table"], "notes": csv["notes"],
                "overview": csv["overview"], "runs": [_run(i, "") for i in csv["runs"]]},
        "evals": evals,
    }


def build(out: Path, repo_url: str) -> Path:
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    for png in CACHE.glob("*.png"):
        shutil.copy(png, out / "assets" / png.name)
    for name in ("live-run.mp4", "poster.png"):
        src = ROOT / "docs" / "demo" / name
        if src.exists():
            shutil.copy(src, out / "assets" / name)
    shutil.copy(ROOT / "demo" / "sample_coffee_sales.csv", out / "assets" / "sample_coffee_sales.csv")
    data = json.dumps(collect(), default=str).replace("</", "<\\/")
    html = (Path(__file__).with_name("static_template.html").read_text()
            .replace("__DATA__", data).replace("__REPO_URL__", repo_url or "#"))
    (out / "index.html").write_text(html)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--repo-url", default="https://github.com/ayush-pl/agent_analyst")
    args = ap.parse_args()
    print(f"Built {build(args.out, args.repo_url) / 'index.html'}")


if __name__ == "__main__":
    main()
