"""End-to-end tests with the offline mock model (no API key needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from analyst.agent import DataAnalyst
from analyst.llm import MockAnalystLLM, SQLProposal
from analyst.sql_safety import UnsafeSQLError, check_read_only
from demo.seed_db import seed


@pytest.fixture(scope="session")
def db(tmp_path_factory) -> str:
    return str(seed(tmp_path_factory.mktemp("db") / "shop.db"))


def run(coro):
    return asyncio.run(coro)


async def _ask(db, tmp_path, question, decisions=("approve",) * 10, llm=None, **kw):
    async with DataAnalyst(db_path=db, llm=llm or MockAnalystLLM(), output_dir=str(tmp_path), **kw) as a:
        r = await a.ask(question)
        seen = []
        for d in decisions:
            if r.done:
                break
            seen.append(r.pending_approval)
            action, *extra = d if isinstance(d, tuple) else (d,)
            r = await a.resume(r, action, sql=extra[0] if action == "edit" else None,
                               reason=extra[0] if action == "reject" and extra else None)
        return r, seen


def test_happy_path_with_chart_and_summary(db, tmp_path):
    r, seen = run(_ask(db, tmp_path, "What are our top 10 products by revenue?"))
    assert len(seen) == 1 and r.state["status"] == "done"
    assert r.state["chart_spec"]["chart_type"] == "hbar"
    assert Path(r.state["chart_path"]).exists()
    assert "Carbon Road Bike" in r.state["summary"]


def test_sql_error_triggers_retry_and_reapproval(db, tmp_path):
    r, seen = run(_ask(db, tmp_path, "What is the monthly revenue trend?"))
    assert r.state["status"] == "done"
    assert len(r.state["attempts"]) == 1 and "no such column" in r.state["attempts"][0]["error"]
    assert len(seen) == 2 and seen[1]["previous_errors"]  # second query was approved again
    assert r.state["chart_spec"]["chart_type"] == "line"


def test_reject_stops_before_execution(db, tmp_path):
    r, _ = run(_ask(db, tmp_path, "Revenue by region", decisions=[("reject", "too broad")]))
    assert r.state["status"] == "rejected" and "result" not in r.state
    assert "too broad" in r.state["summary"]


def test_human_can_edit_sql(db, tmp_path):
    sql = "SELECT channel, COUNT(*) AS orders FROM orders GROUP BY channel ORDER BY orders DESC"
    r, _ = run(_ask(db, tmp_path, "Revenue by region", decisions=[("edit", sql)]))
    assert r.state["status"] == "done" and r.state["sql"] == sql
    assert r.state["result"]["columns"] == ["channel", "orders"]


def test_no_approval_mode(db, tmp_path):
    r, seen = run(_ask(db, tmp_path, "Return rate by category", require_approval=False))
    assert not seen and r.state["status"] == "done"


class AlwaysBroken(MockAnalystLLM):
    async def generate_sql(self, question, schema, attempts):
        return SQLProposal(sql="SELECT nope FROM orders", rationale="broken on purpose")


def test_retries_exhausted(db, tmp_path):
    r, _ = run(_ask(db, tmp_path, "anything", llm=AlwaysBroken(), max_retries=2, require_approval=False))
    assert r.state["status"] == "failed" and len(r.state["attempts"]) == 3


class WritesData(MockAnalystLLM):
    async def generate_sql(self, question, schema, attempts):
        if not attempts:
            return SQLProposal(sql="DELETE FROM orders", rationale="malicious")
        return await super().generate_sql(question, schema, attempts)


def test_write_sql_is_blocked_and_retried(db, tmp_path):
    r, seen = run(_ask(db, tmp_path, "Top products", llm=WritesData()))
    assert r.state["status"] == "done"
    assert r.state["attempts"][0]["stage"] == "guard"
    assert all("DELETE" not in p["sql"] for p in seen)  # never shown to the human / never run


@pytest.mark.parametrize("sql", [
    "DROP TABLE orders", "SELECT 1; DELETE FROM orders", "PRAGMA table_info(orders)",
    "WITH x AS (SELECT 1) DELETE FROM orders", "",
])
def test_guard_rejects(sql):
    with pytest.raises(UnsafeSQLError):
        check_read_only(sql)


def test_guard_allows_keywords_in_strings():
    assert check_read_only("SELECT * FROM orders WHERE status = 'delete me';")


def test_error_hint_points_to_owning_table():
    from analyst.llm import error_hint, retry_feedback
    schema = ("CREATE TABLE customers (\n    customer_id INTEGER PRIMARY KEY,\n    region TEXT NOT NULL\n)\n\n"
              "CREATE TABLE orders (\n    order_id INTEGER PRIMARY KEY,\n    customer_id INTEGER\n)")
    assert "customers" in error_hint("no such column: region", schema)
    assert "customers, orders" in error_hint("ambiguous column name: customer_id", schema)
    assert error_hint("syntax error", schema) == ""
    fb = retry_feedback([{"sql": "SELECT region FROM orders", "error": "no such column: region"}], schema)
    assert "Hint:" in fb and "Do not repeat" in fb


def test_long_format_result_gets_series():
    from analyst.graph import _infer_series
    from analyst.llm import ChartSpec
    cols = ["month", "revenue", "channel"]
    rows = [["2026-01", 1.0, "web"], ["2026-01", 2.0, "mobile"], ["2026-02", 3.0, "web"], ["2026-02", 4.0, "mobile"]]
    spec = _infer_series(ChartSpec(chart_type="line", x="month", y="revenue"), cols, rows)
    assert spec.series == "channel"
    flat = _infer_series(ChartSpec(chart_type="bar", x="month", y="revenue"), cols[:2], [r[:2] for r in rows[::2]])
    assert flat.series is None
