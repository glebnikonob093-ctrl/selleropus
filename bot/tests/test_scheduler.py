from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import scheduler
from app.models import (
    BOOKING_STATUS_NEW,
    Booking,
    Client,
    Master,
    MasterDailySummary,
    ReminderState,
    Service,
)


class _RecordingNotifier:
    def __init__(self, *, delivered: bool = True) -> None:
        self.delivered = delivered
        self.morning_calls: list[list] = []
        self.greeted_master_ids: list[int] = []

    async def notify_master_morning_summary(self, *, master, bookings) -> bool:
        self.morning_calls.append(list(bookings))
        self.greeted_master_ids.append(master.id)
        return self.delivered


class _FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls) -> datetime:  # type: ignore[override]
        # 05:05 UTC = 08:05 Europe/Moscow, inside the 08:00-08:14 window.
        return datetime(2026, 6, 30, 5, 5)


@pytest.fixture()
def _freeze_morning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "datetime", _FrozenDatetime)


async def _make_master(session: AsyncSession, tg_user_id: int, slug: str) -> Master:
    master = Master(
        tg_user_id=tg_user_id,
        tg_chat_id=tg_user_id,
        tg_username=slug,
        display_name=slug.title(),
        slug=slug,
        is_master=True,
    )
    session.add(master)
    await session.flush()
    return master


async def _count_markers(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(MasterDailySummary))
        ).scalar_one()


async def test_morning_greeting_sent_once_on_empty_day(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    async with session_factory() as session:
        await _make_master(session, tg_user_id=900, slug="empty")
        await session.commit()

    notifier = _RecordingNotifier()

    # First tick greets the master even with no bookings.
    await scheduler._send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]
    assert len(notifier.morning_calls) == 1
    assert notifier.morning_calls[0] == []  # empty-day greeting
    assert await _count_markers(session_factory) == 1

    # Second tick within the same morning must not greet again.
    await scheduler._send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]
    assert len(notifier.morning_calls) == 1
    assert await _count_markers(session_factory) == 1


async def test_morning_summary_includes_todays_bookings(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    async with session_factory() as session:
        master = await _make_master(session, tg_user_id=901, slug="busy")
        service = Service(master_id=master.id, name="Cut", price=1000, duration_minutes=60)
        session.add(service)
        client = Client(master_id=master.id, name="Bob")
        session.add(client)
        await session.flush()
        start = datetime(2026, 6, 30, 12, 0)
        session.add(
            Booking(
                master_id=master.id,
                client_id=client.id,
                service_id=service.id,
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                status=BOOKING_STATUS_NEW,
                price_snapshot=1000,
            )
        )
        await session.commit()

    notifier = _RecordingNotifier()
    await scheduler._send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]

    assert len(notifier.morning_calls) == 1
    assert len(notifier.morning_calls[0]) == 1
    assert await _count_markers(session_factory) == 1


