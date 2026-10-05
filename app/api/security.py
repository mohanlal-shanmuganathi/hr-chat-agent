"""Sessions, CSRF protection and the authenticated-employee dependency.

The session is a short-lived HS256 JWT in an HttpOnly, SameSite=Lax cookie. It carries only the
employee id and display fields; everything else is looked up server-side. State-changing
requests must also send the `X-Requested-With: hr-chat` header, which a cross-site form or
image cannot add (CSRF defence in depth on top of SameSite).
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, Response, status

from app.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "hr_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "hr-chat"
_ALGORITHM = "HS256"
_AUDIENCE = "hr-chat-agent"


@dataclass(frozen=True)
class Principal:
    employee_id: uuid.UUID
    name: str
    email: str
    location: str
    auth_method: str  # "google" | "dev"


def resolve_session_secret(settings: Settings) -> str:
    if settings.session_secret and settings.session_secret.get_secret_value():
        return settings.session_secret.get_secret_value()
    if settings.environment in ("local", "test"):
        log.warning("auth.ephemeral_session_secret", hint="set SESSION_SECRET in .env")
        return secrets.token_urlsafe(48)
    raise RuntimeError("SESSION_SECRET must be set outside local development")


def sign(payload: dict[str, Any], secret: str, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    claims = {**payload, "iat": now, "exp": now + ttl, "aud": _AUDIENCE}
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def verify(token: str, secret: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(token, secret, algorithms=[_ALGORITHM], audience=_AUDIENCE)
    return claims


def set_session_cookie(response: Response, request: Request, principal: Principal) -> None:
    settings: Settings = request.app.state.settings
    token = sign(
        {
            "sub": str(principal.employee_id),
            "name": principal.name,
            "email": principal.email,
            "loc": principal.location,
            "amr": principal.auth_method,
        },
        request.app.state.session_secret,
        timedelta(minutes=settings.session_ttl_minutes),
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_principal(request: Request) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        claims = verify(token, request.app.state.session_secret)
        return Principal(
            employee_id=uuid.UUID(claims["sub"]),
            name=claims["name"],
            email=claims["email"],
            location=claims["loc"],
            auth_method=claims["amr"],
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or invalid") from exc


def require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing CSRF header")


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
CsrfProtected = Depends(require_csrf_header)
