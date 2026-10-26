"""Accounts, sessions, CSRF tokens, login throttling, role permissions and the audit log.

No FastAPI here: app/security.py adapts this to HTTP, and app/manage.py uses it
from the command line.

- Passwords: Argon2id (argon2-cffi defaults: 64 MiB, 3 passes, 4 lanes).
- Sessions: a random 256-bit token in an HttpOnly cookie; the database stores
  only its SHA-256. Sessions end after an idle timeout or an absolute lifetime,
  when the user logs out, is deactivated, or has their password reset.
- CSRF: HMAC(secret key, session token), sent back in the X-CSRF-Token header.
- Throttling: failed logins are counted per email and per IP in a time window.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ROLES = ("admin", "hiring_manager", "recruiter")
ROLE_LABELS = {"admin": "Admin", "hiring_manager": "Hiring manager", "recruiter": "Recruiter"}

# Well-known accounts for local demos, created only by `python -m app.manage create-demo`.
# The sign-in page lists them only when GCCX_DEMO_LOGIN is on. Never use them on a shared server.
DEMO_ACCOUNTS = (
    {"role": "admin", "email": "admin@gmail.com", "password": "DEMO@123"},
    {"role": "recruiter", "email": "recruiter@gmail.com", "password": "DEMO@123"},
    {"role": "hiring_manager", "email": "manager@gmail.com", "password": "DEMO@123"},
)


def demo_login_enabled():
    return (os.environ.get("GCCX_DEMO_LOGIN") or "").strip().lower() in ("1", "true", "yes", "on")

# The single source of truth for what each role may do. Every protected route
# names one of these permissions; the frontend only uses them to hide controls.
PERMISSIONS = {
    "admin": frozenset({
        "candidates:read", "ranking:read", "summaries:read", "activity:read", "audit:read", "users:manage",
    }),
    "hiring_manager": frozenset({"candidates:read", "ranking:read", "summaries:read", "activity:read"}),
    "recruiter": frozenset({
        "candidates:read", "ranking:read", "summaries:read", "summaries:generate", "tags:write", "shortlist:write",
    }),
}

COOKIE_NAME = "gccx_session"
CSRF_HEADER = "X-CSRF-Token"
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_hasher = PasswordHasher()
_dummy_hash = None


class AuthError(Exception):
    """A refused login or account operation. `detail` is safe to show to the user."""

    def __init__(self, status_code, detail, retry_after=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


@dataclass(frozen=True)
class AuthSettings:
    cookie_secure: str = "auto"  # "auto" (Secure when the request is HTTPS), "always" or "never"
    idle_minutes: int = 120
    max_hours: int = 12
    window_minutes: int = 15
    max_failures_per_email: int = 5
    max_failures_per_ip: int = 30

    @classmethod
    def from_env(cls):
        def number(name, default):
            try:
                value = int(os.environ.get(name) or default)
            except ValueError:
                return default
            return value if value > 0 else default

        secure = (os.environ.get("GCCX_COOKIE_SECURE") or "auto").strip().lower()
        return cls(
            cookie_secure=secure if secure in ("auto", "always", "never") else "auto",
            idle_minutes=number("GCCX_SESSION_IDLE_MINUTES", cls.idle_minutes),
            max_hours=number("GCCX_SESSION_MAX_HOURS", cls.max_hours),
            window_minutes=number("GCCX_LOGIN_WINDOW_MINUTES", cls.window_minutes),
            max_failures_per_email=number("GCCX_LOGIN_MAX_FAILURES", cls.max_failures_per_email),
            max_failures_per_ip=number("GCCX_LOGIN_MAX_FAILURES_PER_IP", cls.max_failures_per_ip),
        )


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    role: str
    token_hash: str

    @property
    def permissions(self):
        return PERMISSIONS[self.role]

    def can(self, permission):
        return permission in self.permissions


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.isoformat()


# --- Passwords and emails ------------------------------------------------------

def normalize_email(email):
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise ValueError("Enter a valid email address.")
    return email


def check_password_policy(password, email=None):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
    if not password.strip():
        raise ValueError("Password can't be only spaces.")
    if email and password.strip().lower() == email.lower():
        raise ValueError("Password can't be the email address.")


def hash_password(password):
    return _hasher.hash(password)


def verify_password(password_hash, password):
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _equalize_timing(password):
    """Spend the same time on unknown emails as on real ones, so timing doesn't reveal accounts."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(_dummy_hash, password)


# --- Users -----------------------------------------------------------------------

USER_COLUMNS = "id, email, role, is_active, created_at, updated_at, last_login_at"


def public_user(row):
    """Only these fields ever leave the server; never the password hash."""
    user = {key: row[key] for key in ("id", "email", "role", "created_at", "updated_at", "last_login_at")}
    user["is_active"] = bool(row["is_active"])
    return user


