"""Regression test for the get-or-create master race condition.

The Mini App issues several authenticated requests in parallel on first load
(e.g. ``Promise.all([api.getMe(), api.listBookingsToday()])``). Each of these
requests runs ``get_current_master`` which looks up the master by
``tg_user_id`` and INSERTs a new row when none exists. Without serialization
both requests find no row, both try to INSERT, and the second one crashes
with ``IntegrityError: UNIQUE constraint failed: masters.tg_user_id`` —
producing a confusing 500 on the very first frame the user sees.

``get_current_master`` wraps the INSERT in a SAVEPOINT and falls back to a
re-SELECT on UNIQUE violation. This test exercises that path directly so a
future refactor that drops the savepoint will fail loudly.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api import create_api_app
from app.config import Settings
from app.models import Master

BOT_TOKEN = "TEST_TOKEN_FOR_RACE"


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
        # Make the parallel-request user an admin so it bypasses the
        # ``get_current_active_master`` gate and can call master routes on
        # first paint without first being promoted.
        admin_tg_ids=(424242,),
        admin_contact_url="tg://user?id=424242",
        become_master_conditions="",
    )


def _signed_init_data(tg_user_id: int) -> str:
    user = json.dumps(
        {
            "id": tg_user_id,
            "first_name": "Race",
            "last_name": "",
            "username": "raceuser",
            "language_code": "ru",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAEAAQABAAAAAA",
        "user": user,
    }
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


@pytest.mark.asyncio
async def test_parallel_first_requests_share_one_master(
    engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(), session_factory=session_factory, notifier=None
    )

    init_data = _signed_init_data(424242)
    headers = {"Authorization": f"tma {init_data}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            client.get("/api/me", headers=headers),
            client.get("/api/bookings/today", headers=headers),
            client.get("/api/services", headers=headers),
            client.get("/api/clients", headers=headers),
            client.get("/api/stats?period=day", headers=headers),
        )

    for r in responses:
        assert r.status_code == 200, (
            f"parallel request returned {r.status_code}: {r.text}"
        )

    me = responses[0].json()
    assert me["tg_user_id"] == 424242
    assert me["display_name"] == "Race"

    # Exactly one master row must exist after the race.
    async with session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(Master))
    assert total == 1, f"expected exactly 1 master row, got {total}"
