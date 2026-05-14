from __future__ import annotations

import secrets
from collections.abc import Iterable

from sqlalchemy import bindparam, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Base, Master


async def create_all(engine: AsyncEngine) -> None:
    """Create all tables that do not exist yet, then run schema patches.

    For the MVP we intentionally do not use Alembic. Adding a new column on a
    fresh database is handled by ``Base.metadata.create_all``. For databases
    that pre-date the column we apply a tiny set of hand-written ALTERs and
    backfills below.

    Schema patches are idempotent — they check what's there before changing
    anything — so it's safe to call ``create_all`` on every startup.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_patch_master_role_columns)


def _patch_master_role_columns(sync_conn) -> None:
    """Add ``is_master``/``is_admin`` columns on legacy ``masters`` tables.

    Every row that existed *before* the columns were introduced is treated as
    an active master (those installs were created in the old single-role
    world), so we backfill ``is_master=1`` for them. New rows added after the
    migration get ``is_master=0`` by default via the ORM, exactly as intended.
    """
    inspector = inspect(sync_conn)
    if "masters" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("masters")}

    if "is_master" not in existing:
        sync_conn.execute(
            text("ALTER TABLE masters ADD COLUMN is_master BOOLEAN NOT NULL DEFAULT 0")
        )
        # Backfill: rows that existed before this migration were all masters
        # in the legacy single-role model, so flip them on.
        sync_conn.execute(text("UPDATE masters SET is_master = 1"))

    if "is_admin" not in existing:
        sync_conn.execute(
            text("ALTER TABLE masters ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0")
        )


def _slug_taken_sync(sync_conn, slug: str) -> bool:
    res = sync_conn.execute(select(Master.id).where(Master.slug == slug))
    return res.scalar_one_or_none() is not None


def _unique_slug_sync(sync_conn, hint: str) -> str:
    base = "".join(ch for ch in hint.lower() if ch.isalnum())[:48] or f"u{secrets.token_hex(3)}"
    candidate = base
    suffix = 2
    while _slug_taken_sync(sync_conn, candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


async def seed_admins(
    engine: AsyncEngine,
    admin_tg_ids: Iterable[int],
) -> None:
    """Make sure every Telegram id in ``admin_tg_ids`` has a ``masters`` row
    with ``is_admin=True``.

    Existing admins keep their other flags (in particular ``is_master``);
    accounts that have never opened the bot get a stub row so the admin can
    open the Mini App / call admin endpoints without first sending ``/start``.
    """
    ids = [int(i) for i in admin_tg_ids if int(i) > 0]
    if not ids:
        return

    async with engine.begin() as conn:
        await conn.run_sync(_seed_admins_sync, ids)


def _seed_admins_sync(sync_conn, ids: list[int]) -> None:
    res = sync_conn.execute(
        select(Master.tg_user_id).where(Master.tg_user_id.in_(ids))
    )
    existing_ids = {int(r) for (r,) in res.all()}

    if existing_ids:
        sync_conn.execute(
            update(Master)
            .where(Master.tg_user_id.in_(list(existing_ids)))
            .values(is_admin=True)
        )

    # Create stub rows for admins who haven't opened the bot yet. We don't
    # auto-flip is_master here — an admin can promote themselves via the
    # admin panel if they also want to act as a master. The display name is a
    # plain "admin-<id>" placeholder; the row gets updated with real Telegram
    # metadata the first time the user actually opens the bot.
    missing = [tg_id for tg_id in ids if tg_id not in existing_ids]
    if not missing:
        return

    # We have a raw ``Connection`` from ``run_sync`` here, so use a Core
    # INSERT rather than ORM ``Session.add`` (a Session bound to a Connection
    # via ``create_savepoint`` does not flush back to the outer transaction
    # reliably across SQLAlchemy versions / drivers).
    insert_stmt = Master.__table__.insert().values(
        tg_user_id=bindparam("tg_user_id"),
        tg_chat_id=bindparam("tg_chat_id"),
        tg_username=None,
        display_name=bindparam("display_name"),
        slug=bindparam("slug"),
        timezone="Europe/Moscow",
        language="ru",
        work_start_minutes=10 * 60,
        work_end_minutes=20 * 60,
        slot_step_minutes=30,
        is_master=False,
        is_admin=True,
    )
    for tg_id in missing:
        slug = _unique_slug_sync(sync_conn, f"admin-{tg_id}")
        sync_conn.execute(
            insert_stmt,
            {
                "tg_user_id": tg_id,
                "tg_chat_id": tg_id,
                "display_name": f"admin-{tg_id}",
                "slug": slug,
            },
        )
