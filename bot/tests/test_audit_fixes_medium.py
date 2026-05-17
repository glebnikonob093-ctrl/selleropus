"""Regression tests for the medium/low severity audit fixes.

Each test pins one specific behaviour from the audit follow-up PR:

* ``test_today_window_uses_master_timezone`` — ``GET /api/bookings/today`` for
  a Moscow master at 02:00 MSK (= 23:00 UTC prev. day) used to return the
  previous UTC day's bookings; now it returns the current local day.
* ``test_stats_day_window_uses_master_timezone`` — ``GET /api/stats?period=day``
  for the same Moscow master at 02:00 MSK used to bucket revenue against the
  previous UTC day; now it follows the master's local calendar.
* ``test_morning_summary_fires_at_local_eight`` — ``_send_morning_summaries``
  used to fire at 08:00 UTC for everyone; now a Moscow master gets pinged at
  08:00 MSK (= 05:00 UTC) and not at 08:00 UTC (= 11:00 MSK).
* ``test_public_booking_rejects_off_grid_slot`` — ``POST /api/public/{slug}/bookings``
  used to accept arbitrary minute offsets like 11:17; now it 400s if the slot
  isn't aligned to ``slot_step_minutes`` inside working hours.
* ``test_anonymous_clients_dedup_by_phone`` — two anonymous public bookings
  with the same phone (in different formats) now reuse a single ``Client``
  row instead of creating duplicates.
* ``test_generate_unique_slug_falls_back_to_entropy`` — after the bare
  base + 8 sequential suffixes are taken, ``generate_unique_slug`` now
  returns a slug with a random hex suffix instead of looping forever.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api import create_api_app
from app.config import Settings
from app.models import (
    BOOKING_STATUS_CAME,
    BOOKING_STATUS_CONFIRMED,
    Booking,
    Client,
    Master,
    Service,
)
from app.repos import find_or_create_client, generate_unique_slug
from app.scheduler import _send_morning_summaries

BOT_TOKEN = "TEST_TOKEN_FOR_AUDIT_MEDIUM"
ADMIN_TG_ID = 1200247714


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
        default_timezone="Europe/Moscow",
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


def _freeze_datetime_at(
    monkeypatch: pytest.MonkeyPatch, modules: tuple[str, ...], utc_moment: datetime
) -> None:
    """Patch ``datetime`` in each listed module so ``now(tz)`` and ``utcnow``
    deterministically return ``utc_moment`` (which must be aware/UTC).

    The booking/stats/scheduler code reads "now" through ``datetime.now(tz)``
    after our TZ fixes; pre-fix it called ``datetime.utcnow()``. Patching
    both keeps the test useful across the transition and clearly documents
    what wall-clock instant we're simulating.
    """
    assert utc_moment.tzinfo is not None, "frozen moment must be tz-aware"
    utc_moment = utc_moment.astimezone(UTC)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return utc_moment.replace(tzinfo=None)
            return utc_moment.astimezone(tz)

        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return utc_moment.replace(tzinfo=None)

    for mod in modules:
        monkeypatch.setattr(f"{mod}.datetime", _FrozenDT)


# ---------------------------------------------------------------------------
# #5: ``/api/bookings/today`` follows the master's timezone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_today_window_uses_master_timezone(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moscow master at 02:00 MSK sees today's bookings, not yesterday's.

    Scenario: it's currently 23:00 UTC on 2026-05-16 (= 02:00 MSK on
    2026-05-17). The master has one booking at 02:30 MSK / 2026-05-17,
    stored as the naive datetime ``2026-05-17 02:30``. Pre-fix the
    "today" window was ``[2026-05-16 00:00, 2026-05-17 00:00)`` UTC and
    the 02:30 booking fell outside. Post-fix the window is anchored to
    the master's local midnight, ``[2026-05-17 00:00, 2026-05-18 00:00)``,
    so the booking shows up.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = _settings()
    app = create_api_app(settings=settings, session_factory=session_factory, notifier=None)

    # First create the master + service + booking using a non-frozen clock so
    # the auth's auth_date check passes; then freeze for the actual /today
    # request below.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/api/me", headers=_auth(ADMIN_TG_ID, "boss"))

        # Pin the master's timezone deterministically.
        async with session_factory() as session:
            from sqlalchemy import select

            master = (
                await session.execute(
                    select(Master).where(Master.tg_user_id == ADMIN_TG_ID)
                )
            ).scalar_one()
            master.timezone = "Europe/Moscow"
            await session.commit()

        svc = await c.post(
            "/api/services",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"name": "Маникюр", "price": 2000, "duration_minutes": 60},
        )
        assert svc.status_code == 201
        service_id = svc.json()["id"]

        # Booking at 02:30 local on 2026-05-17. Stored as naive wall-clock.
        booking_local = datetime(2026, 5, 17, 2, 30)
        created = await c.post(
            "/api/bookings",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={
                "service_id": service_id,
                "starts_at": booking_local.isoformat(),
                "new_client_name": "Night Owl",
                "status": BOOKING_STATUS_CONFIRMED,
            },
        )
        assert created.status_code == 201, created.text

        # Now freeze "now" at 2026-05-17 02:00 MSK = 2026-05-16 23:00 UTC
        # and request the today list.
        _freeze_datetime_at(
            monkeypatch,
            ("app.api.bookings",),
            datetime(2026, 5, 16, 23, 0, tzinfo=ZoneInfo("UTC")),
        )

        r = await c.get("/api/bookings/today", headers=_auth(ADMIN_TG_ID, "boss"))
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1, rows
        assert rows[0]["client_name"] == "Night Owl"


# ---------------------------------------------------------------------------
# #6: ``/api/stats?period=day`` follows the master's timezone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_day_window_uses_master_timezone(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moscow master's day-revenue at 02:00 MSK includes early-morning bookings."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = _settings()
    app = create_api_app(settings=settings, session_factory=session_factory, notifier=None)

    # Seed master, service, and a CAME booking at 01:00 MSK / 2026-05-17.
    async with session_factory() as session:
        master = Master(
            tg_user_id=ADMIN_TG_ID,
            tg_chat_id=ADMIN_TG_ID,
            tg_username="boss",
            display_name="Boss",
            slug="boss",
            timezone="Europe/Moscow",
            is_master=True,
            is_admin=True,
        )
        session.add(master)
        await session.flush()
        service = Service(
            master_id=master.id, name="Маникюр", price=2000, duration_minutes=60
        )
        session.add(service)
        await session.flush()
        client = Client(master_id=master.id, name="Night Owl")
        session.add(client)
        await session.flush()
        booking = Booking(
            master_id=master.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=datetime(2026, 5, 17, 1, 0),
            ends_at=datetime(2026, 5, 17, 2, 0),
            status=BOOKING_STATUS_CAME,
            price_snapshot=2000,
        )
        session.add(booking)
        await session.commit()

    # Freeze "now" at 2026-05-17 02:00 MSK = 2026-05-16 23:00 UTC.
    _freeze_datetime_at(
        monkeypatch,
        ("app.api.stats",),
        datetime(2026, 5, 16, 23, 0, tzinfo=ZoneInfo("UTC")),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/api/stats?period=day", headers=_auth(ADMIN_TG_ID, "boss")
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The 01:00 MSK booking is in today's window (Moscow local day) and
        # contributes to the day's revenue.
        assert body["revenue"] == 2000, body
        assert body["bookings_came"] == 1, body


# ---------------------------------------------------------------------------
# #7: morning summary anchored to master's local 08:00
# ---------------------------------------------------------------------------


class _MorningRecorder:
    def __init__(self) -> None:
        self.summary_calls: list[tuple[int, int]] = []

    async def notify_master_morning_summary(
        self,
        *,
        master: Master,
        bookings: list[tuple[Booking, Client, Service]],
    ) -> None:
        self.summary_calls.append((master.id, len(bookings)))


@pytest.mark.asyncio
async def test_morning_summary_fires_at_local_eight(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Moscow master gets the morning ping at 08:00 MSK, not 08:00 UTC.

    Two ticks are simulated:
    * 05:05 UTC = 08:05 MSK — must fire exactly once for the Moscow master.
    * 08:05 UTC = 11:05 MSK — must NOT fire again for the Moscow master
      (it's mid-morning local).
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Seed master + a single booking today (Moscow local day = 2026-05-17).
    async with session_factory() as session:
        master = Master(
            tg_user_id=777777,
            tg_chat_id=777777,
            display_name="Morning Master",
            slug="morning",
            timezone="Europe/Moscow",
            is_master=True,
        )
        session.add(master)
        await session.flush()
        service = Service(
            master_id=master.id, name="Стрижка", price=1500, duration_minutes=60
        )
        session.add(service)
        await session.flush()
        client = Client(master_id=master.id, name="Walk-in")
        session.add(client)
        await session.flush()
        # Booking at 12:00 MSK / 2026-05-17.
        booking = Booking(
            master_id=master.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=datetime(2026, 5, 17, 12, 0),
            ends_at=datetime(2026, 5, 17, 13, 0),
            status=BOOKING_STATUS_CONFIRMED,
            price_snapshot=1500,
        )
        session.add(booking)
        master_id = master.id
        await session.commit()

    notifier = _MorningRecorder()

    # Tick 1: 05:05 UTC = 08:05 MSK — should fire.
    _freeze_datetime_at(
        monkeypatch,
        ("app.scheduler",),
        datetime(2026, 5, 17, 5, 5, tzinfo=ZoneInfo("UTC")),
    )
    await _send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]
    assert notifier.summary_calls == [(master_id, 1)], notifier.summary_calls

    # Tick 2: 08:05 UTC = 11:05 MSK — must NOT fire again.
    _freeze_datetime_at(
        monkeypatch,
        ("app.scheduler",),
        datetime(2026, 5, 17, 8, 5, tzinfo=ZoneInfo("UTC")),
    )
    await _send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]
    assert notifier.summary_calls == [(master_id, 1)], notifier.summary_calls


# ---------------------------------------------------------------------------
# #8: public booking rejects off-grid slots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_booking_rejects_off_grid_slot(engine: AsyncEngine) -> None:
    """The public booking endpoint must 400 on slots that don't fit the grid."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = _settings()
    app = create_api_app(settings=settings, session_factory=session_factory, notifier=None)

    # Seed master with 10:00-20:00 / 30-min step. ``timezone="UTC"`` so the
    # "cannot book in the past" check (which uses real utcnow) is easy to
    # satisfy with a far-future date.
    async with session_factory() as session:
        master = Master(
            tg_user_id=5555,
            tg_chat_id=5555,
            display_name="Grid Master",
            slug="grid-master",
            timezone="UTC",
            work_start_minutes=10 * 60,
            work_end_minutes=20 * 60,
            slot_step_minutes=30,
            is_master=True,
        )
        session.add(master)
        await session.flush()
        service = Service(
            master_id=master.id, name="Стрижка", price=1500, duration_minutes=60
        )
        session.add(service)
        await session.flush()
        service_id = service.id
        await session.commit()

    far_future_day = datetime.utcnow().date() + timedelta(days=30)

    def _at(hh: int, mm: int, ss: int = 0) -> str:
        return datetime(
            far_future_day.year, far_future_day.month, far_future_day.day, hh, mm, ss
        ).isoformat()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Off-grid minute (11:17 on a 30-min grid) — must 400.
        r = await c.post(
            "/api/public/grid-master/bookings",
            json={
                "service_id": service_id,
                "starts_at": _at(11, 17),
                "name": "Hacker",
                "phone": "+79991234567",
            },
        )
        assert r.status_code == 400, r.text
        assert "grid" in r.json()["detail"]

        # Before working hours (09:00 with start=10:00) — must 400.
        r2 = await c.post(
            "/api/public/grid-master/bookings",
            json={
                "service_id": service_id,
                "starts_at": _at(9, 0),
                "name": "Early",
            },
        )
        assert r2.status_code == 400, r2.text

        # Service spills past end (19:30 + 60min = 20:30 > 20:00) — must 400.
        r3 = await c.post(
            "/api/public/grid-master/bookings",
            json={
                "service_id": service_id,
                "starts_at": _at(19, 30),
                "name": "Late",
            },
        )
        assert r3.status_code == 400, r3.text

        # On-grid (12:00) — must succeed.
        r4 = await c.post(
            "/api/public/grid-master/bookings",
            json={
                "service_id": service_id,
                "starts_at": _at(12, 0),
                "name": "Normal",
                "phone": "+79991112233",
            },
        )
        assert r4.status_code == 201, r4.text


