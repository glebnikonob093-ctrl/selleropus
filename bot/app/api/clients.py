from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_master, get_session
from app.models import Booking, Client, Master, Service
from app.repos import block_client, list_blocked_clients, unblock_client

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
    is_blocked: bool = False

    @classmethod
    def from_model(cls, c: Client, *, blocked: bool = False) -> ClientOut:
        return cls(
            id=c.id,
            name=c.name,
            phone=c.phone,
            tg_username=c.tg_username,
            tg_user_id=c.tg_user_id,
            notes=c.notes,
            last_visit_at=c.last_visit_at,
            created_at=c.created_at,
            is_blocked=blocked,
        )


class BlockedClientOut(BaseModel):
    tg_user_id: int
    reason: str | None
    blocked_at: datetime


class BlockRequest(BaseModel):
    tg_user_id: int
    reason: str | None = None


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
    clients = list(res.scalars())
    blocked_list = await list_blocked_clients(session, master.id)
    blocked_ids = {bc.tg_user_id for bc in blocked_list}
    return [
        ClientOut.from_model(c, blocked=bool(c.tg_user_id and c.tg_user_id in blocked_ids))
        for c in clients
    ]


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


@router.delete("/{client_id}", status_code=204, response_model=None)
async def delete_client(
    client_id: int,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> None:
    client = await _get_owned_client(session, master, client_id)
    # Delete bookings first (cascade handles their reminder_states)
    res = await session.execute(
        select(Booking).where(Booking.client_id == client.id)
    )
    for booking in res.scalars():
        await session.delete(booking)
    await session.flush()
    await session.delete(client)


@router.post("/block", status_code=201, response_model=BlockedClientOut)
async def block_client_endpoint(
    payload: BlockRequest,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> BlockedClientOut:
    bc = await block_client(session, master.id, payload.tg_user_id, payload.reason)
    return BlockedClientOut(
        tg_user_id=bc.tg_user_id, reason=bc.reason, blocked_at=bc.blocked_at
    )


@router.post("/unblock", status_code=200)
async def unblock_client_endpoint(
    payload: BlockRequest,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    removed = await unblock_client(session, master.id, payload.tg_user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="client not blocked")
    return {"success": True}


@router.get("/blocked", response_model=list[BlockedClientOut])
async def list_blocked_endpoint(
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
) -> list[BlockedClientOut]:
    blocked = await list_blocked_clients(session, master.id)
    return [
        BlockedClientOut(
            tg_user_id=bc.tg_user_id, reason=bc.reason, blocked_at=bc.blocked_at
        )
        for bc in blocked
    ]
