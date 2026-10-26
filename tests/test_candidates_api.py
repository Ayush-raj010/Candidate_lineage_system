"""Tests for GET /api/candidates, GET /api/candidates/{id} and GET /api/facets.

Expected results are computed from candidates.json in Python, independently
of the SQL, and compared with what the API returns.
"""

from collections import Counter

import pytest

from app.importer import import_records
from tests.helpers import signed_in_client

ITEM_FIELDS = {"id", "name", "target_role", "years_experience", "source", "applied_date", "skills", "tags", "shortlisted"}


@pytest.fixture
def client(conn, db_path, dataset):
    import_records(conn, dataset)
    with signed_in_client(db_path) as test_client:
        yield test_client


@pytest.fixture
def empty_client(conn, db_path):
    with signed_in_client(db_path) as test_client:
        yield test_client


def all_items(client, **params):
    """Fetch every page of a listing."""
    items, offset = [], 0
    while True:
        page = client.get("/api/candidates", params={**params, "limit": 200, "offset": offset}).json()
        items += page["items"]
        offset += 200
        if offset >= page["total"]:
            return items


def ids(client, **params):
    return [item["id"] for item in all_items(client, **params)]


def matches(record, q):
    """Reference search: every word appears in name, role, notes or a skill (case-insensitive)."""
    text = [record["name"], record["target_role"], record["notes"], *record["skills"]]
    return all(any(word.lower() in field.lower() for field in text) for word in q.split())


def expected_order(records, sort):
    """Reference sort: the requested field, unknown values last, then id."""
    field = sort.lstrip("-")
    descending = sort.startswith("-")
    by_id = sorted(records, key=lambda r: r["id"])
    if field == "name":
        ordered = sorted(by_id, key=lambda r: r["name"].lower(), reverse=descending)
    else:
        known = [r for r in by_id if r.get(field) is not None]
        unknown = [r for r in by_id if r.get(field) is None]
        ordered = sorted(known, key=lambda r: r[field], reverse=descending) + unknown
    return [r["id"] for r in ordered]


def add_tag(conn, candidate_id, tag):
    conn.execute("INSERT INTO candidate_tags (candidate_id, tag, created_at) VALUES (?, ?, 'now')", (candidate_id, tag))


def shortlist(conn, candidate_id):
    conn.execute("INSERT INTO shortlist (candidate_id, shortlisted_at) VALUES (?, 'now')", (candidate_id,))


# --- Listing ----------------------------------------------------------------

def test_list_returns_every_candidate_newest_first(client, dataset):
    page = client.get("/api/candidates").json()

    assert page["total"] == 48 and page["limit"] == 50 and page["offset"] == 0
    assert [item["id"] for item in page["items"]] == expected_order(dataset, "-applied_date")


def test_list_item_shape_and_values(client, dataset):
    items = {item["id"]: item for item in client.get("/api/candidates").json()["items"]}

    for record in dataset:
        item = items[record["id"]]
        assert set(item) == ITEM_FIELDS
        for key in ("name", "target_role", "years_experience", "source", "applied_date"):
            assert item[key] == record[key]
        assert item["skills"] == sorted(record["skills"], key=str.lower)
        assert item["tags"] == [] and item["shortlisted"] is False


def test_list_does_not_expose_notes_or_internal_columns(client):
    item = client.get("/api/candidates", params={"limit": 1}).json()["items"][0]

    assert not {"notes", "content_hash", "imported_at", "updated_at"} & set(item)


def test_empty_database_returns_empty_page(empty_client):
    response = empty_client.get("/api/candidates")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


# --- Pagination -------------------------------------------------------------

def test_pages_cover_every_candidate_once_in_order(client, dataset):
    seen = []
    for offset in range(0, 50, 10):
        page = client.get("/api/candidates", params={"limit": 10, "offset": offset}).json()
        assert page["total"] == 48
        seen += [item["id"] for item in page["items"]]

    assert seen == expected_order(dataset, "-applied_date")


