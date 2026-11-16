"""Reply and bounce detection by polling an IMAP inbox.

Configure with IMAP_HOST / IMAP_USER / IMAP_PASSWORD (optionally IMAP_PORT,
IMAP_FOLDER). Messages are matched to contacts by the sender address, or – for
bounces – by the failed recipient address in the DSN. Auto-replies (out of
office etc.) are logged but do not stop sequences.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from email.message import Message
from email.utils import parseaddr

from . import engine
from .db import log_activity, ts

BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
FAILED_RCPT_RE = re.compile(r"(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*<?([^\s>]+@[^\s>]+)>?", re.I)
AUTO_REPLY_SUBJECT_RE = re.compile(r"(out of (the )?office|automatic reply|auto[- ]?reply|away from)", re.I)


def _body_text(msg: Message) -> str:
    parts = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            payload = part.get_payload()
            if isinstance(payload, list):
                parts.extend(str(p) for p in payload)
            else:
                parts.append(str(payload))
        elif ctype == "text/plain" and not part.is_multipart():
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def classify(msg: Message) -> tuple[str, str | None]:
    """Return (kind, email) where kind is reply | auto_reply | bounce | unknown."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = msg.get("Subject", "") or ""
    if any(sender.startswith(b) for b in BOUNCE_SENDERS) or "delivery status notification" in subject.lower() \
            or "undeliverable" in subject.lower():
        m = FAILED_RCPT_RE.search(_body_text(msg))
        return "bounce", (m.group(1).lower() if m else None)
    auto = (msg.get("Auto-Submitted", "no").lower() not in ("", "no")) or msg.get("X-Autoreply") \
        or msg.get("X-Autorespond") or AUTO_REPLY_SUBJECT_RE.search(subject)
    if auto:
        return "auto_reply", sender
    return ("reply", sender) if sender else ("unknown", None)


def handle_message(conn, msg: Message) -> str:
    kind, addr = classify(msg)
    if not addr:
        return "ignored"
    contact = conn.execute("SELECT id FROM contacts WHERE lower(email) = ?", (addr,)).fetchone()
    if contact is None:
        return "no_contact"
    cid = contact["id"]
    subject = msg.get("Subject", "")
    if kind == "bounce":
        engine.unsubscribe(conn, cid, reason="bounced")
        return "bounce"
    if kind == "auto_reply":
        log_activity(conn, "auto_reply", subject, contact_id=cid)
        conn.commit()
        return "auto_reply"
    text = _body_text(msg)
    conn.execute(
        "INSERT INTO messages(contact_id, direction, subject, body_text, message_id, status, created_at) "
        "VALUES (?, 'in', ?, ?, ?, 'received', ?)",
        (cid, subject, text[:20000], msg.get("Message-ID"), ts()),
    )
    replied_to = None
    irt = (msg.get("In-Reply-To") or "").strip()
    if irt:
        row = conn.execute("SELECT id FROM messages WHERE message_id = ?", (irt,)).fetchone()
        replied_to = row["id"] if row else None
    engine.mark_replied(conn, cid, detail=f"Replied: {subject}", message_id=replied_to)
    return "reply"


def poll_imap() -> list[Message]:
    host = os.environ.get("IMAP_HOST")
    if not host:
        return []
    imap = imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT", "993")))
    imap.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    imap.select(os.environ.get("IMAP_FOLDER", "INBOX"))
    _, data = imap.search(None, "UNSEEN")
    messages = []
    for num in data[0].split():
        _, msg_data = imap.fetch(num, "(RFC822)")
        messages.append(email.message_from_bytes(msg_data[0][1]))
    imap.logout()
    return messages


def check_inbox(conn) -> dict:
    counts: dict[str, int] = {}
    for msg in poll_imap():
        outcome = handle_message(conn, msg)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
