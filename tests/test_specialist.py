"""Fine-tuned SQL specialist backend, with fake models (no weights needed)."""

import asyncio

from analyst.llm import ChartSpec
from analyst.specialist import FineTunedSQLAnalystLLM, ddl_only, extract_sql

SCHEMA = ("-- dialect: sqlite\n\nCREATE TABLE customers (\n    customer_id INTEGER PRIMARY KEY,\n    region TEXT\n)\n"
          "-- sample rows (customer_id, region):\n  (1, 'Europe')\n\n"
          "CREATE TABLE orders (\n    order_id INTEGER PRIMARY KEY,\n    customer_id INTEGER\n)")


class FakeSQL:
    name = "fake-sql"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    async def __call__(self, system, user, schema, max_tokens, temperature=0):
        self.prompts.append((user, temperature))
        return self.replies.pop(0), {"prompt_tokens": 10, "completion_tokens": 5}


class FakeGeneral:
    name = "fake-general"
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def __init__(self, answerable=True):
        self.answerable = answerable

    async def _chat(self, system, user, schema, max_tokens, temperature=0):
        return f'{{"answerable": {str(self.answerable).lower()}, "reason": "r"}}'

    async def plan_chart(self, *a):
        return ChartSpec(chart_type="none")

    async def summarize(self, *a):
        return "ok"


def test_extract_sql_variants():
    assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert extract_sql("SQL: SELECT a FROM t; DROP TABLE t") == "SELECT a FROM t"
    assert extract_sql("SELECT a\nFROM t\n\nExplanation: picks a") == "SELECT a\nFROM t"


def test_ddl_only_drops_sample_rows():
    out = ddl_only(SCHEMA)
    assert "sample rows" not in out and "'Europe'" not in out and "CREATE TABLE orders" in out


def test_gate_declines_without_calling_specialist():
    sql = FakeSQL([])
    llm = FineTunedSQLAnalystLLM(sql, FakeGeneral(answerable=False))
    p = asyncio.run(llm.generate_sql("Delete all orders", SCHEMA, []))
    assert not p.answerable and p.sql == "" and sql.prompts == []


def test_self_correction_feeds_error_and_resamples_repeats():
    bad = "SELECT region FROM orders"
    sql = FakeSQL([bad, "SELECT c.region FROM orders o JOIN customers c ON c.customer_id = o.customer_id"])
    llm = FineTunedSQLAnalystLLM(sql, FakeGeneral())
    p = asyncio.run(llm.generate_sql("Orders by region", SCHEMA,
                                     [{"sql": bad, "error": "no such column: region"}]))
    assert "c.region" in p.sql
    first_prompt, temp0 = sql.prompts[0]
    assert "no such column: region" in first_prompt and "customers" in first_prompt and temp0 == 0
    assert sql.prompts[1][1] > 0          # repeated failed query -> resampled
    assert llm.usage["calls"] == 2


class ChattyGeneral(FakeGeneral):
    def __init__(self, text):
        super().__init__()
        self.text = text

    async def _chat(self, system, user, schema, max_tokens, temperature=0):
        return self.text


COLS, ROWS = ["region", "revenue"], [["Europe", 826396.65], ["Asia Pacific", 432266.65]]


def test_summary_kept_when_every_number_is_grounded():
    llm = FineTunedSQLAnalystLLM(FakeSQL([]), ChattyGeneral("Europe leads with 826,396.65."))
    assert asyncio.run(llm.summarize("q", "", COLS, ROWS, None, False)) == "Europe leads with 826,396.65."
    assert llm.summary_fallbacks == 0


def test_summary_falls_back_on_invented_numbers_or_placeholders():
    for bad in ["Europe leads with 900,000.", "Top product is [Product Name]."]:
        llm = FineTunedSQLAnalystLLM(FakeSQL([]), ChattyGeneral(bad))
        out = asyncio.run(llm.summarize("Revenue by region?", "", COLS, ROWS, None, False))
        assert "826,396.65" in out and out != bad and llm.summary_fallbacks == 1


def test_sqlforge_preset_matches_training_format():
    class SQLForge(FakeSQL):
        name = "sqlforge-3b-Q4_K_M"

    llm = FineTunedSQLAnalystLLM(SQLForge([]), FakeGeneral())
    assert llm.prompt_format == "sqlforge" and llm.system.startswith("You are an expert SQLite engineer.")
    p = llm.sql_prompt("Orders by region?", "-- note: region is a column of customers\n-- note: revenue = x\n" + SCHEMA,
                       [{"sql": "SELECT region FROM orders", "error": "no such column: region"}])
    assert p.startswith("### Database schema\nCREATE TABLE customers")
    assert "-- sample rows (customer_id | region):\n-- 1 | Europe" in p
    hint = p.split("### Hint\n")[1].split("### SQL")[0]
    assert hint.startswith("region is a column of customers") and "revenue = x" not in hint
    assert "no such column: region" in hint and "Failed query" in hint
    assert p.endswith("### SQL\n") and "-- note:" not in p and "dialect" not in p


def test_closest_columns():
    from analyst.llm import closest_columns
    tables = {"customers": ["customer_id", "region"], "products": ["category", "unit_price"]}
    assert closest_columns("customer_region", tables) == ["customers.region"]
    assert closest_columns("UnitPrice", tables) == ["products.unit_price"]
    assert closest_columns("zzz", tables) == []


def test_hints_only_include_notes_the_question_mentions():
    from analyst.specialist import relevant_evidence
    notes = ("revenue refers to SUM(x) over completed orders.\nregion and segment are columns of customers.\n"
             "Date columns (sale_date) are ISO text.\nOriginal headers: 'Store' is store; 'Qty' is qty.")
    ev = relevant_evidence("How many customers are in each region?", notes)
    assert "revenue" not in ev and "region and segment" in ev and "ISO" not in ev and "'Store'" not in ev
    ev = relevant_evidence("What is monthly revenue?", notes)
    assert "SUM(x)" in ev and "ISO text" in ev
    assert "100.0 * part" in relevant_evidence("What is the return rate?", notes)
