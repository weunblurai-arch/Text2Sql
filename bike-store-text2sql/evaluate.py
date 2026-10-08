"""Evaluate generated SQL by executing it and comparing with ground-truth results."""
import argparse
import csv
import json
import math
import os
import statistics
import time
from pathlib import Path

from bikestore.assignment import DB_PATH, ROOT, ask, ensure_database, run_sql
from bikestore.llm import load_env


def rows_match(actual, expected, ordered=False):
    """Compare rows, preserving duplicates; ignore SQL aliases, not column order."""
    def cell_match(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if isinstance(a, int) and isinstance(b, int):
                return a == b
            return math.isclose(a, b, rel_tol=0, abs_tol=0.01)
        return a == b
    def row_match(a, b):
        return len(a) == len(b) and all(cell_match(x, y) for x, y in zip(a, b))
    if len(actual) != len(expected):
        return False
    if ordered:
        return all(row_match(a, b) for a, b in zip(actual, expected))
    unmatched = list(expected)
    for row in actual:
        match = next((i for i, other in enumerate(unmatched) if row_match(row, other)), None)
        if match is None:
            return False
        unmatched.pop(match)
    return True


def matrix(result):
    return [[row[column] for column in result["columns"]] for row in result["rows"]]


def load_cases(path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            cases = list(csv.DictReader(handle))
        for case in cases:
            case["order_sensitive"] = str(case.get("order_sensitive", "false")).lower() == "true"
            if case.get("expected_rows"):
                case["expected_rows"] = json.loads(case["expected_rows"])
            else:
                case.pop("expected_rows", None)
    else:
        cases = json.loads(path.read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError("Ground truth must contain at least one test case.")
    for case in cases:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k].strip() for k in ("question", "sql")):
            raise ValueError("Every case needs a question and a ground-truth sql string.")
        if "expected_rows" in case and (not isinstance(case["expected_rows"], list)
                or not all(isinstance(row, list) for row in case["expected_rows"])):
            raise ValueError("expected_rows must be an array of row arrays.")
    return cases


def evaluate(cases, path=DB_PATH, api_key="", reference_only=False):
    records = []
    for case in cases:
        started = time.perf_counter()
        record = {"id": case.get("id", str(len(records) + 1)), "question": case["question"],
                  "reference_sql": case["sql"], "generated_sql": None,
                  "execution_ok": False, "result_match": False}
        try:
            reference = run_sql(case["sql"], path, max_rows=1000)
            if reference["truncated"]:
                raise ValueError("Ground truth exceeded 1,000 rows; this case cannot be compared completely.")
            expected = case.get("expected_rows", matrix(reference))
            ordered = case.get("order_sensitive", False)
            if not rows_match(matrix(reference), expected, ordered):
                raise ValueError("Ground-truth SQL does not match supplied expected rows on this dataset.")
            if reference_only:
                record.update(rows=reference["rows"], columns=reference["columns"],
                              execution_ok=True, result_match=True,
                              response_seconds=round(time.perf_counter() - started, 6))
            else:
                result = ask(case["question"], path, api_key, max_rows=1000)
                record.update(generated_sql=result["sql"], rows=result["rows"], columns=result["columns"],
                              answer=result["answer"], usage=result["usage"],
                              estimated_cost_usd=result["estimated_cost_usd"],
                              response_seconds=result["response_seconds"], execution_ok=True,
                              result_match=not result["truncated"] and rows_match(matrix(result), expected, ordered))
        except (ValueError, OSError) as exc:
            record.update(error=str(exc), response_seconds=round(time.perf_counter() - started, 6))
        records.append(record)
    times = [r["response_seconds"] for r in records]
    successes = sum(r["execution_ok"] for r in records)
    matches = sum(r["result_match"] for r in records)
    return {
        "mode": "reference_only" if reference_only else (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"),
        "dataset": "Kaggle bike-store-sample-database",
        "case_count": len(records),
        "metrics": {
            "execution_accuracy": None if reference_only else matches / len(records),
            "sql_execution_success_rate": None if reference_only else successes / len(records),
            "average_response_seconds": None if reference_only else statistics.mean(times),
            "p95_response_seconds": None if reference_only else sorted(times)[math.ceil(.95 * len(times)) - 1],
            "responses_under_60_seconds_rate": None if reference_only else sum(t < 60 for t in times) / len(times),
            "reference_fixture_match_rate": matches / len(records) if reference_only else None,
            "average_reference_execution_seconds": statistics.mean(times) if reference_only else None,
            "estimated_api_cost_usd": sum(r.get("estimated_cost_usd", 0) for r in records),
        },
        "notes": ["Reference-only results are not LLM accuracy or LLM latency measurements.",
                  "Result comparison ignores aliases; column order must match; row order follows order_sensitive.",
                  "Duplicates are preserved; integer values match exactly; decimal absolute tolerance is $0.01.",
                  "Natural-language answers are generated deterministically from executed rows; no separate semantic answer score is claimed.",
                  "Cost includes returned successful-call usage only; failed provider calls may also incur charges."],
        "cases": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(ROOT / "validation/examples.json"))
    parser.add_argument("--out", default=str(ROOT / "validation/results.json"))
    parser.add_argument("--reference-only", action="store_true", help="Verify fixture data without any API call; does not measure LLM accuracy.")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not args.reference_only and not key:
        parser.error("Set GEMINI_API_KEY in .env, or use --reference-only to verify the example dataset.")
    cases = load_cases(args.cases)
    # Keep accidental experimentation batches small; split larger UC10 sets explicitly.
    if not args.reference_only and len(cases) > 100:
        parser.error("Use at most 100 questions per run and check API usage against the $2 experiment budget.")
    report = evaluate(cases, ensure_database(), key, args.reference_only)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"mode": report["mode"], "case_count": report["case_count"], **report["metrics"]}, indent=2))
    print("Results saved to {}".format(output))


if __name__ == "__main__":
    main()
