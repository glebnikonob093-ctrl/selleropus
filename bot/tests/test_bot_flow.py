from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import (
    _client_choice_keyboard,
    _client_link,
    build_dispatcher,
)
from app.config import Settings
from app.models import Booking, Master, Service

BOT_USERNAME = "clientika_bot"


def _settings(webapp_url: str = "https://app.example.com") -> Settings:
    return Settings(
        bot_token="123456:TEST",
        bot_username=BOT_USERNAME,
        database_url="sqlite+aiosqlite:///:memory:",
        api_host="127.0.0.1",
        api_port=8000,
        webapp_url=webapp_url,
        webapp_dist_dir="",
        telegram_proxy_url="",
        scheduler_interval_seconds=60,
        default_work_start=(10, 0),
        default_work_end=(20, 0),
        default_slot_step_minutes=60,
        default_timezone="UTC",
    )


CHAT = Chat(id=555, type="private")
USER = User(id=555, is_bot=False, first_name="Cli", last_name="Ent", username="cli")


class RecordingBot(Bot):
    """Intercepts every Telegram API call at the call boundary (no network)."""

    def __init__(self) -> None:
        super().__init__("123456:TEST")
        self.calls: list = []

    async def __call__(self, method, request_timeout=None):  # type: ignore[override]
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return SimpleNamespace(message_id=len(self.calls))
        return True

    @property
    def sent(self) -> list[SendMessage]:
        return [m for m in self.calls if isinstance(m, SendMessage)]

    @property
    def edited(self) -> list[EditMessageText]:
        return [m for m in self.calls if isinstance(m, EditMessageText)]


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify_master_new_booking(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _message(text: str, mid: int = 1) -> Message:
    return Message(message_id=mid, date=datetime.utcnow(), chat=CHAT, from_user=USER, text=text)


def _callback(data: str) -> CallbackQuery:
    return CallbackQuery(
        id="cb",
        from_user=USER,
        chat_instance="ci",
        message=Message(
            message_id=2, date=datetime.utcnow(), chat=CHAT, from_user=USER, text="x"
        ),
        data=data,
    )


async def _seed(session: AsyncSession) -> tuple[Master, Service]:
    master = Master(
        tg_user_id=1,
        tg_chat_id=1,
        slug="anna",
        display_name="Anna",
        is_master=True,
        work_start_minutes=10 * 60,
        work_end_minutes=20 * 60,
        slot_step_minutes=60,
    )
    session.add(master)
    await session.flush()
    service = Service(master_id=master.id, name="Маникюр", price=1000, duration_minutes=60)
    session.add(service)
    await session.flush()
    return master, service


def test_client_link_prefers_bot_deeplink() -> None:
    master = Master(tg_user_id=1, tg_chat_id=1, slug="anna", display_name="Anna")
    assert _client_link(_settings(), BOT_USERNAME, master) == "https://t.me/clientika_bot?start=anna"


def test_client_link_falls_back_to_webapp_without_username() -> None:
    master = Master(tg_user_id=1, tg_chat_id=1, slug="anna", display_name="Anna")
    link = _client_link(_settings(), "", master)
    assert link == "https://app.example.com?master=anna"


def test_client_choice_keyboard_has_booking_button() -> None:
    kb = _client_choice_keyboard("anna")
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert all(b.web_app is None for b in flat)  # no mini app option
    assert any(b.callback_data == "bkgo:anna" for b in flat)


@pytest.fixture()
def _build(session_factory: async_sessionmaker[AsyncSession]):
    bot = RecordingBot()
    notifier = FakeNotifier()
    dp = build_dispatcher(
        settings=_settings(),
        session_factory=session_factory,
        notifier=notifier,
        bot_username=BOT_USERNAME,
    )
    return dp, bot, notifier


async def _feed(dp, bot, update_obj) -> None:
    await dp.feed_update(bot, update_obj)


async def test_full_in_bot_booking_flow(
    session_factory: async_sessionmaker[AsyncSession], _build
) -> None:
    dp, bot, notifier = _build
    async with session_factory() as s:
        _, service = await _seed(s)
        await s.commit()
        service_id = service.id

    await _feed(dp, bot, Update(update_id=1, message=_message("/start anna")))
    # The greeting should present the choice, not register a master.
    assert bot.sent, "client should get a choice message"

    await _feed(dp, bot, Update(update_id=2, callback_query=_callback("bkgo:anna")))
    await _feed(dp, bot, Update(update_id=3, callback_query=_callback(f"bksvc:{service_id}")))

    day = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    await _feed(dp, bot, Update(update_id=4, callback_query=_callback(f"bkday:{day}")))
    await _feed(dp, bot, Update(update_id=5, callback_query=_callback("bkslot:11:00")))
    await _feed(dp, bot, Update(update_id=6, callback_query=_callback("bkname")))
    # Phone is now required — send text instead of skipping.
    await _feed(dp, bot, Update(update_id=7, message=_message("+79991234567", mid=3)))
    await _feed(dp, bot, Update(update_id=8, callback_query=_callback("bkok")))

    async with session_factory() as s:
        bookings = list((await s.execute(select(Booking))).scalars())
    assert len(bookings) == 1
    assert bookings[0].source == "bot"
    assert bookings[0].starts_at == datetime.fromisoformat(f"{day}T11:00:00")
    assert len(notifier.calls) == 1

    # The referred client must NOT have become a master.
    async with session_factory() as s:
        masters = list((await s.execute(select(Master))).scalars())
    assert {m.tg_user_id for m in masters} == {1}
    # Verify phone was recorded
    async with session_factory() as s:
        from app.models import Client
        clients = list((await s.execute(select(Client))).scalars())
    assert any(c.phone == "+79991234567" for c in clients)


async def test_unknown_slug_is_handled_gracefully(
    session_factory: async_sessionmaker[AsyncSession], _build
) -> None:
    dp, bot, _ = _build
    await _feed(dp, bot, Update(update_id=1, message=_message("/start ghost")))
    assert bot.sent
    text = bot.sent[-1].text or ""
    assert "найти" in text.lower() or "ссылк" in text.lower()
    async with session_factory() as s:
        masters = list((await s.execute(select(Master))).scalars())
    assert masters == []


async def test_link_command_does_not_register_client_as_master(
    session_factory: async_sessionmaker[AsyncSession], _build
) -> None:
    dp, bot, _ = _build
    await _feed(dp, bot, Update(update_id=1, message=_message("/link")))
    async with session_factory() as s:
        masters = list((await s.execute(select(Master))).scalars())
    assert masters == []
    assert bot.sent
    assert "мастер" in (bot.sent[-1].text or "").lower()


async def test_start_without_payload_registers_master(
    session_factory: async_sessionmaker[AsyncSession], _build
) -> None:
    dp, bot, _ = _build
    await _feed(dp, bot, Update(update_id=1, message=_message("/start")))
    async with session_factory() as s:
        masters = list((await s.execute(select(Master))).scalars())
    assert len(masters) == 1
    assert masters[0].tg_user_id == USER.id
