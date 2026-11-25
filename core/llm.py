"""NL-to-SQL generator: built-in zero-service engine by default, optional Gemini when GEMINI_API_KEY is set."""
import os
import re
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


class BuiltInSQLGenerator:
    """Deterministic, zero-cost, zero-external-service NL-to-SQL translator for the schema."""

    @staticmethod
    def _extract_limit(q: str, default: int = 5) -> int:
        m = re.search(r"\b(?:top|first|last|limit)\s+(\d+)\b", q)
        if not m:
            m = re.search(r"\b(\d+)\s+(?:most|least|cheapest|customers|products|orders|items|cities)\b", q)
        return max(1, min(int(m.group(1)), 100)) if m else default

    @staticmethod
    def _extract_year(q: str) -> str | None:
        m = re.search(r"\b(20\d{2})\b", q)
        return m.group(1) if m else None

    def generate(self, question: str, schema: str, history: list[tuple[str, str]] | None = None) -> str:
        q = question.lower().strip()
        n = self._extract_limit(q, default=5)
        yr = self._extract_year(q)

        # Unrelated / unanswerable questions
        schema_terms = (
            "customer", "client", "user", "buyer", "city", "cities",
            "order", "sale", "revenue", "spend", "spent", "income", "month", "year",
            "product", "item", "price", "stock", "inventory", "expensive", "cheap",
            "category", "categories", "unit", "quantity", "sold", "deliver", "cancel", "ship", "pending",
        )
        if not any(t in q for t in schema_terms):
            return "UNANSWERABLE"

        # 1. Customers who never placed an order
        if "customer" in q and ("never" in q or "no order" in q or "without order" in q):
            if "how many" in q or "count" in q:
                return "SELECT COUNT(*) AS customers_without_orders FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)"
            return "SELECT id, name, email, city FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)"

        # 2. Top customers by spend / revenue
        if "customer" in q and ("spend" in q or "spent" in q or "revenue" in q or "top" in q or "best" in q):
            return (
                "SELECT c.name AS customer, c.city, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS total_spend "
                "FROM customers c "
                "JOIN orders o ON o.customer_id = c.id "
                "JOIN order_items oi ON oi.order_id = o.id "
                "WHERE o.status != 'cancelled' "
                f"GROUP BY c.id ORDER BY total_spend DESC LIMIT {n}"
            )

        # 3. Monthly revenue (optionally filtered by year)
        if "month" in q and ("revenue" in q or "sale" in q or "spend" in q or "order" in q):
            where = f"WHERE o.status != 'cancelled' AND strftime('%Y', o.order_date) = '{yr}' " if yr else "WHERE o.status != 'cancelled' "
            return (
                "SELECT strftime('%Y-%m', o.order_date) AS month, "
                "COUNT(DISTINCT o.id) AS total_orders, "
                "ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue "
                "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
                f"{where}GROUP BY month ORDER BY month"
            )

        # 4. Category units / revenue
        if "categor" in q and ("unit" in q or "quantity" in q or "sell" in q or "sold" in q or "most" in q):
            return (
                "SELECT c.name AS category, SUM(oi.quantity) AS total_units_sold, "
                "ROUND(SUM(oi.quantity * oi.unit_price), 2) AS total_revenue "
                "FROM order_items oi "
                "JOIN products p ON p.id = oi.product_id "
                "JOIN categories c ON c.id = p.category_id "
                "GROUP BY c.id ORDER BY total_units_sold DESC"
            )
        if "categor" in q and ("revenue" in q or "sale" in q):
            return (
                "SELECT c.name AS category, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue "
                "FROM order_items oi "
                "JOIN products p ON p.id = oi.product_id "
                "JOIN categories c ON c.id = p.category_id "
                "GROUP BY c.id ORDER BY revenue DESC"
            )

        # 5. Orders by status & city (e.g. cancelled orders per city)
        for st in ("cancelled", "delivered", "shipped", "pending"):
            if st in q and "city" in q:
                return (
                    f"SELECT c.city, COUNT(o.id) AS {st}_orders "
                    "FROM orders o JOIN customers c ON c.id = o.customer_id "
                    f"WHERE o.status = '{st}' GROUP BY c.city ORDER BY {st}_orders DESC"
                )
            if st in q and ("how many" in q or "count" in q or "total" in q):
                return f"SELECT COUNT(*) AS {st}_orders FROM orders WHERE status = '{st}'"
            if st in q:
                return (
                    "SELECT o.id AS order_id, c.name AS customer, c.city, o.order_date, o.status "
                    f"FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status = '{st}' "
                    "ORDER BY o.order_date DESC LIMIT 25"
                )

        # 6. Customers per city / customer count
        if "customer" in q and ("city" in q or "cities" in q):
            return "SELECT city, COUNT(*) AS customer_count FROM customers GROUP BY city ORDER BY customer_count DESC"
        if "customer" in q and ("how many" in q or "count" in q or "total" in q):
            return "SELECT COUNT(*) AS total_customers FROM customers"

        # 7. Products: average price, most expensive, cheapest, stock, top revenue
        if ("average" in q or "avg" in q) and "price" in q:
            return "SELECT ROUND(AVG(price), 2) AS avg_product_price FROM products"
        if "product" in q and ("expensive" in q or "highest price" in q):
            return f"SELECT name, price, stock FROM products ORDER BY price DESC LIMIT {n}"
        if "product" in q and ("cheap" in q or "lowest price" in q):
            return f"SELECT name, price, stock FROM products ORDER BY price ASC LIMIT {n}"
        if "stock" in q or "inventory" in q:
            return f"SELECT name, price, stock FROM products ORDER BY stock ASC LIMIT {n}"
        if "product" in q and ("revenue" in q or "top" in q or "best" in q or "sold" in q):
            return (
                "SELECT p.name AS product, SUM(oi.quantity) AS units_sold, "
                "ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue "
                "FROM order_items oi JOIN products p ON p.id = oi.product_id "
                f"GROUP BY p.id ORDER BY revenue DESC LIMIT {n}"
            )

        # 8. Total revenue / orders overview
        if "revenue" in q or "sale" in q:
            where = f"WHERE o.status != 'cancelled' AND strftime('%Y', o.order_date) = '{yr}'" if yr else "WHERE o.status != 'cancelled'"
            return (
                "SELECT COUNT(DISTINCT o.id) AS total_orders, "
                "ROUND(SUM(oi.quantity * oi.unit_price), 2) AS total_revenue "
                f"FROM orders o JOIN order_items oi ON oi.order_id = o.id {where}"
            )
        if "order" in q and ("status" in q or "breakdown" in q):
            return "SELECT status, COUNT(*) AS order_count FROM orders GROUP BY status ORDER BY order_count DESC"
        if "order" in q and ("how many" in q or "count" in q):
            return "SELECT COUNT(*) AS total_orders FROM orders"

        # 9. General listing fallbacks
        if "categor" in q:
            return "SELECT id, name FROM categories ORDER BY id"
        if "product" in q or "item" in q:
            return "SELECT id, name, price, stock FROM products ORDER BY id LIMIT 25"
        if "customer" in q:
            return "SELECT id, name, email, city, signup_date FROM customers ORDER BY id LIMIT 25"
        if "order" in q:
            return (
                "SELECT o.id AS order_id, c.name AS customer, o.order_date, o.status "
                "FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.order_date DESC LIMIT 25"
            )
        return "UNANSWERABLE"

    def repair(self, question: str, schema: str, bad_sql: str, error: str) -> str:
        return self.generate(question, schema)


