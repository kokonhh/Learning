"""Templates, sequences, task queue."""
from __future__ import annotations

from datetime import timedelta

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from .. import analytics, engine, templating
from ..db import get_db, get_settings, log_activity, ts, utcnow
from . import bp

STEP_TYPES = {"email": "Email", "call": "Phone call", "linkedin": "LinkedIn", "task": "Manual task"}


# ------------------------------------------------------------------ templates

@bp.route("/templates")
def templates():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM sequence_steps WHERE template_id = t.id) used FROM templates t "
        "ORDER BY category, name").fetchall()
    return render_template("templates/list.html", templates=rows)


@bp.route("/templates/new", methods=["GET", "POST"])
@bp.route("/templates/<int:tid>", methods=["GET", "POST"])
def template_edit(tid=None):
    conn = get_db()
    tpl = None
    if tid:
        tpl = conn.execute("SELECT * FROM templates WHERE id = ?", (tid,)).fetchone()
        if tpl is None:
            abort(404)
    if request.method == "POST":
        if request.form.get("delete") and tid:
            conn.execute("DELETE FROM templates WHERE id = ?", (tid,))
            conn.commit()
            flash("Template deleted.", "ok")
            return redirect(url_for("main.templates"))
        name, subject, body = (request.form.get(k, "").strip() for k in ("name", "subject", "body"))
        if not (name and subject and body):
            flash("Name, subject and body are required.", "error")
            return render_template("templates/form.html", tpl=request.form, tid=tid,
                                   fields=templating.AVAILABLE_FIELDS)
        if tid:
            conn.execute("UPDATE templates SET name=?, subject=?, body=?, category=?, updated_at=? WHERE id=?",
                         (name, subject, body, request.form.get("category"), ts(), tid))
        else:
            tid = conn.execute("INSERT INTO templates(name, subject, body, category) VALUES (?,?,?,?)",
                               (name, subject, body, request.form.get("category"))).lastrowid
        conn.commit()
        flash("Template saved.", "ok")
        return redirect(url_for("main.template_edit", tid=tid))
    return render_template("templates/form.html", tpl=dict(tpl) if tpl else {}, tid=tid,
                           fields=templating.AVAILABLE_FIELDS)


@bp.route("/ui/preview", methods=["POST"])
def ui_preview():
    """Render subject/body against a real contact (or a sample) for live preview."""
    conn = get_db()
    data = request.get_json(silent=True) or request.form
    settings = get_settings(conn)
    cid = data.get("contact_id")
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone() if cid else \
        conn.execute("SELECT * FROM contacts WHERE status NOT IN ('unsubscribed','bounced') "
                     "ORDER BY score DESC LIMIT 1").fetchone()
    if row:
        contact, company = engine._load_contact(conn, row["id"])
    else:
        contact = {"id": 0, "first_name": "Alex", "last_name": "Rivera", "email": "alex@example.com",
                   "title": "Head of Operations", "custom_fields": {}}
        company = {"name": "Acme Inc", "domain": "acme.com", "industry": "Logistics"}
    ctx = templating.build_context(contact, company, settings)
    out = templating.preview(data.get("subject", ""), data.get("body", ""), ctx, str(contact["id"]))
    sig, _ = templating.render(settings.get("signature", ""), ctx, str(contact["id"]))
    out["signature"] = sig
    out["contact"] = f"{ctx['full_name'] or ctx['email']} ({ctx['company'] or 'no company'})"
    return jsonify(out)


# ------------------------------------------------------------------ sequences

