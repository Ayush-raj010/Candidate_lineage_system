import json
import sqlite3

import pytest

from app.db import LegacySchemaError, connect, init_schema
from app.importer import import_records, main


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def tag_and_shortlist(conn, candidate_id):
    conn.execute(
        "INSERT INTO candidate_tags (candidate_id, tag, created_at) VALUES (?, 'strong-backend', 'now')",
        (candidate_id,),
    )
    conn.execute("INSERT INTO shortlist (candidate_id, shortlisted_at) VALUES (?, 'now')", (candidate_id,))


# --- First import -----------------------------------------------------------

def test_first_import_inserts_every_record(conn, dataset):
    report = import_records(conn, dataset)

    assert len(report.inserted) == 48
    assert not (report.updated or report.unchanged or report.invalid or report.missing_from_file)
    assert count(conn, "candidates") == 48
    assert count(conn, "candidate_revisions") == 48
    assert count(conn, "skills") == 30
    assert count(conn, "candidate_skills") == sum(len(r["skills"]) for r in dataset)


def test_every_field_matches_the_source_json(conn, dataset):
    import_records(conn, dataset)

    for record in dataset:
        row = conn.execute("SELECT * FROM candidates WHERE id = ?", (record["id"],)).fetchone()
        for key in ("name", "target_role", "source", "notes", "applied_date"):
            assert row[key] == record[key], (record["id"], key)
        assert row["years_experience"] == record["years_experience"]
        assert type(row["years_experience"]) is int

        skills = [r["name"] for r in conn.execute(
            "SELECT s.name FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id WHERE cs.candidate_id = ?",
            (record["id"],),
        )]
        assert sorted(skills) == sorted(record["skills"])


def test_revision_stores_the_original_record_verbatim(conn, dataset):
    import_records(conn, dataset)

    for record in dataset:
        stored = conn.execute(
            "SELECT r.source_json FROM candidates c JOIN candidate_revisions r "
            "ON r.candidate_id = c.id AND r.content_hash = c.content_hash WHERE c.id = ?",
            (record["id"],),
        ).fetchone()[0]
        assert json.loads(stored) == record


# --- Repeated imports -------------------------------------------------------

def test_reimporting_the_same_file_changes_nothing(conn, dataset):
    import_records(conn, dataset)
    before = conn.execute("SELECT id, updated_at, content_hash FROM candidates ORDER BY id").fetchall()

    report = import_records(conn, dataset)

    assert len(report.unchanged) == 48
    assert not (report.inserted or report.updated)
    assert conn.execute("SELECT id, updated_at, content_hash FROM candidates ORDER BY id").fetchall() == before
    assert count(conn, "candidate_revisions") == 48


def test_changed_record_is_updated_and_old_evidence_kept(conn, dataset):
    import_records(conn, dataset)
    original_notes = dataset[0]["notes"]
    dataset[0]["notes"] = "Updated after screening call."
    dataset[0]["skills"].append("Python")

    report = import_records(conn, dataset)

    assert report.updated == {"cand_001": ["notes", "skills"]}
    assert len(report.unchanged) == 47
    row = conn.execute("SELECT notes FROM candidates WHERE id = 'cand_001'").fetchone()
    assert row["notes"] == "Updated after screening call."
    revisions = [json.loads(r[0])["notes"] for r in conn.execute(
        "SELECT source_json FROM candidate_revisions WHERE candidate_id = 'cand_001'"
    )]
    assert sorted(revisions) == sorted([original_notes, "Updated after screening call."])
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_skills WHERE candidate_id = 'cand_001'"
    ).fetchone()[0] == len(dataset[0]["skills"])


def test_reimport_preserves_tags_and_shortlist(conn, dataset):
    import_records(conn, dataset)
    tag_and_shortlist(conn, "cand_001")
    dataset[0]["notes"] = "Changed notes."

    import_records(conn, dataset)
    import_records(conn, dataset)

    assert conn.execute("SELECT tag FROM candidate_tags WHERE candidate_id = 'cand_001'").fetchall()[0][0] == "strong-backend"
    assert count(conn, "shortlist") == 1


def test_reverting_to_an_earlier_version_reuses_its_revision(conn, dataset):
    import_records(conn, dataset)
    original = dataset[0]["notes"]
    dataset[0]["notes"] = "Temporary edit."
    import_records(conn, dataset)
    dataset[0]["notes"] = original

    report = import_records(conn, dataset)

    assert report.updated == {"cand_001": ["notes"]}
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_revisions WHERE candidate_id = 'cand_001'"
    ).fetchone()[0] == 2


def test_candidates_missing_from_file_are_kept(conn, dataset):
    import_records(conn, dataset)
    tag_and_shortlist(conn, "cand_048")

    report = import_records(conn, dataset[:-1])

    assert report.missing_from_file == ["cand_048"]
    assert count(conn, "candidates") == 48
    assert count(conn, "candidate_tags") == 1
    assert count(conn, "shortlist") == 1


# --- Dry run ----------------------------------------------------------------

def test_dry_run_reports_without_writing(conn, dataset):
    report = import_records(conn, dataset, dry_run=True)

    assert report.dry_run and len(report.inserted) == 48
    assert count(conn, "candidates") == 0
    assert count(conn, "candidate_revisions") == 0
    assert count(conn, "skills") == 0


def test_dry_run_reports_pending_updates(conn, dataset):
    import_records(conn, dataset)
    dataset[1]["source"] = "Referral"

    report = import_records(conn, dataset, dry_run=True)

    assert report.updated == {"cand_002": ["source"]}
    assert conn.execute("SELECT source FROM candidates WHERE id = 'cand_002'").fetchone()[0] == "Conference"


