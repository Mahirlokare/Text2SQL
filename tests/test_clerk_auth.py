import base64
import json
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from clerk_auth import ClerkAuthError, ClerkVerifier, decode_frontend_api


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _make_rsa_jwk_and_key(kid: str = "ins_test_123"):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = priv.public_key().public_numbers()
    n_bytes = nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")
    e_bytes = nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")
    jwk = {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": kid, "n": _b64u(n_bytes), "e": _b64u(e_bytes)}
    return priv, jwk


def _sign_jwt(priv, payload: dict, kid: str = "ins_test_123", alg: str = "RS256") -> str:
    hdr = _b64u(json.dumps({"alg": alg, "typ": "JWT", "kid": kid}).encode())
    pld = _b64u(json.dumps(payload).encode())
    signing_input = f"{hdr}.{pld}".encode("ascii")
    sig = priv.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{hdr}.{pld}.{_b64u(sig)}"


def test_decode_frontend_api():
    fapi = decode_frontend_api("pk_test_dGhvcm91Z2gtY2hhbW9pcy0xOTgyLmNsZXJrLmFjY291bnRzLmRldiQ")
    assert fapi == "thorough-chamois-1982.clerk.accounts.dev"


def test_verify_valid_and_tampered_and_expired_jwt():
    priv, jwk = _make_rsa_jwk_and_key()
    v = ClerkVerifier("pk_test_dGhvcm91Z2gtY2hhbW9pcy0xOTgyLmNsZXJrLmFjY291bnRzLmRldiQ")
    now = int(time.time())
    with patch.object(v, "_get_jwks", return_value=[jwk]):
        tok = _sign_jwt(priv, {
            "iss": "https://thorough-chamois-1982.clerk.accounts.dev",
            "sub": "user_abc123",
            "email": "mahir@example.com",
            "name": "Mahir",
            "exp": now + 60,
            "nbf": now - 5,
        })
        claims = v.verify_session_token(tok)
        assert claims["sub"] == "user_abc123"
        assert claims["email"] == "mahir@example.com"

        # Tampered payload must fail signature verification
        parts = tok.split(".")
        tampered = f"{parts[0]}.{_b64u(json.dumps({'iss': v.issuer, 'sub': 'admin'}).encode())}.{parts[2]}"
        with pytest.raises(ClerkAuthError, match="Invalid token signature"):
            v.verify_session_token(tampered)

        # Expired token must fail
        exp_tok = _sign_jwt(priv, {
            "iss": v.issuer,
            "sub": "user_abc123",
            "exp": now - 120,
        })
        with pytest.raises(ClerkAuthError, match="expired"):
            v.verify_session_token(exp_tok)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    priv, jwk = _make_rsa_jwk_and_key()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import web
    web.APP_DB = str(tmp_path / "users.db")
    web.init_db()
    web._clerk = ClerkVerifier("pk_test_dGhvcm91Z2gtY2hhbW9pcy0xOTgyLmNsZXJrLmFjY291bnRzLmRldiQ")
    web._clerk._jwks_cache = (time.time(), [jwk])
    web.app.config["TESTING"] = True
    return web.app.test_client(), priv, web._clerk.issuer


def test_web_auth_routes_and_session(app_client):
    client, priv, issuer = app_client
    # Unauthenticated /app redirects to /login
    assert client.get("/app").status_code == 302
    assert b"clerk-sign-in" in client.get("/login").data
    assert b"clerk-sign-up" in client.get("/signup").data

    # Exchange valid Clerk RS256 JWT via /auth/session
    now = int(time.time())
    tok = _sign_jwt(priv, {"iss": issuer, "sub": "user_999", "email": "u@x.com", "name": "Alice", "exp": now + 60})
    r = client.post("/auth/session", json={"token": tok})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    # Authenticated /app renders workspace with user's name
    dash = client.get("/app")
    assert dash.status_code == 200
    assert b"Hi, Alice" in dash.data
