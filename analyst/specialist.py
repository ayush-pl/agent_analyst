"""Fine-tuned text-to-SQL model as the agent's SQL tool.

A small model fine-tuned on Spider/BIRD (e.g. Qwen2.5-Coder-3B + QLoRA) is very good
at one thing: ``schema + question (+ evidence) -> SQL``. It is not trained to decide
whether a question is answerable, pick charts or write summaries, so this backend
splits the work:

* **SQL specialist** (your fine-tuned model) writes every query, prompted in the
  same Spider/BIRD format it was trained on. Business definitions (what "revenue"
  means) are passed as BIRD-style *evidence*.
* **Execution-guided self-correction**: when a query fails, the database error and
  a schema-derived fix hint are fed back to the specialist (see ``error_hint``),
  and a query that repeats a failed one is resampled. This is the same loop the
  LangGraph agent already runs (``execute_sql -> generate_sql``).
* **Generalist** (any instruct model) handles the answerable/read-only gate, the
  chart plan and the summary.

Both models can run in-process via llama.cpp (``LlamaCppChat``), which is what the
Hugging Face Space uses, or behind any OpenAI-compatible server.

Configuration (environment variables):

``ANALYST_SQL_MODEL``      GGUF of the fine-tuned model: a local path or
                           ``hf://<repo>/<file>.gguf``. Defaults to the generalist.
``ANALYST_SQL_LORA``       Optional GGUF LoRA adapter applied to ``ANALYST_SQL_MODEL``.
``ANALYST_GENERAL_MODEL``  GGUF of the generalist (default: Qwen2.5-Coder-3B-Instruct Q4_K_M).
``ANALYST_SQL_PROMPT``     Path to a prompt template matching your training format,
                           with ``{schema}``, ``{question}``, ``{evidence}`` placeholders.
``ANALYST_SQL_FORMAT``     ``sqlforge`` (exact SQLForge-3B training format; the default when the
                           model name contains "sqlforge") or ``bird`` (generic Spider/BIRD prompt).
``ANALYST_SQL_SCHEMA``     ``ddl`` (CREATE TABLE only, like Spider) or ``ddl+rows``
                           (with sample rows, like many BIRD prompts). Default ``ddl+rows``.
``ANALYST_THREADS``        CPU threads for llama.cpp (default: all cores).
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field

from analyst.grounding import grounding
from analyst.llm import (
    LocalAnalystLLM, SQLProposal, _norm_sql, _preview, error_hint, fact_summary,
)

DEFAULT_GENERAL_MODEL = "hf://Qwen/Qwen2.5-Coder-3B-Instruct-GGUF/qwen2.5-coder-3b-instruct-q4_k_m.gguf"

# Generic SQLite knowledge given to the SQL model as BIRD-style "evidence". Dataset-specific
# definitions come from the "-- note:" lines of the schema (the dataset's notes file).
# Generic SQLite tips, each sent only when the question needs it (see relevant_evidence).
TIME_TIP = "dates are ISO text, use strftime('%Y-%m', col) for months and strftime('%Y', col) for years."
QUARTER_TIP = ("Quarters: Q1 = January-March (months 01-03), Q2 = April-June (04-06), "
               "Q3 = July-September (07-09), Q4 = October-December (10-12).")
RATIO_TIP = "SQLite integer division truncates, so compute rates and percentages as 100.0 * part / total."
# Spider/BIRD gold queries return only the asked-for value, so fine-tuned models learn to drop
# labels and measures; an analytics agent needs both to chart and summarize the result.
OUTPUT_TIP = ("Return the grouping column(s) together with the computed measure(s); questions with "
              "'by X', 'each X', 'per X' or 'in each month' need GROUP BY X; rankings return the label "
              "and the value.")
GENERIC_EVIDENCE = " ".join([TIME_TIP, RATIO_TIP, OUTPUT_TIP])
DEFAULT_EVIDENCE = GENERIC_EVIDENCE  # backwards-compatible name


_TIME_WORDS = re.compile(r"\b(month|monthly|year|yearly|annual|quarter|quarterly|week|weekly|daily|day|date|"
                         r"trend|over time|growth|since|before|after|20\d\d|19\d\d|q[1-4])\b", re.I)
_RATIO_WORDS = re.compile(r"\b(rate|percent|percentage|share|ratio|proportion|average|avg|mean|margin|"
                          r"growth|%)", re.I)
_SUBJECT_END = re.compile(r"\s(?:refers? to|means?|=|are|is|was|were)\s|=", re.I)
_STOP = {"the", "and", "of", "a", "an", "column", "columns", "table", "user", "definitions", "with", "for"}


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
            if len(w) > 2 and w not in _STOP}


def relevant_evidence(question: str, notes: str, always: str = OUTPUT_TIP) -> str:
    """BIRD-style evidence: only the notes whose subject the question mentions, plus tips it needs.

    BIRD hints are always about the question at hand, so a model fine-tuned on BIRD applies every
    hint it sees. Sending all dataset notes with every question made it filter "customers per
    region" to completed orders because a revenue note said so.
    """
    q = _words(question)
    timey = bool(_TIME_WORDS.search(question))
    keep = []
    lines = [ln for ln in notes.splitlines() if not ln.lstrip().lower().startswith(("table ", "original headers"))]
    text = re.sub(r"^\s*User definitions:\s*", "", "\n".join(lines), flags=re.M)
    for sent in re.split(r"(?<=[.;])\s+|\n", text):
        sent = sent.strip()
        if not sent:
            continue
        subj = _words(_SUBJECT_END.split(sent, maxsplit=1)[0])
        if "date" in subj:                       # date-format notes: only for time questions
            if timey:
                keep.append(sent)
        elif subj & q:
            keep.append(sent)
    if timey and not any("strftime" in k for k in keep):
        keep.append(TIME_TIP)
    if re.search(r"\bq[1-4]\b|quarter", question, re.I):
        keep.append(QUARTER_TIP)
    if _RATIO_WORDS.search(question):
        keep.append(RATIO_TIP)
    keep.append(always)
    return " ".join(keep)


def split_notes(schema: str) -> tuple[str, str]:
    """Separate "-- note:" lines (dataset notes) from the rest of the schema text."""
    notes = [ln[len("-- note:"):].strip() for ln in schema.splitlines() if ln.startswith("-- note:")]
    rest = "\n".join(ln for ln in schema.splitlines() if not ln.startswith("-- note:"))
    return "\n".join(notes), rest


# Spider/BIRD-style prompt. Replace with your exact training template via ANALYST_SQL_PROMPT.
DEFAULT_SQL_PROMPT = """Database schema (SQLite):
{schema}

