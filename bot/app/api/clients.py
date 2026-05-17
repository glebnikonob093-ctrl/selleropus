from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_master, get_session
from app.models import Booking, Client, Master, Service

router = APIRouter(prefix="/api/clients", tags=["clients"])


class ClientOut(BaseModel):
    id: int
    name: str
    phone: str | None
    tg_username: str | None
    tg_user_id: int | None
    notes: str | None
    last_visit_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, c: Client) -> ClientOut:
        return cls(
            id=c.id,
            name=c.name,
            phone=c.phone,
            tg_username=c.tg_username,
            tg_user_id=c.tg_user_id,
            notes=c.notes,
            last_visit_at=c.last_visit_at,
            created_at=c.created_at,
        )


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    tg_username: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class ClientUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    tg_username: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class BookingHistoryItem(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    status: str
    service_id: int
    service_name: str
    price_snapshot: int


class ClientDetail(ClientOut):
    bookings: list[BookingHistoryItem]


async def _get_owned_client(
    session: AsyncSession, master: Master, client_id: int
) -> Client:
    res = await session.execute(
        select(Client).where(Client.id == client_id, Client.master_id == master.id)
    )
    client = res.scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="client not found")
    return client


@router.get("", response_model=list[ClientOut])
async def list_clients(
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
) -> list[ClientOut]:
    stmt = select(Client).where(Client.master_id == master.id).order_by(Client.id.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where((Client.name.ilike(like)) | (Client.phone.ilike(like)))
    res = await session.execute(stmt)
    return [ClientOut.from_model(c) for c in res.scalars()]


@router.post("", response_model=ClientOut, status_code=201)
async def create_client(
    payload: ClientCreate,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> ClientOut:
    client = Client(
        master_id=master.id,
        name=payload.name.strip(),
        phone=(payload.phone or "").strip() or None,
        tg_username=(payload.tg_username or "").strip().lstrip("@") or None,
        notes=payload.notes,
    )
    session.add(client)
    await session.flush()
    return ClientOut.from_model(client)


@router.get("/{client_id}", response_model=ClientDetail)
async def get_client(
    client_id: int,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> ClientDetail:
    client = await _get_owned_client(session, master, client_id)

    res = await session.execute(
        select(Booking, Service.name)
        .join(Service, Service.id == Booking.service_id)
        .where(Booking.client_id == client.id)
        .order_by(Booking.starts_at.desc())
    )
    history = [
        BookingHistoryItem(
            id=b.id,
            starts_at=b.starts_at,
            ends_at=b.ends_at,
            status=b.status,
            service_id=b.service_id,
            service_name=service_name,
            price_snapshot=b.price_snapshot,
        )
        for b, service_name in res.all()
    ]

    return ClientDetail(
        **ClientOut.from_model(client).model_dump(),
        bookings=history,
    )


@router.patch("/{client_id}", response_model=ClientOut)
async def update_client(
    client_id: int,
    payload: ClientUpdate,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> ClientOut:
    client = await _get_owned_client(session, master, client_id)
    if payload.name is not None:
        client.name = payload.name.strip() or client.name
    if payload.phone is not None:
        client.phone = payload.phone.strip() or None
    if payload.tg_username is not None:
        client.tg_username = payload.tg_username.strip().lstrip("@") or None
    if payload.notes is not None:
        client.notes = payload.notes or None
    return ClientOut.from_model(client)


@router.delete("/{client_id}", status_code=204, response_class=Response)
async def delete_client(
    client_id: int,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> Response:
    client = await _get_owned_client(session, master, client_id)
    await session.delete(client)
    return Response(status_code=204)