def test_offset_past_the_end_returns_no_items_but_keeps_total(client):
    page = client.get("/api/candidates", params={"offset": 1000}).json()

    assert page["items"] == [] and page["total"] == 48


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 201}, {"limit": -1}, {"limit": "ten"}, {"limit": 1.5},
    {"offset": -1}, {"offset": "x"}, {"offset": 10**12}, {"offset": 2**64},
])
def test_invalid_pagination_is_rejected(client, params):
    assert client.get("/api/candidates", params=params).status_code == 422


# --- Filters ----------------------------------------------------------------

@pytest.mark.parametrize("field", ["target_role", "source"])
def test_each_role_and_source_filter_matches_the_dataset(client, dataset, field):
    param = "role" if field == "target_role" else "source"
    for value in {r[field] for r in dataset}:
        expected = sorted(r["id"] for r in dataset if r[field] == value)
        assert sorted(ids(client, **{param: value})) == expected, value


def test_role_and_source_combine_with_and(client, dataset):
    expected = sorted(r["id"] for r in dataset if r["target_role"] == "ML Engineer" and r["source"] == "AngelList")

    assert sorted(ids(client, role="ML Engineer", source="AngelList")) == expected


@pytest.mark.parametrize("params", [{"role": "Astronaut"}, {"source": "Carrier Pigeon"}, {"role": "ml engineer"}])
def test_unmatched_filter_returns_empty_result(client, params):
    response = client.get("/api/candidates", params=params)

    assert response.status_code == 200
    assert response.json()["total"] == 0 and response.json()["items"] == []


@pytest.mark.parametrize("params", [{"role": ""}, {"role": "   "}, {"source": ""}, {"tag": ""}, {"q": "  "}])
def test_blank_filter_means_no_filter(client, params):
    assert client.get("/api/candidates", params=params).json()["total"] == 48


def test_filter_values_are_trimmed(client, dataset):
    expected = sum(r["target_role"] == "ML Engineer" for r in dataset)

    assert client.get("/api/candidates", params={"role": "  ML Engineer "}).json()["total"] == expected


def test_tag_filter(client, conn):
    add_tag(conn, "cand_001", "strong-backend")
    add_tag(conn, "cand_005", "strong-backend")
    add_tag(conn, "cand_005", "follow-up")

    assert sorted(ids(client, tag="strong-backend")) == ["cand_001", "cand_005"]
    assert ids(client, tag="follow-up") == ["cand_005"]
    assert sorted(ids(client, tag="  Strong-Backend ")) == ["cand_001", "cand_005"]
    assert ids(client, tag="unused") == []


def test_list_items_include_tags_and_shortlist_state(client, conn):
    add_tag(conn, "cand_005", "strong-backend")
    add_tag(conn, "cand_005", "follow-up")
    shortlist(conn, "cand_005")

    item = next(i for i in all_items(client) if i["id"] == "cand_005")

    assert item["tags"] == ["follow-up", "strong-backend"] and item["shortlisted"] is True


@pytest.mark.parametrize("true_value, false_value", [("true", "false"), ("1", "0"), ("yes", "no")])
def test_shortlisted_filter(client, conn, true_value, false_value):
    shortlist(conn, "cand_003")
    shortlist(conn, "cand_010")

    assert sorted(ids(client, shortlisted=true_value)) == ["cand_003", "cand_010"]
    assert len(ids(client, shortlisted=false_value)) == 46


def test_invalid_shortlisted_value_is_rejected(client):
    assert client.get("/api/candidates", params={"shortlisted": "maybe"}).status_code == 422


def test_all_filters_combined(client, conn, dataset):
    targets = [r["id"] for r in dataset if r["target_role"] == "Mobile Engineer (Android)" and r["source"] == "Naukri"]
    for candidate_id in targets:
        add_tag(conn, candidate_id, "mobile")
    shortlist(conn, targets[0])

    result = ids(client, role="Mobile Engineer (Android)", source="Naukri", tag="mobile", shortlisted="true")

    assert result == [targets[0]]


