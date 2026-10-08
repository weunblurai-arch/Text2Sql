# Bike Store Text-to-SQL Chatbot

A simple solution for the supplied statement: **Streamlit + SQLite + gpt-4o-mini**.

Ask a question, see the SQL, and get an answer, table, and applicable graph. The workflow uses focused guardrail/question-breaker, table-selection, SQL-generation, and SQL-correction calls. Python validates and executes SQL, then builds summaries, charts, and follow-up suggestions from the results. No multi-agent framework or vector database is required.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 -m streamlit run streamlit_app.py
```

The nine CSVs from the [assignment's Kaggle dataset](https://www.kaggle.com/datasets/dillonmyrick/bike-store-sample-database/data) are included in `data/csv/`. SQLite is created automatically. The data contains 3 stores, 9 brands, 321 products, 1,445 customers, and 1,615 orders.

Without a key, **Examples (no API calls)** runs the three reference queries. This checks the UI and dataset; it is **not LLM generation**.

## Enable the LLM

Copy `.env.example` to `.env` and add your own key:

```dotenv
OPENAI_API_KEY=your_key_here
```

Choose **GPT-4o mini** in the sidebar. The app always uses `gpt-4o-mini`, even if a different model is configured in the environment. For Streamlit Cloud, set the key in app secrets and use `streamlit_app.py` as the entry point. Keep keys out of source control.

Questions, recent chat context, and schema metadata are sent to OpenAI. One request can be split into at most three subquestions. Each generated query is executed through the read-only validator. If execution fails, the SQL corrector gets the error and full schema for one retry. Repeated failures are shown to the user. SQL results are summarized locally.

## Files

| File | Purpose |
| --- | --- |
| `streamlit_app.py` | Chat UI, SQL, tables, charts, CSV download |
| `bikestore/assignment.py` | CSV import and question-to-result workflow |
| `bikestore/llm.py` | Schema-aware GPT-4o mini prompt and API call |
| `bikestore/sql.py` | Read-only SQL validation and execution |
| `bikestore/answers.py` | Factual answer summaries |
| `data/schema.sql` | Nine tables and constraints |
| `evaluate.py` | Ground-truth evaluation and latency reporting |
| `validation/examples.json` | Supplied questions, SQL, and expected answers |
| `validation/example_results.json` | Executed reference-query results |
| `SUBMISSION.md` | Architecture, design decisions, metrics, and costs |

## Evaluate

Check the provided examples without API calls:

```bash
python3 evaluate.py --reference-only --out validation/example_results.json
```

Evaluate actual model-generated SQL after setting your key:

```bash
python3 evaluate.py --cases validation/examples.json --out validation/llm_results.json
```

Use the same command with the **UC10 ground-truth file** when available. JSON or CSV input needs `question`, `sql`, and optional `expected_rows` and `order_sensitive` fields. Expected rows are an array of row arrays (JSON text inside CSV). See the example file. The model never receives reference SQL or expected answers.

Reports contain result-based execution accuracy, SQL execution success, average/P95 response time, the rate below 60 seconds, generated SQL, answers, rows, and estimated API cost.

**Current verification:** all three reference queries reproduce the supplied answers. Live model accuracy/latency and UC10 results are unmeasured because no API key or UC10 ground truth was provided.

## Cost and limits

At an illustrative 2,000 input + 300 output tokens **per model call**, each call costs about **$0.00048** at standard [GPT-4o mini prices](https://developers.openai.com/api/docs/models/gpt-4o-mini). A simple question normally makes three calls, or about **$0.144 for 100 questions** at that illustrative token count; decomposed questions and correction retries cost more. Actual successful-call token usage is aggregated and displayed. No paid model calls were made during this build.

The UI stops new model requests near $2 of estimated successful-call usage in the current session. This resets with a new session and is **not an account-wide spending cap**. Monitor API usage; failed requests may also be billed.

The UI returns up to 200 rows per subquestion. Evaluation compares up to 1,000 and refuses truncated comparisons. SQL execution has a two-second limit; each model request times out after 35 seconds. Multiple stages can exceed the 60-second target; live latency is unmeasured.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests cover real CSV import, provided answers, SQL restrictions, mocked provider responses, result comparison, and Streamlit interactions. No paid calls are made.

The pre-existing `RAG_BUILD_PROPOSAL.md` is unrelated and was left unchanged.
