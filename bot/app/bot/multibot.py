"""Multi-bot manager: starts/stops per-master client bots dynamically."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.client_bot import build_client_dispatcher
from app.models import MasterBot
from app.notifications import Notifier

log = logging.getLogger(__name__)


class _RunningBot:
    __slots__ = ("bot", "dispatcher", "task")

    def __init__(self, bot: Bot, dispatcher: object, task: asyncio.Task[None]) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.task = task


class MultiBotManager:
    """Manages per-master client bots.

    Call ``start_all()`` once at app startup to launch all active bots,
    then ``add_bot`` / ``remove_bot`` for dynamic changes.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        main_notifier: Notifier,
        proxy_url: str = "",
    ) -> None:
        self._session_factory = session_factory
        self._main_notifier = main_notifier
        self._proxy_url = proxy_url
        self._running: dict[int, _RunningBot] = {}  # master_id -> _RunningBot

    async def start_all(self) -> None:
        """Load all active MasterBot rows and start polling for each."""
        async with self._session_factory() as session:
            rows = list(
                (await session.execute(
                    select(MasterBot).where(MasterBot.is_active.is_(True))
                )).scalars()
            )
            for mb in rows:
                session.expunge(mb)

        for mb in rows:
            await self._launch(mb.master_id, mb.bot_token)

    async def add_bot(self, master_id: int, bot_token: str) -> None:
        """Start polling for a newly added master bot."""
        if master_id in self._running:
            await self.remove_bot(master_id)
        await self._launch(master_id, bot_token)

    async def remove_bot(self, master_id: int) -> None:
        """Stop polling for a master bot."""
        entry = self._running.pop(master_id, None)
        if entry is None:
            return
        try:
            await entry.dispatcher.stop_polling()  # type: ignore[attr-defined]
        except Exception:
            log.exception("stop_polling_error master_id=%s", master_id)
        entry.task.cancel()
        try:
            await entry.task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await entry.bot.session.close()
        except Exception:
            pass
        log.info("master_bot_stopped master_id=%s", master_id)

    async def shutdown(self) -> None:
        """Stop all running master bots."""
        master_ids = list(self._running.keys())
        for mid in master_ids:
            await self.remove_bot(mid)

    async def _launch(self, master_id: int, bot_token: str) -> None:
        try:
            aio_session = (
                AiohttpSession(proxy=self._proxy_url)
                if self._proxy_url
                else AiohttpSession()
            )
            bot = Bot(
                token=bot_token,
                default=DefaultBotProperties(parse_mode=None),
                session=aio_session,
            )
            dp = build_client_dispatcher(
                master_id=master_id,
                session_factory=self._session_factory,
                main_notifier=self._main_notifier,
            )
            task = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
                name=f"master_bot_{master_id}",
            )
            self._running[master_id] = _RunningBot(bot=bot, dispatcher=dp, task=task)
            log.info("master_bot_started master_id=%s", master_id)
        except Exception:
            log.exception("master_bot_launch_failed master_id=%s", master_id)

    def is_running(self, master_id: int) -> bool:
        return master_id in self._running

    def get_bot(self, master_id: int) -> Bot | None:
        """Return the running Bot instance for a master, or None."""
        entry = self._running.get(master_id)
        return entry.bot if entry is not None else None
