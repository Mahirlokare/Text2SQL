"""QueryPilot AI web app: Supabase Auth (signup/email confirm/login) + text-to-SQL workspace."""
import os
import re
import sqlite3
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from core.engine import Text2SQLEngine
from core.llm import SQLGenerator
from supabase_auth import SupabaseAuth, SupabaseAuthError

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
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_engine = None


def get_auth() -> SupabaseAuth:
    return SupabaseAuth()


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


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def _set_user_session(user: dict, access_token: str | None = None):
    meta = user.get("user_metadata") or {}
    email = user.get("email") or ""
    name = meta.get("full_name") or meta.get("name") or (email.split("@")[0] if email else "User")
    session.clear()
    session["uid"] = str(user.get("id"))
    session["email"] = email
    session["name"] = name
    if access_token:
        session["sb_token"] = access_token


# ---------- auth (Supabase) ----------
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "uid" in session else url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    auth = get_auth()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not name or not EMAIL_RE.match(email):
            flash("Enter your name and a valid email.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            try:
                callback_url = url_for("auth_callback", _external=True)
                res = auth.sign_up(email, pw, name, redirect_to=callback_url)
                # Supabase returns an empty identities list when obfuscating an existing account
                user_obj = res.get("user") if isinstance(res.get("user"), dict) else res
                identities = user_obj.get("identities")
                if isinstance(identities, list) and len(identities) == 0:
                    flash("An account with this email already exists. Try signing in instead.", "error")
                    return render_template("signup.html", sb_ready=auth.configured)
                # If email confirmation is disabled in Supabase, an access_token is returned immediately
                if res.get("access_token") and user_obj.get("id"):
                    _set_user_session(user_obj, res["access_token"])
                    flash("Account created and signed in!", "success")
                    return redirect(url_for("dashboard"))
                return render_template(
                    "message.html",
                    title="Check your inbox",
                    email=email,
                    body="Supabase sent a confirmation link to your email. Click it to activate your account and sign in.",
                )
            except SupabaseAuthError as e:
                flash(str(e), "error")
    return render_template("signup.html", sb_ready=auth.configured)


@app.route("/auth/callback")
def auth_callback():
    """Handles Supabase email confirmation redirects (both ?token_hash=... and #access_token=...)."""
    auth = get_auth()
    err_desc = request.args.get("error_description") or request.args.get("error")
    if err_desc:
        flash(err_desc, "error")
        return redirect(url_for("login"))

    token_hash = request.args.get("token_hash")
    otp_type = request.args.get("type", "signup")
    if token_hash:
        try:
            res = auth.verify_otp(token_hash, otp_type)
            user_obj = res.get("user") or {}
            if res.get("access_token") and user_obj.get("id"):
                _set_user_session(user_obj, res["access_token"])
                flash("Email confirmed! Welcome to QueryPilot AI.", "success")
                return redirect(url_for("dashboard"))
            flash("Email confirmed! You can sign in now.", "success")
        except SupabaseAuthError as e:
            flash(f"Confirmation failed: {e}", "error")
        return redirect(url_for("login"))

    # Default Supabase email link puts #access_token=... in the URL fragment
    return render_template("auth_callback.html")


@app.route("/auth/session", methods=["POST"])
def auth_session():
    """Receives #access_token from auth_callback.html, verifies it with Supabase, and logs the user in."""
    auth = get_auth()
    payload = request.get_json(silent=True) or {}
    access_token = (payload.get("access_token") or "").strip()
    if not access_token:
        return jsonify({"ok": False, "error": "Missing access token"}), 400
    try:
        user = auth.get_user(access_token)
        if not user.get("id"):
            return jsonify({"ok": False, "error": "Invalid user"}), 401
        _set_user_session(user, access_token)
        flash("Email confirmed! Welcome to QueryPilot AI.", "success")
        return jsonify({"ok": True, "redirect": url_for("dashboard")})
    except SupabaseAuthError as e:
        return jsonify({"ok": False, "error": str(e)}), 401


@app.route("/resend", methods=["GET", "POST"])
def resend():
    auth = get_auth()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not EMAIL_RE.match(email):
            flash("Enter a valid email address.", "error")
        else:
            try:
                auth.resend_signup_confirmation(email, redirect_to=url_for("auth_callback", _external=True))
                flash("If that account exists and is unconfirmed, Supabase sent a new confirmation link.", "success")
                return redirect(url_for("login"))
            except SupabaseAuthError as e:
                flash(str(e), "error")
    return render_template("resend.html", sb_ready=auth.configured)


@app.route("/login", methods=["GET", "POST"])
def login():
    auth = get_auth()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        try:
            res = auth.sign_in(email, pw)
            user = res.get("user") or {}
            if not user.get("id"):
                flash("Invalid email or password.", "error")
            else:
                _set_user_session(user, res.get("access_token"))
                return redirect(url_for("dashboard"))
        except SupabaseAuthError as e:
            msg = str(e)
            if "email not confirmed" in msg.lower():
                flash("Please confirm your email first. Check your inbox or click 'Resend confirmation'.", "error")
            elif e.status in (400, 401):
                flash("Invalid email or password.", "error")
            else:
                flash(msg, "error")
    return render_template("login.html", sb_ready=auth.configured)


@app.route("/logout")
def logout():
    tok = session.get("sb_token")
    if tok:
        get_auth().sign_out(tok)
    session.clear()
    return redirect(url_for("login"))


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
