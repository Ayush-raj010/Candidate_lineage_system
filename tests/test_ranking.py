"""Tests for skill-overlap ranking: GET /api/rank, the SQL in repository.py,
and the pure-Python reference in app/ranking.py."""

import random

import pytest

from app import repository
from app.db import connect, init_schema
from app.importer import import_records
from tests.helpers import signed_in_client
from app.ranking import SkillIndex, rank_naive
from benchmarks.synthetic import ROLES, SOURCES, generate, skill_pool

SYNTHETIC_COUNT = 3000


@pytest.fixture
def client(conn, db_path, dataset):
    import_records(conn, dataset)
    with signed_in_client(db_path) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    """A few thousand synthetic candidates, imported once for the whole module."""
    records = generate(SYNTHETIC_COUNT, seed=7)
    path = tmp_path_factory.mktemp("synthetic") / "synthetic.db"
    conn = connect(path)
    init_schema(conn)
    import_records(conn, records)
    yield records, conn
    conn.close()


def rank(client, skills, **params):
    return client.get("/api/rank", params={"skills": skills, **params})


def ranked(response):
    return [(item["candidate"]["id"], item["score"]) for item in response.json()["items"]]


def candidate(id, skills, years=5, role="Backend Engineer", source="Referral"):
    return {"id": id, "name": f"Name {id}", "target_role": role, "years_experience": years, "source": source,
            "skills": skills, "notes": "", "applied_date": "2026-09-01"}


# --- Matches the reference on the real dataset -------------------------------

@pytest.mark.parametrize("skills", [
    ["Python"], ["Python", "FastAPI", "Django"], ["Kubernetes", "Docker", "Terraform", "AWS"],
    ["Swift", "Kotlin"], ["Next.js", "React", "TypeScript", "Node.js", "GraphQL"], ["Cypress"],
])
def test_ranking_matches_reference_on_dataset(client, dataset, skills):
    response = rank(client, skills, limit=200)

    assert response.status_code == 200
    expected = rank_naive(dataset, skills, 200)
    assert ranked(response) == expected
    assert response.json()["total"] == len(expected)


def test_response_shape(client, dataset):
    body = rank(client, ["Python", "FastAPI", "Django"]).json()

    assert set(body) == {"query_skills", "unknown_skills", "items", "total", "limit", "offset"}
    top = body["items"][0]
    assert set(top) == {"candidate", "score", "coverage", "matched_skills", "missing_skills"}
    record = next(r for r in dataset if r["id"] == top["candidate"]["id"])
    assert top["candidate"]["name"] == record["name"]
    assert top["candidate"]["skills"] == sorted(record["skills"], key=str.lower)


def test_score_coverage_and_matched_skills(client, dataset):
    body = rank(client, ["Python", "FastAPI", "Django", "Rust"], limit=200).json()

    assert body["query_skills"] == ["Python", "FastAPI", "Django"]
    assert body["unknown_skills"] == ["Rust"]
    for item in body["items"]:
        record = next(r for r in dataset if r["id"] == item["candidate"]["id"])
        has = set(record["skills"])
        assert item["matched_skills"] == [s for s in ["Python", "FastAPI", "Django"] if s in has]
        assert item["missing_skills"] == [s for s in ["Python", "FastAPI", "Django"] if s not in has]
        assert item["score"] == len(item["matched_skills"]) >= 1
        assert item["coverage"] == round(item["score"] / 3, 4)  # unknown skills don't count against anyone


def test_candidates_with_no_required_skill_are_excluded(client, dataset):
    body = rank(client, ["Cypress"], limit=200).json()

    expected = {r["id"] for r in dataset if "Cypress" in r["skills"]}
    assert {i["candidate"]["id"] for i in body["items"]} == expected and body["total"] == len(expected)


# --- Input handling ---------------------------------------------------------

def test_skill_matching_ignores_case_and_spacing(client):
    body = rank(client, ["  python ", "FASTAPI", "rest   apis"]).json()

    assert body["query_skills"] == ["Python", "FastAPI", "REST APIs"]
    assert body["unknown_skills"] == []


def test_duplicate_skills_count_once(client, dataset):
    once = rank(client, ["Python"], limit=200)
    repeated = rank(client, ["Python", "python", " PYTHON "], limit=200)

    assert ranked(repeated) == ranked(once)
    assert repeated.json()["query_skills"] == ["Python"]
    assert max(score for _, score in ranked(repeated)) == 1


