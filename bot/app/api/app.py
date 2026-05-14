from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.admin import router as admin_router
from app.api.bookings import router as bookings_router
from app.api.clients import router as clients_router
from app.api.deps import AppState
from app.api.health import router as health_router
from app.api.me import router as me_router
from app.api.public import router as public_router
from app.api.services import router as services_router
from app.api.stats import router as stats_router
from app.config import Settings


def _resolve_origins(settings: Settings) -> list[str]:
    candidates: Iterable[str] = (
        settings.webapp_url,
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    return [c for c in (s.strip() for s in candidates) if c]


def create_api_app(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    notifier: object | None = None,
) -> FastAPI:
    app = FastAPI(title="Clientika API", version="0.1.0")

    app.state.app_state = AppState(
        settings=settings,
        session_factory=session_factory,
        notifier=notifier,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_origins(settings),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(me_router)
    app.include_router(services_router)
    app.include_router(clients_router)
    app.include_router(bookings_router)
    app.include_router(stats_router)
    app.include_router(public_router)
    app.include_router(admin_router)

    dist_dir = (settings.webapp_dist_dir or "").strip()
    if dist_dir:
        path = Path(dist_dir)
        if path.is_dir():
            assets_dir = path / "assets"
            if assets_dir.is_dir():
                app.mount(
                    "/assets",
                    StaticFiles(directory=str(assets_dir)),
                    name="assets",
                )

            index_file = path / "index.html"

            @app.get("/", include_in_schema=False, response_model=None)
            async def _index() -> FileResponse:
                return FileResponse(str(index_file))

            @app.get(
                "/{full_path:path}",
                include_in_schema=False,
                response_model=None,
            )
            async def _spa_fallback(full_path: str) -> FileResponse | JSONResponse:
                if full_path.startswith("api/"):
                    return JSONResponse({"detail": "not found"}, status_code=404)
                target = path / full_path
                if target.is_file():
                    return FileResponse(str(target))
                return FileResponse(str(index_file))

    return app
