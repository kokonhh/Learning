"""The sequence engine: enrollment, scheduled sending, manual-step tasks and stop rules."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import scoring, templating, tracking
from .db import get_settings, is_suppressed, log_activity, parse_ts, suppress, ts, utcnow
from .mailer import SendError, build_message

MANUAL_STEP_TYPES = ("call", "linkedin", "task")
TERMINAL_CONTACT_STATUSES = ("unsubscribed", "bounced")


# --------------------------------------------------------------------------- enrollment

def enroll(conn, sequence_id: int, contact_ids, now: datetime | None = None) -> dict:
    now = now or utcnow()
    first = conn.execute(
        "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position LIMIT 1", (sequence_id,)
    ).fetchone()
    result = {"enrolled": 0, "skipped_suppressed": 0, "skipped_existing": 0, "skipped_status": 0}
    if first is None:
        raise ValueError("Sequence has no steps")
    seq = conn.execute("SELECT name FROM sequences WHERE id = ?", (sequence_id,)).fetchone()
    for cid in contact_ids:
        contact = conn.execute("SELECT id, email, status FROM contacts WHERE id = ?", (cid,)).fetchone()
        if contact is None:
            continue
        if is_suppressed(conn, contact["email"]):
            result["skipped_suppressed"] += 1
            continue
        if contact["status"] in TERMINAL_CONTACT_STATUSES:
            result["skipped_status"] += 1
            continue
        active = conn.execute(
            "SELECT 1 FROM enrollments WHERE contact_id = ? AND sequence_id = ?", (cid, sequence_id)
        ).fetchone()
        if active:
            result["skipped_existing"] += 1
            continue
        run_at = now + timedelta(days=first["delay_days"])
        conn.execute(
            "INSERT INTO enrollments(sequence_id, contact_id, current_step, status, next_run_at, enrolled_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (sequence_id, cid, first["position"], ts(run_at), ts(now)),
        )
        log_activity(conn, "enrolled", f"Enrolled in sequence “{seq['name']}”", contact_id=cid)
        result["enrolled"] += 1
    conn.commit()
    return result


def _next_step(conn, sequence_id: int, after_position: int):
    return conn.execute(
        "SELECT * FROM sequence_steps WHERE sequence_id = ? AND position > ? ORDER BY position LIMIT 1",
        (sequence_id, after_position),
    ).fetchone()


def advance(conn, enrollment_id: int, now: datetime | None = None) -> None:
    """Move an enrollment to its next step (or complete it)."""
    now = now or utcnow()
    e = conn.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if e is None or e["status"] != "active":
        return
    nxt = _next_step(conn, e["sequence_id"], e["current_step"])
    if nxt is None:
        conn.execute(
            "UPDATE enrollments SET status = 'completed', next_run_at = NULL, finished_at = ? WHERE id = ?",
            (ts(now), enrollment_id),
        )
        log_activity(conn, "sequence_completed", "Finished all sequence steps", contact_id=e["contact_id"])
    else:
        conn.execute(
            "UPDATE enrollments SET current_step = ?, next_run_at = ? WHERE id = ?",
            (nxt["position"], ts(now + timedelta(days=nxt["delay_days"])), enrollment_id),
        )


def stop_enrollments(conn, contact_id: int, status: str, only_stop_on_reply: bool = False) -> int:
    q = ("UPDATE enrollments SET status = ?, next_run_at = NULL, finished_at = ? "
         "WHERE contact_id = ? AND status IN ('active', 'paused')")
    if only_stop_on_reply:
        q += " AND sequence_id IN (SELECT id FROM sequences WHERE stop_on_reply = 1)"
    cur = conn.execute(q, (status, ts(), contact_id))
    # close any open sequence tasks for stopped enrollments
    conn.execute(
        "UPDATE tasks SET status = 'skipped', outcome = ? WHERE contact_id = ? AND status = 'open' "
        "AND enrollment_id IN (SELECT id FROM enrollments WHERE contact_id = ? AND status = ?)",
        (f"sequence stopped ({status})", contact_id, contact_id, status),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- events

def mark_replied(conn, contact_id: int, detail: str = "Reply received", message_id: int | None = None) -> None:
    now = ts()
    if message_id is None:
        last = conn.execute(
            "SELECT id FROM messages WHERE contact_id = ? AND direction = 'out' ORDER BY id DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        message_id = last["id"] if last else None
    if message_id:
        conn.execute("UPDATE messages SET replied_at = COALESCE(replied_at, ?) WHERE id = ?", (now, message_id))
    conn.execute(
        "UPDATE contacts SET status = 'replied', updated_at = ? WHERE id = ? "
        "AND status IN ('new', 'contacted', 'engaged')",
        (now, contact_id),
    )
    stop_enrollments(conn, contact_id, "replied", only_stop_on_reply=True)
    log_activity(conn, "replied", detail, contact_id=contact_id)
    conn.execute(
        "INSERT INTO tasks(contact_id, task_type, title, due_at) VALUES (?, 'follow_up', ?, ?)",
        (contact_id, "Respond to reply", now),
    )
    scoring.compute(conn, contact_id)
    conn.commit()


def unsubscribe(conn, contact_id: int, reason: str = "unsubscribed") -> None:
    c = conn.execute("SELECT email FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if c is None:
        return
    suppress(conn, c["email"], reason)
    status = "bounced" if reason == "bounced" else "unsubscribed"
    conn.execute("UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?", (status, ts(), contact_id))
    stop_enrollments(conn, contact_id, status)
    log_activity(conn, status, f"Contact {status}", contact_id=contact_id)
    scoring.compute(conn, contact_id)
    conn.commit()


def record_open(conn, token: str) -> bool:
    m = conn.execute("SELECT id, contact_id, opened_at FROM messages WHERE token = ?", (token,)).fetchone()
    if m is None:
        return False
    conn.execute(
        "UPDATE messages SET open_count = open_count + 1, opened_at = COALESCE(opened_at, ?) WHERE id = ?",
        (ts(), m["id"]),
    )
    if m["opened_at"] is None:
        log_activity(conn, "email_opened", "Opened email", contact_id=m["contact_id"])
        _bump_engaged(conn, m["contact_id"])
    scoring.compute(conn, m["contact_id"])
    conn.commit()
    return True


def record_click(conn, token: str, url: str) -> bool:
    m = conn.execute("SELECT id, contact_id FROM messages WHERE token = ?", (token,)).fetchone()
    if m is None:
        return False
    now = ts()
    conn.execute(
        "UPDATE messages SET click_count = click_count + 1, clicked_at = COALESCE(clicked_at, ?), "
        "opened_at = COALESCE(opened_at, ?) WHERE id = ?",
        (now, now, m["id"]),
    )
    log_activity(conn, "link_clicked", url, contact_id=m["contact_id"])
    _bump_engaged(conn, m["contact_id"])
    scoring.compute(conn, m["contact_id"])
    conn.commit()
    return True


def _bump_engaged(conn, contact_id: int) -> None:
    conn.execute(
        "UPDATE contacts SET status = 'engaged' WHERE id = ? AND status IN ('new', 'contacted')", (contact_id,)
    )


def complete_task(conn, task_id: int, outcome: str = "", status: str = "done") -> None:
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None or t["status"] != "open":
        return
    conn.execute(
        "UPDATE tasks SET status = ?, outcome = ?, completed_at = ? WHERE id = ?",
        (status, outcome, ts(), task_id),
    )
    if t["contact_id"]:
        kind = {"call": "call", "linkedin": "linkedin"}.get(t["task_type"], "task_done")
        log_activity(conn, kind, f"{t['title']} — {status}{': ' + outcome if outcome else ''}",
                     contact_id=t["contact_id"])
        if t["task_type"] in ("call", "linkedin") and status == "done":
            conn.execute("UPDATE contacts SET last_contacted_at = ?, status = CASE WHEN status = 'new' "
                         "THEN 'contacted' ELSE status END WHERE id = ?", (ts(), t["contact_id"]))
    if t["enrollment_id"]:
        e = conn.execute("SELECT * FROM enrollments WHERE id = ?", (t["enrollment_id"],)).fetchone()
        step = conn.execute("SELECT position FROM sequence_steps WHERE id = ?", (t["step_id"],)).fetchone()
        if e and step and e["status"] == "active" and e["current_step"] == step["position"]:
            advance(conn, e["id"])
    conn.commit()


# --------------------------------------------------------------------------- sending

def in_send_window(settings: dict, now: datetime) -> bool:
    try:
        tz = ZoneInfo(settings.get("send_timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    if settings.get("send_weekdays_only", "1") == "1" and local.weekday() >= 5:
        return False
    start, end = int(settings.get("send_window_start", 0)), int(settings.get("send_window_end", 24))
    return start <= local.hour < end


def sent_today(conn, now: datetime) -> int:
    day_start = now.replace(hour=0, minute=0, second=0)
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction = 'out' AND status = 'sent' AND created_at >= ?",
        (ts(day_start),),
    ).fetchone()[0]


def _load_contact(conn, contact_id):
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    contact = dict(contact)
    try:
        contact["custom_fields"] = json.loads(contact.get("custom_fields") or "{}")
    except ValueError:
        contact["custom_fields"] = {}
    company = None
    if contact.get("company_id"):
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (contact["company_id"],)).fetchone()
        company = dict(row) if row else None
    return contact, company


def compose(conn, step, contact, company, settings, enrollment=None) -> dict:
    """Render the subject/body for a step and contact. Returns dict with missing fields."""
    subject, body = step["subject"], step["body"]
    if step["template_id"]:
        tpl = conn.execute("SELECT subject, body FROM templates WHERE id = ?", (step["template_id"],)).fetchone()
        if tpl:
            subject = subject or tpl["subject"]
            body = body or tpl["body"]
    subject, body = subject or "", body or ""
    ctx = templating.build_context(contact, company, settings)
    seed = str(contact["id"])
    threaded = bool(step["thread_reply"]) and enrollment is not None and enrollment["thread_subject"]
    if threaded:
        subj = enrollment["thread_subject"]
        subj = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        missing_s = []
    else:
        subj, missing_s = templating.render(subject, ctx, seed)
    body_r, missing_b = templating.render(body, ctx, seed)
    sig, _ = templating.render(settings.get("signature", ""), ctx, seed)
    full_text = body_r.rstrip() + ("\n\n" + sig if sig.strip() else "")
    return {
        "subject": subj,
        "text": full_text,
        "missing": sorted(set(missing_s + missing_b)),
        "threaded": bool(threaded),
    }


def send_one(conn, mailer, settings, secret, enrollment, step, contact, company, now) -> dict:
    composed = compose(conn, step, contact, company, settings, enrollment)
    if composed["missing"]:
        return {"ok": False, "reason": "missing_fields", "missing": composed["missing"]}
    if not composed["subject"].strip() or not composed["text"].strip():
        return {"ok": False, "reason": "empty"}

    token = tracking.new_token()
    base_url = settings.get("base_url", "http://localhost:5000")
    unsub = tracking.unsubscribe_url(base_url, token)
    footer_html = (
        '<p style="color:#888;font-size:11px;margin-top:24px">'
        f'{templating.html.escape(settings.get("company_name", ""))} · '
        f'{templating.html.escape(settings.get("physical_address", ""))}<br>'
        f'Not interested? <a href="{unsub}">Unsubscribe</a></p>'
    )
    body_html = tracking.decorate_html(
        templating.text_to_html(composed["text"]),
        base_url=base_url, token=token, secret=secret,
        track_opens=settings.get("track_opens") == "1",
        track_clicks=settings.get("track_clicks") == "1",
        footer_html=footer_html,
    )
    text = (composed["text"] + f"\n\n--\n{settings.get('company_name', '')} · "
            f"{settings.get('physical_address', '')}\nUnsubscribe: {unsub}")
    msg = build_message(
        sender_name=settings.get("sender_name", ""), sender_email=settings.get("sender_email", ""),
        to=contact["email"], subject=composed["subject"], html=body_html, text=text,
        unsubscribe_url=unsub,
        in_reply_to=enrollment["thread_message_id"] if composed["threaded"] else None,
    )
    status, error, message_id = "sent", None, None
    try:
        message_id = mailer.send(msg)
    except SendError as exc:
        status, error = "failed", str(exc)

    conn.execute(
        "INSERT INTO messages(contact_id, enrollment_id, step_id, direction, subject, body_html, body_text, "
        "message_id, token, status, error, created_at) VALUES (?, ?, ?, 'out', ?, ?, ?, ?, ?, ?, ?, ?)",
        (contact["id"], enrollment["id"] if enrollment else None, step["id"] if step else None,
         composed["subject"], body_html, text, message_id, token, status, error, ts(now)),
    )
    if status == "sent":
        log_activity(conn, "email_sent", composed["subject"], contact_id=contact["id"])
        conn.execute(
            "UPDATE contacts SET last_contacted_at = ?, updated_at = ?, "
            "status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END WHERE id = ?",
            (ts(now), ts(now), contact["id"]),
        )
        if enrollment is not None and not enrollment["thread_subject"]:
            conn.execute(
                "UPDATE enrollments SET thread_subject = ?, thread_message_id = ? WHERE id = ?",
                (composed["subject"], message_id, enrollment["id"]),
            )
        return {"ok": True, "message_id": message_id}
    return {"ok": False, "reason": "send_failed", "error": error}


def process_due(conn, mailer, secret: str, now: datetime | None = None, limit: int = 500) -> dict:
    """Run every due sequence step. Safe to call repeatedly (cron / worker loop)."""
    now = now or utcnow()
    settings = get_settings(conn)
    stats = {"sent": 0, "tasks_created": 0, "completed": 0, "paused": 0, "failed": 0,
             "stopped": 0, "deferred": 0}
    window_open = in_send_window(settings, now)
    remaining = max(int(settings.get("daily_send_limit", 50)) - sent_today(conn, now), 0)
    gap = int(settings.get("min_seconds_between_sends", 0) or 0)

    due = conn.execute(
        "SELECT e.* FROM enrollments e JOIN sequences s ON s.id = e.sequence_id "
        "WHERE e.status = 'active' AND s.status = 'active' AND e.next_run_at IS NOT NULL "
        "AND e.next_run_at <= ? ORDER BY e.next_run_at LIMIT ?",
        (ts(now), limit),
    ).fetchall()

    for e in due:
        step = conn.execute(
            "SELECT * FROM sequence_steps WHERE sequence_id = ? AND position = ?",
            (e["sequence_id"], e["current_step"]),
        ).fetchone()
        if step is None:
            # step was deleted: skip ahead
            advance(conn, e["id"], now)
            stats["completed"] += 1
            continue
        contact, company = _load_contact(conn, e["contact_id"])
        if contact["status"] in TERMINAL_CONTACT_STATUSES or is_suppressed(conn, contact["email"]):
            stop_enrollments(conn, contact["id"], "stopped")
            stats["stopped"] += 1
            continue

        if step["step_type"] in MANUAL_STEP_TYPES:
            title = {
                "call": "Call", "linkedin": "LinkedIn touch", "task": "Task",
            }[step["step_type"]] + f" — {contact.get('first_name') or contact['email']}"
            conn.execute(
                "INSERT INTO tasks(contact_id, enrollment_id, step_id, task_type, title, notes, due_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contact["id"], e["id"], step["id"],
                 "todo" if step["step_type"] == "task" else step["step_type"],
                 title, step["instructions"], ts(now)),
            )
            # wait for the rep to complete the task before moving on
            conn.execute("UPDATE enrollments SET next_run_at = NULL WHERE id = ?", (e["id"],))
            stats["tasks_created"] += 1
            continue

        # email step
        if not window_open or remaining <= 0:
            stats["deferred"] += 1
            continue
        result = send_one(conn, mailer, settings, secret, e, step, contact, company, now)
        if result["ok"]:
            stats["sent"] += 1
            remaining -= 1
            e = conn.execute("SELECT * FROM enrollments WHERE id = ?", (e["id"],)).fetchone()
            advance(conn, e["id"], now)
            if gap:
                conn.commit()
                time.sleep(gap)
        elif result["reason"] in ("missing_fields", "empty"):
            detail = ("Paused: missing merge fields " + ", ".join(result.get("missing", []))
                      if result["reason"] == "missing_fields" else "Paused: email step has no content")
            conn.execute("UPDATE enrollments SET status = 'paused' WHERE id = ?", (e["id"],))
            log_activity(conn, "sequence_paused", detail, contact_id=contact["id"])
            stats["paused"] += 1
        else:
            # transient failure: retry in an hour
            conn.execute("UPDATE enrollments SET next_run_at = ? WHERE id = ?",
                         (ts(now + timedelta(hours=1)), e["id"]))
            log_activity(conn, "send_failed", result.get("error", ""), contact_id=contact["id"])
            stats["failed"] += 1
        conn.commit()

    conn.commit()
    return stats


def resume_enrollment(conn, enrollment_id: int, now: datetime | None = None) -> None:
    now = now or utcnow()
    conn.execute(
        "UPDATE enrollments SET status = 'active', next_run_at = COALESCE(next_run_at, ?) "
        "WHERE id = ? AND status = 'paused'",
        (ts(now), enrollment_id),
    )
    conn.commit()


def next_run_label(value: str | None) -> str:
    dt = parse_ts(value)
    if dt is None:
        return "waiting on task"
    delta = dt - utcnow()
    if delta.total_seconds() <= 0:
        return "due now"
    if delta.days >= 1:
        return f"in {delta.days}d"
    return f"in {int(delta.total_seconds() // 3600)}h"
