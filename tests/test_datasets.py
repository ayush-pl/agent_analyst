"""CSV upload: import cleaning, overview, suggested questions, and the agent on the new table."""

import asyncio
import sqlite3

from analyst.agent import DataAnalyst
from analyst.datasets import import_csv, notes_path, profile, suggest_questions
from analyst.llm import MockAnalystLLM, SQLProposal

MESSY = (
    "Order Date;Store Name;Units Sold;Unit Price ($);Margin\n"
    "03/01/2026;North;3;$1,200.50;12%\n"
    "03/15/2026;South;1;$80.00;8%\n"
    "04/02/2026;North;2;(15.00);10%\n"
    "04/20/2026;East;5;$9.99;\n"
)


def _load(tmp_path, text=MESSY, **kw):
    src = tmp_path / "Q1 Sales Report.csv"
    src.write_text(text, encoding="cp1252")
    return import_csv(src, tmp_path / "up.db", **kw)


def test_import_cleans_headers_numbers_and_dates(tmp_path):
    info = _load(tmp_path, user_notes="revenue = units_sold * unit_price")
    assert info.table == "q1_sales_report" and info.rows == 4
    assert info.renamed["Unit Price ($)"] == "unit_price"
    assert info.columns == {"order_date": "date", "store_name": "text", "units_sold": "number",
                            "unit_price": "number", "margin": "number"}
    con = sqlite3.connect(info.db_path)
    rows = con.execute("SELECT order_date, unit_price, margin FROM q1_sales_report ORDER BY rowid").fetchall()
    assert rows[0] == ("2026-03-01", 1200.5, 12.0)
    assert rows[2][1] == -15.0 and rows[3][2] is None          # accounting negative, missing value
    notes = notes_path(info.db_path).read_text()
    assert "strftime" in notes and "'Unit Price ($)' is unit_price" in notes and "units_sold * unit_price" in notes
    assert "12.5 means 12.5%" in notes


def test_profile_and_suggestions(tmp_path):
    info = _load(tmp_path)
    md = profile(info.db_path, info.table)
    assert "4 rows × 5 columns" in md and "2026-03-01 → 2026-04-20" in md
    assert "**North** has the largest total `units_sold`" in md
    qs = suggest_questions(info.db_path, info.table)
    assert qs[0] == "What is the total units sold by store name?"
    assert any("by month" in q for q in qs)


class FixedSQL(MockAnalystLLM):
    def __init__(self, sql):
        self.sql, self.seen_schema = sql, ""

    async def generate_sql(self, question, schema, attempts):
        self.seen_schema = schema
        return SQLProposal(sql=self.sql, rationale="fixed")


def test_agent_answers_from_uploaded_csv(tmp_path):
    info = _load(tmp_path, user_notes="revenue = units_sold * unit_price")
    llm = FixedSQL("SELECT store_name, SUM(units_sold * unit_price) AS revenue FROM q1_sales_report "
                   "GROUP BY store_name ORDER BY revenue DESC")

    async def go():
        async with DataAnalyst(db_path=info.db_path, llm=llm, require_approval=False,
                               output_dir=str(tmp_path / "out")) as a:
            return await a.ask("What is revenue by store?")

    st = asyncio.run(go()).state
    assert st["status"] == "done"
    assert st["result"]["rows"][0] == ["North", 3571.5]
    assert "-- note: User definitions: revenue = units_sold * unit_price" in llm.seen_schema
    assert "North" in st["summary"] and st["chart_path"]


def test_whole_number_percentages_are_real(tmp_path):
    info = _load(tmp_path, text="item,discount %\na,10%\nb,0%\n")
    con = sqlite3.connect(info.db_path)
    assert con.execute(f"SELECT discount / 100 FROM {info.table} WHERE item = 'a'").fetchone()[0] == 0.1
