"""Handlers for per-master client bots.

Each master can connect their own Telegram bot (via /addbot in the main bot).
Clients interact with these bots to book appointments — everything is done
via inline-keyboard buttons, no Mini App.

The handlers are nearly identical to the referral booking flow in the main bot
but scoped to a single master (the bot owner). Notifications about new bookings
go to the master through the main Clientika bot.
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
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.booking import (
    PastBookingError,
    SlotUnavailableError,
    available_day_slots,
    create_client_booking,
)
from app.db import session_scope
from app.models import (
    Booking,
    Client,
    Master,
    Service,
)
from app.notifications import Notifier
from app.repos import list_active_services

log = logging.getLogger(__name__)

_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_BOOK_DAYS_AHEAD = 14


class ClientBookingFlow(StatesGroup):
    service = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


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


def _days_keyboard(today: datetime) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for offset in range(_BOOK_DAYS_AHEAD):
        d = (today + timedelta(days=offset)).date()
        label = f"{_WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"cbday:{d.isoformat()}"))
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
    """Build a Dispatcher for a per-master client bot.

    ``master_id`` is the DB id of the master who owns this bot.
    ``main_notifier`` sends booking notifications through the main Clientika bot.
    """
    dp = Dispatcher(storage=MemoryStorage())
    router = Router(name=f"client_bot_{master_id}")

    async def _get_master(session: AsyncSession) -> Master | None:
        res = await session.execute(select(Master).where(Master.id == master_id))
        return res.scalar_one_or_none()

    # ---- /start — greeting with service list ----

    @router.message(CommandStart())
    async def on_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        async with session_scope(session_factory) as session:
            master = await _get_master(session)
            if master is None:
                await message.answer("Бот временно недоступен.")
                return
            display_name = master.display_name
            services = await list_active_services(session, master.id)

        if not services:
            await message.answer(
                f"Мастер {display_name} пока не добавил(а) услуги. Попробуйте позже."
            )
            return

        await state.set_state(ClientBookingFlow.service)
        await state.update_data(master_id=master_id)
        await message.answer(
            f"Добро пожаловать! Запись к мастеру: <b>{display_name}</b>\n\n"
            "Выберите услугу:",
            reply_markup=_services_keyboard(services),
            parse_mode="HTML",
        )

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
        await callback.message.edit_text(
            f"Услуга: <b>{service_name}</b>\nВыберите день:",
            reply_markup=_days_keyboard(datetime.utcnow()),
            parse_mode="HTML",
        )
        await callback.answer()

    # ---- Day selection ----

    @router.callback_query(ClientBookingFlow.slot, F.data == "cbdays")
    @router.callback_query(ClientBookingFlow.day, F.data == "cbdays")
    async def on_back_to_days(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ClientBookingFlow.day)
        assert isinstance(callback.message, Message)
        await callback.message.edit_text(
            "Выберите день:",
            reply_markup=_days_keyboard(datetime.utcnow()),
        )
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
                await callback.answer("Сессия устарела, нажмите /start", show_alert=True)
                await state.clear()
                return
            slots = await available_day_slots(session, master, service, day)

        assert isinstance(callback.message, Message)
        if not slots:
            await callback.answer("На этот день нет свободного времени", show_alert=True)
            return

        await state.update_data(day=day_iso)
        await state.set_state(ClientBookingFlow.slot)
        label = f"{_WEEKDAYS_RU[day.weekday()]} {day.strftime('%d.%m')}"
        await callback.message.edit_text(
            f"Свободное время на {label}:",
            reply_markup=_slots_keyboard(slots),
        )
        await callback.answer()

    # ---- Slot selection ----

    @router.callback_query(ClientBookingFlow.slot, F.data.startswith("cbslot:"))
    async def on_pick_slot(callback: CallbackQuery, state: FSMContext) -> None:
        hhmm = (callback.data or "cbslot:").split(":", 1)[1]
        data = await state.get_data()
        day_iso = data.get("day")
        if not day_iso:
            await callback.answer("Сессия устарела, нажмите /start", show_alert=True)
            await state.clear()
            return
        starts_at = datetime.fromisoformat(f"{day_iso}T{hhmm}:00")
        await state.update_data(starts_at=starts_at.isoformat())
        await state.set_state(ClientBookingFlow.name)

        from_user = callback.from_user
        tg_name = (
            f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()
            if from_user
            else ""
        )
        rows: list[list[InlineKeyboardButton]] = []
        if tg_name:
            rows.append(
                [InlineKeyboardButton(text=f"Использовать: {tg_name}", callback_data="cbname")]
            )
        rows.append(_cancel_row())
        assert isinstance(callback.message, Message)
        await callback.message.edit_text(
            "Как вас записать? Напишите имя сообщением"
            + (" или нажмите кнопку ниже." if tg_name else "."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    # ---- Name ----

    @router.callback_query(ClientBookingFlow.name, F.data == "cbname")
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

    @router.message(ClientBookingFlow.name, F.text)
    async def on_name_text(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()[:120] or "Клиент"
        await state.update_data(name=name)
        await _ask_phone(message, state)

    # ---- Phone (required) ----

    async def _ask_phone(message: Message, state: FSMContext) -> None:
        await state.set_state(ClientBookingFlow.phone)
        kb = InlineKeyboardMarkup(inline_keyboard=[_cancel_row()])
        await message.answer(
            "Напишите ваш номер телефона для связи.",
            reply_markup=kb,
        )

    @router.message(ClientBookingFlow.phone, F.text)
    async def on_phone_text(message: Message, state: FSMContext) -> None:
        phone = (message.text or "").strip()[:40] or None
        await state.update_data(phone=phone)
        await _show_confirm(message, state)

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
        from_user = callback.from_user
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
                error = "Запись недоступна. Нажмите /start чтобы начать заново."
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
                await main_notifier.notify_master_new_booking(
                    master=master_obj,
                    booking=booking_obj,
                    client=client_obj,
                    service=service_obj,
                )
            except Exception:
                log.exception("master_bot_notify_failed master_id=%s", master_id)

        await state.clear()
        await callback.message.edit_text(
            "✅ Готово! Вы записаны:\n"
            f"  {service_obj.name}\n"
            f"  {booking_obj.starts_at.strftime('%d.%m.%Y %H:%M')}\n"
            f"  Мастер: {master_obj.display_name}\n\n"
            "Мастер получит уведомление и свяжется при необходимости.\n"
            "Нажмите /start чтобы записаться снова."
        )
        await callback.answer()

    # ---- Cancel ----

    @router.callback_query(F.data == "cbcancel")
    async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        assert isinstance(callback.message, Message)
        await callback.message.edit_text(
            "Запись отменена. Нажмите /start чтобы начать заново."
        )
        await callback.answer()

    # ---- Today command for info ----

    @router.message(F.text)
    async def on_fallback(message: Message, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state is not None:
            return
        await message.answer(
            "Нажмите /start чтобы записаться к мастеру."
        )

    dp.include_router(router)
    return dp