class SQLGenerator:
    """Uses Gemini when GEMINI_API_KEY is set; otherwise uses BuiltInSQLGenerator with zero external services."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self._builtin = BuiltInSQLGenerator()
        self.mode = "builtin"
        self.client = None
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        if key:
            from google import genai
            self.client = genai.Client(api_key=key)
            self.mode = "gemini"

    def _call(self, prompt: str) -> str:
        from google.genai import types
        r = self.client.models.generate_content(
            model=self.model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0))
        return (r.text or "").strip()

    def generate(self, question: str, schema: str, history: list[tuple[str, str]] | None = None) -> str:
        if self.client is None:
            return self._builtin.generate(question, schema, history)
        ctx = ""
        if history:
            ctx = "Previous turns:\n" + "\n".join(f"Q: {q}\nSQL: {s}" for q, s in history[-3:]) + "\n\n"
        return self._call(f"SCHEMA:\n{schema}\n\n{FEW_SHOT}\n\n{ctx}Q: {question}\nSQL:")

    def repair(self, question: str, schema: str, bad_sql: str, error: str) -> str:
        if self.client is None:
            return self._builtin.repair(question, schema, bad_sql, error)
        return self._call(
            f"SCHEMA:\n{schema}\n\nQuestion: {question}\nThe following SQL failed.\n"
            f"SQL: {bad_sql}\nError: {error}\nReturn a corrected SQL query only.")
