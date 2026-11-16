"""Outreach — a self-hosted outbound sales system."""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, redirect, request, session, url_for
from markupsafe import Markup, escape

from . import db
from .mailer import mailer_from_env


def _load_secret(directory: str) -> str:
    env = os.environ.get("OUTREACH_SECRET_KEY")
    if env:
        return env
    Path(directory).mkdir(parents=True, exist_ok=True)
    path = Path(directory) / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    return key


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.update(
        DATABASE=os.environ.get("OUTREACH_DB", os.path.join(app.instance_path, "outreach.db")),
        PASSWORD=os.environ.get("OUTREACH_PASSWORD"),
        API_KEY=os.environ.get("OUTREACH_API_KEY"),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        WTF_CSRF=True,
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("SECRET_KEY", None)
    if not app.config["SECRET_KEY"]:
        # kept beside the database so the web app and the worker share it (click links are signed with it)
        app.config["SECRET_KEY"] = _load_secret(os.path.dirname(os.path.abspath(app.config["DATABASE"])))
    app.extensions["mailer"] = app.config.get("MAILER") or mailer_from_env(app.instance_path)

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        db.init_db(conn)
        if app.config.get("SEED_LIBRARY", True) and not conn.execute("SELECT 1 FROM templates").fetchone() \
                and db.get_settings(conn).get("library_seeded") != "1":
            from .seed import seed_library
            seed_library(conn)
            db.set_setting(conn, "library_seeded", "1")
            conn.commit()
        conn.close()
    app.teardown_appcontext(db.close_db)

    from .views import bp, public_bp
    app.register_blueprint(bp)
    app.register_blueprint(public_bp)

    from . import cli
    cli.register(app)

    _register_security(app)
    _register_template_helpers(app)
    return app


def _register_security(app: Flask) -> None:
    @app.before_request
    def guard():
        endpoint = request.endpoint or ""
        if endpoint.startswith("public.") or endpoint == "static":
            return None
        if app.config.get("PASSWORD") and not session.get("authed") and endpoint != "main.login":
            return redirect(url_for("main.login", next=request.path))
        if request.method == "POST" and app.config.get("WTF_CSRF"):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf_token", "")):
                abort(400, "Invalid or missing CSRF token — reload the page and try again.")
        return None


def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


def _tasks_due() -> int:
    try:
        return db.get_db().execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'open' AND due_at <= ?", (db.ts(),)).fetchone()[0]
    except Exception:
        return 0


def _register_template_helpers(app: Flask) -> None:
    @app.context_processor
    def inject():
        return {
            "csrf_token": _csrf_token,
            "csrf_field": lambda: Markup(f'<input type="hidden" name="csrf_token" value="{_csrf_token()}">'),
            "mail_backend": app.extensions["mailer"].name,
            "auth_enabled": bool(app.config.get("PASSWORD")),
            "nav_tasks_due": _tasks_due,
        }

    @app.template_filter("ago")
    def ago(value):
        dt = db.parse_ts(value) if isinstance(value, str) else value
        if not dt:
            return "—"
        secs = (db.utcnow() - dt).total_seconds()
        future = secs < 0
        secs = abs(secs)
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= size:
                n = int(secs // size)
                return f"in {n}{unit}" if future else f"{n}{unit} ago"
        return "just now"

    @app.template_filter("money")
    def money(value):
        try:
            return f"${float(value or 0):,.0f}"
        except (TypeError, ValueError):
            return "$0"

    @app.template_filter("date")
    def date_only(value):
        if not value:
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
        return str(value)[:10]

    @app.template_filter("tags")
    def tags(value):
        return [t for t in (value or "").split(",") if t]

    @app.template_filter("nl2br")
    def nl2br(value):
        return Markup("<br>".join(escape(value or "").split("\n")))
