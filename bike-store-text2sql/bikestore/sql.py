"""Validate and execute a single read-only SQLite query."""
import math
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

QUERY_TIMEOUT_SECONDS = 2.0
MAX_RESULT_ROWS = 1000

class QueryError(ValueError):
    """SQL cannot be safely or successfully executed."""


def _read_connection(path):
    encoded_path = quote(str(Path(path).resolve()), safe="/")
    connection = sqlite3.connect("file:{}?mode=ro".format(encoded_path), uri=True, timeout=1)
    connection.execute("PRAGMA query_only = ON")
    return connection


ALLOWED_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "date", "datetime", "dense_rank",
    "first_value", "glob", "ifnull", "instr", "julianday",
    "lag", "last_value", "lead", "length", "like", "lower", "ltrim", "max", "min", "nth_value",
    "ntile", "nullif", "percent_rank", "rank", "round", "row_number", "rtrim",
    "strftime", "substr", "substring", "sum", "time", "total", "trim", "typeof", "unicode", "upper",
})


def _authorize(action, arg1, arg2, database_name, source, tables=None):
    if action == sqlite3.SQLITE_READ:
        # SQLite 3.32 may report no database/column for COUNT(*) and rowid scans.
        known_database = database_name == "main" or (database_name is None and arg2 == "")
        allowed = () if tables is None else tables
        return sqlite3.SQLITE_OK if known_database and arg1 in allowed else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
    # Recursive expressions can allocate enormous values before a time callback.
    # Ordinary WITH clauses remain supported; this BI demo does not need recursion.
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _first_keyword(sql):
    remaining = sql.lstrip()
    while remaining.startswith(("--", "/*")):
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            remaining = remaining[newline + 1:].lstrip() if newline >= 0 else ""
        else:
            end = remaining.find("*/", 2)
            if end < 0:
                return ""
            remaining = remaining[end + 2:].lstrip()
    match = re.match(r"[A-Za-z]+", remaining)
    return match.group(0).upper() if match else ""


def execute_sql(sql, path, max_rows=200, allowed_tables=()):
    """Run one SELECT/CTE against known tables, with bounded work and output."""
    if not isinstance(sql, str) or not sql.strip():
        raise QueryError("Enter a SELECT query to read the demo database.")
    if len(sql) > 20000:
        raise QueryError("The SQL query is too long (maximum 20,000 characters).")
    if _first_keyword(sql) not in ("SELECT", "WITH"):
        raise QueryError("Only a single read-only SELECT query or WITH query is allowed.")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        raise QueryError("The row limit must be a positive integer.")
    limit = min(max_rows, MAX_RESULT_ROWS)
    connection = None
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    try:
        connection = _read_connection(path)
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 50)
        connection.set_authorizer(lambda *args: _authorize(*args, tables=allowed_tables))
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        cursor = connection.execute(sql)
        if not cursor.description:
            raise QueryError("The query must return a result table.")
        columns = []
        for column in cursor.description:
            candidate = column[0]
            suffix = 2
            while candidate in columns:
                candidate = "{}_{}".format(column[0], suffix)
                suffix += 1
            columns.append(candidate)
        values = cursor.fetchmany(limit + 1)
        truncated = len(values) > limit
        rows = []
        for row in values[:limit]:
            converted = []
            for value in row:
                if isinstance(value, float) and not math.isfinite(value):
                    raise QueryError("The query returned a non-finite number. Adjust the calculation and try again.")
                if isinstance(value, bytes):
                    raise QueryError("The query returned binary data. Select text or numeric columns instead.")
                converted.append(value)
            rows.append(dict(zip(columns, converted)))
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
    except (sqlite3.Error, sqlite3.Warning, OverflowError) as error:
        message = str(error)
        if "interrupt" in message.lower():
            raise QueryError("This query exceeded the two-second work limit. Try a narrower question.") from error
        if "not authorized" in message.lower() or "prohibited" in message.lower():
            raise QueryError("This query is not allowed. Read only the bike-store tables using safe SQL functions.") from error
        if "one statement" in message.lower():
            raise QueryError("Only one SELECT statement may be run at a time.") from error
        raise QueryError("The SQL query could not be run: {}".format(message)) from error
    finally:
        if connection is not None:
            connection.close()
