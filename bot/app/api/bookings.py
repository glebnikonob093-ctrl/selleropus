from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AppState, get_app_state, get_current_active_master, get_session
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    BOOKING_STATUS_CAME,
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_NEW,
    BOOKING_STATUS_NO_SHOW,
    BOOKING_STATUSES,
    Booking,
    Client,
    Master,
    Service,
)

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


class BookingOut(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    status: str
    source: str
    notes: str | None
    price_snapshot: int

    client_id: int
    client_name: str
    client_phone: str | None
    client_tg_username: str | None

    service_id: int
    service_name: str
    service_duration_minutes: int


def _booking_out(booking: Booking, client: Client, service: Service) -> BookingOut:
    return BookingOut(
        id=booking.id,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        status=booking.status,
        source=booking.source,
        notes=booking.notes,
        price_snapshot=booking.price_snapshot,
        client_id=client.id,
        client_name=client.name,
        client_phone=client.phone,
        client_tg_username=client.tg_username,
        service_id=service.id,
        service_name=service.name,
        service_duration_minutes=service.duration_minutes,
    )


class BookingCreate(BaseModel):
    client_id: int | None = None
    new_client_name: str | None = Field(default=None, max_length=120)
    new_client_phone: str | None = Field(default=None, max_length=40)
    new_client_tg_username: str | None = Field(default=None, max_length=64)

    service_id: int
    starts_at: datetime
    notes: str | None = None
    status: str = BOOKING_STATUS_CONFIRMED


class BookingUpdate(BaseModel):
    starts_at: datetime | None = None
    status: str | None = None
    notes: str | None = None
    service_id: int | None = None


def _master_local_today_window(master: Master) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` for "today" as the master perceives it.

    All booking ``starts_at`` values are stored as naive datetimes in the
    master's local wall clock (see the ``# naive UTC`` model comment — it's
    misleading; in practice the API stores whatever the master sends).
    So "today" must be anchored to the master's local midnight, not the
    server's UTC midnight. Before this fix a Moscow master at 02:00 local
    (= 23:00 UTC previous day) would see the *previous* UTC day's bookings
    on the "Сегодня" tab until 03:00 local.
    """
    try:
        tz = ZoneInfo(master.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz).replace(tzinfo=None)
    day_start = datetime(now_local.year, now_local.month, now_local.day)
    return day_start, day_start + timedelta(days=1)


async def _list_bookings(
    session: AsyncSession,
    master: Master,
    *,
    starts_from: datetime | None,
    ends_before: datetime | None,
    status: str | None,
) -> list[BookingOut]:
    stmt = (
        select(Booking, Client, Service)
        .join(Client, Client.id == Booking.client_id)
        .join(Service, Service.id == Booking.service_id)
        .where(Booking.master_id == master.id)
        .order_by(Booking.starts_at)
    )
    if starts_from is not None:
        stmt = stmt.where(Booking.starts_at >= starts_from)
    if ends_before is not None:
        stmt = stmt.where(Booking.starts_at < ends_before)
    if status:
        if status not in BOOKING_STATUSES and status != "active":
            raise HTTPException(status_code=400, detail="unknown status")
        if status == "active":
            stmt = stmt.where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        else:
            stmt = stmt.where(Booking.status == status)

    res = await session.execute(stmt)
    return [_booking_out(b, c, s) for (b, c, s) in res.all()]


@router.get("", response_model=list[BookingOut])
async def list_bookings(
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status: str | None = None,
) -> list[BookingOut]:
    return await _list_bookings(
        session,
        master,
        starts_from=date_from,
        ends_before=date_to,
        status=status,
    )


@router.get("/today", response_model=list[BookingOut])
async def list_today(
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> list[BookingOut]:
    start, end = _master_local_today_window(master)
    return await _list_bookings(
        session, master, starts_from=start, ends_before=end, status="active"
    )


@router.post("", response_model=BookingOut, status_code=201)
async def create_booking(
    payload: BookingCreate,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
    app_state: AppState = Depends(get_app_state),
) -> BookingOut:
    if payload.status not in BOOKING_STATUSES:
        raise HTTPException(status_code=400, detail="unknown status")

    res = await session.execute(
        select(Service).where(
            Service.id == payload.service_id, Service.master_id == master.id
        )
    )
    service = res.scalar_one_or_none()
    if service is None:
        raise HTTPException(status_code=404, detail="service not found")

    if payload.client_id is not None:
        res = await session.execute(
            select(Client).where(
                Client.id == payload.client_id, Client.master_id == master.id
            )
        )
        client = res.scalar_one_or_none()
        if client is None:
            raise HTTPException(status_code=404, detail="client not found")
    else:
        if not payload.new_client_name:
            raise HTTPException(
                status_code=400,
                detail="either client_id or new_client_name is required",
            )
        client = Client(
            master_id=master.id,
            name=payload.new_client_name.strip(),
            phone=(payload.new_client_phone or "").strip() or None,
            tg_username=(payload.new_client_tg_username or "").strip().lstrip("@") or None,
        )
        session.add(client)
        await session.flush()

    starts_at = payload.starts_at.replace(tzinfo=None, microsecond=0)
    ends_at = starts_at + timedelta(minutes=service.duration_minutes)

    overlap_stmt = (
        select(Booking.id)
        .where(Booking.master_id == master.id)
        .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .where(Booking.starts_at < ends_at)
        .where(Booking.ends_at > starts_at)
    )
    overlapping = (await session.execute(overlap_stmt)).first()
    if overlapping is not None:
        raise HTTPException(status_code=409, detail="time slot overlaps another booking")

    booking = Booking(
        master_id=master.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=payload.status,
        source="master",
        price_snapshot=service.price,
        notes=payload.notes,
    )
    session.add(booking)
    await session.flush()

    return _booking_out(booking, client, service)


async def _get_owned_booking(
    session: AsyncSession, master: Master, booking_id: int
) -> tuple[Booking, Client, Service]:
    res = await session.execute(
        select(Booking, Client, Service)
        .join(Client, Client.id == Booking.client_id)
        .join(Service, Service.id == Booking.service_id)
        .where(Booking.id == booking_id, Booking.master_id == master.id)
    )
    row = res.first()
    if row is None:
        raise HTTPException(status_code=404, detail="booking not found")
    return row  # type: ignore[return-value]


@router.patch("/{booking_id}", response_model=BookingOut)
async def update_booking(
    booking_id: int,
    payload: BookingUpdate,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
    app_state: AppState = Depends(get_app_state),
) -> BookingOut:
    booking, client, service = await _get_owned_booking(session, master, booking_id)
    old_status = booking.status

    if payload.service_id is not None and payload.service_id != booking.service_id:
        res = await session.execute(
            select(Service).where(
                Service.id == payload.service_id, Service.master_id == master.id
            )
        )
        new_service = res.scalar_one_or_none()
        if new_service is None:
            raise HTTPException(status_code=404, detail="service not found")
        service = new_service
        booking.service_id = service.id
        booking.price_snapshot = service.price

    if payload.starts_at is not None:
        booking.starts_at = payload.starts_at.replace(tzinfo=None, microsecond=0)

    booking.ends_at = booking.starts_at + timedelta(minutes=service.duration_minutes)

    # If the booking is (or stays) in an active state, reject any time/service
    # edit that would overlap with another active booking owned by the same
    # master. ``create_booking`` already does this — without the same check
    # here, a master can accidentally double-book themselves by dragging a
    # booking onto an occupied slot or by switching to a longer service.
    target_status = payload.status if payload.status is not None else booking.status
    if target_status in ACTIVE_BOOKING_STATUSES:
        overlap_stmt = (
            select(Booking.id)
            .where(Booking.master_id == master.id)
            .where(Booking.id != booking.id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
            .where(Booking.starts_at < booking.ends_at)
            .where(Booking.ends_at > booking.starts_at)
        )
        overlapping = (await session.execute(overlap_stmt)).first()
        if overlapping is not None:
            raise HTTPException(
                status_code=409, detail="time slot overlaps another booking"
            )

    if payload.notes is not None:
        booking.notes = payload.notes or None

    if payload.status is not None:
        if payload.status not in BOOKING_STATUSES:
            raise HTTPException(status_code=400, detail="unknown status")
        booking.status = payload.status
        if payload.status == BOOKING_STATUS_CAME:
            client.last_visit_at = booking.ends_at
        elif payload.status in (
            BOOKING_STATUS_CANCELLED,
            BOOKING_STATUS_NO_SHOW,
            BOOKING_STATUS_NEW,
            BOOKING_STATUS_CONFIRMED,
        ):
            # do nothing extra
            pass

    # Notify the client when the master flips status to confirmed/cancelled.
    # The notifier already handles the "client has no tg_user_id" case and
    # swallows transport errors via _safe_send.
    notifier = app_state.notifier
    if notifier is not None and payload.status is not None and old_status != booking.status:
        try:
            await notifier.notify_status_change(  # type: ignore[attr-defined]
                client=client,
                booking=booking,
                service=service,
                master=master,
                old_status=old_status,
                new_status=booking.status,
            )
        except Exception:  # pragma: no cover - notification failures should not break update
            pass

    return _booking_out(booking, client, service)


@router.delete("/{booking_id}", status_code=204, response_class=Response)
async def delete_booking(
    booking_id: int,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> Response:
    booking, _client, _service = await _get_owned_booking(session, master, booking_id)
    await session.delete(booking)
    return Response(status_code=204)
