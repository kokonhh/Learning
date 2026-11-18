# Outreach — a self-hosted outbound sales system

Find leads, run multi-channel sequences, track engagement, work your daily tasks, and move
deals through a pipeline, all in one small app you own. It's Python, Flask and SQLite, with no
SaaS fees and no per-seat pricing.

```
 Import leads ─► Score ─► Enroll in sequence ─► Email / LinkedIn / Call steps ─► Reply? ─► Meeting ─► Deal ─► Won
      ▲            │              │                      │                         │
  CSV / API     fit +         send window,        opens, clicks,           auto-stops sequence,
  webhook     engagement      daily limits        tasks for reps           creates follow-up task
```

## Features

| Area | What you get |
|---|---|
| **Contacts & companies** | CSV import with smart column matching (Apollo, Sales Nav, Clay and HubSpot exports all work). Duplicates are merged by email, and any extra column becomes a custom field. Includes tags, owners, search and filters, bulk actions, and CSV export. |
| **Lead scoring** | A 0–100 score built from fit (seniority, company size, data completeness) plus engagement (opens, clicks, replies, meetings). Updates live. |
| **Templates** | Merge fields with fallbacks (`{{first_name\|there}}`), any custom CSV column (`{{pain_point}}`), and spintax (`[[Hi\|Hey]]`). Live preview against a real contact. Starts with 7 proven templates. |
| **Sequences** | Multi-step, multi-channel cadences mixing email, LinkedIn, phone and manual tasks, with per-step delays. Follow-ups go out as threaded replies (`Re:` plus `In-Reply-To`). You can clone, pause, reorder and archive them. Per-step open and reply stats. |
| **Safety rails** | Send window by timezone, weekdays only, daily send cap, and throttling. Missing merge fields **pause** a contact instead of sending "Hi ,". A suppression list is checked before every send. |
| **Stop rules** | A reply, meeting, unsubscribe or bounce automatically stops that contact's sequences. Auto-replies (out of office) don't. |
| **Tracking** | Open pixel and click tracking. Redirects are signed with HMAC, so the click endpoint can't be abused as an open redirect. Both can be turned off. |
| **Compliance** | Every email gets an unsubscribe link and your physical address. RFC 8058 one-click `List-Unsubscribe` headers meet the Gmail and Yahoo bulk-sender rules. |
| **Today's tasks** | Call and LinkedIn steps become a task queue with the phone number, LinkedIn link and instructions. You log an outcome (voicemail, meeting booked…), and completing a task advances the sequence. |
| **Inbox sync** | IMAP polling detects replies, bounces (DSN parsing) and auto-replies, and logs incoming emails on the contact. |
| **Pipeline** | Drag-and-drop kanban with configurable stages, deal value, probability, weighted forecast and win rate. Winning a deal marks the contact as a customer. |
| **Dashboard** | Sent, open, click and reply rates, 14-day send chart, lead funnel, hottest leads, recent buying signals, and pipeline by stage. |
| **API** | `POST /api/v1/leads` (website forms, Zapier, Clay → auto-enroll) and `POST /api/v1/events` (reply, meeting, bounce and unsubscribe from other tools such as Calendly). |

## Quick start

**Inside Claude (no install):** the same system runs as a Claude page at
https://claude.ai/artifact/7ybxVZNDvAN7C4oB3M4JYs (source: `artifact/outreach.html`). It saves your data with the
page and sends through your connected Gmail.

**No install (Windows):** download **Outreach.exe** from this repo's
[Releases page](../../releases/latest) and double-click it. Python is bundled inside. Windows may warn
"Windows protected your PC": click *More info → Run anyway*. Your data is kept in `%APPDATA%\Outreach`.
To send real email, put a `.env` file (see `.env.example`) in that folder.

**Easiest:** download the code, then double-click **`Start Outreach (Windows).bat`** (Windows) or
**`start-mac.command`** (Mac). The first run installs everything. After that, your browser opens at
http://localhost:5000. Keep the black window open while you use the app. Python must be installed
(python.org; on Windows tick "Add python.exe to PATH").

**Manual:**

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

