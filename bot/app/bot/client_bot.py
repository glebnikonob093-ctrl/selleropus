"""Handlers for per-master client bots.

Each master can connect their own Telegram bot (via /addbot in the main bot).
Clients interact with these bots to book appointments — everything is done
via inline-keyboard buttons + a persistent bottom menu, no Mini App.

Flow:
  /start → contact sharing (once) → persistent menu with three buttons:
    📝 Записаться  |  📋 Мои записи  |  👤 Мой профиль
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Dispatcher, F, Router
from aiogram.filters import CommandStart
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
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.booking import (
    ACTIVE_BOOKING_STATUSES,
    PastBookingError,
    SlotUnavailableError,
    available_day_slots,
    create_client_booking,
)
from app.db import session_scope
from app.models import (
    BOOKING_STATUS_CANCELLED,
    Booking,
    Client,
    Master,
    Service,
)
from app.notifications import Notifier
from app.repos import (
    find_or_create_client,
    get_master_schedule,
    is_client_blocked,
    list_active_services,
    list_day_offs,
    list_team_members,
)

log = logging.getLogger(__name__)

_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTH_NAMES_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Persistent bottom-menu button labels
_BTN_BOOK = "📝 Записаться"
_BTN_MY_BOOKINGS = "📋 Мои записи"
_BTN_MY_PROFILE = "👤 Мой профиль"


class ClientBookingFlow(StatesGroup):
    phone = State()
    service = State()
    day = State()
    slot = State()
    confirm = State()


def _main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=_BTN_BOOK), KeyboardButton(text=_BTN_MY_BOOKINGS)],
            [KeyboardButton(text=_BTN_MY_PROFILE)],
        ],
        resize_keyboard=True,
    )


def _phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="✖️ Отмена", callback_data="cbcancel")]


def _services_keyboard(services: list[Service]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{s.name} · {s.price}₽ · {s.duration_minutes}мин",
                callback_data=f"cbsvc:{s.id}",
            )
        ]
        for s in services
    ]
    rows.append(_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _calendar_keyboard(
    session: AsyncSession,
    master_id: int,
    month_offset: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a full month-view calendar. All days shown; bookable days clickable."""
    today = datetime.utcnow().date()
    first = (today.replace(day=1) + timedelta(days=32 * month_offset)).replace(day=1)

    schedule = await get_master_schedule(session, master_id)
    schedule_map = {s.weekday: s for s in schedule}

    offs = await list_day_offs(session, master_id)
    off_dates = {o.day for o in offs}

    # Get master's book_days_ahead setting
    res = await session.execute(
        select(Master.book_days_ahead).where(Master.id == master_id)
    )
    book_days = res.scalar_one_or_none() or 30
    max_date = today + timedelta(days=book_days)

    title = f"📅 {_MONTH_NAMES_RU[first.month - 1]} {first.year}"

    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=f"cbcal:{month_offset - 1}"),
        InlineKeyboardButton(text=title, callback_data="cbnoop"),
        InlineKeyboardButton(text="▶️", callback_data=f"cbcal:{month_offset + 1}"),
    ])
    rows.append([
        InlineKeyboardButton(text=wd, callback_data="cbnoop")
        for wd in _WEEKDAYS_RU
    ])

    d = first
    week: list[InlineKeyboardButton] = []
    for _ in range(first.weekday()):
        week.append(InlineKeyboardButton(text=" ", callback_data="cbnoop"))

    while d.month == first.month:
        sched = schedule_map.get(d.weekday())
        is_working = sched.is_working if sched else True
        is_off = d in off_dates
        is_past = d < today
        is_beyond = d >= max_date

        if is_past:
            week.append(InlineKeyboardButton(text="·", callback_data="cbnoop"))
        elif not is_working or is_off:
            week.append(InlineKeyboardButton(text="—", callback_data="cbnoop"))
        elif is_beyond:
            week.append(InlineKeyboardButton(text=str(d.day), callback_data="cbnoop"))
        else:
            label = f"{'✦' if d == today else ''}{d.day}"
            week.append(InlineKeyboardButton(
                text=label,
                callback_data=f"cbday:{d.isoformat()}",
            ))

        if len(week) == 7:
            rows.append(week)
            week = []
        d += timedelta(days=1)

    if week:
        while len(week) < 7:
            week.append(InlineKeyboardButton(text=" ", callback_data="cbnoop"))
        rows.append(week)

    rows.append(_cancel_row())
    return title, InlineKeyboardMarkup(inline_keyboard=rows)


