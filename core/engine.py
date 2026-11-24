"""Pipeline: question -> SQL -> validate -> execute -> (self-correct on error)."""
import sqlite3
import time
from dataclasses import dataclass, field

import pandas as pd

from .schema import get_schema_text, get_table_names
from .validator import clean_sql, validate_sql, UnsafeSQLError


@dataclass
class QueryResult:
    question: str
    sql: str = ""
    df: pd.DataFrame | None = None
    error: str | None = None
    attempts: int = 0
    latency_s: float = 0.0
    trace: list[str] = field(default_factory=list)


class Text2SQLEngine:
    def __init__(self, db_path: str, generator, max_retries: int = 2):
        self.db_path = db_path
        self.gen = generator
        self.max_retries = max_retries
        self.history: list[tuple[str, str]] = []
        with self._connect() as c:
            self.schema = get_schema_text(c)
            self.tables = get_table_names(c)

    def _connect(self):
        # read-only connection = second line of defence behind the validator
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def ask(self, question: str) -> QueryResult:
        t0 = time.time()
        res = QueryResult(question=question)
        sql = clean_sql(self.gen.generate(question, self.schema, self.history))
        for attempt in range(self.max_retries + 1):
            res.attempts = attempt + 1
            res.sql = sql
            if sql.upper().startswith("UNANSWERABLE"):
                res.error = "This question can't be answered from the database schema."
                break
            try:
                safe = validate_sql(sql, self.tables)
                with self._connect() as c:
                    res.df = pd.read_sql_query(safe, c)
                res.error = None
                self.history.append((question, sql))
                break
            except (UnsafeSQLError, Exception) as e:
                res.error = str(e)
                res.trace.append(f"Attempt {attempt + 1} failed: {e}")
                if isinstance(e, UnsafeSQLError) and "Data-modifying" in str(e):
                    break  # never "repair" a malicious query
                if attempt < self.max_retries:
                    sql = clean_sql(self.gen.repair(question, self.schema, sql, str(e)))
        res.latency_s = round(time.time() - t0, 2)
        return res
