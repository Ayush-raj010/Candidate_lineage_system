from app.db import BUSY_TIMEOUT_MS, connect
from app.importer import import_records


def pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_file_connection_settings(db_path):
    conn = connect(db_path)

    assert pragma(conn, "journal_mode") == "wal"
    assert pragma(conn, "busy_timeout") == BUSY_TIMEOUT_MS
    assert pragma(conn, "foreign_keys") == 1
    conn.close()


def test_in_memory_connection_skips_wal():
    conn = connect(":memory:")

    assert pragma(conn, "journal_mode") == "memory"
    assert pragma(conn, "foreign_keys") == 1
    conn.close()


def test_reader_is_not_blocked_by_open_write_transaction(conn, db_path, dataset):
    import_records(conn, dataset)
    writer = connect(db_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("DELETE FROM candidate_skills WHERE candidate_id = 'cand_001'")

    reader = connect(db_path)
    assert reader.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 48

    writer.execute("ROLLBACK")
    writer.close()
    reader.close()
