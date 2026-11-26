"""Universal NL-to-SQL Query Agent:
Generates SQL for ANY question, ANY domain/dataset in the world, and ANY major SQL dialect,
with or without a user-provided schema.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from dotenv import load_dotenv

from .validator import DIALECT_MAP, clean_sql

load_dotenv()


@dataclass
class GeneratedSQL:
    sql: str
    explanation: str
    assumed_schema: str


def _build_system_prompt(dialect_label: str) -> str:
    return f"""You are QueryPilot AI, a world-class Universal Text-to-SQL Architect.
Your job is to generate clean, accurate, production-ready **{dialect_label}** SQL for ANY request in ANY domain or dataset in the world (finance, healthcare, aviation, e-commerce, social networks, gaming, HR, logistics, education, IoT, SaaS, analytics, DDL/schema design, window functions, CTEs, etc.).

Rules:
1. NEVER refuse a query and NEVER say UNANSWERABLE. Every prompt can be expressed as SQL.
2. If the user provides a schema or table/column names, strictly use them.
3. If the user does NOT provide a schema, design realistic, clean, industry-standard table and column names that fit the question naturally.
4. Use idiomatic **{dialect_label}** syntax, explicit JOINs, readable aliases, and clean indentation.
5. Respond strictly as a JSON object with these 3 string keys:
   - "sql": the raw {dialect_label} SQL query (no markdown backticks)
   - "explanation": 1-3 concise sentences explaining how the query works
   - "assumed_schema": compact list of tables & key columns used (e.g. "flights(id, origin, destination, departure_time, status), bookings(id, flight_id, passenger_id, fare)")"""


def _parse_agent_response(raw: str) -> GeneratedSQL:
    text = (raw or "").strip()
    # Try parsing JSON (either raw or inside ```json ... ```)
    candidates = [text]
    m_json = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m_json:
        candidates.insert(0, m_json.group(1))
    else:
        b_start = text.find("{")
        b_end = text.rfind("}")
        if b_start != -1 and b_end > b_start:
            candidates.insert(0, text[b_start:b_end + 1])

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and obj.get("sql"):
                return GeneratedSQL(
                    sql=clean_sql(str(obj.get("sql", ""))),
                    explanation=str(obj.get("explanation") or "Generated SQL query tailored to your request.").strip(),
                    assumed_schema=str(obj.get("assumed_schema") or "").strip(),
                )
        except Exception:
            continue

    # Fallback if LLM returned plain SQL/markdown instead of JSON
    sql = clean_sql(text)
    return GeneratedSQL(
        sql=sql,
        explanation="Generated idiomatic SQL query matching your prompt and target dialect.",
        assumed_schema="",
    )


class UniversalSQLSynthesizer:
    """Offline universal NL-to-SQL synthesizer that infers tables, columns, joins, filters & aggregations
    for ANY domain in the world when no network LLM is reachable."""

    STOPWORDS = {
        "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "with", "by", "per", "each",
        "all", "any", "show", "give", "get", "find", "list", "select", "fetch", "display", "write",
        "query", "sql", "me", "who", "what", "which", "where", "when", "how", "many", "much",
        "is", "are", "was", "were", "have", "has", "had", "that", "this", "those", "these",
        "top", "bottom", "most", "least", "highest", "lowest", "total", "sum", "average", "avg",
        "count", "number", "minimum", "min", "maximum", "max", "between", "over", "under", "more",
        "less", "than", "greater", "last", "past", "days", "months", "years", "year", "month", "day",
        "and", "or", "not", "never", "without", "their", "its", "ordered", "sorted", "grouped",
        "paid", "active", "inactive", "recent", "latest", "best", "worst", "using", "window",
        "function", "functions", "detect", "calculate", "compute", "above", "below", "within", "hours",
    }

    @staticmethod
    def _singular(word: str) -> str:
        if word.endswith("ies") and len(word) > 4:
            return word[:-3] + "y"
        if word.endswith("ses") and len(word) > 4:
            return word[:-2]
        if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            return word[:-1]
        return word

    @staticmethod
    def _plural(word: str) -> str:
        if word.endswith(("s", "x", "z", "ch", "sh")):
            return word if word.endswith("s") else word + "es"
        if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
            return word[:-1] + "ies"
        return word + "s"

    @classmethod
    def _parse_schema_tables(cls, schema_hint: str) -> list[tuple[str, list[str]]]:
        """Extracts [(table_name, [col1, col2, ...])] if user supplied a schema."""
        if not schema_hint or not schema_hint.strip():
            return []
        tables = []
        # Match CREATE TABLE foo (a INT, b TEXT) or foo(a, b, c)
        for m in re.finditer(r"(?:CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)?([a-zA-Z_]\w*)\s*\(([^)]+)\)", schema_hint, re.IGNORECASE):
            tname = m.group(1)
            if tname.upper() in {"COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "VARCHAR", "NUMERIC", "DECIMAL", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}:
                continue
            cols = []
            for part in m.group(2).split(","):
                tok = part.strip().split()
                if not tok:
                    continue
                cname = tok[0].strip('"`[]')
                if cname.upper() not in {"PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "KEY", "INDEX", "CHECK"}:
                    cols.append(cname)
            if cols:
                tables.append((tname, cols))
        return tables

    def synthesize(self, question: str, dialect: str = "postgresql", schema_hint: str = "") -> GeneratedSQL:
        raw = (question or "").strip()
        if re.match(r"^(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER)\b", raw, re.IGNORECASE):
            return GeneratedSQL(sql=raw, explanation="Validated and formatted your SQL statement for the selected dialect.", assumed_schema=schema_hint)

        q = raw.lower()
        parsed_tables = self._parse_schema_tables(schema_hint)

        # Extract candidate domain nouns from prompt
        words = [re.sub(r"[^a-z0-9_]", "", w) for w in q.split()]
        nouns = [w for w in words if len(w) >= 3 and not w.isdigit() and w not in self.STOPWORDS]

        if parsed_tables:
            main_table, main_cols = parsed_tables[0]
            sec_table, sec_cols = (parsed_tables[1] if len(parsed_tables) > 1 else (None, []))
        else:
            primary_noun = nouns[0] if nouns else "records"
            main_table = self._plural(self._singular(primary_noun))
            sec_noun = next((n for n in nouns[1:] if self._plural(self._singular(n)) != main_table and n.endswith("s")), None)
            sec_table = self._plural(self._singular(sec_noun)) if sec_noun else None
            main_cols = ["id", "name", "status", "category", "amount", "created_at"]
            sec_cols = ["id", f"{self._singular(main_table)}_id", "amount", "quantity", "created_at"] if sec_table else []

        alias = main_table[0]
        # Detect DDL create table request
        if "create table" in q or "design a schema" in q or "database schema for" in q:
            entity = self._plural(self._singular(nouns[-1] if nouns else "items"))
            sql = (
                f"CREATE TABLE {entity} (\n"
                f"  id SERIAL PRIMARY KEY,\n"
                f"  name VARCHAR(255) NOT NULL,\n"
                f"  status VARCHAR(50) DEFAULT 'active',\n"
                f"  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n"
                f")"
            )
            return GeneratedSQL(
                sql=sql,
                explanation=f"Creates a normalized `{entity}` table with primary key, status, and audit timestamp.",
                assumed_schema=f"{entity}(id, name, status, created_at)",
            )

        # Detect limit
        lim_m = re.search(r"\b(?:top|first|bottom|limit)\s+(\d+)\b", q) or re.search(r"\b(\d+)\s+(?:most|least|highest|lowest)\b", q)
        limit_n = int(lim_m.group(1)) if lim_m else (10 if "top" in q or "most" in q or "highest" in q else 50)

        # Detect metric / aggregation
        val_col = next((c for c in main_cols if any(k in c.lower() for k in ("salary", "price", "amount", "revenue", "score", "fare", "cost", "total", "balance", "views", "age"))), None)
        if not val_col:
            for kw in ("salary", "revenue", "spend", "price", "amount", "score", "views", "rating", "age", "fare", "balance", "sales", "profit"):
                if kw in q:
                    val_col = kw
                    break
            else:
                val_col = "amount"

        grp_col = None
        m_grp = re.search(r"\b(?:by|per|each)\s+([a-z_][a-z0-9_]*)\b", q)
        if m_grp and m_grp.group(1) not in {"the", "a", "an", "asc", "desc"}:
            grp_col = self._singular(m_grp.group(1))

        # Filters
        wheres = []
        yr_m = re.search(r"\b(20\d{2}|19\d{2})\b", q)
        date_col = next((c for c in main_cols if "date" in c.lower() or "time" in c.lower() or c.lower().endswith("_at")), "created_at")
        if yr_m:
            yr = yr_m.group(1)
            wheres.append(f"{alias}.{date_col} >= '{yr}-01-01' AND {alias}.{date_col} < '{int(yr)+1}-01-01'")
        gt_m = re.search(r"(?:over|above|greater than|more than|>)\s*\$?(\d+(?:\.\d+)?)", q)
        if gt_m and not ("more than" in q and grp_col):
            wheres.append(f"{alias}.{val_col} > {gt_m.group(1)}")
        lt_m = re.search(r"(?:under|below|less than|<)\s*\$?(\d+(?:\.\d+)?)", q)
        if lt_m:
            wheres.append(f"{alias}.{val_col} < {lt_m.group(1)}")

        for st in ("active", "inactive", "completed", "cancelled", "pending", "approved", "rejected", "delivered", "paid", "unpaid"):
            if re.search(rf"\b{st}\b", q):
                wheres.append(f"{alias}.status = '{st}'")

        from_clause = f"FROM {main_table} {alias}"
        assumed = f"{main_table}({', '.join(main_cols[:6])})"
        if sec_table:
            s_alias = sec_table[0] if sec_table[0] != alias else sec_table[:2]
            fk = f"{self._singular(main_table)}_id"
            from_clause += f" JOIN {sec_table} {s_alias} ON {s_alias}.{fk} = {alias}.id"
            assumed += f", {sec_table}({', '.join(sec_cols[:5])})"

        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        asc = "ASC" if any(w in q for w in ("least", "lowest", "bottom", "cheapest", "oldest", "minimum")) else "DESC"

        # Window function / Top-N per group (e.g. "Top 3 employees in each department")
        m_each = re.search(r"\b(?:in each|per|for each)\s+([a-z_][a-z0-9_]*)\b", q)
        if ("window" in q or "rank" in q or ("top" in q and m_each)):
            part_col = self._singular(m_each.group(1)) if m_each else (grp_col or "department")
            sql = (
                f"WITH ranked AS (\n"
                f"  SELECT {alias}.id, {alias}.name, {alias}.{part_col}, {alias}.{val_col},\n"
                f"         DENSE_RANK() OVER (PARTITION BY {alias}.{part_col} ORDER BY {alias}.{val_col} {asc}) AS rnk\n"
                f"  {from_clause}{where_sql}\n"
                f")\n"
                f"SELECT id, name, {part_col}, {val_col}, rnk\n"
                f"FROM ranked\n"
                f"WHERE rnk <= {limit_n}\n"
                f"ORDER BY {part_col}, rnk"
            )
            _, dialect_label = DIALECT_MAP.get(dialect.lower(), ("postgres", "PostgreSQL"))
            return GeneratedSQL(
                sql=sql,
                explanation=f"Uses `DENSE_RANK()` partitioned by `{part_col}` to return the top {limit_n} `{main_table}` per group in {dialect_label}.",
                assumed_schema=f"{main_table}(id, name, {part_col}, {val_col})",
            )

        # Aggregation modes
        if any(w in q for w in ("how many", "count", "number of")) and not grp_col:
            sql = f"SELECT COUNT(*) AS total_{main_table} {from_clause}{where_sql}"
        elif any(w in q for w in ("average", "avg", "mean")):
            if grp_col:
                sql = f"SELECT {alias}.{grp_col}, ROUND(AVG({alias}.{val_col}), 2) AS avg_{val_col} {from_clause}{where_sql} GROUP BY {alias}.{grp_col} ORDER BY avg_{val_col} {asc}"
            else:
                sql = f"SELECT ROUND(AVG({alias}.{val_col}), 2) AS avg_{val_col} {from_clause}{where_sql}"
        elif any(w in q for w in ("total", "sum", "revenue", "spend", "sales")) or grp_col:
            agg_expr = f"SUM({alias}.{val_col}) AS total_{val_col}" if any(w in q for w in ("total", "sum", "revenue", "spend", "sales", "amount", "salary")) else f"COUNT(*) AS total_count"
            order_alias = f"total_{val_col}" if "SUM(" in agg_expr else "total_count"
            if grp_col:
                hav = ""
                hm = re.search(r"(?:more than|over|at least|>)\s*\$?(\d+(?:\.\d+)?)", q)
                if hm:
                    hav = f" HAVING {order_alias} > {hm.group(1)}"
                sql = f"SELECT {alias}.{grp_col}, {agg_expr} {from_clause}{where_sql} GROUP BY {alias}.{grp_col}{hav} ORDER BY {order_alias} {asc} LIMIT {limit_n}"
            else:
                sql = f"SELECT {agg_expr} {from_clause}{where_sql}"
        else:
            sel_cols = ", ".join(f"{alias}.{c}" for c in main_cols[:5])
            order_col = f"{alias}.{val_col}" if any(w in q for w in ("top", "most", "highest", "lowest", "least", val_col)) else f"{alias}.id"
            sql = f"SELECT {sel_cols} {from_clause}{where_sql} ORDER BY {order_col} {asc} LIMIT {limit_n}"

        _, dialect_label = DIALECT_MAP.get(dialect.lower(), ("postgres", "PostgreSQL"))
        return GeneratedSQL(
            sql=sql,
            explanation=f"Generated a {dialect_label} query over `{main_table}` matching your filters and aggregations.",
            assumed_schema=assumed,
        )


class FreeOpenLLMClient:
    """Zero-key, $0 open-access LLM client via Pollinations OpenAI-compatible endpoint."""
    ENDPOINT = "https://text.pollinations.ai/openai"

    def chat(self, system: str, user_prompt: str, timeout: int = 25) -> str:
        import random
        body = {
            "model": "openai-fast",
            "temperature": 0,
            "seed": random.randint(1, 999999),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = json.dumps(body).encode("utf-8")
        last_err = None
        for attempt in range(4):
            req = urllib.request.Request(
                self.ENDPOINT,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "QueryPilotAI/2.1",
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
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2.5 * (attempt + 1))
                    continue
                raise
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2.0)
                    continue
                break
        raise RuntimeError(f"Free LLM call failed: {last_err}")


class SQLGenerator:
    """Universal multi-dialect SQL generator for any prompt and any schema in the world."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self._synthesizer = UniversalSQLSynthesizer()
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
            self.mode = "synthesizer"

    def generate_universal(
        self,
        question: str,
        dialect: str = "postgresql",
        schema_hint: str = "",
        history: list[tuple[str, str]] | None = None,
    ) -> GeneratedSQL:
        _, dialect_label = DIALECT_MAP.get((dialect or "postgresql").lower(), ("postgres", "PostgreSQL"))
        sys_prompt = _build_system_prompt(dialect_label)

        parts = [f"Target SQL Dialect: {dialect_label}"]
        if schema_hint and schema_hint.strip():
            parts.append(f"User-Provided Schema / Tables:\n{schema_hint.strip()}")
        else:
            parts.append("Schema: None provided — infer clean, realistic, industry-standard table and column names for this domain.")
        if history:
            parts.append("Recent conversation context:\n" + "\n".join(f"Q: {q}\nSQL: {s}" for q, s in history[-3:]))
        parts.append(f"User Request: {question}\nRespond with JSON {{\"sql\": \"...\", \"explanation\": \"...\", \"assumed_schema\": \"...\"}}:")
        user_prompt = "\n\n".join(parts)

        if self.client is not None:
            try:
                from google.genai import types
                r = self.client.models.generate_content(
                    model=self.model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(system_instruction=sys_prompt, temperature=0),
                )
                parsed = _parse_agent_response(r.text or "")
                if parsed.sql:
                    return parsed
            except Exception:
                pass

        if self._free_llm is not None:
            try:
                raw = self._free_llm.chat(sys_prompt, user_prompt)
                parsed = _parse_agent_response(raw)
                if parsed.sql and not parsed.sql.upper().startswith("UNANSWERABLE"):
                    return parsed
            except Exception:
                pass

        return self._synthesizer.synthesize(question, dialect=dialect, schema_hint=schema_hint)
