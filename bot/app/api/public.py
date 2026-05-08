from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AppState,
    get_app_state,
    get_optional_init_data,
    get_session,
)
from app.auth import InitData
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    BOOKING_STATUS_NEW,
    Booking,
    Service,
)
from app.repos import (
    find_or_create_client,
    get_master_by_slug,
    list_active_services,
    list_bookings_in_window,
)
from app.slots import TimeRange, generate_day_slots

router = APIRouter(prefix="/api/public", tags=["public"])


class PublicMaster(BaseModel):
    slug: str
    display_name: str


class PublicService(BaseModel):
    id: int
    name: str
    price: int
    duration_minutes: int


class PublicMasterPage(BaseModel):
    master: PublicMaster
    services: list[PublicService]


@router.get("/{slug}", response_model=PublicMasterPage)
async def get_public_master(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> PublicMasterPage:
    master = await get_master_by_slug(session, slug)
    if master is None:
        raise HTTPException(status_code=404, detail="master not found")
    services = await list_active_services(session, master.id)
    return PublicMasterPage(
        master=PublicMaster(slug=master.slug, display_name=master.display_name),
        services=[
            PublicService(
                id=s.id,
                name=s.name,
                price=s.price,
                duration_minutes=s.duration_minutes,
            )
            for s in services
        ],
    )


@router.get("/{slug}/availability", response_model=list[datetime])
async def get_availability(
    slug: str,
    service_id: int,
    day_str: str = Field(..., description="ISO date YYYY-MM-DD", alias="date"),
    session: AsyncSession = Depends(get_session),
) -> list[datetime]:
    master = await get_master_by_slug(session, slug)
    if master is None:
        raise HTTPException(status_code=404, detail="master not found")

    res = await session.execute(
        select(Service).where(Service.id == service_id, Service.master_id == master.id)
    )
    service = res.scalar_one_or_none()
    if service is None or not service.is_active:
        raise HTTPException(status_code=404, detail="service not found")

    try:
        day = date.fromisoformat(day_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid date") from exc

    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    bookings = await list_bookings_in_window(
        session, master.id, day_start, day_end, only_active=True
    )
    booked_ranges = [
        TimeRange(starts_at=b.starts_at, ends_at=b.ends_at) for b in bookings
    ]
    return generate_day_slots(
        day=day,
        work_start_minutes=master.work_start_minutes,
        work_end_minutes=master.work_end_minutes,
        slot_step_minutes=master.slot_step_minutes,
        service_duration_minutes=service.duration_minutes,
        booked=booked_ranges,
        now=datetime.utcnow(),
    )


class PublicBookingCreate(BaseModel):
    service_id: int
    starts_at: datetime
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=40)


class PublicBookingResult(BaseModel):
    booking_id: int
    starts_at: datetime
    ends_at: datetime
    status: str
    master_display_name: str


@router.post("/{slug}/bookings", response_model=PublicBookingResult, status_code=201)
async def create_public_booking(
    slug: str,
    payload: PublicBookingCreate,
    session: AsyncSession = Depends(get_session),
    init_data: InitData | None = Depends(get_optional_init_data),
    app_state: AppState = Depends(get_app_state),
) -> PublicBookingResult:
    master = await get_master_by_slug(session, slug)
    if master is None:
        raise HTTPException(status_code=404, detail="master not found")

    res = await session.execute(
        select(Service).where(Service.id == payload.service_id, Service.master_id == master.id)
    )
    service = res.scalar_one_or_none()
    if service is None or not service.is_active:
        raise HTTPException(status_code=404, detail="service not found")

    starts_at = payload.starts_at.replace(tzinfo=None, microsecond=0)
    ends_at = starts_at + timedelta(minutes=service.duration_minutes)

    if starts_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="cannot book in the past")

    overlap_stmt = (
        select(Booking.id)
        .where(Booking.master_id == master.id)
        .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .where(Booking.starts_at < ends_at)
        .where(Booking.ends_at > starts_at)
    )
    overlapping = (await session.execute(overlap_stmt)).first()
    if overlapping is not None:
        raise HTTPException(status_code=409, detail="time slot is no longer available")

    tg_user_id: int | None = None
    tg_username: str | None = None
    if init_data is not None and init_data.user.id != master.tg_user_id:
        tg_user_id = init_data.user.id
        tg_username = init_data.user.username or None

    client = await find_or_create_client(
        session,
        master.id,
        name=payload.name.strip(),
        phone=(payload.phone or "").strip() or None,
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
        source="public",
        price_snapshot=service.price,
    )
    session.add(booking)
    await session.flush()

    notifier = app_state.notifier
    if notifier is not None:
        try:
            await notifier.notify_master_new_booking(  # type: ignore[attr-defined]
                master=master, booking=booking, client=client, service=service
            )
        except Exception:  # pragma: no cover - notification failures should not break booking
            pass

    return PublicBookingResult(
        booking_id=booking.id,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        status=booking.status,
        master_display_name=master.display_name,
    )
