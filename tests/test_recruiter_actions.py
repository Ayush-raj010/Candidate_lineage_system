"""Tests for the tag and shortlist endpoints and for how their data persists."""

from datetime import datetime

import pytest

from app.importer import import_records
from tests.helpers import signed_in_client


@pytest.fixture
def client(conn, db_path, dataset):
    import_records(conn, dataset)
    with signed_in_client(db_path) as test_client:
        yield test_client


def add(client, candidate_id, tag):
    return client.post(f"/api/candidates/{candidate_id}/tags", json={"tag": tag})


def remove(client, candidate_id, tag):
    return client.delete(f"/api/candidates/{candidate_id}/tags/{tag}")


def set_shortlist(client, candidate_id, value):
    return client.put(f"/api/candidates/{candidate_id}/shortlist", json={"shortlisted": value})


def tag_rows(conn, candidate_id=None):
    sql, params = "SELECT candidate_id, tag, created_at FROM candidate_tags", ()
    if candidate_id:
        sql, params = sql + " WHERE candidate_id = ?", (candidate_id,)
    return [tuple(row) for row in conn.execute(sql + " ORDER BY candidate_id, tag", params)]


def shortlist_rows(conn):
    return [tuple(row) for row in conn.execute("SELECT candidate_id, shortlisted_at FROM shortlist ORDER BY candidate_id")]


def evidence(conn):
    """Everything the importer owns, to show recruiter actions never change it."""
    return (
        [tuple(r) for r in conn.execute("SELECT * FROM candidates ORDER BY id")],
        [tuple(r) for r in conn.execute("SELECT * FROM candidate_revisions ORDER BY candidate_id, content_hash")],
        [tuple(r) for r in conn.execute("SELECT * FROM candidate_skills ORDER BY candidate_id, skill_id")],
    )


def is_utc_timestamp(value):
    return datetime.fromisoformat(value).utcoffset().total_seconds() == 0


# --- Adding tags ------------------------------------------------------------

def test_add_tag_creates_it(client, conn):
    response = add(client, "cand_001", "strong-backend")

    assert response.status_code == 201
    assert response.json() == {"id": "cand_001", "tags": ["strong-backend"]}
    [(candidate_id, tag, created_at)] = tag_rows(conn)
    assert (candidate_id, tag) == ("cand_001", "strong-backend") and is_utc_timestamp(created_at)


def test_adding_a_duplicate_tag_changes_nothing(client, conn):
    add(client, "cand_001", "strong-backend")
    # Backdate the row so a duplicate that overwrote it would be detectable.
    conn.execute("UPDATE candidate_tags SET created_at = '2026-01-01T00:00:00+00:00'")

    response = add(client, "cand_001", "strong-backend")

    assert response.status_code == 200
    assert response.json() == {"id": "cand_001", "tags": ["strong-backend"]}
    assert tag_rows(conn) == [("cand_001", "strong-backend", "2026-01-01T00:00:00+00:00")]


@pytest.mark.parametrize("raw, stored", [
    ("Strong-Backend", "strong-backend"),
    ("  follow up  ", "follow up"),
    ("Needs   Second\tCall", "needs second call"),
    ("ml_2026", "ml_2026"),
    ("a", "a"),
    ("x" * 40, "x" * 40),
])
def test_tags_are_normalized(client, conn, raw, stored):
    assert add(client, "cand_001", raw).json()["tags"] == [stored]
    assert tag_rows(conn)[0][1] == stored


def test_differently_written_tags_count_as_duplicates(client, conn):
    assert add(client, "cand_001", "Strong Backend").status_code == 201
    assert add(client, "cand_001", "STRONG   BACKEND ").status_code == 200

    assert len(tag_rows(conn)) == 1


def test_tags_are_returned_sorted(client):
    for tag in ["zeta", "alpha", "mid"]:
        response = add(client, "cand_001", tag)

    assert response.json()["tags"] == ["alpha", "mid", "zeta"]


def test_same_tag_on_different_candidates_is_independent(client, conn):
    add(client, "cand_001", "shared")
    add(client, "cand_002", "shared")

    assert [row[:2] for row in tag_rows(conn)] == [("cand_001", "shared"), ("cand_002", "shared")]


@pytest.mark.parametrize("body", [
    {"tag": ""},
    {"tag": "   "},
    {"tag": "x" * 41},
    {"tag": "bad/tag"},
    {"tag": "a,b"},
    {"tag": "<script>"},
    {"tag": "café"},
    {"tag": "-leading-dash"},
    {"tag": "x'); DROP TABLE candidate_tags; --"},
    {"tag": 123},
    {"tag": True},
    {"tag": None},
    {"tag": ["a"]},
    {},
    {"tag": "ok", "extra": 1},
    {"label": "ok"},
])
def test_invalid_tag_bodies_are_rejected(client, conn, body):
    response = client.post("/api/candidates/cand_001/tags", json=body)

    assert response.status_code == 422
    assert tag_rows(conn) == []


