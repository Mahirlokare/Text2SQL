from core.validator import clean_sql, format_and_check_sql


def test_clean_sql_strips_markdown_fences():
    assert clean_sql("```sql\nSELECT 1\n```") == "SELECT 1"


def test_transpile_across_dialects():
    sql, valid, err = format_and_check_sql("SELECT name, salary FROM employees ORDER BY salary DESC LIMIT 5", "tsql")
    assert valid is True
    assert err is None
    assert "TOP 5" in sql.upper() or "FETCH" in sql.upper()


def test_allows_any_domain_tables_and_ddl():
    for raw in [
        "SELECT p.name, COUNT(a.id) FROM hospital_patients p JOIN admissions a ON a.patient_id = p.id GROUP BY p.id",
        "SELECT origin, destination, AVG(delay_minutes) FROM flights GROUP BY origin, destination",
        "CREATE TABLE ride_requests (id SERIAL PRIMARY KEY, rider_id INT, fare DECIMAL(10, 2))",
    ]:
        formatted, valid, _ = format_and_check_sql(raw, "postgresql")
        assert valid is True
        assert len(formatted) > 10
