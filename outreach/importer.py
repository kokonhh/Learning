"""CSV import of contacts with flexible header mapping and de-duplication."""
from __future__ import annotations

import csv
import io
import json
import re

from . import scoring
from .db import is_suppressed, log_activity, ts

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ALIASES = {
    "email": ("email", "email address", "e-mail", "work email", "emailaddress"),
    "first_name": ("first name", "first_name", "firstname", "given name", "first"),
    "last_name": ("last name", "last_name", "lastname", "surname", "family name", "last"),
    "full_name": ("name", "full name", "full_name", "contact name"),
    "title": ("title", "job title", "position", "role", "job_title"),
    "phone": ("phone", "phone number", "mobile", "telephone", "direct phone"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin_url", "linkedin profile", "person linkedin url"),
    "company": ("company", "company name", "organization", "organisation", "account", "account name"),
    "domain": ("domain", "company domain", "website", "company website"),
    "industry": ("industry", "vertical"),
    "size": ("size", "company size", "employees", "headcount", "# employees"),
    "tags": ("tags", "tag", "labels", "list"),
    "source": ("source", "lead source"),
    "timezone": ("timezone", "time zone", "tz"),
    "owner": ("owner", "rep", "assigned to"),
}


def _norm(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().lower().replace("_", " "))


def map_headers(headers: list[str]) -> dict[str, str]:
    """Return {csv_header: field_name or 'custom:<name>'}."""
    mapping = {}
    for h in headers:
        n = _norm(h)
        field = next((f for f, names in ALIASES.items() if n in {_norm(x) for x in names}), None)
        mapping[h] = field or f"custom:{h.strip()}"
    return mapping


def size_bucket(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    digits = re.findall(r"\d+", raw.replace(",", ""))
    if not digits:
        return raw
    n = int(digits[-1]) if "-" in raw else int(digits[0])
    for upper, label in ((10, "1-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"), (1000, "501-1000")):
        if n <= upper:
            return label
    return "1000+"


def normalise_tags(raw: str) -> str:
    tags = sorted({t.strip().lower() for t in re.split(r"[,;|]", raw or "") if t.strip()})
    return ",".join(tags)


def upsert_company(conn, name: str, domain: str = "", industry: str = "", size: str = "") -> int | None:
    name = (name or "").strip()
    if not name and domain:
        name = domain.split(".")[0].title()
    if not name:
        return None
    row = conn.execute("SELECT * FROM companies WHERE lower(name) = lower(?)", (name,)).fetchone()
    domain = re.sub(r"^https?://(www\.)?", "", (domain or "").strip()).strip("/")
    if row:
        conn.execute(
            "UPDATE companies SET domain = COALESCE(NULLIF(domain,''), ?), "
            "industry = COALESCE(NULLIF(industry,''), ?), size = COALESCE(NULLIF(size,''), ?) WHERE id = ?",
            (domain or None, industry or None, size or None, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO companies(name, domain, industry, size) VALUES (?, ?, ?, ?)",
        (name, domain or None, industry or None, size or None),
    )
    return cur.lastrowid


def import_csv(conn, data: str | bytes, *, extra_tags: str = "", source: str = "csv import",
               update_existing: bool = True) -> dict:
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(data))
    if not reader.fieldnames:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": ["File has no header row"], "contact_ids": []}
    mapping = map_headers(reader.fieldnames)
    if "email" not in mapping.values():
        return {"created": 0, "updated": 0, "skipped": 0,
                "errors": ["No email column found (expected a header like 'Email')"], "contact_ids": []}

    result = {"created": 0, "updated": 0, "skipped": 0, "suppressed": 0, "errors": [], "contact_ids": []}
    for lineno, raw in enumerate(reader, start=2):
        rec: dict = {}
        custom: dict = {}
        for header, value in raw.items():
            if header is None:
                continue
            value = (value or "").strip()
            field = mapping[header]
            if field.startswith("custom:"):
                if value:
                    custom[field[7:]] = value
            else:
                rec[field] = value
        email = rec.get("email", "").lower()
        if not EMAIL_RE.match(email):
            result["skipped"] += 1
            result["errors"].append(f"Row {lineno}: invalid or missing email '{email}'")
            continue
        if not rec.get("first_name") and rec.get("full_name"):
            parts = rec["full_name"].split()
            rec["first_name"] = parts[0]
            rec["last_name"] = rec.get("last_name") or " ".join(parts[1:])
        company_id = upsert_company(conn, rec.get("company", ""), rec.get("domain", ""),
                                    rec.get("industry", ""), size_bucket(rec.get("size", "")))
        tags = normalise_tags(",".join(filter(None, [rec.get("tags", ""), extra_tags])))
        existing = conn.execute("SELECT * FROM contacts WHERE lower(email) = ?", (email,)).fetchone()
        if existing:
            if not update_existing:
                result["skipped"] += 1
                continue
            merged_custom = json.loads(existing["custom_fields"] or "{}")
            merged_custom.update(custom)
            merged_tags = normalise_tags(",".join(filter(None, [existing["tags"], tags])))
            conn.execute(
                "UPDATE contacts SET first_name = COALESCE(NULLIF(?, ''), first_name), "
                "last_name = COALESCE(NULLIF(?, ''), last_name), title = COALESCE(NULLIF(?, ''), title), "
                "phone = COALESCE(NULLIF(?, ''), phone), linkedin_url = COALESCE(NULLIF(?, ''), linkedin_url), "
                "company_id = COALESCE(?, company_id), timezone = COALESCE(NULLIF(?, ''), timezone), "
                "owner = COALESCE(NULLIF(?, ''), owner), tags = ?, custom_fields = ?, updated_at = ? WHERE id = ?",
                (rec.get("first_name", ""), rec.get("last_name", ""), rec.get("title", ""), rec.get("phone", ""),
                 rec.get("linkedin_url", ""), company_id, rec.get("timezone", ""), rec.get("owner", ""),
                 merged_tags, json.dumps(merged_custom), ts(), existing["id"]),
            )
            cid = existing["id"]
            result["updated"] += 1
        else:
            cur = conn.execute(
                "INSERT INTO contacts(email, first_name, last_name, title, phone, linkedin_url, company_id, "
                "source, tags, timezone, owner, custom_fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email, rec.get("first_name") or None, rec.get("last_name") or None, rec.get("title") or None,
                 rec.get("phone") or None, rec.get("linkedin_url") or None, company_id,
                 rec.get("source") or source, tags, rec.get("timezone") or None, rec.get("owner") or None,
                 json.dumps(custom)),
            )
            cid = cur.lastrowid
            log_activity(conn, "created", f"Imported from {source}", contact_id=cid)
            result["created"] += 1
        if is_suppressed(conn, email):
            result["suppressed"] += 1
        scoring.compute(conn, cid)
        result["contact_ids"].append(cid)
    conn.commit()
    return result
