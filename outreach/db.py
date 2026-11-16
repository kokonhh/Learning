"""SQLite access helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_SETTINGS = {
    "sender_name": "Your Name",
    "sender_email": "you@example.com",
    "company_name": "Your Company",
    "physical_address": "123 Main St, City, Country",
    "signature": "Best,\n{{sender_name}}\n{{sender_company}}",
    "daily_send_limit": "50",
    "send_window_start": "8",    # hour, 24h, in send_timezone
    "send_window_end": "18",
    "send_weekdays_only": "1",
    "send_timezone": "UTC",
    "min_seconds_between_sends": "0",
    "track_opens": "1",
    "track_clicks": "1",
    "base_url": "http://localhost:5000",
    "pipeline_stages": "lead,qualified,meeting,proposal,negotiation,won,lost",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def ts(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime(TS_FORMAT)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value[:19], TS_FORMAT)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=0, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return data


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log_activity(conn, kind: str, detail: str = "", contact_id=None, deal_id=None) -> None:
    conn.execute(
        "INSERT INTO activities(contact_id, deal_id, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (contact_id, deal_id, kind, detail, ts()),
    )


def row_to_dict(row) -> dict:
    d = dict(row)
    if "custom_fields" in d and isinstance(d["custom_fields"], str):
        try:
            d["custom_fields"] = json.loads(d["custom_fields"] or "{}")
        except ValueError:
            d["custom_fields"] = {}
    return d


def is_suppressed(conn, email: str) -> bool:
    return conn.execute("SELECT 1 FROM suppressions WHERE email = ?", (email,)).fetchone() is not None


def suppress(conn, email: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO suppressions(email, reason) VALUES (?, ?) ON CONFLICT(email) DO NOTHING",
        (email.strip().lower(), reason),
    )
