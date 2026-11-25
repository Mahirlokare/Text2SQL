"""QueryPilot AI web app: Clerk Auth (signup/email confirm/login) + text-to-SQL workspace."""
import os
import sqlite3
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from clerk_auth import ClerkAuthError, ClerkVerifier
from core.engine import Text2SQLEngine
from core.llm import SQLGenerator

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
APP_DB = os.path.join(DATA, "users.db")
DEMO_DB = os.path.join(BASE, "data", "shop.db")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("RENDER") is not None)

_engine = None
_clerk = None


def get_clerk() -> ClerkVerifier:
    global _clerk
    if _clerk is None:
        _clerk = ClerkVerifier()
    return _clerk


@app.context_processor
def inject_clerk():
    try:
        c = get_clerk()
        return {"clerk_pk": c.publishable_key, "clerk_fapi": c.frontend_api}
    except Exception:
        return {"clerk_pk": "", "clerk_fapi": ""}


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


def _apply_claims_to_session(claims: dict, fallback_email: str = "", fallback_name: str = ""):
    uid = str(claims["sub"])
    email = (claims.get("email") or fallback_email or "").strip()
    name = (
        claims.get("name")
        or claims.get("first_name")
        or fallback_name
        or (email.split("@")[0] if email else "User")
    ).strip()
    session["uid"] = uid
    session["email"] = email
    session["name"] = name


def _try_cookie_session() -> bool:
    """If Flask session isn't populated yet, verify Clerk's same-origin __session JWT cookie."""
    if "uid" in session:
        return True
    raw_jwt = request.cookies.get("__session")
    if not raw_jwt:
        return False
    try:
        claims = get_clerk().verify_session_token(raw_jwt)
        _apply_claims_to_session(claims)
        return True
    except ClerkAuthError:
        return False


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not _try_cookie_session():
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


# ---------- auth (Clerk) ----------
@app.route("/")
def index():
    return redirect(url_for("dashboard") if _try_cookie_session() else url_for("login"))


@app.route("/login")
def login():
    if _try_cookie_session():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/signup")
def signup():
    if _try_cookie_session():
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/auth/callback")
def auth_callback():
    if _try_cookie_session():
        return redirect(url_for("dashboard"))
    return render_template("auth_callback.html")


@app.route("/auth/session", methods=["POST"])
def auth_session():
    """Verifies a Clerk RS256 session token and establishes the server session."""
    payload = request.get_json(silent=True) or {}
    token = (payload.get("token") or request.cookies.get("__session") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Missing Clerk session token"}), 400
    try:
        claims = get_clerk().verify_session_token(token)
        session.clear()
        _apply_claims_to_session(
            claims,
            fallback_email=payload.get("email") or "",
            fallback_name=payload.get("name") or "",
        )
        return jsonify({"ok": True, "redirect": url_for("dashboard")})
    except ClerkAuthError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status


@app.route("/logout")
def logout():
    session.clear()
    return render_template("logout.html")


# ---------- workspace ----------
@app.route("/app", methods=["GET", "POST"])
@login_required
def dashboard():
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
@login_required
def export():
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