def test_unknown_skills_only_returns_empty_result(client):
    response = rank(client, ["Rust", "Elixir"])

    assert response.status_code == 200
    assert response.json() == {
        "query_skills": [], "unknown_skills": ["Rust", "Elixir"], "items": [], "total": 0, "limit": 20, "offset": 0,
    }


def test_comma_separated_value_is_one_skill(client):
    body = rank(client, ["Python,FastAPI"]).json()

    assert body["unknown_skills"] == ["Python,FastAPI"] and body["items"] == []


def test_blank_skills_are_ignored(client):
    assert rank(client, ["", "Python", "  "]).json()["query_skills"] == ["Python"]


def test_twenty_skills_is_the_maximum(client):
    skills = [f"skill {n}" for n in range(20)]

    assert rank(client, skills).status_code == 200
    assert rank(client, [*skills, "one more"]).status_code == 422


@pytest.mark.parametrize("query", [
    "",
    "skills=",
    "skills=%20&skills=",
    f"skills={'x' * 61}",
    "skills=Python&limit=0",
    "skills=Python&limit=201",
    "skills=Python&limit=abc",
    "skills=Python&offset=-1",
    f"skills=Python&role={'r' * 101}",
    "skills=Python&sort=name",
    "skill=Python",
])
def test_invalid_requests_are_rejected(client, query):
    assert client.get(f"/api/rank?{query}").status_code == 422


# --- Filters and pagination -------------------------------------------------

def test_role_filter(client, dataset):
    expected = rank_naive([r for r in dataset if r["target_role"] == "ML Engineer"], ["Java", "Python"], 200)

    assert ranked(rank(client, ["Java", "Python"], role="ML Engineer", limit=200)) == expected


def test_source_filter(client, dataset):
    expected = rank_naive([r for r in dataset if r["source"] == "AngelList"], ["Docker", "Terraform"], 200)

    assert ranked(rank(client, ["Docker", "Terraform"], source="AngelList", limit=200)) == expected


def test_role_and_source_filters_combine(client, dataset):
    subset = [r for r in dataset if r["target_role"] == "Mobile Engineer (Android)" and r["source"] == "Naukri"]
    expected = rank_naive(subset, ["Kotlin", "Docker", "Next.js"], 200)

    body = rank(client, ["Kotlin", "Docker", "Next.js"], role="Mobile Engineer (Android)", source="Naukri", limit=200).json()
    assert [(i["candidate"]["id"], i["score"]) for i in body["items"]] == expected
    assert body["total"] == len(expected)


@pytest.mark.parametrize("params", [{"role": "Astronaut"}, {"source": "Nowhere"}, {"role": "ml engineer"}])
def test_unmatched_filter_returns_empty(client, params):
    body = rank(client, ["Python"], **params).json()

    assert body["items"] == [] and body["total"] == 0 and body["query_skills"] == ["Python"]


def test_blank_filters_mean_no_filter(client):
    assert ranked(rank(client, ["Python"], role="", source="  ", limit=200)) == ranked(rank(client, ["Python"], limit=200))


def test_pages_join_up_to_the_full_ranking(client, dataset):
    skills = ["Docker", "Next.js", "Terraform"]
    pages = []
    for offset in range(0, 40, 7):
        pages += ranked(rank(client, skills, limit=7, offset=offset))

    assert pages == rank_naive(dataset, skills, 200)


def test_offset_past_the_end_keeps_total(client):
    body = rank(client, ["Python"], offset=500).json()

    assert body["items"] == [] and body["total"] > 0


# --- Ordering ---------------------------------------------------------------

def test_ties_break_on_years_then_id_with_unknown_years_last(conn, db_path):
    import_records(conn, [
        candidate("c_low_years", ["Go", "Rust"], years=2),
        candidate("c_b_unknown", ["Go", "Rust"], years=None),
        candidate("c_high_years", ["Go", "Rust"], years=9),
        candidate("c_a_unknown", ["Go", "Rust"], years=None),
        candidate("c_b_same", ["Go", "Rust"], years=5),
        candidate("c_a_same", ["Go", "Rust"], years=5),
        candidate("c_one_skill", ["Go"], years=15),
        candidate("c_no_skill", ["Java"], years=20),
    ])

    with signed_in_client(db_path) as client:
        order = [cid for cid, _ in ranked(rank(client, ["Go", "Rust"]))]

    assert order == ["c_high_years", "c_a_same", "c_b_same", "c_low_years", "c_a_unknown", "c_b_unknown", "c_one_skill"]