-- External Knowledge: {evidence}
-- Using valid SQLite and understanding External Knowledge, answer the following question.
Question: {question}
SQL:"""

SQL_SPECIALIST_SYSTEM = ("You are a text-to-SQL model. Given a database schema and a question, "
                         "reply with a single SQLite SELECT query and nothing else.")

# Exact training format of SQLForge-3B (huggingface.co/ay2r/sqlforge-3b): its own system prompt,
# "### ..." sections, and sample rows written as "-- 1 | Joe Sharp | ...".
SQLFORGE_SYSTEM = ("You are an expert SQLite engineer. Given a database schema and a question, write a single "
                   "correct SQLite SELECT query that answers it. Return only the SQL, with no explanation.")
SQLFORGE_PROMPT = """### Database schema
{schema}

### Question
{question}

### Hint
{evidence}

### SQL
"""


def pipe_sample_rows(schema: str) -> str:
    """Rewrite "-- sample rows (a, b):" + "  (1, 'x')" lines as "-- sample rows (a | b):" + "-- 1 | x"."""
    import ast

    out = []
    for line in schema.splitlines():
        m = re.match(r"-- sample rows \((.*)\):$", line)
        if m:
            out.append(f"-- sample rows ({' | '.join(c.strip() for c in m.group(1).split(','))}):")
            continue
        if line.startswith(("  (", "  [")) and line.rstrip().endswith((")", "]")):
            try:
                row = ast.literal_eval(line.strip())
                row = tuple(row) if isinstance(row, (tuple, list)) else (row,)
                out.append("-- " + " | ".join("NULL" if v is None else str(v) for v in row))
                continue
            except (ValueError, SyntaxError):
                pass
        out.append(line)
    return "\n".join(out)


def resolve_model(spec: str) -> str:
    """``hf://owner/repo/file.gguf`` -> downloaded local path; anything else is a path."""
    if spec.startswith("hf://"):
        from huggingface_hub import hf_hub_download

        owner, repo, filename = spec[5:].split("/", 2)
        return hf_hub_download(f"{owner}/{repo}", filename)
    return spec


