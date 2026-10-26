"""Account administration from the command line.

There is no default account and no self-registration. The first administrator is
created here, by someone with access to the server and its database:

    python -m app.manage create-admin --email admin@example.com

Passwords are prompted for (not echoed), or read from standard input with
--password-stdin for scripts. They are never accepted as command-line arguments,
which would leave them in shell history and process listings.

Other commands: create-user, create-demo, list-users, set-role, deactivate, activate, reset-password.
"""

import argparse
import getpass
import sys
from pathlib import Path

from app import auth
from app.db import DEFAULT_DB_PATH, connect, init_schema, transaction


def read_password(args, email):
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"Password for {email}: ")
        if getpass.getpass("Repeat password: ") != password:
            raise SystemExit("Passwords don't match.")
    auth.check_password_policy(password, email)
    return password


def find_user(conn, email):
    row = auth.get_user_by_email(conn, auth.normalize_email(email))
    if row is None:
        raise SystemExit(f"No account for {email}.")
    return row


def cmd_create(conn, args, role):
    email = auth.normalize_email(args.email)
    password = read_password(args, email)
    with transaction(conn):
        user_id = auth.create_user(conn, email, role, password=password)
        auth.audit(conn, "user_created", actor_email="cli", target_type="user", target_id=user_id,
                   details={"email": email, "role": role, "via": "command line"})
    print(f"Created {auth.ROLE_LABELS[role].lower()} {email} (id {user_id}).")


def cmd_create_demo(conn, args):
    """Create (or restore) the well-known demo accounts. Re-running resets their passwords and roles."""
    with transaction(conn):
        for account in auth.DEMO_ACCOUNTS:
            row = auth.get_user_by_email(conn, account["email"])
            if row is None:
                # Demo passwords are deliberately short, so they skip the length policy by passing a hash.
                user_id = auth.create_user(conn, account["email"], account["role"],
                                           password_hash=auth.hash_password(account["password"]))
                action = "user_created"
            else:
                user_id = row["id"]
                conn.execute("UPDATE users SET password_hash = ?, role = ?, is_active = 1, updated_at = ? WHERE id = ?",
                             (auth.hash_password(account["password"]), account["role"], auth.iso(auth.utc_now()), user_id))
                auth.revoke_user_sessions(conn, user_id)
                action = "password_reset"
            auth.audit(conn, action, actor_email="cli", target_type="user", target_id=user_id,
                       details={"email": account["email"], "role": account["role"], "via": "command line (demo)"})
    print("Demo accounts (for local use only; anyone who reads this code knows these passwords):")
    for account in auth.DEMO_ACCOUNTS:
        print(f"  {auth.ROLE_LABELS[account['role']]:<15} {account['email']:<24} {account['password']}")
    print("Set GCCX_DEMO_LOGIN=1 to list them on the sign-in page.")


def cmd_list(conn, args):
    users = auth.list_users(conn)
    if not users:
        print("No accounts yet. Create the first administrator with: python -m app.manage create-admin --email ...")
    for user in users:
        state = "active" if user["is_active"] else "DEACTIVATED"
        print(f"{user['id']:>4}  {user['email']:<40} {user['role']:<15} {state:<12} last login: {user['last_login_at'] or 'never'}")


def _ensure_admin_remains(conn, row, new_role, new_active):
    loses_admin = row["role"] == "admin" and row["is_active"] and (new_role != "admin" or not new_active)
    if loses_admin and auth.count_active_admins(conn, excluding_user_id=row["id"]) == 0:
        raise SystemExit("Refused: at least one active administrator must remain.")


def cmd_set_role(conn, args):
    with transaction(conn):
        row = find_user(conn, args.email)
        _ensure_admin_remains(conn, row, args.role, bool(row["is_active"]))
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (args.role, auth.iso(auth.utc_now()), row["id"]))
        auth.audit(conn, "user_role_changed", actor_email="cli", target_type="user", target_id=row["id"],
                   details={"email": row["email"], "from": row["role"], "to": args.role, "via": "command line"})
    print(f"{row['email']} is now {auth.ROLE_LABELS[args.role].lower()}.")


def cmd_set_active(conn, args, active):
    with transaction(conn):
        row = find_user(conn, args.email)
        _ensure_admin_remains(conn, row, row["role"], active)
        conn.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?", (int(active), auth.iso(auth.utc_now()), row["id"]))
        if not active:
            auth.revoke_user_sessions(conn, row["id"])
        auth.audit(conn, "user_activated" if active else "user_deactivated", actor_email="cli", target_type="user",
                   target_id=row["id"], details={"email": row["email"], "via": "command line"})
    print(f"{row['email']} is now {'active' if active else 'deactivated and signed out'}.")


def cmd_reset_password(conn, args):
    row = find_user(conn, args.email)
    password = read_password(args, row["email"])
    with transaction(conn):
        auth.set_password(conn, row["id"], password, row["email"])
        auth.revoke_user_sessions(conn, row["id"])
        auth.audit(conn, "password_reset", actor_email="cli", target_type="user", target_id=row["id"],
                   details={"email": row["email"], "via": "command line"})
    print(f"Password reset for {row['email']}; their sessions were signed out.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.manage", description="Manage GCCX accounts.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database file")
    commands = parser.add_subparsers(dest="command", required=True)

    def with_email(name, help_text, password=False):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--email", required=True)
        if password:
            sub.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
        return sub

    with_email("create-admin", "create an administrator", password=True)
    create = with_email("create-user", "create an account with any role", password=True)
    create.add_argument("--role", required=True, choices=auth.ROLES)
    commands.add_parser("create-demo", help="create demo admin, recruiter and hiring manager accounts (local use only)")
    commands.add_parser("list-users", help="list accounts")
    set_role = with_email("set-role", "change an account's role")
    set_role.add_argument("--role", required=True, choices=auth.ROLES)
    with_email("deactivate", "deactivate an account and sign it out")
    with_email("activate", "re-activate an account")
    with_email("reset-password", "set a new password and sign the account out", password=True)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        init_schema(conn)
        if args.command == "create-admin":
            cmd_create(conn, args, "admin")
        elif args.command == "create-user":
            cmd_create(conn, args, args.role)
        elif args.command == "create-demo":
            cmd_create_demo(conn, args)
        elif args.command == "list-users":
            cmd_list(conn, args)
        elif args.command == "set-role":
            cmd_set_role(conn, args)
        elif args.command in ("deactivate", "activate"):
            cmd_set_active(conn, args, args.command == "activate")
        else:
            cmd_reset_password(conn, args)
    except (ValueError, auth.AuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
