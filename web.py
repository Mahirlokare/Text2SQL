"""QueryPilot AI web app: hardened self-contained auth (encryption at rest, CSRF, rate limiting,
lockout, strict CSP, zero external fonts/CDNs) + Universal Multi-Dialect Text-to-SQL Query Agent."""
import os
import re
import secrets
import smtplib
import sqlite3
import time
from datetime import timedelta
from email.message import EmailMessage
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from core.engine import Text2SQLEngine
from core.llm import SQLGenerator
from core.security import (FieldCipher, SlidingWindowLimiter, new_csrf_token,
                           validate_password_strength, verify_csrf_token)
from core.validator import DIALECT_MAP

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
USERS_DB = os.path.join(DATA, "users.db")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("RENDER") is not None,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=32 * 1024,  # 32 KB max request body
)

_cipher: FieldCipher | None = None
_limiter = SlidingWindowLimiter()
_engine: Text2SQLEngine | None = None
_DUMMY_HASH = generate_password_hash("dummy-timing-shield-password-999!")

ts = URLSafeTimedSerializer(app.secret_key, salt="email-confirm-v2")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 900  # 15 minutes


def get_cipher() -> FieldCipher:
    global _cipher
    if _cipher is None:
        key_material = os.getenv("ENCRYPTION_KEY") or app.secret_key
        _cipher = FieldCipher(key_material)
    return _cipher


def get_engine() -> Text2SQLEngine:
    global _engine
    if _engine is None:
        _engine = Text2SQLEngine(SQLGenerator())
    return _engine


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ---------- CSRF + Security Headers ----------
def get_csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = new_csrf_token()
        session["_csrf"] = tok
    return tok


@app.context_processor
def inject_security():
    return {"csrf_token": get_csrf_token()}


@app.before_request
def enforce_csrf_and_rate_limits():
    if request.method == "POST":
        expected = session.get("_csrf")
        provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not verify_csrf_token(expected, provided):
            abort(403, description="Invalid or missing CSRF token.")

        ip = _client_ip()
        path = request.path
        if path in ("/login", "/signup", "/resend"):
            ok, retry = _limiter.check(f"auth:{path}:{ip}", limit=15, window_seconds=300)
            if not ok:
                return Response(
                    "Too many requests. Please wait before trying again.",
                    status=429,
                    headers={"Retry-After": str(retry)},
                )
        elif path == "/app":
            ok, retry = _limiter.check(f"query:{ip}", limit=40, window_seconds=300)
            if not ok:
                return Response(
                    "Rate limit exceeded. Please slow down and try again shortly.",
                    status=429,
                    headers={"Retry-After": str(retry)},
                )


@app.after_request
def add_security_headers(resp: Response) -> Response:
    # Zero external fonts, zero external scripts, zero inline scripts/styles, anti-clickjacking
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.is_secure or os.getenv("RENDER") is not None:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        resp.headers["Pragma"] = "no-cache"
    return resp


