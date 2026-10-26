"""Login, logout, sessions, expiry, deactivated accounts, throttling and CSRF."""

from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import auth
from app.db import connect
from app.importer import import_records
from app.main import create_app
from tests.helpers import TEST_PASSWORD, make_user, sign_in

EMAIL = "recruiter@example.test"


@pytest.fixture
def client(conn, db_path, dataset):
    import_records(conn, dataset)
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


@pytest.fixture
def user_id(client, db_path):
    return make_user(db_path, "recruiter", EMAIL)[0]


def login(client, email=EMAIL, password=TEST_PASSWORD, **kwargs):
    return client.post("/api/auth/login", json={"email": email, "password": password}, **kwargs)


def session_cookie(response):
    return next(c for c in response.headers.get_list("set-cookie") if c.startswith(f"{auth.COOKIE_NAME}="))


def tag(client, csrf=None, candidate="cand_001", value="x", **kwargs):
    headers = {auth.CSRF_HEADER: csrf} if csrf else {}
    return client.post(f"/api/candidates/{candidate}/tags", json={"tag": value}, headers=headers, **kwargs)


def rows(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


# --- Login ------------------------------------------------------------------------

def test_login_succeeds_and_sets_a_secure_session_cookie(client, user_id, conn):
    response = login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == EMAIL and body["user"]["role"] == "recruiter"
    assert set(body["permissions"]) == auth.PERMISSIONS["recruiter"]
    assert len(body["csrf_token"]) == 64
    assert set(body["user"]) == {"id", "email", "role", "is_active", "created_at", "updated_at", "last_login_at"}
    cookie = session_cookie(response).lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain-HTTP local development in "auto" mode
    token = client.cookies.get(auth.COOKIE_NAME)
    assert len(token) >= 43
    stored = rows(conn, "SELECT token_hash FROM sessions WHERE user_id = ?", user_id)
    assert [r[0] for r in stored] == [auth.hash_token(token)] and token not in stored[0][0]
    assert body["csrf_token"] not in str([tuple(r) for r in rows(conn, "SELECT * FROM sessions")])


def test_cookie_is_secure_over_https_in_auto_mode(conn, db_path, dataset):
    import_records(conn, dataset)
    make_user(db_path, "recruiter", EMAIL)
    with TestClient(create_app(db_path), base_url="https://testserver") as client:
        assert "secure" in session_cookie(login(client)).lower()


@pytest.mark.parametrize("mode, expect_secure", [("always", True), ("never", False)])
def test_cookie_secure_setting_can_be_forced(conn, db_path, mode, expect_secure):
    make_user(db_path, "recruiter", EMAIL)
    with TestClient(create_app(db_path, auth_settings=auth.AuthSettings(cookie_secure=mode))) as client:
        assert ("secure" in session_cookie(login(client)).lower()) is expect_secure


def test_login_is_case_insensitive_on_email_and_records_last_login(client, user_id, conn):
    assert login(client, email="  Recruiter@Example.TEST ").status_code == 200
    assert auth.get_user(conn, user_id)["last_login_at"] is not None


@pytest.mark.parametrize("email, password", [
    (EMAIL, "wrong password entirely"),
    ("nobody@example.test", TEST_PASSWORD),
    ("not-an-email", TEST_PASSWORD),
    (EMAIL, ""),
])
def test_failed_login_is_generic_and_sets_no_cookie(client, user_id, conn, email, password):
    response = login(client, email=email, password=password)

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect email or password."}
    assert auth.COOKIE_NAME not in client.cookies
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 0


@pytest.mark.parametrize("body", [{}, {"email": EMAIL}, {"email": EMAIL, "password": "x", "extra": 1},
                                  {"email": "a" * 300, "password": "x"}, {"email": EMAIL, "password": "x" * 1025}])
def test_malformed_login_bodies_are_rejected(client, body):
    assert client.post("/api/auth/login", json=body).status_code == 422


def test_passwords_are_stored_only_as_argon2id_hashes(client, user_id, conn, db_path):
    login(client)
    login(client, password="a wrong guess here")
    stored = auth.get_user_by_email(conn, EMAIL)
    password_hash = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    assert password_hash.startswith("$argon2id$") and stored is not None
    dump = "\n".join(connect(db_path).iterdump())
    assert TEST_PASSWORD not in dump and "a wrong guess here" not in dump


def test_outdated_hash_is_upgraded_on_login(client, conn):
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(TEST_PASSWORD)
    user_id = auth.create_user(conn, "old@example.test", "recruiter", password_hash=weak)

    assert login(client, email="old@example.test").status_code == 200
    upgraded = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    assert upgraded != weak and "m=65536,t=3,p=4" in upgraded


def test_login_replaces_an_existing_session(client, user_id, conn):
    login(client)
    first = client.cookies.get(auth.COOKIE_NAME)

    login(client)

    assert client.cookies.get(auth.COOKIE_NAME) != first
    assert [r[0] for r in rows(conn, "SELECT token_hash FROM sessions")] == [auth.hash_token(client.cookies.get(auth.COOKIE_NAME))]


def test_login_from_another_site_is_refused(client, user_id):
    assert login(client, headers={"Origin": "https://evil.example"}).status_code == 403
    assert login(client, headers={"Origin": "http://testserver"}).status_code == 200


# --- Current user and logout -------------------------------------------------------

def test_me_returns_the_signed_in_user(client, user_id):
    csrf = login(client).json()["csrf_token"]

    body = client.get("/api/auth/me").json()

    assert body["user"]["email"] == EMAIL and body["csrf_token"] == csrf
    assert "password" not in str(body) and "token_hash" not in str(body)


@pytest.mark.parametrize("cookie", [None, "garbage", "x" * 500])
def test_me_without_a_valid_session_is_401(client, cookie):
    if cookie:
        client.cookies.set(auth.COOKIE_NAME, cookie)

    response = client.get("/api/auth/me")

    assert response.status_code == 401 and response.json() == {"detail": "Not signed in."}


def test_logout_ends_the_session(client, user_id, conn):
    csrf = login(client).json()["csrf_token"]
    token = client.cookies.get(auth.COOKIE_NAME)

    response = client.post("/api/auth/logout", headers={auth.CSRF_HEADER: csrf})

    assert response.status_code == 204
    assert f"{auth.COOKIE_NAME}=\"\"" in session_cookie(response) or "max-age=0" in session_cookie(response).lower()
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 0
    client.cookies.set(auth.COOKIE_NAME, token)  # replaying the old cookie doesn't work
    assert client.get("/api/auth/me").status_code == 401
    assert rows(conn, "SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1")[0][0] == "logout"


def test_logout_needs_the_csrf_token(client, user_id, conn):
    login(client)

    assert client.post("/api/auth/logout").status_code == 403
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 1


def test_logout_without_a_session_is_harmless(client):
    assert client.post("/api/auth/logout").status_code == 204


# --- Expiry and revocation ----------------------------------------------------------

def test_idle_session_expires(client, user_id, conn):
    sign_in(client, email=EMAIL)
    old = auth.iso(auth.utc_now() - timedelta(minutes=client.app.state.auth_settings.idle_minutes + 1))
    conn.execute("UPDATE sessions SET last_seen_at = ?", (old,))

    response = client.get("/api/candidates")

    assert response.status_code == 401
    assert response.json() == {"detail": "Your session has expired. Sign in again."}
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 0


def test_session_has_an_absolute_lifetime(client, user_id, conn):
    sign_in(client, email=EMAIL)
    conn.execute("UPDATE sessions SET expires_at = ?", (auth.iso(auth.utc_now() - timedelta(seconds=1)),))

    assert client.get("/api/candidates").status_code == 401


def test_activity_keeps_a_session_alive(client, user_id, conn):
    sign_in(client, email=EMAIL)
    earlier = auth.iso(auth.utc_now() - timedelta(minutes=5))
    conn.execute("UPDATE sessions SET last_seen_at = ?", (earlier,))

    client.get("/api/facets")

    assert rows(conn, "SELECT last_seen_at FROM sessions")[0][0] > earlier


def test_deactivated_user_cannot_log_in(client, conn):
    make_user(client.app.state.db_path, "recruiter", "gone@example.test", active=False)

    response = login(client, email="gone@example.test")

    assert response.status_code == 403
    assert "deactivated" in response.json()["detail"]
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 0


def test_deactivated_user_cannot_keep_using_an_open_session(client, user_id, conn):
    sign_in(client, email=EMAIL)
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))

    response = client.get("/api/candidates")

    assert response.status_code == 401 and "deactivated" in response.json()["detail"]
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 0


