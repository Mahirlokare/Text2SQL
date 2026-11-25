"""NL-to-SQL generator:
1. Optional Gemini API (if GEMINI_API_KEY is set)
2. Free keyless OpenAI-compatible LLM endpoint (Pollinations, $0, no API key required)
3. Compositional Semantic NL-to-SQL Compiler fallback (offline / zero-network)
"""
import json
import os
import re
import urllib.request
from dotenv import load_dotenv

load_dotenv()

SYSTEM = """You are an expert SQLite data analyst. Convert the user's natural-language question into ONE valid, read-only SQLite SELECT query using ONLY the tables and columns in the provided schema.

Strict rules:
1. Output RAW SQL ONLY — no markdown fences, no commentary, no explanations.
2. Never modify data (SELECT / WITH ... SELECT only).
3. Use explicit JOINs, clear table aliases, and descriptive column aliases.
4. Known enum values in this database:
   - orders.status: 'delivered', 'shipped', 'cancelled', 'pending'
   - categories.name: 'Electronics', 'Books', 'Clothing', 'Home', 'Sports'
   - customers.city: 'Mumbai', 'Delhi', 'Bengaluru', 'Chennai', 'Pune', 'Kolkata', 'Hyderabad'
   - Revenue / spend = SUM(order_items.quantity * order_items.unit_price).
5. For SQLite date parts, use strftime('%Y', col) or strftime('%Y-%m', col).
6. Only if the question has nothing at all to do with this database (e.g., weather, sports scores, trivia), output: UNANSWERABLE"""

FEW_SHOT = """Examples:
Q: How many customers are there?
SQL: SELECT COUNT(*) AS total_customers FROM customers
Q: Top 3 products by revenue
SQL: SELECT p.name, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.id ORDER BY revenue DESC LIMIT 3
Q: Average order value by city
SQL: SELECT c.city, ROUND(AVG(ord_tot.order_total), 2) AS avg_order_value FROM (SELECT o.id, o.customer_id, SUM(oi.quantity * oi.unit_price) AS order_total FROM orders o JOIN order_items oi ON oi.order_id = o.id GROUP BY o.id) ord_tot JOIN customers c ON c.id = ord_tot.customer_id GROUP BY c.city ORDER BY avg_order_value DESC"""

CITIES = ("mumbai", "delhi", "bengaluru", "bangalore", "chennai", "pune", "kolkata", "hyderabad")
CATEGORIES = ("electronics", "books", "clothing", "home", "sports")
STATUSES = ("delivered", "shipped", "cancelled", "canceled", "pending")
MONTHS = {
    "january": "01", "jan": "01", "february": "02", "feb": "02", "march": "03", "mar": "03",
    "april": "04", "apr": "04", "may": "05", "june": "06", "jun": "06",
    "july": "07", "jul": "07", "august": "08", "aug": "08", "september": "09", "sep": "09",
    "october": "10", "oct": "10", "november": "11", "nov": "11", "december": "12", "dec": "12",
}


