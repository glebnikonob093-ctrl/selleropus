from __future__ import annotations

from datetime import date, datetime

from app.slots import TimeRange, generate_day_slots


def test_generates_evenly_spaced_slots() -> None:
    day = date(2026, 5, 10)
    slots = generate_day_slots(
        day=day,
        work_start_minutes=10 * 60,
        work_end_minutes=12 * 60,
        slot_step_minutes=30,
        service_duration_minutes=30,
    )
    assert slots == [
        datetime(2026, 5, 10, 10, 0),
        datetime(2026, 5, 10, 10, 30),
        datetime(2026, 5, 10, 11, 0),
        datetime(2026, 5, 10, 11, 30),
    ]


def test_excludes_overlapping_bookings() -> None:
    day = date(2026, 5, 10)
    booked = [
        TimeRange(
            starts_at=datetime(2026, 5, 10, 10, 30),
            ends_at=datetime(2026, 5, 10, 11, 30),
        )
    ]
    slots = generate_day_slots(
        day=day,
        work_start_minutes=10 * 60,
        work_end_minutes=12 * 60,
        slot_step_minutes=30,
        service_duration_minutes=30,
        booked=booked,
    )
    assert slots == [datetime(2026, 5, 10, 10, 0), datetime(2026, 5, 10, 11, 30)]


def test_skips_slots_in_the_past() -> None:
    day = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 11, 0)
    slots = generate_day_slots(
        day=day,
        work_start_minutes=10 * 60,
        work_end_minutes=13 * 60,
        slot_step_minutes=30,
        service_duration_minutes=30,
        now=now,
    )
    assert datetime(2026, 5, 10, 10, 0) not in slots
    assert datetime(2026, 5, 10, 11, 30) in slots


def test_returns_empty_when_service_longer_than_window() -> None:
    day = date(2026, 5, 10)
    slots = generate_day_slots(
        day=day,
        work_start_minutes=10 * 60,
        work_end_minutes=10 * 60 + 30,
        slot_step_minutes=30,
        service_duration_minutes=60,
    )
    assert slots == []


def test_respects_long_service_duration() -> None:
    day = date(2026, 5, 10)
    slots = generate_day_slots(
        day=day,
        work_start_minutes=10 * 60,
        work_end_minutes=12 * 60,
        slot_step_minutes=30,
        service_duration_minutes=90,
    )
    assert slots == [
        datetime(2026, 5, 10, 10, 0),
        datetime(2026, 5, 10, 10, 30),
    ]
