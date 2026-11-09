CREATE TABLE sessions (
    id               TEXT PRIMARY KEY,
    room_code        TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    created_by       TEXT,
    setting          TEXT NOT NULL DEFAULT '',
    role_definitions TEXT NOT NULL DEFAULT '[]',
    settings         TEXT NOT NULL DEFAULT '{}',
    event_seq        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    closed_at        TEXT
);

CREATE TABLE participants (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    display_name          TEXT NOT NULL,
    role                  TEXT NOT NULL,
    permissions           TEXT NOT NULL DEFAULT '[]',
    authenticated_subject TEXT NOT NULL UNIQUE,
    connection_state      TEXT NOT NULL DEFAULT 'offline',
    last_seen_sequence    INTEGER,
    created_at            TEXT NOT NULL
);

CREATE TABLE utterances (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    turn_id        TEXT NOT NULL,
    mode           TEXT NOT NULL,
    transcript     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (participant_id, turn_id)
);

CREATE TABLE messages (
    id                  TEXT PRIMARY KEY,
    utterance_id        TEXT,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    participant_id      TEXT,
    turn_id             TEXT,
    original_transcript TEXT,
    summary             TEXT NOT NULL,
    request_ids         TEXT NOT NULL DEFAULT '[]',
    timestamp           TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'team',
    shared_answer_id    TEXT,
    UNIQUE (participant_id, turn_id)
);

CREATE TABLE requests (
    id                   TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source_message_id    TEXT,
    requester_id         TEXT,
    assignee_id          TEXT,
    target_role          TEXT,
    details              TEXT NOT NULL,
    status               TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    instruction_revision INTEGER NOT NULL DEFAULT 1,
    accepted_revision    INTEGER,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    closed_at            TEXT
);

CREATE TABLE request_events (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    request_id        TEXT NOT NULL,
    actor_id          TEXT,
    source_message_id TEXT,
    origin            TEXT NOT NULL,
    type              TEXT NOT NULL,
    before            TEXT,
    after             TEXT,
    timestamp         TEXT NOT NULL,
    sequence          INTEGER NOT NULL
);

CREATE TABLE clarifications (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    participant_id     TEXT NOT NULL,
    related_request_id TEXT,
    question           TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    resolved_at        TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE notifications (
    id                   TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    request_id           TEXT,
    instruction_revision INTEGER,
    recipient_id         TEXT,
    message              TEXT NOT NULL,
    delivery_state       TEXT NOT NULL DEFAULT 'queued',
    created_at           TEXT NOT NULL
);

CREATE TABLE bot_exchanges (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    asker_id            TEXT NOT NULL,
    question_transcript TEXT NOT NULL,
    answer_text         TEXT NOT NULL,
    source_message_ids  TEXT NOT NULL DEFAULT '[]',
    state_sequence      INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    shared_message_id   TEXT
);

-- Ordered replay log. visibility is 'shared' or a participant id.
CREATE TABLE events (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence   INTEGER NOT NULL,
    timestamp  TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    visibility TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE TABLE idempotency (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    result     TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);

CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX idx_requests_session ON requests(session_id, status);
CREATE INDEX idx_events_session ON events(session_id, sequence);
CREATE INDEX idx_request_events ON request_events(request_id, sequence);