flask --app outreach seed          # optional: demo contacts + deals (templates & a cadence are always added)
flask --app outreach run           # UI at http://localhost:5000
flask --app outreach worker        # in a 2nd terminal: sends due steps every 60s
```

By default the mailer runs in **dry-run** mode. Every email is written to `instance/outbox/*.eml`
so you can check exactly what would go out. Nothing is sent until you configure SMTP.

### Your first campaign (about 10 minutes)

1. **Settings:** set your name, from-address, company, physical address, send window and timezone,
   and the public base URL that tracking and unsubscribe links point to.
2. **Templates:** edit the starter copy. Keep first touches under ~120 words and personal.
3. **Contacts → Import CSV:** upload your list and tag it (for example `q1-vp-sales`).
4. **Sequences → "Cold outbound — 6 touch":** review the steps, then click **Activate**.
5. **Enroll** by tag, from Contacts with *Bulk action → Enroll*, or while importing.
6. The worker sends email steps on schedule. Manual steps appear in **Today's tasks** each morning.
7. Replies stop the sequence and create a "Respond to reply" task. Click **Meeting booked**, then **+ Deal**.

## Going live

Copy `.env.example` to `.env` and set:

```bash
OUTREACH_MAIL_BACKEND=smtp
SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USER=you@yourdomain.com  SMTP_PASSWORD=<app password>
IMAP_HOST=imap.gmail.com  IMAP_USER=you@yourdomain.com  IMAP_PASSWORD=<app password>
OUTREACH_PASSWORD=<protects the UI>     OUTREACH_SECRET_KEY=<long random string>
OUTREACH_API_KEY=<for webhooks>
```

Any SMTP provider works: Google Workspace, Microsoft 365, SendGrid, Mailgun, Postmark, Amazon SES.

**Docker** runs the web app and the worker against a shared volume:

```bash
cp .env.example .env   # edit it
docker compose up -d   # UI on :8000
```

If you'd rather use cron than the long-running worker, run this every few minutes:
`flask --app outreach tick`.

### Deliverability checklist

- Send from a **secondary domain** (for example `getacme.com`), not your main one, and set up SPF, DKIM and DMARC on it.
- Warm new mailboxes for 2–4 weeks. Start the daily limit around 20 and ramp up slowly; stay under ~50/day per mailbox.
- Plain-text style, one link at most, no attachments in the first touch. Open tracking adds an image, so turn it
  off if deliverability matters more to you than open rates (replies are the metric that counts).
- Verify lists before importing so the bounce rate stays under 2%. Bounces are suppressed automatically.
- Follow the law where you sell: CAN-SPAM (US), CASL (Canada), GDPR/PECR (EU/UK). The app adds the unsubscribe link
  and physical address, but you're responsible for having a lawful basis to email each contact.

## API

```bash
# Create or update leads and enroll them in sequence 1 (a single object or a list)
curl -X POST localhost:5000/api/v1/leads -H "X-API-Key: $OUTREACH_API_KEY" -H "Content-Type: application/json" \
  -d '{"email":"jane@acme.com","first_name":"Jane","company":"Acme","pain_point":"slow follow-ups","sequence_id":1}'

# Report an event from another system: reply | meeting | bounce | unsubscribe | complaint
curl -X POST localhost:5000/api/v1/events -H "X-API-Key: $OUTREACH_API_KEY" -H "Content-Type: application/json" \
  -d '{"email":"jane@acme.com","type":"meeting","detail":"Booked via Calendly"}'

curl localhost:5000/api/v1/contacts/1 -H "X-API-Key: $OUTREACH_API_KEY"
```

## CLI

| Command | Purpose |
|---|---|
| `flask --app outreach run` | Web UI (development server; use `gunicorn wsgi:app` in production) |
| `flask --app outreach worker [--interval 60]` | Long-running sending engine + inbox checker |
| `flask --app outreach tick` | Run once (for cron) |
| `flask --app outreach seed` | Demo data |
| `flask --app outreach rescore` | Recompute every lead score |

## Project layout

```
outreach/
  schema.sql      data model (contacts, companies, sequences, steps, enrollments, messages, tasks, deals, activities)
  engine.py       enrollment, scheduler, stop rules, sending, task → next-step advancement
  templating.py   merge fields, fallbacks, spintax, text→HTML
  mailer.py       dry-run and SMTP transports, RFC 8058 headers, threading headers
  tracking.py     open pixel, signed click redirects, unsubscribe URLs
  inbox.py        IMAP reply / bounce / auto-reply detection
  importer.py     CSV import + header mapping + dedupe
  scoring.py      lead scoring
  analytics.py    dashboard + sequence reports
  seed.py         starter templates, default cadence, demo data
  views/          web UI routes, public tracking endpoints, JSON API
tests/            pytest suite (engine, import, templating, tracking, API, inbox parsing, UI flows)
```

Run the tests with `pytest`.
