"""QueryPilot AI web app: self-contained auth + Universal Multi-Dialect Text-to-SQL Query Agent."""
import os
import re
import smtplib
import sqlite3
from email.message import EmailMessage
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from core.engine import Text2SQLEngine
from core.llm import SQLGenerator
from core.validator import DIALECT_MAP

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
USERS_DB = os.path.join(DATA, "users.db")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("RENDER") is not None,
)
ts = URLSafeTimedSerializer(app.secret_key, salt="email-confirm")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_engine = None


def get_engine() -> Text2SQLEngine:
    global _engine
    if _engine is None:
        _engine = Text2SQLEngine(SQLGenerator())
    return _engine


# ---------- account & history store ----------
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
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY,
      name TEXT,
      email TEXT UNIQUE,
      password_hash TEXT,
      confirmed INTEGER DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS history(
      id INTEGER PRIMARY KEY,
      user_id TEXT,
      dialect TEXT DEFAULT 'postgresql',
      question TEXT,
      sql TEXT,
      ok INTEGER,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Ensure dialect column exists if upgrading from older table
    cols = {r[1] for r in c.execute("PRAGMA table_info(history)").fetchall()}
    if "dialect" not in cols:
        c.execute("ALTER TABLE history ADD COLUMN dialect TEXT DEFAULT 'postgresql'")
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


def issue_confirmation(email: str, name: str) -> tuple[bool, str]:
    token = ts.dumps(email)
    link = url_for("confirm", token=token, _external=True)
    html = (
        f"<div style='font-family:sans-serif'><h2>Welcome to QueryPilot AI, {name}!</h2>"
        f"<p>Confirm your account by clicking the link below:</p>"
        f"<p><a href='{link}'>{link}</a></p>"
        f"<p>This link expires in 24 hours.</p></div>"
    )
    try:
        sent = _try_smtp_email(email, "Confirm your QueryPilot AI account", html)
    except Exception as e:
        app.logger.warning("SMTP delivery skipped/failed: %s", e)
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
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not name or not EMAIL_RE.match(email):
            flash("Enter your name and a valid email.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("An account with this email already exists.", "error")
        else:
            db().execute(
                "INSERT INTO users(name,email,password_hash) VALUES(?,?,?)",
                (name, email, generate_password_hash(pw)),
            )
            db().commit()
            sent, link = issue_confirmation(email, name)
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
        email = ts.loads(token, max_age=86400)
    except SignatureExpired:
        flash("That confirmation link expired. Request a new one below.", "error")
        return redirect(url_for("resend"))
    except BadSignature:
        flash("Invalid confirmation link.", "error")
        return redirect(url_for("login"))
    db().execute("UPDATE users SET confirmed=1 WHERE email=?", (email,))
    db().commit()
    flash("Account confirmed! You can sign in now.", "success")
    return redirect(url_for("login"))


@app.route("/resend", methods=["GET", "POST"])
def resend():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and not u["confirmed"]:
            sent, link = issue_confirmation(email, u["name"])
            return render_template(
                "verify_email.html",
                email=email,
                name=u["name"],
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
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or not check_password_hash(u["password_hash"], pw):
            flash("Invalid email or password.", "error")
        elif not u["confirmed"]:
            flash("Please confirm your account first (or click 'Resend confirmation' below).", "error")
        else:
            session.clear()
            session["uid"] = str(u["id"])
            session["email"] = u["email"]
            session["name"] = u["name"]
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- universal SQL query agent workspace ----------
@app.route("/app", methods=["GET", "POST"])
@login_required
def dashboard():
    result = None
    eng = get_engine()
    selected_dialect = session.get("dialect", "postgresql")
    schema_hint = session.get("schema_hint", "")

    if request.method == "POST":
        q = request.form.get("question", "").strip()[:2000]
        selected_dialect = request.form.get("dialect", "postgresql").strip().lower()
        if selected_dialect not in DIALECT_MAP:
            selected_dialect = "postgresql"
        schema_hint = request.form.get("schema_hint", "").strip()[:6000]
        session["dialect"] = selected_dialect
        session["schema_hint"] = schema_hint

        if q:
            user_hist = [tuple(x) for x in session.get("sql_hist", [])]
            r = eng.ask(q, dialect=selected_dialect, schema_hint=schema_hint, history=user_hist)
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
                "INSERT INTO history(user_id,dialect,question,sql,ok) VALUES(?,?,?,?,?)",
                (session["uid"], r.dialect_label, q, r.sql, 0 if r.error else 1),
            )
            db().commit()

    hist = db().execute(
        "SELECT dialect,question,sql,ok,created_at FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (session["uid"],),
    ).fetchall()
    dialects = [(k, v[1]) for k, v in DIALECT_MAP.items()]
    return render_template(
        "dashboard.html",
        result=result,
        hist=hist,
        dialects=dialects,
        selected_dialect=selected_dialect,
        schema_hint=schema_hint,
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
    app.run(debug=True, port=5000)