@bp.route("/sequences", methods=["GET", "POST"])
def sequences():
    conn = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("main.sequences"))
        sid = conn.execute("INSERT INTO sequences(name, description) VALUES (?, ?)",
                           (name, request.form.get("description"))).lastrowid
        conn.commit()
        return redirect(url_for("main.sequence_detail", sid=sid))
    rows = conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM sequence_steps WHERE sequence_id = s.id) n_steps, "
        "(SELECT COUNT(*) FROM enrollments WHERE sequence_id = s.id) n_enrolled, "
        "(SELECT COUNT(*) FROM enrollments WHERE sequence_id = s.id AND status = 'active') n_active "
        "FROM sequences s WHERE s.status != 'archived' OR ? ORDER BY s.id DESC",
        (bool(request.args.get("archived")),)).fetchall()
    stats = {r["id"]: analytics.email_stats(conn, sequence_id=r["id"]) for r in rows}
    return render_template("sequences/list.html", sequences=rows, stats=stats)


@bp.route("/sequences/<int:sid>")
def sequence_detail(sid):
    conn = get_db()
    seq = conn.execute("SELECT * FROM sequences WHERE id = ?", (sid,)).fetchone()
    if seq is None:
        abort(404)
    status_filter = request.args.get("status")
    enrollments = conn.execute(
        "SELECT e.*, c.email, c.first_name, c.last_name, c.status contact_status FROM enrollments e "
        "JOIN contacts c ON c.id = e.contact_id WHERE e.sequence_id = ? AND (? IS NULL OR e.status = ?) "
        "ORDER BY e.id DESC LIMIT 500", (sid, status_filter, status_filter)).fetchall()
    report = analytics.sequence_report(conn, sid)
    # cumulative day offset per step for the timeline
    day = 0
    for s in report["steps"]:
        day += s["step"]["delay_days"]
        s["day"] = day
    return render_template(
        "sequences/detail.html", seq=seq, report=report, enrollments=enrollments,
        templates=conn.execute("SELECT id, name, subject FROM templates ORDER BY name").fetchall(),
        step_types=STEP_TYPES, next_run_label=engine.next_run_label, status_filter=status_filter,
        tags=sorted({t for r in conn.execute("SELECT tags FROM contacts WHERE tags != ''")
                     for t in r["tags"].split(",") if t}),
    )


@bp.route("/sequences/<int:sid>/settings", methods=["POST"])
def sequence_settings(sid):
    conn = get_db()
    action = request.form.get("action")
    if action in ("activate", "pause", "archive"):
        status = {"activate": "active", "pause": "paused", "archive": "archived"}[action]
        if status == "active":
            n = conn.execute("SELECT COUNT(*) FROM sequence_steps WHERE sequence_id = ?", (sid,)).fetchone()[0]
            if not n:
                flash("Add at least one step before activating.", "error")
                return redirect(url_for("main.sequence_detail", sid=sid))
        conn.execute("UPDATE sequences SET status = ? WHERE id = ?", (status, sid))
        flash(f"Sequence {status}.", "ok")
    elif action == "save":
        conn.execute("UPDATE sequences SET name = ?, description = ?, stop_on_reply = ? WHERE id = ?",
                     (request.form["name"], request.form.get("description"),
                      1 if request.form.get("stop_on_reply") else 0, sid))
        flash("Saved.", "ok")
    elif action == "clone":
        src = conn.execute("SELECT * FROM sequences WHERE id = ?", (sid,)).fetchone()
        new_id = conn.execute("INSERT INTO sequences(name, description, stop_on_reply) VALUES (?, ?, ?)",
                              (src["name"] + " (copy)", src["description"], src["stop_on_reply"])).lastrowid
        conn.execute(
            "INSERT INTO sequence_steps(sequence_id, position, step_type, delay_days, template_id, subject, body, "
            "thread_reply, instructions) SELECT ?, position, step_type, delay_days, template_id, subject, body, "
            "thread_reply, instructions FROM sequence_steps WHERE sequence_id = ?", (new_id, sid))
        conn.commit()
        flash("Sequence cloned.", "ok")
        return redirect(url_for("main.sequence_detail", sid=new_id))
    elif action == "enroll_tag":
        tag = (request.form.get("tag") or "").strip().lower()
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM contacts WHERE (',' || tags || ',') LIKE ?", (f"%,{tag},%",))]
        try:
            res = engine.enroll(conn, sid, ids)
            flash(f"Enrolled {res['enrolled']} contacts tagged '{tag}' "
                  f"({res['skipped_existing']} already in, "
                  f"{res['skipped_suppressed'] + res['skipped_status']} suppressed).", "ok")
        except ValueError as exc:
            flash(str(exc), "error")
    conn.commit()
    return redirect(url_for("main.sequence_detail", sid=sid))


