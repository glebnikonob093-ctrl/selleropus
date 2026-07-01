from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking import (
    PastBookingError,
    SlotUnavailableError,
    available_day_slots,
    create_client_booking,
)
from app.models import Booking, Client, Master, Service


async def _make_master_service(session: AsyncSession) -> tuple[Master, Service]:
    master = Master(
        tg_user_id=1,
        tg_chat_id=1,
        slug="anna",
        display_name="Anna",
        work_start_minutes=10 * 60,
        work_end_minutes=20 * 60,
        slot_step_minutes=60,
    )
    session.add(master)
    await session.flush()
    service = Service(master_id=master.id, name="Маникюр", price=1000, duration_minutes=60)
    session.add(service)
    await session.flush()
    return master, service


def _future_slot(days: int = 1, hour: int = 11) -> datetime:
    d = (datetime.utcnow() + timedelta(days=days)).date()
    return datetime(d.year, d.month, d.day, hour, 0)


async def test_create_client_booking_success(session: AsyncSession) -> None:
    master, service = await _make_master_service(session)
    starts_at = _future_slot()
    booking, client = await create_client_booking(
        session,
        master=master,
        service=service,
        starts_at=starts_at,
        name="Иван",
        phone="+70000000000",
        tg_user_id=999,
        tg_username="ivan",
        source="bot",
    )
    assert booking.source == "bot"
    assert booking.starts_at == starts_at
    assert booking.ends_at == starts_at + timedelta(minutes=60)
    assert booking.price_snapshot == 1000
    assert client.name == "Иван"
    assert client.tg_user_id == 999


async def test_create_client_booking_rejects_past(session: AsyncSession) -> None:
    master, service = await _make_master_service(session)
    with pytest.raises(PastBookingError):
        await create_client_booking(
            session,
            master=master,
            service=service,
            starts_at=datetime.utcnow() - timedelta(hours=1),
            name="Иван",
            source="bot",
        )


async def test_create_client_booking_rejects_overlap(session: AsyncSession) -> None:
    master, service = await _make_master_service(session)
    starts_at = _future_slot()
    await create_client_booking(
        session, master=master, service=service, starts_at=starts_at, name="A", source="bot"
    )
    with pytest.raises(SlotUnavailableError):
        await create_client_booking(
            session,
            master=master,
            service=service,
            starts_at=starts_at + timedelta(minutes=30),
            name="B",
            source="public",
        )


async def test_available_day_slots_excludes_booked(session: AsyncSession) -> None:
    master, service = await _make_master_service(session)
    starts_at = _future_slot(hour=11)
    day = starts_at.date()
    before = await available_day_slots(session, master, service, day)
    assert starts_at in before
    await create_client_booking(
        session, master=master, service=service, starts_at=starts_at, name="A", source="bot"
    )
    after = await available_day_slots(session, master, service, day)
    assert starts_at not in after
    assert len(after) == len(before) - 1


async def test_available_day_slots_hides_past_today(session: AsyncSession) -> None:
    master, service = await _make_master_service(session)
    today = datetime.utcnow().date()
    # A fixed "now" late in the working day leaves no remaining slots today.
    slots = await available_day_slots(
        session, master, service, today, now=datetime(today.year, today.month, today.day, 23, 0)
    )
    assert slots == []


async def test_booking_overlap_uses_active_only(session: AsyncSession) -> None:
    master, service = await _make_master_service(session)
    starts_at = _future_slot()
    other = Client(master_id=master.id, name="Прошлый")
    session.add(other)
    await session.flush()
    # A cancelled booking should not block the slot.
    cancelled = Booking(
        master_id=master.id,
        client_id=other.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=60),
        status="cancelled",
        source="bot",
        price_snapshot=service.price,
    )
    session.add(cancelled)
    await session.flush()
    booking, _ = await create_client_booking(
        session, master=master, service=service, starts_at=starts_at, name="A", source="bot"
    )
    assert booking.id is not None