def test_higher_overlap_beats_more_experience(conn, db_path):
    import_records(conn, [candidate("senior", ["Go"], years=20), candidate("junior", ["Go", "Rust", "SQL"], years=0)])

    with signed_in_client(db_path) as client:
        assert ranked(rank(client, ["Go", "Rust", "SQL"])) == [("junior", 3), ("senior", 1)]


# --- Recruiter state and safety ---------------------------------------------

def test_ranked_candidates_include_tags_and_shortlist(client, conn):
    conn.execute("INSERT INTO candidate_tags VALUES ('cand_002', 'strong-backend', 'now')")
    conn.execute("INSERT INTO shortlist VALUES ('cand_002', 'now')")

    top = rank(client, ["Python", "FastAPI", "Django"]).json()["items"][0]["candidate"]

    assert top["id"] == "cand_002" and top["tags"] == ["strong-backend"] and top["shortlisted"] is True


@pytest.mark.parametrize("payload", ["' OR '1'='1", "Python'); DROP TABLE skills; --", "%", "_"])
def test_injection_attempts_are_plain_text(client, conn, payload):
    body = rank(client, [payload], role=payload).json()

    assert body["unknown_skills"] == [payload] and body["items"] == []
    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 30
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 48


def test_empty_database(conn, db_path):
    with signed_in_client(db_path) as client:
        body = rank(client, ["Python"]).json()

    assert body["items"] == [] and body["total"] == 0 and body["unknown_skills"] == ["Python"]


def test_ranking_does_not_write(client, conn):
    before = conn.total_changes

    rank(client, ["Python", "Docker"], role="ML Engineer")

    assert conn.execute("SELECT COUNT(*) FROM candidate_tags").fetchone()[0] == 0
    assert conn.total_changes == before


# --- Agreement on synthetic data --------------------------------------------

def random_queries(n, seed=11):
    rng = random.Random(seed)
    pool = skill_pool()
    queries = []
    for _ in range(n):
        skills = rng.sample(pool, rng.randint(1, 8))
        role = rng.choice([None, None, *ROLES])
        source = rng.choice([None, None, *SOURCES])
        queries.append((skills, role, source))
    return queries


def test_index_matches_naive_on_synthetic_data(synthetic):
    records, _ = synthetic
    index = SkillIndex(records)

    for skills, _, _ in random_queries(40):
        assert index.rank(skills, 25) == rank_naive(records, skills, 25), skills


@pytest.mark.parametrize("query_number", range(40))
def test_sql_matches_reference_on_synthetic_data(synthetic, query_number):
    records, conn = synthetic
    skills, role, source = random_queries(40)[query_number]
    subset = [r for r in records if (role is None or r["target_role"] == role) and (source is None or r["source"] == source)]

    result = repository.rank_candidates(conn, skills, role=role, source=source, limit=25)

    assert [(i["candidate"]["id"], i["score"]) for i in result["items"]] == rank_naive(subset, skills, 25)
    assert result["total"] == sum(1 for r in subset if set(r["skills"]) & set(skills))


# --- Query plans ------------------------------------------------------------

def query_plans(conn, skills, **filters):
    """EXPLAIN QUERY PLAN for the ranking statements repository.rank_candidates runs."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        repository.rank_candidates(conn, skills, **filters)
    finally:
        conn.set_trace_callback(None)
    ranking_sql = [s for s in statements if s.startswith("WITH matches")]
    assert len(ranking_sql) == 2  # total + page
    return [" | ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")) for sql in ranking_sql]


def test_unfiltered_ranking_reads_only_the_skill_index(synthetic):
    _, conn = synthetic

    for plan in query_plans(conn, ["Python", "Go"]):
        assert "SEARCH candidate_skills USING COVERING INDEX ix_cs_skill (skill_id=?)" in plan
        assert "SCAN candidate_skills" not in plan and "SCAN c" not in plan.replace("SCAN m", "")


def test_filtered_ranking_starts_from_the_filter_index(synthetic):
    _, conn = synthetic

    for plan in query_plans(conn, ["Python", "Go"], role="ML Engineer"):
        assert "USING INDEX ix_candidates_role (target_role=?)" in plan
        assert "SEARCH cs USING PRIMARY KEY (candidate_id=? AND skill_id=?)" in plan
