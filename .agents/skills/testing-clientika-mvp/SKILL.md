---
name: testing-clientika-mvp
description: How to test Clientika (FastAPI + aiogram + React Mini App) end-to-end without needing a real Telegram bot token or public HTTPS WebApp URL. Use when you need to verify the MVP's API, auth, public booking flow, scheduler logic, or Mini App pages.
---

# Testing Clientika MVP locally

Clientika is a single-process Python app (FastAPI + aiogram + APScheduler) that exposes:
- a Telegram bot (long-polling, needs a real BotFather token to actually run)
- a Mini App authenticated via HMAC-signed `initData`
- a public booking endpoint that needs no auth

Real Telegram involvement is a side effect we usually want to avoid in a test session, so we:
1. Skip the bot polling loop entirely (run FastAPI directly via `create_api_app`).
2. Use a fake `BOT_TOKEN` and locally HMAC-sign `initData` with the same token.
3. For browser tests, patch the built Mini App `dist/index.html` so `window.Telegram.WebApp` is mocked with our signed `initData`.

## Prerequisites

Already installed by the env config (`bot/.venv` and `bot/webapp/node_modules`). If not:

```bash
(cd bot && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt)
(cd bot/webapp && npm install)
```

## 1. Smoke + unit tests (no GUI)

```bash
(cd bot && .venv/bin/ruff check app tests)
(cd bot && .venv/bin/pytest -q)
(cd bot/webapp && npm run lint && npm run build)
```

All tests should pass cleanly. `pytest` covers:
- HMAC verification (`test_auth.py`)
- Repos + Cyrillic case-insensitive search (`test_repos.py`, `test_search_unicode.py`)
- Slot generation (`test_slots.py`)
- Bot handlers (`test_bot_handlers.py`)
- API factory import + route registration (`test_api_factory.py`)
- Parallel master-creation race (`test_master_race.py`)

## 2. Start API standalone (skipping bot polling)

Write a small driver that constructs the FastAPI app without `aiogram` polling:

```python
# run_api.py
import asyncio, uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.api import create_api_app
from app.config import Settings
from app.db import create_engine
from app.migrations import create_all

async def main():
    settings = Settings(
        bot_token="TEST_TOKEN_FOR_E2E",
        database_url="sqlite+aiosqlite:////tmp/clientika-test.db",
        webapp_dist_dir="webapp/dist",  # serves the built Mini App
        default_timezone="Europe/Moscow",
    )
    engine = create_engine(settings.database_url)
    await create_all(engine)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(settings=settings, session_factory=sf, notifier=None)
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    await uvicorn.Server(config).serve()

asyncio.run(main())
```

Run in background: `(cd bot && .venv/bin/python run_api.py &)`.

## 3. Sign initData locally

The same fake `BOT_TOKEN` used by the server is the secret for HMAC. Sign a payload for a fake user:

```python
import hmac, hashlib, json, time
from urllib.parse import urlencode

def sign_init_data(bot_token, tg_user_id, first_name="ТестМастер", username="testmaster"):
    user = json.dumps({"id": tg_user_id, "first_name": first_name, "last_name": "",
                       "username": username, "language_code": "ru"},
                      separators=(",", ":"), ensure_ascii=False)
    fields = {"auth_date": str(int(time.time())), "query_id": "AAEAAQABAAAAAA", "user": user}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)

print(sign_init_data("TEST_TOKEN_FOR_E2E", 999000001))
```

Use it in curl: `curl -H "Authorization: tma $INIT_DATA" http://127.0.0.1:8000/api/me`.

## 4. Backend curl test matrix

Drive every endpoint and assert response shape with a Python script. Cover:
- `GET /api/health` → 200 `{"status":"ok"}`
- `GET /api/me` → master auto-created with correct slug
- Services CRUD (POST/GET/PATCH/DELETE)
- Clients CRUD + Cyrillic search `?q=иван`
- Bookings CRUD + status transitions + `?today=true` filter
- Public flow: `GET /api/public/{slug}`, `GET /api/public/{slug}/availability?service_id=X&date=YYYY-MM-DD`, `POST /api/public/{slug}/book`

All of these should pass without modifying the DB between runs (use a fresh `:memory:` or delete the test sqlite file).

## 5. Scheduler logic test

