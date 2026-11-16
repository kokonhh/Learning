"""Contacts & companies."""
from __future__ import annotations

import csv
import io
import json

from flask import Response, abort, current_app, flash, redirect, render_template, request, url_for

from .. import engine, importer, scoring
from ..db import get_db, get_settings, log_activity, row_to_dict, suppress, ts
from . import bp

STATUSES = ["new", "contacted", "engaged", "replied", "meeting", "qualified", "customer", "lost",
            "unsubscribed", "bounced"]
SORTS = {
    "score": "c.score DESC, c.id DESC",
    "recent": "c.id DESC",
    "name": "lower(c.first_name), lower(c.last_name)",
    "last_contacted": "c.last_contacted_at IS NULL, c.last_contacted_at DESC",
}


def _contact_or_404(conn, cid):
    row = conn.execute(
        "SELECT c.*, co.name company_name, co.domain company_domain FROM contacts c "
        "LEFT JOIN companies co ON co.id = c.company_id WHERE c.id = ?", (cid,)).fetchone()
    if row is None:
        abort(404)
    return row_to_dict(row)


def _filtered_contacts(conn, args):
    where, params = [], []
    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q.lower()}%"
        where.append("(lower(c.email) LIKE ? OR lower(c.first_name || ' ' || COALESCE(c.last_name,'')) LIKE ? "
                     "OR lower(COALESCE(co.name,'')) LIKE ? OR lower(COALESCE(c.title,'')) LIKE ?)")
        params += [like] * 4
    if args.get("status"):
        where.append("c.status = ?")
        params.append(args["status"])
    if args.get("tag"):
        where.append("(',' || c.tags || ',') LIKE ?")
        params.append(f"%,{args['tag'].lower()},%")
    if args.get("company_id"):
        where.append("c.company_id = ?")
        params.append(args["company_id"])
    sql = ("SELECT c.*, co.name company_name FROM contacts c LEFT JOIN companies co ON co.id = c.company_id"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {SORTS.get(args.get('sort'), SORTS['score'])}")
    return sql, params


@bp.route("/contacts")
def contacts():
    conn = get_db()
    sql, params = _filtered_contacts(conn, request.args)
    page = max(int(request.args.get("page", 1) or 1), 1)
    per = 50
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    rows = conn.execute(sql + " LIMIT ? OFFSET ?", params + [per, (page - 1) * per]).fetchall()
    all_tags = sorted({t for r in conn.execute("SELECT tags FROM contacts WHERE tags != ''")
                       for t in r["tags"].split(",") if t})
    sequences = conn.execute("SELECT id, name, status FROM sequences WHERE status != 'archived' ORDER BY name").fetchall()
    return render_template("contacts/list.html", contacts=rows, total=total, page=page, per=per,
                           statuses=STATUSES, tags=all_tags, sequences=sequences, args=request.args)


@bp.route("/contacts/bulk", methods=["POST"])
def contacts_bulk():
    conn = get_db()
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    action = request.form.get("action")
    if not ids:
        flash("Select at least one contact.", "warn")
        return redirect(request.referrer or url_for("main.contacts"))
    if action == "enroll":
        seq_id = int(request.form.get("sequence_id") or 0)
        try:
            res = engine.enroll(conn, seq_id, ids)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Enrolled {res['enrolled']} · skipped {res['skipped_existing']} already enrolled, "
                  f"{res['skipped_suppressed'] + res['skipped_status']} suppressed/unsubscribed.", "ok")
    elif action == "tag":
        tag = importer.normalise_tags(request.form.get("tag", ""))
        for cid in ids:
            cur = conn.execute("SELECT tags FROM contacts WHERE id = ?", (cid,)).fetchone()
            if cur:
                conn.execute("UPDATE contacts SET tags = ? WHERE id = ?",
                             (importer.normalise_tags(cur["tags"] + "," + tag), cid))
        conn.commit()
        flash(f"Tagged {len(ids)} contacts.", "ok")
    elif action == "status":
        status = request.form.get("status")
        if status in STATUSES:
            conn.executemany("UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?",
                             [(status, ts(), cid) for cid in ids])
            conn.commit()
            flash(f"Updated {len(ids)} contacts.", "ok")
    elif action == "delete":
        conn.executemany("DELETE FROM contacts WHERE id = ?", [(cid,) for cid in ids])
        conn.commit()
        flash(f"Deleted {len(ids)} contacts.", "ok")
    return redirect(request.referrer or url_for("main.contacts"))


