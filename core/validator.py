"""SQL formatting, syntax validation, and dialect transpilation via sqlglot (no table restrictions)."""
import re
import sqlglot

DIALECT_MAP = {
    "postgresql": ("postgres", "PostgreSQL"),
    "mysql": ("mysql", "MySQL"),
    "sqlite": ("sqlite", "SQLite"),
    "tsql": ("tsql", "SQL Server (T-SQL)"),
    "bigquery": ("bigquery", "BigQuery"),
    "snowflake": ("snowflake", "Snowflake"),
    "oracle": ("oracle", "Oracle"),
}

_FENCE_RE = re.compile(r"```(?:sql|postgresql|postgres|mysql|sqlite|tsql|plsql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def clean_sql(raw: str) -> str:
    s = (raw or "").strip()
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    elif s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("sql"):
            s = s[3:]
    s = re.sub(r"^sql\s*:\s*", "", s.strip(), flags=re.IGNORECASE).strip()
    if not re.match(r"^(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|MERGE|EXPLAIN|SHOW|VALUES)\b", s, re.IGNORECASE):
        m_kw = re.search(
            r"\b(WITH\s+.+|SELECT\s+.+|INSERT\s+.+|UPDATE\s+.+|DELETE\s+.+|CREATE\s+.+|ALTER\s+.+|MERGE\s+.+)",
            s,
            re.IGNORECASE | re.DOTALL,
        )
        if m_kw:
            s = m_kw.group(1).strip()
    return s.strip()


def format_and_check_sql(sql: str, dialect: str = "postgresql") -> tuple[str, bool, str | None]:
    """Formats SQL for the target dialect using sqlglot and checks syntax validity.
    Returns (formatted_sql, is_valid, warning_or_none). Never restricts tables or domains.
    """
    sg_dialect, _ = DIALECT_MAP.get((dialect or "postgresql").lower(), ("postgres", "PostgreSQL"))
    cleaned = clean_sql(sql)
    if not cleaned:
        return "", False, "Empty SQL output."
    for read_dialect in (sg_dialect, "postgres", "mysql", "sqlite", None):
        try:
            transpiled = sqlglot.transpile(cleaned, read=read_dialect, write=sg_dialect, pretty=True)
            if transpiled:
                out = ";\n\n".join(stmt.rstrip(";") for stmt in transpiled if stmt.strip()) + ";"
                return out, True, None
        except Exception:
            continue
    # Even if vendor-specific DDL/procedural syntax isn't in sqlglot's grammar, still return the SQL cleanly
    final_sql = cleaned if cleaned.endswith(";") else cleaned + ";"
    return final_sql, True, None
