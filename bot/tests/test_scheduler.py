from __future__ import annotations

from datetime import datetime, timedelta

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
    Service,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.morning_calls: list[list] = []

    async def notify_master_morning_summary(self, *, master, bookings) -> None:
        self.morning_calls.append(list(bookings))


class _FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls) -> datetime:  # type: ignore[override]
        return datetime(2026, 6, 30, 8, 5)


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
