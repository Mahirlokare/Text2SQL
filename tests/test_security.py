import re
from pathlib import Path
import pytest


def _csrf(html: bytes) -> str:
    m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', html)
    assert m is not None
    return m.group(1).decode("ascii")


@pytest.fixture
def sec_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OFFLINE_SQL_ONLY", "1")
    monkeypatch.setenv("ENCRYPTION_KEY", "super-secret-test-encryption-key-32bytes!!")
    import web
    web._engine = None
    web._cipher = None
    web._limiter.reset()
    web.USERS_DB = str(tmp_path / "users.db")
    web.init_db()
    web.app.config["TESTING"] = True
    return web.app.test_client(), Path(web.USERS_DB)


def test_strict_security_headers_and_no_google_fonts(sec_client):
    client, _ = sec_client
    resp = client.get("/login")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "font-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert "no-store" in resp.headers.get("Cache-Control", "")
    assert b"fonts.googleapis.com" not in resp.data
    assert b"fonts.gstatic.com" not in resp.data

    css = client.get("/static/style.css").data
    assert b"googleapis" not in css
    assert b"Inter" not in css


def test_csrf_blocks_forged_posts(sec_client):
    client, _ = sec_client
    for path in ("/login", "/signup", "/resend", "/app", "/logout"):
        r = client.post(path, data={"email": "x@y.com", "password": "Password1!"})
        assert r.status_code == 403, f"Expected 403 CSRF block on {path}"


def test_sensitive_data_encrypted_at_rest_and_single_use_confirm(sec_client):
    client, db_path = sec_client
    secret_email = "topsecret_user@example.com"
    secret_name = "Mahir ConfidentialName"
    secret_query = "Find secret payroll bonuses for executive board"

    tok = _csrf(client.get("/signup").data)
    r = client.post("/signup", data={
        "csrf_token": tok,
        "name": secret_name,
        "email": secret_email,
        "password": "StrongPassword9!",
    })
    m = re.search(rb'href="([^"]*/confirm/[^"]+)"', r.data)
    assert m is not None
    confirm_url = m.group(1).decode("utf-8")

    # First confirm succeeds; replay of same token is rejected
    assert b"Account confirmed!" in client.get(confirm_url, follow_redirects=True).data
    assert b"already been used or is invalid" in client.get(confirm_url, follow_redirects=True).data

    # Login and run a query
    tok = _csrf(client.get("/login").data)
    dash = client.post("/login", data={
        "csrf_token": tok, "email": secret_email, "password": "StrongPassword9!"
    }, follow_redirects=True)
    assert secret_name.encode() in dash.data

    tok = _csrf(dash.data)
    client.post("/app", data={"csrf_token": tok, "dialect": "postgresql", "question": secret_query})

    # Verify raw SQLite file contains ZERO plaintext PII, password, or query text
    raw_db = db_path.read_bytes()
    assert secret_email.encode() not in raw_db
    assert secret_name.encode() not in raw_db
    assert b"StrongPassword9!" not in raw_db
    assert secret_query.encode() not in raw_db


def test_account_lockout_after_failed_logins_and_weak_password_rejected(sec_client):
    client, _ = sec_client
    # Weak password rejected
    tok = _csrf(client.get("/signup").data)
    weak = client.post("/signup", data={
        "csrf_token": tok, "name": "Test", "email": "lock@example.com", "password": "onlyletters"
    })
    assert b"at least one letter and one number or symbol" in weak.data

    # Create confirmed user
    r = client.post("/signup", data={
        "csrf_token": tok, "name": "Test", "email": "lock@example.com", "password": "ValidPass1!"
    })
    m = re.search(rb'href="([^"]*/confirm/[^"]+)"', r.data)
    client.get(m.group(1).decode("utf-8"), follow_redirects=True)

    # 5 wrong passwords trigger lockout
    tok = _csrf(client.get("/login").data)
    for _ in range(5):
        client.post("/login", data={"csrf_token": tok, "email": "lock@example.com", "password": "WrongPassword9!"})

    # 6th attempt even with CORRECT password is locked
    locked = client.post("/login", data={"csrf_token": tok, "email": "lock@example.com", "password": "ValidPass1!"})
    assert b"temporarily locked" in locked.data