# ---------- encrypted account & history store ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(USERS_DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    c = sqlite3.connect(USERS_DB)
    # Use encrypted v2 schema (no plaintext email, name, question, or SQL at rest)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users_v2(
      id INTEGER PRIMARY KEY,
      email_hash TEXT UNIQUE NOT NULL,
      email_enc TEXT NOT NULL,
      name_enc TEXT NOT NULL,
      password_hash TEXT NOT NULL,
      confirmed INTEGER DEFAULT 0,
      confirm_nonce TEXT,
      failed_attempts INTEGER DEFAULT 0,
      locked_until INTEGER DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS history_v2(
      id INTEGER PRIMARY KEY,
      user_id TEXT NOT NULL,
      dialect TEXT DEFAULT 'PostgreSQL',
      question_enc TEXT NOT NULL,
      sql_enc TEXT NOT NULL,
      ok INTEGER DEFAULT 1,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_history_v2_user ON history_v2(user_id, id DESC);
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


def _try_webhook_email(to_email: str, subject: str, html: str) -> bool:
    """Sends email over HTTPS via the user's own free Google Apps Script Web App (works on Render free tier)."""
    import json
    import urllib.request

    url = (os.getenv("MAIL_WEBHOOK_URL") or "").strip()
    if not url:
        return False
    secret = (os.getenv("MAIL_WEBHOOK_SECRET") or "").strip()
    payload = json.dumps({
        "secret": secret,
        "to": to_email,
        "subject": subject,
        "html": html,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "QueryPilotAI/2.1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 400


def _try_smtp_email(to_email: str, subject: str, html: str) -> bool:
    host = (os.getenv("SMTP_HOST") or "").strip()
    sender = (os.getenv("SMTP_USER") or os.getenv("MAIL_FROM") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    if not (host and sender and password):
        return False
    msg = EmailMessage()
    msg["From"] = f"QueryPilot AI <{sender}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content("Confirm your QueryPilot AI account using an HTML-capable mail reader.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as s:
        s.starttls()
        s.login(sender, password)
        s.send_message(msg)
    return True


def issue_confirmation(user_id: int, email: str, name: str) -> tuple[bool, str]:
    nonce = secrets.token_urlsafe(18)
    db().execute("UPDATE users_v2 SET confirm_nonce=? WHERE id=?", (nonce, user_id))
    db().commit()
    token = ts.dumps({"uid": user_id, "n": nonce})
    link = url_for("confirm", token=token, _external=True)
    html = (
        f"<div style='font-family:sans-serif'><h2>Welcome to QueryPilot AI, {name}!</h2>"
        f"<p>Confirm your account by clicking the link below:</p>"
        f"<p><a href='{link}'>{link}</a></p>"
        f"<p>This single-use link expires in 24 hours.</p></div>"
    )
    try:
        sent = _try_webhook_email(email, "Confirm your QueryPilot AI account", html) or _try_smtp_email(
            email, "Confirm your QueryPilot AI account", html
        )
    except Exception as e:
        app.logger.warning("Email delivery skipped/failed: %s", e)
        sent = False
    return sent, link


# ---------- auth ----------
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "uid" in session else url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "uid" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        email = request.form.get("email", "").strip().lower()[:254]
        pw = request.form.get("password", "")
        pw_err = validate_password_strength(pw)
        cipher = get_cipher()
        email_hash = cipher.blind_index(email)

        if not name or not EMAIL_RE.match(email):
            flash("Enter your name and a valid email.", "error")
        elif pw_err:
            flash(pw_err, "error")
        elif db().execute("SELECT 1 FROM users_v2 WHERE email_hash=?", (email_hash,)).fetchone():
            flash("An account with this email already exists.", "error")
        else:
            cur = db().execute(
                "INSERT INTO users_v2(email_hash,email_enc,name_enc,password_hash) VALUES(?,?,?,?)",
                (email_hash, cipher.encrypt(email), cipher.encrypt(name), generate_password_hash(pw)),
            )
            db().commit()
            sent, link = issue_confirmation(cur.lastrowid, email, name)
            return render_template(
                "verify_email.html",
                email=email,
                name=name,
                sent=sent,
                confirm_link=None if sent else link,
            )
    return render_template("signup.html")


@app.route("/confirm/<token>")
def confirm(token):
    try:
        data = ts.loads(token, max_age=86400)
        uid = int(data["uid"])
        nonce = str(data["n"])
    except SignatureExpired:
        flash("That confirmation link expired. Request a new one below.", "error")
        return redirect(url_for("resend"))
    except (BadSignature, KeyError, TypeError, ValueError):
        flash("Invalid confirmation link.", "error")
        return redirect(url_for("login"))

    u = db().execute("SELECT id, confirm_nonce, confirmed FROM users_v2 WHERE id=?", (uid,)).fetchone()
    if not u or not u["confirm_nonce"] or not verify_csrf_token(u["confirm_nonce"], nonce):
        flash("This confirmation link has already been used or is invalid.", "error")
        return redirect(url_for("login"))

    db().execute("UPDATE users_v2 SET confirmed=1, confirm_nonce=NULL WHERE id=?", (uid,))
    db().commit()
    flash("Account confirmed! You can sign in now.", "success")
    return redirect(url_for("login"))


@app.route("/resend", methods=["GET", "POST"])
def resend():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        cipher = get_cipher()
        u = db().execute("SELECT * FROM users_v2 WHERE email_hash=?", (cipher.blind_index(email),)).fetchone()
        if u and not u["confirmed"]:
            name = cipher.decrypt(u["name_enc"])
            sent, link = issue_confirmation(u["id"], email, name)
            return render_template(
                "verify_email.html",
                email=email,
                name=name,
                sent=sent,
                confirm_link=None if sent else link,
            )
        flash("If that account exists and is unconfirmed, a confirmation link was generated.", "info")
        return redirect(url_for("login"))
    return render_template("resend.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "uid" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        cipher = get_cipher()
        u = db().execute("SELECT * FROM users_v2 WHERE email_hash=?", (cipher.blind_index(email),)).fetchone()
        now = int(time.time())

        if u and u["locked_until"] and now < int(u["locked_until"]):
            mins = max(1, (int(u["locked_until"]) - now + 59) // 60)
            flash(f"Account temporarily locked after too many failed attempts. Try again in {mins} minute(s).", "error")
            return render_template("login.html")

        # Constant-time verification even when user doesn't exist
        pw_ok = check_password_hash(u["password_hash"], pw) if u else check_password_hash(_DUMMY_HASH, pw) and False
        if not u or not pw_ok:
            if u:
                fails = int(u["failed_attempts"] or 0) + 1
                lock_until = now + LOCKOUT_SECONDS if fails >= MAX_FAILED_LOGINS else 0
                db().execute(
                    "UPDATE users_v2 SET failed_attempts=?, locked_until=? WHERE id=?",
                    (fails, lock_until, u["id"]),
                )
                db().commit()
            flash("Invalid email or password.", "error")
        elif not u["confirmed"]:
            flash("Please confirm your account first (or click 'Resend confirmation' below).", "error")
        else:
            db().execute("UPDATE users_v2 SET failed_attempts=0, locked_until=0 WHERE id=?", (u["id"],))
            db().commit()
            session.clear()
            session.permanent = True
            session["_csrf"] = new_csrf_token()
            session["uid"] = str(u["id"])
            session["name"] = cipher.decrypt(u["name_enc"])
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- universal SQL query agent workspace ----------
@app.route("/app", methods=["GET", "POST"])
@login_required
def dashboard():
    result = None
    eng = get_engine()
    cipher = get_cipher()
    selected_dialect = session.get("dialect", "postgresql")

    if request.method == "POST":
        q = request.form.get("question", "").strip()[:2000]
        selected_dialect = request.form.get("dialect", "postgresql").strip().lower()
        if selected_dialect not in DIALECT_MAP:
            selected_dialect = "postgresql"
        session["dialect"] = selected_dialect

        if q:
            user_hist = [tuple(x) for x in session.get("sql_hist", [])]
            r = eng.ask(q, dialect=selected_dialect, schema_hint="", history=user_hist)
            result = {
                "q": q,
                "dialect": r.dialect,
                "dialect_label": r.dialect_label,
                "sql": r.sql,
                "explanation": r.explanation,
                "assumed_schema": r.assumed_schema,
                "syntax_valid": r.syntax_valid,
                "err": r.error,
                "latency": r.latency_s,
            }
            if not r.error and r.sql:
                user_hist.append((q, r.sql))
                session["sql_hist"] = user_hist[-4:]
                session["last_sql"] = r.sql
            db().execute(
                "INSERT INTO history_v2(user_id,dialect,question_enc,sql_enc,ok) VALUES(?,?,?,?,?)",
                (session["uid"], r.dialect_label, cipher.encrypt(q), cipher.encrypt(r.sql), 0 if r.error else 1),
            )
            db().commit()

    raw_hist = db().execute(
        "SELECT dialect,question_enc,sql_enc,ok,created_at FROM history_v2 WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (session["uid"],),
    ).fetchall()
    hist = [
        {
            "dialect": h["dialect"],
            "question": cipher.decrypt(h["question_enc"]),
            "sql": cipher.decrypt(h["sql_enc"]),
            "ok": h["ok"],
            "created_at": h["created_at"],
        }
        for h in raw_hist
    ]
    dialects = [(k, v[1]) for k, v in DIALECT_MAP.items()]
    return render_template(
        "dashboard.html",
        result=result,
        hist=hist,
        dialects=dialects,
        selected_dialect=selected_dialect,
    )


@app.route("/download.sql")
@login_required
def download_sql():
    last_sql = session.get("last_sql", "").strip()
    if not last_sql:
        return redirect(url_for("dashboard"))
    return Response(
        last_sql + "\n",
        mimetype="application/sql",
        headers={"Content-Disposition": "attachment; filename=query.sql"},
    )


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(debug=False, port=5000)