# --- Search -----------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "kubernetes", "KUBERNETES", "sameer", "Sameer Rao", "series b", "Next.js", "ci/cd",
    "android", "devops kubernetes", "GitHub", "rao kotlin", "zzzz-no-match",
])
def test_search_matches_reference_implementation(client, dataset, q):
    expected = expected_order([r for r in dataset if matches(r, q)], "-applied_date")

    assert ids(client, q=q) == expected


def test_search_finds_text_that_only_appears_in_notes(client, dataset):
    # "portfolio" appears in notes but not in any name, role or skill.
    expected = sorted(r["id"] for r in dataset if "portfolio" in r["notes"].lower())

    assert expected and sorted(ids(client, q="portfolio")) == expected


def test_search_combines_with_filters(client, dataset):
    expected = sorted(r["id"] for r in dataset if matches(r, "docker") and r["source"] == "AngelList")

    assert sorted(ids(client, q="docker", source="AngelList")) == expected


@pytest.mark.parametrize("q", ["%", "_", "\\", "%%", "a_b", "100%", "\\%"])
def test_like_wildcards_are_matched_literally(client, dataset, q):
    # None of these characters occur in the dataset, so a correct escape finds nothing;
    # an unescaped % or _ would match every candidate.
    assert ids(client, q=q) == [r["id"] for r in dataset if matches(r, q)] == []


def test_search_longer_than_100_characters_is_rejected(client):
    assert client.get("/api/candidates", params={"q": "a" * 101}).status_code == 422
    assert client.get("/api/candidates", params={"q": "a" * 100}).status_code == 200


# --- Sorting ----------------------------------------------------------------

@pytest.mark.parametrize("sort", ["-applied_date", "applied_date", "name", "-name", "-years_experience", "years_experience"])
def test_every_allowed_sort_order(client, dataset, sort):
    assert ids(client, sort=sort) == expected_order(dataset, sort)


@pytest.mark.parametrize("sort", ["-years_experience", "years_experience", "-applied_date", "applied_date"])
def test_unknown_values_sort_last_in_both_directions(conn, db_path, dataset, sort):
    del dataset[0]["years_experience"]
    dataset[1]["years_experience"] = None
    del dataset[2]["applied_date"]
    import_records(conn, dataset)

    with signed_in_client(db_path) as client:
        result = ids(client, sort=sort)

    assert result == expected_order(dataset, sort)
    unknown = {"cand_001", "cand_002"} if "years" in sort else {"cand_003"}
    assert set(result[-len(unknown):]) == unknown


@pytest.mark.parametrize("sort", ["id", "notes", "random()", "name DESC", "name; DROP TABLE candidates", "-", ""])
def test_sort_outside_allowlist_is_rejected(client, sort):
    response = client.get("/api/candidates", params={"sort": sort})

    assert response.status_code == 422
    assert "applied_date" in response.text  # the error lists the allowed values


def test_unknown_query_parameter_is_rejected(client):
    response = client.get("/api/candidates", params={"rol": "ML Engineer"})

    assert response.status_code == 422
    assert "rol" in response.text


# --- SQL injection ----------------------------------------------------------

INJECTIONS = [
    "' OR '1'='1",
    "' OR 1=1 --",
    "x'; DROP TABLE candidates; --",
    "\" OR \"\"=\"",
    "') UNION SELECT name, sql, 1, 1, 1, 1, 1 FROM sqlite_master --",
    "1; DELETE FROM candidate_revisions",
]


