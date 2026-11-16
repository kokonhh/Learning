"""Reporting queries for the dashboard and sequence pages."""
from __future__ import annotations

from datetime import timedelta

from .db import get_settings, ts, utcnow


def _rate(num, den) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def email_stats(conn, since: str | None = None, sequence_id: int | None = None) -> dict:
    where, params = ["m.direction = 'out'", "m.status = 'sent'"], []
    if since:
        where.append("m.created_at >= ?")
        params.append(since)
    if sequence_id:
        where.append("e.sequence_id = ?")
        params.append(sequence_id)
    row = conn.execute(
        "SELECT COUNT(*) sent, COUNT(m.opened_at) opened, COUNT(m.clicked_at) clicked, "
        "COUNT(m.replied_at) replied FROM messages m LEFT JOIN enrollments e ON e.id = m.enrollment_id "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    d = dict(row)
    d["open_rate"] = _rate(d["opened"], d["sent"])
    d["click_rate"] = _rate(d["clicked"], d["sent"])
    d["reply_rate"] = _rate(d["replied"], d["sent"])
    return d


def daily_sends(conn, days: int = 14) -> list[dict]:
    start = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
    rows = {r["day"]: dict(r) for r in conn.execute(
        "SELECT substr(created_at, 1, 10) day, COUNT(*) sent, COUNT(opened_at) opened, COUNT(replied_at) replied "
        "FROM messages WHERE direction = 'out' AND status = 'sent' AND created_at >= ? GROUP BY day",
        (ts(start),),
    )}
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append(rows.get(day, {"day": day, "sent": 0, "opened": 0, "replied": 0}))
    return out


def pipeline(conn) -> dict:
    stages = [s.strip() for s in get_settings(conn)["pipeline_stages"].split(",") if s.strip()]
    rows = {r["stage"]: dict(r) for r in conn.execute(
        "SELECT stage, COUNT(*) n, COALESCE(SUM(value), 0) value, "
        "COALESCE(SUM(value * COALESCE(probability, 0) / 100.0), 0) weighted FROM deals GROUP BY stage"
    )}
    by_stage = [rows.get(s, {"stage": s, "n": 0, "value": 0, "weighted": 0}) for s in stages]
    open_stages = [s for s in stages if s not in ("won", "lost")]
    open_value = sum(r["value"] for r in by_stage if r["stage"] in open_stages)
    weighted = sum(r["weighted"] for r in by_stage if r["stage"] in open_stages)
    won = rows.get("won", {"n": 0, "value": 0})
    lost = rows.get("lost", {"n": 0, "value": 0})
    return {
        "stages": by_stage,
        "open_value": open_value,
        "weighted": weighted,
        "won_value": won["value"],
        "won_count": won["n"],
        "win_rate": _rate(won["n"], won["n"] + lost["n"]),
    }


def contact_funnel(conn) -> list[dict]:
    order = ["new", "contacted", "engaged", "replied", "meeting", "qualified", "customer"]
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
    # cumulative: anyone further down the funnel also passed the earlier stages
    out, running = [], 0
    for status in reversed(order):
        running += counts.get(status, 0)
        out.append({"status": status, "n": running})
    return list(reversed(out))


def sequence_report(conn, sequence_id: int) -> dict:
    enroll = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM enrollments WHERE sequence_id = ? GROUP BY status", (sequence_id,))}
    steps = []
    for s in conn.execute("SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position", (sequence_id,)):
        if s["step_type"] == "email":
            r = conn.execute(
                "SELECT COUNT(*) sent, COUNT(opened_at) opened, COUNT(clicked_at) clicked, COUNT(replied_at) replied "
                "FROM messages WHERE step_id = ? AND status = 'sent'", (s["id"],)).fetchone()
            d = dict(r)
            d.update(open_rate=_rate(d["opened"], d["sent"]), reply_rate=_rate(d["replied"], d["sent"]))
        else:
            r = conn.execute(
                "SELECT COUNT(*) total, SUM(status = 'done') done, SUM(status = 'open') open "
                "FROM tasks WHERE step_id = ?", (s["id"],)).fetchone()
            d = {k: (r[k] or 0) for k in ("total", "done", "open")}
        d["step"] = dict(s)
        steps.append(d)
    return {"enrollments": enroll, "total": sum(enroll.values()), "steps": steps,
            "email": email_stats(conn, sequence_id=sequence_id)}


def overview(conn) -> dict:
    since = ts(utcnow() - timedelta(days=30))
    return {
        "email_30d": email_stats(conn, since=since),
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "active_enrollments": conn.execute(
            "SELECT COUNT(*) FROM enrollments WHERE status = 'active'").fetchone()[0],
        "open_tasks": conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'open' AND due_at <= ?", (ts(),)).fetchone()[0],
        "meetings_30d": conn.execute(
            "SELECT COUNT(*) FROM activities WHERE kind = 'meeting' AND created_at >= ?", (since,)).fetchone()[0],
        "pipeline": pipeline(conn),
        "daily": daily_sends(conn),
        "funnel": contact_funnel(conn),
    }
