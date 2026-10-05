"""Login, logout and session endpoints (Google SSO and a demo-only developer login)."""

import secrets
from datetime import timedelta
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.google_oidc import OIDCError, authorization_url
from app.api.security import (
    CsrfProtected,
    CurrentPrincipal,
    Principal,
    clear_session_cookie,
    set_session_cookie,
    sign,
    verify,
)
from app.config import Settings
from app.core.logging import get_logger
from app.db.models import Employee
from app.domain.rules.location import DISPLAY
from app.hr.provisioning import ProvisioningError, provision_demo_employee
from app.observability.audit import AuditEvent

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)

OAUTH_COOKIE = "hr_oauth"
DEMO_DOMAIN = "@example.com"


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service is starting or degraded")
    return runtime


async def _principal_for(request: Request, email: str, method: str) -> Principal:
    rt = _runtime(request)
    emp_id = await rt.deps.hr.find_employee_id_by_email(email)
    if emp_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No HR record for this account")
    profile = await rt.deps.hr.get_profile(emp_id)
    return Principal(
        employee_id=profile.id,
        name=profile.full_name,
        email=profile.email,
        location=profile.location.value,
        auth_method=method,
    )


async def _audit(
    request: Request, principal: Principal | None, outcome: str, **details: Any
) -> None:
    rt = getattr(request.app.state, "runtime", None)
    if rt is not None:
        await rt.deps.audit.record(
            AuditEvent(
                event_type="auth",
                name=f"login_{details.pop('method', 'unknown')}",
                outcome=outcome,
                employee_id=principal.employee_id if principal else None,
                request_id=getattr(request.state, "request_id", None),
                details=details,
            )
        )


class AuthConfig(BaseModel):
    google_enabled: bool
    dev_login_enabled: bool
    allowed_domain: str
    demo_users: list[dict[str, str]] = []


@router.get("/config", response_model=AuthConfig)
async def auth_config(request: Request) -> AuthConfig:
    settings: Settings = request.app.state.settings
    users: list[dict[str, str]] = []
    if settings.auth_dev_login_enabled:
        rt = _runtime(request)
        async with rt.sessions() as s:
            rows = await s.execute(
                select(Employee.email, Employee.full_name, Employee.designation, Employee.location)
                .where(Employee.email.like(f"%{DEMO_DOMAIN}"))
                .order_by(Employee.full_name)
            )
            users = [
                {"email": e, "name": n, "designation": d, "location": DISPLAY[loc]}
                for e, n, d, loc in rows
            ]
    return AuthConfig(
        google_enabled=bool(settings.google_client_id and settings.google_client_secret),
        dev_login_enabled=settings.auth_dev_login_enabled,
        allowed_domain=settings.google_allowed_domain,
        demo_users=users,
    )


class DevLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@router.post("/dev-login", dependencies=[CsrfProtected])
async def dev_login(body: DevLoginRequest, request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    email = body.email.strip().lower()
    if not settings.auth_dev_login_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if not email.endswith(DEMO_DOMAIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Developer login is for demo users only")
    principal = await _principal_for(request, email, "dev")
    await _audit(request, principal, "ok", method="dev")
    response = JSONResponse({"name": principal.name})
    set_session_cookie(response, request, principal)
    return response


@router.get("/google/login")
async def google_login(request: Request) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    if not settings.google_client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google sign-in is not configured")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    response = RedirectResponse(
        authorization_url(
            settings.google_client_id,
            settings.google_redirect_uri,
            state,
            nonce,
            settings.google_allowed_domain,
        ),
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        OAUTH_COOKIE,
        sign(
            {"state": state, "nonce": nonce},
            request.app.state.session_secret,
            timedelta(minutes=10),
        ),
        max_age=600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/auth/google",
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    failure = RedirectResponse("/?login_error=1", status_code=status.HTTP_302_FOUND)
    failure.delete_cookie(OAUTH_COOKIE, path="/auth/google")
    raw = request.cookies.get(OAUTH_COOKIE)
    if error or not code or not state or not raw:
        await _audit(request, None, "denied", method="google", reason=error or "missing_params")
        return failure
    try:
        expected = verify(raw, request.app.state.session_secret)
    except jwt.PyJWTError:
        await _audit(request, None, "denied", method="google", reason="bad_state_cookie")
        return failure
    if not secrets.compare_digest(str(expected.get("state")), state):
        await _audit(request, None, "denied", method="google", reason="state_mismatch")
        return failure
    try:
        identity = await request.app.state.google_verifier.exchange_and_verify(
            code, str(expected["nonce"])
        )
    except OIDCError as exc:
        log.warning("auth.google_rejected", reason=str(exc))
        await _audit(request, None, "denied", method="google", reason=str(exc))
        return failure

    rt = _runtime(request)
    if await rt.deps.hr.find_employee_id_by_email(identity.email) is None:
        if not settings.mock_hr_autoprovision:
            await _audit(request, None, "denied", method="google", reason="no_hr_record")
            return RedirectResponse("/?login_error=no_record", status_code=status.HTTP_302_FOUND)
        try:
            await provision_demo_employee(
                rt.sessions, identity.email, identity.name, settings.mock_hr_template_email
            )
        except ProvisioningError:
            return failure
    principal = await _principal_for(request, identity.email, "google")
    await _audit(request, principal, "ok", method="google")
    response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(OAUTH_COOKIE, path="/auth/google")
    set_session_cookie(response, request, principal)
    return response


class Me(BaseModel):
    name: str
    email: str
    location: str
    auth_method: str


@router.get("/me", response_model=Me)
async def me(principal: CurrentPrincipal) -> Me:
    from app.db.models import Location

    return Me(
        name=principal.name,
        email=principal.email,
        location=DISPLAY.get(Location(principal.location), principal.location),
        auth_method=principal.auth_method,
    )


@router.post("/logout", dependencies=[CsrfProtected])
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response
