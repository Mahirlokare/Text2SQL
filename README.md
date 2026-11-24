# Text-to-SQL Assistant (Final Year Project)

Natural-language interface to relational databases using an LLM (Gemini), with safety validation,
self-correction and quantitative evaluation.

## Architecture
```
Question → Schema-aware prompt (+few-shot, +chat history) → LLM → SQL
        → Validator (sqlglot AST: SELECT-only, table allow-list, LIMIT) → Read-only SQLite exec
        → on error: Repair loop (LLM sees error) → Results table / chart / CSV
```
Modules: `core/schema.py` (introspection), `core/llm.py` (generation/repair), `core/validator.py` (security),
`core/engine.py` (pipeline), `app.py` (Streamlit UI), `evaluate.py` (benchmark), `tests/`.

## Run
```
pip install -r requirements.txt
copy .env.example .env      # add GEMINI_API_KEY
python seed_db.py
streamlit run app.py
pytest                      # validator tests
python evaluate.py          # execution accuracy
```

## Report ideas / extensions
- Compare models/prompts (zero-shot vs few-shot vs schema+samples) using `evaluate.py`; chart results.
- Ablation: with/without self-correction.
- Security section: prompt injection, SQL injection, read-only connection, AST validation.
- Extensions: PostgreSQL/MySQL support, schema pruning via embeddings (RAG) for big DBs, CSV upload,
  Spider/BIRD benchmark, voice input, fine-tuned small model.
