import pytest
from fastapi.testclient import TestClient

from app.db import LegacySchemaError, connect
from app.importer import import_records
from app.main import create_app


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


def test_health_reports_imported_candidates(conn, db_path, dataset):
    import_records(conn, dataset)

    with TestClient(create_app(db_path)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["db"] == "ok"
    assert response.json()["candidates"] == 48


def test_health_on_empty_database(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["candidates"] == 0


def test_startup_creates_schema(client, db_path):
    conn = connect(db_path)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()

    assert {"candidates", "candidate_revisions", "candidate_tags", "shortlist", "ai_summaries"} <= tables


@pytest.mark.parametrize("key, expected", [("gsk-test", True), ("", False), ("   ", False), (None, False)])
def test_health_reports_whether_ai_is_configured(client, monkeypatch, key, expected):
    if key is not None:
        monkeypatch.setenv("GROQ_API_KEY", key)

    assert client.get("/api/health").json()["ai_configured"] is expected


def test_health_never_returns_the_api_key(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")

    assert "gsk-secret-value" not in client.get("/api/health").text


def test_health_returns_503_when_database_is_unreadable(client, tmp_path):
    client.app.state.db_path = tmp_path  # a directory cannot be opened as a database

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "candidates": None, "ai_configured": False, "demo_accounts": []}


def test_unknown_api_route_returns_404(client):
    assert client.get("/api/nope").status_code == 404


def test_startup_rejects_legacy_database(tmp_path):
    legacy_path = tmp_path / "legacy.db"
    legacy = connect(legacy_path)
    legacy.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, skills_json TEXT)")
    legacy.close()

    with pytest.raises(LegacySchemaError):
        with TestClient(create_app(legacy_path)):
            pass


def test_db_path_can_come_from_environment(tmp_path, monkeypatch):
    env_path = tmp_path / "from_env.db"
    monkeypatch.setenv("GCCX_DB_PATH", str(env_path))

    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
    assert env_path.exists()


def test_health_lists_demo_accounts_only_when_enabled(client, monkeypatch):
    monkeypatch.delenv("GCCX_DEMO_LOGIN", raising=False)
    assert client.get("/api/health").json()["demo_accounts"] == []

    monkeypatch.setenv("GCCX_DEMO_LOGIN", "1")
    emails = [a["email"] for a in client.get("/api/health").json()["demo_accounts"]]
    assert "admin@gmail.com" in emails
