"""Open/click tracking and unsubscribe links."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from urllib.parse import quote

HREF_RE = re.compile(r'href="(https?://[^"]+)"')

# 1x1 transparent GIF
PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def new_token() -> str:
    return secrets.token_urlsafe(16)


def sign_url(secret: str, token: str, url: str) -> str:
    return hmac.new(secret.encode(), f"{token}|{url}".encode(), hashlib.sha256).hexdigest()[:20]


def verify_url(secret: str, token: str, url: str, sig: str) -> bool:
    return hmac.compare_digest(sign_url(secret, token, url), sig or "")


def unsubscribe_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/u/{token}"


def decorate_html(body_html: str, *, base_url: str, token: str, secret: str,
                  track_opens: bool, track_clicks: bool, footer_html: str = "") -> str:
    base = base_url.rstrip("/")
    if track_clicks:
        def repl(m: re.Match) -> str:
            url = m.group(1).replace("&amp;", "&")
            sig = sign_url(secret, token, url)
            return f'href="{base}/t/c/{token}?u={quote(url, safe="")}&amp;s={sig}"'
        body_html = HREF_RE.sub(repl, body_html)
    out = body_html + footer_html
    if track_opens:
        out += f'<img src="{base}/t/o/{token}.gif" width="1" height="1" alt="" style="display:none">'
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">{out}</div>'
