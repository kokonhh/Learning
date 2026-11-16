from datetime import timedelta

import pytest

from outreach import engine
from outreach.db import parse_ts, set_setting, suppress, utcnow

from .conftest import make_contact, make_sequence

SECRET = "test-secret"


def run(conn, mailer, now=None):
    return engine.process_due(conn, mailer, SECRET, now=now)


def test_full_cadence_email_task_threaded_followup(conn, mailer):
    cid = make_contact(conn, pain_point="slow follow-ups")
    sid = make_sequence(conn, [
        {"subject": "Idea for {{company}}", "body": "Hi {{first_name}}, re {{pain_point}}. https://seller.test/demo"},
        {"type": "call", "delay": 2, "instructions": "Call them"},
        {"delay": 3, "body": "Bumping this, {{first_name}}.", "thread": 1},
    ])
    now = utcnow()
    assert engine.enroll(conn, sid, [cid], now=now)["enrolled"] == 1

    # step 1: email goes out immediately
    stats = run(conn, mailer, now)
    assert stats["sent"] == 1
    msg = mailer.sent[-1]
    assert msg["Subject"] == "Idea for Acme"
    assert msg["To"] == "jane@acme.test"
    assert msg["List-Unsubscribe"].startswith("<https://out.seller.test/u/")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    html = msg.get_body(("html",)).get_content()
    assert "/t/c/" in html and "/t/o/" in html and "Unsubscribe" in html
    text = msg.get_body(("plain",)).get_content()
    assert "Hi Jane, re slow follow-ups." in text and "Rita Rep" in text  # signature appended

    e = conn.execute("SELECT * FROM enrollments").fetchone()
    assert e["current_step"] == 2 and parse_ts(e["next_run_at"]) == now + timedelta(days=2)
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "contacted"

    # nothing due before the delay elapses
    assert run(conn, mailer, now + timedelta(days=1))["sent"] == 0

    # step 2: manual call creates a task and the enrollment waits on it
    stats = run(conn, mailer, now + timedelta(days=2))
    assert stats["tasks_created"] == 1
    task = conn.execute("SELECT * FROM tasks").fetchone()
    assert task["task_type"] == "call" and task["notes"] == "Call them"
    assert conn.execute("SELECT next_run_at FROM enrollments").fetchone()[0] is None
    assert run(conn, mailer, now + timedelta(days=30))["sent"] == 0  # still waiting on the task

    engine.complete_task(conn, task["id"], "left voicemail")
    e = conn.execute("SELECT * FROM enrollments").fetchone()
    assert e["current_step"] == 3

    # step 3: threaded follow-up reuses the subject and sets In-Reply-To
    stats = run(conn, mailer, parse_ts(e["next_run_at"]))
    assert stats["sent"] == 1
    follow = mailer.sent[-1]
    assert follow["Subject"] == "Re: Idea for Acme"
    assert follow["In-Reply-To"] == msg["Message-ID"]
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "completed"


def test_missing_merge_field_pauses_instead_of_sending(conn, mailer):
    cid = make_contact(conn, first="")
    sid = make_sequence(conn, [{"subject": "Hi", "body": "Hi {{first_name}}"}])
    engine.enroll(conn, sid, [cid])
    stats = run(conn, mailer)
    assert stats["paused"] == 1 and not mailer.sent
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "paused"
    act = conn.execute("SELECT detail FROM activities WHERE kind = 'sequence_paused'").fetchone()
    assert "first_name" in act[0]


def test_reply_stops_sequence(conn, mailer):
    cid = make_contact(conn)
    sid = make_sequence(conn, [{"subject": "A", "body": "one"}, {"delay": 1, "body": "two", "thread": 1}])
    engine.enroll(conn, sid, [cid])
    run(conn, mailer)
    engine.mark_replied(conn, cid)
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "replied"
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "replied"
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE replied_at IS NOT NULL").fetchone()[0] == 1
    assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Respond to reply"
    assert run(conn, mailer, utcnow() + timedelta(days=5))["sent"] == 0


def test_suppressed_contacts_are_never_enrolled_or_emailed(conn, mailer):
    a = make_contact(conn, email="a@x.test")
    b = make_contact(conn, email="b@x.test")
    suppress(conn, "A@X.test", "manual")
    sid = make_sequence(conn, [{"subject": "S", "body": "B"}])
    res = engine.enroll(conn, sid, [a, b, b])
    assert res["enrolled"] == 1 and res["skipped_suppressed"] == 1 and res["skipped_existing"] == 1

    # suppressed after enrollment => stopped at send time
    suppress(conn, "b@x.test", "complaint")
    stats = run(conn, mailer)
    assert stats["sent"] == 0 and stats["stopped"] == 1


def test_send_window_and_daily_limit(conn, mailer):
    ids = [make_contact(conn, email=f"p{i}@x.test") for i in range(5)]
    sid = make_sequence(conn, [{"subject": "S", "body": "B"}])
    midnight = utcnow().replace(hour=0, minute=0, second=0)
    engine.enroll(conn, sid, ids, now=midnight)

    set_setting(conn, "send_window_start", "9")
    set_setting(conn, "send_window_end", "17")
    set_setting(conn, "send_timezone", "UTC")
    conn.commit()
    night = midnight.replace(hour=3)
    assert run(conn, mailer, night)["deferred"] == 5

    set_setting(conn, "daily_send_limit", "3")
    conn.commit()
    noon = midnight.replace(hour=12)
    stats = run(conn, mailer, noon)
    assert stats["sent"] == 3 and stats["deferred"] == 2


def test_paused_or_draft_sequence_does_not_send(conn, mailer):
    cid = make_contact(conn)
    sid = make_sequence(conn, [{"subject": "S", "body": "B"}], status="draft")
    engine.enroll(conn, sid, [cid])
    assert run(conn, mailer)["sent"] == 0
    conn.execute("UPDATE sequences SET status = 'active'")
    conn.commit()
    assert run(conn, mailer)["sent"] == 1


def test_enroll_requires_steps(conn):
    sid = make_sequence(conn, [])
    with pytest.raises(ValueError):
        engine.enroll(conn, sid, [1])


def test_send_window_respects_timezone():
    s = {"send_window_start": "9", "send_window_end": "17", "send_timezone": "America/New_York",
         "send_weekdays_only": "0"}
    t = utcnow().replace(month=1, day=15, hour=15)  # 10:00 in New York (EST)
    assert engine.in_send_window(s, t)
    assert not engine.in_send_window(s, t.replace(hour=23))  # 18:00 NY
