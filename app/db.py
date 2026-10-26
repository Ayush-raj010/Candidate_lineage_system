"""SQLite connection and versioned schema migrations."""

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "candidates.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# (version, script). Version 1 is the original schema; databases created before
# versioning existed (user_version 0 with the candidates tables) count as version 1.
MIGRATIONS = [
    (1, SCHEMA_PATH),
    (2, MIGRATIONS_DIR / "002_auth.sql"),
]
LATEST_VERSION = MIGRATIONS[-1][0]


class LegacySchemaError(RuntimeError):
    """The database was created by the original init_db.py schema."""


class SchemaVersionError(RuntimeError):
    """The database was created by a newer version of this code."""


BUSY_TIMEOUT_MS = 5000


def connect(db_path=DEFAULT_DB_PATH):
    """Open a connection with foreign keys enforced.

    The connection is in autocommit mode; callers that need atomicity
    issue BEGIN/COMMIT themselves (the importer relies on this to roll
    back a dry run).

    File databases use WAL so API reads are not blocked by a write, and
    a busy timeout so concurrent writers wait instead of failing at once.

    check_same_thread is off because FastAPI may open the per-request
    connection and run the route in different worker threads; each
    connection is still used by only one request at a time.
    """
    in_memory = str(db_path) == ":memory:"
    if not in_memory:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn):
    """Group statements on an autocommit connection: commit together or not at all."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
        if columns and "content_hash" not in columns:
            raise LegacySchemaError(
                "This database uses the old schema (no candidate revisions). "
                "Move or delete it, then run the import again to rebuild it from candidates.json."
            )
        if columns:
            return 1  # created by the original schema.sql, before versioning
    return version


def _backup(conn, version):
    """Copy a file database aside before migrating it. Returns the backup path, or None."""
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not path:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # A random suffix: two processes upgrading in the same second must not share (and delete) one file.
    backup_path = Path(path).with_name(f"{Path(path).name}.v{version}-{stamp}-{secrets.token_hex(3)}.bak")
    target = sqlite3.connect(backup_path)
    try:
        conn.backup(target)
    finally:
        target.close()
    return backup_path


def _statements(sql):
    """Split a script into statements; sqlite3.complete_statement keeps trigger bodies whole."""
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                yield statement
            statement = ""
    if statement.strip():
        yield statement  # an incomplete trailing statement: let SQLite report the error


def init_schema(conn):
    """Bring the database up to LATEST_VERSION. Safe to run repeatedly, and from several processes at once.

    All pending migrations run inside one write-locked transaction (BEGIN IMMEDIATE) together with
    the version bump, so a failure leaves the database exactly as it was. The version is re-read
    after taking the lock: if another process (e.g. a second server worker) migrated first, this
    one does nothing. An existing database with data is backed up next to itself before upgrading.
    The backup is taken before the lock (SQLite can't back up through a connection that holds an
    open write transaction); a process that then finds nothing left to do deletes its own copy.
    Returns the list of versions applied.
    """
    current = schema_version(conn)
    if current > LATEST_VERSION:
        raise SchemaVersionError(
            f"The database is at schema version {current}, but this code only knows up to {LATEST_VERSION}."
        )
    if current == LATEST_VERSION:
        return []
    backup_path = _backup(conn, current) if current >= 1 else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = schema_version(conn)  # re-check under the write lock
        pending = [(version, script) for version, script in MIGRATIONS if version > current]
        for version, script in pending:
            for statement in _statements(script.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise  # the backup is kept
    conn.execute("COMMIT")
    if backup_path and not pending:
        backup_path.unlink(missing_ok=True)  # another process migrated first and kept its own backup
    return [version for version, _ in pending]
