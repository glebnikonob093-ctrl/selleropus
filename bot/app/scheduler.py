"""APScheduler-based reminder loop.

Runs once per `SCHEDULER_INTERVAL_SECONDS` and:
* sends a 24h-out reminder to clients of every active booking
* sends a 2h-out reminder to clients
* at the start of each master's working day, sends them a summary

ReminderState rows make every send idempotent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import session_scope
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    Client,
    Master,
    ReminderState,
    Service,
)
from app.notifications import Notifier

log = logging.getLogger(__name__)

REMINDER_CLIENT_24H = "client_24h"
REMINDER_CLIENT_2H = "client_2h"
REMINDER_MASTER_MORNING = "master_morning"


async def _mark_sent(session: AsyncSession, booking_id: int, kind: str) -> bool:
    state = ReminderState(booking_id=booking_id, kind=kind)
    session.add(state)
    try:
        await session.flush()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def _send_client_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    kind: str,
    hours_until: int,
    window_minutes: int,
) -> None:
    """Send reminders for bookings whose start is in roughly `hours_until` hours.

    A booking qualifies if its `starts_at` is in the window
    `[now + hours_until, now + hours_until + window_minutes)` and we have not
    already sent this kind of reminder for it.
    """
    now = datetime.utcnow()
    target_start = now + timedelta(hours=hours_until)
    target_end = target_start + timedelta(minutes=window_minutes)

    async with session_scope(session_factory) as session:
        stmt = (
            select(Booking, Client, Service, Master)
            .join(Client, Client.id == Booking.client_id)
            .join(Service, Service.id == Booking.service_id)
            .join(Master, Master.id == Booking.master_id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
            .where(Booking.starts_at >= target_start)
            .where(Booking.starts_at < target_end)
        )
        rows = list((await session.execute(stmt)).all())

        for booking, client, service, master in rows:
            already = await session.execute(
                select(ReminderState.id).where(
                    ReminderState.booking_id == booking.id,
                    ReminderState.kind == kind,
                )
            )
            if already.scalar_one_or_none() is not None:
                continue

            if not await _mark_sent(session, booking.id, kind):
                continue

            try:
                await notifier.notify_client_reminder(
                    client=client,
                    booking=booking,
                    service=service,
                    master=master,
                    hours_until=hours_until,
                )
            except Exception:  # pragma: no cover - log and continue
                log.exception("reminder_send_failed booking_id=%s kind=%s", booking.id, kind)


async def _send_morning_summaries(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
) -> None:
    """Once per day, around each master's morning, send a summary of today's bookings.

    For the MVP we trigger when the local time is between 08:00 and 08:15 in the
    master's timezone. We fall back to UTC math to keep it dependency-free.
    """
    now = datetime.utcnow()
    if not (now.hour == 8 and now.minute < 15):
        return

    async with session_scope(session_factory) as session:
        masters = list((await session.execute(select(Master))).scalars())

        for master in masters:
            day_start = datetime(now.year, now.month, now.day)
            day_end = day_start + timedelta(days=1)
            sentinel_kind = f"{REMINDER_MASTER_MORNING}:{day_start.date().isoformat()}"

            already_q = await session.execute(
                select(ReminderState.id)
                .join(Booking, Booking.id == ReminderState.booking_id)
                .where(Booking.master_id == master.id)
                .where(ReminderState.kind == sentinel_kind)
                .limit(1)
            )
            if already_q.scalar_one_or_none() is not None:
                continue

            stmt = (
                select(Booking, Client, Service)
                .join(Client, Client.id == Booking.client_id)
                .join(Service, Service.id == Booking.service_id)
                .where(Booking.master_id == master.id)
                .where(Booking.starts_at >= day_start)
                .where(Booking.starts_at < day_end)
                .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
                .order_by(Booking.starts_at)
            )
            todays = [tuple(row) for row in (await session.execute(stmt)).all()]

            try:
                await notifier.notify_master_morning_summary(
                    master=master,
                    bookings=todays,  # type: ignore[arg-type]
                )
            except Exception:  # pragma: no cover
                log.exception("morning_summary_failed master_id=%s", master.id)
                continue

            if todays:
                first_booking_id = todays[0][0].id
                await _mark_sent(session, first_booking_id, sentinel_kind)


async def run_reminder_tick(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
) -> None:
    """One full pass of all reminder kinds. Safe to call concurrently with itself."""
    await _send_client_reminders(
        session_factory,
        notifier,
        kind=REMINDER_CLIENT_24H,
        hours_until=24,
        window_minutes=60,
    )
    await _send_client_reminders(
        session_factory,
        notifier,
        kind=REMINDER_CLIENT_2H,
        hours_until=2,
        window_minutes=15,
    )
    await _send_morning_summaries(session_factory, notifier)


def start_reminder_scheduler(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    interval_seconds: int,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_reminder_tick,
        trigger=IntervalTrigger(seconds=max(15, interval_seconds)),
        kwargs={"session_factory": session_factory, "notifier": notifier},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    return scheduler
