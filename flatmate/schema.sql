-- FlatMate schema. SQLite is the authoritative store; Excel exports are read-only views.
-- Money is stored as integer minor units (paisa) to avoid floating-point errors.
-- Records are never overwritten: corrections add a new version and mark the old one superseded.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flats (
    flat_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,                -- "<flat_id>.<short_id>", e.g. flatA.sara
    flat_id   TEXT NOT NULL REFERENCES flats(flat_id),
    short_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    email     TEXT NOT NULL UNIQUE,            -- allowlisted address; identifies the flat for emails
    role      TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    active    INTEGER NOT NULL DEFAULT 1,
    joined_at TEXT NOT NULL,
    left_at   TEXT,
    UNIQUE (flat_id, short_id)
);

-- Source knowledge: every raw note or email the agent has seen. Ledger rows cite these.
CREATE TABLE IF NOT EXISTS messages (
    msg_id          TEXT PRIMARY KEY,          -- CLI: generated; email: Message-ID header
    flat_id         TEXT NOT NULL REFERENCES flats(flat_id),
    sender_id       TEXT NOT NULL REFERENCES members(member_id),
    channel         TEXT NOT NULL CHECK (channel IN ('cli', 'email', 'labelled_import')),
    subject         TEXT,
    text            TEXT NOT NULL,
    attachment_path TEXT,
    received_at     TEXT NOT NULL,
    business_date   TEXT NOT NULL,             -- date on the (possibly advanced) demo clock
    processed_at    TEXT,                      -- NULL until the agent has handled it
    outcome         TEXT                       -- short note of what the agent did with it
);
CREATE INDEX IF NOT EXISTS ix_messages_flat ON messages (flat_id, processed_at);

-- Long-term memory: expenses, one row per version.
CREATE TABLE IF NOT EXISTS expenses (
    row_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id     TEXT NOT NULL,              -- stable across versions, e.g. flatA-E003
    version        INTEGER NOT NULL,
    flat_id        TEXT NOT NULL REFERENCES flats(flat_id),
    payer_id       TEXT NOT NULL REFERENCES members(member_id),
    amount         INTEGER NOT NULL CHECK (amount > 0),
    category       TEXT NOT NULL,
    description    TEXT NOT NULL,
    expense_date   TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'superseded', 'rejected', 'void')),
    source_msg_id  TEXT REFERENCES messages(msg_id),
    created_by     TEXT NOT NULL,
    approved_by    TEXT,
    reason         TEXT,                       -- why it is pending, or why it was corrected
    idem_key       TEXT UNIQUE,                -- retry safety: the same request never creates two rows
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    UNIQUE (expense_id, version)
);
CREATE INDEX IF NOT EXISTS ix_expenses_flat ON expenses (flat_id, status);

CREATE TABLE IF NOT EXISTS expense_shares (
    row_id    INTEGER NOT NULL REFERENCES expenses(row_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    share     INTEGER NOT NULL,
    PRIMARY KEY (row_id, member_id)
);

-- Extra evidence linked to an expense, e.g. a bill email matched to an existing entry.
CREATE TABLE IF NOT EXISTS expense_evidence (
    expense_id TEXT NOT NULL,
    msg_id     TEXT NOT NULL REFERENCES messages(msg_id),
    kind       TEXT NOT NULL CHECK (kind IN ('source', 'bill', 'confirmation', 'correction')),
    added_at   TEXT NOT NULL,
    PRIMARY KEY (expense_id, msg_id)
);

-- Preferences that change future splits, e.g. Ali excluded from 'meat'.
CREATE TABLE IF NOT EXISTS preferences (
    pref_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    flat_id       TEXT NOT NULL REFERENCES flats(flat_id),
    member_id     TEXT NOT NULL REFERENCES members(member_id),
    key           TEXT NOT NULL,              -- 'exclude_category' or 'include_category'
    value         TEXT NOT NULL,              -- a category name
    valid_from    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'current', 'superseded', 'rejected')),
    supersedes_id INTEGER REFERENCES preferences(pref_id),
    note          TEXT,
    source_msg_id TEXT REFERENCES messages(msg_id),
    created_by    TEXT NOT NULL,
    approved_by   TEXT,
    idem_key      TEXT UNIQUE,
    created_at    TEXT NOT NULL
);

-- Payments between members. A claim only counts once the receiver confirms it.
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    flat_id        TEXT NOT NULL REFERENCES flats(flat_id),
    from_id        TEXT NOT NULL REFERENCES members(member_id),
    to_id          TEXT NOT NULL REFERENCES members(member_id),
    amount         INTEGER NOT NULL CHECK (amount > 0),
    status         TEXT NOT NULL CHECK (status IN ('claimed', 'confirmed', 'rejected')),
    claim_msg_id   TEXT REFERENCES messages(msg_id),
    confirmed_by   TEXT,
    idem_key       TEXT UNIQUE,
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);

-- Questions the agent is waiting on. Carried across cycles and restarts.
CREATE TABLE IF NOT EXISTS questions (
    question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    flat_id       TEXT NOT NULL REFERENCES flats(flat_id),
    cycle_id      TEXT,
    about_msg_id  TEXT REFERENCES messages(msg_id),
    asked_to      TEXT NOT NULL REFERENCES members(member_id),
    text          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('open', 'answered', 'cancelled')),
    answer_msg_id TEXT REFERENCES messages(msg_id),
    idem_key      TEXT UNIQUE,
    created_at    TEXT NOT NULL,
    answered_at   TEXT
);

-- Working state: one bounded checkpoint per flat so a restart resumes where it stopped.
CREATE TABLE IF NOT EXISTS working_state (
    flat_id        TEXT PRIMARY KEY REFERENCES flats(flat_id),
    cycle_id       TEXT,
    goal           TEXT,
    plan           TEXT,                      -- JSON list of planned steps
    last_email_uid INTEGER,
    checkpoint     TEXT,                      -- JSON: step reached, items still to handle
    updated_at     TEXT NOT NULL
);

-- One row per agent cycle, with a short episode summary used by later cycles.
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id      TEXT PRIMARY KEY,
    flat_id       TEXT NOT NULL REFERENCES flats(flat_id),
    business_date TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    summary       TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);

-- Every outgoing email goes through the outbox. msg_key makes sends idempotent.
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_key         TEXT NOT NULL UNIQUE,
    flat_id         TEXT NOT NULL REFERENCES flats(flat_id),
    cycle_id        TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('question', 'reminder', 'summary', 'notice')),
    recipient_id    TEXT NOT NULL REFERENCES members(member_id),
    recipient_email TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    debt_ref        TEXT,                     -- "<from>-><to>" for reminders, used for limits
    approval        TEXT NOT NULL CHECK (approval IN ('auto', 'approved', 'pending')),
    status          TEXT NOT NULL CHECK (status IN ('UNSENT_DRAFT', 'sent', 'failed', 'cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    business_date   TEXT NOT NULL,            -- demo-clock date, used for reminder spacing
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);

-- Audit trail: tool calls, memory reads/writes, LLM decisions, errors.
CREATE TABLE IF NOT EXISTS action_log (
    log_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    flat_id  TEXT,
    cycle_id TEXT,
    actor    TEXT NOT NULL,                   -- 'agent', 'user' or 'system'
    event    TEXT NOT NULL,                   -- 'tool_call', 'memory_read', 'memory_write', 'llm_decision', 'error'
    tool     TEXT,
    input    TEXT,
    output   TEXT,
    status   TEXT NOT NULL,
    error    TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_flat ON action_log (flat_id, ts);
