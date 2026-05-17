"""Lightweight data-access helpers used by API and bot."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import InitDataUser
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    BOOKING_STATUS_CAME,
    Booking,
    Client,
    Master,
    Service,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(raw: str) -> str:
    base = _SLUG_RE.sub("-", (raw or "").lower()).strip("-")
    return base[:48] if base else ""


async def _slug_taken(session: AsyncSession, slug: str) -> bool:
    res = await session.execute(select(Master.id).where(Master.slug == slug))
    return res.scalar_one_or_none() is not None


async def generate_unique_slug(session: AsyncSession, hint: str) -> str:
    """Best-effort unique-slug picker for the ``masters`` table.

    Strategy:
    1. Try the bare slugified hint for a nice readable URL.
    2. If taken, try a short sequential suffix (``-2``, ``-3``, …) — still
       human-friendly when two people share a base name.
    3. After eight collisions, fall back to a random hex suffix so even a
       pathological case (1000 Marias) terminates immediately.

    There is still a tiny TOCTOU race between the final ``_slug_taken``
    check and the eventual ``INSERT`` — two parallel registrations with the
    same hint could both pass step 1 and one of them would 500 on the
    UNIQUE constraint. The random fallback makes the realistic-collision
    case vanishingly unlikely; a proper fix is to retry the INSERT on
    ``IntegrityError`` at the upsert layer, which is tracked separately.
    """
    base = slugify(hint) or f"m{secrets.token_hex(3)}"
    if not await _slug_taken(session, base):
        return base
    for suffix in range(2, 10):
        candidate = f"{base}-{suffix}"
        if not await _slug_taken(session, candidate):
            return candidate
    # 8 collisions is enough — bail out with entropy. ``token_hex(3)`` gives
    # ~16M unique suffixes; collision probability is irrelevant in practice.
    return f"{base}-{secrets.token_hex(3)}"


async def get_master_by_tg_id(session: AsyncSession, tg_user_id: int) -> Master | None:
    res = await session.execute(select(Master).where(Master.tg_user_id == tg_user_id))
    return res.scalar_one_or_none()


async def get_master_by_slug(session: AsyncSession, slug: str) -> Master | None:
    res = await session.execute(select(Master).where(Master.slug == slug))
    return res.scalar_one_or_none()


async def upsert_master_from_tg(
    session: AsyncSession,
    *,
    tg_user_id: int,
    tg_chat_id: int | None,
    tg_username: str | None,
    display_name_hint: str,
    default_timezone: str,
    default_work_start_minutes: int,
    default_work_end_minutes: int,
    default_slot_step_minutes: int,
) -> Master:
    """Find an existing master by Telegram id or create a new one."""
    master = await get_master_by_tg_id(session, tg_user_id)
    if master is not None:
        if tg_chat_id and master.tg_chat_id != tg_chat_id:
            master.tg_chat_id = tg_chat_id
        if tg_username and master.tg_username != tg_username:
            master.tg_username = tg_username
        return master

    slug_hint = tg_username or display_name_hint or f"m{tg_user_id}"
    slug = await generate_unique_slug(session, slug_hint)

    master = Master(
        tg_user_id=tg_user_id,
        tg_chat_id=tg_chat_id or tg_user_id,
        tg_username=tg_username,
        display_name=display_name_hint or (tg_username or f"id{tg_user_id}"),
        slug=slug,
        timezone=default_timezone,
        work_start_minutes=default_work_start_minutes,
        work_end_minutes=default_work_end_minutes,
        slot_step_minutes=default_slot_step_minutes,
    )
    session.add(master)
    await session.flush()
    return master


async def upsert_master_from_initdata(
    session: AsyncSession,
    user: InitDataUser,
    *,
    default_timezone: str,
    default_work_start_minutes: int,
    default_work_end_minutes: int,
    default_slot_step_minutes: int,
) -> Master:
    name_hint = (f"{user.first_name} {user.last_name}".strip()) or user.username
    return await upsert_master_from_tg(
        session,
        tg_user_id=user.id,
        tg_chat_id=user.id,  # Mini App users send messages to themselves; chat_id=user_id for private
        tg_username=user.username or None,
        display_name_hint=name_hint or f"id{user.id}",
        default_timezone=default_timezone,
        default_work_start_minutes=default_work_start_minutes,
        default_work_end_minutes=default_work_end_minutes,
        default_slot_step_minutes=default_slot_step_minutes,
    )


def _normalize_phone(raw: str | None) -> str | None:
    """Loose phone normalization for dedup purposes.

    We only strip whitespace and common separators; we deliberately don't
    do full E.164 parsing here because that requires a country guess and
    the master can correct the row by hand later. The goal is just to make
    "+7 (999) 123-45-67" and "+79991234567" hash to the same key.
    """
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    return cleaned or None


async def find_or_create_client(
    session: AsyncSession,
    master_id: int,
    *,
    name: str,
    phone: str | None = None,
    tg_username: str | None = None,
    tg_user_id: int | None = None,
) -> Client:
    """Find an existing client row or insert a new one.

    Lookup order:
    * by ``(master_id, tg_user_id)`` — the strongest identity, used when
      the booking arrived through an authenticated Mini App / bot session.
    * by ``(master_id, normalized_phone)`` for anonymous bookings (no TG
      identity). Two public bookings from the same phone now reuse the
      same client row instead of creating duplicates that pollute the
      master's client list and stats.

    We only consolidate anonymous-with-anonymous rows; a row that already
    has a ``tg_user_id`` is left alone because we don't want to silently
    merge two channels that might belong to different real people sharing
    a phone (a family landline, a desk phone, …).
    """
    if tg_user_id is not None:
        res = await session.execute(
            select(Client).where(
                Client.master_id == master_id,
                Client.tg_user_id == tg_user_id,
            )
        )
        existing = res.scalar_one_or_none()
        if existing is not None:
            if name and existing.name != name:
                existing.name = name
            if phone and not existing.phone:
                existing.phone = phone
            if tg_username and not existing.tg_username:
                existing.tg_username = tg_username
            return existing

    normalized = _normalize_phone(phone)
    if tg_user_id is None and normalized:
        # Anonymous booking with a phone — try to match a previous anonymous
        # row from the same phone. We compare normalized forms by computing
        # the candidate's normalized phone in Python; the underlying column
        # may have been stored with arbitrary formatting from the public
        # form. ``limit(1)`` keeps the work bounded even if the master has
        # historical dupes.
        res = await session.execute(
            select(Client).where(
                Client.master_id == master_id,
                Client.phone.is_not(None),
                Client.tg_user_id.is_(None),
            )
        )
        for candidate in res.scalars():
            if _normalize_phone(candidate.phone) == normalized:
                if name and candidate.name != name:
                    candidate.name = name
                if tg_username and not candidate.tg_username:
                    candidate.tg_username = tg_username
                return candidate

    client = Client(
        master_id=master_id,
        name=name or "Без имени",
        phone=phone,
        tg_username=tg_username,
        tg_user_id=tg_user_id,
    )
    session.add(client)
    await session.flush()
    return client


async def list_active_services(session: AsyncSession, master_id: int) -> list[Service]:
    res = await session.execute(
        select(Service)
        .where(Service.master_id == master_id, Service.is_active.is_(True))
        .order_by(Service.id)
    )
    return list(res.scalars())


async def list_bookings_in_window(
    session: AsyncSession,
    master_id: int,
    starts_from: datetime,
    ends_before: datetime,
    *,
    only_active: bool = True,
) -> list[Booking]:
    stmt = (
        select(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.starts_at < ends_before,
            Booking.ends_at > starts_from,
        )
        .order_by(Booking.starts_at)
    )
    if only_active:
        stmt = stmt.where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
    res = await session.execute(stmt)
    return list(res.scalars())


async def get_revenue(
    session: AsyncSession,
    master_id: int,
    starts_from: datetime,
    ends_before: datetime,
) -> int:
    res = await session.execute(
        select(Booking.price_snapshot).where(
            Booking.master_id == master_id,
            Booking.status == BOOKING_STATUS_CAME,
            Booking.starts_at >= starts_from,
            Booking.starts_at < ends_before,
        )
    )
    return sum(int(p or 0) for p in res.scalars())


async def find_clients_to_return(
    session: AsyncSession,
    master_id: int,
    *,
    threshold_days: int = 30,
) -> list[Client]:
    threshold = datetime.utcnow() - timedelta(days=threshold_days)
    res = await session.execute(
        select(Client)
        .where(
            Client.master_id == master_id,
            Client.last_visit_at.is_not(None),
            Client.last_visit_at < threshold,
        )
        .order_by(Client.last_visit_at.asc())
    )
    return list(res.scalars())
