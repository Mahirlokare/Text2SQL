"""SQL safety layer: only a single read-only SELECT over known tables is allowed."""
import re
import sqlglot
from sqlglot import exp


class UnsafeSQLError(ValueError):
    pass


FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
             exp.Alter, exp.Command, exp.Pragma, exp.Attach, exp.Transaction)

_FENCE_RE = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


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
    if s.upper().startswith("UNANSWERABLE"):
        return "UNANSWERABLE"
    # Strip trailing semicolon; if model added leading commentary before SELECT/WITH, trim it
    if not re.match(r"^(SELECT|WITH|VALUES|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|PRAGMA)\b", s, re.IGNORECASE):
        m_sel = re.search(r"\b(WITH\s+.+|SELECT\s+.+)", s, re.IGNORECASE | re.DOTALL)
        if m_sel:
            s = m_sel.group(1).strip()
    return s.rstrip(";").strip()


def validate_sql(sql: str, allowed_tables: set[str], max_rows: int = 500) -> str:
    """Returns a safe SQL string (with LIMIT enforced) or raises UnsafeSQLError."""
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQLError(f"Could not parse SQL: {e}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQLError("Exactly one statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.With)) and not tree.find(exp.Select):
        raise UnsafeSQLError("Only SELECT queries are allowed.")
    if isinstance(tree, FORBIDDEN) or any(tree.find(f) for f in FORBIDDEN):
        raise UnsafeSQLError("Data-modifying or administrative statements are blocked.")
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name not in allowed_tables and name not in cte_names:
            raise UnsafeSQLError(f"Unknown or disallowed table: {t.name}")
    if not tree.find(exp.Limit):
        sql = f"SELECT * FROM ({sql}) LIMIT {max_rows}"
    return sql
