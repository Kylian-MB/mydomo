"""What Domo spends on the models: the calls, the limits, the real spending and the rates."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class ModelCall(Base):
    """One call to a model, counted from the moment it is reserved (``reserved_usd`` is its
    maximum cost) until it is settled with what it really cost (``cost_usd``).

    ``estimated`` says the tokens are an estimate; ``price_unknown`` says the cost was counted at
    a fall-back price. The key is kept as long as it exists (``ON DELETE SET NULL``). A call made
    to build a tool of Domo's (``toolmaking``, ``tool_review``) names its build (``build_id``)
    and the tool, once there is one (``tool_id``): what a tool cost to make
    (``madetools.launches.figures``); both are forgotten with their row (``ON DELETE SET NULL``).
    A call of sound (``hear``, ``say``, ``voice_print``) counts the seconds it heard
    (``heard_seconds``) and the characters it said (``said_chars``), at home too, where it costs
    nothing.
    """

    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint(
            "task IN ('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
            "'writing', 'checking', 'arrival', 'memory', 'toolmaking', 'tool_review', "
            "'hear', 'say', 'voice_print', 'attention', 'heard_sorting')",
            name="task_values",
        ),
        CheckConstraint("status IN ('reserved', 'settled')", name="status_values"),
        Index("ix_model_calls_provider_id_started_at", "provider_id", "started_at"),
        Index("ix_model_calls_tool_id", "tool_id"),
        Index("ix_model_calls_build_id", "build_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"))
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    task: Mapped[str]
    model_id: Mapped[str]
    status: Mapped[str]
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    # A call of sound counts seconds heard and characters said, where a chat call counts tokens.
    heard_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 2), server_default=text("0"))
    said_chars: Mapped[int] = mapped_column(server_default=text("0"))
    estimated: Mapped[bool] = mapped_column(server_default=text("false"))
    price_unknown: Mapped[bool] = mapped_column(server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    settled_at: Mapped[datetime | None]
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tools.id", ondelete="SET NULL")
    )
    build_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_builds.id", ondelete="SET NULL")
    )


class SpendingLimit(Base):
    """A limit on what a key may spend, for everyone, for one person (``person_id``) or for a
    role (``role``, with ``split``: ``each`` of them has the amount, or ``together`` they share it;
    it holds for whoever has the role at the time of the call).

    ``warn_at_percent`` is the threshold of whoever set the limit, and ``warned_at`` when they
    were told; ``domo_warned_at`` is the same for the threshold Domo gave himself.
    """

    __tablename__ = "spending_limits"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency IN ('USD', 'EUR')", name="currency_values"),
        CheckConstraint("period IN ('day', 'week', 'month')", name="period_values"),
        CheckConstraint("warn_at_percent BETWEEN 1 AND 100", name="warn_at_percent_range"),
        CheckConstraint("role IN ('adult', 'child', 'guest')", name="role_values"),
        CheckConstraint("split IN ('each', 'together')", name="split_values"),
        CheckConstraint("(role IS NULL) = (split IS NULL)", name="role_matches_split"),
        CheckConstraint("role IS NULL OR person_id IS NULL", name="role_or_person"),
        UniqueConstraint(
            "provider_id",
            "person_id",
            "role",
            "split",
            name="uq_spending_limits_provider_whom",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    role: Mapped[str | None]
    split: Mapped[str | None]
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    currency: Mapped[str]
    period: Mapped[str]
    warn_at_percent: Mapped[int | None]
    set_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    set_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    warned_at: Mapped[datetime | None]
    domo_warned_at: Mapped[datetime | None]


class KeyNewcomer(Base):
    """Someone whom the giver of a key has been told began using it (once, for good): it goes with
    the key (``ON DELETE CASCADE``)."""

    __tablename__ = "key_newcomers"

    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"), primary_key=True)
    told_at: Mapped[datetime]


class RealSpend(Base):
    """What the provider says a key really spent on a day (read with the admin key)."""

    __tablename__ = "real_spend"

    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(primary_key=True)
    usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class ExchangeRate(Base):
    """What a unit of a currency is worth in dollars."""

    __tablename__ = "exchange_rates"
    __table_args__ = (
        CheckConstraint("currency IN ('EUR')", name="currency_values"),
        CheckConstraint("usd_per_unit > 0", name="usd_per_unit_positive"),
    )

    currency: Mapped[str] = mapped_column(primary_key=True)
    usd_per_unit: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class CatalogSnapshot(Base):
    """The catalog of models and prices, as it was read at ``fetched_at``."""

    __tablename__ = "catalog_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    data: Mapped[dict[str, object]] = mapped_column(JSONB)
