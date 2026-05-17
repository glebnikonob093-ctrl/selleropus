"""Regression tests for the bug-audit fixes shipped with the roles-admin PR.

Each test pins one specific behaviour that an earlier version of the code
got wrong:

* ``test_spa_fallback_rejects_path_traversal`` — the SPA catch-all used to
  ``return FileResponse(path / full_path)`` for any input, which let an
  attacker read files outside the dist directory via ``..%2f``.
* ``test_update_booking_rejects_overlap`` — ``update_booking`` did not run
  the same overlap check that ``create_booking`` does, so a master could
  drag one of their bookings onto an occupied slot.
* ``test_update_booking_notifies_status_change`` — flipping status from the
  master Mini App did not trigger ``notify_status_change`` even though the
  notifier had been built for exactly that case.
* ``test_morning_summary_skips_empty_day`` — if a master had no bookings,
  the scheduler kept re-sending "no bookings today" every minute between
  08:00 and 08:15 UTC because the sentinel marker was anchored to a
  ``booking_id`` and there was no booking to anchor to.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api import create_api_app
from app.config import Settings
from app.models import (
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_NEW,
    Booking,
    Client,
    Master,
    Service,
)
from app.scheduler import _send_morning_summaries

BOT_TOKEN = "TEST_TOKEN_FOR_AUDIT"
ADMIN_TG_ID = 1200247714


def _settings(*, webapp_dist_dir: str = "") -> Settings:
    return Settings(
        bot_token=BOT_TOKEN,
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
        admin_tg_ids=(ADMIN_TG_ID,),
        admin_contact_url=f"tg://user?id={ADMIN_TG_ID}",
        become_master_conditions="Pro 299 ₽/мес + написать админу",
    )


def _signed_init_data(tg_user_id: int, username: str = "tester") -> str:
    user = json.dumps(
        {
            "id": tg_user_id,
            "first_name": "Test",
            "last_name": "",
            "username": username,
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
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def _auth(tg_user_id: int, username: str = "tester") -> dict[str, str]:
    return {"Authorization": f"tma {_signed_init_data(tg_user_id, username)}"}


# ---------------------------------------------------------------------------
# CRITICAL: SPA fallback path traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spa_fallback_rejects_path_traversal(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A request whose path tries to escape the dist root must NOT stream the
    real file from disk; it must fall back to ``index.html`` instead.

    Before the fix, ``/{full_path:path}`` would happily resolve
    ``path / "../../etc/passwd"`` and return its contents.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")

    # Drop a target file *outside* the dist that the attacker would want
    # to read. We use a sibling file with distinctive content so we can
    # be sure the response did NOT serve it.
    secret = tmp_path / "secret.txt"
    secret.write_text("SUPER_SECRET_VALUE", encoding="utf-8")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(webapp_dist_dir=str(dist)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Direct traversal attempt — must NOT leak the sibling file.
        r = await c.get("/..%2Fsecret.txt")
        assert r.status_code == 200, r.text
        assert "SUPER_SECRET_VALUE" not in r.text
        assert "<html>spa</html>" in r.text

        # A nested path that resolves outside dist must also be coerced
        # back to the SPA index.
        r2 = await c.get("/static/..%2F..%2Fsecret.txt")
        assert r2.status_code == 200
        assert "SUPER_SECRET_VALUE" not in r2.text

        # Legitimate non-existent paths still fall back to index.html.
        r3 = await c.get("/bookings/123")
        assert r3.status_code == 200
        assert "<html>spa</html>" in r3.text


# ---------------------------------------------------------------------------
# HIGH: update_booking overlap check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_booking_rejects_overlap(engine: AsyncEngine) -> None:
    """PATCH /api/bookings/{id} must 409 when the new time overlaps with an
    existing active booking owned by the same master."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # The admin gets master-level access automatically.
        await c.get("/api/me", headers=_auth(ADMIN_TG_ID, "boss"))

        # Set up one 60-minute service.
        svc = await c.post(
            "/api/services",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"name": "Стрижка", "price": 1500, "duration_minutes": 60},
        )
        assert svc.status_code == 201, svc.text
        service_id = svc.json()["id"]

        # Create two non-overlapping bookings: 10:00 and 12:00 tomorrow.
        tomorrow = (datetime.utcnow() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        a = await c.post(
            "/api/bookings",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={
                "service_id": service_id,
                "starts_at": (tomorrow.replace(hour=10)).isoformat(),
                "new_client_name": "A",
                "status": BOOKING_STATUS_CONFIRMED,
            },
        )
        assert a.status_code == 201, a.text
        a_id = a.json()["id"]

        b = await c.post(
            "/api/bookings",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={
                "service_id": service_id,
                "starts_at": (tomorrow.replace(hour=12)).isoformat(),
                "new_client_name": "B",
                "status": BOOKING_STATUS_CONFIRMED,
            },
        )
        assert b.status_code == 201, b.text

        # Now try to drag A onto B's slot. This used to silently succeed.
        r = await c.patch(
            f"/api/bookings/{a_id}",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"starts_at": tomorrow.replace(hour=12).isoformat()},
        )
        assert r.status_code == 409, r.text
        assert "overlap" in r.json()["detail"].lower()

        # No-op PATCH on A (same time) must still succeed.
        r2 = await c.patch(
            f"/api/bookings/{a_id}",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"notes": "no time change"},
        )
        assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------------------
