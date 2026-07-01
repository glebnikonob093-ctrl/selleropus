from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import structlog
import uvicorn
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv

from app.api import create_api_app
from app.bot import MultiBotManager, build_dispatcher
from app.config import Settings, load_settings
from app.db import create_engine, create_session_factory, ensure_sqlite_dir, ping_db
from app.migrations import create_all
from app.notifications import Notifier
from app.scheduler import start_reminder_scheduler


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )


async def _run(settings: Settings) -> None:
    log = structlog.get_logger()

    ensure_sqlite_dir(settings.database_url)
    engine = create_engine(settings.database_url)
    await ping_db(engine)
    await create_all(engine)
    session_factory = create_session_factory(engine)

    aio_session = (
        AiohttpSession(proxy=settings.telegram_proxy_url)
        if settings.telegram_proxy_url
        else AiohttpSession()
    )
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
        session=aio_session,
    )
    notifier = Notifier(bot)

    bot_username = settings.bot_username
    if not bot_username:
        try:
            me = await bot.get_me()
            bot_username = me.username or ""
        except Exception:  # pragma: no cover - network/startup hiccup
            log.warning("get_me_failed; deep links will be unavailable")

    multibot_mgr = MultiBotManager(
        session_factory=session_factory,
        main_notifier=notifier,
        proxy_url=settings.telegram_proxy_url,
    )

    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=session_factory,
        notifier=notifier,
        bot_username=bot_username,
        multibot_manager=multibot_mgr,
    )

    api_app = create_api_app(
        settings=settings,
        session_factory=session_factory,
        notifier=notifier,
    )
    api_config = uvicorn.Config(
        api_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        access_log=False,
    )
    api_server = uvicorn.Server(api_config)

    scheduler = start_reminder_scheduler(
        session_factory=session_factory,
        notifier=notifier,
        interval_seconds=settings.scheduler_interval_seconds,
    )

    log.info("clientika_start", api=f"{settings.api_host}:{settings.api_port}")

    await multibot_mgr.start_all()

    api_task = asyncio.create_task(api_server.serve(), name="uvicorn")
    polling_task = asyncio.create_task(
        dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types()),
        name="aiogram",
    )

    try:
        done, pending = await asyncio.wait(
            {api_task, polling_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in done:
            if t.exception() is not None:
                log.error("task_crashed", task=t.get_name(), error=str(t.exception()))
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("task_shutdown_error")
    finally:
        try:
            await multibot_mgr.shutdown()
        except Exception:
            pass
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            await dispatcher.stop_polling()
        except Exception:
            pass
        try:
            await bot.session.close()
        except Exception:
            pass
        await engine.dispose()


def main() -> None:
    bot_dir = Path(__file__).resolve().parents[1]
    load_dotenv(dotenv_path=bot_dir / ".env", encoding="utf-8-sig")
    _configure_logging()
    settings = load_settings()
    asyncio.run(_run(settings))
