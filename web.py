"""QueryPilot AI web app: Auth0 (signup/email confirm/login) + text-to-SQL workspace."""
import os
import sqlite3
from functools import wraps

from auth0_server_python.auth_types import LogoutOptions, StartInteractiveLoginOptions
from dotenv import load_dotenv
from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import get_auth0_client, get_auth0_secret, is_auth0_configured
from core.engine import Text2SQLEngine
from core.llm import SQLGenerator

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
APP_DB = os.path.join(DATA, "users.db")
DEMO_DB = os.path.join(BASE, "data", "shop.db")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = get_auth0_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("RENDER") is not None,
)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = Text2SQLEngine(DEMO_DB, SQLGenerator())
    return _engine


# ---------- local history db ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(APP_DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    c = sqlite3.connect(APP_DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, user_id TEXT, question TEXT,
      sql TEXT, ok INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    c.commit()
    c.close()


init_db()


async def _current_user() -> dict | None:
    if not is_auth0_configured():
        return None
    client = get_auth0_client(url_for("callback", _external=True))
    return await client.get_user()


def _sync_display_session(user: dict) -> None:
    email = (user.get("email") or "").strip()
    name = (
        user.get("name")
        or user.get("nickname")
        or (email.split("@")[0] if email else "User")
    ).strip()
    session["uid"] = str(user.get("sub"))
    session["email"] = email
    session["name"] = name


def require_verified_user(f):
    @wraps(f)
    async def w(*a, **k):
        user = await _current_user()
        if user is None:
            return redirect(url_for("login"))
        _sync_display_session(user)
        if user.get("email_verified") is False:
            return redirect(url_for("verify_email"))
        return await f(*a, **k)
    return w


# ---------- auth (Auth0) ----------
@app.route("/")
async def index():
    user = await _current_user()
    if user:
        _sync_display_session(user)
        if user.get("email_verified") is False:
            return redirect(url_for("verify_email"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login")
async def login():
    user = await _current_user()
    if user:
        _sync_display_session(user)
        if user.get("email_verified") is False:
            return redirect(url_for("verify_email"))
        return redirect(url_for("dashboard"))
    return render_template("login.html", auth0_ready=is_auth0_configured())


@app.route("/signup")
async def signup():
    user = await _current_user()
    if user:
        _sync_display_session(user)
        if user.get("email_verified") is False:
            return redirect(url_for("verify_email"))
        return redirect(url_for("dashboard"))
    return render_template("signup.html", auth0_ready=is_auth0_configured())


@app.route("/auth/login")
async def auth_login():
    if not is_auth0_configured():
        flash("Auth0 is not configured yet. Set AUTH0_DOMAIN, AUTH0_CLIENT_ID, and AUTH0_CLIENT_SECRET.", "error")
        return redirect(url_for("login"))
    client = get_auth0_client(url_for("callback", _external=True))
    prompt = request.args.get("prompt")
    opts = (
        StartInteractiveLoginOptions(authorization_params={"prompt": prompt})
        if prompt
        else None
    )
    authorization_url = await client.start_interactive_login(options=opts) if opts else await client.start_interactive_login()
    return redirect(authorization_url)


@app.route("/auth/signup")
async def auth_signup():
    if not is_auth0_configured():
        flash("Auth0 is not configured yet. Set AUTH0_DOMAIN, AUTH0_CLIENT_ID, and AUTH0_CLIENT_SECRET.", "error")
        return redirect(url_for("signup"))
    client = get_auth0_client(url_for("callback", _external=True))
    authorization_url = await client.start_interactive_login(
        options=StartInteractiveLoginOptions(
            authorization_params={"screen_hint": "signup"}
        )
    )
    return redirect(authorization_url)


@app.route("/callback")
async def callback():
    if not is_auth0_configured():
        return redirect(url_for("login"))
    client = get_auth0_client(url_for("callback", _external=True))
    try:
        await client.complete_interactive_login(str(request.url))
        user = await client.get_user()
        if user:
            _sync_display_session(user)
            if user.get("email_verified") is False:
                return redirect(url_for("verify_email"))
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Authentication error: {e}", "error")
        return redirect(url_for("login"))


@app.route("/verify-email")
async def verify_email():
    user = await _current_user()
    if user is None:
        return redirect(url_for("login"))
    _sync_display_session(user)
    if user.get("email_verified") is True:
        return redirect(url_for("dashboard"))
    return render_template("verify_email.html", email=user.get("email") or "")


@app.route("/logout")
async def logout():
    if is_auth0_configured():
        try:
            client = get_auth0_client(url_for("callback", _external=True))
            return_to = url_for("login", _external=True)
            logout_url = await client.logout(options=LogoutOptions(return_to=return_to))
            session.clear()
            return redirect(logout_url)
        except Exception:
            pass
    session.clear()
    return redirect(url_for("login"))


# ---------- workspace ----------
@app.route("/app", methods=["GET", "POST"])
@require_verified_user
async def dashboard():
    result = None
    try:
        eng = get_engine()
    except Exception as e:  # missing API key etc.
        flash(f"Engine unavailable: {e}", "error")
        eng = None
    if request.method == "POST" and eng:
        q = request.form.get("question", "").strip()[:500]
        if q:
            r = eng.ask(q)
            result = {"q": q, "sql": r.sql, "err": r.error, "attempts": r.attempts,
                      "latency": r.latency_s,
                      "cols": list(r.df.columns) if r.df is not None else [],
                      "rows": r.df.head(200).values.tolist() if r.df is not None else [],
                      "total": len(r.df) if r.df is not None else 0}
            db().execute("INSERT INTO history(user_id,question,sql,ok) VALUES(?,?,?,?)",
                         (session["uid"], q, r.sql, 0 if r.error else 1))
            db().commit()
            session["last_q"] = q
    hist = db().execute("SELECT question,sql,ok,created_at FROM history WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 8", (session["uid"],)).fetchall()
    return render_template("dashboard.html", result=result, hist=hist,
                           schema=eng.schema if eng else "")


@app.route("/export.csv")
@require_verified_user
async def export():
    eng = get_engine()
    q = session.get("last_q")
    if not q:
        return redirect(url_for("dashboard"))
    r = eng.ask(q)
    if r.df is None:
        return redirect(url_for("dashboard"))
    return Response(r.df.to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=result.csv"})


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