def _slots_keyboard(slots: list[datetime]) -> InlineKeyboardMarkup:
    """Group time slots by period (morning/afternoon/evening)."""
    morning: list[datetime] = []
    afternoon: list[datetime] = []
    evening: list[datetime] = []

    for slot in slots:
        h = slot.hour
        if h < 12:
            morning.append(slot)
        elif h < 17:
            afternoon.append(slot)
        else:
            evening.append(slot)

    rows: list[list[InlineKeyboardButton]] = []
    for label, group in [
        ("🌅 Утро", morning),
        ("☀️ День", afternoon),
        ("🌙 Вечер", evening),
    ]:
        if not group:
            continue
        rows.append([InlineKeyboardButton(text=label, callback_data="cbnoop")])
        row: list[InlineKeyboardButton] = []
        for slot in group:
            hhmm = slot.strftime("%H:%M")
            row.append(InlineKeyboardButton(text=hhmm, callback_data=f"cbslot:{hhmm}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    rows.append(
        [InlineKeyboardButton(text="◀️ Другой день", callback_data="cbdays")]
    )
    rows.append(_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_client_dispatcher(
    *,
    master_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    main_notifier: Notifier | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    router = Router(name=f"client_bot_{master_id}")

    async def _get_master(session: AsyncSession) -> Master | None:
        res = await session.execute(select(Master).where(Master.id == master_id))
        return res.scalar_one_or_none()

    async def _get_client(session: AsyncSession, tg_user_id: int) -> Client | None:
        res = await session.execute(
            select(Client).where(
                Client.master_id == master_id,
                Client.tg_user_id == tg_user_id,
            )
        )
        return res.scalar_one_or_none()

    async def _start_booking(message: Message, state: FSMContext) -> None:
        async with session_scope(session_factory) as session:
            services = await list_active_services(session, master_id)
        if not services:
            await message.answer(
                "Мастер пока не добавил(а) услуги. Попробуйте позже.",
                reply_markup=_main_menu_kb(),
            )
            return
        await state.set_state(ClientBookingFlow.service)
        await message.answer(
            "Выберите услугу:",
            reply_markup=_services_keyboard(services),
        )

    # ---- /start ----

    @router.message(CommandStart())
    async def on_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        from_user = message.from_user
        assert from_user is not None

        async with session_scope(session_factory) as session:
            master = await _get_master(session)
            if master is None:
                await message.answer("Бот временно недоступен.")
                return
            display_name = master.display_name
            blocked = await is_client_blocked(session, master_id, from_user.id)
            client = await _get_client(session, from_user.id)

        if blocked:
            await message.answer(
                "К сожалению, вы заблокированы этим мастером и не можете записаться."
            )
            return

        if client is not None and client.phone:
            await state.update_data(
                master_id=master_id,
                phone=client.phone,
                name=client.name,
                tg_user_id=from_user.id,
                tg_username=from_user.username or None,
            )
            await message.answer(
                f"Привет, {client.name}! 👋\n"
                f"Запись к мастеру: <b>{display_name}</b>\n\n"
                "Выберите действие в меню ниже.",
                reply_markup=_main_menu_kb(),
                parse_mode="HTML",
            )
            return

        await state.set_state(ClientBookingFlow.phone)
        await state.update_data(master_id=master_id, master_name=display_name)
        await message.answer(
            f"Добро пожаловать! Запись к мастеру: <b>{display_name}</b>\n\n"
            "Для продолжения, пожалуйста, подтвердите ваш номер телефона, "
            "нажав кнопку ниже.",
            reply_markup=_phone_kb(),
            parse_mode="HTML",
        )

    # ---- Phone via Telegram contact ----

    @router.message(ClientBookingFlow.phone, F.contact)
    async def on_contact_shared(message: Message, state: FSMContext) -> None:
        contact = message.contact
        assert contact is not None
        from_user = message.from_user

        if from_user and contact.user_id != from_user.id:
            await message.answer(
                "Пожалуйста, отправьте свой номер телефона, "
                "а не чужой контакт.",
            )
            return

        phone = contact.phone_number
        if not phone.startswith("+"):
            phone = f"+{phone}"

        tg_name = (
            f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()
            if from_user
            else ""
        )
        name = tg_name or "Клиент"

        async with session_scope(session_factory) as session:
            await find_or_create_client(
                session,
                master_id,
                name=name,
                phone=phone,
                tg_user_id=from_user.id if from_user else None,
                tg_username=(from_user.username if from_user else None) or None,
            )

        await state.clear()
        await state.update_data(
            phone=phone,
            name=name,
            tg_user_id=from_user.id if from_user else None,
            tg_username=(from_user.username if from_user else None) or None,
        )
        await message.answer(
            f"Спасибо, {name}! Номер {phone} сохранён.\n\n"
            "Выберите действие в меню ниже.",
            reply_markup=_main_menu_kb(),
        )

    @router.message(ClientBookingFlow.phone)
    async def on_phone_not_contact(message: Message, state: FSMContext) -> None:
        await message.answer(
            "Пожалуйста, нажмите кнопку ниже, чтобы поделиться номером телефона.",
            reply_markup=_phone_kb(),
        )

    # ---- Persistent menu buttons ----

    @router.message(F.text == _BTN_BOOK)
    async def on_menu_book(message: Message, state: FSMContext) -> None:
        await state.clear()
        from_user = message.from_user
        assert from_user is not None
        data = await _ensure_client_data(message, state, from_user.id)
        if data is None:
            return
        await _start_booking(message, state)

    @router.message(F.text == _BTN_MY_BOOKINGS)
    async def on_menu_my_bookings(message: Message, state: FSMContext) -> None:
        await state.clear()
        from_user = message.from_user
        assert from_user is not None
        data = await _ensure_client_data(message, state, from_user.id)
        if data is None:
            return

        now = datetime.utcnow()
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Booking, Service)
                .join(Service, Booking.service_id == Service.id)
                .where(
                    Booking.master_id == master_id,
                    Booking.starts_at >= now,
                    Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                )
                .order_by(Booking.starts_at)
            )
            rows = res.all()
            client = await _get_client(session, from_user.id)

        if client is None:
            await message.answer("Записей не найдено.", reply_markup=_main_menu_kb())
            return

        bookings_for_client = [
            (b, s) for b, s in rows if b.client_id == client.id
        ]

        if not bookings_for_client:
            await message.answer(
                "У вас пока нет предстоящих записей.\n"
                f"Нажмите «{_BTN_BOOK}» чтобы записаться!",
                reply_markup=_main_menu_kb(),
            )
            return

        for booking, service in bookings_for_client:
            dt = booking.starts_at
            day_label = f"{_WEEKDAYS_RU[dt.weekday()]} {dt.strftime('%d.%m.%Y')}"
            text = (
                f"<b>{service.name}</b>\n"
                f"{day_label} в {dt.strftime('%H:%M')}\n"
                f"{service.price}₽ · {service.duration_minutes} мин"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отменить запись",
                            callback_data=f"mycxl:{booking.id}",
                        )
                    ]
                ]
            )
            await message.answer(text, reply_markup=kb, parse_mode="HTML")

    @router.callback_query(F.data.startswith("mycxl:"))
    async def on_client_cancel_booking(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        booking_id = int((callback.data or "mycxl:0").split(":", 1)[1])
        from_user = callback.from_user
        assert isinstance(callback.message, Message)

        async with session_scope(session_factory) as session:
            client = await _get_client(session, from_user.id) if from_user else None
            if client is None:
                await callback.answer("Клиент не найден", show_alert=True)
                return
            res = await session.execute(
                select(Booking).where(
                    Booking.id == booking_id,
                    Booking.client_id == client.id,
                    Booking.master_id == master_id,
                )
            )
            booking = res.scalar_one_or_none()
            if booking is None:
                await callback.answer("Запись не найдена", show_alert=True)
                return
            if booking.status == BOOKING_STATUS_CANCELLED:
                await callback.answer("Уже отменена", show_alert=True)
                return
            booking.status = BOOKING_STATUS_CANCELLED
            master = await _get_master(session)

        await callback.message.edit_text(
            callback.message.text + "\n\n❌ <b>Отменена вами</b>",
            parse_mode="HTML",
        )
        await callback.answer("Запись отменена")

        if main_notifier is not None and master is not None:
            try:
                await main_notifier._safe_send(
                    master.tg_chat_id,
                    f"❌ Клиент отменил запись\n"
                    f"Клиент: {client.name}\n"
                    f"Когда: {booking.starts_at.strftime('%d.%m.%Y %H:%M')}",
                )
            except Exception:
                log.exception("cancel_notify_failed master_id=%s", master_id)

    @router.message(F.text == _BTN_MY_PROFILE)
    async def on_menu_my_profile(message: Message, state: FSMContext) -> None:
        await state.clear()
        from_user = message.from_user
        assert from_user is not None

        async with session_scope(session_factory) as session:
            client = await _get_client(session, from_user.id)
            master = await _get_master(session)

        if client is None or not client.phone:
            await state.set_state(ClientBookingFlow.phone)
            await message.answer(
                "Вы ещё не зарегистрированы. Поделитесь номером телефона:",
                reply_markup=_phone_kb(),
            )
            return

        master_name = master.display_name if master else "—"

        # Booking history
        async with session_scope(session_factory) as session:
            past_res = await session.execute(
                select(Booking, Service)
                .join(Service, Booking.service_id == Service.id)
                .where(
                    Booking.client_id == client.id,
                    Booking.master_id == master_id,
                    Booking.starts_at < datetime.utcnow(),
                )
                .order_by(Booking.starts_at.desc())
                .limit(5)
            )
            past_rows = past_res.all()

        history = ""
        if past_rows:
            history = "\n\n<b>Последние записи:</b>"
            for b, s in past_rows:
                dt = b.starts_at
                status_icon = {"came": "✅", "cancelled": "❌", "no_show": "⚠️"}.get(
                    b.status, "📋"
                )
                history += (
                    f"\n{status_icon} {dt.strftime('%d.%m.%Y %H:%M')} — {s.name}"
                )

        await message.answer(
            "<b>Ваш профиль:</b>\n\n"
            f"Имя: {client.name}\n"
            f"Телефон: {client.phone}\n"
            f"Мастер: {master_name}"
            + history,
            reply_markup=_main_menu_kb(),
            parse_mode="HTML",
        )

    async def _ensure_client_data(
        message: Message, state: FSMContext, tg_user_id: int
    ) -> dict[str, object] | None:
        async with session_scope(session_factory) as session:
            blocked = await is_client_blocked(session, master_id, tg_user_id)
        if blocked:
            await message.answer(
                "К сожалению, вы заблокированы этим мастером.",
                reply_markup=_main_menu_kb(),
            )
            return None
        data = await state.get_data()
        if data.get("phone"):
            return data
        async with session_scope(session_factory) as session:
            client = await _get_client(session, tg_user_id)
        if client is not None and client.phone:
            from_user = message.from_user
            await state.update_data(
                phone=client.phone,
                name=client.name,
                tg_user_id=tg_user_id,
                tg_username=(from_user.username if from_user else None) or None,
            )
            return await state.get_data()
        await state.set_state(ClientBookingFlow.phone)
        await message.answer(
            "Сначала поделитесь номером телефона:",
            reply_markup=_phone_kb(),
        )
        return None

    # ---- Service selection ----

    @router.callback_query(ClientBookingFlow.service, F.data.startswith("cbsvc:"))
    async def on_pick_service(callback: CallbackQuery, state: FSMContext) -> None:
        service_id = int((callback.data or "cbsvc:0").split(":", 1)[1])
        async with session_scope(session_factory) as session:
            res = await session.execute(
                select(Service).where(
                    Service.id == service_id,
                    Service.master_id == master_id,
                )
            )
            service = res.scalar_one_or_none()
            if service is None or not service.is_active:
                await callback.answer("Услуга недоступна", show_alert=True)
                return
            service_name = service.name

        await state.update_data(service_id=service_id, service_name=service_name)
        await state.set_state(ClientBookingFlow.day)
        assert isinstance(callback.message, Message)
        async with session_scope(session_factory) as session:
            title, kb = await _calendar_keyboard(session, master_id)
        await callback.message.edit_text(
            f"Услуга: <b>{service_name}</b>\n{title}\nВыберите день:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()

    # ---- Day selection ----

    @router.callback_query(ClientBookingFlow.slot, F.data == "cbdays")
    @router.callback_query(ClientBookingFlow.day, F.data == "cbdays")
    async def on_back_to_days(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ClientBookingFlow.day)
        assert isinstance(callback.message, Message)
        data = await state.get_data()
        svc_name = data.get("service_name", "")
        async with session_scope(session_factory) as session:
            title, kb = await _calendar_keyboard(session, master_id)
        header = f"Услуга: <b>{svc_name}</b>\n" if svc_name else ""
        await callback.message.edit_text(
            f"{header}{title}\nВыберите день:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(ClientBookingFlow.day, F.data.startswith("cbcal:"))
    async def on_calendar_nav(callback: CallbackQuery, state: FSMContext) -> None:
        month_offset = int((callback.data or "cbcal:0").split(":", 1)[1])
        assert isinstance(callback.message, Message)
        data = await state.get_data()
        svc_name = data.get("service_name", "")
        async with session_scope(session_factory) as session:
            title, kb = await _calendar_keyboard(session, master_id, month_offset)
        header = f"Услуга: <b>{svc_name}</b>\n" if svc_name else ""
        await callback.message.edit_text(
            f"{header}{title}\nВыберите день:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data == "cbnoop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(ClientBookingFlow.day, F.data.startswith("cbday:"))
    async def on_pick_day(callback: CallbackQuery, state: FSMContext) -> None:
        day_iso = (callback.data or "cbday:").split(":", 1)[1]
        day = datetime.fromisoformat(day_iso).date()
        data = await state.get_data()
        async with session_scope(session_factory) as session:
            master = await _get_master(session)
            res = await session.execute(
                select(Service).where(Service.id == data.get("service_id", 0))
            )
            service = res.scalar_one_or_none()
            if master is None or service is None:
                await callback.answer("Сессия устарела", show_alert=True)
                await state.clear()
                return
            slots = await available_day_slots(session, master, service, day)

        assert isinstance(callback.message, Message)
        if not slots:
            await callback.answer("На этот день нет свободного времени", show_alert=True)
            return

        await state.update_data(day=day_iso)
        await state.set_state(ClientBookingFlow.slot)
        data = await state.get_data()
        svc_name = data.get("service_name", "")
        label = f"{_WEEKDAYS_RU[day.weekday()]} {day.strftime('%d.%m')}"
        header = f"Услуга: <b>{svc_name}</b>\n" if svc_name else ""
        await callback.message.edit_text(
            f"{header}🕐 Свободное время — {label}:",
            reply_markup=_slots_keyboard(slots),
            parse_mode="HTML",
        )
        await callback.answer()

    # ---- Slot selection → confirm ----

    @router.callback_query(ClientBookingFlow.slot, F.data.startswith("cbslot:"))
    async def on_pick_slot(callback: CallbackQuery, state: FSMContext) -> None:
        hhmm = (callback.data or "cbslot:").split(":", 1)[1]
        data = await state.get_data()
        day_iso = data.get("day")
        if not day_iso:
            await callback.answer("Сессия устарела", show_alert=True)
            await state.clear()
            return
        starts_at = datetime.fromisoformat(f"{day_iso}T{hhmm}:00")
        await state.update_data(starts_at=starts_at.isoformat())
        assert isinstance(callback.message, Message)
        await _show_confirm(callback.message, state)
        await callback.answer()

    # ---- Confirmation ----

    async def _show_confirm(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        starts_at = datetime.fromisoformat(data["starts_at"])
        phone = data.get("phone")
        lines = [
            "Проверьте запись:",
            f"  Услуга: {data.get('service_name', '')}",
            f"  Когда: {starts_at.strftime('%d.%m.%Y %H:%M')}",
            f"  Имя: {data.get('name', '')}",
        ]
        if phone:
            lines.append(f"  Телефон: {phone}")
        await state.set_state(ClientBookingFlow.confirm)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data="cbok")],
                _cancel_row(),
            ]
        )
        await message.answer("\n".join(lines), reply_markup=kb)

    @router.callback_query(ClientBookingFlow.confirm, F.data == "cbok")
    async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        starts_at = datetime.fromisoformat(data["starts_at"])
        assert isinstance(callback.message, Message)

        booking_obj: Booking | None = None
        master_obj: Master | None = None
        service_obj: Service | None = None
        client_obj: Client | None = None
        error: str | None = None

        async with session_scope(session_factory) as session:
            master = await _get_master(session)
            res = await session.execute(
                select(Service).where(
                    Service.id == data.get("service_id", 0),
                    Service.master_id == master_id,
                )
            )
            service = res.scalar_one_or_none()
            if master is None or service is None or not service.is_active:
                error = "Запись недоступна."
            else:
                try:
                    booking, client = await create_client_booking(
                        session,
                        master=master,
                        service=service,
                        starts_at=starts_at,
                        name=data.get("name", "Клиент"),
                        phone=data.get("phone"),
                        tg_user_id=data.get("tg_user_id"),
                        tg_username=data.get("tg_username"),
                        source="master_bot",
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
        if main_notifier is not None:
            try:
                async with session_scope(session_factory) as sess_tm:
                    team = await list_team_members(sess_tm, master_id)
                    for t in team:
                        sess_tm.expunge(t)
                await main_notifier.notify_master_new_booking(
                    master=master_obj,
                    booking=booking_obj,
                    client=client_obj,
                    service=service_obj,
                    team_members=team,
                )
            except Exception:
                log.exception("master_bot_notify_failed master_id=%s", master_id)

        await state.clear()
        await callback.message.edit_text(
            "✅ Готово! Вы записаны:\n"
            f"  {service_obj.name}\n"
            f"  {booking_obj.starts_at.strftime('%d.%m.%Y %H:%M')}\n"
            f"  Мастер: {master_obj.display_name}\n\n"
            "Мастер получит уведомление и свяжется при необходимости."
        )
        await callback.answer()

    # ---- Cancel ----

    @router.callback_query(F.data == "cbcancel")
    async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        assert isinstance(callback.message, Message)
        await callback.message.edit_text("Запись отменена.")
        await callback.answer()

    # ---- Fallback ----

    @router.message(F.text)
    async def on_fallback(message: Message, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state is not None:
            return
        await message.answer(
            "Выберите действие в меню ниже.",
            reply_markup=_main_menu_kb(),
        )

    dp.include_router(router)
    return dp
