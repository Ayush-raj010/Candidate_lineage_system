"""HTTP routes. Routes validate input and shape responses; SQL lives in repository.py and auth.py.

Every route except /api/health and /api/auth/login requires a signed-in user,
and each names the permission it needs (see auth.PERMISSIONS).
"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response

from app import ai_summary, auth, repository
from app.db import connect, transaction
from app.models import (
    AuditPage,
    AuditQuery,
    CandidateDetail,
    CandidatePage,
    CandidateQuery,
    CandidateTags,
    Facets,
    HealthOut,
    LoginIn,
    MeOut,
    PageQuery,
    PasswordChangeIn,
    PasswordResetIn,
    RankQuery,
    RankResponse,
    ShortlistIn,
    ShortlistOut,
    SummaryLookup,
    SummaryOut,
    SummaryRequest,
    Tag,
    TagIn,
    UserCreateIn,
    UserOut,
    UserUpdateIn,
)
from app.security import (
    check_origin,
    clear_session_cookie,
    client_ip,
    current_user,
    get_conn,
    require,
    set_session_cookie,
)

router = APIRouter(prefix="/api")

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
CandidateId = Annotated[str, Path(min_length=1, max_length=64)]
SignedIn = Annotated[auth.CurrentUser, Depends(current_user)]


def Permitted(permission):  # noqa: N802 (reads like a type in signatures)
    return Annotated[auth.CurrentUser, Depends(require(permission))]


@router.get("/health", response_model=HealthOut)
def health(request: Request, response: Response):
    """Public: liveness for monitoring and the sign-in page."""
    ai_configured = ai_summary.AISettings.from_env().configured
    demo_accounts = [dict(a) for a in auth.DEMO_ACCOUNTS] if auth.demo_login_enabled() else []
    # Opens its own connection: reporting a database failure is this route's job.
    try:
        conn = connect(request.app.state.db_path)
        try:
            candidates = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        response.status_code = 503
        return HealthOut(status="error", db="error", candidates=None, ai_configured=ai_configured)
    return HealthOut(status="ok", db="ok", candidates=candidates, ai_configured=ai_configured,
                     demo_accounts=demo_accounts)


# --- Authentication ---------------------------------------------------------------

def _me(conn, request, user, token):
    return {
        "user": auth.public_user(auth.get_user(conn, user.id)),
        "permissions": sorted(user.permissions),
        "csrf_token": auth.csrf_token(request.app.state.secret_key, token),
        "session_expires_at": auth.session_expiry(conn, user.token_hash),
    }


def _auth_error(exc):
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers)


@router.post(
    "/auth/login",
    response_model=MeOut,
    responses={401: {"description": "Wrong email or password"}, 403: {"description": "Account deactivated"},
               429: {"description": "Too many failed attempts (see Retry-After)"}},
)
def login(body: LoginIn, request: Request, response: Response, conn: Conn):
    check_origin(request)
    settings = request.app.state.auth_settings
    try:
        row = auth.authenticate(conn, body.email, body.password, settings, ip=client_ip(request))
    except auth.AuthError as exc:
        raise _auth_error(exc) from exc
    previous = request.cookies.get(auth.COOKIE_NAME)
    if previous:
        auth.revoke_session(conn, previous)  # a fresh token on every sign-in
    token = auth.create_session(conn, row["id"], settings, ip=client_ip(request),
                                user_agent=request.headers.get("user-agent"))
    set_session_cookie(response, request, token)
    user = auth.CurrentUser(id=row["id"], email=row["email"], role=row["role"], token_hash=auth.hash_token(token))
    return _me(conn, request, user, token)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, conn: Conn):
    """Ends the session. Signing out without a valid session just clears the cookie."""
    token = request.cookies.get(auth.COOKIE_NAME)
    user, _ = auth.resolve_session(conn, token, request.app.state.auth_settings)
    if user is not None:
        current_user(request, conn)  # CSRF and Origin checks for a live session
        auth.revoke_session(conn, token)
        auth.audit(conn, "logout", actor=user, ip=client_ip(request))
    clear_session_cookie(response, request)


@router.get("/auth/me", response_model=MeOut)
def me(request: Request, user: SignedIn, conn: Conn):
    return _me(conn, request, user, request.state.session_token)


@router.post("/auth/password", status_code=204)
def change_password(body: PasswordChangeIn, request: Request, user: SignedIn, conn: Conn):
    """Change your own password. Your other sessions are signed out."""
    try:
        auth.change_password(conn, user, body.current_password, body.new_password,
                             request.app.state.auth_settings, ip=client_ip(request))
    except auth.AuthError as exc:
        raise _auth_error(exc) from exc


# --- Candidates -------------------------------------------------------------------

@router.get("/candidates", response_model=CandidatePage)
def list_candidates(params: Annotated[CandidateQuery, Query()], conn: Conn, _: Permitted("candidates:read")):
    items, total = repository.list_candidates(
        conn,
        q=params.q,
        role=params.role,
        source=params.source,
        tag=params.tag,
        shortlisted=params.shortlisted,
        sort=params.sort.value,
        limit=params.limit,
        offset=params.offset,
    )
    return {"items": items, "total": total, "limit": params.limit, "offset": params.offset}


@router.get("/candidates/{candidate_id}", response_model=CandidateDetail)
def get_candidate(candidate_id: CandidateId, conn: Conn, _: Permitted("candidates:read")):
    candidate = repository.get_candidate(conn, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.get("/facets", response_model=Facets)
def get_facets(conn: Conn, _: Permitted("candidates:read")):
    return repository.facets(conn)


@router.get("/rank", response_model=RankResponse)
def rank(params: Annotated[RankQuery, Query()], conn: Conn, _: Permitted("ranking:read")):
    """Rank candidates by required-skill overlap: /api/rank?skills=Python&skills=FastAPI"""
    result = repository.rank_candidates(
        conn, params.skills, role=params.role, source=params.source, limit=params.limit, offset=params.offset
    )
    return {**result, "limit": params.limit, "offset": params.offset}


def _require_candidate(conn, candidate_id):
    if not repository.candidate_exists(conn, candidate_id):
        raise HTTPException(status_code=404, detail="Candidate not found")


@router.post(
    "/candidates/{candidate_id}/tags",
    response_model=CandidateTags,
    status_code=201,
    responses={200: {"description": "The candidate already had this tag; nothing changed"}},
)
def add_tag(candidate_id: CandidateId, body: TagIn, conn: Conn, request: Request, response: Response,
            user: Permitted("tags:write")):
    _require_candidate(conn, candidate_id)
    with transaction(conn):
        added = repository.add_tag(conn, candidate_id, body.tag)
        if added:
            auth.audit(conn, "tag_added", actor=user, target_type="candidate", target_id=candidate_id,
                       details={"tag": body.tag}, ip=client_ip(request))
    if not added:
        response.status_code = 200
    return {"id": candidate_id, "tags": repository.get_tags(conn, candidate_id)}


@router.delete("/candidates/{candidate_id}/tags/{tag}", response_model=CandidateTags)
def remove_tag(candidate_id: CandidateId, tag: Annotated[Tag, Path()], conn: Conn, request: Request,
               user: Permitted("tags:write")):
    """Removing a tag the candidate doesn't have is not an error."""
    _require_candidate(conn, candidate_id)
    with transaction(conn):
        if repository.remove_tag(conn, candidate_id, tag):
            auth.audit(conn, "tag_removed", actor=user, target_type="candidate", target_id=candidate_id,
                       details={"tag": tag}, ip=client_ip(request))
    return {"id": candidate_id, "tags": repository.get_tags(conn, candidate_id)}


