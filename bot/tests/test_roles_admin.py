"""End-to-end role-based access tests against the FastAPI app.

These tests boot the real ``create_api_app`` via an in-process ``httpx``
client and exercise the new role-aware behaviour:

* ``/start`` (i.e. first ``/api/me``) creates an unprivileged user — the
  master-scoped routes must return 403 for them.
* Admins (``is_admin=true`` via ``ADMIN_TG_IDS``) bypass the master gate.
* Admin endpoints (``/api/admin/users``) are admin-only.
* Promotions via the admin endpoint are idempotent and unlock master routes.
* Admins cannot demote themselves.
* The migration backfills ``is_master=true`` on legacy rows so existing
  masters keep their access after the schema patch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.api import create_api_app
from app.config import Settings
from app.migrations import create_all, seed_admins
from app.models import Master

BOT_TOKEN = "TEST_TOKEN_FOR_ROLES"
ADMIN_TG_ID = 1200247714


def _settings(*, admin_ids: tuple[int, ...] = (ADMIN_TG_ID,)) -> Settings:
    return Settings(
        bot_token=BOT_TOKEN,
        database_url="sqlite+aiosqlite:///:memory:",
        api_host="127.0.0.1",
        api_port=8000,
        webapp_url="",
        webapp_dist_dir="",
        telegram_proxy_url="",
        scheduler_interval_seconds=60,
        default_work_start=(10, 0),
        default_work_end=(20, 0),
        default_slot_step_minutes=30,
        default_timezone="UTC",
        admin_tg_ids=admin_ids,
        admin_contact_url=f"tg://user?id={admin_ids[0]}" if admin_ids else "",
        become_master_conditions="Pro 299 ₽/мес + написать админу",
    )


def _signed_init_data(tg_user_id: int, username: str = "tester") -> str:
    user = json.dumps(
        {
            "id": tg_user_id,
            "first_name": "Test",
            "last_name": "",
            "username": username,
            "language_code": "ru",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAEAAQABAAAAAA",
        "user": user,
    }
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def _auth(tg_user_id: int, username: str = "tester") -> dict[str, str]:
    return {"Authorization": f"tma {_signed_init_data(tg_user_id, username)}"}


@pytest.mark.asyncio
async def test_new_user_is_not_master(engine: AsyncEngine) -> None:
    """First /api/me call creates an unprivileged user, NOT a master."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=()),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/me", headers=_auth(7001))
        assert r.status_code == 200, r.text
        me = r.json()
        assert me["is_master"] is False
        assert me["is_admin"] is False
        assert me["become_master_conditions"]


@pytest.mark.asyncio
async def test_non_master_cannot_access_master_routes(engine: AsyncEngine) -> None:
    """A vanilla user gets 403 on every master-scoped endpoint."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=()),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Bootstrap the user row via /api/me (which is public to any user).
        boot = await c.get("/api/me", headers=_auth(7002))
        assert boot.status_code == 200

        for path in (
            "/api/services",
            "/api/clients",
            "/api/bookings",
            "/api/bookings/today",
            "/api/stats?period=day",
        ):
            r = await c.get(path, headers=_auth(7002))
            assert r.status_code == 403, f"{path}: expected 403, got {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_admin_bootstrap_via_env(engine: AsyncEngine) -> None:
    """A user listed in ``ADMIN_TG_IDS`` is upgraded to is_admin on first hit
    even if they were never seeded via ``seed_admins``."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/me", headers=_auth(ADMIN_TG_ID, "boss"))
        assert r.status_code == 200, r.text
        me = r.json()
        assert me["is_admin"] is True
        # is_master stays off until the admin explicitly promotes themselves.
        assert me["is_master"] is False