@pytest.mark.parametrize("content", ["not json", '"just a string"', "[]"])
def test_non_object_tag_body_is_rejected(client, conn, content):
    response = client.post(
        "/api/candidates/cand_001/tags", content=content, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert tag_rows(conn) == []


def test_add_tag_to_unknown_candidate_returns_404(client, conn):
    response = add(client, "cand_999", "strong-backend")

    assert response.status_code == 404
    assert response.json() == {"detail": "Candidate not found"}
    assert tag_rows(conn) == []


# --- Removing tags ----------------------------------------------------------

def test_remove_tag(client, conn):
    add(client, "cand_001", "strong-backend")
    add(client, "cand_001", "follow-up")

    response = remove(client, "cand_001", "strong-backend")

    assert response.status_code == 200
    assert response.json() == {"id": "cand_001", "tags": ["follow-up"]}
    assert [row[1] for row in tag_rows(conn)] == ["follow-up"]


def test_removing_a_missing_tag_is_not_an_error(client, conn):
    add(client, "cand_001", "keep")

    for _ in range(2):
        response = remove(client, "cand_001", "never-added")
        assert response.status_code == 200
        assert response.json()["tags"] == ["keep"]


def test_repeated_remove_is_harmless(client, conn):
    add(client, "cand_001", "temp")

    assert remove(client, "cand_001", "temp").json()["tags"] == []
    assert remove(client, "cand_001", "temp").status_code == 200
    assert tag_rows(conn) == []


def test_remove_normalizes_the_tag_in_the_url(client, conn):
    add(client, "cand_001", "strong backend")

    assert remove(client, "cand_001", "  STRONG   Backend").json()["tags"] == []


def test_remove_only_affects_that_candidate(client, conn):
    add(client, "cand_001", "shared")
    add(client, "cand_002", "shared")

    remove(client, "cand_001", "shared")

    assert [row[:2] for row in tag_rows(conn)] == [("cand_002", "shared")]


@pytest.mark.parametrize("tag", ["bad!", "x" * 41, "café", "%27%20OR%201%3D1", " "])
def test_remove_with_invalid_tag_is_rejected(client, conn, tag):
    add(client, "cand_001", "keep")

    assert remove(client, "cand_001", tag).status_code == 422
    assert [row[1] for row in tag_rows(conn)] == ["keep"]


def test_remove_tag_from_unknown_candidate_returns_404(client):
    assert remove(client, "cand_999", "anything").status_code == 404


# --- Shortlist --------------------------------------------------------------

def test_shortlist_candidate(client, conn):
    response = set_shortlist(client, "cand_001", True)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "cand_001" and body["shortlisted"] is True and is_utc_timestamp(body["shortlisted_at"])
    assert shortlist_rows(conn) == [("cand_001", body["shortlisted_at"])]


def test_repeated_shortlist_keeps_original_timestamp(client, conn):
    set_shortlist(client, "cand_001", True)
    # Backdate the row so a re-shortlist that overwrote it would be detectable.
    conn.execute("UPDATE shortlist SET shortlisted_at = '2026-01-01T00:00:00+00:00'")

    second = set_shortlist(client, "cand_001", True).json()

    assert second["shortlisted_at"] == "2026-01-01T00:00:00+00:00"
    assert len(shortlist_rows(conn)) == 1


def test_unshortlist_candidate(client, conn):
    set_shortlist(client, "cand_001", True)

    response = set_shortlist(client, "cand_001", False)

    assert response.status_code == 200
    assert response.json() == {"id": "cand_001", "shortlisted": False, "shortlisted_at": None}
    assert shortlist_rows(conn) == []


def test_unshortlisting_a_candidate_never_shortlisted_is_not_an_error(client, conn):
    for _ in range(2):
        response = set_shortlist(client, "cand_001", False)
        assert response.status_code == 200 and response.json()["shortlisted"] is False
    assert shortlist_rows(conn) == []


def test_shortlist_again_after_removal_gets_new_timestamp(client, conn):
    set_shortlist(client, "cand_001", True)
    conn.execute("UPDATE shortlist SET shortlisted_at = '2026-01-01T00:00:00+00:00'")
    set_shortlist(client, "cand_001", False)

    response = set_shortlist(client, "cand_001", True)

    assert response.json()["shortlisted_at"] != "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("body", [
    {"shortlisted": "true"}, {"shortlisted": "yes"}, {"shortlisted": 1}, {"shortlisted": 0},
    {"shortlisted": None}, {}, {"shortlisted": True, "note": "x"},
])
def test_invalid_shortlist_bodies_are_rejected(client, conn, body):
    set_shortlist(client, "cand_002", True)
    before = shortlist_rows(conn)

    response = client.put("/api/candidates/cand_001/shortlist", json=body)

    assert response.status_code == 422
    assert shortlist_rows(conn) == before


