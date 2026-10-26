"""User administration, the audit log and recruitment activity."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.importer import import_records
from app.main import create_app
from tests.helpers import TEST_PASSWORD, make_user, sign_in

NEW_PASSWORD = "a perfectly fine password"


@pytest.fixture
def app(conn, db_path, dataset):
    import_records(conn, dataset)
    application = create_app(db_path)
    with TestClient(application):
        pass
    return application


@pytest.fixture
def admin(app):
    client = TestClient(app)
    sign_in(client, "admin")
    return client


def create(admin, email="new@example.test", role="recruiter", password=NEW_PASSWORD):
    return admin.post("/api/admin/users", json={"email": email, "role": role, "password": password})


def audit_rows(conn, action=None):
    sql = "SELECT action, actor_email, target_type, target_id, details FROM audit_logs"
    return conn.execute(sql + (" WHERE action = ?" if action else "") + " ORDER BY id", (action,) if action else ()).fetchall()


def login(app, email, password):
    return TestClient(app).post("/api/auth/login", json={"email": email, "password": password})


# --- Creating and listing users -------------------------------------------------------

def test_admin_creates_a_user_who_can_then_sign_in(app, admin, conn):
    response = create(admin, email="  New@Example.test ", role="hiring_manager")

    assert response.status_code == 201
    user = response.json()
    assert user["email"] == "new@example.test" and user["role"] == "hiring_manager" and user["is_active"] is True
    assert set(user) == {"id", "email", "role", "is_active", "created_at", "updated_at", "last_login_at"}
    assert login(app, "new@example.test", NEW_PASSWORD).status_code == 200
    [(action, actor, target_type, target_id, details)] = audit_rows(conn, "user_created")
    assert actor == "admin@example.test" and target_id == str(user["id"])
    assert NEW_PASSWORD not in details


@pytest.mark.parametrize("body, status", [
    ({"email": "not-an-email", "role": "recruiter", "password": NEW_PASSWORD}, 422),
    ({"email": "a@example.test", "role": "superuser", "password": NEW_PASSWORD}, 422),
    ({"email": "a@example.test", "role": "recruiter", "password": "too short"}, 422),
    ({"email": "a@example.test", "role": "recruiter", "password": "a@example.test"}, 422),
    ({"email": "a@example.test", "role": "recruiter"}, 422),
    ({"email": "a@example.test", "role": "recruiter", "password": NEW_PASSWORD, "is_active": False}, 422),
])
def test_invalid_new_users_are_rejected(admin, conn, body, status):
    assert admin.post("/api/admin/users", json=body).status_code == status
    assert conn.execute("SELECT COUNT(*) FROM users WHERE email = 'a@example.test'").fetchone()[0] == 0


def test_duplicate_email_is_rejected(admin):
    create(admin)

    response = create(admin, email="NEW@example.test")

    assert response.status_code == 409 and "already exists" in response.json()["detail"]


def test_user_list_never_includes_secrets(admin):
    create(admin)

    users = admin.get("/api/admin/users").json()

    assert [u["email"] for u in users] == ["admin@example.test", "new@example.test"]
    assert "password" not in str(users) and "$argon2" not in str(users) and "token" not in str(users)


# --- Changing roles and activation ------------------------------------------------------

def test_change_role_is_audited(admin, conn):
    user_id = create(admin).json()["id"]

    response = admin.patch(f"/api/admin/users/{user_id}", json={"role": "hiring_manager"})

    assert response.status_code == 200 and response.json()["role"] == "hiring_manager"
    [(_, _, _, _, details)] = audit_rows(conn, "user_role_changed")
    assert '"from": "recruiter"' in details and '"to": "hiring_manager"' in details


def test_deactivation_signs_the_user_out_and_blocks_sign_in(app, admin, conn):
    user_id = create(admin).json()["id"]
    victim = TestClient(app)
    victim.post("/api/auth/login", json={"email": "new@example.test", "password": NEW_PASSWORD})
    assert victim.get("/api/auth/me").status_code == 200

    response = admin.patch(f"/api/admin/users/{user_id}", json={"is_active": False})

    assert response.status_code == 200 and response.json()["is_active"] is False
    assert victim.get("/api/auth/me").status_code == 401
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0] == 0
    assert login(app, "new@example.test", NEW_PASSWORD).status_code == 403
    assert len(audit_rows(conn, "user_deactivated")) == 1


def test_reactivation_allows_sign_in_again(app, admin, conn):
    user_id = create(admin).json()["id"]
    admin.patch(f"/api/admin/users/{user_id}", json={"is_active": False})

    admin.patch(f"/api/admin/users/{user_id}", json={"is_active": True})

    assert login(app, "new@example.test", NEW_PASSWORD).status_code == 200
    assert len(audit_rows(conn, "user_activated")) == 1


def test_no_op_update_changes_nothing(admin, conn):
    user_id = create(admin).json()["id"]

    admin.patch(f"/api/admin/users/{user_id}", json={"role": "recruiter", "is_active": True})
    admin.patch(f"/api/admin/users/{user_id}", json={})

    assert audit_rows(conn, "user_role_changed") == [] and audit_rows(conn, "user_activated") == []


@pytest.mark.parametrize("body", [{"role": "owner"}, {"is_active": "no"}, {"is_active": 0}, {"email": "x@y.z"}])
def test_invalid_updates_are_rejected(admin, body):
    user_id = create(admin).json()["id"]

    assert admin.patch(f"/api/admin/users/{user_id}", json=body).status_code == 422


def test_unknown_user_returns_404(admin):
    assert admin.patch("/api/admin/users/9999", json={"role": "recruiter"}).status_code == 404
    assert admin.post("/api/admin/users/9999/password", json={"password": NEW_PASSWORD}).status_code == 404
    assert admin.patch("/api/admin/users/not-a-number", json={}).status_code == 422


@pytest.mark.parametrize("body", [{"role": "recruiter"}, {"is_active": False}])
def test_the_last_active_admin_cannot_be_removed(admin, conn, body):
    me = admin.get("/api/auth/me").json()["user"]["id"]

    response = admin.patch(f"/api/admin/users/{me}", json=body)

    assert response.status_code == 409 and "administrator must remain" in response.json()["detail"]
    assert auth.get_user(conn, me)["role"] == "admin" and auth.get_user(conn, me)["is_active"] == 1


def test_an_admin_can_step_down_when_another_admin_exists(admin, conn):
    create(admin, email="second@example.test", role="admin")
    me = admin.get("/api/auth/me").json()["user"]["id"]

    assert admin.patch(f"/api/admin/users/{me}", json={"role": "recruiter"}).status_code == 200
    assert admin.get("/api/admin/users").status_code == 403  # takes effect at once


# --- Password reset -------------------------------------------------------------------------

def test_reset_password_signs_the_user_out(app, admin, conn):
    user_id, email = make_user(app.state.db_path, "recruiter", "reset@example.test")
    user_client = TestClient(app)
    sign_in(user_client, email="reset@example.test")

    response = admin.post(f"/api/admin/users/{user_id}/password", json={"password": NEW_PASSWORD})

    assert response.status_code == 204
    assert user_client.get("/api/auth/me").status_code == 401
    assert login(app, email, TEST_PASSWORD).status_code == 401
    assert login(app, email, NEW_PASSWORD).status_code == 200
    [(_, _, _, _, details)] = audit_rows(conn, "password_reset")
    assert NEW_PASSWORD not in details


def test_reset_password_enforces_the_policy(app, admin):
    user_id, _ = make_user(app.state.db_path, "recruiter", "reset@example.test")

    assert admin.post(f"/api/admin/users/{user_id}/password", json={"password": "short"}).status_code == 422
    assert admin.post(f"/api/admin/users/{user_id}/password", json={"password": "reset@example.test"}).status_code == 422


# --- Audit log and activity -------------------------------------------------------------------

@pytest.fixture
def recruiter(app):
    client = TestClient(app)
    sign_in(client, "recruiter")
    return client


def test_recruiter_actions_are_audited_once(recruiter, conn):
    recruiter.post("/api/candidates/cand_001/tags", json={"tag": "Strong Backend"})
    recruiter.post("/api/candidates/cand_001/tags", json={"tag": "strong backend"})  # duplicate: not logged
    recruiter.delete("/api/candidates/cand_001/tags/strong backend")
    recruiter.delete("/api/candidates/cand_001/tags/strong backend")  # nothing removed: not logged
    recruiter.put("/api/candidates/cand_001/shortlist", json={"shortlisted": True})
    recruiter.put("/api/candidates/cand_001/shortlist", json={"shortlisted": True})  # no change: not logged
    recruiter.put("/api/candidates/cand_001/shortlist", json={"shortlisted": False})

    actions = [(a, actor, t, tid) for a, actor, t, tid, _ in audit_rows(conn) if t == "candidate"]
    assert actions == [
        ("tag_added", "recruiter@example.test", "candidate", "cand_001"),
        ("tag_removed", "recruiter@example.test", "candidate", "cand_001"),
        ("shortlist_added", "recruiter@example.test", "candidate", "cand_001"),
        ("shortlist_removed", "recruiter@example.test", "candidate", "cand_001"),
    ]


def test_failed_action_leaves_no_audit_entry(recruiter, conn):
    recruiter.post("/api/candidates/cand_999/tags", json={"tag": "x"})
    recruiter.post("/api/candidates/cand_001/tags", json={"tag": "bad/tag"})

    assert [r for r in audit_rows(conn) if r[2] == "candidate"] == []


def test_hiring_manager_sees_recruitment_activity_without_ips(app, recruiter):
    recruiter.post("/api/candidates/cand_009/tags", json={"tag": "follow-up"})
    recruiter.put("/api/candidates/cand_009/shortlist", json={"shortlisted": True})
    manager = TestClient(app)
    sign_in(manager, "hiring_manager")

    page = manager.get("/api/activity").json()

    assert page["total"] == 2
    latest = page["items"][0]
    assert latest["action"] == "shortlist_added" and latest["candidate_name"] == "Siddharth Mehta"
    assert latest["actor_email"] == "recruiter@example.test"
    assert all(item.get("ip") is None for item in page["items"])
    assert page["items"][1]["details"] == {"tag": "follow-up"}


def test_activity_excludes_account_events(app, admin, recruiter):
    create(admin)  # user_created, plus sign-ins: not recruitment activity
    recruiter.put("/api/candidates/cand_001/shortlist", json={"shortlisted": True})

    actions = [i["action"] for i in admin.get("/api/activity").json()["items"]]

    assert actions == ["shortlist_added"]


def test_admin_audit_log_lists_everything_newest_first(admin, recruiter):
    create(admin)
    recruiter.post("/api/candidates/cand_001/tags", json={"tag": "x"})

    page = admin.get("/api/admin/audit-logs").json()
    actions = [i["action"] for i in page["items"]]

    assert actions[:2] == ["tag_added", "user_created"]
    assert admin.get("/api/admin/audit-logs", params={"action": "user_created"}).json()["total"] == 1
    assert admin.get("/api/admin/audit-logs", params={"limit": 1, "offset": 1}).json()["items"][0]["action"] == "user_created"
    assert admin.get("/api/admin/audit-logs", params={"action": "DROP TABLE"}).status_code == 422


def test_audit_log_is_append_only(admin, conn):
    create(admin)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_logs SET action = 'nothing'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_logs")
