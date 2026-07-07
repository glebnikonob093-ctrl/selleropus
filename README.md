# Clientika

Мини-CRM в Telegram для самозанятых: бьюти-мастеров, репетиторов, тренеров,
фотографов. Записи, клиенты, напоминания и доход — в одном Telegram, без Excel
и блокнота.

Гайд для мастеров (без технических терминов): [docs/MASTER_GUIDE_RU.md](docs/MASTER_GUIDE_RU.md)

## Возможности

* **Главный бот `@Clientikabot`** — рабочее место мастера: записи, клиенты,
  статистика, расписание, команда, блокировки.
* **Персональный бот мастера** — каждый мастер подключает своего бота через
  `/addbot`, и клиенты записываются прямо в нём (календарь, свободные слоты,
  «Мои записи»).
* **Mini App** (React) с экранами «Сегодня», «Записи», «Клиенты», «Услуги»,
  «Доход» и публичной страницей записи.
* **Напоминания** (APScheduler): клиенту за 24ч и за 2ч, мастеру — утренняя
  сводка на день. Уведомления клиентам идут через бот мастера.
* **REST API** (FastAPI) с проверкой Telegram WebApp `initData`.
* **БД**: SQLite через async SQLAlchemy 2 (переключается на Postgres через
  `DATABASE_URL`).

## Структура репозитория

```
bot/
├── app/                # backend: FastAPI + aiogram + scheduler
│   ├── api/            # роутеры REST API
│   ├── bot/            # aiogram-хендлеры
│   │   ├── handlers.py # главный бот мастера
│   │   ├── client_bot.py  # персональный бот мастера (запись клиентов)
│   │   └── multibot.py    # менеджер запуска ботов мастеров
│   ├── auth.py         # проверка Telegram initData
│   ├── config.py       # загрузка .env -> Settings
│   ├── db.py
│   ├── main.py         # entrypoint: API + бот + scheduler в одном процессе
│   ├── migrations.py   # create_all (без Alembic для MVP)
│   ├── models.py       # Master / Service / Client / Booking / MasterBot / ...
│   ├── notifications.py
│   ├── repos.py
│   ├── scheduler.py
│   └── slots.py        # генерация слотов
├── tests/              # pytest
├── webapp/             # React Mini App (Vite + TS)
├── Dockerfile          # multi-stage: webapp build -> python image
├── docker-compose.yml
├── pyproject.toml
└── requirements.txt
```

## Запуск через Docker

```bash
cd bot
cp .env.example .env   # заполнить BOT_TOKEN
docker compose up --build
```

После старта: API + Mini App на `http://<host>:8000`, бот — long-polling.

## Локальный запуск (без Docker)

```bash
cd bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # заполнить BOT_TOKEN
python -m app
```

Mini App в отдельном терминале:

```bash
cd bot/webapp
npm install
npm run dev
```

* API — `http://127.0.0.1:8000`
* Mini App (dev) — `http://localhost:5173` (Vite проксирует `/api` в FastAPI)

## Тесты и линт

```bash
cd bot
. .venv/bin/activate
ruff check app tests
pytest -q

cd webapp
npm run build       # tsc + vite build
npm run lint        # eslint
```

## Конфигурация (`.env`)

См. [`bot/.env.example`](bot/.env.example). Ключевые переменные:

| Переменная                   | Описание                                                  |
| ---------------------------- | -------------------------------------------------------- |
| `BOT_TOKEN`                  | токен главного Telegram-бота (обязательно)               |
| `DATABASE_URL`               | по умолчанию `sqlite+aiosqlite:///./data/app.db`         |
| `API_HOST` / `API_PORT`      | host/port FastAPI                                        |
| `WEBAPP_URL`                 | публичный HTTPS-URL Mini App                             |
| `TELEGRAM_PROXY_URL`         | необязательный proxy для запросов к Telegram             |
| `SCHEDULER_INTERVAL_SECONDS` | как часто тикает шедулер (по умолчанию 60 сек)           |
| `DEFAULT_TIMEZONE`           | дефолтная таймзона мастера (по умолчанию `Europe/Moscow`)|