def create_user(conn, email, role, *, password=None, password_hash=None):
    """Create an account and return its id. Pass a password (checked and hashed) or an existing hash."""
    email = normalize_email(email)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    if password_hash is None:
        check_password_policy(password, email)
        password_hash = hash_password(password)
    now = iso(utc_now())
    try:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, role, is_active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (email, password_hash, role, now, now),
        )
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise AuthError(409, "An account with this email already exists.") from exc
        raise
    return cursor.lastrowid


def get_user(conn, user_id):
    return conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(conn, email):
    return conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE email = ?", (email,)).fetchone()


def list_users(conn):
    return [public_user(row) for row in conn.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY email")]


def count_active_admins(conn, excluding_user_id=None):
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1 AND id IS NOT ?", (excluding_user_id,)
    ).fetchone()[0]


def set_password(conn, user_id, password, email):
    check_password_policy(password, email)
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(password), iso(utc_now()), user_id),
    )


# --- Sessions --------------------------------------------------------------------

def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id, settings, *, ip=None, user_agent=None):
    """Store a new session and return its token (only ever sent to the browser as a cookie)."""
    token = secrets.token_urlsafe(32)
    now = utc_now()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hash_token(token), user_id, iso(now), iso(now), iso(now + timedelta(hours=settings.max_hours)),
         ip, (user_agent or "")[:300] or None),
    )
    return token


def resolve_session(conn, token, settings):
    """Return (CurrentUser, "ok"), or (None, reason) with reason "missing", "expired" or "inactive".

    Expired sessions and sessions of deactivated users are deleted on sight.
    """
    if not token or len(token) > 100:
        return None, "missing"
    token_hash = hash_token(token)
    row = conn.execute(
        "SELECT s.user_id, s.last_seen_at, s.expires_at, u.email, u.role, u.is_active "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None, "missing"
    now = utc_now()
    idle_limit = datetime.fromisoformat(row["last_seen_at"]) + timedelta(minutes=settings.idle_minutes)
    if now >= datetime.fromisoformat(row["expires_at"]) or now >= idle_limit:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None, "expired"
    if not row["is_active"]:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None, "inactive"
    if now - datetime.fromisoformat(row["last_seen_at"]) >= timedelta(seconds=60):
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (iso(now), token_hash))
    return CurrentUser(id=row["user_id"], email=row["email"], role=row["role"], token_hash=token_hash), "ok"


def session_expiry(conn, token_hash):
    return conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()[0]


def revoke_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def revoke_user_sessions(conn, user_id, *, keep_token_hash=None):
    return conn.execute(
        "DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?", (user_id, keep_token_hash)
    ).rowcount


