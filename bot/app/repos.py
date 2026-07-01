"""Lightweight data-access helpers used by API and bot."""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import InitDataUser
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    BOOKING_STATUS_CAME,
    BlockedClient,
    Booking,
    Client,
    Master,
    MasterBot,
    MasterDayOff,
    MasterSchedule,
    Service,
    TeamMember,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(raw: str) -> str:
    base = _SLUG_RE.sub("-", (raw or "").lower()).strip("-")
    return base[:48] if base else ""


async def _slug_taken(session: AsyncSession, slug: str) -> bool:
    res = await session.execute(select(Master.id).where(Master.slug == slug))
    return res.scalar_one_or_none() is not None


async def generate_unique_slug(session: AsyncSession, hint: str) -> str:
    base = slugify(hint) or f"m{secrets.token_hex(3)}"
    candidate = base
    suffix = 2
    while await _slug_taken(session, candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


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
    is_master: bool = True,
) -> Master:
    """Find an existing master by Telegram id or create a new one."""
    master = await get_master_by_tg_id(session, tg_user_id)
    if master is not None:
        if tg_chat_id and master.tg_chat_id != tg_chat_id:
            master.tg_chat_id = tg_chat_id
        if tg_username and master.tg_username != tg_username:
            master.tg_username = tg_username
        if is_master and not master.is_master:
            master.is_master = True
        return master

    slug_hint = tg_username or display_name_hint or f"m{tg_user_id}"
    slug = await generate_unique_slug(session, slug_hint)

    master = Master(
        tg_user_id=tg_user_id,
        tg_chat_id=tg_chat_id or tg_user_id,
        tg_username=tg_username,
        display_name=display_name_hint or (tg_username or f"id{tg_user_id}"),
        slug=slug,
        is_master=is_master,
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
    is_master: bool = False,
) -> Master:
    name_hint = (f"{user.first_name} {user.last_name}".strip()) or user.username
    return await upsert_master_from_tg(
        session,
        tg_user_id=user.id,
        tg_chat_id=user.id,
        tg_username=user.username or None,
        display_name_hint=name_hint or f"id{user.id}",
        default_timezone=default_timezone,
        default_work_start_minutes=default_work_start_minutes,
        default_work_end_minutes=default_work_end_minutes,
        default_slot_step_minutes=default_slot_step_minutes,
        is_master=is_master,
    )


async def find_or_create_client(
    session: AsyncSession,
    master_id: int,
    *,
    name: str,
    phone: str | None = None,
    tg_username: str | None = None,
    tg_user_id: int | None = None,
) -> Client:
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


# ---- MasterBot helpers ----


async def get_master_bot(session: AsyncSession, master_id: int) -> MasterBot | None:
    res = await session.execute(
        select(MasterBot).where(MasterBot.master_id == master_id)
    )
    return res.scalar_one_or_none()


async def get_master_bot_by_bot_id(session: AsyncSession, bot_id: int) -> MasterBot | None:
    res = await session.execute(
        select(MasterBot).where(MasterBot.bot_id == bot_id)
    )
    return res.scalar_one_or_none()


async def create_master_bot(
    session: AsyncSession,
    *,
    master_id: int,
    bot_token: str,
    bot_username: str,
    bot_id: int,
) -> MasterBot:
    mb = MasterBot(
        master_id=master_id,
        bot_token=bot_token,
        bot_username=bot_username,
        bot_id=bot_id,
        is_active=True,
    )
    session.add(mb)
    await session.flush()
    return mb


async def delete_master_bot(session: AsyncSession, master_id: int) -> bool:
    mb = await get_master_bot(session, master_id)
    if mb is None:
        return False
    await session.delete(mb)
    await session.flush()
    return True


