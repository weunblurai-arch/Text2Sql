"""The assignment pipeline: CSV → SQLite; question → SQL → results."""
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .sql import QueryError, execute_sql
from .answers import summarize
from .llm import DEFAULT_MODEL, ModelError, break_question, generate_plan, select_tables

ROOT = Path(__file__).resolve().parent.parent
CSV_DIR = ROOT / "data" / "csv"
DB_PATH = ROOT / "data" / "assignment.db"
TABLES = ("brands", "categories", "stores", "customers", "staffs", "products", "orders", "order_items", "stocks")
EXAMPLES = json.loads((ROOT / "validation" / "examples.json").read_text())
BUSINESS_RULES = """Bike Store assignment dataset from the cited Kaggle source.
Status codes: 1 Pending, 2 Processing, 3 Rejected, 4 Completed. Include ALL statuses
unless the question explicitly requests a status; the assignment's reference SQL does this.
Revenue is SUM(quantity * order_items.list_price * (1-discount)), in USD.
Do not round result values unless requested; display formatting is separate.
Best-selling means highest revenue unless the question specifically requests units/quantity.
'Products from California' refers to customers.state = 'CA', as in the assignment example.
Explicit store-location questions use stores.state; customer locations use customers.state.
Other state names map to two-letter US abbreviations. Only use available schema values.
staffs is the employee table (plural). NULL shipped_date means not yet shipped.
Dates cover the loaded dataset; don't assume a year or use the last date as today's date.
Stock is a current snapshot; low stock is <10 units per store and product.
Count DISTINCT orders after an item join. Use historical item prices for revenue.
No cost/profit/tax/returns data exists. Never invent missing data or ignore requested filters.
"""


def ensure_database(csv_dir=CSV_DIR, path=DB_PATH):
    """Import all nine CSVs atomically; preserve types, constraints, and NULLs."""
    csv_dir, path = Path(csv_dir), Path(path)
    missing = [name + ".csv" for name in TABLES if not (csv_dir / (name + ".csv")).is_file()]
    if missing:
        raise ValueError("Missing CSV files in {}: {}".format(csv_dir, ", ".join(missing)))
    digest = hashlib.sha256((ROOT / "data/schema.sql").read_bytes())
    for name in TABLES:
        digest.update((csv_dir / (name + ".csv")).read_bytes())
    fingerprint = digest.hexdigest()
    if path.is_file():
        with sqlite3.connect(str(path)) as db:
            try:
                if db.execute("SELECT fingerprint FROM _dataset_meta").fetchone() == (fingerprint,):
                    return path
            except sqlite3.Error:
                pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(suffix=".db", dir=str(path.parent))
    os.close(fd)
    try:
        with sqlite3.connect(temp) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript((ROOT / "data/schema.sql").read_text())
            db.execute("BEGIN")
            db.execute("PRAGMA defer_foreign_keys=ON")
            for name in TABLES:
                columns = db.execute('PRAGMA table_info("{}")'.format(name)).fetchall()
                fields = [col[1] for col in columns]
                with (csv_dir / (name + ".csv")).open(newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames != fields:
                        raise ValueError("{} columns must be {}".format(name + ".csv", ", ".join(fields)))
                    rows = []
                    for record in reader:
                        values = []
                        for col in columns:
                            raw = record[col[1]]
                            if raw is None or raw.strip().upper() in ("", "NULL"):
                                value = None
                            elif col[2] == "INTEGER":
                                value = int(raw)
                            elif col[2] == "REAL":
                                value = float(raw)
                            else:
                                value = raw
                            values.append(value)
                        rows.append(values)
                db.executemany('INSERT INTO "{}" VALUES ({})'.format(name, ",".join("?" for _ in fields)), rows)
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("CSV relationships are invalid. Check foreign key IDs.")
            db.execute("CREATE TABLE _dataset_meta (fingerprint TEXT NOT NULL)")
            db.execute("INSERT INTO _dataset_meta VALUES (?)", (fingerprint,))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path


def context(path=DB_PATH):
    tables = []
    with sqlite3.connect("file:{}?mode=ro".format(Path(path).resolve().as_posix()), uri=True) as db:
        for name in TABLES:
            columns = [{"name": c[1], "type": c[2], "primary_key": bool(c[5]), "nullable": not bool(c[3] or c[5])}
                       for c in db.execute('PRAGMA table_info("{}")'.format(name))]
            foreign_keys = [{"column": c[3], "table": c[2], "to": c[4]}
                            for c in db.execute('PRAGMA foreign_key_list("{}")'.format(name))]
            tables.append({"name": name, "columns": columns, "foreign_keys": foreign_keys,
                           "row_count": db.execute('SELECT COUNT(*) FROM "{}"'.format(name)).fetchone()[0]})
        start, end = db.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone()
        hints = {"states": [r[0] for r in db.execute("SELECT DISTINCT state FROM customers ORDER BY state")],
                 "categories": [r[0] for r in db.execute("SELECT category_name FROM categories")],
                 "brands": [r[0] for r in db.execute("SELECT brand_name FROM brands")],
                 "stores": [r[0] for r in db.execute("SELECT store_name FROM stores")]}
    return {"tables": tables, "business_definitions": BUSINESS_RULES, "value_hints": hints}, {"data_start": start, "data_end": end}


def run_sql(sql, path=DB_PATH, max_rows=200):
    return execute_sql(sql, path, max_rows=max_rows, allowed_tables=TABLES)


def usage_cost(usage, model=DEFAULT_MODEL):
    # Standard paid-tier Gemini text prices per million tokens; free tier costs $0.
    rates = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50)}
    input_rate, output_rate = rates[model]
    return (usage.get("input_tokens", 0) * input_rate + usage.get("output_tokens", 0) * output_rate) / 1_000_000


