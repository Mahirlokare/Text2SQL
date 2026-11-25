"""Clerk authentication helper: publishable-key decoding & RS256 JWKS JWT verification."""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DEFAULT_CLERK_PK = "pk_test_dGhvcm91Z2gtY2hhbW9pcy0xOTgyLmNsZXJrLmFjY291bnRzLmRldiQ"


class ClerkAuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _b64url_decode(data: str) -> bytes:
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data.encode("ascii"))


def decode_frontend_api(publishable_key: str) -> str:
    """Extracts the Clerk Frontend API domain from pk_test_... or pk_live_..."""
    pk = (publishable_key or "").strip()
    for prefix in ("pk_test_", "pk_live_"):
        if pk.startswith(prefix):
            raw = _b64url_decode(pk[len(prefix):]).decode("utf-8", errors="strict")
            return raw.rstrip("$")
    raise ClerkAuthError("Invalid CLERK_PUBLISHABLE_KEY format.", 500)


class ClerkVerifier:
    def __init__(self, publishable_key: str | None = None):
        self.publishable_key = (
            publishable_key
            or os.getenv("CLERK_PUBLISHABLE_KEY")
            or DEFAULT_CLERK_PK
        ).strip()
        self.frontend_api = decode_frontend_api(self.publishable_key)
        self.issuer = f"https://{self.frontend_api}"
        self._jwks_cache: tuple[float, list[dict]] | None = None

    def _get_jwks(self, force_refresh: bool = False) -> list[dict]:
        now = time.time()
        if not force_refresh and self._jwks_cache and (now - self._jwks_cache[0] < 3600):
            return self._jwks_cache[1]
        url = f"{self.issuer}/.well-known/jwks.json"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "QueryPilotAI/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                keys = data.get("keys") or []
                self._jwks_cache = (now, keys)
                return keys
        except Exception as e:
            raise ClerkAuthError(f"Unable to fetch Clerk JWKS: {e}", 502) from e

    @staticmethod
    def _jwk_to_public_key(jwk: dict):
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        return rsa.RSAPublicNumbers(e, n).public_key()

    def verify_session_token(self, token: str, clock_skew: int = 15) -> dict:
        """Verifies a Clerk RS256 session JWT against the instance JWKS and returns its claims."""
        parts = (token or "").strip().split(".")
        if len(parts) != 3:
            raise ClerkAuthError("Malformed session token.")
        header_b64, payload_b64, sig_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
            payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
            signature = _b64url_decode(sig_b64)
        except Exception as e:
            raise ClerkAuthError("Invalid token encoding.") from e

        if header.get("alg") != "RS256":
            raise ClerkAuthError("Unsupported JWT algorithm.")

        kid = header.get("kid")
        keys = self._get_jwks()
        jwk = next((k for k in keys if k.get("kid") == kid and k.get("kty") == "RSA"), None)
        if jwk is None:
            keys = self._get_jwks(force_refresh=True)
            jwk = next((k for k in keys if k.get("kid") == kid and k.get("kty") == "RSA"), None)
        if jwk is None:
            raise ClerkAuthError("Signing key not found in Clerk JWKS.")

        pub_key = self._jwk_to_public_key(jwk)
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        try:
            pub_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as e:
            raise ClerkAuthError("Invalid token signature.") from e

        now = time.time()
        if payload.get("iss") != self.issuer:
            raise ClerkAuthError("Invalid token issuer.")
        if "exp" in payload and now > float(payload["exp"]) + clock_skew:
            raise ClerkAuthError("Session token expired.")
        if "nbf" in payload and now + clock_skew < float(payload["nbf"]):
            raise ClerkAuthError("Session token not yet valid.")
        if not payload.get("sub"):
            raise ClerkAuthError("Missing subject claim in token.")
        return payload