def csrf_token(secret_key, session_token):
    return hmac.new(secret_key, session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_valid(secret_key, session_token, submitted):
    return bool(submitted) and hmac.compare_digest(csrf_token(secret_key, session_token), submitted)


# --- Login and throttling ----------------------------------------------------------

def _retry_after(conn, column, value, since, limit, window):
    rows = conn.execute(
        f"SELECT attempted_at FROM login_attempts WHERE {column} = ? AND succeeded = 0 AND attempted_at > ? "
        "ORDER BY attempted_at DESC LIMIT ?",
        (value, iso(since), limit),
    ).fetchall()
    if len(rows) < limit:
        return 0
    oldest_counted = datetime.fromisoformat(rows[-1]["attempted_at"])
    return max(1, int((oldest_counted + window - utc_now()).total_seconds()) + 1)


def throttle_seconds(conn, email, ip, settings):
    """Seconds to wait before another login attempt is allowed, or 0."""
    window = timedelta(minutes=settings.window_minutes)
    since = utc_now() - window
    last_success = conn.execute(
        "SELECT MAX(attempted_at) FROM login_attempts WHERE email = ? AND succeeded = 1", (email,)
    ).fetchone()[0]
    email_since = max(since, datetime.fromisoformat(last_success)) if last_success else since
    waits = [_retry_after(conn, "email", email, email_since, settings.max_failures_per_email, window)]
    if ip:
        waits.append(_retry_after(conn, "ip", ip, since, settings.max_failures_per_ip, window))
    return max(waits)


def _record_attempt(conn, email, ip, succeeded, settings):
    now = utc_now()
    conn.execute(
        "INSERT INTO login_attempts (email, ip, succeeded, attempted_at) VALUES (?, ?, ?, ?)",
        (email, ip, int(succeeded), iso(now)),
    )
    # Keep a day (or the window, if longer); the last success per email is kept regardless.
    keep = max(timedelta(days=1), timedelta(minutes=settings.window_minutes))
    conn.execute(
        "DELETE FROM login_attempts WHERE attempted_at < ? AND NOT (succeeded = 1 AND attempted_at = "
        "(SELECT MAX(attempted_at) FROM login_attempts AS l WHERE l.email = login_attempts.email AND l.succeeded = 1))",
        (iso(now - keep),),
    )


def authenticate(conn, email, password, settings, *, ip=None):
    """Return the user's row, or raise AuthError (401 wrong credentials, 403 disabled, 429 throttled)."""
    try:
        email = normalize_email(email)
    except ValueError:
        email = (email or "").strip().lower()[:254]
    wait = throttle_seconds(conn, email, ip, settings)
    if wait:
        audit(conn, "login_throttled", actor_email=email, ip=ip)
        raise AuthError(429, "Too many failed sign-in attempts. Try again later.", retry_after=wait)

    row = conn.execute(f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        _equalize_timing(password or "")
        ok = False
    else:
        ok = verify_password(row["password_hash"], password or "")
    if not ok:
        _record_attempt(conn, email, ip, False, settings)
        audit(conn, "login_failed", actor_email=email, ip=ip,
              details={"reason": "unknown_email" if row is None else "wrong_password"})
        raise AuthError(401, "Incorrect email or password.")
    if not row["is_active"]:
        _record_attempt(conn, email, ip, False, settings)
        audit(conn, "login_blocked_inactive", actor_user_id=row["id"], actor_email=email, ip=ip)
        raise AuthError(403, "This account has been deactivated. Contact an administrator.")

    _record_attempt(conn, email, ip, True, settings)
    updates, params = ["last_login_at = ?"], [iso(utc_now())]
    if _hasher.check_needs_rehash(row["password_hash"]):
        updates.append("password_hash = ?")
        params.append(hash_password(password))
    conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", (*params, row["id"]))
    audit(conn, "login", actor_user_id=row["id"], actor_email=email, ip=ip)
    return row


def change_password(conn, user, current_password, new_password, settings, *, ip=None):
    """Change the signed-in user's own password. Wrong current passwords count as failed logins."""
    wait = throttle_seconds(conn, user.email, ip, settings)
    if wait:
        raise AuthError(429, "Too many failed attempts. Try again later.", retry_after=wait)
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    if not verify_password(row["password_hash"], current_password or ""):
        _record_attempt(conn, user.email, ip, False, settings)
        audit(conn, "password_change_failed", actor=user, ip=ip)
        raise AuthError(400, "Current password is incorrect.")
    if current_password == new_password:
        raise AuthError(422, "Choose a password different from the current one.")
    try:
        set_password(conn, user.id, new_password, user.email)
    except ValueError as exc:
        raise AuthError(422, str(exc)) from exc
    revoke_user_sessions(conn, user.id, keep_token_hash=user.token_hash)
    audit(conn, "password_changed", actor=user, target_type="user", target_id=user.id, ip=ip)


# --- Audit log -------------------------------------------------------------------

def audit(conn, action, *, actor=None, actor_user_id=None, actor_email=None, target_type=None,
          target_id=None, details=None, ip=None):
    """Append one entry. `actor` (a CurrentUser) fills in the actor fields. Never pass secrets in details."""
    if actor is not None:
        actor_user_id, actor_email = actor.id, actor.email
    conn.execute(
        "INSERT INTO audit_logs (created_at, actor_user_id, actor_email, action, target_type, target_id, details, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (iso(utc_now()), actor_user_id, actor_email, action, target_type,
         None if target_id is None else str(target_id),
         None if details is None else json.dumps(details, ensure_ascii=False), ip),
    )


def _audit_rows(conn, where, params, limit, offset):
    total = conn.execute(f"SELECT COUNT(*) FROM audit_logs a{where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT a.id, a.created_at, a.actor_email, a.action, a.target_type, a.target_id, a.details, a.ip, "
        "c.name AS candidate_name FROM audit_logs a "
        "LEFT JOIN candidates c ON a.target_type = 'candidate' AND c.id = a.target_id"
        f"{where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item["details"]) if item["details"] else None
        items.append(item)
    return items, total


CANDIDATE_ACTIONS = ("tag_added", "tag_removed", "shortlist_added", "shortlist_removed", "summary_generated")


def candidate_activity(conn, limit, offset):
    """Recruitment activity on candidates, without IPs or account events."""
    placeholders = ", ".join("?" * len(CANDIDATE_ACTIONS))
    items, total = _audit_rows(conn, f" WHERE a.action IN ({placeholders})", CANDIDATE_ACTIONS, limit, offset)
    for item in items:
        item.pop("ip")
    return items, total


def audit_log(conn, limit, offset, action=None):
    where, params = ("", ()) if action is None else (" WHERE a.action = ?", (action,))
    return _audit_rows(conn, where, params, limit, offset)
