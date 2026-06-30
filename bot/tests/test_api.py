from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import create_api_app
from app.config import Settings

BOT_TOKEN = "1234:ABC"


def _settings() -> Settings:
    return Settings(
        bot_token=BOT_TOKEN,
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


def _init_data(user: dict) -> str:
    pairs: dict[str, str] = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH",
        "user": json.dumps(user, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(pairs)


def _auth_headers(user: dict) -> dict[str, str]:
    return {"Authorization": f"tma {_init_data(user)}"}


class _RecordingNotifier:
    def __init__(self) -> None:
        self.status_changes: list[tuple[str, str]] = []
        self.new_bookings: int = 0

    async def notify_status_change(self, *, old_status: str, new_status: str, **_: object) -> None:
        self.status_changes.append((old_status, new_status))

    async def notify_master_new_booking(self, **_: object) -> None:
        self.new_bookings += 1


@pytest.fixture()
def notifier() -> _RecordingNotifier:
    return _RecordingNotifier()


@pytest.fixture()
def client(
    session_factory: async_sessionmaker[AsyncSession], notifier: _RecordingNotifier
) -> TestClient:
    app = create_api_app(
        settings=_settings(), session_factory=session_factory, notifier=notifier
    )
    return TestClient(app)


MASTER = {"id": 100, "first_name": "Anna", "username": "annab"}


def test_app_builds_and_health(client: TestClient) -> None:
    # Regression: the API app used to fail to even construct because 204 routes
    # had a `-> None` return annotation under `from __future__ import annotations`.
    assert client.get("/api/health").json() == {"status": "ok"}


def test_me_creates_master(client: TestClient) -> None:
    res = client.get("/api/me", headers=_auth_headers(MASTER))
    assert res.status_code == 200
    body = res.json()
    assert body["tg_user_id"] == 100
    assert body["slug"]


def test_full_booking_flow_and_delete(client: TestClient) -> None:
    headers = _auth_headers(MASTER)

    svc = client.post(
        "/api/services",
        headers=headers,
        json={"name": "Haircut", "price": 1500, "duration_minutes": 60},
    ).json()

    starts_at = (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat()
    booking = client.post(
        "/api/bookings",
        headers=headers,
        json={
            "service_id": svc["id"],
            "starts_at": starts_at,
            "new_client_name": "Client A",
            "status": "new",
        },
    )
    assert booking.status_code == 201
    booking_id = booking.json()["id"]

    # 204 delete route must work (was broken before the fix).
    deleted = client.delete(f"/api/bookings/{booking_id}", headers=headers)
    assert deleted.status_code == 204


def test_status_change_notifies(client: TestClient, notifier: _RecordingNotifier) -> None:
    headers = _auth_headers(MASTER)
    svc = client.post(
        "/api/services",
        headers=headers,
        json={"name": "Massage", "price": 2000, "duration_minutes": 30},
    ).json()
    starts_at = (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat()
    booking = client.post(
        "/api/bookings",
        headers=headers,
        json={"service_id": svc["id"], "starts_at": starts_at,
              "new_client_name": "Client B", "status": "new"},
    ).json()

    res = client.patch(
        f"/api/bookings/{booking['id']}", headers=headers, json={"status": "cancelled"}
    )
    assert res.status_code == 200
    assert notifier.status_changes == [("new", "cancelled")]


def test_public_availability_uses_date_alias(client: TestClient) -> None:
    headers = _auth_headers(MASTER)
    me = client.get("/api/me", headers=headers).json()
    slug = me["slug"]
    svc = client.post(
        "/api/services",
        headers=headers,
        json={"name": "Nails", "price": 800, "duration_minutes": 60},
    ).json()

    day = (datetime.utcnow() + timedelta(days=2)).date().isoformat()
    # The Mini App sends `?date=...`; the endpoint must accept that alias.
    res = client.get(
        f"/api/public/{slug}/availability",
        params={"service_id": svc["id"], "date": day},
    )
    assert res.status_code == 200
    slots = res.json()
    assert isinstance(slots, list)
    assert len(slots) > 0