@pytest.mark.asyncio
async def test_admin_endpoints_require_admin(engine: AsyncEngine) -> None:
    """``/api/admin/users`` returns 403 for non-admin users and 200 for admins."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Non-admin: 403.
        r = await c.get("/api/admin/users", headers=_auth(8001))
        assert r.status_code == 403, r.text

        # Admin: 200, sees at least themselves in the list.
        r = await c.get("/api/admin/users", headers=_auth(ADMIN_TG_ID, "boss"))
        assert r.status_code == 200, r.text
        users = r.json()
        assert any(u["tg_user_id"] == ADMIN_TG_ID and u["is_admin"] for u in users)


@pytest.mark.asyncio
async def test_admin_can_promote_user_by_tg_id(engine: AsyncEngine) -> None:
    """POST /api/admin/users promotes by tg_user_id and is idempotent. After
    promotion the user can hit master routes."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Admin promotes a brand-new TG id (no existing row).
        r = await c.post(
            "/api/admin/users",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={
                "tg_user_id": 9001,
                "display_name": "New Master",
                "tg_username": "newmaster",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["is_master"] is True
        assert body["tg_user_id"] == 9001

        # Idempotent: posting again returns the same row.
        r2 = await c.post(
            "/api/admin/users",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"tg_user_id": 9001},
        )
        assert r2.status_code == 201, r2.text
        assert r2.json()["id"] == body["id"]

        # Newly promoted user can now load master endpoints.
        r3 = await c.get("/api/services", headers=_auth(9001, "newmaster"))
        assert r3.status_code == 200, r3.text


@pytest.mark.asyncio
async def test_non_admin_cannot_promote(engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/api/admin/users",
            headers=_auth(8002),
            json={"tg_user_id": 9002},
        )
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_admin_can_demote_master(engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        created = await c.post(
            "/api/admin/users",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"tg_user_id": 9003, "display_name": "Tmp"},
        )
        assert created.status_code == 201
        user_id = created.json()["id"]

        r = await c.delete(
            f"/api/admin/users/{user_id}/master",
            headers=_auth(ADMIN_TG_ID, "boss"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["is_master"] is False

        # Demoted user can no longer hit master routes.
        r2 = await c.get("/api/services", headers=_auth(9003, "tmp"))
        assert r2.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_demote_themselves(engine: AsyncEngine) -> None:
    """PATCH /api/admin/users/{me} with is_admin=false should fail to prevent
    accidental lockout."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        me = await c.get("/api/me", headers=_auth(ADMIN_TG_ID, "boss"))
        assert me.status_code == 200
        my_id = me.json()["id"]

        r = await c.patch(
            f"/api/admin/users/{my_id}",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"is_admin": False},
        )
        assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_admin_can_set_user_roles(engine: AsyncEngine) -> None:
    """PATCH /api/admin/users/{id} flips is_master and is_admin flags."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api_app(
        settings=_settings(admin_ids=(ADMIN_TG_ID,)),
        session_factory=session_factory,
        notifier=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Bootstrap target user as a regular non-master.
        boot = await c.get("/api/me", headers=_auth(9004, "regular"))
        assert boot.status_code == 200
        target_id = boot.json()["id"]

        r = await c.patch(
            f"/api/admin/users/{target_id}",
            headers=_auth(ADMIN_TG_ID, "boss"),
            json={"is_master": True, "is_admin": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_master"] is True
        assert body["is_admin"] is True


@pytest.mark.asyncio
async def test_migration_preserves_existing_masters() -> None:
    """A database that pre-dates the role columns must be patched in-place
    so every existing row gets ``is_master=1``."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    try:
        # Step 1: create a legacy ``masters`` table without the role columns.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE masters (
                        id INTEGER PRIMARY KEY,
                        tg_user_id BIGINT UNIQUE NOT NULL,
                        tg_chat_id BIGINT NOT NULL,
                        tg_username VARCHAR(64),
                        display_name VARCHAR(120) NOT NULL DEFAULT '',
                        slug VARCHAR(64) UNIQUE NOT NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow',
                        language VARCHAR(8) NOT NULL DEFAULT 'ru',
                        work_start_minutes INTEGER NOT NULL DEFAULT 600,
                        work_end_minutes INTEGER NOT NULL DEFAULT 1200,
                        slot_step_minutes INTEGER NOT NULL DEFAULT 30,
                        created_at DATETIME
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO masters (tg_user_id, tg_chat_id, display_name, slug) "
                    "VALUES (5555, 5555, 'Legacy', 'legacy')"
                )
            )

        # Step 2: run create_all — should add columns and backfill is_master=1.
        await create_all(engine)
        await seed_admins(engine, [ADMIN_TG_ID])

        # Step 3: verify the legacy row is still a master, and the admin row
        # was inserted.
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            rows = (await session.execute(select(Master))).scalars().all()
            by_id = {m.tg_user_id: m for m in rows}
            assert 5555 in by_id, "legacy master row was lost"
            assert by_id[5555].is_master is True, "legacy master lost is_master"
            assert by_id[5555].is_admin is False
            assert ADMIN_TG_ID in by_id, "admin stub row was not created"
            assert by_id[ADMIN_TG_ID].is_admin is True
    finally:
        await engine.dispose()
