"""People, their login accounts and their web sessions."""

import uuid
from datetime import date, datetime
from typing import Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, event, func, text
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

# Where a person stands towards the house, how their record stands, and who opened it.
Standing = Literal["lives_here", "visiting", "elsewhere"]
State = Literal["known", "has_access", "withdrawn", "refused", "gone"]
Origin = Literal["setup", "invitation", "domo"]


def _child_by_role(context: DefaultExecutionContext) -> bool:
    """The mark of a record made with the built-in child role, when nothing else is said."""
    parameters = context.get_current_parameters()
    return parameters["role"] == "child"


class Person(Base):
    """Someone the assistant knows: the owner, someone of the household, or a record he keeps
    without an access or a login, which he opens and keeps himself.

    ``role`` is the built-in base: ``owner``, ``adult``, ``child`` or ``guest``; a role the
    assistant made for the household is ``made_role_id``, on that base. A person who is removed
    is not deleted (their history and the audit trail keep pointing at them): ``disabled_at``
    records when they were removed. ``expires_at`` is the end of a stay, when it has one.

    A record is a person, with or without a login: ``standing`` says where they stand towards the
    house, ``is_child`` is the mark that keeps the protections of a child (the built-in child role
    always carries it), ``state`` where their record stands (``disabled_at`` is the leaving date
    of a ``gone`` or ``withdrawn`` one) and ``origin`` who opened it (the assistant always keeps
    his reason). ``role`` is the base their rights start from.
    """

    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'adult', 'child', 'guest')", name="role_values"),
        CheckConstraint(
            "calls_domo IS NULL OR char_length(calls_domo) BETWEEN 1 AND 40",
            name="calls_domo_length",
        ),
        CheckConstraint(
            "standing IN ('lives_here', 'visiting', 'elsewhere')", name="standing_values"
        ),
        CheckConstraint("role <> 'child' OR is_child", name="child_role_is_marked"),
        CheckConstraint(
            "state IN ('known', 'has_access', 'withdrawn', 'refused', 'gone')",
            name="state_values",
        ),
        CheckConstraint(
            "state NOT IN ('gone', 'withdrawn') OR disabled_at IS NOT NULL",
            name="left_has_a_date",
        ),
        CheckConstraint(
            "state <> 'has_access' OR disabled_at IS NULL", name="access_is_not_disabled"
        ),
        CheckConstraint("origin IN ('setup', 'invitation', 'domo')", name="origin_values"),
        CheckConstraint(
            "char_length(origin_reason) BETWEEN 1 AND 300", name="origin_reason_length"
        ),
        CheckConstraint(
            "origin <> 'domo' OR origin_reason IS NOT NULL", name="domo_keeps_his_reason"
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
    standing: Mapped[Standing] = mapped_column(server_default=text("'lives_here'"))
    # A record made as a child carries the mark, unless it says otherwise.
    is_child: Mapped[bool] = mapped_column(
        default=_child_by_role,
        server_default=text("false"),
    )
    # What lifts the child mark by age.
    born_on: Mapped[date | None]
    state: Mapped[State] = mapped_column(default="has_access", server_default=text("'has_access'"))
    origin: Mapped[Origin] = mapped_column(server_default=text("'invitation'"))
    origin_reason: Mapped[str | None]
    # Set the first time access is given.
    first_access_at: Mapped[datetime | None]
    # Whether a guest's access wakes at the next visit.
    access_returns: Mapped[bool] = mapped_column(server_default=text("false"))
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    forgotten_at: Mapped[datetime | None]
    # The role the assistant made that the record carries (``people.models.MadeRole``).
    made_role_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("roles.id"))


@event.listens_for(Person, "init")
def _has_access_unless_said(target: Person, args: object, kwargs: dict[str, object]) -> None:
    """A record made in memory has access unless it says otherwise, as its column does once
    stored: ``is_active`` reads ``state`` before any flush."""
    kwargs.setdefault("state", "has_access")


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
