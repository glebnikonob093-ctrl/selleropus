"""aiogram handlers for Clientika.

Two audiences share one bot:

* **Masters** open the bot directly (``/start`` with no payload) and get the
  mini-CRM: their shareable booking link plus the Mini App.
* **Clients** arrive through a master's referral deep link
  (``t.me/<bot>?start=<slug>``). They never get master capabilities — only a
  choice to book in the Mini App or right here in the chat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.booking import (
    PastBookingError,
    SlotUnavailableError,
    available_day_slots,
    create_client_booking,
)
from app.config import Settings
from app.db import session_scope
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    Client,
    Master,
    MasterBot,
    Service,
)
from app.notifications import Notifier
from app.repos import (
    add_team_member,
    adjust_schedule_time,
    block_client,
    count_active_master_bots,
    count_bookings,
    count_clients,
    count_masters,
    create_master_bot,
    delete_master_bot,
    delete_master_full,
    get_master_bot,
    get_master_bot_by_bot_id,
    get_master_by_slug,
    get_master_schedule,
    get_revenue,
    init_default_schedule,
    list_active_services,
    list_all_masters,
    list_blocked_clients,
    list_clients_for_master,
    list_day_offs,
    list_team_members,
    remove_team_member,
    toggle_day_off,
    toggle_schedule_day,
    unblock_client,
    upsert_master_from_tg,
)

if TYPE_CHECKING:
    from app.bot.multibot import MultiBotManager

log = logging.getLogger(__name__)

_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_BOOK_DAYS_AHEAD = 14

# Master persistent menu button labels
_M_BTN_TODAY = "📅 Сегодня"
_M_BTN_CLIENTS = "👥 Клиенты"
_M_BTN_STATS = "📊 Статистика"
_M_BTN_LINK = "🔗 Ссылка"
_M_BTN_BOT = "🤖 Мой бот"
_M_BTN_BLOCKED = "🚫 Заблокированные"
_M_BTN_TEAM = "👥 Команда"
_M_BTN_SCHEDULE = "⏰ Расписание"
_M_BTN_HELP = "❓ Помощь"
_M_BTN_ADMIN = "👑 Админ-панель"


class BookingFlow(StatesGroup):
    """Conversational booking states for a client booking inside the bot."""

    service = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


class BlockFlow(StatesGroup):
    """States for blocking a client by TG ID."""

    tg_id = State()


class TeamFlow(StatesGroup):
    """States for adding a team member."""

    tg_id = State()



class BroadcastFlow(StatesGroup):
    """States for admin broadcast."""

    text = State()


_M_BTN_BACK = "◀️ Назад"


def _master_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=_M_BTN_TODAY), KeyboardButton(text=_M_BTN_CLIENTS)],
        [KeyboardButton(text=_M_BTN_STATS), KeyboardButton(text=_M_BTN_LINK)],
        [KeyboardButton(text=_M_BTN_BOT), KeyboardButton(text=_M_BTN_BLOCKED)],
        [KeyboardButton(text=_M_BTN_TEAM), KeyboardButton(text=_M_BTN_SCHEDULE)],
        [KeyboardButton(text=_M_BTN_HELP)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=_M_BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=_M_BTN_BACK)]],
        resize_keyboard=True,
    )


def _client_link(settings: Settings, bot_username: str, master: Master) -> str:
    """The link a master shares with clients. Prefers the bot deep link."""
    if bot_username:
        return f"https://t.me/{bot_username}?start={master.slug}"
    if settings.webapp_url:
        base = settings.webapp_url.rstrip("/")
        return f"{base}?{urlencode({'master': master.slug})}"
    return f"?master={master.slug}"


def _miniapp_url(settings: Settings, slug: str) -> str | None:
    if not settings.webapp_url:
        return None
    base = settings.webapp_url.rstrip("/")
    return f"{base}?{urlencode({'master': slug})}"


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


def _client_choice_keyboard(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Записаться", callback_data=f"bkgo:{slug}")]
        ]
    )


def _cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="✖️ Отмена", callback_data="bkcancel")]


def _services_keyboard(services: list[Service]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{s.name} · {s.price}₽ · {s.duration_minutes}мин",
                callback_data=f"bksvc:{s.id}",
            )
        ]
        for s in services
    ]
    rows.append(_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _days_keyboard(today: datetime) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for offset in range(_BOOK_DAYS_AHEAD):
        d = (today + timedelta(days=offset)).date()
        label = f"{_WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"bkday:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _slots_keyboard(slots: list[datetime]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for slot in slots:
        hhmm = slot.strftime("%H:%M")
        row.append(InlineKeyboardButton(text=hhmm, callback_data=f"bkslot:{hhmm}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="◀️ Другой день", callback_data="bkdays")]
    )
    rows.append(_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier | None = None,
    bot_username: str = "",
    multibot_manager: MultiBotManager | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
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

    async def _get_existing_master(tg_user_id: int) -> Master | None:
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Master).where(Master.tg_user_id == tg_user_id)
            )
            master = res.scalar_one_or_none()
            if master is not None:
                session.expunge(master)
            return master

    # ---- Client referral flow -------------------------------------------------

    async def _start_client_flow(message: Message, slug: str, state: FSMContext) -> None:
        async with session_scope(session_factory) as session:
            master = await get_master_by_slug(session, slug)
            if master is None:
                await message.answer(
                    "Не удалось найти мастера по этой ссылке. "
                    "Попросите свежую ссылку для записи."
                )
                return
            display_name = master.display_name
            services = await list_active_services(session, master.id)

        if not services:
            await message.answer(
                f"{display_name} пока не добавил(а) услуги — записаться нельзя. "
                "Попробуйте позже."
            )
            return

        await state.update_data(master_slug=slug)
        await message.answer(
            f"Запись к мастеру: <b>{display_name}</b>\n\n"
            "Нажмите кнопку ниже, чтобы выбрать услугу и время.",
            reply_markup=_client_choice_keyboard(slug),
            parse_mode="HTML",
        )

    async def _show_services(
        target: Message, slug: str, state: FSMContext
    ) -> None:
        async with session_scope(session_factory) as session:
            master = await get_master_by_slug(session, slug)
            if master is None:
                await target.edit_text("Мастер не найден. Откройте ссылку записи заново.")
                await state.clear()
                return
            services = await list_active_services(session, master.id)
            master_id = master.id

        if not services:
            await target.edit_text("У мастера нет доступных услуг.")
            await state.clear()
            return

        await state.set_state(BookingFlow.service)
        await state.update_data(master_slug=slug, master_id=master_id)
        await target.edit_text(
            "Выберите услугу:", reply_markup=_services_keyboard(services)
        )

    @router.callback_query(F.data.startswith("bkgo:"))
    async def on_book_in_chat(callback: CallbackQuery, state: FSMContext) -> None:
        slug = (callback.data or "").split(":", 1)[1]
        assert isinstance(callback.message, Message)
        await _show_services(callback.message, slug, state)
        await callback.answer()

    @router.callback_query(BookingFlow.service, F.data.startswith("bksvc:"))
    async def on_pick_service(callback: CallbackQuery, state: FSMContext) -> None:
        service_id = int((callback.data or "bksvc:0").split(":", 1)[1])
        data = await state.get_data()
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Service).where(
                    Service.id == service_id,
                    Service.master_id == data.get("master_id", 0),
                )
            )
            service = res.scalar_one_or_none()
            if service is None or not service.is_active:
                await callback.answer("Услуга недоступна", show_alert=True)
                return
            service_name = service.name

        await state.update_data(service_id=service_id, service_name=service_name)
        await state.set_state(BookingFlow.day)
        assert isinstance(callback.message, Message)
        await callback.message.edit_text(
            f"Услуга: <b>{service_name}</b>\nВыберите день:",
            reply_markup=_days_keyboard(datetime.utcnow()),
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(BookingFlow.slot, F.data == "bkdays")
    @router.callback_query(BookingFlow.day, F.data == "bkdays")
    async def on_back_to_days(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(BookingFlow.day)
        assert isinstance(callback.message, Message)
        await callback.message.edit_text(
            "Выберите день:",
            reply_markup=_days_keyboard(datetime.utcnow()),
        )
        await callback.answer()

    @router.callback_query(BookingFlow.day, F.data.startswith("bkday:"))
    async def on_pick_day(callback: CallbackQuery, state: FSMContext) -> None:
        day_iso = (callback.data or "bkday:").split(":", 1)[1]
        day = datetime.fromisoformat(day_iso).date()
        data = await state.get_data()
        async with session_scope(session_factory) as session:
            master = await get_master_by_slug(session, data.get("master_slug", ""))
            res = await session.execute(
                select(Service).where(Service.id == data.get("service_id", 0))
            )
            service = res.scalar_one_or_none()
            if master is None or service is None:
                await callback.answer("Сессия устарела, откройте ссылку заново", show_alert=True)
                await state.clear()
                return
            slots = await available_day_slots(session, master, service, day)

        assert isinstance(callback.message, Message)
        if not slots:
            await callback.answer("На этот день нет свободного времени", show_alert=True)
            return

        await state.update_data(day=day_iso)
        await state.set_state(BookingFlow.slot)
        label = f"{_WEEKDAYS_RU[day.weekday()]} {day.strftime('%d.%m')}"
        await callback.message.edit_text(
            f"Свободное время на {label}:",
            reply_markup=_slots_keyboard(slots),
        )
        await callback.answer()

    @router.callback_query(BookingFlow.slot, F.data.startswith("bkslot:"))
    async def on_pick_slot(callback: CallbackQuery, state: FSMContext) -> None:
        hhmm = (callback.data or "bkslot:").split(":", 1)[1]
        data = await state.get_data()
        day_iso = data.get("day")
        if not day_iso:
            await callback.answer("Сессия устарела, откройте ссылку заново", show_alert=True)
            await state.clear()
            return
        starts_at = datetime.fromisoformat(f"{day_iso}T{hhmm}:00")
        await state.update_data(starts_at=starts_at.isoformat())
        await state.set_state(BookingFlow.name)

        from_user = callback.from_user
        tg_name = (
            f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()
            if from_user
            else ""
        )
        rows: list[list[InlineKeyboardButton]] = []
        if tg_name:
            rows.append(
                [InlineKeyboardButton(text=f"Использовать: {tg_name}", callback_data="bkname")]
            )
        rows.append(_cancel_row())
        assert isinstance(callback.message, Message)
        await callback.message.edit_text(
            "Как вас записать? Напишите имя сообщением"
            + (" или нажмите кнопку ниже." if tg_name else "."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    async def _ask_phone(message: Message, state: FSMContext) -> None:
        await state.set_state(BookingFlow.phone)
        kb = InlineKeyboardMarkup(inline_keyboard=[_cancel_row()])
        await message.answer(
            "Напишите ваш номер телефона для связи.",
            reply_markup=kb,
        )

    @router.callback_query(BookingFlow.name, F.data == "bkname")
    async def on_name_from_tg(callback: CallbackQuery, state: FSMContext) -> None:
        from_user = callback.from_user
        name = (
            f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()
            if from_user
            else ""
        )
        await state.update_data(name=name or "Клиент")
        assert isinstance(callback.message, Message)
        await _ask_phone(callback.message, state)
        await callback.answer()

    @router.message(BookingFlow.name, F.text)
    async def on_name_text(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()[:120] or "Клиент"
        await state.update_data(name=name)
        await _ask_phone(message, state)

    async def _show_confirm(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        starts_at = datetime.fromisoformat(data["starts_at"])
        phone = data.get("phone")
        lines = [
            "Проверьте запись:",
            f"• Услуга: {data.get('service_name', '')}",
            f"• Когда: {starts_at.strftime('%d.%m.%Y %H:%M')}",
            f"• Имя: {data.get('name', '')}",
        ]
        if phone:
            lines.append(f"• Телефон: {phone}")
        await state.set_state(BookingFlow.confirm)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data="bkok")],
                _cancel_row(),
            ]
        )
        await message.answer("\n".join(lines), reply_markup=kb)

    @router.message(BookingFlow.phone, F.text)
    async def on_phone_text(message: Message, state: FSMContext) -> None:
        phone = (message.text or "").strip()[:40] or None
        await state.update_data(phone=phone)
        await _show_confirm(message, state)

    @router.callback_query(BookingFlow.confirm, F.data == "bkok")
    async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        from_user = callback.from_user
        starts_at = datetime.fromisoformat(data["starts_at"])
        assert isinstance(callback.message, Message)

        booking_obj: Booking | None = None
        master_obj: Master | None = None
        service_obj: Service | None = None
        client_obj: Client | None = None
        error: str | None = None

        async with session_scope(session_factory) as session:
            master = await get_master_by_slug(session, data.get("master_slug", ""))
            res = await session.execute(
                select(Service).where(
                    Service.id == data.get("service_id", 0),
                    Service.master_id == (master.id if master else 0),
                )
            )
            service = res.scalar_one_or_none()
            if master is None or service is None or not service.is_active:
                error = "Запись недоступна. Откройте ссылку записи заново."
            else:
                try:
                    booking, client = await create_client_booking(
                        session,
                        master=master,
                        service=service,
                        starts_at=starts_at,
                        name=data.get("name", "Клиент"),
                        phone=data.get("phone"),
                        tg_user_id=from_user.id if from_user else None,
                        tg_username=(from_user.username if from_user else None) or None,
                        source="bot",
                    )
                except PastBookingError:
                    error = "Это время уже прошло. Выберите другой слот."
                except SlotUnavailableError:
                    error = "Это время только что заняли. Выберите другой слот."
                else:
                    booking_obj, client_obj = booking, client
                    master_obj, service_obj = master, service

        if error is not None:
            await callback.message.edit_text(error)
            await state.clear()
            await callback.answer()
            return

        assert booking_obj and master_obj and service_obj and client_obj
        if notifier is not None:
            try:
                await notifier.notify_master_new_booking(
                    master=master_obj,
                    booking=booking_obj,
                    client=client_obj,
                    service=service_obj,
                )
            except Exception:  # pragma: no cover - notification must not break booking
                pass

        await state.clear()
        await callback.message.edit_text(
            "✅ Готово! Вы записаны:\n"
            f"• {service_obj.name}\n"
            f"• {booking_obj.starts_at.strftime('%d.%m.%Y %H:%M')}\n"
            f"• Мастер: {master_obj.display_name}\n\n"
            "Мастер получит уведомление и свяжется при необходимости."
        )
        await callback.answer()

    @router.callback_query(F.data == "bkcancel")
    async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        assert isinstance(callback.message, Message)
        await callback.message.edit_text("Запись отменена. Откройте ссылку снова, чтобы начать заново.")
        await callback.answer()

    # ---- Master commands ------------------------------------------------------

    def _is_admin(tg_user_id: int) -> bool:
        return tg_user_id in settings.admin_tg_user_ids

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        payload = (command.args or "").strip()
        if payload:
            await _start_client_flow(message, payload, state)
            return

        master = await _ensure_master(message)
        link = _client_link(settings, bot_username, master)
        is_admin = _is_admin(master.tg_user_id)

        bot_line = ""
        async with session_scope(session_factory) as session:
            mb = await get_master_bot(session, master.id)
            if mb is not None:
                bot_line = (
                    f"\n🤖 Ваш бот для клиентов: @{mb.bot_username}\n"
                    f"Ссылка: https://t.me/{mb.bot_username}\n"
                )

        text = (
            f"Привет, {master.display_name}! 👋\n\n"
            "Это <b>Clientika</b> — ваш мини-CRM в Telegram:\n"
            "• услуги, клиенты и записи в одном месте,\n"
            "• автоматические напоминания клиентам,\n"
            "• статистика дохода за день/неделю/месяц.\n\n"
            f"Ваша ссылка для записи клиентов: <code>{link}</code>\n"
            + bot_line
            + (
                "\nПодключите своего бота для записи клиентов: /addbot"
                if not bot_line
                else ""
            )
            + ("\n\n👑 Вы администратор платформы." if is_admin else "")
        )
        await message.answer(
            text,
            reply_markup=_master_menu_kb(is_admin),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _show_link(
        message: Message, master: Master, *, back_kb: bool = False
    ) -> None:
        link = _client_link(settings, bot_username, master)
        is_admin = _is_admin(master.tg_user_id)
        kb = _back_kb() if back_kb else _master_menu_kb(is_admin)
        await message.answer(
            f"Ваша персональная ссылка для клиентов:\n<code>{link}</code>",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    async def _show_today(
        message: Message, master: Master, *, back_kb: bool = False
    ) -> None:
        is_admin = _is_admin(master.tg_user_id)
        kb = _back_kb() if back_kb else _master_menu_kb(is_admin)
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
            await message.answer("На сегодня записей нет.", reply_markup=kb)
            return

        lines = [f"<b>Сегодня записей: {len(rows)}</b>"]
        for booking, client, service in rows:
            lines.append(
                f"• {booking.starts_at.strftime('%H:%M')} — {service.name} — "
                f"{client.name}"
                + (f" ({client.phone})" if client.phone else "")
            )
        await message.answer(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb
        )

    @router.message(Command("link"))
    async def on_link(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None:
            await message.answer(
                "Эта команда для мастеров. Если вы хотите записаться — "
                "откройте ссылку, которую дал вам мастер."
            )
            return
        await _show_link(message, master)

    @router.message(Command("today"))
    async def on_today(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None:
            await message.answer(
                "Эта команда для мастеров. Чтобы записаться к мастеру, "
                "откройте присланную им ссылку."
            )
            return
        await _show_today(message, master)

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        is_admin = _is_admin(from_user.id)
        await message.answer(
            "Команды бота:\n"
            "/start — приветствие и меню\n"
            "/link — ваша ссылка для клиентов\n"
            "/today — записи на сегодня\n"
            "/addbot <токен> — подключить бот для записи клиентов\n"
            "/removebot — отключить бот для записи\n"
            "/mybot — информация о подключённом боте\n"
            "/block <tg_id> — заблокировать клиента\n"
            "/unblock <tg_id> — разблокировать клиента\n"
            "/blocked — список заблокированных"
            + ("\n\n👑 Админ-панель доступна через меню" if is_admin else ""),
            reply_markup=_master_menu_kb(is_admin),
        )

    # ---- Master bot management -----------------------------------------------

    @router.message(Command("addbot"))
    async def on_addbot(message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer(
                "Эта команда доступна только мастерам. "
                "Нажмите /start чтобы зарегистрироваться."
            )
            return

        token = (command.args or "").strip()
        if not token:
            await message.answer(
                "Отправьте токен бота после команды:\n"
                "<code>/addbot 123456789:ABCDefghIjklmnop</code>\n\n"
                "Создать бота можно в @BotFather (команда /newbot).",
                parse_mode="HTML",
            )
            return

        # Validate the token via Telegram API
        try:
            temp_bot = Bot(token=token)
            me = await temp_bot.get_me()
            await temp_bot.session.close()
        except Exception:
            await message.answer(
                "Не удалось подключиться с этим токеном. "
                "Проверьте, что токен правильный и бот не заблокирован."
            )
            return

        assert me.id is not None
        assert me.username is not None

        async with session_scope(session_factory) as session:
            # Check if this bot_id is already used by another master
            existing = await get_master_bot_by_bot_id(session, me.id)
            if existing is not None and existing.master_id != master.id:
                await message.answer(
                    "Этот бот уже подключён к другому мастеру."
                )
                return

            # Remove old bot if exists
            old = await get_master_bot(session, master.id)
            if old is not None:
                if multibot_manager is not None:
                    await multibot_manager.remove_bot(master.id)
                await delete_master_bot(session, master.id)

            await create_master_bot(
                session,
                master_id=master.id,
                bot_token=token,
                bot_username=me.username or "",
                bot_id=me.id,
            )

        # Start polling for the new bot
        if multibot_manager is not None:
            await multibot_manager.add_bot(master.id, token)

        await message.answer(
            f"✅ Бот @{me.username} подключён!\n\n"
            f"Клиенты теперь могут записываться через: https://t.me/{me.username}\n"
            "Уведомления о записях будут приходить сюда.",
            disable_web_page_preview=True,
        )

    @router.message(Command("removebot"))
    async def on_removebot(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        async with session_scope(session_factory) as session:
            mb = await get_master_bot(session, master.id)
            if mb is None:
                await message.answer("У вас нет подключённого бота.")
                return
            bot_username = mb.bot_username
            if multibot_manager is not None:
                await multibot_manager.remove_bot(master.id)
            await delete_master_bot(session, master.id)

        await message.answer(
            f"Бот @{bot_username} отключён. Клиенты больше не смогут "
            "записываться через него."
        )

    @router.message(Command("mybot"))
    async def on_mybot(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        async with session_scope(session_factory) as session:
            mb = await get_master_bot(session, master.id)

        if mb is None:
            await message.answer(
                "У вас нет подключённого бота.\n"
                "Создайте бота в @BotFather и подключите:\n"
                "<code>/addbot ТОКЕН</code>",
                parse_mode="HTML",
            )
            return

        running = multibot_manager.is_running(master.id) if multibot_manager else False
        status = "✅ работает" if running else "⚠️ остановлен"
        await message.answer(
            f"Ваш бот: @{mb.bot_username}\n"
            f"Статус: {status}\n"
            f"Ссылка для клиентов: https://t.me/{mb.bot_username}",
            disable_web_page_preview=True,
        )

    # ---- Block / Unblock commands for masters ---------------------------------

    @router.message(Command("block"))
    async def on_block(message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Укажите Telegram ID клиента:\n"
                "<code>/block 123456789</code>\n\n"
                "ID клиента можно найти в списке клиентов (👥 Клиенты).",
                parse_mode="HTML",
            )
            return
        try:
            tg_user_id = int(raw)
        except ValueError:
            await message.answer("Неверный формат ID. Укажите числовой Telegram ID.")
            return

        async with session_scope(session_factory) as session:
            await block_client(session, master.id, tg_user_id)
        await message.answer(
            f"🚫 Клиент с ID <code>{tg_user_id}</code> заблокирован.\n"
            "Он больше не сможет записаться через вашего бота.",
            parse_mode="HTML",
            reply_markup=_master_menu_kb(_is_admin(from_user.id)),
        )

    @router.message(Command("unblock"))
    async def on_unblock(message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Укажите Telegram ID клиента:\n"
                "<code>/unblock 123456789</code>",
                parse_mode="HTML",
            )
            return
        try:
            tg_user_id = int(raw)
        except ValueError:
            await message.answer("Неверный формат ID.")
            return

        async with session_scope(session_factory) as session:
            removed = await unblock_client(session, master.id, tg_user_id)
        if removed:
            await message.answer(
                f"✅ Клиент с ID <code>{tg_user_id}</code> разблокирован.",
                parse_mode="HTML",
            )
        else:
            await message.answer("Этот клиент не был заблокирован.")

    @router.message(Command("blocked"))
    async def on_blocked_list(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        async with session_scope(session_factory) as session:
            blocked = await list_blocked_clients(session, master.id)
        if not blocked:
            await message.answer("Нет заблокированных клиентов.")
            return
        lines = ["<b>Заблокированные клиенты:</b>"]
        for bc in blocked:
            lines.append(f"• ID: <code>{bc.tg_user_id}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")

    # ---- Master menu button handlers ------------------------------------------

    @router.message(F.text == _M_BTN_BACK)
    async def on_btn_back_to_menu(message: Message, state: FSMContext) -> None:
        from_user = message.from_user
        assert from_user is not None
        await state.clear()
        is_admin = _is_admin(from_user.id)
        await message.answer(
            "Главное меню:",
            reply_markup=_master_menu_kb(is_admin),
        )

    @router.message(F.text == _M_BTN_TODAY)
    async def on_btn_today(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return
        await _show_today(message, master, back_kb=True)

    _CLIENTS_PAGE_SIZE = 5

    async def _build_clients_page(
        master_id: int, page: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build paginated clients message text + inline keyboard."""
        async with session_scope(session_factory) as session:
            clients = await list_clients_for_master(session, master_id)
            blocked_ids: set[int] = set()
            blocked_list = await list_blocked_clients(session, master_id)
            for bc in blocked_list:
                blocked_ids.add(bc.tg_user_id)

        total = len(clients)
        if total == 0:
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            return "У вас пока нет клиентов.", kb

        total_pages = (total + _CLIENTS_PAGE_SIZE - 1) // _CLIENTS_PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        start = page * _CLIENTS_PAGE_SIZE
        page_items = clients[start : start + _CLIENTS_PAGE_SIZE]

        lines = [f"<b>👥 Клиенты ({total}):</b>\n"]
        for c in page_items:
            is_blocked = bool(c.tg_user_id and c.tg_user_id in blocked_ids)
            status_icon = " 🚫" if is_blocked else ""
            note_icon = " 📝" if c.notes else ""
            lines.append(f"👤 <b>{c.name}</b>{status_icon}{note_icon}")

        lines.append(f"\nСтраница {page + 1}/{total_pages} · Всего: {total}")

        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(text="◀️", callback_data=f"mclients:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="mcl:noop"
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(text="▶️", callback_data=f"mclients:{page + 1}")
            )

        detail_rows: list[list[InlineKeyboardButton]] = []
        for c in page_items:
            detail_rows.append([
                InlineKeyboardButton(
                    text=f"📋 {c.name[:25]}",
                    callback_data=f"mcdet:{c.id}:{page}",
                )
            ])

        kb = InlineKeyboardMarkup(inline_keyboard=[nav_row, *detail_rows])
        return "\n".join(lines), kb

    _STATUS_LABELS_RU = {
        "new": "Новая",
        "confirmed": "Подтверждена",
        "came": "Пришёл",
        "cancelled": "Отменена",
        "no_show": "Не пришёл",
    }

    async def _show_client_detail(
        message: Message, master_id: int, client_id: int, back_page: int
    ) -> None:
        """Show a detailed client card: notes, booking history, stats."""
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Client).where(
                    Client.id == client_id, Client.master_id == master_id
                )
            )
            client = res.scalar_one_or_none()
            if client is None:
                await message.edit_text("Клиент не найден.")
                return

            bookings_res = await session.execute(
                select(Booking, Service.name)
                .join(Service, Service.id == Booking.service_id)
                .where(Booking.client_id == client.id)
                .order_by(Booking.starts_at.desc())
            )
            bookings_rows = bookings_res.all()

            blocked_list = await list_blocked_clients(session, master_id)
            blocked_ids = {bc.tg_user_id for bc in blocked_list}

        is_blocked = bool(client.tg_user_id and client.tg_user_id in blocked_ids)

        lines = [f"<b>👤 {client.name}</b>"]
        if is_blocked:
            lines[0] += " 🚫"
        if client.phone:
            lines.append(f"📱 {client.phone}")
        if client.tg_username:
            username = client.tg_username.lstrip("@")
            lines.append(f'💬 <a href="https://t.me/{username}">@{username}</a>')
        if client.tg_user_id:
            lines.append(f"🆔 <code>{client.tg_user_id}</code>")
        if client.notes:
            lines.append(f"\n📝 <i>{client.notes}</i>")

        total_bookings = len(bookings_rows)
        came_count = sum(1 for b, _ in bookings_rows if b.status == "came")
        total_revenue = sum(b.price_snapshot for b, _ in bookings_rows if b.status == "came")
        cancelled_count = sum(
            1 for b, _ in bookings_rows if b.status in ("cancelled", "no_show")
        )

        lines.append("\n<b>📊 Статистика:</b>")
        lines.append(f"Всего записей: {total_bookings}")
        lines.append(f"Пришёл: {came_count}")
        lines.append(f"Отменил/не пришёл: {cancelled_count}")
        lines.append(f"Доход: {total_revenue} ₽")

        if bookings_rows:
            lines.append("\n<b>📋 Последние записи:</b>")
            for b, svc_name in bookings_rows[:5]:
                dt = b.starts_at.strftime("%d.%m %H:%M")
                status_label = _STATUS_LABELS_RU.get(b.status, b.status)
                lines.append(f"• {dt} — {svc_name} ({status_label})")

        buttons: list[list[InlineKeyboardButton]] = []
        if client.tg_user_id:
            if is_blocked:
                buttons.append([
                    InlineKeyboardButton(
                        text="✅ Разблокировать",
                        callback_data=f"mcdunblk:{client.id}:{back_page}",
                    )
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(
                        text="🚫 Заблокировать",
                        callback_data=f"mcdblk:{client.id}:{back_page}",
                    )
                ])
        buttons.append([
            InlineKeyboardButton(
                text="◀️ К списку",
                callback_data=f"mclients:{back_page}",
            )
        ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb,
            disable_web_page_preview=True,
        )

    @router.message(F.text == _M_BTN_CLIENTS)
    async def on_btn_clients(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return

        text, kb = await _build_clients_page(master.id, 0)
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("mclients:"))
    async def on_clients_page(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        page = int((callback.data or "mclients:0").split(":", 1)[1])
        text, kb = await _build_clients_page(master.id, page)
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await callback.answer()

    @router.callback_query(F.data == "mcl:noop")
    async def on_clients_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data.startswith("mcdet:"))
    async def on_client_detail(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        parts = (callback.data or "").split(":")
        client_id = int(parts[1])
        back_page = int(parts[2]) if len(parts) > 2 else 0
        await _show_client_detail(callback.message, master.id, client_id, back_page)
        await callback.answer()

    @router.callback_query(F.data.startswith("mcdblk:"))
    async def on_client_detail_block(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        parts = (callback.data or "").split(":")
        client_id = int(parts[1])
        back_page = int(parts[2]) if len(parts) > 2 else 0
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Client).where(
                    Client.id == client_id, Client.master_id == master.id
                )
            )
            client = res.scalar_one_or_none()
            if client and client.tg_user_id:
                await block_client(session, master.id, client.tg_user_id)
        await _show_client_detail(callback.message, master.id, client_id, back_page)
        await callback.answer("Заблокирован", show_alert=True)

    @router.callback_query(F.data.startswith("mcdunblk:"))
    async def on_client_detail_unblock(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        parts = (callback.data or "").split(":")
        client_id = int(parts[1])
        back_page = int(parts[2]) if len(parts) > 2 else 0
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Client).where(
                    Client.id == client_id, Client.master_id == master.id
                )
            )
            client = res.scalar_one_or_none()
            if client and client.tg_user_id:
                await unblock_client(session, master.id, client.tg_user_id)
        await _show_client_detail(callback.message, master.id, client_id, back_page)
        await callback.answer("Разблокирован", show_alert=True)

    @router.message(F.text == _M_BTN_STATS)
    async def on_btn_stats(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return

        now = datetime.utcnow()
        today_start = datetime(now.year, now.month, now.day)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = datetime(now.year, now.month, 1)

        async with session_scope(session_factory) as session:
            today_rev = await get_revenue(session, master.id, today_start, today_start + timedelta(days=1))
            week_rev = await get_revenue(session, master.id, week_start, today_start + timedelta(days=1))
            month_rev = await get_revenue(session, master.id, month_start, today_start + timedelta(days=1))

            today_count_res = await session.execute(
                select(Booking).where(
                    Booking.master_id == master.id,
                    Booking.starts_at >= today_start,
                    Booking.starts_at < today_start + timedelta(days=1),
                    Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                )
            )
            today_count = len(list(today_count_res.scalars()))

            total_clients_res = await session.execute(
                select(Client).where(Client.master_id == master.id)
            )
            total_clients = len(list(total_clients_res.scalars()))

        await message.answer(
            "<b>📊 Ваша статистика</b>\n\n"
            f"Записей сегодня: {today_count}\n"
            f"Всего клиентов: {total_clients}\n\n"
            f"<b>Доход (завершённые):</b>\n"
            f"• Сегодня: {today_rev} ₽\n"
            f"• Неделя: {week_rev} ₽\n"
            f"• Месяц: {month_rev} ₽",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    @router.message(F.text == _M_BTN_LINK)
    async def on_btn_link(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return
        await _show_link(message, master, back_kb=True)

    @router.message(F.text == _M_BTN_BOT)
    async def on_btn_bot(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return

        async with session_scope(session_factory) as session:
            mb = await get_master_bot(session, master.id)

        if mb is None:
            await message.answer(
                "У вас нет подключённого бота.\n"
                "Создайте бота в @BotFather и подключите:\n"
                "<code>/addbot ТОКЕН</code>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            return

        running = multibot_manager.is_running(master.id) if multibot_manager else False
        status = "✅ работает" if running else "⚠️ остановлен"
        await message.answer(
            f"Ваш бот: @{mb.bot_username}\n"
            f"Статус: {status}\n"
            f"Ссылка для клиентов: https://t.me/{mb.bot_username}",
            disable_web_page_preview=True,
            reply_markup=_back_kb(),
        )

    @router.message(F.text == _M_BTN_HELP)
    async def on_btn_help(message: Message) -> None:
        await message.answer(
            "Команды бота:\n"
            "/start — приветствие и меню\n"
            "/link — ваша ссылка для клиентов\n"
            "/today — записи на сегодня\n"
            "/addbot <токен> — подключить бот для записи\n"
            "/removebot — отключить бот для записи\n"
            "/mybot — информация о боте\n"
            "/block <tg_id> — заблокировать клиента\n"
            "/unblock <tg_id> — разблокировать клиента\n"
            "/blocked — список заблокированных",
            reply_markup=_back_kb(),
        )

    # ---- Blocked list button handler -------------------------------------------

    @router.message(F.text == _M_BTN_BLOCKED)
    async def on_btn_blocked(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return

        async with session_scope(session_factory) as session:
            blocked = await list_blocked_clients(session, master.id)

        if not blocked:
            await message.answer(
                "Нет заблокированных клиентов.",
                reply_markup=_back_kb(),
            )
            return

        for bc in blocked:
            text = f"🚫 TG ID: <code>{bc.tg_user_id}</code>"
            if bc.reason:
                text += f"\nПричина: {bc.reason}"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="✅ Разблокировать",
                        callback_data=f"munblk:{bc.tg_user_id}",
                    )]
                ]
            )
            await message.answer(text, parse_mode="HTML", reply_markup=kb)

    # ---- Team management button handler ----------------------------------------

    @router.message(F.text == _M_BTN_TEAM)
    async def on_btn_team(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return

        async with session_scope(session_factory) as session:
            members = await list_team_members(session, master.id)

        lines = ["<b>Ваша команда</b>\n"]
        if not members:
            lines.append("Пока нет участников команды.")
        else:
            for tm in members:
                tg_link = ""
                if tm.tg_username:
                    username = tm.tg_username.lstrip("@")
                    tg_link = f' · <a href="https://t.me/{username}">@{username}</a>'
                lines.append(f"• {tm.display_name or 'Участник'}{tg_link} · <code>{tm.tg_user_id}</code>")

        lines.append(
            "\nДобавить: <code>/addteam TG_ID Имя</code>\n"
            "Удалить: <code>/removeteam TG_ID</code>"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить участника", callback_data="team:add")],
            ]
        )
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb if not members else InlineKeyboardMarkup(
                inline_keyboard=[
                    *[
                        [InlineKeyboardButton(
                            text=f"❌ {tm.display_name or tm.tg_user_id}",
                            callback_data=f"teamrm:{tm.tg_user_id}",
                        )]
                        for tm in members
                    ],
                    [InlineKeyboardButton(text="➕ Добавить", callback_data="team:add")],
                ]
            ),
        )

    @router.message(Command("addteam"))
    async def on_addteam(message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Укажите TG ID и имя участника:\n"
                "<code>/addteam 123456789 Имя</code>",
                parse_mode="HTML",
            )
            return

        parts = raw.split(maxsplit=1)
        try:
            tg_user_id = int(parts[0])
        except ValueError:
            await message.answer("Неверный формат TG ID.")
            return

        display_name = parts[1] if len(parts) > 1 else ""
        async with session_scope(session_factory) as session:
            await add_team_member(session, master.id, tg_user_id, display_name=display_name)
        await message.answer(
            f"✅ Участник <code>{tg_user_id}</code> добавлен в команду.\n"
            "Теперь ему будут приходить уведомления о записях.",
            parse_mode="HTML",
            reply_markup=_master_menu_kb(_is_admin(from_user.id)),
        )

    @router.message(Command("removeteam"))
    async def on_removeteam(message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await message.answer("Эта команда доступна только мастерам.")
            return

        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Укажите TG ID участника:\n"
                "<code>/removeteam 123456789</code>",
                parse_mode="HTML",
            )
            return

        try:
            tg_user_id = int(raw)
        except ValueError:
            await message.answer("Неверный формат TG ID.")
            return

        async with session_scope(session_factory) as session:
            removed = await remove_team_member(session, master.id, tg_user_id)
        if removed:
            await message.answer(f"✅ Участник <code>{tg_user_id}</code> удалён из команды.", parse_mode="HTML")
        else:
            await message.answer("Этот участник не найден в команде.")

    @router.callback_query(F.data == "team:add")
    async def on_team_add_prompt(callback: CallbackQuery, state: FSMContext) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        await state.set_state(TeamFlow.tg_id)
        await callback.message.answer(
            "Отправьте TG ID и имя участника в формате:\n"
            "<code>123456789 Имя</code>\n\n"
            "Для отмены отправьте /start",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(TeamFlow.tg_id, F.text)
    async def on_team_add_input(message: Message, state: FSMContext) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await state.clear()
            return

        raw = (message.text or "").strip()
        parts = raw.split(maxsplit=1)
        try:
            tg_user_id = int(parts[0])
        except (ValueError, IndexError):
            await message.answer("Неверный формат. Укажите TG ID и имя через пробел.")
            return

        display_name = parts[1] if len(parts) > 1 else ""
        async with session_scope(session_factory) as session:
            await add_team_member(session, master.id, tg_user_id, display_name=display_name)
        await state.clear()
        await message.answer(
            f"✅ Участник <code>{tg_user_id}</code> добавлен в команду.",
            parse_mode="HTML",
            reply_markup=_master_menu_kb(_is_admin(from_user.id)),
        )

    @router.callback_query(F.data.startswith("teamrm:"))
    async def on_team_remove(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return

        tg_user_id = int((callback.data or "teamrm:0").split(":", 1)[1])
        async with session_scope(session_factory) as session:
            await remove_team_member(session, master.id, tg_user_id)
        await callback.answer(f"Участник {tg_user_id} удалён", show_alert=True)

    # ---- Schedule management ---------------------------------------------------

    _WEEKDAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    _WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    def _minutes_to_hhmm(m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    async def _show_schedule(message: Message, master_id: int, *, edit: bool = False) -> None:
        async with session_scope(session_factory) as session:
            schedule = await get_master_schedule(session, master_id)
            if not schedule:
                schedule = await init_default_schedule(session, master_id)
            res = await session.execute(
                select(Master.book_days_ahead).where(Master.id == master_id)
            )
            book_days = res.scalar_one_or_none() or 30

        lines = ["<b>⏰ Ваше расписание</b>\n"]
        for row in schedule:
            wd = _WEEKDAYS_FULL[row.weekday]
            if row.is_working:
                lines.append(
                    f"✅ <b>{wd}</b>: {_minutes_to_hhmm(row.start_minutes)} — "
                    f"{_minutes_to_hhmm(row.end_minutes)}"
                )
            else:
                lines.append(f"❌ <b>{wd}</b>: выходной")
        lines.append(f"\n📆 Запись открыта на <b>{book_days}</b> дн. вперёд")

        buttons: list[list[InlineKeyboardButton]] = []
        row1: list[InlineKeyboardButton] = []
        row2: list[InlineKeyboardButton] = []
        for i, row in enumerate(schedule):
            icon = "✅" if row.is_working else "❌"
            btn = InlineKeyboardButton(
                text=f"{icon} {_WEEKDAYS_SHORT[i]}",
                callback_data=f"sched:day:{i}",
            )
            if i < 4:
                row1.append(btn)
            else:
                row2.append(btn)
        buttons.append(row1)
        buttons.append(row2)
        buttons.append([
            InlineKeyboardButton(text="◀️", callback_data="sched:horizon:-7"),
            InlineKeyboardButton(text=f"📆 {book_days} дн.", callback_data="sched:noop"),
            InlineKeyboardButton(text="▶️", callback_data="sched:horizon:7"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📅 Выходные дни", callback_data="sched:offs:0"),
        ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        if edit:
            await message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)

    @router.message(F.text == _M_BTN_SCHEDULE)
    async def on_btn_schedule(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            return
        await _show_schedule(message, master.id)

    async def _show_schedule_day(message: Message, master_id: int, weekday: int) -> None:
        async with session_scope(session_factory) as session:
            schedule = await get_master_schedule(session, master_id)
            if not schedule:
                schedule = await init_default_schedule(session, master_id)

        row = next((s for s in schedule if s.weekday == weekday), None)
        if row is None:
            return

        wd_name = _WEEKDAYS_FULL[weekday]
        status = "Рабочий день ✅" if row.is_working else "Выходной ❌"
        text = (
            f"<b>⚙️ {wd_name}</b>\n\n"
            f"Статус: {status}\n"
            f"Начало: {_minutes_to_hhmm(row.start_minutes)}\n"
            f"Конец: {_minutes_to_hhmm(row.end_minutes)}"
        )
        start_hm = _minutes_to_hhmm(row.start_minutes)
        end_hm = _minutes_to_hhmm(row.end_minutes)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Вкл/Выкл",
                    callback_data=f"sched:toggle:{weekday}",
                )],
                [
                    InlineKeyboardButton(text="◀️", callback_data=f"sched:adj:{weekday}:start:-30"),
                    InlineKeyboardButton(text=f"🌅 {start_hm}", callback_data="sched:noop"),
                    InlineKeyboardButton(text="▶️", callback_data=f"sched:adj:{weekday}:start:30"),
                ],
                [
                    InlineKeyboardButton(text="◀️", callback_data=f"sched:adj:{weekday}:end:-30"),
                    InlineKeyboardButton(text=f"🌙 {end_hm}", callback_data="sched:noop"),
                    InlineKeyboardButton(text="▶️", callback_data=f"sched:adj:{weekday}:end:30"),
                ],
                [InlineKeyboardButton(text="◀️ К расписанию", callback_data="sched:back")],
            ]
        )
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("sched:day:"))
    async def on_schedule_day(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        weekday = int((callback.data or "sched:day:0").split(":", 2)[2])
        await _show_schedule_day(callback.message, master.id, weekday)
        await callback.answer()

    @router.callback_query(F.data.startswith("sched:toggle:"))
    async def on_schedule_toggle(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        weekday = int((callback.data or "sched:toggle:0").split(":", 2)[2])
        async with session_scope(session_factory) as session:
            await toggle_schedule_day(session, master.id, weekday)
        await _show_schedule_day(callback.message, master.id, weekday)
        await callback.answer()

    @router.callback_query(F.data.startswith("sched:adj:"))
    async def on_schedule_adjust(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        parts = (callback.data or "").split(":")
        weekday = int(parts[2])
        field = parts[3]
        delta = int(parts[4])
        async with session_scope(session_factory) as session:
            await adjust_schedule_time(session, master.id, weekday, field, delta)
        await _show_schedule_day(callback.message, master.id, weekday)
        await callback.answer()

    @router.callback_query(F.data.startswith("sched:horizon:"))
    async def on_schedule_horizon(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        delta = int((callback.data or "sched:horizon:0").split(":", 2)[2])
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Master).where(Master.id == master.id)
            )
            m = res.scalar_one_or_none()
            if m is not None:
                new_val = max(7, min(90, m.book_days_ahead + delta))
                m.book_days_ahead = new_val
        await _show_schedule(callback.message, master.id, edit=True)
        await callback.answer()

    @router.callback_query(F.data == "sched:back")
    async def on_schedule_back(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        await _show_schedule(callback.message, master.id, edit=True)
        await callback.answer()

    _MONTH_NAMES = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ]

    async def _show_day_offs(message: Message, master_id: int, month_offset: int) -> None:
        today = datetime.utcnow().date()
        first = (today.replace(day=1) + timedelta(days=32 * month_offset)).replace(day=1)

        async with session_scope(session_factory) as session:
            offs = await list_day_offs(session, master_id)
        off_dates = {o.day for o in offs}

        text = (
            f"<b>📅 Выходные дни — {_MONTH_NAMES[first.month - 1]} {first.year}</b>\n\n"
            "Нажмите на дату, чтобы добавить/убрать выходной.\n"
            "🔴 = выходной"
        )

        rows: list[list[InlineKeyboardButton]] = []
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"sched:offs:{month_offset - 1}"),
            InlineKeyboardButton(
                text=f"{_MONTH_NAMES[first.month - 1]} {first.year}",
                callback_data="sched:noop",
            ),
            InlineKeyboardButton(text="▶️", callback_data=f"sched:offs:{month_offset + 1}"),
        ])
        rows.append([
            InlineKeyboardButton(text=wd, callback_data="sched:noop")
            for wd in _WEEKDAYS_SHORT
        ])

        d = first
        week: list[InlineKeyboardButton] = []
        for _ in range(first.weekday()):
            week.append(InlineKeyboardButton(text=" ", callback_data="sched:noop"))

        while d.month == first.month:
            is_off = d in off_dates
            label = f"🔴{d.day}" if is_off else str(d.day)
            week.append(InlineKeyboardButton(
                text=label,
                callback_data=f"sched:toff:{d.isoformat()}:{month_offset}",
            ))
            if len(week) == 7:
                rows.append(week)
                week = []
            d += timedelta(days=1)

        if week:
            while len(week) < 7:
                week.append(InlineKeyboardButton(text=" ", callback_data="sched:noop"))
            rows.append(week)

        rows.append([InlineKeyboardButton(text="◀️ К расписанию", callback_data="sched:back")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("sched:offs:"))
    async def on_schedule_day_offs(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)
        month_offset = int((callback.data or "sched:offs:0").split(":", 2)[2])
        await _show_day_offs(callback.message, master.id, month_offset)
        await callback.answer()

    @router.callback_query(F.data.startswith("sched:toff:"))
    async def on_toggle_day_off(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        parts = (callback.data or "").split(":")
        day_iso = parts[2]
        month_offset = int(parts[3])
        day = datetime.fromisoformat(day_iso).date()

        async with session_scope(session_factory) as session:
            is_off = await toggle_day_off(session, master.id, day)

        status = "добавлен выходной" if is_off else "выходной убран"
        await callback.answer(f"{day.strftime('%d.%m')} — {status}")
        await _show_day_offs(callback.message, master.id, month_offset)

    @router.callback_query(F.data == "sched:noop")
    async def on_sched_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    # ---- Admin panel ----------------------------------------------------------

    _ADM_PAGE_SIZE = 5

    def _admin_menu_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📊 Статистика платформы", callback_data="adm:stats")],
                [InlineKeyboardButton(text="🤖 Боты и мастера", callback_data="adm:bots:0")],
                [InlineKeyboardButton(text="📢 Рассылка мастерам", callback_data="adm:broadcast")],
            ]
        )

    @router.message(F.text == _M_BTN_ADMIN)
    async def on_btn_admin(message: Message) -> None:
        from_user = message.from_user
        assert from_user is not None
        if not _is_admin(from_user.id):
            return

        await message.answer(
            "👑 <b>Админ-панель Clientika</b>\n\nВыберите раздел:",
            reply_markup=_admin_menu_kb(),
            parse_mode="HTML",
        )

    @router.callback_query(F.data == "adm:stats")
    async def on_admin_stats(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        if not _is_admin(from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        now = datetime.utcnow()
        today_start = datetime(now.year, now.month, now.day)

        async with session_scope(session_factory) as session:
            n_masters = await count_masters(session)
            n_clients = await count_clients(session)
            n_bookings = await count_bookings(session)
            n_bots = await count_active_master_bots(session)

            today_bookings_res = await session.execute(
                select(Booking).where(
                    Booking.starts_at >= today_start,
                    Booking.starts_at < today_start + timedelta(days=1),
                    Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                )
            )
            today_bookings = len(list(today_bookings_res.scalars()))

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")],
            ]
        )
        await callback.message.edit_text(
            "<b>📊 Статистика платформы</b>\n\n"
            f"Мастеров: {n_masters}\n"
            f"Клиентов: {n_clients}\n"
            f"Записей всего: {n_bookings}\n"
            f"Записей сегодня: {today_bookings}\n"
            f"Активных ботов: {n_bots}",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("adm:bots:"))
    async def on_admin_bots_page(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        if not _is_admin(from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        page = int((callback.data or "adm:bots:0").split(":", 2)[2])

        # Collect all masters with their bot info
        async with session_scope(session_factory) as session:
            masters = await list_all_masters(session)
            items: list[tuple[int, str, str | None, int, str | None, bool]] = []
            for m in masters:
                bot_res = await session.execute(
                    select(MasterBot).where(
                        MasterBot.master_id == m.id, MasterBot.is_active.is_(True)
                    )
                )
                mb = bot_res.scalar_one_or_none()
                bot_un = mb.bot_username if mb else None
                running = (
                    multibot_manager.is_running(m.id)
                    if multibot_manager and mb
                    else False
                )
                items.append(
                    (m.id, m.display_name, m.tg_username, m.tg_user_id, bot_un, running)
                )

        total = len(items)
        if total == 0:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")],
                ]
            )
            await callback.message.edit_text(
                "Нет зарегистрированных мастеров.", reply_markup=kb
            )
            await callback.answer()
            return

        total_pages = (total + _ADM_PAGE_SIZE - 1) // _ADM_PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        start = page * _ADM_PAGE_SIZE
        page_items = items[start : start + _ADM_PAGE_SIZE]

        lines = [f"<b>🤖 Боты и мастера ({total}):</b>\n"]
        for _mid, name, tg_un, tg_uid, bot_un, running in page_items:
            master_link = ""
            if tg_un:
                master_link = f' · <a href="https://t.me/{tg_un}">@{tg_un}</a>'
            bot_line = ""
            if bot_un:
                status = "✅" if running else "⚠️"
                bot_line = (
                    f"\n   🤖 <a href=\"https://t.me/{bot_un}\">@{bot_un}</a> {status}"
                )
            else:
                bot_line = "\n   🤖 нет бота"
            lines.append(
                f"👤 <b>{name}</b>{master_link}\n"
                f"   TG ID: <code>{tg_uid}</code>{bot_line}"
            )

        lines.append(f"\nСтраница {page + 1}/{total_pages} · Всего: {total}")

        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(text="◀️", callback_data=f"adm:bots:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data="adm:noop"
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(text="▶️", callback_data=f"adm:bots:{page + 1}")
            )

        del_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text=f"🗑 {name[:20]}",
                    callback_data=f"adm:delmaster:{mid}",
                )
            ]
            for mid, name, _tg_un, _tg_uid, _bot_un, _running in page_items
        ]

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                nav_row,
                *del_rows,
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")],
            ]
        )
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await callback.answer()

    @router.callback_query(F.data == "adm:noop")
    async def on_admin_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data == "adm:broadcast")
    async def on_admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext) -> None:
        from_user = callback.from_user
        if not _is_admin(from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        await state.set_state(BroadcastFlow.text)
        await callback.message.edit_text(
            "📢 <b>Рассылка мастерам</b>\n\n"
            "Отправьте текст сообщения, которое получат все мастера.\n"
            "Для отмены отправьте /start",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(BroadcastFlow.text, F.text)
    async def on_broadcast_text(message: Message, state: FSMContext) -> None:
        from_user = message.from_user
        assert from_user is not None
        if not _is_admin(from_user.id):
            await state.clear()
            return

        broadcast_text = (message.text or "").strip()
        if not broadcast_text:
            await message.answer("Сообщение пустое.")
            return

        await state.clear()
        async with session_scope(session_factory) as session:
            masters = await list_all_masters(session)
            for m in masters:
                session.expunge(m)

        sent = 0
        for m in masters:
            if notifier is not None:
                ok = await notifier._safe_send(
                    m.tg_chat_id,
                    f"📢 Сообщение от администрации Clientika:\n\n{broadcast_text}",
                )
                if ok:
                    sent += 1

        await message.answer(
            f"✅ Рассылка отправлена: {sent}/{len(masters)} мастерам.",
            reply_markup=_master_menu_kb(True),
        )

    @router.callback_query(F.data.startswith("adm:delmaster:"))
    async def on_admin_delete_master(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        if not _is_admin(from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        master_id = int((callback.data or "adm:delmaster:0").split(":", 2)[2])

        # Stop the master's bot if running
        if multibot_manager is not None:
            await multibot_manager.stop_bot(master_id)

        async with session_scope(session_factory) as session:
            deleted = await delete_master_full(session, master_id)

        if deleted:
            await callback.message.edit_text("🗑 Мастер удалён.")
            await callback.answer("Мастер удалён", show_alert=True)
        else:
            await callback.answer("Мастер не найден", show_alert=True)

    @router.callback_query(F.data == "adm:back")
    async def on_admin_back(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        if not _is_admin(from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        assert isinstance(callback.message, Message)

        await callback.message.edit_text(
            "👑 <b>Админ-панель Clientika</b>\n\nВыберите раздел:",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
        await callback.answer()

    # ---- Inline block/unblock from client list --------------------------------

    @router.callback_query(F.data.startswith("mblk:"))
    async def on_inline_block(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        assert isinstance(callback.message, Message)
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return

        tg_user_id = int((callback.data or "mblk:0").split(":", 1)[1])
        async with session_scope(session_factory) as session:
            await block_client(session, master.id, tg_user_id)
        await callback.answer(f"Клиент {tg_user_id} заблокирован", show_alert=True)

    @router.callback_query(F.data.startswith("munblk:"))
    async def on_inline_unblock(callback: CallbackQuery) -> None:
        from_user = callback.from_user
        assert isinstance(callback.message, Message)
        master = await _get_existing_master(from_user.id)
        if master is None or not master.is_master:
            await callback.answer("Нет доступа", show_alert=True)
            return

        tg_user_id = int((callback.data or "munblk:0").split(":", 1)[1])
        async with session_scope(session_factory) as session:
            await unblock_client(session, master.id, tg_user_id)
        await callback.answer(f"Клиент {tg_user_id} разблокирован", show_alert=True)

    @router.message(F.web_app_data)
    async def on_webapp_data(message: Message) -> None:
        await message.answer("Принято.")

    dp.include_router(router)
    return dp
