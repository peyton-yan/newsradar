"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    APP_VERSION,
    Settings,
    load_or_create_master_key,
    session_secret_from_master_key,
)
from app.analysis.service import AnalysisService
from app.db import Database
from app.ingest.service import IngestService
from app.models import WorkerHeartbeat
from app.push.service import PushService
from app.security import Security
from app.settings_service import SettingsService
from app.web import LoginLimiter, is_logged_in, router, setup_complete


STATIC_DIR = Path(__file__).resolve().parent / "static"

for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    bootstrap = settings or Settings.from_env()
    bootstrap.ensure_directories()
    master_key = load_or_create_master_key(bootstrap.master_key_file)

    db = Database(bootstrap.db_path)
    db.init_schema()
    security = Security(master_key)
    settings_service = SettingsService(db, security)
    settings_service.ensure_defaults()
    ingest = IngestService(db, settings_service)
    ingest.ensure_default_sources()
    analysis = AnalysisService(db, settings_service)
    push = PushService(db, settings_service)

    with db.session() as session:
        heartbeat = session.get(WorkerHeartbeat, 1)
        if heartbeat is None:
            session.add(WorkerHeartbeat(id=1, last_beat_at=None, version=APP_VERSION))

    app = FastAPI(
        title="NewsRadar",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bootstrap = bootstrap
    app.state.db = db
    app.state.security = security
    app.state.settings_service = settings_service
    app.state.ingest = ingest
    app.state.analysis = analysis
    app.state.push = push
    app.state.login_limiter = LoginLimiter()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        )
        return response

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path == "/healthz":
            return await call_next(request)

        if not setup_complete(request):
            if path == "/setup" or path.startswith("/setup/test/"):
                return await call_next(request)
            return RedirectResponse("/setup", status_code=303)

        if not is_logged_in(request):
            if path in ("/login", "/setup"):
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=303)

        if path == "/login":
            return RedirectResponse("/", status_code=303)
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret_from_master_key(master_key),
        session_cookie="newsradar_session",
        max_age=12 * 3600,
        same_site="lax",
        https_only=False,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    @app.exception_handler(404)
    async def not_found(request: Request, _exc):  # noqa: ANN001
        return RedirectResponse("/", status_code=303)

    return app


app = None if os.getenv("NR_DISABLE_DEFAULT_APP") == "1" else create_app()


if __name__ == "__main__":
    import uvicorn

    runtime = Settings.from_env()
    uvicorn.run(app, host=runtime.host, port=runtime.port, log_level=runtime.log_level.lower())
