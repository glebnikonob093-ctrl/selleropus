"""Regression tests for the low-severity audit fixes (#9, #12-#17 + N1, N9).

See ``bugs-audit.md`` for the full audit. Each ``test_*`` here pins one
behaviour from the audit so the next refactor that silently reverts a fix
fails loudly here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.api import create_api_app
from app.api.app import _resolve_origins
from app.api.deps import _LOCK_CAP, _lock_for, _master_create_locks
from app.auth import InitDataError, parse_init_data
from app.bot.handlers import (
    BTN_ADMIN_PANEL,
    BTN_BECOME_MASTER,
    BTN_HELP,
    BTN_OPEN_APP,
    BTN_PROFILE,
    _main_reply_keyboard,
)
from app.config import Settings
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    BOOKING_STATUS_CAME,
    BOOKING_STATUS_CONFIRMED,
    Booking,
    Client,
    Master,
    ReminderState,
    Service,
)
from app.scheduler import REMINDER_CLIENT_2H, _send_client_reminders

BOT_TOKEN = "TEST_TOKEN_AUDIT_LOW"


def _settings(webapp_url: str = "") -> Settings:
    return Settings(
        bot_token=BOT_TOKEN,
        database_url="sqlite+aiosqlite:///:memory:",
        api_host="127.0.0.1",
        api_port=8000,
        webapp_url=webapp_url,
        webapp_dist_dir="",
        telegram_proxy_url="",
        scheduler_interval_seconds=60,
        default_work_start=(10, 0),
        default_work_end=(20, 0),
        default_slot_step_minutes=30,
        default_timezone="UTC",
        admin_tg_ids=(777,),
        admin_contact_url="tg://user?id=777",
        become_master_conditions="",
    )


def _signed_init_data(
    tg_user_id: int = 777,
    *,
    extra_pairs: list[tuple[str, str]] | None = None,
    bot_token: str = BOT_TOKEN,
) -> str:
    user = json.dumps(
        {
            "id": tg_user_id,
            "first_name": "Audit",
            "last_name": "",
            "username": "audituser",
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
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check.encode(), hashlib.sha256
    ).hexdigest()
    encoded = urlencode(fields)
    if extra_pairs:
        encoded += "&" + urlencode(extra_pairs)
    return encoded


# ---------------------------------------------------------------------------
# Fix #9 — _master_create_locks bounded LRU
# ---------------------------------------------------------------------------


def test_lock_for_caches_per_user() -> None:
    """Calling ``_lock_for`` twice for the same user returns the same lock."""
    _master_create_locks.clear()
    lock_a = _lock_for(10_000_001)
    lock_b = _lock_for(10_000_001)
    assert lock_a is lock_b
    assert isinstance(lock_a, asyncio.Lock)


def test_lock_for_evicts_oldest_past_cap() -> None:
    """Bug #9 regression: the lock dict must not grow without bound.

    Push ``_LOCK_CAP + 5`` distinct users into the cache and verify only
    ``_LOCK_CAP`` entries remain. The first user that was inserted must be
    the one evicted (LRU order).
    """
    _master_create_locks.clear()
    first_user = 20_000_000
    last_user = first_user + _LOCK_CAP + 5
    for uid in range(first_user, last_user):
        _lock_for(uid)
    assert len(_master_create_locks) == _LOCK_CAP, (
        f"expected {_LOCK_CAP} live locks, got {len(_master_create_locks)}"
    )
    # The first 5 users inserted should be evicted in FIFO order; the last
    # _LOCK_CAP must still be present.
    evicted = set(range(first_user, first_user + 5))
    present = set(_master_create_locks.keys())
    assert evicted.isdisjoint(present), (
        f"expected {evicted} to be evicted but found {evicted & present} still present"
    )
    _master_create_locks.clear()


def test_lock_for_moves_recently_used_to_end() -> None:
    """LRU semantics: touching an existing key bumps it to the end."""
    _master_create_locks.clear()
    _lock_for(30_000_001)
    _lock_for(30_000_002)
    _lock_for(30_000_003)
    # Touch the first one.
    _lock_for(30_000_001)
    keys = list(_master_create_locks.keys())
    assert keys[-1] == 30_000_001, (
        f"expected 30_000_001 at the end after touch, got order {keys}"
    )
    _master_create_locks.clear()


# ---------------------------------------------------------------------------
# Fix #13 — parse_init_data must reject duplicate keys
# ---------------------------------------------------------------------------


def test_parse_init_data_rejects_duplicate_hash_keys() -> None:
    """Bug #13: ``hash=A&...&hash=B`` must be rejected, not silently merged."""
    valid = _signed_init_data()
    # Append a second hash with a junk value — the signed prefix is correct,
    # but a second 'hash' must trip the duplicate-key guard.
    forged = valid + "&hash=deadbeef"
    with pytest.raises(InitDataError, match="duplicate key in initData"):
        parse_init_data(forged, BOT_TOKEN)


