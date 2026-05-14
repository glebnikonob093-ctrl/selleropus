from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_hhmm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = (raw or "").strip()
    if not raw or ":" not in raw:
        return default
    try:
        h, m = raw.split(":", 1)
        return int(h), int(m)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    api_host: str
    api_port: int
    webapp_url: str
    webapp_dist_dir: str
    telegram_proxy_url: str
    scheduler_interval_seconds: int
    default_work_start: tuple[int, int]
    default_work_end: tuple[int, int]
    default_slot_step_minutes: int
    default_timezone: str


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required (set it in bot/.env)")

    # Cloud providers (Railway/Render/Fly.io/Heroku) inject ``PORT`` and
    # expect the app to bind to ``0.0.0.0``. Honour both so a deployment
    # works without overriding our local-dev defaults.
    cloud_port_raw = os.getenv("PORT", "").strip()
    api_host_default = "0.0.0.0" if cloud_port_raw else "127.0.0.1"

    return Settings(
        bot_token=bot_token,
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db"),
        api_host=os.getenv("API_HOST", api_host_default),
        api_port=_get_int("PORT", _get_int("API_PORT", 8000)),
        webapp_url=os.getenv("WEBAPP_URL", "").strip(),
        webapp_dist_dir=os.getenv("WEBAPP_DIST_DIR", "").strip(),
        telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip(),
        scheduler_interval_seconds=_get_int("SCHEDULER_INTERVAL_SECONDS", 60),
        default_work_start=_parse_hhmm(os.getenv("DEFAULT_WORK_START", "10:00"), (10, 0)),
        default_work_end=_parse_hhmm(os.getenv("DEFAULT_WORK_END", "20:00"), (20, 0)),
        default_slot_step_minutes=_get_int("DEFAULT_SLOT_STEP_MINUTES", 30),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow").strip() or "UTC",
    )
