"""Smoke test that the FastAPI factory can build the app and register all routes.

This catches FastAPI-level import-time validation errors (e.g. routes with
`status_code=204` that still declare a JSON body, which crash the assertion
in `fastapi.routing.APIRoute.__init__`).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import create_api_app
from app.config import Settings


def _settings() -> Settings:
    return Settings(
        bot_token="TEST_TOKEN",
        database_url="sqlite+aiosqlite:///:memory:",
        api_host="127.0.0.1",
        api_port=8000,
        webapp_url="",
        webapp_dist_dir="",
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

    # Sanity-check that core routers are mounted.
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
