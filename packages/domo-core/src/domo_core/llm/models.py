"""The keys of the model providers, and what Domo keeps about their models."""

import secrets
import uuid
from datetime import datetime
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


def new_provider_id() -> str:
    """A fresh provider id: ``p-`` and 8 hexadecimal characters.

    It is short because it appears in secret references (``provider:<id>:api_key``) and in URLs;
    the application generates it (the primary key rejects the odd collision).
    """
    return "p-" + secrets.token_hex(4)


class Provider(Base):
    """A key: which provider, where it is, which secret unlocks it. Its models are listed apart
    (``ProviderModel``), and each call chooses one (``llm.router``).

    The key itself is never here: ``secret_ref`` names it in the gatekeeper's vault (null for a
    provider that needs none, such as a local server).

    ``given_by`` is the person who gave the key; ``is_general`` marks the one key of the whole
    household (at most one);
    ``admin_secret_ref`` names the admin key in the vault, which reads the real spending;
    ``spend_factor`` corrects the estimated cost to the real one.
    """

    __tablename__ = "providers"
    __table_args__ = (
        CheckConstraint("id ~ '^p-[0-9a-f]{8}$'", name="id_format"),
        Index(
            "uq_providers_one_general",
            "is_general",
            unique=True,
            postgresql_where=text("is_general"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_provider_id)
    kind: Mapped[str]
    base_url: Mapped[str]
    secret_ref: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    given_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    is_general: Mapped[bool] = mapped_column(server_default=text("false"))
    admin_secret_ref: Mapped[str | None]
    spend_factor: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))


class ProviderModel(Base):
    """A model a key lists (read from the provider, ``listed_at`` is when)."""

    __tablename__ = "provider_models"
    __table_args__ = (CheckConstraint("char_length(model_id) <= 200", name="model_id_length"),)

    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(primary_key=True)
    listed_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class ProviderUser(Base):
    """Who may use a key: the whole household, the invitees of the one who gave it, or a person.

    A line for a person is the only one with a ``person_id``.
    """

    __tablename__ = "provider_users"
    __table_args__ = (
        CheckConstraint("scope IN ('household', 'invitees', 'person')", name="scope_values"),
        CheckConstraint(
            "(scope = 'person') = (person_id IS NOT NULL)", name="person_matches_scope"
        ),
        UniqueConstraint(
            "provider_id",
            "scope",
            "person_id",
            name="uq_provider_users_provider_scope_person",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    scope: Mapped[str]
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    allowed: Mapped[bool]
    set_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    set_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class ModelLevel(Base):
    """The level the owner says a model is (it beats the project's table)."""

    __tablename__ = "model_levels"
    __table_args__ = (
        CheckConstraint("level IN ('small', 'medium', 'strong')", name="level_values"),
        CheckConstraint("char_length(model_id) <= 200", name="model_id_length"),
    )

    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(primary_key=True)
    level: Mapped[str]
    set_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    set_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class ModelChoices(Base):
    """What Domo chose about models, by scope: ``household`` or ``provider:<id>``."""

    __tablename__ = "model_choices"
    __table_args__ = (CheckConstraint("char_length(why) <= 200", name="why_length"),)

    scope: Mapped[str] = mapped_column(primary_key=True)
    choices: Mapped[dict[str, object]] = mapped_column(JSONB)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    changed_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    why: Mapped[str | None]


class ModelLesson(Base):
    """What Domo learned about a model and a kind of task: how often it failed."""

    __tablename__ = "model_lessons"
    __table_args__ = (
        CheckConstraint("char_length(last_reason) <= 200", name="last_reason_length"),
    )

    provider_kind: Mapped[str] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(primary_key=True)
    failures: Mapped[int] = mapped_column(server_default=text("0"))
    last_failure_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    last_reason: Mapped[str]
