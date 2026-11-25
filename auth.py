"""Auth0 ServerClient setup with stateless JWE-encrypted Flask session stores."""
import hashlib
import os

from auth0_server_python.auth_server.server_client import ServerClient
from auth0_server_python.auth_types import StateData, TransactionData
from auth0_server_python.store import StateStore, TransactionStore
from dotenv import load_dotenv
from flask import session as flask_session

load_dotenv()


def get_auth0_secret() -> str:
    raw = os.getenv("AUTH0_SECRET") or os.getenv("SECRET_KEY") or "dev-only-change-me"
    # Ensure >= 64 hex chars (32+ bytes) for JWE encryption
    if len(raw) < 32:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw


class FlaskSessionStateStore(StateStore):
    """State store that persists JWE-encrypted Auth0 session state in Flask's session."""

    def __init__(self, secret: str):
        super().__init__({"secret": secret})

    async def set(self, identifier, state, remove_if_expires=False, options=None):
        if hasattr(state, "model_dump"):
            data = state.model_dump()
        elif hasattr(state, "dict"):
            data = state.dict()
        else:
            data = state
        flask_session[identifier] = self.encrypt(identifier, data)

    async def get(self, identifier, options=None):
        data = flask_session.get(identifier)
        if data is None:
            return None
        decrypted = self.decrypt(identifier, data)
        return StateData(**decrypted) if isinstance(decrypted, dict) else decrypted

    async def delete(self, identifier, options=None):
        flask_session.pop(identifier, None)

    async def delete_by_logout_token(self, claims, options=None):
        pass


class FlaskSessionTransactionStore(TransactionStore):
    """Transaction store that persists PKCE/OIDC transaction data in Flask's session."""

    def __init__(self, secret: str):
        super().__init__({"secret": secret})

    async def set(self, identifier, state, remove_if_expires=False, options=None):
        if hasattr(state, "model_dump"):
            data = state.model_dump()
        elif hasattr(state, "dict"):
            data = state.dict()
        else:
            data = state
        flask_session[identifier] = self.encrypt(identifier, data)

    async def get(self, identifier, options=None):
        data = flask_session.get(identifier)
        if data is None:
            return None
        decrypted = self.decrypt(identifier, data)
        return TransactionData(**decrypted) if isinstance(decrypted, dict) else decrypted

    async def delete(self, identifier, options=None):
        flask_session.pop(identifier, None)


def is_auth0_configured() -> bool:
    return bool(
        os.getenv("AUTH0_DOMAIN")
        and os.getenv("AUTH0_CLIENT_ID")
        and os.getenv("AUTH0_CLIENT_SECRET")
    )


_client: ServerClient | None = None


def get_auth0_client(redirect_uri: str | None = None) -> ServerClient:
    global _client
    if _client is not None:
        return _client
    domain = (os.getenv("AUTH0_DOMAIN") or "").strip().removeprefix("https://").rstrip("/")
    client_id = (os.getenv("AUTH0_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("AUTH0_CLIENT_SECRET") or "").strip()
    secret = get_auth0_secret()
    resolved_redirect = (
        os.getenv("AUTH0_REDIRECT_URI")
        or redirect_uri
        or "http://localhost:5000/callback"
    ).strip()
    _client = ServerClient(
        domain=domain,
        client_id=client_id,
        client_secret=client_secret,
        secret=secret,
        redirect_uri=resolved_redirect,
        state_store=FlaskSessionStateStore(secret=secret),
        transaction_store=FlaskSessionTransactionStore(secret=secret),
        authorization_params={"scope": "openid profile email"},
    )
    return _client
