"""Pure utilities for computing available booking slots.

Kept free of database dependencies so it can be tested in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class TimeRange:
    starts_at: datetime
    ends_at: datetime

    def overlaps(self, other: TimeRange) -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


def _minutes_to_time(minutes: int) -> time:
    minutes = max(0, min(24 * 60 - 1, minutes))
    return time(hour=minutes // 60, minute=minutes % 60)


def generate_day_slots(
    *,
    day: date,
    work_start_minutes: int,
    work_end_minutes: int,
    slot_step_minutes: int,
    service_duration_minutes: int,
    booked: Iterable[TimeRange] = (),
    now: datetime | None = None,
) -> list[datetime]:
    """Return naive `datetime`s for the start of each free slot on `day`.

    All times are treated as belonging to the same timezone. We expect callers
    to convert to/from UTC at the boundary; this function only does math.
    """

    if slot_step_minutes <= 0:
        raise ValueError("slot_step_minutes must be > 0")
    if service_duration_minutes <= 0:
        raise ValueError("service_duration_minutes must be > 0")
    if work_end_minutes <= work_start_minutes:
        return []

    booked_list = list(booked)

    last_start = work_end_minutes - service_duration_minutes
    if last_start < work_start_minutes:
        return []

    slots: list[datetime] = []
    minute = work_start_minutes
    while minute <= last_start:
        start_dt = datetime.combine(day, _minutes_to_time(minute))
        end_dt = start_dt + timedelta(minutes=service_duration_minutes)
        candidate = TimeRange(starts_at=start_dt, ends_at=end_dt)

        if now is not None and start_dt <= now:
            minute += slot_step_minutes
            continue

        if not any(candidate.overlaps(b) for b in booked_list):
            slots.append(start_dt)

        minute += slot_step_minutes

    return slots
