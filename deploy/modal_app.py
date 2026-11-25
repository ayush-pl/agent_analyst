"""Run the live web demo on Modal (serverless; scales to zero when nobody is using it).

    pip install modal
    modal setup                      # one-time: log in, creates a token
    modal deploy deploy/modal_app.py # prints the public URL (https://<you>--ai-data-analyst-web.modal.run)

Cost: Modal bills CPU/memory per second only while the container runs and the Starter plan
includes monthly free credits (see modal.com/pricing for current numbers). The container
shuts down SCALEDOWN_S seconds after the last request; the next visitor waits for a cold
start (about a minute: boot + loading the models, which are baked into the image).

One container only: the human-approval step keeps the paused LangGraph run in memory
between "Ask" and "Approve", so every request has to reach the same process.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
SQL_MODEL = os.environ.get("ANALYST_SQL_MODEL", "hf://ay2r/sqlforge-3b/gguf/sqlforge-3b-Q4_K_M.gguf")
GENERAL_MODEL = "hf://Qwen/Qwen2.5-Coder-3B-Instruct-GGUF/qwen2.5-coder-3b-instruct-q4_k_m.gguf"
REPO_URL = os.environ.get("ANALYST_REPO_URL", "https://github.com/ayush-pl/agent_analyst")
CPU, MEMORY_MB, SCALEDOWN_S = 4.0, 8192, 300


def _download_cmd(*specs: str) -> str:
    calls = []
    for spec in specs:
        owner, repo, filename = spec[len("hf://"):].split("/", 2)
        calls.append(f"d('{owner}/{repo}', '{filename}')")
    return 'python -c "from huggingface_hub import hf_hub_download as d; ' + "; ".join(calls) + '"'


image = (
    modal.Image.debian_slim(python_version="3.11")
    # Prebuilt CPU wheel: nothing is compiled during the image build.
    .pip_install("llama-cpp-python==0.3.38",
                 extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cpu",
                 extra_options="--only-binary=llama-cpp-python")
    .pip_install("gradio==6.32.0", "anthropic>=2.11.0", "langgraph>=1.2", "mcp>=2.3", "matplotlib>=3.9",
                 "pydantic>=2", "httpx>=0.27", "huggingface_hub>=0.24", "pandas>=2")
    # Bake the models (~4 GB) into the image so a cold start does not download them.
    .run_commands(_download_cmd(GENERAL_MODEL, SQL_MODEL))
    .env({"ANALYST_LLM": "specialist", "ANALYST_SQL_MODEL": SQL_MODEL, "ANALYST_GENERAL_MODEL": GENERAL_MODEL,
          "ANALYST_REPO_URL": REPO_URL, "ANALYST_THREADS": str(int(CPU)), "GRADIO_ANALYTICS_ENABLED": "False"})
    # Code last, so code changes redeploy without re-downloading models.
    .add_local_dir(str(ROOT), "/root/app", ignore=[".git", "**/__pycache__", "**/*.pyc", ".venv", "models",
                                                   "outputs", "site", "eval/runs", "eval/*.log", "**/*.db",
                                                   ".pytest_cache", "docs"])
)

app = modal.App("ai-data-analyst", image=image)


@app.function(cpu=CPU, memory=MEMORY_MB, max_containers=1, scaledown_window=SCALEDOWN_S, timeout=3600)
@modal.concurrent(max_inputs=100)
@modal.web_server(port=7860, startup_timeout=300)
def web() -> None:
    import subprocess

    subprocess.Popen(["python", "app.py"], cwd="/root/app",
                     env={**os.environ, "HOST": "0.0.0.0", "PORT": "7860"})
