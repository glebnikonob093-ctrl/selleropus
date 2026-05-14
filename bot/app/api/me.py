from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AppState,
    get_app_state,
    get_current_active_master,
    get_current_user,
    get_session,
)
from app.models import Master
from app.repos import generate_unique_slug, slugify

router = APIRouter(prefix="/api/me", tags=["me"])


class MeResponse(BaseModel):
    id: int
    tg_user_id: int
    tg_username: str | None
    display_name: str
    slug: str
    timezone: str
    language: str
    work_start_minutes: int
    work_end_minutes: int
    slot_step_minutes: int
    is_master: bool
    is_admin: bool
    admin_contact_url: str = Field(
        default="",
        description=(
            "Deep-link to the super-admin; rendered by the Mini App on the "
            "'Become a master' screen. Empty when no admins are configured."
        ),
    )
    become_master_conditions: str = Field(
        default="",
        description=(
            "Plain-text conditions shown to non-masters who want to be "
            "promoted. Configured via BECOME_MASTER_CONDITIONS env var."
        ),
    )
    public_link_path: str = Field(
        description="Frontend path that can be shared with clients (e.g. ?master=<slug>).",
    )


def _to_response(master: Master, state: AppState) -> MeResponse:
    return MeResponse(
        id=master.id,
        tg_user_id=master.tg_user_id,
        tg_username=master.tg_username,
        display_name=master.display_name,
        slug=master.slug,
        timezone=master.timezone,
        language=master.language,
        work_start_minutes=master.work_start_minutes,
        work_end_minutes=master.work_end_minutes,
        slot_step_minutes=master.slot_step_minutes,
        is_master=master.is_master,
        is_admin=master.is_admin,
        admin_contact_url=state.settings.admin_contact_url,
        become_master_conditions=state.settings.become_master_conditions,
        public_link_path=f"?master={master.slug}",
    )


@router.get("", response_model=MeResponse)
async def read_me(
    user: Master = Depends(get_current_user),
    state: AppState = Depends(get_app_state),
) -> MeResponse:
    """Return the caller's profile + role flags.

    Available to every Telegram user, master or not — the Mini App renders a
    "become a master" landing page when ``is_master`` is false.
    """
    return _to_response(user, state)


class UpdateMeRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    slug: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    work_start_minutes: int | None = Field(default=None, ge=0, le=24 * 60 - 1)
    work_end_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    slot_step_minutes: int | None = Field(default=None, ge=5, le=240)


@router.patch("", response_model=MeResponse)
async def update_me(
    payload: UpdateMeRequest,
    master: Master = Depends(get_current_active_master),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_app_state),
) -> MeResponse:
    if payload.display_name is not None:
        master.display_name = payload.display_name.strip() or master.display_name

    if payload.slug is not None:
        cleaned = slugify(payload.slug)
        if not cleaned:
            raise HTTPException(status_code=400, detail="slug must contain letters or digits")
        if cleaned != master.slug:
            master.slug = await generate_unique_slug(session, cleaned)

    if payload.timezone is not None:
        master.timezone = payload.timezone.strip() or master.timezone

    if payload.work_start_minutes is not None:
        master.work_start_minutes = payload.work_start_minutes
    if payload.work_end_minutes is not None:
        master.work_end_minutes = payload.work_end_minutes

    if master.work_end_minutes <= master.work_start_minutes:
        raise HTTPException(
            status_code=400, detail="work_end_minutes must be greater than work_start_minutes"
        )

    if payload.slot_step_minutes is not None:
        master.slot_step_minutes = payload.slot_step_minutes

    return _to_response(master)
