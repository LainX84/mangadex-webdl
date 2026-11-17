from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import mimetypes

# Python 3.12 does not know these; without them they would be served as octet-stream.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("font/woff2", ".woff2")
from sqlalchemy import func, select, text

from app import __version__
from app.api.deps import require_auth
from app.api.routes import archive, auth, follows, manga, queue, stats, system
from app.api.security import SecurityHeadersMiddleware, TrustedProxyMiddleware, token_hash
from app.config import get_config
from app.db import get_engine, session_scope
from app.logging_setup import configure_logging, get_logger

log = get_logger("api")


def ensure_setup_token() -> str | None:
    """If no user exists, create a one-time setup token and print it to the logs."""
    from app.models import User
    from app.settings_service import SETUP_TOKEN_KEY, set_raw

    with session_scope() as s:
        if (s.scalar(select(func.count()).select_from(User)) or 0) > 0:
            return None
        token = secrets.token_urlsafe(24)
        set_raw(s, SETUP_TOKEN_KEY, token_hash(token))
    banner = "=" * 72
    # Printed (not structured-logged) on purpose so it is easy to find with `docker logs`.
    print(f"\n{banner}\nFIRST-RUN SETUP: no administrator exists yet.\n"
          f"Open the web UI and enter this one-time setup token:\n\n    {token}\n\n"
          f"(A new token is generated on every restart until setup is completed.)\n{banner}\n", flush=True)
    return token


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.cli import bootstrap_admin_from_env

    try:
        bootstrap_admin_from_env()
        ensure_setup_token()
    except Exception:
        log.exception("startup_bootstrap_failed")
    yield


def create_app() -> FastAPI:
    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_json)
    app = FastAPI(
        title="MangaDex Download Manager",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # don't advertise the API schema publicly
        lifespan=lifespan,
    )

    # Public endpoints: health + login/setup only.
    app.include_router(auth.router)

    protected = [Depends(require_auth)]
    app.include_router(manga.router, dependencies=protected)
    app.include_router(queue.router, dependencies=protected)
    app.include_router(system.router, dependencies=protected)
    app.include_router(follows.router, dependencies=protected)
    app.include_router(archive.router, dependencies=protected)
    app.include_router(stats.router, dependencies=protected)

    @app.get("/api/health", include_in_schema=False)
    def health():
        ok = True
        detail = {}
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            detail["database"] = "ok"
        except Exception:
            ok, detail["database"] = False, "error"
        try:
            from app.redis_client import get_redis

            get_redis().ping()
            detail["redis"] = "ok"
        except Exception:
            ok, detail["redis"] = False, "error"
        return JSONResponse({"status": "ok" if ok else "error", **detail}, status_code=200 if ok else 503)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            detail = getattr(exc, "detail", "Not found")
            return JSONResponse({"detail": detail}, status_code=404)
        return _spa_index(cfg.static_dir)

    static = Path(cfg.static_dir)
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (static / full_path).resolve()
        if full_path and candidate.is_file() and static.resolve() in candidate.parents:
            return FileResponse(candidate)
        return _spa_index(static)

    # Order matters: proxy handling must run first (outermost).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TrustedProxyMiddleware)
    return app


def _spa_index(static: Path):
    index = Path(static) / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Frontend not built"}, status_code=404)


app = create_app()
