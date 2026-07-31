---
name: testing-clientika
description: End-to-end test the Clientika Telegram Mini-CRM bot (FastAPI backend + React Mini App). Use when verifying booking, status-change notifications, service CRUD, the public booking flow, or SPA-serving/security changes.
---

# Testing Clientika (selleropus)

Telegram Mini-CRM: FastAPI backend (`bot/app`), aiogram bot, React/Vite Mini App (`bot/webapp`).
Auth is via Telegram `initData` HMAC. There is no real Telegram in the test box, so use the local
harness that stubs a signed `initData` and a logging notifier.

## Devin Secrets Needed
- `BOT_TOKEN` — any non-empty value works for local testing (e.g. `123456:TEST`); the harness signs
  initData with whatever token you pass. A real token is only needed for live Telegram dispatch.

## Run the local test harness
`bot/run_test_server.py` seeds master `anna` (tg_user_id=555111) + 2 services, patches the built
`dist/index.html` with a signed stub `window.Telegram.WebApp`, and runs FastAPI + the SPA on
`127.0.0.1:8000` with a `LoggingNotifier` that prints `[NOTIFY] ...` to stdout.

```bash
cd bot/webapp && npm run build            # build the SPA first
cd ..  # bot/
mkdir -p data && rm -f data/test_e2e.db   # fresh DB each run
BOT_TOKEN="123456:TEST" \
DATABASE_URL="sqlite+aiosqlite:///./data/test_e2e.db" \
WEBAPP_DIST_DIR="$(pwd)/webapp/dist" \
python run_test_server.py
```
On Windows PowerShell, set the env vars separately (`$env:BOT_TOKEN="123456:TEST"`, etc.) then run
`python run_test_server.py`. Run the server with `run_in_background` so you can poll its stdout for
`[NOTIFY]` / request log lines.

Setting `WEBAPP_DIST_DIR` is important: it registers the SPA routes — that is the **production**
config and the path where a class of FastAPI route-construction bugs only surfaces. Always test
with it set.

Verify boot: log shows `[seed] master slug=anna ...` and `Application startup complete`.

## Routes / API quick-checks (curl)
- Public master page route is `/api/public/{slug}` (e.g. `/api/public/anna`) — **not** `/masters/{slug}`.
- Availability: `/api/public/anna/availability?service_id=1&date=YYYY-MM-DD` (the `date` query alias).
- `GET /api/health` → `{"status":"ok"}`.

## Browser navigation gotchas (Windows + Chrome computer-use)
- The `type` action sometimes **drops the `:` in `host:port`** (you get `127.0.0.18000`). Work around it:
  type the host, send the colon via `key` = `shift+semicolon`, then type `8000/...`.
- The `type` action also frequently **drops the first character after a click** (and mangles capitals),
  so a name typed as `Test Client` may land as `est lient`. This is a computer-use keyboard quirk, NOT
  an app bug; the booking still succeeds. If a clean value matters, prefix with a sacrificial space.
- The Chrome omnibox tends to **drop `?query=` params** / turn the URL into a Google search. To reach
  the public booking page use the **path route `/book/<slug>`** (handled in `App.tsx`) instead of
  `?master=<slug>`. Master app pages are reachable directly: `/bookings`, `/services`, etc.
- The Mini App uses **bottom-sheet modals**; their bottom buttons (e.g. "Скрыть услугу") sit **behind
  the Windows taskbar** when Chrome is maximized. Fix: **un-maximize** the Chrome window (restore-down)
  so the sheet's buttons clear the taskbar. Zooming out does NOT help (the sheet stays anchored to the
  viewport bottom).

## Golden-path E2E flow (what to assert)
1. **Public booking** (`/book/anna`): master + services + time slots render → pick a slot, enter a name,
   submit → "Записали!" confirmation. Log: `POST /api/public/anna/bookings 201` + `[NOTIFY] master_new_booking`.
   - If the date alias is broken, **no slots render** (422) — that's the visible failure signal.
   - Pick a **future date** — past/today slots that have passed are filtered out, so an empty grid on
     today's date is expected, not a bug.
2. **Master app** (`/bookings`): the booking appears with pill "Новая".
3. **Status change**: click "Подтверждена" → pill updates and persists on reload. Log MUST show
   `[NOTIFY] status_change ... new=confirmed` **then** `PATCH /api/bookings/<id> 200` (notify-after-commit).
4. **Service delete** (`/services` → open a service → "Скрыть услугу"): log shows
   `DELETE /api/services/<id> 204 No Content`. This is a **soft-delete** (service stays, marked `скрыта`).
5. **Path traversal**: `GET /%2e%2e/<some-file>` returns the SPA shell, not the file. The adversarial
   proof is the unit test `test_spa_fallback_blocks_path_traversal` (revert the guard → secret leaks).

## Client referral / in-bot booking (PR #11 onward)
- The Master's shareable link points at the **bot**: `t.me/<bot>?start=<slug>`. `/start <slug>` = client
  (never registered as master); `/start` = master. Client picks Mini App or in-chat FSM booking.
- The **bot chat FSM is not live-testable** in this box (no Telegram network). Cover it with
  `tests/test_bot_flow.py` (full walkthrough through the aiogram dispatcher) — do NOT claim a live
  Telegram test. The Mini App path (`/book/<slug>`) IS the live-testable UI and is the destination of
  the client's "📱 Записаться в приложении" button.
- `create_client_booking` / `available_day_slots` in `app/booking.py` back both the HTTP API and the
  bot; a break there shows up as the public page rendering no slots or failing to submit.

## Unit tests
`cd bot && BOT_TOKEN="123456:TEST" python -m pytest -q` — all should pass (API tests need a BOT_TOKEN env).
Bot/booking suites: `tests/test_bot_flow.py`, `tests/test_booking.py`.
Lint/format: `ruff check` / `ruff format`. Webapp: `npm run build`, `npm run lint`.

## Known product gap (escalate, don't "fix" silently)
After the morning-summary spam fix, masters with **no** bookings get **no** morning greeting at all.
A once-per-day greeting on empty days without spam needs a schema change (per-master per-day marker),
since idempotency is currently keyed off booking rows. This is a product decision — flag it to the user.
