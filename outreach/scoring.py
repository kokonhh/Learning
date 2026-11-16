"""Lead scoring: fit (who they are) + engagement (what they did). 0–100."""
from __future__ import annotations

SENIORITY = [
    (("founder", "owner", "ceo", "cto", "cfo", "coo", "cmo", "cro", "chief", "president"), 30),
    (("vp", "vice president", "head of", "director"), 22),
    (("manager", "lead", "principal"), 14),
    (("senior", "sr."), 8),
]

SIZE_POINTS = {"1-10": 4, "11-50": 8, "51-200": 12, "201-500": 14, "501-1000": 12, "1000+": 10}


def fit_score(contact: dict, company: dict | None) -> int:
    score = 0
    title = (contact.get("title") or "").lower()
    for keywords, pts in SENIORITY:
        if any(k in title for k in keywords):
            score += pts
            break
    if company:
        score += SIZE_POINTS.get((company.get("size") or "").strip(), 0)
    for field in ("phone", "linkedin_url", "title"):
        if contact.get(field):
            score += 2
    return min(score, 50)


def engagement_score(stats: dict) -> int:
    score = 0
    score += min(stats.get("opens", 0), 5) * 2          # up to 10
    score += min(stats.get("clicks", 0), 3) * 5         # up to 15
    score += 20 if stats.get("replies", 0) else 0
    score += 10 if stats.get("meetings", 0) else 0
    return min(score, 50)


def compute(conn, contact_id: int) -> int:
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if contact is None:
        return 0
    contact = dict(contact)
    company = None
    if contact.get("company_id"):
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (contact["company_id"],)).fetchone()
        company = dict(row) if row else None
    m = conn.execute(
        "SELECT COALESCE(SUM(open_count),0) opens, COALESCE(SUM(click_count),0) clicks, "
        "COUNT(replied_at) replies FROM messages WHERE contact_id = ? AND direction = 'out'",
        (contact_id,),
    ).fetchone()
    meetings = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE contact_id = ? AND kind = 'meeting'", (contact_id,)
    ).fetchone()[0]
    stats = {"opens": m["opens"], "clicks": m["clicks"], "replies": m["replies"], "meetings": meetings}
    if contact["status"] in ("unsubscribed", "bounced", "lost"):
        total = 0
    else:
        total = fit_score(contact, company) + engagement_score(stats)
    conn.execute("UPDATE contacts SET score = ? WHERE id = ?", (total, contact_id))
    return total


def recompute_all(conn) -> int:
    ids = [r[0] for r in conn.execute("SELECT id FROM contacts")]
    for cid in ids:
        compute(conn, cid)
    conn.commit()
    return len(ids)