def ask(question, path=DB_PATH, api_key="", history=None, examples_only=False, max_rows=200):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
        raise QueryError("Enter a question of 1–2,000 characters.")
    started = time.perf_counter()
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not examples_only and model not in {"gemini-3.8-flash", "gemini-3.5-flash-lite"}:
        raise ModelError("GEMINI_MODEL must be gemini-3.8-flash or gemini-3.5-flash-lite.")
    usage = {"input_tokens": 0, "output_tokens": 0}

    def add_usage(tokens):
        for key in usage:
            usage[key] += int((tokens or {}).get(key, 0) or 0)

    if examples_only:
        normalize = lambda s: s.strip().rstrip(".?!").lower()
        example = next((x for x in EXAMPLES if normalize(x["question"]) == normalize(question)), None)
        if example is None:
            raise QueryError("Example mode only runs the three provided questions. Add GEMINI_API_KEY and select Gemini for other questions.")
        questions = [question]
    else:
        if not api_key:
            raise ModelError("Set GEMINI_API_KEY in .env, then choose Gemini.")
        schema, overview = context(path)
        questions, tokens = break_question(question, history or [], schema, api_key, model)
        add_usage(tokens)

    subresults = []
    for subquestion in questions:
        if examples_only:
            plan = {"sql": example["sql"], "title": subquestion, "chart": example["chart"], "notes": [], "usage": {}}
            selected = []
        else:
            selected_schema, tokens = select_tables(subquestion, schema, api_key, model)
            selected = [table["name"] for table in selected_schema["tables"]]
            add_usage(tokens)
            plan = generate_plan(subquestion, history or [], selected_schema, overview,
                                 api_key, model, include_usage=True)
            add_usage(plan.get("usage"))
        corrections = 0
        try:
            result = run_sql(plan["sql"], path, max_rows=max_rows)
        except QueryError as first_error:
            if examples_only:
                raise
            # A failed execution goes to the SQL-corrector agent once. Every attempt
            # still passes through the same read-only, bounded executor.
            corrections = 1
            repaired = generate_plan(subquestion, history or [], schema, overview, api_key,
                                     model, include_usage=True,
                                     repair={"sql": plan["sql"], "error": str(first_error)[:500]})
            add_usage(repaired.get("usage"))
            if repaired["sql"].strip() == plan["sql"].strip():
                raise QueryError("SQL correction returned the same failing query. Try a more specific question.")
            plan = repaired
            result = run_sql(plan["sql"], path, max_rows=max_rows)
        answer = summarize(plan, result)
        if result["columns"] == ["total_stores"] and len(result["rows"]) == 1:
            answer = "There are a total of {} stores.".format(result["rows"][0]["total_stores"])
        elif result["columns"] == ["brand_name"]:
            answer = "The catalog contains these brands: {}.".format(
                ", ".join(str(row["brand_name"]) for row in result["rows"])) if result["rows"] else "No brands matched this query."
        subresults.append(dict(result, question=subquestion, sql=plan["sql"], answer=answer,
                               chart=plan["chart"], notes=plan["notes"], selected_tables=selected,
                               corrections=corrections, title=plan["title"]))

    # Final summary and suggested-question agents use executed values only.
    final_answer = "\n\n".join((item["title"] + ": " if len(subresults) > 1 else "") + item["answer"]
                             for item in subresults)
    suggestions = [item["question"] for item in EXAMPLES if item["question"].strip().lower() != question.strip().lower()][:2]
    return dict(subresults[0], question=question, answer=final_answer,
                subresults=subresults, suggestions=suggestions,
                response_seconds=round(time.perf_counter() - started, 4),
                usage=usage, estimated_cost_usd=usage_cost(usage, model),
                mode="example" if examples_only else model)
