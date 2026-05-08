"""Helpers that wrap the aiogram bot to send user-facing messages.

The Notifier is intentionally tolerant: every send is wrapped in try/except
so that a failed send never breaks API or scheduler callers.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.models import (
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_CONFIRMED,
    Booking,
    Client,
    Master,
    Service,
)

log = logging.getLogger(__name__)


def _format_local(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def _client_label(client: Client) -> str:
    parts = [client.name or "Клиент"]
    if client.phone:
        parts.append(client.phone)
    if client.tg_username:
        parts.append(f"@{client.tg_username.lstrip('@')}")
    return " · ".join(parts)


class Notifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, text, disable_web_page_preview=True)
        except TelegramAPIError as exc:
            log.warning("notifier_send_failed chat_id=%s error=%s", chat_id, exc)

    async def notify_master_new_booking(
        self,
        *,
        master: Master,
        booking: Booking,
        client: Client,
        service: Service,
    ) -> None:
        text = (
            "🆕 Новая запись\n"
            f"Клиент: {_client_label(client)}\n"
            f"Услуга: {service.name}\n"
            f"Когда: {_format_local(booking.starts_at)}\n"
            f"Стоимость: {service.price} ₽\n"
            f"Статус: {booking.status}"
        )
        await self._safe_send(master.tg_chat_id, text)

    async def notify_client_booking_confirmed(
        self,
        *,
        client: Client,
        booking: Booking,
        service: Service,
        master: Master,
    ) -> None:
        if not client.tg_user_id:
            return
        text = (
            f"✅ Ваша запись к {master.display_name} подтверждена\n"
            f"Услуга: {service.name}\n"
            f"Когда: {_format_local(booking.starts_at)}"
        )
        await self._safe_send(client.tg_user_id, text)

    async def notify_client_booking_cancelled(
        self,
        *,
        client: Client,
        booking: Booking,
        service: Service,
        master: Master,
    ) -> None:
        if not client.tg_user_id:
            return
        text = (
            f"❌ Запись к {master.display_name} отменена\n"
            f"Услуга: {service.name}\n"
            f"Когда было: {_format_local(booking.starts_at)}"
        )
        await self._safe_send(client.tg_user_id, text)

    async def notify_status_change(
        self,
        *,
        client: Client,
        booking: Booking,
        service: Service,
        master: Master,
        old_status: str,
        new_status: str,
    ) -> None:
        if old_status == new_status:
            return
        if new_status == BOOKING_STATUS_CONFIRMED:
            await self.notify_client_booking_confirmed(
                client=client, booking=booking, service=service, master=master
            )
        elif new_status == BOOKING_STATUS_CANCELLED:
            await self.notify_client_booking_cancelled(
                client=client, booking=booking, service=service, master=master
            )

    async def notify_client_reminder(
        self,
        *,
        client: Client,
        booking: Booking,
        service: Service,
        master: Master,
        hours_until: int,
    ) -> None:
        if not client.tg_user_id:
            return
        when = _format_local(booking.starts_at)
        if hours_until >= 24:
            head = f"⏰ Напоминание: завтра в {booking.starts_at.strftime('%H:%M')} запись"
        else:
            head = f"⏰ Напоминание: через {hours_until} ч запись"
        text = f"{head} к {master.display_name}\nУслуга: {service.name}\nКогда: {when}"
        await self._safe_send(client.tg_user_id, text)

    async def notify_master_morning_summary(
        self,
        *,
        master: Master,
        bookings: list[tuple[Booking, Client, Service]],
    ) -> None:
        if not bookings:
            text = "☕️ Доброе утро! На сегодня записей нет."
        else:
            lines = [f"☕️ Доброе утро! Сегодня {len(bookings)} запис(ей):"]
            for b, c, s in bookings:
                lines.append(
                    f"• {b.starts_at.strftime('%H:%M')} — {s.name} — {_client_label(c)}"
                )
            text = "\n".join(lines)
        await self._safe_send(master.tg_chat_id, text)
