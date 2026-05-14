"""Common FastAPI dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import InitData, InitDataError, parse_init_data
from app.config import Settings
from app.models import Master
from app.repos import get_master_by_tg_id, upsert_master_from_initdata

# Process-wide locks keyed by tg_user_id that serialize the very first
# "find-or-create master" call across parallel Mini App requests. The lock
# is created lazily on first use and never released to the global dict — in
# practice there is one entry per active user, which is bounded by the
# number of users who have ever opened the Mini App in this process. Once
# the master row exists the lock is taken only briefly (just long enough to
# re-check the row), so steady-state cost is negligible.
_master_create_locks: dict[int, asyncio.Lock] = {}


def _lock_for(tg_user_id: int) -> asyncio.Lock:
    lock = _master_create_locks.get(tg_user_id)
    if lock is None:
        lock = asyncio.Lock()
        _master_create_locks[tg_user_id] = lock
    return lock


@dataclass
class AppState:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    notifier: object | None  # forward ref to Notifier; kept untyped to avoid cycle


def get_app_state(request: Request) -> AppState:
    state = getattr(request.app.state, "app_state", None)
    if state is None:
        raise RuntimeError("AppState is not configured on FastAPI app")
    return state


async def get_session(
    state: AppState = Depends(get_app_state),
) -> AsyncIterator[AsyncSession]:
    session = state.session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _extract_init_data(
    authorization: str | None,
    tg_init_data_header: str | None,
) -> str:
    if tg_init_data_header:
        return tg_init_data_header
    if authorization and authorization.lower().startswith("tma "):
        return authorization[4:]
    return ""


async def get_init_data(
    state: AppState = Depends(get_app_state),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> InitData:
    raw = _extract_init_data(authorization, tg_init_data)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing telegram initData",
        )
    try:
        return parse_init_data(raw, state.settings.bot_token)
    except InitDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"initData invalid: {exc}",
        ) from exc


async def get_optional_init_data(
    state: AppState = Depends(get_app_state),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> InitData | None:
    raw = _extract_init_data(authorization, tg_init_data)
    if not raw:
        return None
    try:
        return parse_init_data(raw, state.settings.bot_token)
    except InitDataError:
        return None


async def get_current_master(
    state: AppState = Depends(get_app_state),
    init_data: InitData = Depends(get_init_data),
    session: AsyncSession = Depends(get_session),
) -> Master:
    """Resolve (and lazily create) the master associated with this Mini App user.

    The Mini App issues several authenticated requests in parallel on first
    load (the Today page does ``Promise.all([getMe, listBookingsToday])``).
    When the master row does not yet exist, *every* concurrent request would
    independently SELECT-empty + INSERT, and all but the first would crash
    with ``UNIQUE constraint failed: masters.tg_user_id``.

    We avoid the race by serializing the very first creation behind a
    per-user ``asyncio.Lock``. The lock scope is just long enough to perform
    the INSERT in its own short-lived transaction (committed immediately so
    the row becomes visible to any waiting concurrent request) and to
    re-read the row in the caller's session. Once the row exists the lock
    is held only for a fast ``SELECT`` so the steady-state cost is
    negligible.
    """

    master = await get_master_by_tg_id(session, init_data.user.id)
    if master is not None:
        return master

    async with _lock_for(init_data.user.id):
        # Re-check inside the lock: a concurrent waiter may have just
        # finished creating the row.
        master = await get_master_by_tg_id(session, init_data.user.id)
        if master is not None:
            return master

        # Create in a fresh session so the INSERT commits immediately,
        # making the row visible to other parallel requests on this engine.
        async with state.session_factory() as create_session:
            await upsert_master_from_initdata(
                create_session,
                init_data.user,
                default_timezone=state.settings.default_timezone,
                default_work_start_minutes=state.settings.default_work_start[0] * 60
                + state.settings.default_work_start[1],
                default_work_end_minutes=state.settings.default_work_end[0] * 60
                + state.settings.default_work_end[1],
                default_slot_step_minutes=state.settings.default_slot_step_minutes,
            )
            await create_session.commit()

        # Re-fetch in the request's session so the returned instance is
        # attached and shares the caller's transaction (so e.g. a later
        # session.commit() picks up any in-request mutations).
        master = await get_master_by_tg_id(session, init_data.user.id)
        if master is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to create master record",
            )
        return master
