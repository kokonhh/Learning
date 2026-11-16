"""Public endpoints: tracking pixel, click redirect, unsubscribe, and the JSON API."""
from __future__ import annotations

import csv
import io
import json
import secrets
from functools import wraps

from flask import Response, abort, current_app, jsonify, redirect, render_template, request

from .. import engine, importer, scoring, tracking
from ..db import get_db, get_settings, log_activity
from . import public_bp


@public_bp.route("/t/o/<token>.gif")
def track_open(token):
    engine.record_open(get_db(), token)
    return Response(tracking.PIXEL_GIF, mimetype="image/gif",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@public_bp.route("/t/c/<token>")
def track_click(token):
    url, sig = request.args.get("u", ""), request.args.get("s", "")
    if not url.startswith(("http://", "https://")) or \
            not tracking.verify_url(current_app.config["SECRET_KEY"], token, url, sig):
        abort(400)  # refuse to act as an open redirect
    engine.record_click(get_db(), token, url)
    return redirect(url, code=302)


@public_bp.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    conn = get_db()
    m = conn.execute("SELECT contact_id FROM messages WHERE token = ?", (token,)).fetchone()
    if m is None:
        abort(404)
    company = get_settings(conn).get("company_name", "")
    # RFC 8058 one-click: mail clients POST "List-Unsubscribe=One-Click" with no cookies / CSRF token
    if request.method == "POST":
        engine.unsubscribe(conn, m["contact_id"])
        if request.form.get("List-Unsubscribe") == "One-Click":
            return "", 200
        return render_template("unsubscribe.html", done=True, company=company)
    return render_template("unsubscribe.html", done=False, company=company, token=token)


# ------------------------------------------------------------------ JSON API (X-API-Key)

def api_key_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = current_app.config.get("API_KEY")
        sent = request.headers.get("X-API-Key") or request.args.get("api_key") or ""
        if not key:
            return jsonify(error="API disabled: set OUTREACH_API_KEY"), 403
        if not secrets.compare_digest(sent, key):
            return jsonify(error="invalid API key"), 401
        return fn(*args, **kwargs)
    return wrapper


@public_bp.route("/api/v1/leads", methods=["POST"])
@api_key_required
def api_create_lead():
    """Inbound lead webhook (website form, Zapier, Clay, ...). Optional auto-enroll via sequence_id."""
    conn = get_db()
    payload = request.get_json(silent=True) or request.form.to_dict()
    leads = payload if isinstance(payload, list) else [payload]
    rows = []
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in lead.items()
                if k not in ("sequence_id",)}
        rows.append(flat)
    if not rows:
        return jsonify(error="no leads in payload"), 400
    headers = sorted({k for r in rows for k in r})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers)
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in headers})
    res = importer.import_csv(conn, buf.getvalue(), source=(rows[0].get("source") or "api"),
                              extra_tags=request.args.get("tags", ""))
    seq_id = (leads[0].get("sequence_id") if isinstance(leads[0], dict) else None) or request.args.get("sequence_id")
    enrolled = None
    if seq_id and res["contact_ids"]:
        try:
            enrolled = engine.enroll(conn, int(seq_id), res["contact_ids"])
        except ValueError as exc:
            enrolled = {"error": str(exc)}
    status = 201 if res["created"] else 200
    return jsonify(created=res["created"], updated=res["updated"], skipped=res["skipped"], errors=res["errors"],
                   contact_ids=res["contact_ids"], enrolled=enrolled), status


@public_bp.route("/api/v1/events", methods=["POST"])
@api_key_required
def api_event():
    """Report reply / bounce / meeting / unsubscribe from another system (ESP webhook, Calendly, ...)."""
    conn = get_db()
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    kind = data.get("type")
    row = conn.execute("SELECT id FROM contacts WHERE lower(email) = ?", (email,)).fetchone()
    if row is None:
        return jsonify(error="contact not found"), 404
    cid = row["id"]
    if kind == "reply":
        engine.mark_replied(conn, cid, detail=data.get("detail") or "Reply (via API)")
    elif kind in ("bounce", "bounced"):
        engine.unsubscribe(conn, cid, reason="bounced")
    elif kind in ("unsubscribe", "complaint"):
        engine.unsubscribe(conn, cid, reason="complaint" if kind == "complaint" else "unsubscribed")
    elif kind == "meeting":
        log_activity(conn, "meeting", data.get("detail") or "Meeting booked (via API)", contact_id=cid)
        conn.execute("UPDATE contacts SET status = 'meeting' WHERE id = ? AND status NOT IN ('qualified','customer')",
                     (cid,))
        engine.stop_enrollments(conn, cid, "replied", only_stop_on_reply=True)
        scoring.compute(conn, cid)
        conn.commit()
    else:
        return jsonify(error="type must be one of reply, bounce, unsubscribe, complaint, meeting"), 400
    return jsonify(ok=True, contact_id=cid)


@public_bp.route("/api/v1/contacts/<int:cid>", methods=["GET"])
@api_key_required
def api_contact(cid):
    conn = get_db()
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return jsonify(error="not found"), 404
    d = dict(row)
    d["custom_fields"] = json.loads(d["custom_fields"] or "{}")
    return jsonify(d)
