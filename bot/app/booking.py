"""Shared booking helpers used by both the public HTTP API and the in-bot flow.

Keeping slot computation and booking creation in one place means the Mini App
and the conversational bot flow apply exactly the same availability and
double-booking rules.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ACTIVE_BOOKING_STATUSES,
    BOOKING_STATUS_NEW,
    Booking,
    Client,
    Master,
    Service,
)
from app.repos import find_or_create_client, list_bookings_in_window
from app.slots import TimeRange, generate_day_slots


class BookingError(Exception):
    """Base class for booking failures that map to user-facing messages."""


class PastBookingError(BookingError):
    """The requested start time is in the past."""


class SlotUnavailableError(BookingError):
    """The requested slot overlaps an existing active booking."""


async def available_day_slots(
    session: AsyncSession,
    master: Master,
    service: Service,
    day: date,
    *,
    now: datetime | None = None,
) -> list[datetime]:
    """Free slot start times (naive UTC) for `service` on `day`."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    bookings = await list_bookings_in_window(
        session, master.id, day_start, day_end, only_active=True
    )
    booked = [TimeRange(starts_at=b.starts_at, ends_at=b.ends_at) for b in bookings]
    return generate_day_slots(
        day=day,
        work_start_minutes=master.work_start_minutes,
        work_end_minutes=master.work_end_minutes,
        slot_step_minutes=master.slot_step_minutes,
        service_duration_minutes=service.duration_minutes,
        booked=booked,
        now=now if now is not None else datetime.utcnow(),
    )


async def create_client_booking(
    session: AsyncSession,
    *,
    master: Master,
    service: Service,
    starts_at: datetime,
    name: str,
    phone: str | None = None,
    tg_user_id: int | None = None,
    tg_username: str | None = None,
    source: str,
    now: datetime | None = None,
) -> tuple[Booking, Client]:
    """Create a booking for a client, enforcing past/overlap rules.

    Raises `PastBookingError` or `SlotUnavailableError` on conflict. The caller
    owns the transaction (commit) and any notification side effects.
    """
    now = now if now is not None else datetime.utcnow()
    starts_at = starts_at.replace(tzinfo=None, microsecond=0)
    ends_at = starts_at + timedelta(minutes=service.duration_minutes)

    if starts_at < now:
        raise PastBookingError

    overlapping = await list_bookings_in_window(
        session, master.id, starts_at, ends_at, only_active=True
    )
    if overlapping:
        raise SlotUnavailableError

    client = await find_or_create_client(
        session,
        master.id,
        name=name.strip() or "Без имени",
        phone=(phone or "").strip() or None,
        tg_username=tg_username,
        tg_user_id=tg_user_id,
    )

    booking = Booking(
        master_id=master.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=BOOKING_STATUS_NEW,
        source=source,
        price_snapshot=service.price,
    )
    session.add(booking)
    await session.flush()
    return booking, client


# Re-exported for callers that build their own overlap queries/messages.
__all__ = [
    "ACTIVE_BOOKING_STATUSES",
    "BookingError",
    "PastBookingError",
    "SlotUnavailableError",
    "available_day_slots",
    "create_client_booking",
]