def test_wrong_password_for_a_deactivated_account_reveals_nothing(client, conn):
    make_user(client.app.state.db_path, "recruiter", "gone@example.test", active=False)

    assert login(client, email="gone@example.test", password="definitely wrong").status_code == 401


# --- Throttling ---------------------------------------------------------------------

def test_repeated_failures_lock_the_email_temporarily(client, user_id, conn):
    for _ in range(5):
        assert login(client, password="wrong password here").status_code == 401

    response = login(client)  # even the right password is refused while locked

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 15 * 60 + 1
    assert rows(conn, "SELECT COUNT(*) FROM sessions")[0][0] == 0
    assert rows(conn, "SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1")[0][0] == "login_throttled"


def test_lock_lifts_after_the_window(client, user_id, conn):
    for _ in range(5):
        login(client, password="wrong password here")
    conn.execute("UPDATE login_attempts SET attempted_at = ?", (auth.iso(auth.utc_now() - timedelta(minutes=16)),))

    assert login(client).status_code == 200


def test_success_resets_the_failure_count(client, user_id):
    for _ in range(4):
        login(client, password="wrong password here")
    assert login(client).status_code == 200
    for _ in range(4):
        assert login(client, password="wrong password here").status_code == 401

    assert login(client).status_code == 200


def test_many_failures_from_one_ip_are_throttled_across_emails(conn, db_path):
    settings = auth.AuthSettings(max_failures_per_ip=6)
    make_user(db_path, "recruiter", EMAIL)
    with TestClient(create_app(db_path, auth_settings=settings)) as client:
        for n in range(6):
            login(client, email=f"guess{n}@example.test", password="whatever whatever")

        assert login(client).status_code == 429