@bp.route("/sequences/<int:sid>/steps", methods=["POST"])
@bp.route("/sequences/<int:sid>/steps/<int:step_id>", methods=["POST"])
def sequence_step(sid, step_id=None):
    conn = get_db()
    action = request.form.get("action", "save")
    if step_id and action == "delete":
        conn.execute("DELETE FROM sequence_steps WHERE id = ? AND sequence_id = ?", (step_id, sid))
        _renumber(conn, sid)
        conn.commit()
        flash("Step removed.", "ok")
        return redirect(url_for("main.sequence_detail", sid=sid))
    if step_id and action in ("up", "down"):
        steps = [r["id"] for r in conn.execute(
            "SELECT id FROM sequence_steps WHERE sequence_id = ? ORDER BY position", (sid,))]
        i = steps.index(step_id)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(steps):
            steps[i], steps[j] = steps[j], steps[i]
            _renumber(conn, sid, steps)
            conn.commit()
        return redirect(url_for("main.sequence_detail", sid=sid))

    step_type = request.form.get("step_type", "email")
    if step_type not in STEP_TYPES:
        abort(400)
    vals = dict(
        step_type=step_type,
        delay_days=max(int(request.form.get("delay_days") or 0), 0),
        template_id=int(request.form["template_id"]) if request.form.get("template_id") else None,
        subject=request.form.get("subject") or None,
        body=request.form.get("body") or None,
        thread_reply=1 if request.form.get("thread_reply") else 0,
        instructions=request.form.get("instructions") or None,
    )
    if step_type == "email" and not (vals["template_id"] or (vals["subject"] or vals["thread_reply"]) and vals["body"]):
        flash("Email steps need a template, or a body (plus a subject unless sent as a reply in-thread).", "error")
        return redirect(url_for("main.sequence_detail", sid=sid))
    if step_id:
        conn.execute(
            "UPDATE sequence_steps SET step_type=:step_type, delay_days=:delay_days, template_id=:template_id, "
            "subject=:subject, body=:body, thread_reply=:thread_reply, instructions=:instructions "
            "WHERE id=:id AND sequence_id=:sid", dict(vals, id=step_id, sid=sid))
    else:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM sequence_steps WHERE sequence_id = ?",
                           (sid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO sequence_steps(sequence_id, position, step_type, delay_days, template_id, subject, body, "
            "thread_reply, instructions) VALUES (:sid, :pos, :step_type, :delay_days, :template_id, :subject, "
            ":body, :thread_reply, :instructions)", dict(vals, sid=sid, pos=pos))
    conn.commit()
    flash("Step saved.", "ok")
    return redirect(url_for("main.sequence_detail", sid=sid))


def _renumber(conn, sid, order=None):
    """Re-number step positions 1..n, keeping enrollments pointed at the same step."""
    if order is None:
        order = [r["id"] for r in conn.execute(
            "SELECT id FROM sequence_steps WHERE sequence_id = ? ORDER BY position", (sid,))]
    old = {r["id"]: r["position"] for r in conn.execute(
        "SELECT id, position FROM sequence_steps WHERE sequence_id = ?", (sid,))}
    # move to negative positions first to avoid clashes, then remap enrollments
    mapping = {}
    for new_pos, step_id in enumerate(order, start=1):
        mapping[old[step_id]] = new_pos
        conn.execute("UPDATE sequence_steps SET position = ? WHERE id = ?", (-new_pos, step_id))
    conn.execute("UPDATE sequence_steps SET position = -position WHERE sequence_id = ?", (sid,))
    for e in conn.execute("SELECT id, current_step FROM enrollments WHERE sequence_id = ? AND status IN "
                          "('active','paused')", (sid,)).fetchall():
        if e["current_step"] in mapping:
            conn.execute("UPDATE enrollments SET current_step = ? WHERE id = ?", (mapping[e["current_step"]], e["id"]))
        else:
            # its step was deleted: point at the next surviving step
            later = sorted(p for p in mapping if p > e["current_step"])
            if later:
                conn.execute("UPDATE enrollments SET current_step = ? WHERE id = ?", (mapping[later[0]], e["id"]))
            else:
                conn.execute("UPDATE enrollments SET status = 'completed', next_run_at = NULL, finished_at = ? "
                             "WHERE id = ?", (ts(), e["id"]))


@bp.route("/run", methods=["POST"])
def run_now():
    conn = get_db()
    stats = engine.process_due(conn, current_app.extensions["mailer"], current_app.config["SECRET_KEY"])
    flash("Engine run: " + ", ".join(f"{k} {v}" for k, v in stats.items() if v) if any(stats.values())
          else "Engine run: nothing due right now.", "ok")
    return redirect(request.referrer or url_for("main.dashboard"))


# ------------------------------------------------------------------ tasks

@bp.route("/tasks")
def tasks():
    conn = get_db()
    now = utcnow()
    end_today = ts(now.replace(hour=23, minute=59, second=59))
    base = ("SELECT t.*, c.first_name, c.last_name, c.email, c.phone, c.linkedin_url, c.title contact_title, "
            "co.name company_name, s.name seq_name FROM tasks t "
            "LEFT JOIN contacts c ON c.id = t.contact_id LEFT JOIN companies co ON co.id = c.company_id "
            "LEFT JOIN enrollments e ON e.id = t.enrollment_id LEFT JOIN sequences s ON s.id = e.sequence_id "
            "WHERE t.status = 'open' AND (? = '' OR t.task_type = ?) ")
    tt = request.args.get("type", "")
    due = conn.execute(base + "AND t.due_at <= ? ORDER BY t.due_at", (tt, tt, end_today)).fetchall()
    upcoming = conn.execute(base + "AND t.due_at > ? ORDER BY t.due_at LIMIT 100", (tt, tt, end_today)).fetchall()
    done = conn.execute(
        "SELECT t.*, c.first_name, c.last_name, c.email FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id "
        "WHERE t.status != 'open' AND t.completed_at >= ? ORDER BY t.completed_at DESC",
        (ts(now - timedelta(days=1)),)).fetchall()
    return render_template("tasks.html", due=due, upcoming=upcoming, done=done, type=tt, now=ts(now))


@bp.route("/tasks/<int:task_id>", methods=["POST"])
def task_action(task_id):
    conn = get_db()
    action = request.form.get("action", "done")
    if action in ("done", "skipped"):
        engine.complete_task(conn, task_id, request.form.get("outcome", ""), status=action)
        t = conn.execute("SELECT contact_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        outcome = request.form.get("outcome", "")
        if t and t["contact_id"] and outcome in ("meeting booked", "interested"):
            kind = "meeting" if outcome == "meeting booked" else "note"
            log_activity(conn, kind, f"Task outcome: {outcome}", contact_id=t["contact_id"])
            if outcome == "meeting booked":
                conn.execute("UPDATE contacts SET status = 'meeting' WHERE id = ?", (t["contact_id"],))
                engine.stop_enrollments(conn, t["contact_id"], "replied", only_stop_on_reply=True)
            conn.commit()
    elif action == "snooze":
        days = int(request.form.get("days") or 1)
        conn.execute("UPDATE tasks SET due_at = ? WHERE id = ?", (ts(utcnow() + timedelta(days=days)), task_id))
        conn.commit()
    return redirect(request.referrer or url_for("main.tasks"))