def test_shortlist_unknown_candidate_returns_404(client, conn):
    response = set_shortlist(client, "cand_999", True)

    assert response.status_code == 404
    assert shortlist_rows(conn) == []


@pytest.mark.parametrize("candidate_id", ["cand_001' OR '1'='1", "x'; DELETE FROM shortlist; --", "%"])
def test_injection_in_candidate_id_returns_404_for_every_action(client, conn, candidate_id):
    set_shortlist(client, "cand_002", True)
    add(client, "cand_002", "keep")

    assert add(client, candidate_id, "x").status_code == 404
    assert remove(client, candidate_id, "keep").status_code == 404
    assert set_shortlist(client, candidate_id, False).status_code == 404
    assert len(shortlist_rows(conn)) == 1 and len(tag_rows(conn)) == 1


# --- Visible through the read endpoints -------------------------------------

def test_actions_show_up_in_detail_list_and_facets(client):
    add(client, "cand_005", "strong-backend")
    add(client, "cand_005", "follow-up")
    add(client, "cand_009", "strong-backend")
    set_shortlist(client, "cand_005", True)

    detail = client.get("/api/candidates/cand_005").json()
    assert detail["tags"] == ["follow-up", "strong-backend"] and detail["shortlisted"] is True

    tagged = client.get("/api/candidates", params={"tag": "strong-backend"}).json()
    assert sorted(i["id"] for i in tagged["items"]) == ["cand_005", "cand_009"]
    assert [i["id"] for i in client.get("/api/candidates", params={"shortlisted": "true"}).json()["items"]] == ["cand_005"]

    facets = client.get("/api/facets").json()
    assert facets["tags"] == [{"value": "strong-backend", "count": 2}, {"value": "follow-up", "count": 1}]
    assert facets["shortlisted"] == 1


def test_undo_shows_up_in_read_endpoints(client):
    add(client, "cand_005", "temp")
    set_shortlist(client, "cand_005", True)
    remove(client, "cand_005", "temp")
    set_shortlist(client, "cand_005", False)

    detail = client.get("/api/candidates/cand_005").json()
    assert detail["tags"] == [] and detail["shortlisted"] is False
    assert client.get("/api/facets").json()["tags"] == []


# --- Persistence ------------------------------------------------------------

def test_recruiter_actions_never_change_candidate_evidence(client, conn):
    before = evidence(conn)

    add(client, "cand_001", "a")
    add(client, "cand_001", "a")
    remove(client, "cand_001", "a")
    set_shortlist(client, "cand_001", True)
    set_shortlist(client, "cand_001", False)

    assert evidence(conn) == before


def test_state_survives_an_app_restart(client, db_path):
    add(client, "cand_003", "follow-up")
    set_shortlist(client, "cand_003", True)

    with signed_in_client(db_path) as restarted:
        detail = restarted.get("/api/candidates/cand_003").json()

    assert detail["tags"] == ["follow-up"] and detail["shortlisted"] is True


def test_state_survives_reimporting_the_same_file(client, conn, dataset):
    add(client, "cand_003", "follow-up")
    set_shortlist(client, "cand_003", True)
    before = (tag_rows(conn), shortlist_rows(conn))

    import_records(conn, dataset)

    assert (tag_rows(conn), shortlist_rows(conn)) == before


def test_state_survives_reimport_with_changed_record(client, conn, dataset):
    add(client, "cand_001", "follow-up")
    set_shortlist(client, "cand_001", True)
    before = (tag_rows(conn), shortlist_rows(conn))
    dataset[0]["notes"] = "Updated after screening."
    dataset[0]["skills"] = ["Python"]

    report = import_records(conn, dataset)

    assert report.updated == {"cand_001": ["notes", "skills"]}
    assert (tag_rows(conn), shortlist_rows(conn)) == before
    detail = client.get("/api/candidates/cand_001").json()
    assert detail["notes"] == "Updated after screening." and detail["tags"] == ["follow-up"] and detail["shortlisted"]


def test_state_survives_candidate_missing_from_new_file(client, conn, dataset):
    add(client, "cand_048", "follow-up")
    set_shortlist(client, "cand_048", True)

    report = import_records(conn, dataset[:-1])

    assert report.missing_from_file == ["cand_048"]
    assert tag_rows(conn, "cand_048")[0][1] == "follow-up"
    assert shortlist_rows(conn)[0][0] == "cand_048"


def test_dry_run_import_leaves_state_alone(client, conn, dataset):
    add(client, "cand_001", "follow-up")
    set_shortlist(client, "cand_001", True)
    before = (tag_rows(conn), shortlist_rows(conn))
    dataset[0]["notes"] = "Changed."

    import_records(conn, dataset, dry_run=True)

    assert (tag_rows(conn), shortlist_rows(conn)) == before
