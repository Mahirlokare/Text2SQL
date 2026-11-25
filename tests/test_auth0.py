from unittest.mock import AsyncMock, MagicMock, patch
import pytest


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH0_DOMAIN", "example.us.auth0.com")
    monkeypatch.setenv("AUTH0_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("AUTH0_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("AUTH0_SECRET", "a" * 64)
    monkeypatch.setenv("AUTH0_REDIRECT_URI", "http://localhost:5000/callback")

    import auth
    import web
    auth._client = None
    web.APP_DB = str(tmp_path / "users.db")
    web.init_db()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_login_and_signup_pages_render(app_client):
    fake = MagicMock()
    fake.get_user = AsyncMock(return_value=None)
    with patch("web.get_auth0_client", return_value=fake):
        assert b"Continue to Sign in" in app_client.get("/login").data
        assert b"Create Account with Auth0" in app_client.get("/signup").data


def test_auth_signup_passes_signup_screen_hint(app_client):
    fake = MagicMock()
    fake.start_interactive_login = AsyncMock(return_value="https://example.us.auth0.com/authorize?screen_hint=signup")
    with patch("web.get_auth0_client", return_value=fake):
        r = app_client.get("/auth/signup")
        assert r.status_code == 302
        assert "screen_hint=signup" in r.headers["Location"]
        fake.start_interactive_login.assert_awaited_once()


def test_unverified_user_redirected_to_verify_email(app_client):
    fake = MagicMock()
    fake.complete_interactive_login = AsyncMock()
    fake.get_user = AsyncMock(return_value={
        "sub": "auth0|123",
        "email": "newuser@example.com",
        "name": "New User",
        "email_verified": False,
    })
    with patch("web.get_auth0_client", return_value=fake):
        r = app_client.get("/callback?code=abc&state=xyz")
        assert r.status_code == 302
        assert "/verify-email" in r.headers["Location"]

        # Direct access to /app is also gated until email_verified is True
        app_r = app_client.get("/app")
        assert app_r.status_code == 302
        assert "/verify-email" in app_r.headers["Location"]

        ver_page = app_client.get("/verify-email")
        assert ver_page.status_code == 200
        assert b"newuser@example.com" in ver_page.data


def test_verified_user_accesses_dashboard(app_client):
    fake = MagicMock()
    fake.get_user = AsyncMock(return_value={
        "sub": "auth0|123",
        "email": "verified@example.com",
        "name": "Mahir",
        "email_verified": True,
    })
    with patch("web.get_auth0_client", return_value=fake):
        r = app_client.get("/app")
        assert r.status_code == 200
        assert b"Hi, Mahir" in r.data
