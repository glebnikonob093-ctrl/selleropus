from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---- Booking statuses ----
BOOKING_STATUS_NEW = "new"
BOOKING_STATUS_CONFIRMED = "confirmed"
BOOKING_STATUS_CAME = "came"
BOOKING_STATUS_CANCELLED = "cancelled"
BOOKING_STATUS_NO_SHOW = "no_show"

BOOKING_STATUSES = (
    BOOKING_STATUS_NEW,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_CAME,
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_NO_SHOW,
)

ACTIVE_BOOKING_STATUSES = (
    BOOKING_STATUS_NEW,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_CAME,
)


class Master(Base):
    """A self-employed user (the bot's customer). Owns services, clients and bookings."""

    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    display_name: Mapped[str] = mapped_column(String(120), default="")
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    language: Mapped[str] = mapped_column(String(8), default="ru")

    work_start_minutes: Mapped[int] = mapped_column(Integer, default=10 * 60)  # 10:00
    work_end_minutes: Mapped[int] = mapped_column(Integer, default=20 * 60)  # 20:00
    slot_step_minutes: Mapped[int] = mapped_column(Integer, default=30)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    services: Mapped[list[Service]] = relationship(
        back_populates="master", cascade="all, delete-orphan"
    )
    clients: Mapped[list[Client]] = relationship(
        back_populates="master", cascade="all, delete-orphan"
    )
    bookings: Mapped[list[Booking]] = relationship(
        back_populates="master", cascade="all, delete-orphan"
    )


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), index=True)

    name: Mapped[str] = mapped_column(String(120))
    price: Mapped[int] = mapped_column(Integer, default=0)  # in rubles, integer
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    master: Mapped[Master] = relationship(back_populates="services")
    bookings: Mapped[list[Booking]] = relationship(back_populates="service")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), index=True)

    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_visit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    master: Mapped[Master] = relationship(back_populates="clients")
    bookings: Mapped[list[Booking]] = relationship(back_populates="client")

    __table_args__ = (
        UniqueConstraint("master_id", "tg_user_id", name="uq_client_master_tg"),
    )


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)

    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # naive UTC
    ends_at: Mapped[datetime] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(String(20), default=BOOKING_STATUS_NEW, index=True)
    source: Mapped[str] = mapped_column(String(20), default="master")  # master | public | bot

    price_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    master: Mapped[Master] = relationship(back_populates="bookings")
    client: Mapped[Client] = relationship(back_populates="bookings")
    service: Mapped[Service] = relationship(back_populates="bookings")

    reminders: Mapped[list[ReminderState]] = relationship(
        back_populates="booking", cascade="all, delete-orphan"
    )


class ReminderState(Base):
    """One row per (booking, kind) marking when we sent that reminder.

    Used as an idempotency record so the scheduler does not double-send.
    """

    __tablename__ = "reminder_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # client_24h | client_2h | master_morning
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    booking: Mapped[Booking] = relationship(back_populates="reminders")

    __table_args__ = (
        UniqueConstraint("booking_id", "kind", name="uq_reminder_booking_kind"),
    )


class MasterDailySummary(Base):
    """One row per (master, calendar day) marking the morning summary as sent.

    Keyed on the master and the day rather than a booking, so a master with no
    bookings is still marked once per day and greeted exactly once.
    """

    __tablename__ = "master_daily_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("master_id", "day", name="uq_master_daily_summary"),
    )


class MasterBot(Base):
    """A per-master Telegram bot that clients use to book appointments.

    Each master can optionally connect their own Telegram bot (created via
    BotFather). The platform runs polling for all active master bots.
    """

    __tablename__ = "master_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), unique=True, index=True)
    bot_token: Mapped[str] = mapped_column(String(100))
    bot_username: Mapped[str] = mapped_column(String(64), default="")
    bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    master: Mapped[Master] = relationship()


class BlockedClient(Base):
    """A client blocked by a master. Blocked clients cannot book via master bot."""

    __tablename__ = "blocked_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), index=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("master_id", "tg_user_id", name="uq_blocked_client_master_tg"),
    )


class TeamMember(Base):
    """A team member added by a master. Receives booking notifications."""

    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), index=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("master_id", "tg_user_id", name="uq_team_member_master_tg"),
    )
