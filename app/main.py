"""FastAPI application.

Run: uvicorn app.main:app --reload
The database path defaults to data/candidates.db; set GCCX_DB_PATH to override.
Settings are read from environment variables; a .env file in the project root is
loaded first, without overriding variables that are already set.
The dashboard is served at / (signed-in users only) and the sign-in page at /login,
with assets under /static; the API stays under /api.
"""

import logging
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import api, auth
from app.db import BASE_DIR, DEFAULT_DB_PATH, connect, init_schema

load_dotenv(BASE_DIR / ".env", override=False)
log = logging.getLogger(__name__)

STATIC_DIR = BASE_DIR / "static"
# Some Windows registries map .js to text/plain, and browsers refuse ES modules served that way.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    ),
}
# FastAPI's /docs page loads its assets from a CDN, so it gets a looser policy.
DOCS_PATHS = ("/docs", "/redoc")
MIN_SECRET_LENGTH = 32


class RevalidatedStaticFiles(StaticFiles):
    """Without Cache-Control, browsers may reuse an old app.js after an update.
    no-cache keeps the cache but revalidates each load (a cheap 304 via ETag)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def load_secret_key():
    """GCCX_SECRET_KEY signs CSRF tokens. Without it, a random key is used and CSRF tokens
    change on restart (the dashboard simply fetches a new one)."""
    configured = os.environ.get("GCCX_SECRET_KEY", "")
    if configured:
        if len(configured) < MIN_SECRET_LENGTH:
            raise RuntimeError(f"GCCX_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters.")
        return configured.encode("utf-8")
    return secrets.token_bytes(32)


def create_app(db_path=None, ai_client=None, auth_settings=None):
    """`ai_client` replaces the real LLM client (tests pass a fake one)."""
    db_path = Path(db_path or os.environ.get("GCCX_DB_PATH") or DEFAULT_DB_PATH)

    @asynccontextmanager
    async def lifespan(app):
        # Applies pending migrations; raises LegacySchemaError or SchemaVersionError on an unusable database.
        conn = connect(db_path)
        try:
            applied = init_schema(conn)
            if applied:
                log.info("Database migrated to schema version %s", applied[-1])
            if not conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1").fetchone():
                log.warning("No active administrator. Create one with: python -m app.manage create-admin --email ...")
        finally:
            conn.close()
        yield

    app = FastAPI(title="GCCX Candidate Triage", lifespan=lifespan)
    app.state.db_path = db_path
    app.state.ai_client = ai_client
    app.state.auth_settings = auth_settings or auth.AuthSettings.from_env()
    app.state.secret_key = load_secret_key()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and request.url.path.startswith(DOCS_PATHS):
                continue
            response.headers.setdefault(name, value)
        return response

    app.include_router(api.router)
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

    def signed_in(request):
        conn = connect(db_path)
        try:
            user, _ = auth.resolve_session(conn, request.cookies.get(auth.COOKIE_NAME), app.state.auth_settings)
        finally:
            conn.close()
        return user is not None

    @app.get("/", include_in_schema=False)
    def dashboard(request: Request):
        if not signed_in(request):
            target = "/" + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if signed_in(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
