"""FastAPI dependencies for authentication, CSRF protection and role permissions.

Every protected route depends on require("<permission>"). That resolves the
session cookie, rejects expired/revoked sessions and deactivated users (401),
checks CSRF and Origin on state-changing methods (403), then checks the role's
permissions (403).
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app import auth
from app.db import connect

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SIGNED_OUT_MESSAGES = {
    "missing": "Not signed in.",
    "expired": "Your session has expired. Sign in again.",
    "inactive": "This account has been deactivated.",
}


def get_conn(request: Request):
    """One connection per request, always closed."""
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def client_ip(request):
    return request.client.host if request.client else None


def cookie_is_secure(request):
    mode = request.app.state.auth_settings.cookie_secure
    return mode == "always" or (mode == "auto" and request.url.scheme == "https")


def set_session_cookie(response, request, token):
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=request.app.state.auth_settings.max_hours * 3600,
        httponly=True, samesite="lax", secure=cookie_is_secure(request), path="/",
    )


def clear_session_cookie(response, request):
    response.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=cookie_is_secure(request))


def check_origin(request):
    """Browsers send Origin on cross-site POSTs; refuse any that isn't this site."""
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="Cross-site request refused.")


def _signed_out(request, reason):
    secure = "; Secure" if cookie_is_secure(request) else ""
    expired_cookie = f"{auth.COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=lax{secure}"
    raise HTTPException(status_code=401, detail=SIGNED_OUT_MESSAGES[reason], headers={"Set-Cookie": expired_cookie})


def current_user(request: Request, conn: Annotated[object, Depends(get_conn)]):
    token = request.cookies.get(auth.COOKIE_NAME)
    user, status = auth.resolve_session(conn, token, request.app.state.auth_settings)
    if user is None:
        _signed_out(request, status)
    if request.method not in SAFE_METHODS:
        check_origin(request)
        if not auth.csrf_valid(request.app.state.secret_key, token, request.headers.get(auth.CSRF_HEADER)):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token. Reload the page and try again.",
                                headers={"X-Error-Code": "csrf_failed"})
    request.state.session_token = token
    return user


def require(permission):
    """Dependency factory: the signed-in user, if their role grants `permission`."""

    def dependency(user: Annotated[auth.CurrentUser, Depends(current_user)]):
        if not user.can(permission):
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({auth.ROLE_LABELS[user.role]}) doesn't allow this action.",
            )
        return user

    dependency.permission = permission
    return dependency