async def test_already_greeted_master_skipped_others_still_greeted(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    async with session_factory() as session:
        done = await _make_master(session, tg_user_id=902, slug="done")
        pending = await _make_master(session, tg_user_id=903, slug="pending")
        session.add(MasterDailySummary(master_id=done.id, day=date(2026, 6, 30)))
        await session.commit()
        done_id, pending_id = done.id, pending.id

    notifier = _RecordingNotifier()
    await scheduler._send_morning_summaries(session_factory, notifier)  # type: ignore[arg-type]

    # The already-greeted master is skipped; the other is greeted exactly once.
    assert notifier.greeted_master_ids == [pending_id]
    assert done_id not in notifier.greeted_master_ids
    assert await _count_markers(session_factory) == 2


async def test_morning_greeting_not_marked_when_delivery_fails(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    async with session_factory() as session:
        await _make_master(session, tg_user_id=910, slug="blocked")
        await session.commit()

    # Swallowed Telegram error -> delivered=False -> not marked, retried later.
    failing = _RecordingNotifier(delivered=False)
    await scheduler._send_morning_summaries(session_factory, failing)  # type: ignore[arg-type]
    assert len(failing.morning_calls) == 1
    assert await _count_markers(session_factory) == 0

    # A later tick delivers and marks the day exactly once.
    ok = _RecordingNotifier()
    await scheduler._send_morning_summaries(session_factory, ok)  # type: ignore[arg-type]
    assert len(ok.morning_calls) == 1
    assert await _count_markers(session_factory) == 1


class _ClientReminderNotifier:
    def __init__(self, *, fail: bool = False, delivered: bool = True) -> None:
        self.fail = fail
        self.delivered = delivered
        self.sent_booking_ids: list[int] = []

    async def notify_client_reminder(
        self, *, client, booking, service, master, hours_until
    ) -> bool:
        self.sent_booking_ids.append(booking.id)
        if self.fail:
            raise RuntimeError("send failed")
        return self.delivered


async def _count_reminder_states(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(ReminderState))).scalar_one()


async def _make_booking_in_24h_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        master = await _make_master(session, tg_user_id=950, slug="rem")
        service = Service(master_id=master.id, name="Cut", price=1000, duration_minutes=60)
        session.add(service)
        client = Client(master_id=master.id, name="Bob")
        session.add(client)
        await session.flush()
        # Frozen now is 2026-06-30 05:05; 24h window is [+24h, +24h+60m).
        start = datetime(2026, 7, 1, 5, 30)
        booking = Booking(
            master_id=master.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            status=BOOKING_STATUS_NEW,
            price_snapshot=1000,
        )
        session.add(booking)
        await session.commit()
        return booking.id


async def test_client_reminder_marked_after_successful_send(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    await _make_booking_in_24h_window(session_factory)
    notifier = _ClientReminderNotifier()

    await scheduler._send_client_reminders(  # type: ignore[arg-type]
        session_factory,
        notifier,
        kind=scheduler.REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    assert len(notifier.sent_booking_ids) == 1
    assert await _count_reminder_states(session_factory) == 1

    # Second tick within the window must not re-send.
    await scheduler._send_client_reminders(  # type: ignore[arg-type]
        session_factory,
        notifier,
        kind=scheduler.REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    assert len(notifier.sent_booking_ids) == 1
    assert await _count_reminder_states(session_factory) == 1


async def test_client_reminder_failed_send_is_retried(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    await _make_booking_in_24h_window(session_factory)

    # A failed delivery must NOT be recorded as sent.
    failing = _ClientReminderNotifier(fail=True)
    await scheduler._send_client_reminders(  # type: ignore[arg-type]
        session_factory,
        failing,
        kind=scheduler.REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    assert len(failing.sent_booking_ids) == 1
    assert await _count_reminder_states(session_factory) == 0

    # A later tick retries and, on success, records exactly one marker.
    ok = _ClientReminderNotifier()
    await scheduler._send_client_reminders(  # type: ignore[arg-type]
        session_factory,
        ok,
        kind=scheduler.REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    assert len(ok.sent_booking_ids) == 1
    assert await _count_reminder_states(session_factory) == 1


async def test_client_reminder_undelivered_send_is_retried(
    session_factory: async_sessionmaker[AsyncSession], _freeze_morning: None
) -> None:
    await _make_booking_in_24h_window(session_factory)

    # A swallowed Telegram error surfaces as delivered=False -> not marked.
    undelivered = _ClientReminderNotifier(delivered=False)
    await scheduler._send_client_reminders(  # type: ignore[arg-type]
        session_factory,
        undelivered,
        kind=scheduler.REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    assert len(undelivered.sent_booking_ids) == 1
    assert await _count_reminder_states(session_factory) == 0

    # A later tick delivers and records exactly one marker.
    ok = _ClientReminderNotifier()
    await scheduler._send_client_reminders(  # type: ignore[arg-type]
        session_factory,
        ok,
        kind=scheduler.REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    assert len(ok.sent_booking_ids) == 1
    assert await _count_reminder_states(session_factory) == 1
