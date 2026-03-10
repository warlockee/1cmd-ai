"""Admin panel authentication — session cookie management.

Calling spec:
  Inputs: Config (admin_password field)
  Outputs: FastAPI dependency for auth checking
  Side effects: session cookie management

Endpoints:
  POST /api/auth/login   -> {password} -> set session cookie
  GET  /api/auth/status   -> {authenticated: bool}
  POST /api/auth/logout   -> clear session

Implementation:
  Token-based sessions: on login, generate a random token, store it in a
  server-side dict, set it as an HTTP-only cookie.  If no admin_password
  is configured, generate a random one on startup and log it.
"""


import logging
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session store (server-side, in-memory)
# ---------------------------------------------------------------------------

_sessions: dict[str, bool] = {}  # token -> True

_SESSION_COOKIE = "onecmd_session"
_TOKEN_BYTES = 32

# ---------------------------------------------------------------------------
# Password resolution
# ---------------------------------------------------------------------------

_resolved_password: str | None = None


def resolve_password(configured: str | None) -> str:
    """Return the admin password, generating one if not configured."""
    global _resolved_password
    if _resolved_password is not None:
        return _resolved_password
    if configured:
        _resolved_password = configured
    else:
        _resolved_password = "admin"
    return _resolved_password


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_auth(onecmd_session: str | None = Cookie(default=None)) -> bool:
    """FastAPI dependency that enforces authentication via session cookie.

    Raises HTTPException 401 if the session cookie is missing or invalid.
    Returns True if authenticated.
    """
    if onecmd_session is None or onecmd_session not in _sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@auth_router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    """Validate password and set session cookie."""
    password = resolve_password(request.app.state.config.admin_password)
    if not secrets.compare_digest(body.password, password):
        raise HTTPException(status_code=403, detail="Invalid password")

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    _sessions[token] = True

    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True}


@auth_router.get("/status")
async def status(onecmd_session: str | None = Cookie(default=None)):
    """Check whether the current session is authenticated."""
    authenticated = onecmd_session is not None and onecmd_session in _sessions
    return {"authenticated": authenticated}


@auth_router.post("/logout")
async def logout(
    response: Response,
    onecmd_session: str | None = Cookie(default=None),
):
    """Clear the session cookie and invalidate the server-side token."""
    if onecmd_session and onecmd_session in _sessions:
        del _sessions[onecmd_session]

    response.delete_cookie(key=_SESSION_COOKIE, path="/")
    return {"authenticated": False}
