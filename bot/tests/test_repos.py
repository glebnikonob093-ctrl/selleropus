from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BOOKING_STATUS_CAME,
    BOOKING_STATUS_CANCELLED,
    Booking,
    Client,
    Master,
    Service,
)
from app.repos import (
    find_clients_to_return,
    find_or_create_client,
    generate_unique_slug,
    get_revenue,
    list_bookings_in_window,
    slugify,
    upsert_master_from_tg,
)


def test_slugify_basic() -> None:
    assert slugify("Анна Петрова") == ""
    assert slugify("Anna Petrova") == "anna-petrova"
    assert slugify("  multiple   spaces  ") == "multiple-spaces"
    assert slugify("Some@Username") == "some-username"


async def test_generate_unique_slug_collision(session: AsyncSession) -> None:
    session.add(
        Master(
            tg_user_id=1,
            tg_chat_id=1,
            tg_username="test",
            display_name="Test",
            slug="anna",
        )
    )
    await session.flush()

    new_slug = await generate_unique_slug(session, "anna")
    assert new_slug == "anna-2"


async def test_upsert_master_creates_then_updates(session: AsyncSession) -> None:
    master = await upsert_master_from_tg(
        session,
        tg_user_id=10,
        tg_chat_id=10,
        tg_username=None,
        display_name_hint="Anna",
        default_timezone="Europe/Moscow",
        default_work_start_minutes=600,
        default_work_end_minutes=1200,
        default_slot_step_minutes=30,
    )
    assert master.id is not None
    assert master.slug == "anna"

    same = await upsert_master_from_tg(
        session,
        tg_user_id=10,
        tg_chat_id=99,
        tg_username="anna_b",
        display_name_hint="Anna B",
        default_timezone="UTC",
        default_work_start_minutes=600,
        default_work_end_minutes=1200,
        default_slot_step_minutes=30,
    )
    assert same.id == master.id
    assert same.tg_chat_id == 99
    assert same.tg_username == "anna_b"


async def test_find_or_create_client_dedupes_by_tg_id(session: AsyncSession) -> None:
    master = await upsert_master_from_tg(
        session,
        tg_user_id=20,
        tg_chat_id=20,
        tg_username="m",
        display_name_hint="M",
        default_timezone="UTC",
        default_work_start_minutes=600,
        default_work_end_minutes=1200,
        default_slot_step_minutes=30,
    )
    a = await find_or_create_client(session, master.id, name="Bob", tg_user_id=42)
    b = await find_or_create_client(session, master.id, name="Bob", tg_user_id=42)
    assert a.id == b.id


async def test_revenue_and_window(session: AsyncSession) -> None:
    master = await upsert_master_from_tg(
        session,
        tg_user_id=30,
        tg_chat_id=30,
        tg_username="mm",
        display_name_hint="MM",
        default_timezone="UTC",
        default_work_start_minutes=600,
        default_work_end_minutes=1200,
        default_slot_step_minutes=30,
    )
    service = Service(master_id=master.id, name="Cut", price=1000, duration_minutes=60)
    session.add(service)
    await session.flush()
    client = Client(master_id=master.id, name="C")
    session.add(client)
    await session.flush()

    base = datetime(2026, 5, 10, 10, 0)
    bookings = [
        Booking(
            master_id=master.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=base,
            ends_at=base + timedelta(hours=1),
            status=BOOKING_STATUS_CAME,
            price_snapshot=1000,
        ),
        Booking(
            master_id=master.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=base + timedelta(hours=2),
            ends_at=base + timedelta(hours=3),
            status=BOOKING_STATUS_CANCELLED,
            price_snapshot=1000,
        ),
    ]
    session.add_all(bookings)
    await session.flush()

    revenue = await get_revenue(
        session,
        master.id,
        starts_from=base - timedelta(days=1),
        ends_before=base + timedelta(days=1),
    )
    assert revenue == 1000

    window = await list_bookings_in_window(
        session,
        master.id,
        base - timedelta(hours=1),
        base + timedelta(hours=4),
        only_active=True,
    )
    assert len(window) == 1


async def test_find_clients_to_return(session: AsyncSession) -> None:
    master = await upsert_master_from_tg(
        session,
        tg_user_id=40,
        tg_chat_id=40,
        tg_username="m4",
        display_name_hint="M4",
        default_timezone="UTC",
        default_work_start_minutes=600,
        default_work_end_minutes=1200,
        default_slot_step_minutes=30,
    )
    recent = Client(
        master_id=master.id,
        name="Recent",
        last_visit_at=datetime.utcnow() - timedelta(days=10),
    )
    stale = Client(
        master_id=master.id,
        name="Stale",
        last_visit_at=datetime.utcnow() - timedelta(days=60),
    )
    never = Client(master_id=master.id, name="Never")
    session.add_all([recent, stale, never])
    await session.flush()

    out = await find_clients_to_return(session, master.id, threshold_days=30)
    names = [c.name for c in out]
    assert names == ["Stale"]
