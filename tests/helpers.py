"""Signed-in test clients.

sign_in() creates a real account and a real session with the same functions the
login route uses after its password check, then gives the client the session
cookie and CSRF header. Every request therefore still goes through session,
CSRF and permission checks; only the Argon2 password check is skipped (it is
covered in test_auth.py) to keep the suite fast.
"""

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app import auth
from app.db import connect
from app.main import create_app

TEST_PASSWORD = "correct horse battery staple"
_password_hash = None


def password_hash():
    global _password_hash
    if _password_hash is None:
        _password_hash = auth.hash_password(TEST_PASSWORD)
    return _password_hash


def make_user(db_path, role="recruiter", email=None, active=True):
    email = email or f"{role.replace('_', '-')}@example.test"
    conn = connect(db_path)
    try:
        row = auth.get_user_by_email(conn, email)
        user_id = row["id"] if row else auth.create_user(conn, email, role, password_hash=password_hash())
        if not active:
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    finally:
        conn.close()
    return user_id, email


def sign_in(client, role="recruiter", email=None):
    """Give `client` a session for a user with `role`. Returns (user id, session token)."""
    user_id, _ = make_user(client.app.state.db_path, role, email)
    conn = connect(client.app.state.db_path)
    try:
        token = auth.create_session(conn, user_id, client.app.state.auth_settings)
    finally:
        conn.close()
    client.cookies.set(auth.COOKIE_NAME, token)
    client.headers[auth.CSRF_HEADER] = auth.csrf_token(client.app.state.secret_key, token)
    return user_id, token


@contextmanager
def signed_in_client(db_path, role="recruiter", **app_kwargs):
    with TestClient(create_app(db_path, **app_kwargs)) as client:
        sign_in(client, role)
        yield client