class SemanticSQLCompiler:
    """Compositional offline NL-to-SQL compiler covering filters, joins, aggregations, groupings & subqueries."""

    @staticmethod
    def _extract_limit(q: str, default: int | None = None) -> int | None:
        m = re.search(r"\b(?:top|first|bottom|lowest|cheapest|limit)\s+(\d+)\b", q)
        if not m:
            m = re.search(r"\b(\d+)\s+(?:most|least|cheapest|expensive|customers|products|orders|items|cities|categories)\b", q)
        if m:
            return max(1, min(int(m.group(1)), 500))
        if any(w in q for w in ("most ", "highest ", "lowest ", "cheapest ", "best ", "worst ", "which city", "which category", "which customer", "which product", "who ")):
            return default
        return default

    def generate(self, question: str, schema: str, history: list[tuple[str, str]] | None = None) -> str:
        raw = (question or "").strip()
        if re.match(r"^(SELECT|WITH)\b", raw, re.IGNORECASE):
            return raw

        q = raw.lower()
        # Check domain relevance broadly
        domain_words = (
            "customer", "client", "buyer", "user", "people", "person", "who", "name", "email",
            "city", "cities", *CITIES,
            "order", "purchase", "bought", "buy", "sale", "sold", "sell", "transaction", *STATUSES,
            "revenue", "spend", "spent", "earning", "income", "amount", "value", "total", "money",
            "product", "item", "price", "cost", "expensive", "cheap", "stock", "inventory",
            "categor", *CATEGORIES,
            "unit", "quantity", "qty", "month", "year", "daily", "date", "recent", "latest",
            "count", "how many", "number of", "average", "avg", "min", "max", "list", "show", "all", "table",
        )
        if not any(w in q for w in domain_words):
            return "UNANSWERABLE"

        # Special relational/subquery patterns
        if ("never" in q or "no order" in q or "without order" in q or "haven't ordered" in q or "not ordered" in q) and ("customer" in q or "who" in q):
            if "how many" in q or "count" in q or "number of" in q:
                return "SELECT COUNT(*) AS customers_never_ordered FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)"
            return "SELECT id, name, email, city, signup_date FROM customers WHERE id NOT IN (SELECT customer_id FROM orders) ORDER BY id"

        if ("never" in q or "unsold" in q or "not sold" in q or "no sale" in q) and ("product" in q or "item" in q):
            return "SELECT id, name, price, stock FROM products WHERE id NOT IN (SELECT product_id FROM order_items) ORDER BY id"

        if "out of stock" in q or ("zero stock" in q) or ("stock" in q and ("0" in q or "empty" in q)):
            return "SELECT id, name, price, stock FROM products WHERE stock = 0 ORDER BY name"

        if "average order value" in q or "aov" in q or ("average" in q and "order" in q and ("value" in q or "total" in q or "amount" in q)):
            if "city" in q or "cities" in q:
                return (
                    "SELECT c.city, ROUND(AVG(t.order_value), 2) AS avg_order_value "
                    "FROM (SELECT o.id, o.customer_id, SUM(oi.quantity * oi.unit_price) AS order_value "
                    "FROM orders o JOIN order_items oi ON oi.order_id = o.id GROUP BY o.id) t "
                    "JOIN customers c ON c.id = t.customer_id GROUP BY c.city ORDER BY avg_order_value DESC"
                )
            return (
                "SELECT ROUND(AVG(order_value), 2) AS avg_order_value FROM ("
                "SELECT o.id, SUM(oi.quantity * oi.unit_price) AS order_value "
                "FROM orders o JOIN order_items oi ON oi.order_id = o.id GROUP BY o.id)"
            )

        # Extract filters
        where = []
        needs_customers = False
        needs_orders = False
        needs_oi = False
        needs_products = False
        needs_categories = False

        for city in CITIES:
            if re.search(rf"\b{city}\b", q):
                canon = "Bengaluru" if city == "bangalore" else city.capitalize()
                where.append(f"c.city = '{canon}'")
                needs_customers = True

        for cat in CATEGORIES:
            if re.search(rf"\b{cat}\b", q):
                where.append(f"cat.name = '{cat.capitalize()}'")
                needs_categories = True
                needs_products = True

        matched_status = None
        for st in STATUSES:
            if re.search(rf"\b{st}\b", q):
                matched_status = "cancelled" if st == "canceled" else st
                where.append(f"o.status = '{matched_status}'")
                needs_orders = True

        yr_m = re.search(r"\b(202[0-9])\b", q)
        if yr_m:
            yr = yr_m.group(1)
            if "signup" in q or "signed up" in q or "joined" in q:
                where.append(f"strftime('%Y', c.signup_date) = '{yr}'")
                needs_customers = True
            else:
                where.append(f"strftime('%Y', o.order_date) = '{yr}'")
                needs_orders = True

        for m_name, m_num in MONTHS.items():
            if re.search(rf"\b{m_name}\b", q):
                where.append(f"strftime('%m', o.order_date) = '{m_num}'")
                needs_orders = True
                break

        # Numeric comparison filter (price / stock)
        cmp_m = re.search(r"(?:price|cost|priced|products?|items?)\s+(?:is\s+|are\s+)?(?:over|above|greater than|more than|>)\s*\$?(\d+(?:\.\d+)?)", q)
        if cmp_m and "spend" not in q and "revenue" not in q:
            where.append(f"p.price > {cmp_m.group(1)}")
            needs_products = True
        cmp_lt = re.search(r"(?:price|cost|priced|products?|items?)\s+(?:is\s+|are\s+)?(?:under|below|less than|cheaper than|<)\s*\$?(\d+(?:\.\d+)?)", q)
        if cmp_lt and "spend" not in q and "revenue" not in q:
            where.append(f"p.price < {cmp_lt.group(1)}")
            needs_products = True
        btw_m = re.search(r"between\s+\$?(\d+(?:\.\d+)?)\s+and\s+\$?(\d+(?:\.\d+)?)", q)
        if btw_m and ("price" in q or "product" in q or "cost" in q):
            where.append(f"p.price BETWEEN {btw_m.group(1)} AND {btw_m.group(2)}")
            needs_products = True

        asc = any(w in q for w in ("least", "lowest", "cheapest", "bottom", "minimum", "smallest", "ascending", "asc"))

        # Determine grouping dimension
        group_dim = None
        if any(p in q for p in ("per city", "by city", "each city", "city wise", "cities with", "which city")):
            group_dim = "city"
            needs_customers = True
        elif any(p in q for p in ("per category", "by category", "each category", "which category", "category sells", "categories by")):
            group_dim = "category"
            needs_categories = True
            needs_products = True
        elif any(p in q for p in ("per status", "by status", "each status", "order status")):
            group_dim = "status"
            needs_orders = True
        elif any(p in q for p in ("monthly", "per month", "by month", "each month")):
            group_dim = "month"
            needs_orders = True
        elif any(p in q for p in ("yearly", "per year", "by year", "annual")):
            group_dim = "year"
            needs_orders = True
        elif any(p in q for p in ("daily", "per day", "by date", "by day")):
            group_dim = "day"
            needs_orders = True
        elif ("customer" in q or "who " in q or "buyer" in q) and any(p in q for p in ("top", "spend", "spent", "revenue", "most order", "per customer", "by customer", "each customer", "who spent", "who bought")):
            group_dim = "customer"
            needs_customers = True
        elif ("product" in q or "item" in q) and any(p in q for p in ("revenue", "units sold", "most sold", "best selling", "top selling", "by revenue", "by sales", "per product", "most popular")):
            group_dim = "product"
            needs_products = True
            needs_oi = True

        # Determine metric
        metric = None
        if any(w in q for w in ("revenue", "spend", "spent", "sales", "total amount", "income", "earning")):
            metric = "revenue"
            needs_oi = True
            needs_orders = True
        elif any(w in q for w in ("unit", "units", "quantity", "quantities", "items sold")) or ("sell" in q and group_dim in ("category", "product")):
            metric = "units"
            needs_oi = True
        elif ("average" in q or "avg" in q) and "price" in q:
            metric = "avg_price"
            needs_products = True
        elif ("max" in q or "highest" in q or "most expensive") and "price" in q:
            metric = "max_price"
            needs_products = True
        elif ("min" in q or "lowest" in q or "cheapest") and "price" in q and ("what is" in q or "minimum" in q):
            metric = "min_price"
            needs_products = True
        elif any(w in q for w in ("how many", "count", "number of")) or group_dim in ("city", "status", "category"):
            if "product" in q and not needs_orders and not needs_oi:
                metric = "count_products"
                needs_products = True
            elif "order" in q or matched_status or group_dim == "status":
                metric = "count_orders"
                needs_orders = True
            elif "categor" in q and not needs_products and not needs_oi:
                metric = "count_categories"
                needs_categories = True
            else:
                metric = "count_customers"
                needs_customers = True

        # Build JOIN graph whenever multiple tables are involved
        def build_from() -> str:
            # Connect disconnected entities through bridge tables
            nc, no, noi, np_, ncat = needs_customers, needs_orders, needs_oi, needs_products, needs_categories
            if ncat:
                np_ = True
            if nc and (np_ or noi):
                no = True
                noi = True
            if no and np_:
                noi = True
            if noi and (nc or no):
                no = True

            if nc and not (no or noi or np_ or ncat):
                return "FROM customers c"
            if ncat and not (np_ or noi or no or nc):
                return "FROM categories cat"
            if np_ and not (noi or no or nc):
                base = "FROM products p"
                if ncat:
                    base += " JOIN categories cat ON cat.id = p.category_id"
                return base
            if no and not (noi or np_ or ncat):
                base = "FROM orders o"
                if nc:
                    base += " JOIN customers c ON c.id = o.customer_id"
                return base

            # Full transactional chain via order_items
            parts = ["FROM order_items oi"]
            if no or nc:
                parts.append("JOIN orders o ON o.id = oi.order_id")
            if nc:
                parts.append("JOIN customers c ON c.id = o.customer_id")
            if np_ or ncat:
                parts.append("JOIN products p ON p.id = oi.product_id")
            if ncat:
                parts.append("JOIN categories cat ON cat.id = p.category_id")
            return " ".join(parts)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_dir = "ASC" if asc else "DESC"

        # Grouped / Aggregated query construction
        if group_dim or metric:
            if metric == "revenue" and matched_status != "cancelled" and "cancel" not in q and group_dim == "customer":
                where_with_nc = list(where) + ["o.status != 'cancelled'"]
                where_sql = " WHERE " + " AND ".join(where_with_nc)

            dim_map = {
                "city": ("c.city AS city", "c.city"),
                "category": ("cat.name AS category", "cat.id"),
                "status": ("o.status AS status", "o.status"),
                "month": ("strftime('%Y-%m', o.order_date) AS month", "month"),
                "year": ("strftime('%Y', o.order_date) AS year", "year"),
                "day": ("o.order_date AS order_date", "o.order_date"),
                "customer": ("c.name AS customer, c.city", "c.id"),
                "product": ("p.name AS product", "p.id"),
            }
            metric_map = {
                "revenue": ("ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue", "revenue"),
                "units": ("SUM(oi.quantity) AS total_units", "total_units"),
                "avg_price": ("ROUND(AVG(p.price), 2) AS avg_price", "avg_price"),
                "max_price": ("MAX(p.price) AS max_price", "max_price"),
                "min_price": ("MIN(p.price) AS min_price", "min_price"),
                "count_products": ("COUNT(DISTINCT p.id) AS product_count", "product_count"),
                "count_orders": ("COUNT(DISTINCT o.id) AS order_count", "order_count"),
                "count_categories": ("COUNT(DISTINCT cat.id) AS category_count", "category_count"),
                "count_customers": ("COUNT(DISTINCT c.id) AS customer_count", "customer_count"),
            }

            if metric is None:
                metric = "revenue"
                needs_oi = True
                needs_orders = True

            m_expr, m_alias = metric_map[metric]
            from_sql = build_from()

            # HAVING filter if user asked "spent more than X" / "more than N orders"
            having_sql = ""
            hav_m = re.search(r"(?:more than|over|greater than|at least|>)\s*\$?(\d+(?:\.\d+)?)", q)
            if hav_m and group_dim and ("spend" in q or "spent" in q or "revenue" in q or "order" in q):
                having_sql = f" HAVING {m_alias} > {hav_m.group(1)}"

            if group_dim:
                d_sel, d_grp = dim_map[group_dim]
                order_col = "month" if group_dim in ("month", "year", "day") and "top" not in q and "most" not in q else m_alias
                if order_col in ("month", "year", "order_date"):
                    order_dir = "ASC"
                lim = self._extract_limit(q, default=5 if ("top" in q or "which " in q or "who " in q) else None)
                lim_sql = f" LIMIT {lim}" if lim else ""
                return f"SELECT {d_sel}, {m_expr} {from_sql}{where_sql} GROUP BY {d_grp}{having_sql} ORDER BY {order_col} {order_dir}{lim_sql}"
            else:
                return f"SELECT {m_expr} {from_sql}{where_sql}"

        # Row-level listing queries (e.g. "3 most expensive products", "orders from Mumbai", "products in Books", "low stock products")
        lim = self._extract_limit(q, default=25)
        if "product" in q or "item" in q or "expensive" in q or "cheap" in q or "stock" in q or needs_products:
            needs_products = True
            from_sql = build_from()
            order_by = "p.stock ASC" if "stock" in q else (f"p.price {order_dir}" if ("expensive" in q or "cheap" in q or "price" in q) else "p.id ASC")
            cols = "p.id, p.name, cat.name AS category, p.price, p.stock" if needs_categories else "p.id, p.name, p.price, p.stock"
            return f"SELECT {cols} {from_sql}{where_sql} ORDER BY {order_by} LIMIT {lim}"

        if "order" in q or matched_status or needs_orders:
            needs_orders = True
            needs_customers = True
            from_sql = build_from()
            return f"SELECT o.id AS order_id, c.name AS customer, c.city, o.order_date, o.status {from_sql}{where_sql} ORDER BY o.order_date DESC LIMIT {lim}"

        if "categor" in q:
            return "SELECT id, name FROM categories ORDER BY id"

        from_sql = "FROM customers c"
        return f"SELECT c.id, c.name, c.email, c.city, c.signup_date {from_sql}{where_sql} ORDER BY c.id ASC LIMIT {lim}"

    def repair(self, question: str, schema: str, bad_sql: str, error: str) -> str:
        return self.generate(question, schema)


