"""The account command line: creating the first administrator and managing users."""

import io

import pytest

from app import auth, manage
from app.db import connect

PASSWORD = "first admin passphrase"


def run(db_path, monkeypatch, *args, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return manage.main(["--db", str(db_path), *args])


def users(db_path):
    conn = connect(db_path)
    try:
        return {r["email"]: dict(r) for r in conn.execute("SELECT email, role, is_active, password_hash FROM users")}
    finally:
        conn.close()


def test_create_first_admin(db_path, monkeypatch, capsys):
    code = run(db_path, monkeypatch, "create-admin", "--email", "Boss@Example.test", "--password-stdin", stdin=PASSWORD + "\n")

    assert code == 0
    account = users(db_path)["boss@example.test"]
    assert account["role"] == "admin" and account["is_active"] == 1
    assert account["password_hash"].startswith("$argon2id$") and PASSWORD not in account["password_hash"]
    assert PASSWORD not in capsys.readouterr().out


def test_password_is_prompted_without_echo(db_path, monkeypatch):
    prompts = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt: next(prompts))

    assert manage.main(["--db", str(db_path), "create-admin", "--email", "boss@example.test"]) == 0
    assert "boss@example.test" in users(db_path)


def test_mismatched_prompted_passwords_are_refused(db_path, monkeypatch):
    prompts = iter([PASSWORD, PASSWORD + "x"])
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt: next(prompts))

    with pytest.raises(SystemExit, match="don't match"):
        manage.main(["--db", str(db_path), "create-admin", "--email", "boss@example.test"])
    assert users(db_path) == {}


def test_password_cannot_be_given_as_an_argument(db_path):
    with pytest.raises(SystemExit):
        manage.main(["--db", str(db_path), "create-admin", "--email", "boss@example.test", "--password", PASSWORD])


@pytest.mark.parametrize("password", ["short", "", "boss@example.test"])
def test_weak_passwords_are_refused(db_path, monkeypatch, capsys, password):
    assert run(db_path, monkeypatch, "create-admin", "--email", "boss@example.test", "--password-stdin", stdin=password) == 2
    assert users(db_path) == {}
    assert "Error:" in capsys.readouterr().err


def test_duplicate_account_is_refused(db_path, monkeypatch):
    run(db_path, monkeypatch, "create-admin", "--email", "boss@example.test", "--password-stdin", stdin=PASSWORD)

    assert run(db_path, monkeypatch, "create-user", "--email", "boss@example.test", "--role", "recruiter",
               "--password-stdin", stdin=PASSWORD) == 2


def test_list_users_shows_no_secrets(db_path, monkeypatch, capsys):
    run(db_path, monkeypatch, "create-admin", "--email", "boss@example.test", "--password-stdin", stdin=PASSWORD)
    capsys.readouterr()

    run(db_path, monkeypatch, "list-users")

    out = capsys.readouterr().out
    assert "boss@example.test" in out and "admin" in out and "$argon2" not in out and PASSWORD not in out


def test_last_admin_cannot_be_deactivated_or_demoted(db_path, monkeypatch):
    run(db_path, monkeypatch, "create-admin", "--email", "boss@example.test", "--password-stdin", stdin=PASSWORD)

    with pytest.raises(SystemExit, match="administrator must remain"):
        run(db_path, monkeypatch, "deactivate", "--email", "boss@example.test")
    with pytest.raises(SystemExit, match="administrator must remain"):
        run(db_path, monkeypatch, "set-role", "--email", "boss@example.test", "--role", "recruiter")
    assert users(db_path)["boss@example.test"]["role"] == "admin"


def test_deactivate_signs_out_and_reset_password_works(db_path, monkeypatch):
    run(db_path, monkeypatch, "create-user", "--email", "r@example.test", "--role", "recruiter", "--password-stdin", stdin=PASSWORD)
    conn = connect(db_path)
    user_id = auth.get_user_by_email(conn, "r@example.test")["id"]
    auth.create_session(conn, user_id, auth.AuthSettings())

    run(db_path, monkeypatch, "deactivate", "--email", "r@example.test")
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    run(db_path, monkeypatch, "activate", "--email", "r@example.test")
    run(db_path, monkeypatch, "reset-password", "--email", "r@example.test", "--password-stdin", stdin="a different passphrase")

    row = conn.execute("SELECT password_hash, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    assert auth.verify_password(row["password_hash"], "a different passphrase") and row["is_active"] == 1
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_logs ORDER BY id")]
    assert actions == ["user_created", "user_deactivated", "user_activated", "password_reset"]
    conn.close()


def test_create_demo_accounts_can_sign_in_and_rerun_restores_them(db_path, monkeypatch, capsys):
    assert run(db_path, monkeypatch, "create-demo") == 0
    accounts = users(db_path)
    for demo in auth.DEMO_ACCOUNTS:
        assert accounts[demo["email"]]["role"] == demo["role"]
        assert auth.verify_password(accounts[demo["email"]]["password_hash"], demo["password"])
    assert "admin@gmail.com" in capsys.readouterr().out

    assert run(db_path, monkeypatch, "set-role", "--email", "recruiter@gmail.com", "--role", "admin") == 0
    assert run(db_path, monkeypatch, "create-demo") == 0
    accounts = users(db_path)
    assert len(accounts) == len(auth.DEMO_ACCOUNTS)
    assert accounts["recruiter@gmail.com"]["role"] == "recruiter"
    assert all(a["is_active"] == 1 for a in accounts.values())