def test_cli_dry_run_does_not_create_the_database(db_path, dataset, write_json, capsys):
    exit_code = main(["--json", str(write_json(dataset)), "--db", str(db_path), "--dry-run"])

    assert exit_code == 0
    assert not db_path.exists()
    assert "inserted:          48" in capsys.readouterr().out


# --- Validation -------------------------------------------------------------

@pytest.mark.parametrize("change, reason", [
    ({"id": ""}, "'id'"),
    ({"name": None}, "'name'"),
    ({"name": "   "}, "'name'"),
    ({"target_role": 7}, "'target_role'"),
    ({"source": None}, "'source'"),
    ({"notes": None}, "'notes'"),
    ({"years_experience": -1}, "'years_experience'"),
    ({"years_experience": True}, "'years_experience'"),
    ({"years_experience": "5"}, "'years_experience'"),
    ({"years_experience": 2.5}, "'years_experience'"),
    ({"skills": "Python, Go"}, "'skills'"),
    ({"skills": ["Python", 3]}, "skill"),
    ({"skills": ["Python", " "]}, "skill"),
    ({"applied_date": "10/09/2026"}, "'applied_date'"),
    ({"applied_date": "2026-02-30"}, "'applied_date'"),
    ({"applied_date": "20260910"}, "'applied_date'"),
])
def test_invalid_record_is_skipped_with_reason(conn, dataset, change, reason):
    dataset[0].update(change)

    report = import_records(conn, dataset)

    assert len(report.invalid) == 1
    index, _, message = report.invalid[0]
    assert index == 0 and reason in message
    assert len(report.inserted) == 47
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE id = 'cand_001'").fetchone()[0] == 0


@pytest.mark.parametrize("missing_field", ["id", "name", "target_role", "source", "notes", "skills"])
def test_missing_required_field_is_invalid(conn, dataset, missing_field):
    del dataset[0][missing_field]

    report = import_records(conn, dataset)

    assert len(report.invalid) == 1 and missing_field in report.invalid[0][2]


def test_non_object_record_and_duplicate_id_are_invalid(conn, dataset):
    records = dataset + ["not a record", dict(dataset[0], name="Someone Else")]

    report = import_records(conn, records)

    assert [i for i, _, _ in report.invalid] == [48, 49]
    assert "duplicate id" in report.invalid[1][2]
    assert conn.execute("SELECT name FROM candidates WHERE id = 'cand_001'").fetchone()[0] == "Sameer Rao"


def test_invalid_existing_record_is_not_reported_missing(conn, dataset):
    import_records(conn, dataset)
    dataset[0]["years_experience"] = -3

    report = import_records(conn, dataset)

    assert report.missing_from_file == []
    assert report.invalid[0][1] == "cand_001"
    assert conn.execute("SELECT years_experience FROM candidates WHERE id = 'cand_001'").fetchone()[0] == 6


def test_optional_fields_stay_null_instead_of_defaults(conn, dataset):
    del dataset[0]["years_experience"]
    dataset[1]["years_experience"] = None
    del dataset[2]["applied_date"]

    import_records(conn, dataset)

    years = dict(conn.execute("SELECT id, years_experience FROM candidates WHERE id IN ('cand_001', 'cand_002')"))
    assert years == {"cand_001": None, "cand_002": None}
    assert conn.execute("SELECT applied_date FROM candidates WHERE id = 'cand_003'").fetchone()[0] is None


def test_whole_number_float_years_is_accepted(conn, dataset):
    dataset[0]["years_experience"] = 6.0

    import_records(conn, dataset)

    assert conn.execute("SELECT years_experience FROM candidates WHERE id = 'cand_001'").fetchone()[0] == 6


def test_skills_are_trimmed_deduplicated_and_shared(conn, dataset):
    dataset[0]["skills"] = ["  python ", "Python", "Next.js"]
    dataset[1]["skills"] = ["PYTHON"]

    import_records(conn, dataset[:2])

    assert conn.execute("SELECT COUNT(*) FROM candidate_skills WHERE candidate_id = 'cand_001'").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM skills WHERE name_key = 'python'").fetchall()[0][0] == "python"
    assert count(conn, "skills") == 2


def test_cli_exit_code_is_1_when_records_are_invalid(db_path, dataset, write_json, capsys):
    dataset[0]["name"] = ""

    assert main(["--json", str(write_json(dataset)), "--db", str(db_path)]) == 1
    assert "invalid record #0 (cand_001)" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{not json", '{"candidates": []}'])
def test_cli_exit_code_is_2_for_unreadable_dataset(db_path, tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")

    assert main(["--json", str(path), "--db", str(db_path)]) == 2


# --- Schema constraints -----------------------------------------------------

def test_cannot_delete_candidate_with_recruiter_data(conn, dataset):
    import_records(conn, dataset)
    tag_and_shortlist(conn, "cand_001")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM candidates WHERE id = 'cand_001'")


@pytest.mark.parametrize("tag", ["Strong", " padded ", "", "x" * 41])
def test_tags_must_be_normalized(conn, dataset, tag):
    import_records(conn, dataset)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO candidate_tags VALUES ('cand_001', ?, 'now')", (tag,))


def test_candidate_must_point_to_a_stored_revision(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidates (id, name, target_role, source, notes, content_hash, imported_at, updated_at) "
            "VALUES ('x', 'X', 'Role', 'Src', '', 'no-such-hash', 'now', 'now')"
        )


def test_init_schema_is_idempotent(conn):
    init_schema(conn)
    init_schema(conn)


def test_legacy_database_is_rejected(tmp_path):
    legacy = connect(tmp_path / "legacy.db")
    legacy.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, skills_json TEXT)")

    with pytest.raises(LegacySchemaError):
        init_schema(legacy)
    legacy.close()
