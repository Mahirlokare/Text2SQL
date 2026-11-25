import os
from unittest.mock import patch
import pytest

from supabase_auth import SupabaseAuth, SupabaseAuthError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-key")
    import web
    web.APP_DB = str(tmp_path / "users.db")
    web.init_db()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_signup_triggers_supabase_email(client):
    with patch.object(SupabaseAuth, "sign_up", return_value={"id": "u-123", "email": "a@b.com", "identities": [{"id": "1"}]}) as mock_su:
        r = client.post("/signup", data={"name": "Alice", "email": "a@b.com", "password": "password123"})
        assert r.status_code == 200
        assert b"Supabase sent a confirmation link" in r.data
        mock_su.assert_called_once()


def test_login_blocks_unconfirmed_email(client):
    with patch.object(SupabaseAuth, "sign_in", side_effect=SupabaseAuthError("Email not confirmed", 400)):
        r = client.post("/login", data={"email": "a@b.com", "password": "password123"})
        assert r.status_code == 200
        assert b"Please confirm your email first" in r.data


def test_callback_token_hash_confirms_and_logs_in(client):
    fake_res = {
        "access_token": "tok-abc",
        "user": {"id": "u-123", "email": "a@b.com", "user_metadata": {"full_name": "Alice"}},
    }
    with patch.object(SupabaseAuth, "verify_otp", return_value=fake_res):
        r = client.get("/auth/callback?token_hash=xyz&type=signup", follow_redirects=False)
        assert r.status_code == 302
        assert "/app" in r.headers["Location"]


def test_fragment_session_exchange(client):
    fake_user = {"id": "u-123", "email": "a@b.com", "user_metadata": {"full_name": "Alice"}}
    with patch.object(SupabaseAuth, "get_user", return_value=fake_user):
        r = client.post("/auth/session", json={"access_token": "tok-abc"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True


def test_login_success_redirects_to_dashboard(client):
    fake_res = {
        "access_token": "tok-abc",
        "user": {"id": "u-123", "email": "a@b.com", "user_metadata": {"full_name": "Alice"}},
    }
    with patch.object(SupabaseAuth, "sign_in", return_value=fake_res):
        r = client.post("/login", data={"email": "a@b.com", "password": "password123"}, follow_redirects=False)
        assert r.status_code == 302
        assert "/app" in r.headers["Location"]