def test_failed_logins_are_audited_without_the_password(client, user_id, conn):
    login(client, password="my secret guess 123")

    action, details = rows(conn, "SELECT action, details FROM audit_logs")[-1]
    assert action == "login_failed" and "wrong_password" in details and "my secret guess" not in details


# --- CSRF ---------------------------------------------------------------------------

def test_state_changes_need_the_csrf_token(client, user_id, conn):
    csrf = login(client).json()["csrf_token"]

    missing = tag(client)
    wrong = tag(client, csrf="0" * 64)
    ok = tag(client, csrf=csrf)

    assert missing.status_code == 403 and missing.headers["X-Error-Code"] == "csrf_failed"
    assert wrong.status_code == 403
    assert ok.status_code == 201
    assert rows(conn, "SELECT COUNT(*) FROM candidate_tags")[0][0] == 1


def test_csrf_token_from_another_session_is_rejected(client, user_id, db_path):
    other = TestClient(client.app)
    make_user(db_path, "recruiter", "other@example.test")
    other_csrf = login(other, email="other@example.test").json()["csrf_token"]
    login(client)

    assert tag(client, csrf=other_csrf).status_code == 403


def test_reads_do_not_need_the_csrf_token(client, user_id):
    login(client)

    assert client.get("/api/candidates").status_code == 200


def test_cross_site_origin_is_refused_even_with_a_valid_token(client, user_id, conn):
    csrf = login(client).json()["csrf_token"]

    response = client.post("/api/candidates/cand_001/tags", json={"tag": "x"},
                           headers={auth.CSRF_HEADER: csrf, "Origin": "https://evil.example"})

    assert response.status_code == 403 and response.json() == {"detail": "Cross-site request refused."}
    assert rows(conn, "SELECT COUNT(*) FROM candidate_tags")[0][0] == 0


def test_csrf_tokens_differ_per_session(client, user_id):
    first = login(client).json()["csrf_token"]
    second = login(client).json()["csrf_token"]

    assert first != second


# --- Own password -------------------------------------------------------------------

def change(client, csrf, current, new):
    return client.post("/api/auth/password", json={"current_password": current, "new_password": new},
                       headers={auth.CSRF_HEADER: csrf})


def test_change_password(client, user_id, conn, db_path):
    other = TestClient(client.app)
    login(other)
    csrf = login(client).json()["csrf_token"]

    assert change(client, csrf, TEST_PASSWORD, "a brand new passphrase").status_code == 204

    assert client.get("/api/auth/me").status_code == 200  # this session stays
    assert other.get("/api/auth/me").status_code == 401  # other sessions are signed out
    fresh = TestClient(client.app)
    assert login(fresh, password=TEST_PASSWORD).status_code == 401
    assert login(fresh, password="a brand new passphrase").status_code == 200


@pytest.mark.parametrize("current, new, status", [
    ("not my password", "a brand new passphrase", 400),
    (TEST_PASSWORD, "short", 422),
    (TEST_PASSWORD, TEST_PASSWORD, 422),
    (TEST_PASSWORD, " " * 20, 422),
    (TEST_PASSWORD, EMAIL, 422),
])
def test_change_password_errors(client, user_id, current, new, status):
    csrf = login(client).json()["csrf_token"]

    assert change(client, csrf, current, new).status_code == status
    assert login(TestClient(client.app)).status_code == 200  # old password still works
