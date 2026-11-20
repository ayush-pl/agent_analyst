"""Read-only guard for generated SQL.

The database connection is also opened read-only, so this is defence in depth:
it rejects anything that is not a single SELECT / WITH statement before it ever
reaches the engine, and gives the SQL model a clear error to retry on.
"""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|"
    r"pragma|vacuum|reindex|grant|revoke)\b",
    re.IGNORECASE,
)


class UnsafeSQLError(ValueError):
    """Raised when generated SQL is not a single read-only query."""


def _strip_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # blank out string literals so keywords inside them don't trip the check
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def check_read_only(sql: str) -> str:
    """Return the cleaned SQL if it is a single read-only query, else raise."""
    if not sql or not sql.strip():
        raise UnsafeSQLError("Empty SQL statement.")
    cleaned = sql.strip().rstrip(";").strip()
    bare = _strip_comments_and_strings(cleaned)
    if ";" in bare:
        raise UnsafeSQLError("Only a single SQL statement is allowed.")
    first = bare.strip().split(None, 1)[0].lower() if bare.strip() else ""
    if first not in ("select", "with"):
        raise UnsafeSQLError(f"Only SELECT/WITH queries are allowed (got '{first.upper()}').")
    m = _FORBIDDEN.search(bare)
    if m:
        raise UnsafeSQLError(f"Write/DDL keyword '{m.group(1).upper()}' is not allowed.")
    return cleaned