# HIGH: update_booking notifies on status change
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    """Stand-in for ``Notifier`` that just records every call. Lets us
    assert that ``notify_status_change`` actually fires from the API."""

    def __init__(self) -> None:
        self.status_changes: list[dict[str, object]] = []
        self.new_bookings: list[dict[str, object]] = []

    async def notify_status_change(
        self,
        *,
        client: Client,
        booking: Booking,
        service: Service,
        master: Master,
        old_status: str,
        new_status: str,
    ) -> None:
        self.status_changes.append(
            {
                "client_id": client.id,
                "booking_id": booking.id,
                "service_id": service.id,
                "master_id": master.id,
                "old_status": old_status,
                "new_status": new_status,
            }
        )

    async def notify_master_new_booking(
        self,
        *,
        master: Master,
        booking: Booking,
        client: Client,
        service: Service,
    ) -> None:
        self.new_bookings.append(
            {"master_id": master.id, "booking_id": booking.id}
        )


@pytest.mark.asyncio
async def test_update_booking_notifies_status_change(engine: AsyncEngine) -> None:
    """Master PATCHes a booking from ``new`` → ``cancelled``. The notifier
    must see exactly one status_change call with the right old/new values.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    notifier = _RecordingNotifier()
    app = create_api_app(
        settings=_settings(),
        session_factory=session_factory,
        notifier=notifier,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/api/me", headers=_auth(ADMIN_TG_ID, "boss"))
        svc = await c.post(
            "/api/services",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"name": "Маникюр", "price": 2000, "duration_minutes": 60},
        )
        assert svc.status_code == 201
        service_id = svc.json()["id"]

        when = (datetime.utcnow() + timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        created = await c.post(
            "/api/bookings",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={
                "service_id": service_id,
                "starts_at": when.isoformat(),
                "new_client_name": "Klient",
                "status": BOOKING_STATUS_NEW,
            },
        )
        assert created.status_code == 201, created.text
        booking_id = created.json()["id"]

        # Status change: new → cancelled.
        r = await c.patch(
            f"/api/bookings/{booking_id}",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"status": "cancelled"},
        )
        assert r.status_code == 200, r.text

        assert len(notifier.status_changes) == 1
        evt = notifier.status_changes[0]
        assert evt["booking_id"] == booking_id
        assert evt["old_status"] == BOOKING_STATUS_NEW
        assert evt["new_status"] == "cancelled"

        # PATCH that does NOT change status must not produce extra calls.
        r2 = await c.patch(
            f"/api/bookings/{booking_id}",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"notes": "still cancelled"},
        )
        assert r2.status_code == 200, r2.text
        assert len(notifier.status_changes) == 1


# ---------------------------------------------------------------------------
# HIGH: morning summary doesn't fire on empty day
# ---------------------------------------------------------------------------


class _MorningRecorder:
    def __init__(self) -> None:
        self.summary_calls: list[int] = []

    async def notify_master_morning_summary(
        self,
        *,
        master: Master,
        bookings: list[tuple[Booking, Client, Service]],
    ) -> None:
        self.summary_calls.append(len(bookings))


@pytest.mark.asyncio
async def test_morning_summary_skips_empty_day(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A master with no bookings today must not receive the morning summary.

    Pre-fix, ``_send_morning_summaries`` would call ``notify_master_morning_summary``
    every tick from 08:00 to 08:15 UTC, because it never marked the day as
    "sent" when there were no bookings to anchor the sentinel to. We assert
    that two consecutive ticks during the morning window produce zero calls.
    """
    # Pretend it's currently 08:05 UTC so ``_send_morning_summaries`` enters
    # the active branch.
    class _FrozenDT(datetime):
        @classmethod
        def utcnow(cls) -> _FrozenDT:  # type: ignore[override]
            return cls(2026, 5, 17, 8, 5, 0)

    monkeypatch.setattr("app.scheduler.datetime", _FrozenDT)

    # Insert one master with NO bookings.
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        master = Master(
            tg_user_id=42424242,
            tg_chat_id=42424242,
            display_name="No Bookings Master",
            slug="no-bookings",
            is_master=True,
        )
        session.add(master)
        await session.commit()

    notifier = _MorningRecorder()

    # Two consecutive ticks within the morning window. Pre-fix this would
    # call notify_master_morning_summary twice with bookings=[]. Post-fix
    # it must call zero times.
    await _send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]
    await _send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]

    assert notifier.summary_calls == [], (
        "morning summary was sent for a master with no bookings today; "
        f"got {notifier.summary_calls} calls"
    )