def extract_sql(text: str) -> str:
    """Pull the query out of a model reply (code fences, 'SQL:' prefixes, trailing chatter)."""
    m = re.search(r"```(?:sql|sqlite)?\s*(.*?)```", text, re.S | re.I)
    sql = m.group(1) if m else text
    sql = re.sub(r"^\s*(?:SQL|Answer)\s*:\s*", "", sql.strip(), flags=re.I)
    # Keep the first statement only; the read-only guard rejects anything after anyway.
    first = sql.split(";")[0].strip()
    # Stop at a blank line followed by prose ("Explanation: ...").
    return re.split(r"\n\s*\n(?=[A-Za-z])", first)[0].strip()


def ddl_only(schema: str) -> str:
    """Drop the '-- sample rows' blocks from a formatted schema (Spider-style prompt)."""
    out, skip = [], False
    for line in schema.splitlines():
        if line.startswith("-- sample rows"):
            skip = True
            continue
        if skip and (line.startswith("  ") or not line.strip()):
            if not line.strip():
                skip = False
                out.append(line)
            continue
        skip = False
        out.append(line)
    return "\n".join(out).strip()


class LlamaCppChat:
    """In-process llama.cpp chat model with JSON-schema constrained decoding."""

    def __init__(self, model: str, lora: str | None = None, n_ctx: int = 8192, threads: int | None = None):
        from llama_cpp import Llama

        kw: dict[str, Any] = dict(model_path=resolve_model(model), n_ctx=n_ctx, verbose=False,
                                  n_threads=threads or int(os.environ.get("ANALYST_THREADS", os.cpu_count() or 4)))
        if lora:
            kw["lora_path"] = resolve_model(lora)
        self.name = Path(kw["model_path"]).stem + (f"+{Path(kw['lora_path']).stem}" if lora else "")
        self.llm = Llama(**kw)
        self._lock = threading.Lock()   # llama.cpp contexts are not thread-safe

    def _complete(self, messages, schema, max_tokens, temperature) -> dict:
        kw: dict[str, Any] = dict(messages=messages, max_tokens=max_tokens, temperature=temperature, seed=7)
        if schema is not None:
            kw["response_format"] = {"type": "json_object", "schema": schema}
        with self._lock:
            return self.llm.create_chat_completion(**kw)

    async def __call__(self, system: str, user: str, schema: dict | None, max_tokens: int,
                       temperature: float = 0) -> tuple[str, dict]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        out = await asyncio.to_thread(self._complete, messages, schema, max_tokens, temperature)
        return out["choices"][0]["message"]["content"] or "", out.get("usage") or {}


class InProcessAnalystLLM(LocalAnalystLLM):
    """``LocalAnalystLLM`` prompts, but the model runs in this process via llama.cpp."""

    def __init__(self, chat: LlamaCppChat):
        self.chat = chat
        self.model = chat.name
        self.name = f"llama.cpp:{chat.name}"
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def _chat(self, system, user, schema, max_tokens, temperature: float = 0) -> str:
        js = None if schema is None else (schema if isinstance(schema, dict) else schema.model_json_schema())
        text, u = await self.chat(system, user, js, max_tokens, temperature)
        self.usage["calls"] += 1
        self.usage["input_tokens"] += u.get("prompt_tokens", 0)
        self.usage["output_tokens"] += u.get("completion_tokens", 0)
        return text


