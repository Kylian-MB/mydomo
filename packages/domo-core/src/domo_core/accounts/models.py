"""People, their login accounts and their web sessions."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class Person(Base):
    """Someone Domo talks to: the owner, or a member of the household they invited.

    ``role`` is ``owner``, ``adult``, ``child`` or ``guest``. A person who is removed is not
    deleted (their history and the audit trail keep pointing at them): ``disabled_at`` records
    when they were removed. ``expires_at`` is the end of a guest's stay.
    """

    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'adult', 'child', 'guest')", name="role_values"),
        CheckConstraint(
            "calls_domo IS NULL OR char_length(calls_domo) BETWEEN 1 AND 40",
            name="calls_domo_length",
        ),
        # At most one owner, whatever the code does (two concurrent setups, a bug): the unique
        # index only covers the rows whose role is "owner", so other roles are not limited.
        Index(
            "uq_persons_single_owner",
            "role",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    display_name: Mapped[str]
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    disabled_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    # Who invited this person (null for the owner).
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    # The language the person reads (``None``: not seen yet), the note that tells Domo how they
    # like to be written to (``None``: none), and when their page was last opened.
    language: Mapped[str | None]
    writing_note: Mapped[str | None]
    last_visit_at: Mapped[datetime | None]
    # When something new last came for the person that no turn of Domo said yet (a note left for
    # them, a request of theirs someone else decided, a newcomer on a key they gave): ``None`` once
    # one did (``conversation.visits.news_for``).
    news_at: Mapped[datetime | None]
    # Whether Domo owes the person one last review once they are gone (``memory.review``): set
    # where they are removed and where a guest's stay is given (it ends by itself), cleared once
    # that review is over.
    last_review_due: Mapped[bool] = mapped_column(server_default=text("false"))
    # The name the person calls Domo by ("Brenda", ``tools.profile.DomoName``): shown to them
    # alone, where the code writes "Domo" itself. ``None``: they see the house's name for him
    # (``policy.settings``), else "Domo" (``naming.name_shown``).
    calls_domo: Mapped[str | None]


class Account(Base):
    """A person's login: username and password hash (never the password)."""

    __tablename__ = "accounts"
    __table_args__ = (
        # Logins are case-insensitive: the username is stored in lower case (and unique as such).
        CheckConstraint("username = lower(username)", name="username_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"), unique=True)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class WebSession(Base):
    """A browser session. Only the hash of its token is stored: a database leak does not leak
    working cookies."""

    __tablename__ = "web_sessions"
    __table_args__ = (
        # "The sessions of this account" (removing someone ends them all).
        Index("ix_web_sessions_account_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
