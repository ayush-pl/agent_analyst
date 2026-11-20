"""Sanity checks for the evaluation harness itself."""

from __future__ import annotations

import asyncio
import json

from analyst.llm import MockAnalystLLM, SQLProposal
from eval import run_eval
from eval.dataset import CASES
from eval.report import aggregate
from eval.scoring import grounding, results_match

GOLD = {c["question"]: c for c in CASES}


class OracleLLM(MockAnalystLLM):
    """Returns the reference SQL, declines what should be declined."""

    async def generate_sql(self, question, schema, attempts):
        c = GOLD[question]
        if c["expect"] != "answer":
            return SQLProposal(answerable=False, sql="", rationale="not in data / write request")
        if c["gold_sql"] is None:
            return SQLProposal(sql="SELECT COUNT(*) AS n FROM orders", rationale="ambiguous")
        return SQLProposal(sql=c["gold_sql"], rationale="oracle")


def test_oracle_scores_100(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "make_llm", lambda kind: OracleLLM())
    monkeypatch.setattr("sys.argv", ["run_eval", "--llm", "mock", "--out", str(tmp_path)])
    asyncio.run(run_eval.main())
    recs = [json.loads(l) for l in (tmp_path / "results.jsonl").read_text().splitlines()]
    m = aggregate(recs, {"llm": "oracle"})
    assert len(recs) == len(CASES)
    assert m["answer_accuracy"] == 1.0
    assert m["safety"]["db_unchanged"] == 1.0
    assert m["unanswerable_declined"] == 1.0
    assert m["false_decline_rate"] == 0.0


def test_result_matching_rules():
    g = [("Asia Pacific", 432266.65), ("Europe", 826396.65)]
    assert results_match(g, ["r", "v", "n"], [["Europe", 826396.7, 5], ["Asia Pacific", 432266.6, 3]], False)
    assert not results_match(g, ["r", "v"], [["Europe", 826396.7], ["Asia Pacific", 432266.6]], True)
    assert not results_match(g, ["r", "v"], [["Europe", 826396.7]], False)
    assert results_match([("2025", 140)], ["y", "c"], [[2025, 140]], False)


def test_grounding_flags_invented_numbers():
    rows = [["NA", 602573.4], ["EU", 440000.0]]
    assert grounding("NA leads with 602,573.40, about 57.8% of the total", ["r", "v"], rows)["rate"] == 1.0
    assert grounding("NA leads with 999,999", ["r", "v"], rows)["rate"] == 0.0
