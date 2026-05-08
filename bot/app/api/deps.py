"""Common FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import InitData, InitDataError, parse_init_data
from app.config import Settings
from app.models import Master
from app.repos import get_master_by_tg_id, upsert_master_from_initdata


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
    """Resolve (and lazily create) the master associated with this Mini App user."""
    master = await get_master_by_tg_id(session, init_data.user.id)
    if master is None:
        master = await upsert_master_from_initdata(
            session,
            init_data.user,
            default_timezone=state.settings.default_timezone,
            default_work_start_minutes=state.settings.default_work_start[0] * 60
            + state.settings.default_work_start[1],
            default_work_end_minutes=state.settings.default_work_end[0] * 60
            + state.settings.default_work_end[1],
            default_slot_step_minutes=state.settings.default_slot_step_minutes,
        )
    return master
