"""Regression: SQLite's default LOWER doesn't case-fold Cyrillic, so without
a Unicode-aware LOWER function, ``Client.name.ilike(...)`` fails to match
"иван" against "Иван". The fix lives in app.db.create_engine which registers
``str.lower`` as the SQLite ``LOWER`` function.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import create_engine, create_session_factory
from app.migrations import create_all
from app.models import Client, Master


async def test_ilike_matches_cyrillic_case_insensitively() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            master = Master(
                tg_user_id=1,
                tg_chat_id=1,
                tg_username="x",
                display_name="X",
                slug="x",
            )
            session.add(master)
            await session.flush()
            session.add(Client(master_id=master.id, name="Иван", phone="+79991112233"))
            session.add(Client(master_id=master.id, name="Мария", phone="+79992223344"))
            await session.flush()

            for query in ("иван", "ИВАН", "Иван", "ИвАн", "ва"):
                stmt = select(Client).where(
                    Client.master_id == master.id,
                    Client.name.ilike(f"%{query}%"),
                )
                rows = list((await session.execute(stmt)).scalars())
                assert any(c.name == "Иван" for c in rows), (
                    f"query {query!r} did not match 'Иван'; got {[c.name for c in rows]}"
                )
    finally:
        await engine.dispose()
