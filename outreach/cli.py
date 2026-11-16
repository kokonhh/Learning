"""Command-line entry points (``flask --app outreach <command>``)."""
from __future__ import annotations

import time

import click
from flask import current_app

from . import engine, inbox, scoring
from .db import connect


def _conn():
    return connect(current_app.config["DATABASE"])


def tick(conn) -> dict:
    stats = engine.process_due(conn, current_app.extensions["mailer"], current_app.config["SECRET_KEY"])
    try:
        stats["inbox"] = inbox.check_inbox(conn)
    except Exception as exc:  # IMAP problems should never stop sending
        stats["inbox_error"] = str(exc)
    return stats


def register(app):
    @app.cli.command("tick")
    def tick_cmd():
        """Run due sequence steps and check the inbox once (use from cron)."""
        conn = _conn()
        click.echo(tick(conn))
        conn.close()

    @app.cli.command("worker")
    @click.option("--interval", default=60, show_default=True, help="Seconds between runs")
    def worker_cmd(interval):
        """Run the sending engine forever."""
        click.echo(f"Outreach worker started (mailer={current_app.extensions['mailer'].name}, every {interval}s)")
        while True:
            conn = _conn()
            try:
                stats = tick(conn)
                if any(v for k, v in stats.items() if k != "inbox"):
                    click.echo(f"{engine.ts()} {stats}")
            except Exception as exc:
                click.echo(f"{engine.ts()} worker error: {exc}", err=True)
            finally:
                conn.close()
            time.sleep(interval)

    @app.cli.command("rescore")
    def rescore_cmd():
        """Recompute every lead score."""
        conn = _conn()
        click.echo(f"Rescored {scoring.recompute_all(conn)} contacts")
        conn.close()

    @app.cli.command("seed")
    def seed_cmd():
        """Load demo data: contacts, templates, a 5-step sequence and deals."""
        from .seed import seed
        conn = _conn()
        click.echo(seed(conn))
        conn.close()
