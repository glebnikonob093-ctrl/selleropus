# AGENTS.md

Guidance for AI agents working on this repository.

## Project layout

```
bot/
  app/                # Python package (FastAPI + aiogram)
    main.py           # entrypoint: starts FastAPI + bot polling + scheduler
    config.py         # Settings loaded from .env
    db.py             # async SQLAlchemy engine/session helpers
    models.py         # SQLAlchemy ORM models
    migrations.py     # `create_all` bootstrap (lightweight, no Alembic for MVP)
    auth.py           # Telegram WebApp initData verification
    repos.py          # data-access helpers
    notifications.py  # bot-side message senders
    scheduler.py      # APScheduler: reminders + daily summaries
    api/              # FastAPI routers (one file per resource)
    bot/              # aiogram handlers
  tests/              # pytest unit tests
  webapp/             # React + Vite Mini App
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example
```

## Conventions

- **Python**: 3.12+. Async SQLAlchemy 2.0. Type hints everywhere. Ruff for
  lint/format (config in `bot/pyproject.toml`).
- **TypeScript**: strict mode in `webapp/`. `eslint` for lint.
- **Imports**: top of file; never inline imports inside functions.
- **Times**: store everything as naive UTC `datetime` in DB; convert to master
  timezone on display.
- **Money**: store prices as integer rubles (no kopeks for MVP).
- **Slugs**: lowercase ASCII, generated from `display_name` or `username`,
  collision suffix `-2`, `-3`, ...
- **No Alembic**: `migrations.create_all` builds tables from models on startup.
  When you add a column, write a small idempotent ALTER in `migrations.py`.
- **Don't commit secrets**: `.env`, `bot/.env`, `data/*.db` are gitignored.

## Running locally

```bash
cd bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill BOT_TOKEN
python -m app

# Mini App in another terminal
cd bot/webapp
npm install
npm run dev
```

## Tests / lint

```bash
cd bot
ruff check app tests
pytest -q
(cd webapp && npm run lint && npm run build)
```
