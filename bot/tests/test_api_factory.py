"""Smoke test that the FastAPI factory can build the app and register all routes.

This catches FastAPI-level import-time validation errors (e.g. routes with
`status_code=204` that still declare a JSON body, which crash the assertion
in `fastapi.routing.APIRoute.__init__`, or SPA-fallback routes whose return
annotation is a Union that FastAPI can't turn into a Pydantic response field).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import create_api_app
from app.config import Settings


def _settings(webapp_dist_dir: str = "") -> Settings:
    return Settings(
        bot_token="TEST_TOKEN",
        database_url="sqlite+aiosqlite:///:memory:",
        api_host="127.0.0.1",
        api_port=8000,
        webapp_url="",
        webapp_dist_dir=webapp_dist_dir,
        telegram_proxy_url="",
        scheduler_interval_seconds=60,
        default_work_start=(10, 0),
        default_work_end=(20, 0),
        default_slot_step_minutes=30,
        default_timezone="UTC",
    )


def test_create_api_app_registers_expected_routes(
    session_factory: async_sessionmaker,
) -> None:
    app = create_api_app(
        settings=_settings(),
        session_factory=session_factory,
        notifier=None,
    )
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    for required in (
        "/api/health",
        "/api/me",
        "/api/services",
        "/api/services/{service_id}",
        "/api/clients",
        "/api/clients/{client_id}",
        "/api/bookings",
        "/api/bookings/{booking_id}",
        "/api/bookings/today",
        "/api/stats",
        "/api/stats/return-clients",
        "/api/public/{slug}",
        "/api/public/{slug}/availability",
        "/api/public/{slug}/bookings",
    ):
        assert required in paths, f"missing route: {required}"


def test_create_api_app_with_webapp_dist_dir(
    session_factory: async_sessionmaker, tmp_path: Path
) -> None:
    """Regression: SPA fallback route uses a union return type which without
    response_model=None makes FastAPI try (and fail) to build a pydantic
    response field."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    app = create_api_app(
        settings=_settings(webapp_dist_dir=str(dist)),
        session_factory=session_factory,
        notifier=None,
    )
    catch_all = [
        r for r in app.routes if getattr(r, "path", "") == "/{full_path:path}"
    ]
    assert catch_all, "SPA catch-all route missing"


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/services/{service_id}", "DELETE"),
        ("/api/clients/{client_id}", "DELETE"),
        ("/api/bookings/{booking_id}", "DELETE"),
    ],
)
def test_delete_endpoints_204_have_no_body(
    session_factory: async_sessionmaker, path: str, method: str
) -> None:
    """Regression: FastAPI rejects 204 routes with a JSON response body."""
    app = create_api_app(
        settings=_settings(), session_factory=session_factory, notifier=None
    )
    matching = [
        r
        for r in app.routes
        if hasattr(r, "path")
        and r.path == path
        and method in getattr(r, "methods", set())
    ]
    assert matching, f"no {method} route for {path}"
    route = matching[0]
    assert route.status_code == 204
