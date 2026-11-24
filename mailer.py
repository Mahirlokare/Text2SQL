"""Transactional email. Priority: Brevo HTTP API (works on Render free) -> SMTP -> dev fallback (returns False)."""
import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage


def send_email(to: str, subject: str, html: str) -> bool:
    sender = os.getenv("MAIL_FROM", "")
    name = os.getenv("MAIL_FROM_NAME", "QueryPilot AI")
    key = os.getenv("BREVO_API_KEY")
    if key and sender:
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=json.dumps({"sender": {"name": name, "email": sender},
                             "to": [{"email": to}], "subject": subject,
                             "htmlContent": html}).encode(),
            headers={"api-key": key, "content-type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    host = os.getenv("SMTP_HOST")
    if host and sender:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = f"{name} <{sender}>", to, subject
        msg.set_content("Please view this email in an HTML-capable client.")
        msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", sender), os.getenv("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return True
    return False
