-- Migration 2: accounts, sessions, login throttling and the audit log.
-- Adds tables only; no existing table or row is changed.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE CHECK (email = lower(trim(email)) AND length(email) BETWEEN 3 AND 254),
    password_hash TEXT NOT NULL CHECK (password_hash LIKE '$argon2%'),   -- never a plaintext password
    role          TEXT NOT NULL CHECK (role IN ('admin', 'hiring_manager', 'recruiter')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- The cookie holds a random token; only its SHA-256 is stored, so a copy of the
-- database can't be used to hijack sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,   -- for the idle timeout
    expires_at   TEXT NOT NULL,   -- absolute limit
    ip           TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY,
    email        TEXT NOT NULL,
    ip           TEXT,
    succeeded    INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_attempts_email ON login_attempts (email, attempted_at);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ip ON login_attempts (ip, attempted_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users (id),
    actor_email   TEXT,             -- copied, so the entry stays readable if the email changes
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    details       TEXT CHECK (details IS NULL OR json_valid(details)),
    ip            TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_logs (target_type, target_id);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_logs (action);

-- The audit log is append-only.
CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
