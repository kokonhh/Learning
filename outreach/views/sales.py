"""Dashboard, pipeline/deals, settings and login."""
from __future__ import annotations

import secrets

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from .. import analytics, scoring
from ..db import DEFAULT_SETTINGS, get_db, get_settings, log_activity, set_setting, ts
from . import bp

DEFAULT_PROBABILITY = {"lead": 10, "qualified": 20, "meeting": 40, "proposal": 60, "negotiation": 80,
                       "won": 100, "lost": 0}


def _stages(conn):
    return [s.strip() for s in get_settings(conn)["pipeline_stages"].split(",") if s.strip()]


@bp.route("/")
def dashboard():
    conn = get_db()
    data = analytics.overview(conn)
    hot = conn.execute(
        "SELECT c.*, co.name company_name FROM contacts c LEFT JOIN companies co ON co.id = c.company_id "
        "WHERE c.status IN ('engaged','replied','meeting','qualified') ORDER BY c.score DESC LIMIT 8").fetchall()
    recent = conn.execute(
        "SELECT a.*, c.first_name, c.last_name, c.email FROM activities a LEFT JOIN contacts c ON c.id = a.contact_id "
        "WHERE a.kind IN ('replied','email_opened','link_clicked','meeting','unsubscribed','bounced','stage_change') "
        "ORDER BY a.id DESC LIMIT 15").fetchall()
    settings = get_settings(conn)
    setup_needed = settings["sender_email"] == DEFAULT_SETTINGS["sender_email"]
    return render_template("dashboard.html", d=data, hot=hot, recent=recent, setup_needed=setup_needed)


@bp.route("/pipeline")
def pipeline():
    conn = get_db()
    stages = _stages(conn)
    deals = conn.execute(
        "SELECT d.*, c.first_name, c.last_name, c.email, co.name company_name FROM deals d "
        "LEFT JOIN contacts c ON c.id = d.contact_id LEFT JOIN companies co ON co.id = d.company_id "
        "ORDER BY d.value DESC").fetchall()
    by_stage = {s: [] for s in stages}
    for d in deals:
        by_stage.setdefault(d["stage"], []).append(d)
    return render_template("pipeline.html", stages=list(by_stage), by_stage=by_stage,
                           summary=analytics.pipeline(conn))


@bp.route("/deals/new", methods=["GET", "POST"])
@bp.route("/deals/<int:did>", methods=["GET", "POST"])
def deal_edit(did=None):
    conn = get_db()
    stages = _stages(conn)
    deal = None
    if did:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (did,)).fetchone()
        if deal is None:
            abort(404)
    if request.method == "POST":
        if request.form.get("delete") and did:
            conn.execute("DELETE FROM deals WHERE id = ?", (did,))
            conn.commit()
            flash("Deal deleted.", "ok")
            return redirect(url_for("main.pipeline"))
        f = request.form
        contact_id = int(f["contact_id"]) if f.get("contact_id") else None
        company_id = int(f["company_id"]) if f.get("company_id") else None
        if contact_id and not company_id:
            row = conn.execute("SELECT company_id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            company_id = row["company_id"] if row else None
        stage = f.get("stage") if f.get("stage") in stages else stages[0]
        prob = int(f["probability"]) if f.get("probability") not in (None, "") else DEFAULT_PROBABILITY.get(stage)
        vals = (f.get("name") or "Untitled deal", contact_id, company_id, stage, float(f.get("value") or 0), prob,
                f.get("close_date") or None, f.get("notes"), f.get("lost_reason") or None)
        if did:
            if deal["stage"] != stage:
                _log_stage(conn, deal, stage)
            conn.execute("UPDATE deals SET name=?, contact_id=?, company_id=?, stage=?, value=?, probability=?, "
                         "close_date=?, notes=?, lost_reason=?, updated_at=? WHERE id=?", vals + (ts(), did))
        else:
            did = conn.execute("INSERT INTO deals(name, contact_id, company_id, stage, value, probability, close_date, "
                               "notes, lost_reason) VALUES (?,?,?,?,?,?,?,?,?)", vals).lastrowid
            log_activity(conn, "deal_created", f"Deal created: {vals[0]} ({stage})", contact_id=contact_id,
                         deal_id=did)
            if contact_id:
                conn.execute("UPDATE contacts SET status = 'qualified' WHERE id = ? AND status IN "
                             "('new','contacted','engaged','replied','meeting')", (contact_id,))
        _sync_contact_on_stage(conn, contact_id, stage)
        conn.commit()
        flash("Deal saved.", "ok")
        return redirect(url_for("main.pipeline"))
    prefill = dict(deal) if deal else {"contact_id": request.args.get("contact_id", type=int), "stage": stages[0]}
    if not deal and prefill.get("contact_id"):
        c = conn.execute("SELECT c.first_name, c.last_name, co.name company FROM contacts c LEFT JOIN companies co "
                         "ON co.id = c.company_id WHERE c.id = ?", (prefill["contact_id"],)).fetchone()
        if c:
            prefill["name"] = f"{c['company'] or (c['first_name'] or '') + ' ' + (c['last_name'] or '')} — new deal"
    return render_template(
        "deal_form.html", deal=prefill, did=did, stages=stages,
        contacts=conn.execute("SELECT c.id, c.email, c.first_name, c.last_name, co.name company_name FROM contacts c "
                              "LEFT JOIN companies co ON co.id = c.company_id ORDER BY c.score DESC LIMIT 1000").fetchall(),
        companies=conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall(),
        activities=conn.execute("SELECT * FROM activities WHERE deal_id = ? ORDER BY id DESC", (did,)).fetchall()
        if did else [],
    )


def _log_stage(conn, deal, stage):
    log_activity(conn, "stage_change", f"{deal['name']}: {deal['stage']} → {stage}",
                 contact_id=deal["contact_id"], deal_id=deal["id"])


def _sync_contact_on_stage(conn, contact_id, stage):
    if not contact_id:
        return
    if stage == "won":
        conn.execute("UPDATE contacts SET status = 'customer' WHERE id = ?", (contact_id,))
    scoring.compute(conn, contact_id)


@bp.route("/ui/deals/<int:did>/stage", methods=["POST"])
def ui_deal_stage(did):
    conn = get_db()
    stage = (request.get_json(silent=True) or {}).get("stage") or request.form.get("stage")
    if stage not in _stages(conn):
        return jsonify(error="unknown stage"), 400
    deal = conn.execute("SELECT * FROM deals WHERE id = ?", (did,)).fetchone()
    if deal is None:
        return jsonify(error="not found"), 404
    if deal["stage"] != stage:
        _log_stage(conn, deal, stage)
        conn.execute("UPDATE deals SET stage = ?, probability = ?, updated_at = ? WHERE id = ?",
                     (stage, DEFAULT_PROBABILITY.get(stage, deal["probability"]), ts(), did))
        _sync_contact_on_stage(conn, deal["contact_id"], stage)
        conn.commit()
    return jsonify(ok=True, summary=analytics.pipeline(conn))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    conn = get_db()
    if request.method == "POST":
        for key in DEFAULT_SETTINGS:
            if key in ("track_opens", "track_clicks", "send_weekdays_only"):
                set_setting(conn, key, "1" if request.form.get(key) else "0")
            elif key in request.form:
                set_setting(conn, key, request.form[key].strip())
        conn.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", s=get_settings(conn),
                           api_enabled=bool(current_app.config.get("API_KEY")))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), current_app.config.get("PASSWORD") or ""):
            session["authed"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        flash("Wrong password.", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))
