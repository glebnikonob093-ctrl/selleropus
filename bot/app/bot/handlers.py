"""aiogram handlers for Clientika.

Two audiences share one bot:

* **Masters** open the bot directly (``/start`` with no payload) and get the
  mini-CRM: their shareable booking link plus the Mini App.
* **Clients** arrive through a master's referral deep link
  (``t.me/<bot>?start=<slug>``). They never get master capabilities — only a
  choice to book in the Mini App or right here in the chat.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
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
    Service,
)
from app.notifications import Notifier
from app.repos import (
    get_master_by_slug,
    list_active_services,
    upsert_master_from_tg,
)

_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_BOOK_DAYS_AHEAD = 14


class BookingFlow(StatesGroup):
    """Conversational booking states for a client booking inside the bot."""

    service = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


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

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        payload = (command.args or "").strip()
        if payload:
            await _start_client_flow(message, payload, state)
            return

        master = await _ensure_master(message)
        link = _client_link(settings, bot_username, master)
        text = (
            f"Привет, {master.display_name}! 👋\n\n"
            "Это <b>Clientika</b> — ваш мини-CRM в Telegram:\n"
            "• услуги, клиенты и записи в одном месте,\n"
            "• автоматические напоминания клиентам,\n"
            "• статистика дохода за день/неделю/месяц.\n\n"
            f"Ваша ссылка для записи клиентов: <code>{link}</code>\n"
            "Клиент откроет её и сам выберет — записаться в приложении или прямо в чате с ботом."
        )
        await message.answer(
            text,
            reply_markup=_open_app_keyboard(settings),
            parse_mode="HTML",
            disable_web_page_preview=True,
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
        link = _client_link(settings, bot_username, master)
        await message.answer(
            f"Ваша персональная ссылка для клиентов:\n<code>{link}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

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