class FreeOpenLLMClient:
    """Zero-key, $0 open-access LLM client via Pollinations OpenAI-compatible endpoint."""
    ENDPOINT = "https://text.pollinations.ai/openai"

    def chat(self, system: str, user_prompt: str, timeout: int = 20) -> str:
        import time
        import urllib.error

        body = {
            "model": "openai-fast",
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = json.dumps(body).encode("utf-8")
        last_err = None
        for attempt in range(2):
            req = urllib.request.Request(
                self.ENDPOINT,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "QueryPilotAI/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                choices = payload.get("choices") or []
                if choices:
                    content = ((choices[0].get("message") or {}).get("content") or "").strip()
                    if content:
                        return content
                raise RuntimeError("Empty response from free LLM endpoint")
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt == 0:
                    time.sleep(2.0)
                    continue
                raise
            except Exception as e:
                last_err = e
                break
        raise RuntimeError(f"Free LLM call failed: {last_err}")


class SQLGenerator:
    """Multi-tier NL-to-SQL generator:
    - Direct SQL passthrough if user wrote SELECT/WITH
    - Gemini API if GEMINI_API_KEY is configured
    - Free keyless LLM (Pollinations) for arbitrary English questions ($0, no account/key)
    - SemanticSQLCompiler fallback when offline or if network fails
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self._compiler = SemanticSQLCompiler()
        self._free_llm = None if os.getenv("OFFLINE_SQL_ONLY") == "1" else FreeOpenLLMClient()
        self.client = None
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        if key:
            from google import genai
            self.client = genai.Client(api_key=key)
            self.mode = "gemini"
        elif self._free_llm is not None:
            self.mode = "free-llm"
        else:
            self.mode = "compiler"

    def _call_gemini(self, prompt: str) -> str:
        from google.genai import types
        r = self.client.models.generate_content(
            model=self.model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0))
        return (r.text or "").strip()

    def generate(self, question: str, schema: str, history: list[tuple[str, str]] | None = None) -> str:
        raw = (question or "").strip()
        if re.match(r"^(SELECT|WITH)\b", raw, re.IGNORECASE):
            return raw

        ctx = ""
        if history:
            ctx = "Previous conversation turns:\n" + "\n".join(f"Q: {q}\nSQL: {s}" for q, s in history[-3:]) + "\n\n"
        prompt = f"SCHEMA:\n{schema}\n\n{FEW_SHOT}\n\n{ctx}Q: {question}\nSQL:"

        if self.client is not None:
            return self._call_gemini(prompt)
        if self._free_llm is not None:
            try:
                out = self._free_llm.chat(SYSTEM, prompt)
                if not out.strip().upper().startswith("UNANSWERABLE"):
                    return out
                # Double-check with semantic compiler before declaring UNANSWERABLE
                comp = self._compiler.generate(question, schema, history)
                return comp
            except Exception:
                pass
        return self._compiler.generate(question, schema, history)

    def repair(self, question: str, schema: str, bad_sql: str, error: str) -> str:
        prompt = (
            f"SCHEMA:\n{schema}\n\nQuestion: {question}\n"
            f"The following SQLite query failed validation or execution.\n"
            f"Failed SQL: {bad_sql}\nError: {error}\n"
            f"Return ONLY the corrected raw SQLite SELECT query."
        )
        if self.client is not None:
            return self._call_gemini(prompt)
        if self._free_llm is not None:
            try:
                return self._free_llm.chat(SYSTEM, prompt)
            except Exception:
                pass
        return self._compiler.repair(question, schema, bad_sql, error)
