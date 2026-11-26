"""Security utilities:
- Field-level authenticated encryption at rest (Fernet: AES-128-CBC + HMAC-SHA256)
- HMAC-SHA256 blind indexing for encrypted email lookups
- HKDF-SHA256 key derivation from ENCRYPTION_KEY / SECRET_KEY
- Sliding-window in-memory rate limiter
- Cryptographic CSRF token generation & constant-time verification
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class FieldCipher:
    """Encrypts sensitive database fields at rest and computes keyed blind indexes."""

    def __init__(self, master_secret: str):
        raw = (master_secret or "").encode("utf-8")
        if len(raw) < 16:
            raw = hashlib.sha256(raw).digest()
        enc_bytes = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"querypilot-field-enc-v1",
            info=b"fernet-aes-hmac",
        ).derive(raw)
        idx_bytes = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"querypilot-blind-idx-v1",
            info=b"hmac-email-index",
        ).derive(raw)
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_bytes))
        self._idx_key = idx_bytes

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt((plaintext or "").encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""

    def blind_index(self, value: str) -> str:
        norm = (value or "").strip().lower().encode("utf-8")
        return hmac.new(self._idx_key, norm, hashlib.sha256).hexdigest()


class SlidingWindowLimiter:
    """Per-key sliding-window rate limiter to mitigate brute-force and DoS floods."""

    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        q = self._hits[key]
        cutoff = now - window_seconds
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= limit:
            retry_after = max(1, int(window_seconds - (now - q[0])) + 1)
            return False, retry_after
        q.append(now)
        return True, 0

    def reset(self) -> None:
        self._hits.clear()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


def validate_password_strength(pw: str) -> str | None:
    """Enforces strong passwords; returns error message or None if valid."""
    if len(pw) < 8:
        return "Password must be at least 8 characters long."
    if len(pw) > 128:
        return "Password must be 128 characters or fewer."
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() or not c.isalnum() for c in pw):
        return "Password must include at least one letter and one number or symbol."
    return None
