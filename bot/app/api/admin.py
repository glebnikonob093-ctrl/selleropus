"""Admin-only endpoints for managing bot users / masters.

Every route in this module is gated behind ``get_current_admin``, which
requires the Mini App caller to be a verified Telegram user whose row in
``masters`` has ``is_admin=True`` (set either via the ``ADMIN_TG_IDS`` env
var bootstrap or by an existing admin promoting another account here).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_session
from app.models import Master
from app.repos import generate_unique_slug

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminUserOut(BaseModel):
    id: int
    tg_user_id: int
    tg_username: str | None
    display_name: str
    slug: str
    is_master: bool
    is_admin: bool
    created_at: datetime

    @classmethod
    def from_model(cls, m: Master) -> AdminUserOut:
        return cls(
            id=m.id,
            tg_user_id=m.tg_user_id,
            tg_username=m.tg_username,
            display_name=m.display_name,
            slug=m.slug,
            is_master=m.is_master,
            is_admin=m.is_admin,
            created_at=m.created_at,
        )


class AddMasterRequest(BaseModel):
    tg_user_id: int = Field(gt=0, description="Telegram numeric user id to promote.")
    display_name: str | None = Field(default=None, max_length=120)
    tg_username: str | None = Field(default=None, max_length=64)


def _master_visible_to_admin(
    _admin: Master,
    m: Master,
) -> bool:
    """Filter hook for listings. For now the super-admin sees every row."""
    return True


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    _admin: Master = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
    role: str | None = None,
) -> list[AdminUserOut]:
    """List every bot user.

    Pass ``role=master`` / ``role=admin`` / ``role=pending`` to filter to a
    subset; the default returns the full directory ordered by most recently
    created so newly registered users surface first.
    """
    stmt = select(Master).order_by(Master.created_at.desc())
    if role == "master":
        stmt = stmt.where(Master.is_master.is_(True))
    elif role == "admin":
        stmt = stmt.where(Master.is_admin.is_(True))
    elif role == "pending":
        stmt = stmt.where(Master.is_master.is_(False), Master.is_admin.is_(False))
    elif role:
        raise HTTPException(
            status_code=400, detail="role must be one of: master, admin, pending"
        )
    res = await session.execute(stmt)
    return [
        AdminUserOut.from_model(m)
        for m in res.scalars()
        if _master_visible_to_admin(_admin, m)
    ]


@router.post("/users", response_model=AdminUserOut, status_code=201)
async def add_master_by_tg_id(
    payload: AddMasterRequest,
    admin: Master = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Promote a Telegram account to master by its numeric id.

    If the user has never opened the bot we still create a stub row so the
    admin can hand out the public booking link immediately — the row gets
    updated with real Telegram metadata on the user's first ``/start``.
    Idempotent: re-posting the same id just flips the flag back on.
    """
    _ = admin  # presence already verified by the dependency
    res = await session.execute(
        select(Master).where(Master.tg_user_id == payload.tg_user_id)
    )
    user = res.scalar_one_or_none()
    if user is not None:
        user.is_master = True
        if payload.display_name:
            user.display_name = payload.display_name.strip() or user.display_name
        if payload.tg_username:
            user.tg_username = payload.tg_username.strip().lstrip("@") or user.tg_username
        return AdminUserOut.from_model(user)

    slug_hint = (
        (payload.tg_username or "").strip().lstrip("@")
        or (payload.display_name or "").strip()
        or f"m{payload.tg_user_id}"
    )
    slug = await generate_unique_slug(session, slug_hint)
    user = Master(
        tg_user_id=payload.tg_user_id,
        tg_chat_id=payload.tg_user_id,
        tg_username=(payload.tg_username or "").strip().lstrip("@") or None,
        display_name=(payload.display_name or "").strip()
        or (payload.tg_username or f"id{payload.tg_user_id}"),
        slug=slug,
        is_master=True,
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return AdminUserOut.from_model(user)


class SetRoleRequest(BaseModel):
    is_master: bool | None = None
    is_admin: bool | None = None


async def _get_user_or_404(session: AsyncSession, user_id: int) -> Master:
    res = await session.execute(select(Master).where(Master.id == user_id))
    user = res.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user_roles(
    user_id: int,
    payload: SetRoleRequest,
    admin: Master = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Update a user's role flags. Admin self-demote is rejected so an org
    can't accidentally lock itself out of the admin panel."""
    user = await _get_user_or_404(session, user_id)
    if payload.is_master is not None:
        user.is_master = payload.is_master
    if payload.is_admin is not None:
        if user.id == admin.id and payload.is_admin is False:
            raise HTTPException(
                status_code=400, detail="cannot remove your own admin flag"
            )
        user.is_admin = payload.is_admin
    return AdminUserOut.from_model(user)


@router.delete("/users/{user_id}/master", response_model=AdminUserOut)
async def demote_master(
    user_id: int,
    _admin: Master = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Convenience shortcut for "revoke master access" without touching the
    admin flag — the user keeps their data, just can't act as a master."""
    user = await _get_user_or_404(session, user_id)
    user.is_master = False
    return AdminUserOut.from_model(user)
