"""Simple assignment UI. Run: python3 -m streamlit run streamlit_app.py"""
import csv
import io
import os

import pandas as pd
import streamlit as st

from bikestore.assignment import DB_PATH, EXAMPLES, ROOT, ask, context, ensure_database
from bikestore.sql import QueryError
from bikestore.llm import ModelError, load_env

st.set_page_config(page_title="Bike Store Text-to-SQL", page_icon="🚲", layout="centered")
load_env(ROOT / ".env")
api_key = os.environ.get("OPENAI_API_KEY", "").strip()
if not api_key:
    try:
        api_key = st.secrets.get("OPENAI_API_KEY", "")
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass

st.title("🚲 Bike Store Text-to-SQL")
st.caption("Ask a question → generate SQL → get an answer, table, and graph.")
try:
    ensure_database()
    schema, overview = context()
except (ValueError, OSError) as exc:
    st.error(str(exc))
    st.stop()

with st.sidebar:
    st.header("Project settings")
    mode = st.radio("Query mode", ["GPT-4o mini", "Examples (no API calls)"], index=0 if api_key else 1)
    examples_only = mode.startswith("Examples")
    st.caption("SQLite · 9 tables · {} to {}".format(overview["data_start"], overview["data_end"]))
    if not api_key:
        st.info("To enable the LLM, add OPENAI_API_KEY to the project's .env file or Streamlit secrets.")
    with st.expander("Database schema"):
        for table in schema["tables"]:
            st.markdown("**{}** ({} rows)".format(table["name"], table["row_count"]))
            st.code(", ".join(c["name"] for c in table["columns"]), language="text")
    if st.button("Clear chat"):
        st.session_state.messages = []
    st.caption("Only read-only queries are allowed. Model: gpt-4o-mini.")

if examples_only:
    st.info("Example mode uses the three reference SQL queries from the statement. It does not call an LLM. Select GPT-4o mini for arbitrary questions.")
else:
    st.caption("Questions, recent chat context, and schema metadata are sent to OpenAI. SQL results are summarized locally.")

st.session_state.setdefault("messages", [])
st.session_state.setdefault("estimated_spend", 0.0)
selected = None
with st.expander("Try the problem statement's examples", expanded=not st.session_state.messages):
    for example in EXAMPLES:
        if st.button(example["question"], key=example["id"]):
            selected = example["question"]


def show_result(result, key):
    st.write(result["answer"])
    st.markdown("**Generated SQL**" if result["mode"] != "example" else "**Reference SQL (example mode)**")
    st.code(result["sql"], language="sql")
    frame = pd.DataFrame(result["rows"], columns=result["columns"])
    st.dataframe(frame, hide_index=True, width="stretch")
    chart = result["chart"]
    if (len(frame) > 1 and chart.get("type") in ("bar", "line")
            and chart.get("x") in frame and chart.get("y") in frame
            and pd.api.types.is_numeric_dtype(frame[chart["y"]])):
        st.caption(chart.get("title", ""))
        plot = st.line_chart if chart["type"] == "line" else st.bar_chart
        plot(frame, x=chart["x"], y=chart["y"])
    for note in result["notes"]:
        st.caption(note)
    if result["truncated"]:
        st.warning("Only the first 200 rows are shown. Narrow the question for a complete result.")
    st.caption("Response time: {:.3f}s · {} · Estimated API cost: ${:.6f}".format(
        result["response_seconds"], result["mode"], result["estimated_cost_usd"]))
    # Prefix formula-like text so downloaded values cannot become spreadsheet formulas.
    output = io.StringIO()
    writer = csv.writer(output)
    def safe(value):
        return "'" + value if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))) else value
    writer.writerow([safe(c) for c in result["columns"]])
    writer.writerows([[safe(row[c]) for c in result["columns"]] for row in result["rows"]])
    st.download_button("Download results", output.getvalue(), "results.csv", "text/csv", key="csv-" + str(key))


for index, message in enumerate(st.session_state.messages):
    with st.chat_message("user"):
        st.write(message["question"])
    with st.chat_message("assistant"):
        if "error" in message:
            st.error(message["error"])
        else:
            show_result(message, index)

question = st.chat_input("Ask a business question about the bike store…") or selected
if question:
    if not examples_only and st.session_state.estimated_spend >= 1.99:
        st.error("The current session is close to the $2 experiment budget. Stop and check your API usage before continuing.")
        st.stop()
    history = []
    for message in st.session_state.messages[-3:]:
        if "error" not in message:
            history.extend([{"role": "user", "content": message["question"]},
                            {"role": "assistant", "content": message["answer"][:1200]}])
    try:
        with st.spinner("Generating and running SQL…"):
            result = ask(question, DB_PATH, api_key, history, examples_only)
        st.session_state.estimated_spend += result["estimated_cost_usd"]
        st.session_state.messages.append(result)
    except (QueryError, ModelError, ValueError) as exc:
        st.session_state.messages.append({"question": question, "error": str(exc)})
    st.rerun()
