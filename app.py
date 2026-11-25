import streamlit as st
from core.engine import Text2SQLEngine
from core.llm import SQLGenerator
from core.validator import DIALECT_MAP

st.set_page_config(page_title="QueryPilot AI — Universal SQL Agent", page_icon="🧠", layout="wide")
st.title("🧠 QueryPilot AI — Universal Text-to-SQL Agent")
st.caption("Generate production-ready SQL for any domain, dataset, or dialect in the world.")

with st.sidebar:
    st.header("Settings")
    dialect = st.selectbox("SQL Dialect", list(DIALECT_MAP.keys()), format_func=lambda k: DIALECT_MAP[k][1])
    schema_hint = st.text_area("Optional Custom Schema", placeholder="e.g. users(id, name, created_at)\nevents(id, user_id, event_type, ts)")
    if st.button("Reset context"):
        st.session_state.pop("engine", None)
        st.session_state.pop("log", None)

if "engine" not in st.session_state:
    st.session_state.engine = Text2SQLEngine(SQLGenerator())
    st.session_state.log = []

eng = st.session_state.engine
q = st.chat_input("Ask any SQL question in any domain...")
if q:
    with st.spinner("Generating SQL..."):
        st.session_state.log.append(eng.ask(q, dialect=dialect, schema_hint=schema_hint))

for r in reversed(st.session_state.log):
    with st.container(border=True):
        st.markdown(f"**🧑 {r.question}**  `{r.dialect_label}`")
        if r.error:
            st.error(r.error)
        else:
            st.code(r.sql, language="sql")
            if r.explanation:
                st.info(r.explanation)
            if r.assumed_schema:
                st.caption(f"Schema used: `{r.assumed_schema}` • {r.latency_s}s")
