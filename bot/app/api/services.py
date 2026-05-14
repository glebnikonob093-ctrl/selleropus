from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_master, get_session
from app.models import Master, Service

router = APIRouter(prefix="/api/services", tags=["services"])


class ServiceOut(BaseModel):
    id: int
    name: str
    price: int
    duration_minutes: int
    is_active: bool

    @classmethod
    def from_model(cls, svc: Service) -> ServiceOut:
        return cls(
            id=svc.id,
            name=svc.name,
            price=svc.price,
            duration_minutes=svc.duration_minutes,
            is_active=svc.is_active,
        )


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price: int = Field(ge=0)
    duration_minutes: int = Field(ge=5, le=24 * 60)
    is_active: bool = True


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    price: int | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    is_active: bool | None = None


@router.get("", response_model=list[ServiceOut])
async def list_services(
    master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_session),
    include_hidden: bool = False,
) -> list[ServiceOut]:
    stmt = select(Service).where(Service.master_id == master.id).order_by(Service.id)
    if not include_hidden:
        stmt = stmt.where(Service.is_active.is_(True))
    res = await session.execute(stmt)
    return [ServiceOut.from_model(s) for s in res.scalars()]


@router.post("", response_model=ServiceOut, status_code=201)
async def create_service(
    payload: ServiceCreate,
    master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_session),
) -> ServiceOut:
    svc = Service(
        master_id=master.id,
        name=payload.name.strip(),
        price=payload.price,
        duration_minutes=payload.duration_minutes,
        is_active=payload.is_active,
    )
    session.add(svc)
    await session.flush()
    return ServiceOut.from_model(svc)


async def _get_owned_service(
    session: AsyncSession, master: Master, service_id: int
) -> Service:
    res = await session.execute(
        select(Service).where(Service.id == service_id, Service.master_id == master.id)
    )
    svc = res.scalar_one_or_none()
    if svc is None:
        raise HTTPException(status_code=404, detail="service not found")
    return svc


@router.patch("/{service_id}", response_model=ServiceOut)
async def update_service(
    service_id: int,
    payload: ServiceUpdate,
    master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_session),
) -> ServiceOut:
    svc = await _get_owned_service(session, master, service_id)
    if payload.name is not None:
        svc.name = payload.name.strip() or svc.name
    if payload.price is not None:
        svc.price = payload.price
    if payload.duration_minutes is not None:
        svc.duration_minutes = payload.duration_minutes
    if payload.is_active is not None:
        svc.is_active = payload.is_active
    return ServiceOut.from_model(svc)


@router.delete("/{service_id}", status_code=204, response_class=Response)
async def delete_service(
    service_id: int,
    master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_session),
) -> Response:
    svc = await _get_owned_service(session, master, service_id)
    # Soft delete to keep historical bookings/stats intact.
    svc.is_active = False
    return Response(status_code=204)