def _save_contact(conn, form, contact_id=None):
    email = (form.get("email") or "").strip().lower()
    if not importer.EMAIL_RE.match(email):
        raise ValueError("A valid email is required.")
    dupe = conn.execute("SELECT id FROM contacts WHERE lower(email) = ? AND id != ?",
                        (email, contact_id or 0)).fetchone()
    if dupe:
        raise ValueError("Another contact already uses that email.")
    company_id = importer.upsert_company(conn, form.get("company", ""), form.get("domain", ""),
                                         form.get("industry", ""), form.get("size", ""))
    custom = {}
    for line in (form.get("custom_fields") or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                custom[k.strip()] = v.strip()
    vals = (email, form.get("first_name") or None, form.get("last_name") or None, form.get("title") or None,
            form.get("phone") or None, form.get("linkedin_url") or None, company_id,
            importer.normalise_tags(form.get("tags", "")), form.get("timezone") or None,
            form.get("owner") or None, json.dumps(custom))
    if contact_id:
        status = form.get("status") if form.get("status") in STATUSES else None
        conn.execute(
            "UPDATE contacts SET email=?, first_name=?, last_name=?, title=?, phone=?, linkedin_url=?, company_id=?, "
            "tags=?, timezone=?, owner=?, custom_fields=?, status=COALESCE(?, status), updated_at=? WHERE id=?",
            vals + (status, ts(), contact_id))
    else:
        cur = conn.execute(
            "INSERT INTO contacts(email, first_name, last_name, title, phone, linkedin_url, company_id, tags, "
            "timezone, owner, custom_fields, source) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'manual')", vals)
        contact_id = cur.lastrowid
        log_activity(conn, "created", "Created manually", contact_id=contact_id)
    scoring.compute(conn, contact_id)
    conn.commit()
    return contact_id


@bp.route("/contacts/new", methods=["GET", "POST"])
def contact_new():
    conn = get_db()
    if request.method == "POST":
        try:
            cid = _save_contact(conn, request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("contacts/form.html", contact=request.form, statuses=STATUSES)
        flash("Contact created.", "ok")
        return redirect(url_for("main.contact_detail", cid=cid))
    return render_template("contacts/form.html", contact={}, statuses=STATUSES)


@bp.route("/contacts/<int:cid>/edit", methods=["GET", "POST"])
def contact_edit(cid):
    conn = get_db()
    contact = _contact_or_404(conn, cid)
    if request.method == "POST":
        try:
            _save_contact(conn, request.form, cid)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("contacts/form.html", contact=request.form, statuses=STATUSES, cid=cid)
        flash("Saved.", "ok")
        return redirect(url_for("main.contact_detail", cid=cid))
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (contact["company_id"],)).fetchone() \
        if contact["company_id"] else None
    form = dict(contact)
    form["company"] = company["name"] if company else ""
    form["domain"] = company["domain"] if company else ""
    form["industry"] = company["industry"] if company else ""
    form["size"] = company["size"] if company else ""
    form["custom_fields"] = "\n".join(f"{k}: {v}" for k, v in contact["custom_fields"].items())
    return render_template("contacts/form.html", contact=form, statuses=STATUSES, cid=cid)


@bp.route("/contacts/<int:cid>")
def contact_detail(cid):
    conn = get_db()
    contact = _contact_or_404(conn, cid)
    return render_template(
        "contacts/detail.html",
        c=contact,
        activities=conn.execute("SELECT * FROM activities WHERE contact_id = ? ORDER BY id DESC LIMIT 200",
                                (cid,)).fetchall(),
        messages=conn.execute("SELECT * FROM messages WHERE contact_id = ? ORDER BY id DESC", (cid,)).fetchall(),
        enrollments=conn.execute(
            "SELECT e.*, s.name seq_name FROM enrollments e JOIN sequences s ON s.id = e.sequence_id "
            "WHERE e.contact_id = ? ORDER BY e.id DESC", (cid,)).fetchall(),
        tasks=conn.execute("SELECT * FROM tasks WHERE contact_id = ? AND status = 'open' ORDER BY due_at",
                           (cid,)).fetchall(),
        deals=conn.execute("SELECT * FROM deals WHERE contact_id = ? ORDER BY id DESC", (cid,)).fetchall(),
        sequences=conn.execute("SELECT id, name FROM sequences WHERE status != 'archived' ORDER BY name").fetchall(),
        templates=conn.execute("SELECT id, name FROM templates ORDER BY name").fetchall(),
        statuses=STATUSES,
        next_run_label=engine.next_run_label,
    )


@bp.route("/contacts/<int:cid>/action", methods=["POST"])
def contact_action(cid):
    conn = get_db()
    contact = _contact_or_404(conn, cid)
    action = request.form.get("action")
    text = (request.form.get("text") or "").strip()
    if action == "note" and text:
        log_activity(conn, "note", text, contact_id=cid)
        conn.commit()
    elif action == "call":
        log_activity(conn, "call", text or "Logged a call", contact_id=cid)
        conn.execute("UPDATE contacts SET last_contacted_at = ?, status = CASE WHEN status='new' THEN 'contacted' "
                     "ELSE status END WHERE id = ?", (ts(), cid))
        conn.commit()
    elif action == "replied":
        engine.mark_replied(conn, cid, detail=text or "Marked as replied")
    elif action == "meeting":
        log_activity(conn, "meeting", text or "Meeting booked", contact_id=cid)
        conn.execute("UPDATE contacts SET status = 'meeting' WHERE id = ? AND status NOT IN ('qualified','customer')",
                     (cid,))
        engine.stop_enrollments(conn, cid, "replied", only_stop_on_reply=True)
        scoring.compute(conn, cid)
        conn.commit()
        flash("Meeting logged — active sequences stopped.", "ok")
    elif action == "unsubscribe":
        engine.unsubscribe(conn, cid)
        flash(f"{contact['email']} unsubscribed and suppressed.", "ok")
    elif action == "enroll":
        try:
            res = engine.enroll(conn, int(request.form.get("sequence_id") or 0), [cid])
            flash("Enrolled." if res["enrolled"] else "Not enrolled (already enrolled or suppressed).",
                  "ok" if res["enrolled"] else "warn")
        except ValueError as exc:
            flash(str(exc), "error")
    elif action == "task" and text:
        conn.execute("INSERT INTO tasks(contact_id, task_type, title, due_at) VALUES (?, ?, ?, ?)",
                     (cid, request.form.get("task_type") or "todo", text,
                      (request.form.get("due") or ts()[:10]) + " 09:00:00"))
        conn.commit()
        flash("Task added.", "ok")
    elif action == "delete":
        conn.execute("DELETE FROM contacts WHERE id = ?", (cid,))
        conn.commit()
        flash("Contact deleted.", "ok")
        return redirect(url_for("main.contacts"))
    return redirect(url_for("main.contact_detail", cid=cid))


@bp.route("/contacts/<int:cid>/email", methods=["POST"])
def contact_email(cid):
    """Send a one-off email (not part of a sequence)."""
    conn = get_db()
    contact, company = engine._load_contact(conn, cid)
    if contact["status"] in engine.TERMINAL_CONTACT_STATUSES:
        flash("This contact is unsubscribed/bounced — not sending.", "error")
        return redirect(url_for("main.contact_detail", cid=cid))
    step = {"id": None, "subject": request.form.get("subject", ""), "body": request.form.get("body", ""),
            "template_id": int(request.form["template_id"]) if request.form.get("template_id") else None,
            "thread_reply": 0}
    settings = get_settings(conn)
    res = engine.send_one(conn, current_app.extensions["mailer"], settings, current_app.config["SECRET_KEY"],
                          None, step, contact, company, engine.utcnow())
    conn.commit()
    if res["ok"]:
        flash("Email sent.", "ok")
    elif res["reason"] == "missing_fields":
        flash("Not sent — missing merge fields: " + ", ".join(res["missing"]), "error")
    else:
        flash(f"Not sent: {res.get('error') or res['reason']}", "error")
    return redirect(url_for("main.contact_detail", cid=cid))


@bp.route("/enrollments/<int:eid>/<action>", methods=["POST"])
def enrollment_action(eid, action):
    conn = get_db()
    e = conn.execute("SELECT * FROM enrollments WHERE id = ?", (eid,)).fetchone()
    if e is None:
        abort(404)
    if action == "pause":
        conn.execute("UPDATE enrollments SET status = 'paused' WHERE id = ? AND status = 'active'", (eid,))
    elif action == "resume":
        engine.resume_enrollment(conn, eid)
    elif action == "stop":
        conn.execute("UPDATE enrollments SET status = 'stopped', next_run_at = NULL, finished_at = ? WHERE id = ?",
                     (ts(), eid))
    conn.commit()
    return redirect(request.referrer or url_for("main.contact_detail", cid=e["contact_id"]))


@bp.route("/contacts/import", methods=["GET", "POST"])
def contacts_import():
    conn = get_db()
    sequences = conn.execute("SELECT id, name FROM sequences WHERE status != 'archived' ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.files.get("file")
        data = f.read() if f and f.filename else (request.form.get("csv_text") or "").encode()
        if not data.strip():
            flash("Upload a CSV file or paste CSV text.", "error")
            return render_template("contacts/import.html", sequences=sequences)
        res = importer.import_csv(conn, data, extra_tags=request.form.get("tags", ""),
                                  source=request.form.get("source") or "csv import",
                                  update_existing=bool(request.form.get("update_existing")))
        enrolled = None
        if request.form.get("sequence_id") and res["contact_ids"]:
            enrolled = engine.enroll(conn, int(request.form["sequence_id"]), res["contact_ids"])
        return render_template("contacts/import.html", sequences=sequences, result=res, enrolled=enrolled)
    return render_template("contacts/import.html", sequences=sequences)


@bp.route("/contacts/export.csv")
def contacts_export():
    conn = get_db()
    sql, params = _filtered_contacts(conn, request.args)
    rows = conn.execute(sql, params).fetchall()
    custom_keys = sorted({k for r in rows for k in json.loads(r["custom_fields"] or "{}")})
    buf = io.StringIO()
    w = csv.writer(buf)
    base = ["email", "first_name", "last_name", "title", "company_name", "phone", "linkedin_url", "status",
            "score", "tags", "source", "owner", "last_contacted_at", "created_at"]
    w.writerow(base + custom_keys)
    for r in rows:
        custom = json.loads(r["custom_fields"] or "{}")
        w.writerow([r[k] for k in base] + [custom.get(k, "") for k in custom_keys])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=contacts.csv"})


@bp.route("/companies")
def companies():
    conn = get_db()
    rows = conn.execute(
        "SELECT co.*, COUNT(DISTINCT c.id) n_contacts, COUNT(DISTINCT d.id) n_deals, "
        "COALESCE((SELECT SUM(value) FROM deals WHERE company_id = co.id AND stage NOT IN ('won','lost')), 0) "
        "open_value, MAX(c.score) top_score "
        "FROM companies co LEFT JOIN contacts c ON c.company_id = co.id LEFT JOIN deals d ON d.company_id = co.id "
        "GROUP BY co.id ORDER BY top_score DESC NULLS LAST, co.name").fetchall()
    return render_template("companies/list.html", companies=rows)


@bp.route("/companies/<int:coid>", methods=["GET", "POST"])
def company_detail(coid):
    conn = get_db()
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (coid,)).fetchone()
    if co is None:
        abort(404)
    if request.method == "POST":
        conn.execute("UPDATE companies SET name=?, domain=?, industry=?, size=?, website=?, notes=? WHERE id=?",
                     (request.form["name"], request.form.get("domain"), request.form.get("industry"),
                      request.form.get("size"), request.form.get("website"), request.form.get("notes"), coid))
        conn.commit()
        flash("Company saved.", "ok")
        return redirect(url_for("main.company_detail", coid=coid))
    return render_template(
        "companies/detail.html", co=co,
        contacts=conn.execute("SELECT * FROM contacts WHERE company_id = ? ORDER BY score DESC", (coid,)).fetchall(),
        deals=conn.execute("SELECT * FROM deals WHERE company_id = ? ORDER BY id DESC", (coid,)).fetchall(),
    )


@bp.route("/suppressions", methods=["GET", "POST"])
def suppressions():
    conn = get_db()
    if request.method == "POST":
        if request.form.get("remove"):
            conn.execute("DELETE FROM suppressions WHERE email = ?", (request.form["remove"],))
        else:
            added = 0
            for line in (request.form.get("emails") or "").replace(",", "\n").splitlines():
                line = line.strip().lower()
                if importer.EMAIL_RE.match(line):
                    suppress(conn, line, "manual")
                    row = conn.execute("SELECT id FROM contacts WHERE lower(email) = ?", (line,)).fetchone()
                    if row:
                        engine.unsubscribe(conn, row["id"], reason="manual")
                    added += 1
            flash(f"Added {added} addresses to the suppression list.", "ok")
        conn.commit()
        return redirect(url_for("main.suppressions"))
    rows = conn.execute("SELECT * FROM suppressions ORDER BY created_at DESC").fetchall()
    return render_template("suppressions.html", rows=rows)
