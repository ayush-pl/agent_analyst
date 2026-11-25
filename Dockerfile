# Web demo image: Hugging Face Spaces (Docker SDK), Render, Fly.io, Cloud Run or any Docker host.
# Models run on CPU in-process via llama.cpp; no API keys needed.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs containers as uid 1000.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY requirements.txt requirements-app.txt ./
# GGML_NATIVE=OFF: portable CPU build (the build host and the serving host can differ).
RUN CMAKE_ARGS="-DGGML_NATIVE=OFF" pip install --no-cache-dir -r requirements.txt -r requirements-app.txt

COPY --chown=user . .
USER user
ENV HOME=/home/user HF_HOME=/home/user/.cache/huggingface PYTHONUNBUFFERED=1 \
    ANALYST_LLM=specialist GRADIO_ANALYTICS_ENABLED=False

# Set these as Space variables (they are passed as build args) or with --build-arg.
ARG ANALYST_SQL_MODEL="hf://ay2r/sqlforge-3b/gguf/sqlforge-3b-Q4_K_M.gguf"
ARG ANALYST_SQL_LORA=""
ARG ANALYST_REPO_URL=""
ENV ANALYST_SQL_MODEL=${ANALYST_SQL_MODEL} ANALYST_SQL_LORA=${ANALYST_SQL_LORA} ANALYST_REPO_URL=${ANALYST_REPO_URL}

# Seed the demo database and download the models at build time so cold starts are fast.
RUN python -c "from demo.seed_db import seed; seed('demo/shop.db')" \
 && python -c "import os; from analyst.specialist import resolve_model as r, DEFAULT_GENERAL_MODEL as g; \
[r(m) for m in (os.environ.get('ANALYST_GENERAL_MODEL', g), os.environ['ANALYST_SQL_MODEL'], os.environ['ANALYST_SQL_LORA']) if m]"

EXPOSE 7860
CMD ["python", "app.py"]
