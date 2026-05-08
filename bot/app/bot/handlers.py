"""aiogram handlers for the master-facing Telegram bot."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import session_scope
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    Client,
    Master,
    Service,
)
from app.repos import upsert_master_from_tg


def _public_link(settings: Settings, master: Master) -> str:
    base = settings.webapp_url.rstrip("/") if settings.webapp_url else ""
    if not base:
        return f"?master={master.slug}"
    return f"{base}?{urlencode({'master': master.slug})}"


def _open_app_keyboard(settings: Settings) -> InlineKeyboardMarkup | None:
    if not settings.webapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 Открыть Clientika",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            ]
        ]
    )


def build_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    dp = Dispatcher()
    router = Router(name="clientika")

    work_start_minutes = settings.default_work_start[0] * 60 + settings.default_work_start[1]
    work_end_minutes = settings.default_work_end[0] * 60 + settings.default_work_end[1]

    async def _ensure_master(message: Message) -> Master:
        async with session_scope(session_factory) as session:
            from_user = message.from_user
            assert from_user is not None
            display_name = (
                f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()
                or (from_user.username or "")
                or f"id{from_user.id}"
            )
            master = await upsert_master_from_tg(
                session,
                tg_user_id=from_user.id,
                tg_chat_id=message.chat.id,
                tg_username=from_user.username,
                display_name_hint=display_name,
                default_timezone=settings.default_timezone,
                default_work_start_minutes=work_start_minutes,
                default_work_end_minutes=work_end_minutes,
                default_slot_step_minutes=settings.default_slot_step_minutes,
            )
            session.expunge(master)
            return master

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        master = await _ensure_master(message)
        link = _public_link(settings, master)
        text = (
            f"Привет, {master.display_name}! 👋\n\n"
            "Это <b>Clientika</b> — ваш мини-CRM в Telegram:\n"
            "• услуги, клиенты и записи в одном месте,\n"
            "• автоматические напоминания клиентам,\n"
            "• статистика дохода за день/неделю/месяц.\n\n"
            f"Ваша ссылка для записи клиентов: <code>{link}</code>\n"
            "Откройте приложение и добавьте свои услуги — и можно делиться ссылкой."
        )
        await message.answer(
            text,
            reply_markup=_open_app_keyboard(settings),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("link"))
    async def on_link(message: Message) -> None:
        master = await _ensure_master(message)
        link = _public_link(settings, master)
        await message.answer(
            f"Ваша персональная ссылка для клиентов:\n<code>{link}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("today"))
    async def on_today(message: Message) -> None:
        master = await _ensure_master(message)
        async with session_scope(session_factory) as session:
            now = datetime.utcnow()
            day_start = datetime(now.year, now.month, now.day)
            day_end = day_start + timedelta(days=1)
            stmt = (
                select(Booking, Client, Service)
                .join(Client, Client.id == Booking.client_id)
                .join(Service, Service.id == Booking.service_id)
                .where(Booking.master_id == master.id)
                .where(Booking.starts_at >= day_start)
                .where(Booking.starts_at < day_end)
                .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
                .order_by(Booking.starts_at)
            )
            rows = list((await session.execute(stmt)).all())

        if not rows:
            await message.answer("На сегодня записей нет.")
            return

        lines = [f"<b>Сегодня записей: {len(rows)}</b>"]
        for booking, client, service in rows:
            lines.append(
                f"• {booking.starts_at.strftime('%H:%M')} — {service.name} — "
                f"{client.name}"
                + (f" ({client.phone})" if client.phone else "")
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        await message.answer(
            "Команды бота:\n"
            "/start — приветствие и кнопка приложения\n"
            "/link — ваша ссылка для клиентов\n"
            "/today — записи на сегодня\n",
            reply_markup=_open_app_keyboard(settings),
        )

    @router.message(F.web_app_data)
    async def on_webapp_data(message: Message) -> None:
        # Forward-compatible stub: Mini App uses the HTTP API, but if some
        # workflow ever calls Telegram.WebApp.sendData() we don't crash.
        await message.answer("Принято.")

    dp.include_router(router)
    return dp
