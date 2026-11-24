"""MCP server exposing the analyst's data tools over stdio.

Tools
-----
- ``list_tables``      table names with row counts
- ``describe_schema``  CREATE statements + 3 sample rows per table
- ``run_query``        execute one read-only SQL query (row-capped)
- ``render_chart``     draw a chart spec over a result set to a PNG

Errors are returned as ``{"ok": false, "error": "..."}`` rather than raised, so
the exact database message (e.g. "no such column: revenue") reaches the agent
and can drive the SQL-retry loop.

Run standalone (e.g. for MCP Inspector / Claude Desktop):

    ANALYST_DB=demo/shop.db python -m analyst.mcp_server
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from analyst.charts import render_chart as _render_chart
from analyst.sql_safety import UnsafeSQLError, check_read_only

DB_PATH = os.environ.get("ANALYST_DB", str(Path(__file__).resolve().parent.parent / "demo" / "shop.db"))
MAX_ROWS = int(os.environ.get("ANALYST_MAX_ROWS", "500"))

mcp = MCPServer(
    "analyst-sql",
    instructions="Read-only SQL access to the analytics database, plus chart rendering.",
)


def _connect() -> sqlite3.Connection:
    if not Path(DB_PATH).exists():
        raise FileNotFoundError(f"Database not found at {DB_PATH}. Run: python demo/seed_db.py")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


@mcp.tool()
def list_tables() -> dict[str, Any]:
    """List the tables in the database with their row counts."""
    try:
        with _connect() as con:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {"ok": True, "tables": {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}}
    except Exception as e:  # noqa: BLE001 - surface the message to the agent
        return {"ok": False, "error": str(e)}


@mcp.tool()
def describe_schema() -> dict[str, Any]:
    """Return the CREATE TABLE statement and three sample rows for every table, plus dataset notes."""
    try:
        with _connect() as con:
            out = []
            for name, ddl in con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                cur = con.execute(f'SELECT * FROM "{name}" LIMIT 3')
                cols = [d[0] for d in cur.description]
                out.append({"table": name, "ddl": ddl.strip(), "columns": cols, "sample_rows": cur.fetchall()})
            notes_file = Path(DB_PATH).with_suffix(".notes.md")
            notes = notes_file.read_text().strip() if notes_file.exists() else ""
            return {"ok": True, "dialect": "sqlite", "tables": out, "notes": notes}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@mcp.tool()
def run_query(sql: str) -> dict[str, Any]:
    """Execute a single read-only SQL (SELECT/WITH) query and return columns and rows."""
    try:
        sql = check_read_only(sql)
        with _connect() as con:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        return {"ok": True, "columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]],
                "row_count": min(len(rows), MAX_ROWS), "truncated": truncated}
    except UnsafeSQLError as e:
        return {"ok": False, "error": f"Rejected by read-only guard: {e}"}
    except sqlite3.Error as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def render_chart(spec: dict[str, Any], columns: list[str], rows: list[list[Any]], out_path: str) -> dict[str, Any]:
    """Render a chart (spec: chart_type bar|hbar|line|none, x, y, series?, title) to a PNG file."""
    try:
        return {"ok": True, "path": _render_chart(spec, columns, rows, out_path)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    mcp.run("stdio")
