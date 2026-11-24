import pytest
from core.validator import validate_sql, clean_sql, UnsafeSQLError

T = {"customers", "orders"}


def test_select_ok_adds_limit():
    assert "LIMIT" in validate_sql("SELECT * FROM customers", T).upper()


def test_existing_limit_kept():
    assert validate_sql("SELECT * FROM customers LIMIT 5", T) == "SELECT * FROM customers LIMIT 5"


@pytest.mark.parametrize("sql", [
    "DROP TABLE customers", "DELETE FROM customers", "UPDATE customers SET name='x'",
    "INSERT INTO customers VALUES (1)", "SELECT 1; DROP TABLE customers",
    "PRAGMA table_info(customers)", "SELECT * FROM secrets",
])
def test_blocked(sql):
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql, T)


def test_cte_allowed():
    validate_sql("WITH a AS (SELECT * FROM orders) SELECT * FROM a", T)


def test_clean_sql_strips_fences():
    assert clean_sql("```sql\nSELECT 1;\n```") == "SELECT 1"
