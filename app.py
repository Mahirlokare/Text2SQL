import os
import streamlit as st
import pandas as pd

from core.engine import Text2SQLEngine
from core.llm import SQLGenerator

st.set_page_config(page_title="Text-to-SQL Assistant", page_icon="🗄️", layout="wide")
st.title("🗄️ Text-to-SQL Assistant")
st.caption("Ask questions in plain English; get SQL + results.")

with st.sidebar:
    st.header("Settings")
    db_path = st.text_input("SQLite DB path", "data/shop.db")
    api_key = st.text_input("Gemini API key", type="password",
                            value=os.getenv("GEMINI_API_KEY", ""))
    retries = st.slider("Self-correction retries", 0, 3, 2)
    if st.button("Reset conversation"):
        st.session_state.pop("engine", None)
        st.session_state.pop("log", None)

if not os.path.exists(db_path):
    st.error(f"Database not found: {db_path}. Run `python seed_db.py` first.")
    st.stop()
if not api_key:
    st.info("Enter a Gemini API key in the sidebar (or put it in .env).")
    st.stop()

if "engine" not in st.session_state:
    st.session_state.engine = Text2SQLEngine(db_path, SQLGenerator(api_key), retries)
    st.session_state.log = []
eng = st.session_state.engine
eng.max_retries = retries

with st.expander("📚 Database schema"):
    st.code(eng.schema, language="sql")

examples = ["Top 5 customers by total spend", "Monthly revenue in 2024",
            "Which category sells the most units?", "How many orders were cancelled per city?"]
cols = st.columns(len(examples))
picked = None
for col, ex in zip(cols, examples):
    if col.button(ex):
        picked = ex
q = st.chat_input("Ask a question about the data...") or picked

if q:
    with st.spinner("Thinking..."):
        st.session_state.log.append(eng.ask(q))

for r in reversed(st.session_state.log):
    with st.container(border=True):
        st.markdown(f"**🧑 {r.question}**")
        st.code(r.sql, language="sql")
        if r.error:
            st.error(r.error)
        else:
            st.dataframe(r.df, use_container_width=True)
            num = r.df.select_dtypes("number").columns
            if len(r.df) > 1 and len(num) >= 1 and len(r.df.columns) >= 2:
                st.bar_chart(r.df.set_index(r.df.columns[0])[num[0]])
            st.download_button("Download CSV", r.df.to_csv(index=False), "result.csv",
                               key=f"dl{id(r)}")
        st.caption(f"Attempts: {r.attempts} • {r.latency_s}s")
        for t in r.trace:
            st.caption("↺ " + t)