Call `app.scheduler.run_reminder_tick(...)` directly with a `SpyNotifier` to verify reminder selection + idempotency without waiting for wall-clock ticks:

```python
from app.scheduler import run_reminder_tick
class SpyNotifier:
    def __init__(self): self.calls = []
    async def notify_master_new_booking(self, *a, **kw): self.calls.append(("master", a, kw))
    async def remind_client(self, *a, **kw): self.calls.append(("client", a, kw))
```

Verify the second `run_reminder_tick` sends zero (idempotency via `reminder_states`).

## 6. Browser Mini App test

The Mini App reads `window.Telegram.WebApp.initData` on launch. Telegram's real script (`https://telegram.org/js/telegram-web-app.js`) would overwrite our mock at runtime, so we REPLACE the real script tag with our mock in the built `dist/index.html`:

```python
from pathlib import Path
DIST = Path("bot/webapp/dist/index.html")
INIT_DATA = sign_init_data("TEST_TOKEN_FOR_E2E", 999000001)
mock = f"""<script>
window.Telegram = {{ WebApp: {{
  initData: "{INIT_DATA}",
  initDataUnsafe: {{ user: {{ id: 999000001, first_name: "ТестМастер", username: "testmaster" }} }},
  ready: () => {{}}, expand: () => {{}}, MainButton: {{ hide: () => {{}} }},
  themeParams: {{}}, colorScheme: "light"
}} }};
</script>"""
html = DIST.read_text()
marker = '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
DIST.write_text(html.replace(marker, mock))
```

**Critical**: REPLACE the marker, don't append before it. If both scripts are present the real one wins and your mock is wiped.

Then `(cd bot && .venv/bin/python run_api.py)` and open `http://127.0.0.1:8000/` in Chrome. The Mini App loads with our mocked user and authenticates automatically.

### Browser flow checklist

| It should... |
|---|
| load Today page (greeting `Привет, <Name>!`, counter `Сегодня записей: 0`) |
| create services (price + duration; English names if using xdotool — see caveat) |
| add clients with phone, Telegram username, notes |
| create bookings (service + client + datetime → status `Подтверждена`) |
| show today's bookings on Today page |
| open Settings (slug + tz `Europe/Moscow` + work hours 10:00–20:00 + step 30) |
| render public booking page at `?master=<slug>` |
| correctly hide slot overlapping an existing booking |
| create booking anonymously via public page |
| reflect anonymous booking in master view |
| flip status (Новая → Подтверждена → Пришёл → Отмена / Не пришёл) |
| update Stats (Доход, Записей, «пришёл», Топ услуг) on Пришёл |

### Caveat: Cyrillic typing

`xdotool type` does NOT pass non-ASCII characters reliably — Russian text appears as literal `\u0421\u0442...` escapes. Workarounds:
- Use Latin-character test data (`Haircut`, `Anna Petrova`) for the visual demo.
- Cyrillic at the API layer is exercised by `test_search_unicode.py` and curl-driven backend tests.
- For UI demos requiring Cyrillic, use `xclip` to put text on the clipboard and `ctrl+v` to paste.

## 7. What you canNOT test in a sandbox

These all need real-world side effects and should be deferred or asked of the user:
- Real Telegram bot login + Mini App launched through the Telegram client (BotFather token + public HTTPS WebApp URL)
- `aiogram` long-polling reaching Telegram
- APScheduler real wall-clock 60s tick in a long-running process
- `docker compose up --build` against a public tunnel
- Telegram Stars billing

Flag these explicitly in the test report so the user knows what was and wasn't covered.

## 8. Bugs historically blocking first-time runs (sanity check before claiming the MVP works)

All fixed in PR #1, but if regressed they will block app startup or first-load:

1. `204` DELETE routes must declare `response_class=Response` and return `Response(status_code=204)` — typing `-> None` triggers `AssertionError` at import.
2. Query-param aliases use `fastapi.Query(..., alias=...)`, NEVER `pydantic.Field`.
3. SQLite needs `LOWER` registered as Python `str.lower` for Cyrillic `ilike`.
4. SPA fallback routes with union return types need `response_model=None`.
5. `get_current_master` must serialize first-time INSERT (per-user `asyncio.Lock` + fresh-session commit). The Mini App's Today page issues `Promise.all([getMe, listBookingsToday])` so every parallel request races to INSERT. Regression test: `tests/test_master_race.py`.
