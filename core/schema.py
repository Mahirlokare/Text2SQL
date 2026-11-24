"""Schema introspection: builds a compact text description of the DB for the LLM prompt."""
import sqlite3


def get_schema_text(conn: sqlite3.Connection, sample_rows: int = 2) -> str:
    cur = conn.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    parts = []
    for t in tables:
        cols = cur.execute(f'PRAGMA table_info("{t}")').fetchall()
        fks = cur.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
        lines = [f"TABLE {t} ("]
        for c in cols:
            pk = " PRIMARY KEY" if c[5] else ""
            lines.append(f"  {c[1]} {c[2]}{pk},")
        for fk in fks:
            lines.append(f"  FOREIGN KEY ({fk[3]}) REFERENCES {fk[2]}({fk[4]}),")
        lines.append(")")
        rows = cur.execute(f'SELECT * FROM "{t}" LIMIT {sample_rows}').fetchall()
        if rows:
            lines.append(f"-- sample rows: {rows}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def get_table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0].lower() for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
