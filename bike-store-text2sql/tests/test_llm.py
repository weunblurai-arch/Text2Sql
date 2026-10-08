"""Provider boundary tests; never call an external API or require credentials."""
import io
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from urllib import error

from bikestore.llm import ModelError, PLAN_SCHEMA, generate_plan, validate_plan


def query_plan(**changes):
    plan = {
        "sql": "SELECT store_name, 10 AS revenue FROM stores ORDER BY store_name",
        "title": "Revenue by store",
        "clarification": "",
        "notes": ["Synthetic data; amounts in USD."],
        "chart": {"type": "bar", "x": "store_name", "y": "revenue", "title": "Revenue"},
    }
    plan.update(changes)
    return plan


def envelope(plan=None, **changes):
    body = {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": json.dumps(query_plan() if plan is None else plan)}
        ]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5},
    }
    body.update(changes)
    return body


class LLMTransportTests(unittest.TestCase):
    def generate(self, transport):
        with patch("bikestore.llm.request.urlopen", transport):
            return generate_plan(
                "Revenue by store", [{"role": "user", "content": "Use all sample dates"}],
                {"tables": [{"name": "stores", "columns": ["store_name"]}]},
                {"data_start": "2025-01-01", "data_end": "2025-12-31"},
                "test-key-never-sent", "test-model")

    def from_body(self, body):
        return self.generate(lambda *_args, **_kwargs: io.BytesIO(json.dumps(body).encode()))

    def test_schema_and_history_reach_provider_and_plan_is_parsed(self):
        captured = {}

        def fake_transport(req, **kwargs):
            captured["request"] = req
            captured["timeout"] = kwargs["timeout"]
            return io.BytesIO(json.dumps(envelope()).encode())

        self.assertEqual(self.generate(fake_transport), query_plan())
        req = captured["request"]
        self.assertEqual(req.full_url, "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("X-goog-api-key"), "test-key-never-sent")
        payload = json.loads(req.data)
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(payload["generationConfig"]["responseJsonSchema"], PLAN_SCHEMA)
        context = json.loads(payload["contents"][0]["parts"][0]["text"])
        self.assertEqual(context["question"], "Revenue by store")
        self.assertEqual(context["schema"]["tables"][0]["name"], "stores")
        self.assertEqual(context["history"][0]["content"], "Use all sample dates")
        self.assertEqual(context["sample_period"]["end"], "2025-12-31")
        self.assertNotIn("test-key-never-sent", req.data.decode())
        self.assertLessEqual(captured["timeout"], 40)

    def test_refusal_is_an_actionable_error(self):
        with self.assertRaisesRegex(ModelError, "blocked"):
            self.from_body(envelope(candidates=[]))

    def test_incomplete_response_is_not_executed(self):
        with self.assertRaisesRegex(ModelError, "did not finish"):
            self.from_body(envelope(candidates=[{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]))

    def test_invalid_envelopes_become_model_errors(self):
        invalid = [None, [], {}, envelope(candidates=None), envelope(candidates=[]),
                   envelope(candidates=[{"finishReason": "STOP", "content": None}]),
                   envelope(candidates=[{"finishReason": "STOP", "content": 42}]),
                   envelope(candidates=[{"finishReason": "STOP", "content": {"parts": [
                       {"text": "{truncated"}]}}])]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ModelError):
                self.from_body(body)

    def test_unreadable_and_oversized_provider_response(self):
        for raw in (b"not JSON", b"\xff", b"x" * 1_000_001):
            with self.subTest(size=len(raw)), self.assertRaises(ModelError):
                self.generate(lambda *_args, **_kwargs: io.BytesIO(raw))

    def test_provider_failures_do_not_expose_response_or_credentials(self):
        cases = [
            (401, "API key"), (403, "permissions"), (404, "model"),
            (429, "limits"), (503, "retry"),
        ]
        for status, expected in cases:
            def fail(*_args, **_kwargs):
                raise error.HTTPError("https://example.invalid", status, "SECRET_DETAIL", {},
                                      io.BytesIO(b"PRIVATE_PROVIDER_BODY"))
            with self.subTest(status=status), self.assertRaises(ModelError) as raised:
                self.generate(fail)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("SECRET_DETAIL", str(raised.exception))
            self.assertNotIn("PRIVATE_PROVIDER_BODY", str(raised.exception))

    def test_network_timeout_becomes_actionable_error(self):
        for exc in (TimeoutError("private details"), error.URLError("private details")):
            def fail(*_args, **_kwargs):
                raise exc
            with self.subTest(error=type(exc).__name__), self.assertRaisesRegex(ModelError, "connection"):
                self.generate(fail)


class PlanValidationTests(unittest.TestCase):
    def test_clarification_is_returned_without_attempting_sql(self):
        with self.assertRaisesRegex(ModelError, "Which store"):
            validate_plan(query_plan(sql="", clarification="Which store should I use?"))

    def test_invalid_plans_are_rejected_at_provider_boundary(self):
        invalid = [None, [], {}, query_plan(sql=None), query_plan(sql=""),
                   query_plan(title=42), query_plan(notes="not a list"),
                   query_plan(notes=[42]), query_plan(notes=["n"] * 13),
                   query_plan(chart={"type": "pie", "x": "a", "y": "b", "title": "c"}),
                   query_plan(chart={"type": [], "x": "a", "y": "b", "title": "c"}),
                   query_plan(chart={"type": {}, "x": "a", "y": "b", "title": "c"}),
                   query_plan(chart={"type": "bar", "x": "store", "y": None, "title": "c"})]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ModelError):
                validate_plan(plan)

    def test_valid_no_chart_plan_is_accepted(self):
        plan = query_plan(chart={"type": "none", "x": "", "y": "", "title": ""})
        self.assertEqual(validate_plan(deepcopy(plan)), plan)


if __name__ == "__main__":
    unittest.main()
