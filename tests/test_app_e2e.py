import re
import pytest


def _csrf(html: bytes) -> str:
    m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', html)
    assert m is not None, "Missing csrf_token in form"
    return m.group(1).decode("ascii")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OFFLINE_SQL_ONLY", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    import web
    web._engine = None
    web._cipher = None
    web._limiter.reset()
    web.USERS_DB = str(tmp_path / "users.db")
    web.init_db()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_universal_query_agent_across_world_domains_and_dialects(client):
    tok = _csrf(client.get("/signup").data)
    r = client.post("/signup", data={
        "csrf_token": tok,
        "name": "Mahir",
        "email": "mahir@example.com",
        "password": "Password123!",
    })
    m = re.search(rb'href="([^"]*/confirm/[^"]+)"', r.data)
    assert m is not None
    client.get(m.group(1).decode("utf-8"), follow_redirects=True)

    tok = _csrf(client.get("/login").data)
    login_r = client.post(
        "/login",
        data={"csrf_token": tok, "email": "mahir@example.com", "password": "Password123!"},
        follow_redirects=True,
    )
    assert b"Hi, Mahir" in login_r.data

    world_queries = [
        ("postgresql", "Find top 5 cardiologists with highest patient recovery rate in 2025"),
        ("mysql", "Detect bank accounts with total transactions over 10000 in 2025"),
        ("tsql", "Top 10 flights by average delay per airline"),
        ("bigquery", "Monthly active users and average watch time per country on a streaming platform"),
        ("snowflake", "Average gpa by department for students over 3.5"),
        ("oracle", "Highest salary per department for active employees"),
        ("sqlite", "Design a database schema for a ride sharing app"),
    ]
    tok = _csrf(login_r.data)
    for dialect, q in world_queries:
        res = client.post("/app", data={"csrf_token": tok, "question": q, "dialect": dialect})
        assert res.status_code == 200, f"Failed HTTP on: {q}"
        assert b'class="flash error"' not in res.data, f"Errored on: {q}"
        assert b'id="sqlOut"' in res.data, f"No SQL output for: {q}"

    dl = client.get("/download.sql")
    assert dl.status_code == 200
    assert len(dl.data) > 10
