from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_master, get_session
from app.models import (
    BOOKING_STATUS_CAME,
    Booking,
    Client,
    Master,
    Service,
)
from app.repos import find_clients_to_return

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _master_now(master: Master) -> datetime:
    """Wall-clock "now" in the master's timezone, as a naive datetime."""
    try:
        tz = ZoneInfo(master.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).replace(tzinfo=None)


class TopServiceItem(BaseModel):
    service_id: int
    service_name: str
    bookings: int
    revenue: int


class StatsResponse(BaseModel):
    period: str
    starts_at: datetime
    ends_at: datetime
    revenue: int
    bookings_total: int
    bookings_came: int
    top_services: list[TopServiceItem]


def _period_window(master: Master, period: str) -> tuple[datetime, datetime]:
    """Compute the ``[start, end)`` window for a stats period.

    Anchored to the master's local wall clock — the day/week/month boundary
    must follow the master, not the server. A Moscow master asking for
    "today's revenue" at 02:00 local previously got the UTC day window,
    which excluded all bookings made between local midnight and 03:00.
    """
    now = _master_now(master)
    if period == "day":
        start = datetime(now.year, now.month, now.day)
        end = start + timedelta(days=1)
    elif period == "week":
        start_day = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
        start = start_day
        end = start + timedelta(days=7)
    elif period == "month":
        start = datetime(now.year, now.month, 1)
        if now.month == 12:
            end = datetime(now.year + 1, 1, 1)
        else:
            end = datetime(now.year, now.month + 1, 1)
    else:
        raise HTTPException(status_code=400, detail="period must be one of: day, week, month")
    return start, end


@router.get("", response_model=StatsResponse)
async def get_stats(
    period: str = "month",
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> StatsResponse:
    start, end = _period_window(master, period)

    total_q = select(func.count(Booking.id)).where(
        Booking.master_id == master.id,
        Booking.starts_at >= start,
        Booking.starts_at < end,
    )
    came_q = select(func.count(Booking.id)).where(
        Booking.master_id == master.id,
        Booking.status == BOOKING_STATUS_CAME,
        Booking.starts_at >= start,
        Booking.starts_at < end,
    )
    revenue_q = select(func.coalesce(func.sum(Booking.price_snapshot), 0)).where(
        Booking.master_id == master.id,
        Booking.status == BOOKING_STATUS_CAME,
        Booking.starts_at >= start,
        Booking.starts_at < end,
    )

    bookings_total = int((await session.execute(total_q)).scalar_one() or 0)
    bookings_came = int((await session.execute(came_q)).scalar_one() or 0)
    revenue = int((await session.execute(revenue_q)).scalar_one() or 0)

    top_q = (
        select(
            Service.id,
            Service.name,
            func.count(Booking.id),
            func.coalesce(func.sum(Booking.price_snapshot), 0),
        )
        .join(Booking, Booking.service_id == Service.id)
        .where(
            Booking.master_id == master.id,
            Booking.status == BOOKING_STATUS_CAME,
            Booking.starts_at >= start,
            Booking.starts_at < end,
        )
        .group_by(Service.id)
        .order_by(func.count(Booking.id).desc())
        .limit(5)
    )
    top_rows = (await session.execute(top_q)).all()
    top_services = [
        TopServiceItem(
            service_id=int(sid),
            service_name=str(sname),
            bookings=int(cnt or 0),
            revenue=int(rev or 0),
        )
        for sid, sname, cnt, rev in top_rows
    ]

    return StatsResponse(
        period=period,
        starts_at=start,
        ends_at=end,
        revenue=revenue,
        bookings_total=bookings_total,
        bookings_came=bookings_came,
        top_services=top_services,
    )


class ReturnClientItem(BaseModel):
    client_id: int
    name: str
    last_visit_at: datetime | None
    days_since: int | None


@router.get("/return-clients", response_model=list[ReturnClientItem])
async def get_return_clients(
    threshold_days: int = 30,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> list[ReturnClientItem]:
    clients: list[Client] = await find_clients_to_return(
        session, master.id, threshold_days=threshold_days
    )
    now = datetime.utcnow()
    return [
        ReturnClientItem(
            client_id=c.id,
            name=c.name,
            last_visit_at=c.last_visit_at,
            days_since=(now - c.last_visit_at).days if c.last_visit_at else None,
        )
        for c in clients
    ]
