"""The tables of the people of the house.

A person is a row of ``persons`` (``accounts.models.Person``); everything about people that is not
a column of that row lives here, and every column naming a person points at ``persons.id``.

* ``roles`` and ``role_accesses``: the roles the assistant makes (a name, the built-in base whose
  rights they start from, a reason) and what each gives or denies. A made role never takes the
  name of a built-in one.
* ``person_links``: "``from`` is ``relation`` ``to``" (Dana, "mother of", Alex), who said it, and
  when someone confirmed it.
* ``access_promises``: access the assistant promised to someone who has no record yet, kept (or
  dropped) later.
* ``owner_directives``: what the owner ruled, which the code applies before any judgement.
* ``record_merges``: two records made one, with the ids of what moved, so that it can be undone.
* ``person_acts``: the reason of each act on a person, and who did it. The audit log keeps only
  the id of such a row.
* ``person_channel_refs``: an opaque reference a channel owns for a person, never what it is made
  of.

An author (``created_by``, ``by``, ``asked_by``, ``dropped_by``) is the text ``domo`` or
``person:<uuid>``, as ``policy.roles.actor_label`` writes it.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

BaseRole = Literal["adult", "child", "guest"]
Access = Literal["allow", "deny"]
DirectiveKind = Literal["no_new_people", "no_keeping", "withdraw", "refuse"]
Act = Literal[
    "opened",
    "completed",
    "erased",
    "forgotten",
    "linked",
    "unlinked",
    "merged",
    "split",
    "role_created",
    "role_changed",
    "role_deleted",
    "access_given",
    "access_removed",
    "access_mistake",
    "promised",
    "promise_kept",
    "promise_dropped",
    "directive_set",
    "directive_lifted",
    "exception_used",
    "invited",
    "link_sent",
    "joining_stopped",
    "invitation_cancelled",
]

_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def author_rule(column: str) -> str:
    """The check that ``column`` says ``domo`` or ``person:<uuid>``."""
    return f"{column} = 'domo' OR {column} ~ '^person:{_UUID}$'"


class MadeRole(Base):
    """A role the assistant made: ``base`` is the built-in role whose rights it starts from.
    Its ``name_key`` is its name as it is compared. A deleted role is kept, marked."""

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 40", name="name_length"),
        CheckConstraint("base IN ('adult', 'child', 'guest')", name="base_values"),
        CheckConstraint("name_key NOT IN ('owner', 'adult', 'child', 'guest')", name="name_free"),
        CheckConstraint(author_rule("created_by"), name="created_by_format"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 300", name="reason_length"),
        Index(
            "uq_roles_name_key",
            "name_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str]
    name_key: Mapped[str]
    base: Mapped[BaseRole]
    created_by: Mapped[str]
    reason: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    deleted_at: Mapped[datetime | None]


class RoleAccess(Base):
    """What a made role gives (``allow``) or denies on purpose (``deny``) for an action type. A
    line the owner said for the role (``by_owner``) is his rule: only his own request changes it,
    and it may carry a limit (``limit_expression``, with ``judge_question``), an approver, what to
    ask before (``ask``) and its words (``summary``), for whoever has the role, now and later."""

    __tablename__ = "role_accesses"
    __table_args__ = (
        CheckConstraint("char_length(action_type) BETWEEN 1 AND 100", name="action_type_length"),
        CheckConstraint("access IN ('allow', 'deny')", name="access_values"),
        CheckConstraint("approver IN ('self', 'adult', 'owner')", name="approver_values"),
        CheckConstraint("ask IN ('yes', 'explicit_yes', 'proof')", name="ask_values"),
        CheckConstraint("char_length(limit_expression) <= 2000", name="limit_expression_length"),
        CheckConstraint("char_length(judge_question) <= 300", name="judge_question_length"),
        CheckConstraint(
            "judge_question IS NULL OR limit_expression IS NOT NULL", name="judge_needs_a_limit"
        ),
        CheckConstraint("char_length(summary) <= 300", name="summary_length"),
        CheckConstraint(
            "by_owner OR (limit_expression IS NULL AND approver IS NULL AND ask IS NULL)",
            name="more_is_the_owner_s",
        ),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    action_type: Mapped[str] = mapped_column(primary_key=True)
    access: Mapped[Access]
    by_owner: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    limit_expression: Mapped[str | None]
    judge_question: Mapped[str | None]
    approver: Mapped[str | None]
    ask: Mapped[str | None]
    summary: Mapped[str | None]


class PersonLink(Base):
    """``from`` is ``relation`` ``to``: from Dana, "mother of", to Alex."""

    __tablename__ = "person_links"
    __table_args__ = (
        CheckConstraint("from_id <> to_id", name="two_records"),
        CheckConstraint("char_length(relation) BETWEEN 1 AND 60", name="relation_length"),
        Index("uq_person_links_told_once", "from_id", "to_id", "relation", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    from_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    to_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    relation: Mapped[str]
    said_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class AccessPromise(Base):
    """Access promised to someone before their record could have it: what it is, the role and
    where they stand, who asked, whether it was the owner's order, and why. It is kept (to a
    person) or dropped (by someone), once."""

    __tablename__ = "access_promises"
    __table_args__ = (
        CheckConstraint("char_length(description) BETWEEN 1 AND 300", name="description_length"),
        CheckConstraint("role IN ('adult', 'child', 'guest')", name="role_values"),
        CheckConstraint(
            "standing IN ('lives_here', 'visiting', 'elsewhere')", name="standing_values"
        ),
        CheckConstraint("guest_days IS NULL OR guest_days > 0", name="guest_days_positive"),
        CheckConstraint(author_rule("asked_by"), name="asked_by_format"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 300", name="reason_length"),
        CheckConstraint(
            f"dropped_by IS NULL OR {author_rule('dropped_by')}", name="dropped_by_format"
        ),
        CheckConstraint("(dropped_at IS NULL) = (dropped_by IS NULL)", name="dropped_by_whom"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    description: Mapped[str]
    role: Mapped[BaseRole]
    made_role_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("roles.id"))
    standing: Mapped[str]
    guest_days: Mapped[int | None]
    asked_by: Mapped[str]
    is_order: Mapped[bool]
    reason: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    kept_at: Mapped[datetime | None]
    kept_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    dropped_at: Mapped[datetime | None]
    dropped_by: Mapped[str | None]


class OwnerDirective(Base):
    """What the owner ruled: no new people, nobody kept, someone withdrawn or refused
    (``person_id`` says who: only for those two kinds). In force until ``lifted_at``, or
    ``until`` when set; one in force per kind and person."""

    __tablename__ = "owner_directives"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('no_new_people', 'no_keeping', 'withdraw', 'refuse')", name="kind_values"
        ),
        CheckConstraint(
            "(kind IN ('withdraw', 'refuse')) = (person_id IS NOT NULL)", name="person_for_kind"
        ),
        CheckConstraint("char_length(description) <= 300", name="description_length"),
        CheckConstraint("char_length(except_words) <= 300", name="except_words_length"),
        Index(
            "uq_owner_directives_in_force",
            "kind",
            "person_id",
            unique=True,
            postgresql_where=text("lifted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    kind: Mapped[DirectiveKind]
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    description: Mapped[str | None]
    except_words: Mapped[str | None]
    until: Mapped[datetime | None]
    set_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    set_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    lifted_at: Mapped[datetime | None]
    lifted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))


class RecordMerge(Base):
    """Two records made one: ``absorbed_id`` into ``kept_id``. ``moved`` holds ids only (what
    moved, and the absorbed record's state before) so that the merge can be split until
    ``expires_at``."""

    __tablename__ = "record_merges"
    __table_args__ = (
        CheckConstraint("kept_id <> absorbed_id", name="two_records"),
        CheckConstraint(author_rule('"by"'), name="by_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    kept_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    absorbed_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    moved: Mapped[dict[str, Any]] = mapped_column(JSONB)
    by: Mapped[str]
    merged_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    expires_at: Mapped[datetime]
    split_at: Mapped[datetime | None]


class PersonAct(Base):
    """The trace of one act on a person: what (``act``), whom, with what reason, who did it
    (``by``) and in whose turn. ``owed_to_owner`` marks an act the owner must be told of;
    ``told_owner_at`` says when they were."""

    __tablename__ = "person_acts"
    __table_args__ = (
        CheckConstraint("char_length(act) BETWEEN 1 AND 40", name="act_length"),
        CheckConstraint("char_length(reason) <= 300", name="reason_length"),
        CheckConstraint(author_rule('"by"'), name="by_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    act: Mapped[Act]
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    other_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    role_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("roles.id", ondelete="SET NULL"))
    promise_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_promises.id", ondelete="SET NULL")
    )
    directive_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("owner_directives.id", ondelete="SET NULL")
    )
    # The invitation the act made (``invited``): someone new has no record before they join.
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invitations.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None]
    by: Mapped[str]
    turn_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"))
    at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    owed_to_owner: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    told_owner_at: Mapped[datetime | None]


class ChannelRef(Base):
    """An opaque reference a channel owns for a person (several per person and channel)."""

    __tablename__ = "person_channel_refs"
    __table_args__ = (
        CheckConstraint("char_length(channel) BETWEEN 1 AND 40", name="channel_length"),
        CheckConstraint("char_length(reference) BETWEEN 1 AND 200", name="reference_length"),
        Index("uq_person_channel_refs_reference", "channel", "reference", unique=True),
        Index("ix_person_channel_refs_person_id", "person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    channel: Mapped[str]
    reference: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
