import re
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    import web
    web._engine = None
    web.USERS_DB = str(tmp_path / "users.db")
    web.init_db()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_full_self_contained_signup_confirm_login_and_query(client):
    # 1. Sign up
    r = client.post("/signup", data={
        "name": "Mahir",
        "email": "mahir@example.com",
        "password": "password123",
    })
    assert r.status_code == 200
    assert b"Confirm my email" in r.data

    # Extract signed confirmation URL
    m = re.search(rb'href="([^"]*/confirm/[^"]+)"', r.data)
    assert m is not None
    confirm_url = m.group(1).decode("utf-8")

    # 2. Unconfirmed login is blocked
    blocked = client.post("/login", data={"email": "mahir@example.com", "password": "password123"})
    assert b"Please confirm your account first" in blocked.data

    # 3. Confirm account via signed token link
    conf = client.get(confirm_url, follow_redirects=True)
    assert conf.status_code == 200
    assert b"Account confirmed!" in conf.data

    # 4. Sign in succeeds
    login_r = client.post(
        "/login",
        data={"email": "mahir@example.com", "password": "password123"},
        follow_redirects=True,
    )
    assert login_r.status_code == 200
    assert b"Hi, Mahir" in login_r.data

    # 5. Ask natural-language SQL questions with zero external API keys
    q_r = client.post("/app", data={"question": "Top 5 customers by total spend"})
    assert q_r.status_code == 200
    assert b"SELECT" in q_r.data
    assert b"total_spend" in q_r.data

    # 6. CSV export works
    csv_r = client.get("/export.csv")
    assert csv_r.status_code == 200
    assert b"total_spend" in csv_r.data
