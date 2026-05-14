"""aiogram handlers for the unified Clientika bot.

The bot has two modes of operation that share a single dispatcher:

* Anyone who presses ``/start`` is registered as a **user** (``is_master=0``).
  Users see a reply keyboard with "Open Clientika", "My profile" and "Help",
  where "My profile" surfaces the "Become a master" flow.
* Telegram ids in ``ADMIN_TG_IDS`` are bootstrapped to ``is_admin=1`` on
  startup (see ``app.migrations.seed_admins``). Existing masters from the
  pre-roles MVP keep ``is_master=1`` through the column backfill. Admins can
  promote other users from the Mini App's admin panel.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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

# Labels for the persistent reply keyboard. We match on these strings in
# handlers so they have to stay in sync with the keyboard markup below.
BTN_OPEN_APP = "📱 Открыть Clientika"
BTN_PROFILE = "👤 Мой профиль"
BTN_HELP = "❓ Помощь"
BTN_BECOME_MASTER = "✨ Стать мастером"


def _public_link(settings: Settings, master: Master) -> str:
    base = settings.webapp_url.rstrip("/") if settings.webapp_url else ""
    if not base:
        return f"?master={master.slug}"
    return f"{base}?{urlencode({'master': master.slug})}"


def _main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_OPEN_APP), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _open_app_inline_button(settings: Settings) -> InlineKeyboardButton | None:
    if not settings.webapp_url:
        return None
    return InlineKeyboardButton(
        text=BTN_OPEN_APP,
        web_app=WebAppInfo(url=settings.webapp_url),
    )


def _open_app_keyboard(settings: Settings) -> InlineKeyboardMarkup | None:
    btn = _open_app_inline_button(settings)
    if btn is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def _become_master_inline_keyboard(
    settings: Settings, *, include_open_app: bool = False
) -> InlineKeyboardMarkup | None:
    """Inline keyboard for the "Become a master" screen.

    Combines an "Open Mini App" button (for users who want to explore the
    landing in the WebApp) with a "Contact admin" deep link. Returns ``None``
    if neither URL is configured so we don't render an empty keyboard.
    """
    rows: list[list[InlineKeyboardButton]] = []
    contact = settings.admin_contact_url.strip()
    if contact:
        rows.append([InlineKeyboardButton(text="✉️ Написать админу", url=contact)])
    if include_open_app:
        btn = _open_app_inline_button(settings)
        if btn is not None:
            rows.append([btn])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    dp = Dispatcher()
    router = Router(name="clientika")

    work_start_minutes = settings.default_work_start[0] * 60 + settings.default_work_start[1]
    work_end_minutes = settings.default_work_end[0] * 60 + settings.default_work_end[1]
    admin_ids = set(settings.admin_tg_ids)

    async def _ensure_user(message: Message) -> Master:
        """Find or create the ``masters`` row for this Telegram identity.

        Brand-new rows are stored with ``is_master=False`` — they're regular
        users until an admin promotes them. We do *not* auto-flip the
        master flag here; the admin panel is the only path to "master".

        Admins configured via ``ADMIN_TG_IDS`` get ``is_admin=True`` reapplied
        on every ``/start`` so the bootstrap survives a stale row.
        """
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
            if from_user.id in admin_ids and not master.is_admin:
                master.is_admin = True
            session.expunge(master)
            return master

    def _role_label(master: Master) -> str:
        if master.is_admin and master.is_master:
            return "админ + мастер"
        if master.is_admin:
            return "админ"
        if master.is_master:
            return "мастер"
        return "обычный пользователь"

    async def _send_profile(message: Message, master: Master) -> None:
        """Render the "My profile" screen for the given user.

        Layout differs by role:

        * non-master gets the "Become a master" CTA + admin contact link;
        * master sees their public booking link + an Open-App button.
        """
        username = f"@{master.tg_username}" if master.tg_username else "—"
        lines = [
            f"<b>{master.display_name}</b>",
            f"Telegram: {username}",
            f"TG ID: <code>{master.tg_user_id}</code>",
            f"Роль: <b>{_role_label(master)}</b>",
        ]
        if master.is_master:
            link = _public_link(settings, master)
            lines.append("")
            lines.append(
                "Ваша персональная ссылка для клиентов:\n"
                f"<code>{link}</code>"
            )
            await message.answer(
                "\n".join(lines),
                reply_markup=_open_app_keyboard(settings),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        lines.append("")
        lines.append("Вы пока не мастер Clientika. Чтобы получить доступ:")
        lines.append(settings.become_master_conditions)
        kb = _become_master_inline_keyboard(settings, include_open_app=True)
        await message.answer(
            "\n".join(lines),
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = await _ensure_user(message)
        if user.is_master:
            link = _public_link(settings, user)
            text = (
                f"Привет, {user.display_name}! 👋\n\n"
                "Это <b>Clientika</b> — ваш мини-CRM в Telegram:\n"
                "• услуги, клиенты и записи в одном месте,\n"
                "• автоматические напоминания клиентам,\n"
                "• статистика дохода за день/неделю/месяц.\n\n"
                f"Ваша ссылка для записи клиентов: <code>{link}</code>\n"
                "Откройте приложение и добавьте свои услуги — и можно "
                "делиться ссылкой."
            )
        else:
            text = (
                f"Привет, {user.display_name}! 👋\n\n"
                "Это <b>Clientika</b> — Telegram-бот для онлайн-записи "
                "к мастерам и для самих мастеров (мини-CRM в одном чате).\n\n"
                "Если вы пришли записаться к мастеру — откройте присланную им "
                "ссылку. Если вы сами мастер и хотите получать клиентов "
                "через бота — нажмите «👤 Мой профиль» → «✨ Стать мастером»."
            )
        await message.answer(
            text,
            reply_markup=_main_reply_keyboard(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("profile"))
    async def on_profile_cmd(message: Message) -> None:
        master = await _ensure_user(message)
        await _send_profile(message, master)

    @router.message(F.text == BTN_PROFILE)
    async def on_profile_button(message: Message) -> None:
        master = await _ensure_user(message)
        await _send_profile(message, master)

    @router.message(Command("become_master"))
    async def on_become_master_cmd(message: Message) -> None:
        await _ensure_user(message)
        kb = _become_master_inline_keyboard(settings, include_open_app=False)
        await message.answer(
            "<b>Стать мастером Clientika</b>\n\n"
            f"{settings.become_master_conditions}",
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(F.text == BTN_BECOME_MASTER)
    async def on_become_master_button(message: Message) -> None:
        await on_become_master_cmd(message)

    @router.message(F.text == BTN_OPEN_APP)
    async def on_open_app_button(message: Message) -> None:
        await _ensure_user(message)
        kb = _open_app_keyboard(settings)
        if kb is None:
            await message.answer(
                "Mini App пока не настроен. Свяжитесь с админом — "
                "он включит публичную ссылку."
            )
            return
        await message.answer("Откройте Clientika:", reply_markup=kb)

    @router.message(Command("admin"))
    async def on_admin(message: Message) -> None:
        master = await _ensure_user(message)
        if not master.is_admin:
            await message.answer(
                "Эта команда доступна только админу. "
                "Если вы хотите стать мастером — нажмите «👤 Мой профиль»."
            )
            return

        kb = _open_app_keyboard(settings)
        await message.answer(
            "<b>Админ-панель</b>\n\n"
            "Откройте Mini App и перейдите во вкладку «Админ», чтобы:\n"
            "• увидеть всех пользователей бота,\n"
            "• назначить мастером по TG ID,\n"
            "• выдать или снять права у существующего пользователя.",
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("link"))
    async def on_link(message: Message) -> None:
        master = await _ensure_user(message)
        if not master.is_master:
            await message.answer(
                "Личная ссылка появится у вас, как только админ сделает вас "
                "мастером. Нажмите «👤 Мой профиль» — там есть кнопка "
                "«Стать мастером»."
            )
            return
        link = _public_link(settings, master)
        await message.answer(
            f"Ваша персональная ссылка для клиентов:\n<code>{link}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("today"))
    async def on_today(message: Message) -> None:
        master = await _ensure_user(message)
        if not master.is_master:
            await message.answer(
                "Эта команда доступна только мастерам. "
                "Откройте «👤 Мой профиль», чтобы подать заявку."
            )
            return
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
    async def on_help_cmd(message: Message) -> None:
        await _help(message)

    @router.message(F.text == BTN_HELP)
    async def on_help_button(message: Message) -> None:
        await _help(message)

    async def _help(message: Message) -> None:
        master = await _ensure_user(message)
        lines = [
            "Команды бота:",
            "/start — приветствие",
            "/profile — мой профиль и роль",
            "/become_master — условия и контакт админа",
        ]
        if master.is_master:
            lines += [
                "/link — ваша ссылка для клиентов",
                "/today — записи на сегодня",
            ]
        if master.is_admin:
            lines.append("/admin — открыть админ-панель")
        await message.answer(
            "\n".join(lines),
            reply_markup=_main_reply_keyboard(),
        )

    @router.message(F.web_app_data)
    async def on_webapp_data(message: Message) -> None:
        # Forward-compatible stub: Mini App uses the HTTP API, but if some
        # workflow ever calls Telegram.WebApp.sendData() we don't crash.
        await message.answer("Принято.")

    dp.include_router(router)
    return dp