async def list_active_master_bots(session: AsyncSession) -> list[MasterBot]:
    res = await session.execute(
        select(MasterBot).where(MasterBot.is_active.is_(True))
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


async def block_client(
    session: AsyncSession,
    master_id: int,
    tg_user_id: int,
    reason: str | None = None,
) -> BlockedClient:
    res = await session.execute(
        select(BlockedClient).where(
            BlockedClient.master_id == master_id,
            BlockedClient.tg_user_id == tg_user_id,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is not None:
        existing.reason = reason
        return existing
    bc = BlockedClient(master_id=master_id, tg_user_id=tg_user_id, reason=reason)
    session.add(bc)
    await session.flush()
    return bc


async def unblock_client(
    session: AsyncSession, master_id: int, tg_user_id: int
) -> bool:
    res = await session.execute(
        select(BlockedClient).where(
            BlockedClient.master_id == master_id,
            BlockedClient.tg_user_id == tg_user_id,
        )
    )
    bc = res.scalar_one_or_none()
    if bc is None:
        return False
    await session.delete(bc)
    await session.flush()
    return True


async def is_client_blocked(
    session: AsyncSession, master_id: int, tg_user_id: int
) -> bool:
    res = await session.execute(
        select(BlockedClient.id).where(
            BlockedClient.master_id == master_id,
            BlockedClient.tg_user_id == tg_user_id,
        )
    )
    return res.scalar_one_or_none() is not None


async def list_blocked_clients(
    session: AsyncSession, master_id: int
) -> list[BlockedClient]:
    res = await session.execute(
        select(BlockedClient)
        .where(BlockedClient.master_id == master_id)
        .order_by(BlockedClient.blocked_at.desc())
    )
    return list(res.scalars())


async def count_masters(session: AsyncSession) -> int:
    res = await session.execute(
        select(func.count()).select_from(Master).where(Master.is_master.is_(True))
    )
    return res.scalar_one()


async def count_clients(session: AsyncSession) -> int:
    res = await session.execute(select(func.count()).select_from(Client))
    return res.scalar_one()


async def count_bookings(session: AsyncSession) -> int:
    res = await session.execute(select(func.count()).select_from(Booking))
    return res.scalar_one()


async def count_active_master_bots(session: AsyncSession) -> int:
    res = await session.execute(
        select(func.count()).select_from(MasterBot).where(MasterBot.is_active.is_(True))
    )
    return res.scalar_one()


async def list_all_masters(session: AsyncSession) -> list[Master]:
    res = await session.execute(
        select(Master).where(Master.is_master.is_(True)).order_by(Master.created_at.desc())
    )
    return list(res.scalars())


async def list_clients_for_master(
    session: AsyncSession, master_id: int
) -> list[Client]:
    res = await session.execute(
        select(Client)
        .where(Client.master_id == master_id)
        .order_by(Client.created_at.desc())
    )
    return list(res.scalars())


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


# ---- Team members -----------------------------------------------------------


async def add_team_member(
    session: AsyncSession,
    master_id: int,
    tg_user_id: int,
    tg_username: str | None = None,
    display_name: str = "",
) -> TeamMember:
    res = await session.execute(
        select(TeamMember).where(
            TeamMember.master_id == master_id,
            TeamMember.tg_user_id == tg_user_id,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is not None:
        existing.tg_username = tg_username
        existing.display_name = display_name or existing.display_name
        return existing
    tm = TeamMember(
        master_id=master_id,
        tg_user_id=tg_user_id,
        tg_username=tg_username,
        display_name=display_name,
    )
    session.add(tm)
    await session.flush()
    return tm


async def remove_team_member(
    session: AsyncSession, master_id: int, tg_user_id: int
) -> bool:
    res = await session.execute(
        select(TeamMember).where(
            TeamMember.master_id == master_id,
            TeamMember.tg_user_id == tg_user_id,
        )
    )
    tm = res.scalar_one_or_none()
    if tm is None:
        return False
    await session.delete(tm)
    return True


async def list_team_members(
    session: AsyncSession, master_id: int
) -> list[TeamMember]:
    res = await session.execute(
        select(TeamMember)
        .where(TeamMember.master_id == master_id)
        .order_by(TeamMember.added_at.desc())
    )
    return list(res.scalars())


# ---- Master schedule --------------------------------------------------------


async def get_master_schedule(
    session: AsyncSession, master_id: int
) -> list[MasterSchedule]:
    res = await session.execute(
        select(MasterSchedule)
        .where(MasterSchedule.master_id == master_id)
        .order_by(MasterSchedule.weekday)
    )
    return list(res.scalars())


async def get_schedule_for_weekday(
    session: AsyncSession, master_id: int, weekday: int
) -> MasterSchedule | None:
    res = await session.execute(
        select(MasterSchedule).where(
            MasterSchedule.master_id == master_id,
            MasterSchedule.weekday == weekday,
        )
    )
    return res.scalar_one_or_none()


async def init_default_schedule(
    session: AsyncSession,
    master_id: int,
    work_start: int = 10 * 60,
    work_end: int = 20 * 60,
) -> list[MasterSchedule]:
    """Create 7 schedule rows (Mon-Sun) if none exist. Sat/Sun default off."""
    existing = await get_master_schedule(session, master_id)
    if existing:
        return existing
    rows: list[MasterSchedule] = []
    for wd in range(7):
        row = MasterSchedule(
            master_id=master_id,
            weekday=wd,
            is_working=wd < 5,
            start_minutes=work_start,
            end_minutes=work_end,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def toggle_schedule_day(
    session: AsyncSession, master_id: int, weekday: int
) -> bool:
    """Toggle working/not-working for a weekday. Returns new is_working."""
    row = await get_schedule_for_weekday(session, master_id, weekday)
    if row is None:
        return False
    row.is_working = not row.is_working
    return row.is_working


async def adjust_schedule_time(
    session: AsyncSession,
    master_id: int,
    weekday: int,
    field: str,
    delta: int,
) -> int:
    """Adjust start_minutes or end_minutes by delta. Returns new value."""
    row = await get_schedule_for_weekday(session, master_id, weekday)
    if row is None:
        return 0
    if field == "start":
        row.start_minutes = max(0, min(23 * 60, row.start_minutes + delta))
        if row.start_minutes >= row.end_minutes:
            row.start_minutes = row.end_minutes - 30
        return row.start_minutes
    row.end_minutes = max(60, min(24 * 60, row.end_minutes + delta))
    if row.end_minutes <= row.start_minutes:
        row.end_minutes = row.start_minutes + 30
    return row.end_minutes


# ---- Master day-offs --------------------------------------------------------


async def list_day_offs(
    session: AsyncSession, master_id: int
) -> list[MasterDayOff]:
    res = await session.execute(
        select(MasterDayOff)
        .where(MasterDayOff.master_id == master_id)
        .order_by(MasterDayOff.day)
    )
    return list(res.scalars())


async def is_day_off(
    session: AsyncSession, master_id: int, day: date
) -> bool:
    res = await session.execute(
        select(MasterDayOff).where(
            MasterDayOff.master_id == master_id,
            MasterDayOff.day == day,
        )
    )
    return res.scalar_one_or_none() is not None


async def toggle_day_off(
    session: AsyncSession, master_id: int, day: date
) -> bool:
    """Toggle day-off. Returns True if day is now a day-off."""
    res = await session.execute(
        select(MasterDayOff).where(
            MasterDayOff.master_id == master_id,
            MasterDayOff.day == day,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is not None:
        await session.delete(existing)
        return False
    session.add(MasterDayOff(master_id=master_id, day=day))
    await session.flush()
    return True


# ---- Admin: delete master ---------------------------------------------------


async def delete_master_full(session: AsyncSession, master_id: int) -> bool:
    """Delete a master and all associated data (cascading)."""
    res = await session.execute(
        select(Master).where(Master.id == master_id)
    )
    master = res.scalar_one_or_none()
    if master is None:
        return False

    # Delete associated master bot
    bot_res = await session.execute(
        select(MasterBot).where(MasterBot.master_id == master_id)
    )
    mb = bot_res.scalar_one_or_none()
    if mb is not None:
        await session.delete(mb)

    # Delete blocked clients
    blocked_res = await session.execute(
        select(BlockedClient).where(BlockedClient.master_id == master_id)
    )
    for bc in blocked_res.scalars():
        await session.delete(bc)

    # Delete team members
    team_res = await session.execute(
        select(TeamMember).where(TeamMember.master_id == master_id)
    )
    for tm in team_res.scalars():
        await session.delete(tm)

    # Delete schedule and day-offs
    sched_res = await session.execute(
        select(MasterSchedule).where(MasterSchedule.master_id == master_id)
    )
    for s in sched_res.scalars():
        await session.delete(s)
    off_res = await session.execute(
        select(MasterDayOff).where(MasterDayOff.master_id == master_id)
    )
    for o in off_res.scalars():
        await session.delete(o)

    # Master cascade handles services, clients, bookings
    await session.delete(master)
    return True