def test_parse_init_data_rejects_duplicate_user_key_smuggling() -> None:
    """Bug #13 (security): ``user=evil&user=victim`` must be rejected.

    Without the guard, ``dict(parse_qsl(...))`` keeps the *last* ``user``
    value while the request as a whole still carries both — i.e. signing
    the data-check string for the second value but transmitting both
    payloads. We reject before either side gets a chance to be confused.
    """
    valid = _signed_init_data()
    forged = valid + "&" + urlencode([("user", '{"id":1,"first_name":"Evil"}')])
    with pytest.raises(InitDataError, match="duplicate key in initData"):
        parse_init_data(forged, BOT_TOKEN)


def test_parse_init_data_accepts_unique_keys() -> None:
    """Sanity: a normal, well-formed initData must still pass."""
    init = parse_init_data(_signed_init_data(), BOT_TOKEN)
    assert init.user.id == 777


# ---------------------------------------------------------------------------
# Fix #15 — role-aware reply keyboard
# ---------------------------------------------------------------------------


def _kb_rows(master: Master | None) -> list[list[str]]:
    kb = _main_reply_keyboard(master)
    return [[b.text for b in row] for row in kb.keyboard]


def _make_master(
    *, tg_user_id: int = 1, is_master: bool = False, is_admin: bool = False
) -> Master:
    return Master(
        tg_user_id=tg_user_id,
        tg_chat_id=tg_user_id,
        slug=f"slug-{tg_user_id}",
        display_name="X",
        is_master=is_master,
        is_admin=is_admin,
        timezone="UTC",
        work_start_minutes=600,
        work_end_minutes=1200,
        slot_step_minutes=30,
    )


def test_main_reply_keyboard_for_regular_user_shows_become_master() -> None:
    """Bug #15: a non-master, non-admin user must see the "Стать мастером" CTA."""
    rows = _kb_rows(_make_master(is_master=False, is_admin=False))
    assert rows[0] == [BTN_OPEN_APP, BTN_PROFILE]
    assert BTN_BECOME_MASTER in rows[1]
    assert BTN_ADMIN_PANEL not in [b for row in rows for b in row]


def test_main_reply_keyboard_for_admin_shows_admin_button() -> None:
    rows = _kb_rows(_make_master(is_master=False, is_admin=True))
    assert rows[0] == [BTN_OPEN_APP, BTN_PROFILE]
    assert BTN_ADMIN_PANEL in rows[1]
    # Admin doesn't need the "Стать мастером" CTA — they can self-promote.
    assert BTN_BECOME_MASTER not in [b for row in rows for b in row]


def test_main_reply_keyboard_for_plain_master_is_minimal() -> None:
    rows = _kb_rows(_make_master(is_master=True, is_admin=False))
    assert rows[0] == [BTN_OPEN_APP, BTN_PROFILE]
    assert rows[1] == [BTN_HELP]


def test_main_reply_keyboard_without_master_keeps_old_layout() -> None:
    """Back-compat: callers that don't know the role get the minimal layout."""
    rows = _kb_rows(None)
    assert rows == [[BTN_OPEN_APP, BTN_PROFILE], [BTN_HELP]]


# ---------------------------------------------------------------------------
# Fix #16 — no localhost in production CORS
# ---------------------------------------------------------------------------


def test_resolve_origins_omits_localhost_when_webapp_url_set() -> None:
    """Bug #16: with a deployed front-end configured, localhost must be out."""
    origins = _resolve_origins(_settings(webapp_url="https://app.example.com"))
    assert origins == ["https://app.example.com"]
    assert "http://localhost:5173" not in origins
    assert "http://127.0.0.1:5173" not in origins


def test_resolve_origins_keeps_localhost_in_dev() -> None:
    """Local dev (no WEBAPP_URL) must still allow Vite's localhost."""
    origins = _resolve_origins(_settings(webapp_url=""))
    assert "http://localhost:5173" in origins
    assert "http://127.0.0.1:5173" in origins


# ---------------------------------------------------------------------------
# Fix #17 — scheduler uses a single LEFT JOIN, not N+1 SELECTs
# ---------------------------------------------------------------------------


class _NullNotifier:
    """Drop-in notifier that records what would have been sent."""

    def __init__(self) -> None:
        self.sent_reminders: list[tuple[int, int]] = []

    async def notify_client_reminder(
        self, *, client: Client, booking: Booking, service: Service,
        master: Master, hours_until: int,
    ) -> None:
        self.sent_reminders.append((booking.id, hours_until))