# ---------------------------------------------------------------------------
# #10: anonymous client dedup by phone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_clients_dedup_by_phone(engine: AsyncEngine) -> None:
    """Two anonymous bookings from the same phone reuse one ``Client`` row.

    Different formatting (``+7 (999) 123-45-67`` vs ``+79991234567``) must
    normalize to the same key. A subsequent TG-identified booking must
    stay separate.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        master = Master(
            tg_user_id=10101,
            tg_chat_id=10101,
            display_name="Dedup Master",
            slug="dedup-master",
            timezone="UTC",
            is_master=True,
        )
        session.add(master)
        await session.flush()

        first = await find_or_create_client(
            session,
            master.id,
            name="Анна",
            phone="+79991234567",
        )
        second = await find_or_create_client(
            session,
            master.id,
            name="Anna P.",
            phone="+7 (999) 123-45-67",
        )
        third = await find_or_create_client(
            session,
            master.id,
            name="Other Person",
            phone="+78005553535",
        )
        fourth = await find_or_create_client(
            session,
            master.id,
            name="Anna with TG",
            phone="+79991234567",
            tg_user_id=42,
        )

        assert second.id == first.id, "anonymous clients with same phone must merge"
        assert third.id != first.id, "different phones must stay separate"
        assert fourth.id != first.id, (
            "an authenticated TG client must not be merged into an anonymous row"
        )
        # The merged row gets the latest name (Anna P.) — useful when the
        # master wants to see the most recent display in the client list.
        assert second.name == "Anna P."


# ---------------------------------------------------------------------------
# #11: slug-pick entropy fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_unique_slug_falls_back_to_entropy(engine: AsyncEngine) -> None:
    """When base + ``-2..-9`` are all taken, return a slug with a hex suffix.

    Pre-fix the loop ran ``-2``, ``-3``, …, ``-100``, ``-101`` indefinitely;
    fine in practice but a slow path for pathological collisions. Post-fix
    we cap sequential at 8 and switch to entropy.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        # Seed ``anna`` plus ``anna-2`` … ``anna-9`` to exhaust the
        # human-friendly suffix range.
        for i, slug in enumerate(["anna"] + [f"anna-{n}" for n in range(2, 10)]):
            session.add(
                Master(
                    tg_user_id=2000 + i,
                    tg_chat_id=2000 + i,
                    display_name=f"Anna {i}",
                    slug=slug,
                )
            )
        await session.flush()

        new_slug = await generate_unique_slug(session, "Anna")
        assert new_slug.startswith("anna-"), new_slug
        # Sequential range is anna-2..anna-9; the fallback must produce
        # something outside it.
        assert new_slug not in {
            f"anna-{n}" for n in range(2, 10)
        }, f"sequential suffix still chosen: {new_slug}"
        # The fallback uses ``secrets.token_hex(3)`` → 6 hex chars.
        suffix = new_slug.removeprefix("anna-")
        assert len(suffix) == 6, suffix
        assert all(ch in "0123456789abcdef" for ch in suffix), suffix
