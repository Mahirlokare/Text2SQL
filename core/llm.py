"""LLM wrapper (Gemini). Generates SQL from a question + schema, and can repair failed SQL."""
import os
from dotenv import load_dotenv

load_dotenv()

SYSTEM = """You are an expert SQLite analyst. Convert the user's question into ONE read-only
SQLite SELECT query using ONLY the tables and columns in the schema.
Rules: no explanations, no markdown, output raw SQL only. Never modify data.
Use explicit JOINs, table aliases, and meaningful column aliases.
If the question cannot be answered from the schema, output exactly: UNANSWERABLE"""

FEW_SHOT = """Examples:
Q: How many customers are there?
SQL: SELECT COUNT(*) AS total_customers FROM customers
Q: Top 3 products by revenue
SQL: SELECT p.name, SUM(oi.quantity * oi.unit_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.id ORDER BY revenue DESC LIMIT 3"""


class SQLGenerator:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set. Add it to .env")
        self.client = genai.Client(api_key=key)
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def _call(self, prompt: str) -> str:
        from google.genai import types
        r = self.client.models.generate_content(
            model=self.model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0))
        return (r.text or "").strip()

    def generate(self, question: str, schema: str, history: list[tuple[str, str]] | None = None) -> str:
        ctx = ""
        if history:
            ctx = "Previous turns:\n" + "\n".join(f"Q: {q}\nSQL: {s}" for q, s in history[-3:]) + "\n\n"
        return self._call(f"SCHEMA:\n{schema}\n\n{FEW_SHOT}\n\n{ctx}Q: {question}\nSQL:")

    def repair(self, question: str, schema: str, bad_sql: str, error: str) -> str:
        return self._call(
            f"SCHEMA:\n{schema}\n\nQuestion: {question}\nThe following SQL failed.\n"
            f"SQL: {bad_sql}\nError: {error}\nReturn a corrected SQL query only.")

    def explain(self, question: str, sql: str, preview: str) -> str:
        from google.genai import types
        r = self.client.models.generate_content(
            model=self.model,
            contents=f"Question: {question}\nSQL: {sql}\nResult preview:\n{preview}\n"
                     "Answer the question in 1-2 plain-English sentences.",
            config=types.GenerateContentConfig(temperature=0.2))
        return (r.text or "").strip()
