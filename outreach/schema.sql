PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    domain      TEXT,
    industry    TEXT,
    size        TEXT,
    website     TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_name ON companies(lower(name));

CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL,
    first_name     TEXT,
    last_name      TEXT,
    title          TEXT,
    phone          TEXT,
    linkedin_url   TEXT,
    company_id     INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'new',   -- new, contacted, engaged, replied, meeting, qualified, customer, lost, unsubscribed, bounced
    source         TEXT,
    tags           TEXT NOT NULL DEFAULT '',      -- comma separated, normalised lower-case
    timezone       TEXT,
    custom_fields  TEXT NOT NULL DEFAULT '{}',    -- JSON object
    score          INTEGER NOT NULL DEFAULT 0,
    owner          TEXT,
    last_contacted_at TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_email ON contacts(lower(email));
CREATE INDEX IF NOT EXISTS ix_contacts_status ON contacts(status);

CREATE TABLE IF NOT EXISTS suppressions (
    email      TEXT PRIMARY KEY COLLATE NOCASE,
    reason     TEXT NOT NULL,                    -- unsubscribed, bounced, manual, complaint
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    category    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sequences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'draft',   -- draft, active, paused, archived
    stop_on_reply INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sequence_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id   INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    step_type     TEXT NOT NULL,                 -- email, call, linkedin, task
    delay_days    INTEGER NOT NULL DEFAULT 0,    -- wait after previous step (or enrollment for first)
    template_id   INTEGER REFERENCES templates(id) ON DELETE SET NULL,
    subject       TEXT,                          -- override / inline content
    body          TEXT,
    thread_reply  INTEGER NOT NULL DEFAULT 1,    -- send as "Re:" in same thread
    instructions  TEXT                           -- for manual steps
);
CREATE INDEX IF NOT EXISTS ix_steps_seq ON sequence_steps(sequence_id, position);

CREATE TABLE IF NOT EXISTS enrollments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id   INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    contact_id    INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    current_step  INTEGER NOT NULL DEFAULT 1,    -- position of next step to execute
    status        TEXT NOT NULL DEFAULT 'active',-- active, paused, completed, replied, unsubscribed, bounced, stopped
    next_run_at   TEXT,
    thread_subject TEXT,
    thread_message_id TEXT,
    enrolled_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    UNIQUE(sequence_id, contact_id)
);
CREATE INDEX IF NOT EXISTS ix_enroll_due ON enrollments(status, next_run_at);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id    INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    step_id       INTEGER REFERENCES sequence_steps(id) ON DELETE SET NULL,
    direction     TEXT NOT NULL DEFAULT 'out',   -- out, in
    subject       TEXT,
    body_html     TEXT,
    body_text     TEXT,
    message_id    TEXT,
    token         TEXT UNIQUE,                   -- tracking token
    status        TEXT NOT NULL DEFAULT 'sent',  -- sent, failed, received
    error         TEXT,
    opened_at     TEXT,
    open_count    INTEGER NOT NULL DEFAULT 0,
    clicked_at    TEXT,
    click_count   INTEGER NOT NULL DEFAULT 0,
    replied_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_messages_contact ON messages(contact_id);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id    INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    deal_id       INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    step_id       INTEGER REFERENCES sequence_steps(id) ON DELETE SET NULL,
    task_type     TEXT NOT NULL DEFAULT 'todo',  -- call, linkedin, todo, follow_up
    title         TEXT NOT NULL,
    notes         TEXT,
    due_at        TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'open',  -- open, done, skipped
    outcome       TEXT,
    completed_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_tasks_open ON tasks(status, due_at);

CREATE TABLE IF NOT EXISTS deals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    contact_id    INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    company_id    INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    stage         TEXT NOT NULL DEFAULT 'lead',
    value         REAL NOT NULL DEFAULT 0,
    probability   INTEGER,
    close_date    TEXT,
    notes         TEXT,
    lost_reason   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_deals_stage ON deals(stage);

CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    deal_id     INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,     -- email_sent, email_opened, link_clicked, replied, unsubscribed, bounced, note, call, linkedin, stage_change, enrolled, task_done, meeting
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_activities_contact ON activities(contact_id, created_at);
