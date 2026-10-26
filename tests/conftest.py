import copy
import json

import pytest

from app.db import connect, init_schema
from app.importer import DEFAULT_JSON_PATH, load_records


AI_ENV_VARS = ("GROQ_API_KEY", "GROQ_MODEL", "GROQ_BASE_URL", "GROQ_TIMEOUT_SECONDS")


@pytest.fixture(autouse=True)
def _no_real_ai_credentials(monkeypatch):
    """app.main loads .env on import; tests must never see or use the real key."""
    for name in AI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def _dataset():
    return load_records(DEFAULT_JSON_PATH)


@pytest.fixture
def dataset(_dataset):
    """A fresh, mutable copy of the real candidates.json records."""
    return copy.deepcopy(_dataset)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    connection = connect(db_path)
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def write_json(tmp_path):
    def _write(records, name="candidates.json"):
        path = tmp_path / name
        path.write_text(json.dumps(records), encoding="utf-8")
        return path

    return _write