GROUNDED_SUMMARY_SYSTEM = """You write a short answer for a business stakeholder.
You get the question, the query result and VERIFIED FACTS computed from it.
- Lead with a one-sentence direct answer, then 2-3 bullet points.
- Use ONLY numbers that appear in the verified facts or the result, copied exactly.
- Never use placeholders like [Name] or invent comparisons, causes or forecasts.
- Under 110 words, Markdown."""

_PLACEHOLDER = re.compile(r"\[[A-Z][^\]]{0,40}\]|\{[a-z_]+\}|XX")


class Gate(BaseModel):
    answerable: bool = Field(description="False if the data is missing or the request would modify the database.")
    reason: str


GATE_SYSTEM = """You screen questions for a read-only SQL analytics agent.
answerable=false ONLY if (a) the request asks to insert, update, delete, create, drop or
otherwise change data, or (b) the schema clearly has no data for it (e.g. NPS, marketing
spend, employees, website traffic, support tickets, shipping costs).
Vague questions ("how are we doing?") ARE answerable: pick a sensible metric.
Reply with JSON {"answerable": bool, "reason": str}."""


class FineTunedSQLAnalystLLM:
    """Agent LLM whose SQL comes from a fine-tuned text-to-SQL model.

    ``sql_chat`` is the specialist (a ``LlamaCppChat`` or any async callable with the
    same signature); ``general`` is an ``InProcessAnalystLLM``/``LocalAnalystLLM``
    used for the gate, the chart plan and the summary.
    """

    def __init__(self, sql_chat: Any, general: Any, prompt_template: str | None = None,
                 evidence: str = GENERIC_EVIDENCE, schema_style: str | None = None,
                 prompt_format: str | None = None):
        self.sql_chat = sql_chat
        self.general = general
        tpl = prompt_template or os.environ.get("ANALYST_SQL_PROMPT")
        fmt = prompt_format or os.environ.get("ANALYST_SQL_FORMAT") or (
            "sqlforge" if "sqlforge" in str(getattr(sql_chat, "name", "")).lower() else "bird")
        self.prompt_format = "custom" if tpl else fmt
        if tpl:
            self.template = Path(tpl).read_text() if Path(tpl).is_file() else tpl
            self.system = SQL_SPECIALIST_SYSTEM
        elif fmt == "sqlforge":
            self.template, self.system = SQLFORGE_PROMPT, SQLFORGE_SYSTEM
        else:
            self.template, self.system = DEFAULT_SQL_PROMPT, SQL_SPECIALIST_SYSTEM
        self.evidence = evidence
        self.schema_style = schema_style or os.environ.get("ANALYST_SQL_SCHEMA", "ddl+rows")
        self.name = f"sql:{getattr(sql_chat, 'name', 'specialist')} + {general.name}"
        self._sql_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.summary_fallbacks = 0

    @property
    def usage(self) -> dict:
        g = self.general.usage
        return {k: g.get(k, 0) + self._sql_usage[k] for k in self._sql_usage}

    async def _gate(self, question: str, schema: str) -> Gate:
        user = f"<schema>\n{ddl_only(schema)}\n</schema>\n\n<question>{question}</question>"
        return Gate.model_validate_json(await self.general._chat(GATE_SYSTEM, user, Gate, 120))

    async def _specialist(self, prompt: str, temperature: float) -> str:
        text, u = await self.sql_chat(self.system, prompt, None, 400, temperature)
        self._sql_usage["calls"] += 1
        self._sql_usage["input_tokens"] += u.get("prompt_tokens", 0)
        self._sql_usage["output_tokens"] += u.get("completion_tokens", 0)
        return extract_sql(text)

    def sql_prompt(self, question: str, schema: str, attempts: Sequence[dict]) -> str:
        notes, schema_only = split_notes(schema)
        sch = ddl_only(schema_only) if self.schema_style == "ddl" else schema_only
        if self.prompt_format == "sqlforge":
            sch = pipe_sample_rows(sch.replace("-- dialect: sqlite", "").strip())
        evidence = (relevant_evidence(question, notes) if self.evidence == GENERIC_EVIDENCE
                    else f"{notes} {self.evidence}".strip())
        # Execution-guided self-correction: show the failed queries and what went wrong.
        fb = []
        for a in attempts:
            hint = error_hint(str(a.get("error", "")), schema, a.get("sql", ""))
            fb.append(f"-- Failed query: {' '.join(a['sql'].split())}\n-- Error: {a['error']}"
                      + (f"\n-- Fix: {hint}" if hint else ""))
        if fb and self.prompt_format == "sqlforge":
            # The model only learned to read extra knowledge from ### Hint, so feedback goes there.
            evidence += "\nThese earlier queries failed; write a different, corrected query.\n" + "\n".join(fb)
            fb = []
        prompt = self.template.format(schema=sch, question=question, evidence=evidence)
        if fb:
            prompt = prompt.replace("Question:", "\n".join(fb) + "\n-- Write a corrected query.\nQuestion:", 1) \
                if "Question:" in prompt else prompt + "\n" + "\n".join(fb)
        return prompt

    async def generate_sql(self, question: str, schema: str, attempts: Sequence[dict]) -> SQLProposal:
        if not attempts:
            gate = await self._gate(question, schema)
            if not gate.answerable:
                return SQLProposal(answerable=False, sql="", rationale=gate.reason)
        prompt = self.sql_prompt(question, schema, attempts)
        sql = await self._specialist(prompt, 0)
        if sql and _norm_sql(sql) in {_norm_sql(a["sql"]) for a in attempts}:
            sql = await self._specialist(prompt, 0.7)
        why = "Generated by the fine-tuned SQL model" + (f" (self-correction #{len(attempts)})" if attempts else "")
        return SQLProposal(answerable=True, sql=sql, rationale=why)

    async def plan_chart(self, question, columns, rows):
        return await self.general.plan_chart(question, columns, rows)

    async def summarize(self, question, sql, columns, rows, chart, truncated) -> str:
        """Small models misread tables, so facts are computed in code and the model only
        phrases them. A summary with any number not traceable to the result, or with a
        placeholder, is replaced by the exact fact summary."""
        facts = fact_summary(question, columns, rows, chart)
        if not rows or len(columns) == 1 and len(rows) == 1 or all(v is None for r in rows for v in r):
            return facts
        user = (f"Question: {question}\n\nVerified facts:\n{facts}\n\n"
                f"Result ({len(rows)} rows{', truncated' if truncated else ''}; first row is the header):\n"
                f"{_preview(columns, rows, 30)}")
        text = (await self.general._chat(GROUNDED_SUMMARY_SYSTEM, user, None, 260)).strip()
        g = grounding(text, columns, rows)
        if not text or _PLACEHOLDER.search(text) or (g["rate"] is not None and g["ungrounded"]):
            self.summary_fallbacks += 1
            return facts
        return text


def specialist_from_env() -> FineTunedSQLAnalystLLM:
    """Build the two-model backend from environment variables (see module docstring)."""
    general_chat = LlamaCppChat(os.environ.get("ANALYST_GENERAL_MODEL", DEFAULT_GENERAL_MODEL))
    sql_spec = os.environ.get("ANALYST_SQL_MODEL")
    sql_chat = (LlamaCppChat(sql_spec, lora=os.environ.get("ANALYST_SQL_LORA")) if sql_spec else general_chat)
    return FineTunedSQLAnalystLLM(sql_chat, InProcessAnalystLLM(general_chat))
