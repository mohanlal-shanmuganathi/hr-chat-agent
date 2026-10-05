"""FastAPI application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models import BaseChatModel

from app.agent.runtime import build_runtime
from app.api.errors import register_exception_handlers
from app.api.google_oidc import GoogleVerifier, HttpGoogleVerifier
from app.api.rate_limit import RateLimiter
from app.api.routes import auth, chat, health
from app.api.security import resolve_session_secret
from app.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def create_app(
    settings: Settings | None = None,
    model: BaseChatModel | None = None,
    google_verifier: GoogleVerifier | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            try:
                app.state.runtime = await stack.enter_async_context(
                    build_runtime(settings, model=model)
                )
            except Exception as exc:
                # Start degraded (health checks still answer) rather than crash-loop. The reason
                # is logged in full and reported by /ready so it can be fixed.
                log.exception("app.runtime_unavailable", error_type=type(exc).__name__)
                app.state.runtime = None
                app.state.startup_error = f"{type(exc).__name__}: {exc}"[:300]
            log.info("app.startup", environment=settings.environment)
            yield
        log.info("app.shutdown")

    app = FastAPI(title="HR Chat Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_secret = resolve_session_secret(settings)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.google_verifier = google_verifier or (
        HttpGoogleVerifier(
            settings.google_client_id,
            settings.google_client_secret.get_secret_value(),
            settings.google_redirect_uri,
            settings.google_allowed_domain,
        )
        if settings.google_client_id and settings.google_client_secret
        else None
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate the UI on every load (cheap: ETag / 304), so an update never mixes a
            # new page with a cached old stylesheet or script.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app
