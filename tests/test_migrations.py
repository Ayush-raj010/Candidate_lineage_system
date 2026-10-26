"""Versioned schema upgrades preserve every existing row."""

import sqlite3

import pytest

from app import db
from app.db import LATEST_VERSION, SCHEMA_PATH, SchemaVersionError, connect, init_schema, schema_version
from app.importer import import_records

DATA_TABLES = ("candidates", "candidate_revisions", "skills", "candidate_skills", "candidate_tags", "shortlist", "ai_summaries")


def dump(conn, tables=DATA_TABLES):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables}


@pytest.fixture
def pre_auth_db(tmp_path, dataset):
    """A database exactly as the pre-authentication code left it: version 0, with recruiter data and a cached summary."""
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    import_records(conn, dataset)
    conn.execute("INSERT INTO candidate_tags VALUES ('cand_001', 'follow-up', '2026-10-01T00:00:00+00:00')")
    conn.execute("INSERT INTO shortlist VALUES ('cand_009', '2026-10-02T00:00:00+00:00')")
    content_hash = conn.execute("SELECT content_hash FROM candidates WHERE id = 'cand_009'").fetchone()[0]
    conn.execute(
        "INSERT INTO ai_summaries VALUES ('k1', 'cand_009', ?, '', 'v', 'm', '{\"summary\": \"x\"}', '2026-10-03T00:00:00+00:00')",
        (content_hash,),
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    yield path, conn
    conn.close()


def test_fresh_database_gets_the_latest_schema(tmp_path):
    conn = connect(tmp_path / "new.db")

    assert init_schema(conn) == [1, 2]
    assert schema_version(conn) == LATEST_VERSION == 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "sessions", "login_attempts", "audit_logs", *DATA_TABLES} <= tables
    assert list((tmp_path).glob("*.bak")) == []  # nothing to back up


def test_pre_auth_database_is_upgraded_in_place_without_losing_data(pre_auth_db, tmp_path):
    path, conn = pre_auth_db
    before = dump(conn)

    assert schema_version(conn) == 1
    assert init_schema(conn) == [2]

    assert schema_version(conn) == 2
    assert dump(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_upgrade_backs_up_the_database_first(pre_auth_db, tmp_path):
    path, conn = pre_auth_db
    before = dump(conn)

    init_schema(conn)

    [backup] = list(tmp_path.glob("old.db.v1-*.bak"))
    copy = sqlite3.connect(backup)
    copy.row_factory = sqlite3.Row
    assert dump(copy) == before and copy.execute("PRAGMA user_version").fetchone()[0] == 0
    copy.close()


def test_running_again_is_a_no_op(pre_auth_db, tmp_path):
    path, conn = pre_auth_db
    init_schema(conn)

    assert init_schema(conn) == []
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_import_and_recruiter_data_keep_working_after_upgrade(pre_auth_db, dataset):
    path, conn = pre_auth_db
    init_schema(conn)

    report = import_records(conn, dataset)
    dataset[0]["notes"] = "Changed after upgrade."
    changed = import_records(conn, dataset)

    assert len(report.unchanged) == 48 and changed.updated == {"cand_001": ["notes"]}
    assert conn.execute("SELECT tag FROM candidate_tags").fetchall()[0][0] == "follow-up"
    assert conn.execute("SELECT candidate_id FROM shortlist").fetchall()[0][0] == "cand_009"


def test_failed_migration_rolls_back_completely(pre_auth_db, tmp_path, monkeypatch):
    path, conn = pre_auth_db
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE half_done (id INTEGER);\nTHIS IS NOT SQL;\n", encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS", [(1, SCHEMA_PATH), (2, broken)])
    monkeypatch.setattr(db, "LATEST_VERSION", 2)

    with pytest.raises(sqlite3.OperationalError):
        init_schema(conn)

    assert schema_version(conn) == 1 and not conn.in_transaction
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None


def test_concurrent_startups_migrate_once(pre_auth_db, tmp_path):
    """Two server processes starting together (seen with uvicorn --reload): one migrates, the other waits and skips."""
    import threading

    path, conn = pre_auth_db
    before = dump(conn)
    barrier = threading.Barrier(4)
    results, errors = [], []

    def start():
        own = connect(path)
        try:
            barrier.wait()
            results.append(init_schema(own))
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [[], [], [], [2]]
    assert len(list(tmp_path.glob("old.db.v1-*.bak"))) == 1
    assert schema_version(conn) == 2 and dump(conn) == before


def test_trigger_bodies_survive_statement_splitting():
    statements = list(db._statements((db.MIGRATIONS_DIR / "002_auth.sql").read_text(encoding="utf-8")))

    triggers = [s for s in statements if "CREATE TRIGGER" in s]
    assert len(triggers) == 2 and all(s.rstrip().endswith("END;") for s in triggers)


def test_database_from_newer_code_is_refused(tmp_path):
    conn = connect(tmp_path / "future.db")
    init_schema(conn)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")

    with pytest.raises(SchemaVersionError):
        init_schema(conn)
