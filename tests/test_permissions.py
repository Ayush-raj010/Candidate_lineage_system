"""Role-based access: every protected route, called directly, by every role."""

import re

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.importer import import_records
from app.main import create_app
from tests.helpers import make_user, sign_in
from tests.test_ai_summary import FakeLLM

PUBLIC_ROUTES = {("GET", "/api/health"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout")}


@pytest.fixture
def app(conn, db_path, dataset):
    import_records(conn, dataset)
    application = create_app(db_path, ai_client=FakeLLM())
    with TestClient(application):  # run startup (migrations) once
        pass
    return application


def client_as(app, role):
    client = TestClient(app)
    if role:
        sign_in(client, role)
    return client


def target_user(app):
    return make_user(app.state.db_path, "recruiter", "target@example.test")[0]


def requests_for(app):
    """(name, method, path, kwargs, permission) for every protected endpoint."""
    uid = target_user(app)
    return [
        ("list candidates", "GET", "/api/candidates", {}, "candidates:read"),
        ("candidate detail", "GET", "/api/candidates/cand_001", {}, "candidates:read"),
        ("facets", "GET", "/api/facets", {}, "candidates:read"),
        ("rank", "GET", "/api/rank?skills=Python", {}, "ranking:read"),
        ("add tag", "POST", "/api/candidates/cand_001/tags", {"json": {"tag": "perm-test"}}, "tags:write"),
        ("remove tag", "DELETE", "/api/candidates/cand_001/tags/perm-test", {}, "tags:write"),
        ("shortlist", "PUT", "/api/candidates/cand_001/shortlist", {"json": {"shortlisted": True}}, "shortlist:write"),
        ("read cached summary", "GET", "/api/candidates/cand_001/summary", {}, "summaries:read"),
        ("generate summary", "POST", "/api/candidates/cand_001/summary", {}, "summaries:generate"),
        ("activity", "GET", "/api/activity", {}, "activity:read"),
        ("list users", "GET", "/api/admin/users", {}, "users:manage"),
        ("create user", "POST", "/api/admin/users",
         {"json": {"email": "new@example.test", "role": "recruiter", "password": "a long enough password"}}, "users:manage"),
        ("change role", "PATCH", f"/api/admin/users/{uid}", {"json": {"role": "hiring_manager"}}, "users:manage"),
        ("deactivate", "PATCH", f"/api/admin/users/{uid}", {"json": {"is_active": False}}, "users:manage"),
        ("reset password", "POST", f"/api/admin/users/{uid}/password",
         {"json": {"password": "another long password"}}, "users:manage"),
        ("audit log", "GET", "/api/admin/audit-logs", {}, "audit:read"),
    ]


def side_effect_snapshot(app):
    from app.db import connect

    conn = connect(app.state.db_path)
    queries = {table: f"SELECT * FROM {table} ORDER BY 1" for table in ("candidate_tags", "shortlist", "ai_summaries", "candidates")}
    queries["users"] = "SELECT id, email, role, is_active, password_hash FROM users ORDER BY id"
    try:
        return {name: [tuple(r) for r in conn.execute(sql)] for name, sql in queries.items()}
    finally:
        conn.close()


# --- Unauthenticated ----------------------------------------------------------------

def test_every_route_except_public_ones_requires_sign_in(app):
    """Built from the OpenAPI schema, so a new route without protection fails this test."""
    client = TestClient(app)
    spec = app.openapi()["paths"]
    checked = 0
    for path, operations in spec.items():
        for method in operations:
            if (method.upper(), path) in PUBLIC_ROUTES:
                continue
            url = re.sub(r"\{[^}]+\}", "1", path)
            response = client.request(method.upper(), url, json={})
            assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"
            checked += 1
    assert checked >= 17


def test_dashboard_page_redirects_to_sign_in(app):
    client = TestClient(app, follow_redirects=False)

    response = client.get("/?view=rank&skills=Go")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F%3Fview%3Drank%26skills%3DGo"
    assert client.get("/login").status_code == 200


def test_signed_in_users_get_the_dashboard_and_skip_the_sign_in_page(app):
    client = client_as(app, "recruiter")
    client.follow_redirects = False

    assert client.get("/").status_code == 200
    login_page = client.get("/login")
    assert login_page.status_code == 303 and login_page.headers["location"] == "/"


# --- Role matrix --------------------------------------------------------------------

@pytest.mark.parametrize("role", auth.ROLES)
def test_role_matrix(app, role):
    client = client_as(app, role)
    for name, method, path, kwargs, permission in requests_for(app):
        allowed = permission in auth.PERMISSIONS[role]
        before = None if allowed else side_effect_snapshot(app)
        response = client.request(method, path, **kwargs)
        if allowed:
            assert response.status_code not in (401, 403), f"{role} should be allowed to {name}: {response.text}"
        else:
            assert response.status_code == 403, f"{role} must not {name} (got {response.status_code})"
            assert auth.ROLE_LABELS[role] in response.json()["detail"]
            assert side_effect_snapshot(app) == before, f"{role} {name} changed data despite 403"


def test_permission_table_matches_the_role_descriptions():
    p = auth.PERMISSIONS
    assert {"users:manage", "audit:read", "candidates:read"} <= p["admin"]
    assert p["hiring_manager"] == {"candidates:read", "ranking:read", "summaries:read", "activity:read"}
    assert {"tags:write", "shortlist:write", "summaries:generate", "candidates:read"} <= p["recruiter"]
    assert "users:manage" not in p["recruiter"] | p["hiring_manager"]


# --- Direct calls to admin endpoints -----------------------------------------------

@pytest.mark.parametrize("role", ["recruiter", "hiring_manager"])
def test_non_admins_cannot_manage_users_by_calling_the_api(app, role):
    client = client_as(app, role)
    me_id = client.get("/api/auth/me").json()["user"]["id"]
    before = side_effect_snapshot(app)

    attempts = [
        client.get("/api/admin/users"),
        client.post("/api/admin/users", json={"email": "evil@example.test", "role": "admin", "password": "x" * 20}),
        client.patch(f"/api/admin/users/{me_id}", json={"role": "admin"}),  # self-promotion
        client.post(f"/api/admin/users/{me_id}/password", json={"password": "y" * 20}),
        client.get("/api/admin/audit-logs"),
    ]

    assert [r.status_code for r in attempts] == [403] * 5
    assert side_effect_snapshot(app) == before
    assert client.get("/api/auth/me").json()["user"]["role"] == role


def test_hiring_manager_can_read_but_not_generate_summaries(app):
    recruiter, manager = client_as(app, "recruiter"), client_as(app, "hiring_manager")

    assert manager.get("/api/candidates/cand_009/summary").status_code == 404  # nothing cached yet
    assert manager.post("/api/candidates/cand_009/summary").status_code == 403
    assert app.state.ai_client.calls == []

    generated = recruiter.post("/api/candidates/cand_009/summary").json()
    cached = manager.get("/api/candidates/cand_009/summary")

    assert cached.status_code == 200 and cached.json() == {**generated, "cached": True}
    assert len(app.state.ai_client.calls) == 1


def test_role_change_applies_to_open_sessions_immediately(app):
    client = client_as(app, "recruiter")
    admin = client_as(app, "admin")
    me_id = client.get("/api/auth/me").json()["user"]["id"]
    assert client.post("/api/candidates/cand_001/tags", json={"tag": "before"}).status_code == 201

    admin.patch(f"/api/admin/users/{me_id}", json={"role": "hiring_manager"})

    assert client.post("/api/candidates/cand_001/tags", json={"tag": "after"}).status_code == 403
    assert client.get("/api/activity").status_code == 200


def test_permission_errors_do_not_leak_details(app):
    response = client_as(app, "recruiter").get("/api/admin/users")

    assert response.json() == {"detail": "Your role (Recruiter) doesn't allow this action."}