async def _seed_booking_with_reminder_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hours_until: int,
    already_reminded: bool,
) -> int:
    async with session_factory() as s:
        m = Master(
            tg_user_id=1,
            tg_chat_id=1,
            slug="m1",
            display_name="M",
            is_master=True,
            timezone="UTC",
        )
        s.add(m)
        await s.flush()
        c = Client(master_id=m.id, name="Alice", phone="+7000", tg_user_id=99)
        s.add(c)
        await s.flush()
        svc = Service(master_id=m.id, name="Cut", price=100, duration_minutes=30)
        s.add(svc)
        await s.flush()
        when = datetime.utcnow() + timedelta(hours=hours_until, minutes=2)
        b = Booking(
            master_id=m.id,
            client_id=c.id,
            service_id=svc.id,
            starts_at=when,
            ends_at=when + timedelta(minutes=30),
            status=BOOKING_STATUS_CONFIRMED,
            price_snapshot=100,
            source="master",
        )
        s.add(b)
        await s.flush()
        if already_reminded:
            s.add(ReminderState(booking_id=b.id, kind=REMINDER_CLIENT_2H))
        await s.commit()
        return b.id


@pytest.mark.asyncio
async def test_scheduler_fires_when_no_reminder_state(engine: AsyncEngine) -> None:
    """Bug #17 regression: bookings without a prior ReminderState get notified."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    booking_id = await _seed_booking_with_reminder_state(
        session_factory, hours_until=2, already_reminded=False
    )
    notifier = _NullNotifier()
    await _send_client_reminders(
        session_factory,
        notifier,  # type: ignore[arg-type]
        kind=REMINDER_CLIENT_2H,
        hours_until=2,
        window_minutes=15,
    )
    assert notifier.sent_reminders == [(booking_id, 2)]


@pytest.mark.asyncio
async def test_scheduler_skips_when_reminder_state_exists(
    engine: AsyncEngine,
) -> None:
    """Bug #17 regression: the LEFT JOIN must filter out already-reminded rows."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed_booking_with_reminder_state(
        session_factory, hours_until=2, already_reminded=True
    )
    notifier = _NullNotifier()
    await _send_client_reminders(
        session_factory,
        notifier,  # type: ignore[arg-type]
        kind=REMINDER_CLIENT_2H,
        hours_until=2,
        window_minutes=15,
    )
    assert notifier.sent_reminders == [], (
        "scheduler must not re-fire a reminder that already has a "
        "ReminderState row"
    )


# ---------------------------------------------------------------------------
# Fix N9 — DELETE /api/clients/{id} must 409 when bookings exist
# ---------------------------------------------------------------------------


async def _seed_master_client_booking(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    with_booking: bool,
) -> tuple[int, int]:
    async with session_factory() as s:
        m = Master(
            tg_user_id=777,
            tg_chat_id=777,
            slug="m-n9",
            display_name="N9",
            is_master=True,
            is_admin=True,
            timezone="UTC",
        )
        s.add(m)
        await s.flush()
        c = Client(master_id=m.id, name="Bob", phone="+71111")
        s.add(c)
        await s.flush()
        if with_booking:
            svc = Service(master_id=m.id, name="Cut", price=100, duration_minutes=30)
            s.add(svc)
            await s.flush()
            when = datetime.utcnow() + timedelta(days=2)
            b = Booking(
                master_id=m.id,
                client_id=c.id,
                service_id=svc.id,
                starts_at=when,
                ends_at=when + timedelta(minutes=30),
                status=BOOKING_STATUS_CAME,
                price_snapshot=100,
                source="master",
            )
            s.add(b)
        await s.commit()
        return m.id, c.id


@pytest.mark.asyncio
async def test_delete_client_with_bookings_rejected_409(engine: AsyncEngine) -> None:
    """Bug N9: DELETE /api/clients/{id} must 409 when there are bookings."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _, client_id = await _seed_master_client_booking(
        session_factory, with_booking=True
    )
    app = create_api_app(
        settings=_settings(), session_factory=session_factory, notifier=None
    )
    headers = {"Authorization": f"tma {_signed_init_data(tg_user_id=777)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http:
        r = await http.delete(f"/api/clients/{client_id}", headers=headers)
    assert r.status_code == 409, (
        f"expected 409 when deleting client with bookings, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert "booking" in body["detail"].lower()


@pytest.mark.asyncio
async def test_delete_client_without_bookings_succeeds(
    engine: AsyncEngine,
) -> None:
    """Sanity: the 409 guard does not block deleting clients without bookings."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _, client_id = await _seed_master_client_booking(
        session_factory, with_booking=False
    )
    app = create_api_app(
        settings=_settings(), session_factory=session_factory, notifier=None
    )
    headers = {"Authorization": f"tma {_signed_init_data(tg_user_id=777)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http:
        r = await http.delete(f"/api/clients/{client_id}", headers=headers)
    assert r.status_code == 204, (
        f"expected 204 when deleting client without bookings, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# Sanity check: ACTIVE_BOOKING_STATUSES sentinel still imported cleanly.
# (Keeps the module-level imports from being silently broken by a refactor.)
# ---------------------------------------------------------------------------


def test_active_statuses_sentinel_imported() -> None:
    assert BOOKING_STATUS_CONFIRMED in ACTIVE_BOOKING_STATUSES