@pytest.mark.parametrize("payload", INJECTIONS)
@pytest.mark.parametrize("param", ["q", "role", "source", "tag"])
def test_injection_attempts_in_filters_are_plain_text(client, conn, dataset, param, payload):
    response = client.get("/api/candidates", params={param: payload[:40]})

    assert response.status_code == 200
    expected = [r["id"] for r in dataset if matches(r, payload[:40])] if param == "q" else []
    assert sorted(i["id"] for i in response.json()["items"]) == sorted(expected)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 48
    assert conn.execute("SELECT COUNT(*) FROM candidate_revisions").fetchone()[0] == 48


@pytest.mark.parametrize("payload", INJECTIONS + ["cand_001' --", "cand_001%", "%"])
def test_injection_attempts_in_candidate_id_return_404(client, conn, payload):
    response = client.get(f"/api/candidates/{payload}")

    assert response.status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 48


# --- Detail -----------------------------------------------------------------

def test_detail_matches_source_record(client, dataset):
    for record in dataset:
        detail = client.get(f"/api/candidates/{record['id']}").json()

        assert set(detail) == ITEM_FIELDS | {"notes"}
        for key in ("id", "name", "target_role", "years_experience", "source", "applied_date", "notes"):
            assert detail[key] == record[key], (record["id"], key)
        assert detail["skills"] == sorted(record["skills"], key=str.lower)


def test_detail_includes_tags_and_shortlist(client, conn):
    add_tag(conn, "cand_007", "frontend")
    shortlist(conn, "cand_007")

    detail = client.get("/api/candidates/cand_007").json()

    assert detail["tags"] == ["frontend"] and detail["shortlisted"] is True


def test_detail_returns_null_for_unstated_values(conn, db_path, dataset):
    del dataset[0]["years_experience"]
    del dataset[0]["applied_date"]
    import_records(conn, dataset)

    with signed_in_client(db_path) as client:
        detail = client.get("/api/candidates/cand_001").json()

    assert detail["years_experience"] is None and detail["applied_date"] is None


@pytest.mark.parametrize("candidate_id", ["cand_999", "CAND_001", "cand_001 ", "nope"])
def test_unknown_candidate_returns_404(client, candidate_id):
    response = client.get(f"/api/candidates/{candidate_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Candidate not found"}


def test_overlong_candidate_id_is_rejected(client):
    assert client.get(f"/api/candidates/{'x' * 65}").status_code == 422


# --- Facets -----------------------------------------------------------------

def facet_counts(records, field):
    return Counter(value for r in records for value in (r[field] if field == "skills" else [r[field]]))


def test_facets_match_the_dataset(client, dataset):
    facets = client.get("/api/facets").json()

    assert facets["total"] == 48 and facets["shortlisted"] == 0 and facets["tags"] == []
    for name, field in [("roles", "target_role"), ("sources", "source"), ("skills", "skills")]:
        assert {f["value"]: f["count"] for f in facets[name]} == facet_counts(dataset, field), name


def test_facets_are_sorted_by_count_then_value(client):
    facets = client.get("/api/facets").json()

    for name in ("roles", "sources", "skills"):
        values = facets[name]
        assert values == sorted(values, key=lambda f: (-f["count"], f["value"].lower())), name


def test_facets_count_tags_and_shortlist(client, conn):
    add_tag(conn, "cand_001", "strong-backend")
    add_tag(conn, "cand_002", "strong-backend")
    add_tag(conn, "cand_002", "follow-up")
    shortlist(conn, "cand_001")

    facets = client.get("/api/facets").json()

    assert facets["tags"] == [{"value": "strong-backend", "count": 2}, {"value": "follow-up", "count": 1}]
    assert facets["shortlisted"] == 1


def test_facets_on_empty_database(empty_client):
    assert empty_client.get("/api/facets").json() == {
        "total": 0, "shortlisted": 0, "roles": [], "sources": [], "skills": [], "tags": [],
    }


def test_facet_values_work_as_filters(client):
    facets = client.get("/api/facets").json()

    for name, param in [("roles", "role"), ("sources", "source")]:
        for facet in facets[name]:
            assert client.get("/api/candidates", params={param: facet["value"]}).json()["total"] == facet["count"]
