"""Publish the web demo to a Hugging Face Space (Docker SDK, free CPU tier).

    pip install huggingface_hub
    huggingface-cli login                     # or export HF_TOKEN=... (a *write* token)
    python deploy/hf_space.py --space <hf-user>/ai-data-analyst \\
        --repo-url https://github.com/<github-user>/agent_analyst

The Space builds the Dockerfile at the repo root and serves app.py on port 7860.
Re-running the script uploads the current code and triggers a rebuild.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SPACE_README = """---
title: AI Data Analyst
emoji: 📊
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: LangGraph agent + fine-tuned text-to-SQL model, MCP tools
---

# AI Data Analyst

Ask a business question in plain English. A LangGraph agent writes SQL with a fine-tuned
text-to-SQL model, [SQLForge-3B](https://huggingface.co/ay2r/sqlforge-3b)
(Qwen2.5-Coder-3B + QLoRA on Spider/BIRD), waits for your approval, runs it
through MCP tools on a read-only SQLite database, corrects its own failed queries using the
database error, then charts and summarizes the result.
{repo_line}
Runs entirely on CPU with llama.cpp, so a live question takes 1–2 minutes. The example
buttons show answers the same agent produced earlier.
"""

IGNORE = [".git/*", "**/__pycache__/*", "*.pyc", ".venv/*", ".pytest_cache/*", "outputs/*", "models/*",
          "demo/*.db", "eval/runs/*", "eval/*.log", "README.md", "*.egg-info/*"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--space", required=True, help="<hf-user>/<space-name>")
    ap.add_argument("--sql-model", default="hf://ay2r/sqlforge-3b/gguf/sqlforge-3b-Q4_K_M.gguf",
                    help="fine-tuned GGUF as hf://owner/repo/file (default: SQLForge-3B Q4_K_M)")
    ap.add_argument("--sql-lora", default="", help="optional GGUF LoRA adapter (hf://repo/file)")
    ap.add_argument("--repo-url", default="", help="GitHub URL shown in the app header")
    ap.add_argument("--private", action="store_true")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(args.space, repo_type="space", space_sdk="docker", private=args.private, exist_ok=True)
    for key, value in {"ANALYST_SQL_MODEL": args.sql_model, "ANALYST_SQL_LORA": args.sql_lora,
                       "ANALYST_REPO_URL": args.repo_url}.items():
        if value:
            api.add_space_variable(args.space, key, value)
    repo_line = f"\nSource code: {args.repo_url}\n" if args.repo_url else ""
    api.upload_file(path_or_fileobj=SPACE_README.format(repo_line=repo_line).encode(),
                    path_in_repo="README.md", repo_id=args.space, repo_type="space")
    api.upload_folder(folder_path=str(ROOT), repo_id=args.space, repo_type="space", ignore_patterns=IGNORE,
                      commit_message="Deploy AI Data Analyst demo")
    print(f"Deployed. Build logs and app: https://huggingface.co/spaces/{args.space}")


if __name__ == "__main__":
    main()
