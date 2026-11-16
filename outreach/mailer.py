"""Outbound mail transports.

``dryrun`` (default) never touches the network: every message is written to the
``outbox/`` directory as an .eml file so you can inspect exactly what would go out.
``smtp`` sends through any SMTP server (Gmail/Workspace app password, Outlook,
SendGrid, Mailgun, Postmark, SES SMTP, ...).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate
from pathlib import Path


class SendError(Exception):
    pass


def build_message(*, sender_name, sender_email, to, subject, html, text,
                  unsubscribe_url=None, in_reply_to=None, reply_to=None) -> EmailMessage:
    msg = EmailMessage()
    domain = sender_email.split("@")[-1] or "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Date"] = formatdate(localtime=False)
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if unsubscribe_url:
        # RFC 8058 one-click unsubscribe (required by Gmail/Yahoo bulk-sender rules)
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


class DryRunMailer:
    name = "dryrun"

    def __init__(self, outbox: str):
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.sent: list[EmailMessage] = []

    def send(self, msg: EmailMessage) -> str:
        self.sent.append(msg)
        safe_id = msg["Message-ID"].strip("<>").replace("@", "_at_").replace("/", "_")
        (self.outbox / f"{safe_id}.eml").write_bytes(bytes(msg))
        return msg["Message-ID"]


class SMTPMailer:
    name = "smtp"

    def __init__(self, host, port, username=None, password=None, security="starttls", timeout=30):
        self.host, self.port = host, int(port)
        self.username, self.password = username, password
        self.security = security
        self.timeout = timeout

    def send(self, msg: EmailMessage) -> str:
        ctx = ssl.create_default_context()
        try:
            if self.security == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server:
                if self.security == "starttls":
                    server.starttls(context=ctx)
                if self.username:
                    server.login(self.username, self.password or "")
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(str(exc)) from exc
        return msg["Message-ID"]


def mailer_from_env(instance_path: str):
    backend = os.environ.get("OUTREACH_MAIL_BACKEND", "dryrun").lower()
    if backend == "smtp":
        host = os.environ.get("SMTP_HOST")
        if not host:
            raise RuntimeError("OUTREACH_MAIL_BACKEND=smtp requires SMTP_HOST")
        return SMTPMailer(
            host=host,
            port=os.environ.get("SMTP_PORT", "587"),
            username=os.environ.get("SMTP_USER"),
            password=os.environ.get("SMTP_PASSWORD"),
            security=os.environ.get("SMTP_SECURITY", "starttls").lower(),
        )
    return DryRunMailer(os.environ.get("OUTREACH_OUTBOX", os.path.join(instance_path, "outbox")))