@router.get(
    "/candidates/{candidate_id}/summary",
    response_model=SummaryOut,
    responses={404: {"description": "Unknown candidate, or no summary generated yet for these requirements"}},
)
def cached_candidate_summary(candidate_id: CandidateId, params: Annotated[SummaryLookup, Query()], conn: Conn,
                             _: Permitted("summaries:read")):
    """A previously generated summary, read from the cache. Never calls the AI service."""
    exists, summary = ai_summary.cached_summary(
        conn, candidate_id, requirements=params.requirements, settings=ai_summary.AISettings.from_env()
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if summary is None:
        raise HTTPException(status_code=404, detail="No summary has been generated for this candidate yet.")
    return summary


@router.post(
    "/candidates/{candidate_id}/summary",
    response_model=SummaryOut,
    responses={
        502: {"description": "The AI service failed or returned an unusable response"},
        503: {"description": "AI not configured, or the AI service is rate-limiting"},
        504: {"description": "The AI service timed out"},
    },
)
def candidate_summary(
    candidate_id: CandidateId, request: Request, conn: Conn, user: Permitted("summaries:generate"),
    body: Annotated[SummaryRequest | None, Body()] = None,
):
    """Evidence-grounded fit summary. Cached per candidate version and requirements; the body is optional."""
    body = body or SummaryRequest()
    settings = ai_summary.AISettings.from_env()
    client = request.app.state.ai_client or (ai_summary.make_client(settings) if settings.configured else None)
    # The connection is in autocommit mode, so no lock is held while waiting for the model.
    try:
        result = ai_summary.get_summary(
            conn, candidate_id, requirements=body.requirements, refresh=body.refresh, settings=settings, client=client
        )
    except ai_summary.AIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not result["cached"]:
        auth.audit(conn, "summary_generated", actor=user, target_type="candidate", target_id=candidate_id,
                   details={"requirements": result["requirements"], "fit": result["fit"]}, ip=client_ip(request))
    return result


@router.put("/candidates/{candidate_id}/shortlist", response_model=ShortlistOut)
def set_shortlist(candidate_id: CandidateId, body: ShortlistIn, conn: Conn, request: Request,
                  user: Permitted("shortlist:write")):
    _require_candidate(conn, candidate_id)
    with transaction(conn):
        changed = repository.is_shortlisted(conn, candidate_id) != body.shortlisted
        result = repository.set_shortlisted(conn, candidate_id, body.shortlisted)
        if changed:
            auth.audit(conn, "shortlist_added" if body.shortlisted else "shortlist_removed", actor=user,
                       target_type="candidate", target_id=candidate_id, ip=client_ip(request))
    return result


@router.get("/activity", response_model=AuditPage)
def activity(params: Annotated[PageQuery, Query()], conn: Conn, _: Permitted("activity:read")):
    """Recent recruitment activity on candidates: tags, shortlist changes, generated summaries."""
    items, total = auth.candidate_activity(conn, params.limit, params.offset)
    return {"items": items, "total": total, "limit": params.limit, "offset": params.offset}


# --- Administration -------------------------------------------------------------------

def _user_or_404(conn, user_id):
    row = auth.get_user(conn, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return row


@router.get("/admin/users", response_model=list[UserOut])
def list_users(conn: Conn, _: Permitted("users:manage")):
    return auth.list_users(conn)


@router.post("/admin/users", response_model=UserOut, status_code=201,
             responses={409: {"description": "Email already in use"}})
def create_user(body: UserCreateIn, request: Request, conn: Conn, admin: Permitted("users:manage")):
    try:
        with transaction(conn):
            user_id = auth.create_user(conn, body.email, body.role, password=body.password)
            auth.audit(conn, "user_created", actor=admin, target_type="user", target_id=user_id,
                       details={"email": auth.normalize_email(body.email), "role": body.role}, ip=client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except auth.AuthError as exc:
        raise _auth_error(exc) from exc
    return auth.public_user(auth.get_user(conn, user_id))


@router.patch("/admin/users/{user_id}", response_model=UserOut,
              responses={409: {"description": "Would leave no active administrator"}})
def update_user(user_id: int, body: UserUpdateIn, request: Request, conn: Conn, admin: Permitted("users:manage")):
    """Change a role and/or activate or deactivate. Deactivating signs the user out everywhere."""
    with transaction(conn):
        row = _user_or_404(conn, user_id)
        role = body.role if body.role is not None else row["role"]
        active = body.is_active if body.is_active is not None else bool(row["is_active"])
        loses_admin = row["role"] == "admin" and row["is_active"] and (role != "admin" or not active)
        if loses_admin and auth.count_active_admins(conn, excluding_user_id=user_id) == 0:
            raise HTTPException(status_code=409, detail="At least one active administrator must remain.")
        if role == row["role"] and active == bool(row["is_active"]):
            return auth.public_user(row)
        conn.execute("UPDATE users SET role = ?, is_active = ?, updated_at = ? WHERE id = ?",
                     (role, int(active), auth.iso(auth.utc_now()), user_id))
        ip = client_ip(request)
        if role != row["role"]:
            auth.audit(conn, "user_role_changed", actor=admin, target_type="user", target_id=user_id,
                       details={"email": row["email"], "from": row["role"], "to": role}, ip=ip)
        if active != bool(row["is_active"]):
            auth.audit(conn, "user_activated" if active else "user_deactivated", actor=admin, target_type="user",
                       target_id=user_id, details={"email": row["email"]}, ip=ip)
            if not active:
                auth.revoke_user_sessions(conn, user_id)
    return auth.public_user(auth.get_user(conn, user_id))


@router.post("/admin/users/{user_id}/password", status_code=204)
def reset_password(user_id: int, body: PasswordResetIn, request: Request, conn: Conn,
                   admin: Permitted("users:manage")):
    """Set a new password for a user and sign them out everywhere."""
    with transaction(conn):
        row = _user_or_404(conn, user_id)
        try:
            auth.set_password(conn, user_id, body.password, row["email"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        auth.revoke_user_sessions(conn, user_id)
        auth.audit(conn, "password_reset", actor=admin, target_type="user", target_id=user_id,
                   details={"email": row["email"]}, ip=client_ip(request))


@router.get("/admin/audit-logs", response_model=AuditPage)
def audit_logs(params: Annotated[AuditQuery, Query()], conn: Conn, _: Permitted("audit:read")):
    items, total = auth.audit_log(conn, params.limit, params.offset, params.action)
    return {"items": items, "total": total, "limit": params.limit, "offset": params.offset}
