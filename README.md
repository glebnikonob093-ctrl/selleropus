# Clientika

Mini-CRM в Telegram для самозанятых: бьюти-мастера, репетиторы, тренеры, фотографы.
Все записи, клиенты, напоминания и доход — в одном Telegram, без Excel и блокнота.

## Что внутри MVP

* **Telegram-бот** (aiogram 3): `/start`, `/link`, `/today`, кнопка Mini App,
  уведомления мастеру при новых записях.
* **Mini App** (React + Vite + TS) с экранами «Сегодня», «Записи», «Клиенты»,
  «Услуги», «Доход» и публичной страницей записи `?master=<slug>`.
* **REST API** (FastAPI) с верификацией Telegram WebApp `initData`.
* **Шедулер напоминаний** (APScheduler): за 24ч и за 2ч клиенту, утренняя сводка
  мастеру.
* **БД**: SQLite через async SQLAlchemy 2 (легко переключить на Postgres через
  `DATABASE_URL`).

## Структура репозитория

```
bot/
├── app/                # backend: FastAPI + aiogram + scheduler
│   ├── api/            # роутеры REST API
│   ├── bot/            # aiogram-handlers
│   ├── auth.py         # верификация Telegram initData
│   ├── config.py       # загрузка .env -> Settings
│   ├── db.py
│   ├── main.py         # entrypoint: API + bot + scheduler в одном процессе
│   ├── migrations.py   # create_all (без Alembic для MVP)
│   ├── models.py       # Master / Service / Client / Booking / ReminderState
│   ├── notifications.py
│   ├── repos.py
│   ├── scheduler.py
│   └── slots.py        # генерация слотов для публичной записи
├── tests/              # pytest
├── webapp/             # React Mini App (Vite + TS)
├── Dockerfile          # multi-stage: webapp build -> python image
├── docker-compose.yml
├── pyproject.toml
└── requirements.txt
```

## Локальный запуск (без Docker)

```bash
cd bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполнить BOT_TOKEN

python -m app
```

В отдельном терминале:

```bash
cd bot/webapp
npm install
npm run dev
```

* API — `http://127.0.0.1:8000`
* Mini App (dev) — `http://localhost:5173`
  (Vite проксирует `/api` в FastAPI)

## Запуск через Docker

```bash
cd bot
cp .env.example .env
docker compose up --build
```

`docker-compose` собирает Mini App, кладёт её в `/app/webapp_dist` и FastAPI
отдаёт её как статику. После старта:

* API + Mini App — `http://<host>:8000`
* Бот — long-polling, токен из `.env`

## Аутентификация Mini App

Mini App шлёт заголовок `Authorization: tma <initData>` (и дублирует в
`X-Telegram-Init-Data`). Бэкенд проверяет HMAC-SHA256 от sorted-key=value
с секретом `HMAC_SHA256("WebAppData", BOT_TOKEN)` (как описано в
[Telegram WebApp docs](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)).
По первой валидной auth-сессии создаётся `Master`, дальше используется он же.

## Публичная запись

* Каждому мастеру выдаётся `slug` (по имени, с защитой от коллизий).
* Ссылка для клиентов: `${WEBAPP_URL}?master=<slug>` — открывает публичную
  страницу записи внутри Mini App.
* Свободные слоты считаются на лету из расписания мастера и пересечения с
  активными записями.
* Анонимная запись из публичного flow не требует авторизации в Telegram.

## Напоминания

`APScheduler` каждые `SCHEDULER_INTERVAL_SECONDS` секунд:

* за 24ч до записи — клиенту;
* за 2ч до записи — клиенту;
* утром (08:00 UTC, разово на день) — мастеру список записей на день.

Каждое отправленное напоминание записывается в `reminder_states` с unique
constraint на `(booking_id, kind)`, поэтому повторных отправок не будет.

## Тесты и линт

```bash
cd bot
. .venv/bin/activate
ruff check app tests
pytest -q
```

Есть unit-тесты на:

* генератор слотов (`slots.py`);
* верификацию initData (`auth.py`);
* репозиторий (`repos.py`).

Фронт:

```bash
cd bot/webapp
npm run build       # tsc + vite build
npm run lint        # eslint
```

## Конфигурация (`.env`)

См. [`bot/.env.example`](bot/.env.example). Ключевые переменные:

| Переменная                | Описание                                                      |
| ------------------------- | ------------------------------------------------------------- |
| `BOT_TOKEN`               | токен Telegram-бота (обязательно)                             |
| `DATABASE_URL`            | по умолчанию `sqlite+aiosqlite:///./data/app.db`              |
| `API_HOST` / `API_PORT`   | host/port FastAPI                                             |
| `WEBAPP_URL`              | публичный HTTPS-URL Mini App                                  |
| `TELEGRAM_PROXY_URL`      | необязательный proxy для исходящих запросов к Telegram        |
| `SCHEDULER_INTERVAL_SECONDS` | как часто шедулер тикает (по умолчанию 60 сек)             |
| `DEFAULT_TIMEZONE`        | дефолтная таймзона мастера (по умолчанию `Europe/Moscow`)     |

## Дальше (после MVP)

* Telegram Stars / ЮKassa для подписки Pro/Premium.
* Multi-master аккаунты (несколько мастеров под одним ИП).
* Шаблоны сообщений и автоматические «верни клиента».
* Брендированная страница записи.
* Postgres + Alembic, S3-бэкап.
