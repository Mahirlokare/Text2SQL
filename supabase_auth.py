"""Supabase Auth (GoTrue) client wrapper for sign-up, login, confirmation & resend."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class SupabaseAuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SupabaseAuth:
    def __init__(self, url: str | None = None, anon_key: str | None = None):
        self.url = (url or os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.anon_key = (anon_key or os.getenv("SUPABASE_ANON_KEY") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key)

    def _request(self, method: str, path: str, body: dict | None = None,
                 bearer: str | None = None) -> dict:
        if not self.configured:
            raise SupabaseAuthError(
                "Supabase Auth is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY.", 503)
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {bearer or self.anon_key}",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(raw)
                msg = (err.get("msg") or err.get("error_description")
                       or err.get("message") or err.get("error") or raw)
            except Exception:
                msg = raw or str(e)
            raise SupabaseAuthError(str(msg), e.code) from e
        except urllib.error.URLError as e:
            raise SupabaseAuthError(f"Could not reach Supabase: {e.reason}", 502) from e

    def sign_up(self, email: str, password: str, name: str, redirect_to: str) -> dict:
        qs = urllib.parse.urlencode({"redirect_to": redirect_to})
        return self._request(
            "POST",
            f"/auth/v1/signup?{qs}",
            {"email": email, "password": password, "data": {"full_name": name}},
        )

    def sign_in(self, email: str, password: str) -> dict:
        return self._request(
            "POST",
            "/auth/v1/token?grant_type=password",
            {"email": email, "password": password},
        )

    def resend_signup_confirmation(self, email: str, redirect_to: str) -> dict:
        return self._request(
            "POST",
            "/auth/v1/resend",
            {"type": "signup", "email": email, "options": {"emailRedirectTo": redirect_to}},
        )

    def verify_otp(self, token_hash: str, otp_type: str = "signup") -> dict:
        return self._request(
            "POST",
            "/auth/v1/verify",
            {"token_hash": token_hash, "type": otp_type},
        )

    def get_user(self, access_token: str) -> dict:
        return self._request("GET", "/auth/v1/user", bearer=access_token)

    def sign_out(self, access_token: str) -> None:
        try:
            self._request("POST", "/auth/v1/logout", bearer=access_token)
        except SupabaseAuthError:
            pass
