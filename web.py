"""QueryPilot AI web app: 100% self-contained auth (signup/confirm/login) + text-to-SQL workspace."""
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

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
USERS_DB = os.path.join(DATA, "users.db")
DEMO_DB = os.path.join(BASE, "data", "shop.db")

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


def get_engine():
    global _engine
    if _engine is None:
        _engine = Text2SQLEngine(DEMO_DB, SQLGenerator())
    return _engine


# ---------- database ----------
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
      question TEXT,
      sql TEXT,
      ok INTEGER,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
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


def _try_smtp_email(to_email: str, subject: str, html: str) -> bool:
    """Optional stdlib SMTP (no paid or third-party SDK). Returns False when not configured."""
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


# ---------- workspace ----------
@app.route("/app", methods=["GET", "POST"])
@login_required
def dashboard():
    result = None
    eng = get_engine()
    if request.method == "POST":
        q = request.form.get("question", "").strip()[:500]
        if q:
            r = eng.ask(q)
            result = {
                "q": q,
                "sql": r.sql,
                "err": r.error,
                "attempts": r.attempts,
                "latency": r.latency_s,
                "cols": list(r.df.columns) if r.df is not None else [],
                "rows": r.df.head(200).values.tolist() if r.df is not None else [],
                "total": len(r.df) if r.df is not None else 0,
            }
            db().execute(
                "INSERT INTO history(user_id,question,sql,ok) VALUES(?,?,?,?)",
                (session["uid"], q, r.sql, 0 if r.error else 1),
            )
            db().commit()
            session["last_q"] = q
    hist = db().execute(
        "SELECT question,sql,ok,created_at FROM history WHERE user_id=? ORDER BY id DESC LIMIT 8",
        (session["uid"],),
    ).fetchall()
    return render_template(
        "dashboard.html",
        result=result,
        hist=hist,
        schema=eng.schema,
        engine_mode=getattr(eng.gen, "mode", "builtin"),
    )


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
    return Response(
        r.df.to_csv(index=False),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=result.csv"},
    )


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
