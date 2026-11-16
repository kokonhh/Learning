"""Starter content: a template library, a proven multi-channel cadence, and optional demo data."""
from __future__ import annotations

from datetime import timedelta

from . import importer
from .db import log_activity, ts, utcnow

TEMPLATES = [
    {
        "name": "Cold intro — problem-led",
        "category": "Cold outbound",
        "subject": "[[quick question|idea for {{company}}|{{company}} + {{sender_company}}]]",
        "body": (
            "[[Hi|Hey|Hello]] {{first_name|there}},\n\n"
            "Teams like {{company|yours}} often lose hours every week to {{pain_point|manual follow-up work}}.\n\n"
            "We help teams fix that — {{proof_point|customers typically get those hours back within the first month}}.\n\n"
            "Worth a 15-minute chat to see if it applies to {{company|you}}?"
        ),
    },
    {
        "name": "Follow-up — gentle bump",
        "category": "Cold outbound",
        "subject": "Re: quick question",
        "body": (
            "{{first_name|Hi}} — floating this back to the top of your inbox.\n\n"
            "If {{pain_point|this}} isn't a priority right now, just let me know and I'll stop following up."
        ),
    },
    {
        "name": "Follow-up — case study",
        "category": "Cold outbound",
        "subject": "How a team like {{company|yours}} solved this",
        "body": (
            "Hi {{first_name|there}},\n\n"
            "Thought this might be useful: {{case_study|a team similar to yours cut their response time in half "
            "in six weeks}}.\n\n"
            "Happy to walk you through how they did it. Does a quick call later this week work?"
        ),
    },
    {
        "name": "Breakup email",
        "category": "Cold outbound",
        "subject": "Should I close your file?",
        "body": (
            "Hi {{first_name|there}},\n\n"
            "I haven't heard back, so I'll assume the timing isn't right and won't reach out again.\n\n"
            "If things change, just reply to this email — I'm happy to help whenever it makes sense."
        ),
    },
    {
        "name": "Referral ask",
        "category": "Cold outbound",
        "subject": "Right person at {{company|your team}}?",
        "body": (
            "Hi {{first_name|there}},\n\n"
            "Apologies if you're not the right person — who at {{company|your company}} owns "
            "{{pain_point|this area}}?\n\nA name is all I need. Thank you!"
        ),
    },
    {
        "name": "Inbound lead — fast response",
        "category": "Inbound",
        "subject": "Thanks for reaching out, {{first_name|there}}",
        "body": (
            "Hi {{first_name|there}},\n\n"
            "Thanks for your interest in {{sender_company}}! I'd love to learn more about what you're working on "
            "at {{company|your company}}.\n\n"
            "Do you have 20 minutes this week? Just reply with a couple of times that suit you."
        ),
    },
    {
        "name": "Post-meeting recap",
        "category": "Sales",
        "subject": "Recap + next steps — {{company|our call}}",
        "body": (
            "Hi {{first_name|there}},\n\n"
            "Thanks for the time today. Quick recap:\n\n"
            "- What you're solving: {{pain_point|...}}\n"
            "- What success looks like: ...\n"
            "- Next step: ...\n\n"
            "I'll follow up on {{next_step_date|Thursday}}. Shout if I missed anything."
        ),
    },
]

CADENCE = [
    # (type, delay_days, template name or None, thread_reply, instructions)
    ("email", 0, "Cold intro — problem-led", 0, None),
    ("linkedin", 1, None, 0, "View their profile, then send a connection request (no pitch)."),
    ("email", 2, "Follow-up — gentle bump", 1, None),
    ("call", 2, None, 0, "Call. If no answer, leave a 20-second voicemail referencing your email."),
    ("email", 3, "Follow-up — case study", 0, None),
    ("email", 5, "Breakup email", 1, None),
]

DEMO_CSV = """email,first_name,last_name,title,company,domain,industry,employees,tags,pain_point
maya.chen@example.com,Maya,Chen,VP of Sales,Northwind Logistics,northwind.example.com,Logistics,350,"q4,saas",reps spending hours on manual follow-ups
liam.ortiz@example.com,Liam,Ortiz,Head of Growth,Brightleaf Health,brightleaf.example.com,Healthcare,120,q4,slow lead response times
priya.nair@example.com,Priya,Nair,CEO,Copperline Studio,copperline.example.com,Agency,18,"q4,agency",inconsistent pipeline
sam.okafor@example.com,Sam,Okafor,Sales Manager,Harbor Fintech,harbor.example.com,Fintech,800,q4,
ella.berg@example.com,Ella,Berg,Director of Operations,Fjord Retail,fjord.example.com,Retail,2200,q4,disconnected tools
noah.kim@example.com,Noah,Kim,Account Executive,Summit Cloud,summit.example.com,SaaS,95,saas,
"""


def seed_library(conn) -> dict:
    """Templates + the default cadence. Safe to run more than once."""
    created_templates = 0
    ids = {}
    for t in TEMPLATES:
        row = conn.execute("SELECT id FROM templates WHERE name = ?", (t["name"],)).fetchone()
        if row:
            ids[t["name"]] = row["id"]
            continue
        ids[t["name"]] = conn.execute(
            "INSERT INTO templates(name, subject, body, category) VALUES (?, ?, ?, ?)",
            (t["name"], t["subject"], t["body"], t["category"])).lastrowid
        created_templates += 1
    seq = conn.execute("SELECT id FROM sequences WHERE name = 'Cold outbound — 6 touch'").fetchone()
    if seq is None:
        sid = conn.execute(
            "INSERT INTO sequences(name, description, status) VALUES (?, ?, 'draft')",
            ("Cold outbound — 6 touch", "Email + LinkedIn + phone over ~2 weeks. Stops automatically on reply."),
        ).lastrowid
        for pos, (stype, delay, tpl, thread, instr) in enumerate(CADENCE, start=1):
            conn.execute(
                "INSERT INTO sequence_steps(sequence_id, position, step_type, delay_days, template_id, "
                "thread_reply, instructions) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, pos, stype, delay, ids.get(tpl) if tpl else None, thread, instr))
    else:
        sid = seq["id"]
    conn.commit()
    return {"templates_created": created_templates, "sequence_id": sid}


def seed(conn) -> dict:
    lib = seed_library(conn)
    res = importer.import_csv(conn, DEMO_CSV, source="demo data")
    ids = res["contact_ids"]
    now = utcnow()
    if not conn.execute("SELECT 1 FROM deals").fetchone() and len(ids) >= 3:
        for cid, name, stage, value, prob, days in [
            (ids[0], "Northwind — team rollout", "proposal", 24000, 60, 21),
            (ids[1], "Brightleaf — pilot", "meeting", 9000, 40, 35),
            (ids[2], "Copperline — starter plan", "won", 4800, 100, -3),
        ]:
            co = conn.execute("SELECT company_id FROM contacts WHERE id = ?", (cid,)).fetchone()["company_id"]
            did = conn.execute(
                "INSERT INTO deals(name, contact_id, company_id, stage, value, probability, close_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, cid, co, stage, value, prob, ts(now + timedelta(days=days))[:10])).lastrowid
            log_activity(conn, "deal_created", f"Deal created: {name} ({stage})", contact_id=cid, deal_id=did)
        conn.execute("UPDATE contacts SET status = 'customer' WHERE id = ?", (ids[2],))
        conn.execute("UPDATE contacts SET status = 'meeting' WHERE id = ?", (ids[1],))
    conn.commit()
    return {**lib, "contacts_created": res["created"], "contacts_updated": res["updated"]}
