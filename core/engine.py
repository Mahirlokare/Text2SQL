"""Universal Text-to-SQL Query Agent pipeline (no fixed database — works for any domain & schema)."""
import time
from dataclasses import dataclass

from .validator import DIALECT_MAP, format_and_check_sql


@dataclass
class QueryResult:
    question: str
    dialect: str = "postgresql"
    dialect_label: str = "PostgreSQL"
    sql: str = ""
    explanation: str = ""
    assumed_schema: str = ""
    syntax_valid: bool = True
    error: str | None = None
    latency_s: float = 0.0


class Text2SQLEngine:
    """Stateless universal query-giving agent — generates, validates & formats SQL for any domain."""

    def __init__(self, generator):
        self.gen = generator
        self.history: list[tuple[str, str]] = []

    def ask(
        self,
        question: str,
        dialect: str = "postgresql",
        schema_hint: str = "",
        history: list[tuple[str, str]] | None = None,
    ) -> QueryResult:
        t0 = time.time()
        d_key = (dialect or "postgresql").lower()
        if d_key not in DIALECT_MAP:
            d_key = "postgresql"
        _, d_label = DIALECT_MAP[d_key]
        res = QueryResult(question=question, dialect=d_key, dialect_label=d_label)

        active_hist = history if history is not None else self.history
        try:
            gen_out = self.gen.generate_universal(
                question=question,
                dialect=d_key,
                schema_hint=schema_hint,
                history=active_hist,
            )
            formatted_sql, valid, warn = format_and_check_sql(gen_out.sql, dialect=d_key)
            res.sql = formatted_sql
            res.explanation = gen_out.explanation
            res.assumed_schema = gen_out.assumed_schema
            res.syntax_valid = valid
            if not formatted_sql:
                res.error = warn or "Could not generate SQL for this prompt."
            elif history is None:
                self.history.append((question, formatted_sql))
        except Exception as e:
            res.error = str(e)
        res.latency_s = round(time.time() - t0, 2)
        return res
