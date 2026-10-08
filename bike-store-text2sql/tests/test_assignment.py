"""Assignment data, ground-truth comparison, and Streamlit smoke tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bikestore.assignment import EXAMPLES, TABLES, ask, context, ensure_database, run_sql
from bikestore.sql import QueryError
from evaluate import evaluate, rows_match


class AssignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = ensure_database(path=Path(cls.temp.name) / "assignment.db")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_nine_csvs_import_with_real_counts_and_constraints(self):
        schema, overview = context(self.path)
        counts = {t["name"]: t["row_count"] for t in schema["tables"]}
        self.assertEqual(set(counts), set(TABLES))
        self.assertEqual(counts["staffs"], 10)
        self.assertEqual(counts["orders"], 1615)
        self.assertEqual(counts["order_items"], 4722)
        self.assertEqual(overview["data_start"], "2016-01-01")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertGreater(db.execute("SELECT COUNT(*) FROM orders WHERE shipped_date IS NULL").fetchone()[0], 0)

    def test_three_reference_examples_match_supplied_answers(self):
        report = evaluate(EXAMPLES, self.path, reference_only=True)
        self.assertEqual(report["metrics"]["reference_fixture_match_rate"], 1.0)
        self.assertIsNone(report["metrics"]["execution_accuracy"])
        self.assertIsNone(report["metrics"]["average_response_seconds"])

    def test_readonly_execution_supports_staffs_but_rejects_metadata_and_writes(self):
        self.assertEqual(run_sql("SELECT COUNT(*) AS n FROM staffs", self.path)["rows"], [{"n": 10}])
        for sql in ("DELETE FROM orders", "SELECT * FROM _dataset_meta", "SELECT * FROM sqlite_master", "SELECT load_extension('x')"):
            with self.subTest(sql=sql), self.assertRaises(QueryError):
                run_sql(sql, self.path)

    def test_example_mode_uses_reference_sql_and_never_calls_model(self):
        with patch("bikestore.assignment.generate_plan") as model:
            answer = ask(EXAMPLES[0]["question"], self.path, examples_only=True)
            self.assertEqual(answer["answer"], "There are a total of 3 stores.")
            self.assertEqual(answer["mode"], "example")
            self.assertEqual(answer["estimated_cost_usd"], 0)
            model.assert_not_called()
        with self.assertRaises(QueryError):
            ask("How many stores are in California?", self.path, examples_only=True)

    def test_sql_bounds_ctes_and_non_json_values(self):
        self.assertEqual(run_sql("WITH n AS (SELECT COUNT(*) AS n FROM stores) SELECT n FROM n", self.path)["rows"], [{"n": 3}])
        self.assertTrue(run_sql("SELECT * FROM customers", self.path)["truncated"])
        for sql in ("SELECT 1; DELETE FROM stores", "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
                    "SELECT 1e999", "SELECT x'ff'", "SELECT randomblob(100000000)"):
            with self.subTest(sql=sql), self.assertRaises(QueryError):
                run_sql(sql, self.path)

    def test_model_pipeline_is_pinned_to_required_model_and_real_schema(self):
        plan = {"sql": EXAMPLES[0]["sql"], "title": "Store count", "chart": EXAMPLES[0]["chart"], "notes": [],
                "usage": {"input_tokens": 2000, "output_tokens": 300}}
        with patch("bikestore.assignment.break_question", return_value=([EXAMPLES[0]["question"]], {})), \
             patch("bikestore.assignment.select_tables", side_effect=lambda _q, schema, _key, _model: (schema, {})), \
             patch("bikestore.assignment.generate_plan", return_value=plan) as model:
            answer = ask(EXAMPLES[0]["question"], self.path, "fake-key")
            self.assertEqual(model.call_args.args[5], "gemini-3.5-flash-lite")
            self.assertIn("staffs", [t["name"] for t in model.call_args.args[2]["tables"]])
            self.assertEqual(answer["rows"], [{"total_stores": 3}])
            self.assertAlmostEqual(answer["estimated_cost_usd"], 0.00135)

    def test_evaluation_compares_results_instead_of_sql_text(self):
        equivalent = {"sql": "SELECT SUM(1) AS differently_named_count FROM stores", "title": "Stores",
                      "chart": EXAMPLES[0]["chart"], "notes": [], "usage": {}}
        with patch("bikestore.assignment.break_question", return_value=([EXAMPLES[0]["question"]], {})), \
             patch("bikestore.assignment.select_tables", side_effect=lambda _q, schema, _key, _model: (schema, {})), \
             patch("bikestore.assignment.generate_plan", return_value=equivalent):
            report = evaluate(EXAMPLES[:1], self.path, "fake-key")
        self.assertEqual(report["metrics"]["execution_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["sql_execution_success_rate"], 1.0)

    def test_failed_sql_is_repaired_once_with_full_schema(self):
        bad = {"sql": "SELECT missing_column FROM stores", "title": "Stores",
               "chart": EXAMPLES[0]["chart"], "notes": [], "usage": {"input_tokens": 10, "output_tokens": 5}}
        good = {**bad, "sql": EXAMPLES[0]["sql"]}
        with patch("bikestore.assignment.break_question", return_value=([EXAMPLES[0]["question"]], {})), \
             patch("bikestore.assignment.select_tables", side_effect=lambda _q, schema, _key, _model: (schema, {})), \
             patch("bikestore.assignment.generate_plan", side_effect=[bad, good]) as model:
            answer = ask(EXAMPLES[0]["question"], self.path, "fake-key")
        self.assertEqual(answer["rows"], [{"total_stores": 3}])
        self.assertEqual(answer["corrections"], 1)
        self.assertEqual(model.call_count, 2)
        self.assertIn("no such column", model.call_args.kwargs["repair"]["error"])
        self.assertEqual(answer["usage"]["input_tokens"], 20)

    def test_unsafe_sql_remains_blocked_after_one_correction(self):
        bad = {"sql": "DELETE FROM stores", "title": "Stores", "chart": EXAMPLES[0]["chart"],
               "notes": [], "usage": {}}
        worse = {**bad, "sql": "DROP TABLE stores"}
        with patch("bikestore.assignment.break_question", return_value=([EXAMPLES[0]["question"]], {})), \
             patch("bikestore.assignment.select_tables", side_effect=lambda _q, schema, _key, _model: (schema, {})), \
             patch("bikestore.assignment.generate_plan", side_effect=[bad, worse]) as model:
            with self.assertRaises(QueryError):
                ask(EXAMPLES[0]["question"], self.path, "fake-key")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(run_sql("SELECT COUNT(*) AS n FROM stores", self.path)["rows"], [{"n": 3}])

    def test_compound_question_runs_each_subquestion(self):
        questions = ["How many stores?", "Which brands exist?"]
        plans = [{"sql": EXAMPLES[0]["sql"], "title": "Stores", "chart": EXAMPLES[0]["chart"],
                  "notes": [], "usage": {}},
                 {"sql": EXAMPLES[1]["sql"], "title": "Brands", "chart": EXAMPLES[1]["chart"],
                  "notes": [], "usage": {}}]
        with patch("bikestore.assignment.break_question", return_value=(questions, {})), \
             patch("bikestore.assignment.select_tables", side_effect=lambda _q, schema, _key, _model: (schema, {})), \
             patch("bikestore.assignment.generate_plan", side_effect=plans):
            answer = ask("How many stores and which brands?", self.path, "fake-key")
        self.assertEqual(len(answer["subresults"]), 2)
        self.assertIn("There are a total of 3 stores", answer["answer"])
        self.assertIn("brand", answer["answer"].lower())

    def test_result_comparison_preserves_duplicates_and_optional_order(self):
        self.assertTrue(rows_match([[1], [2]], [[2], [1]]))
        self.assertFalse(rows_match([[1], [2]], [[2], [1]], ordered=True))
        self.assertFalse(rows_match([[1], [1]], [[1], [2]]))
        self.assertTrue(rows_match([[103199.65599999999]], [[103199.6560]]))
        self.assertFalse(rows_match([[100.0]], [[100.1]]))


class StreamlitUITests(unittest.TestCase):
    def test_example_buttons_and_freeform_error_without_api_calls(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file("streamlit_app.py").run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        app.sidebar.radio[0].set_value("Examples (no API calls)").run()
        app.button(key="example_1").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.dataframe[0].value.iloc[0, 0], 3)
        self.assertIn("There are a total of 3 stores.", [m.value for m in app.markdown])
        app.button(key="example_3").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.dataframe[-1].value), 3)
        app.chat_input[0].set_value("Invent next year's profits").run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Example mode only" in e.value for e in app.error))


if __name__ == "__main__":
    unittest.main()
