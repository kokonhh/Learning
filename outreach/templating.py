"""Merge-field rendering, spintax and text→HTML conversion for outreach emails.

Syntax supported in subjects and bodies:

* ``{{first_name}}``            – merge field (missing value => reported as missing)
* ``{{first_name|there}}``      – merge field with fallback
* ``{{custom.plan}}`` / ``{{plan}}`` – custom fields imported with the contact
* ``[[Hi|Hello|Hey]]``          – spintax; one option picked deterministically per contact
"""
from __future__ import annotations

import hashlib
import html
import re

MERGE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*(?:\|([^}]*))?\}\}")
SPIN_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
URL_RE = re.compile(r"(https?://[^\s<>\"')\]]+)")

AVAILABLE_FIELDS = [
    "first_name", "last_name", "full_name", "email", "title", "phone",
    "company", "company_domain", "industry", "linkedin_url",
    "sender_name", "sender_email", "sender_company",
]


def build_context(contact: dict, company: dict | None, settings: dict) -> dict:
    company = company or {}
    first = (contact.get("first_name") or "").strip()
    last = (contact.get("last_name") or "").strip()
    ctx = {
        "first_name": first,
        "last_name": last,
        "full_name": " ".join(p for p in (first, last) if p),
        "email": contact.get("email") or "",
        "title": contact.get("title") or "",
        "phone": contact.get("phone") or "",
        "linkedin_url": contact.get("linkedin_url") or "",
        "company": company.get("name") or "",
        "company_domain": company.get("domain") or "",
        "industry": company.get("industry") or "",
        "sender_name": settings.get("sender_name", ""),
        "sender_email": settings.get("sender_email", ""),
        "sender_company": settings.get("company_name", ""),
    }
    custom = contact.get("custom_fields") or {}
    if isinstance(custom, dict):
        for key, value in custom.items():
            k = str(key).strip()
            ctx[f"custom.{k}"] = "" if value is None else str(value)
            ctx.setdefault(k, "" if value is None else str(value))
    return ctx


def _spin(text: str, seed: str) -> str:
    def pick(match: re.Match) -> str:
        options = match.group(1).split("|")
        digest = hashlib.sha256(f"{seed}:{match.group(0)}".encode()).hexdigest()
        return options[int(digest, 16) % len(options)]

    return SPIN_RE.sub(pick, text)


def render(text: str, ctx: dict, seed: str = "") -> tuple[str, list[str]]:
    """Render merge fields + spintax. Returns (rendered, missing_fields)."""
    if not text:
        return "", []
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        name, fallback = match.group(1), match.group(2)
        value = ctx.get(name, "")
        if value:
            return value
        if fallback is not None:
            return fallback.strip()
        missing.append(name)
        return ""

    out = MERGE_RE.sub(repl, _spin(text, seed))
    return out, missing


def text_to_html(text: str) -> str:
    """Convert a plain-text email body to simple HTML paragraphs with links."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    parts = []
    for para in paragraphs:
        escaped = html.escape(para)
        escaped = URL_RE.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', escaped)
        parts.append("<p>" + escaped.replace("\n", "<br>") + "</p>")
    return "\n".join(parts)


def html_to_text(body_html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", body_html)
    text = re.sub(r"</p>\s*", "\n\n", text)
    text = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def preview(subject: str, body: str, ctx: dict, seed: str = "") -> dict:
    subj, m1 = render(subject, ctx, seed)
    body_out, m2 = render(body, ctx, seed)
    return {"subject": subj, "body": body_out, "missing": sorted(set(m1 + m2))}
