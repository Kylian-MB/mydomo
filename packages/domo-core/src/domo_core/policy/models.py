"""The owner's choices about what Domo may do: settings, and rights."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class PolicySetting(Base):
    """One choice of the owner, by key. The default values live in the code: a row exists only
    for a choice the owner has made, and no row means "the default"."""

    __tablename__ = "policy_settings"
    __table_args__ = (CheckConstraint("char_length(key) <= 200", name="key_length"),)

    key: Mapped[str] = mapped_column(primary_key=True)
    # Any JSON value; the policy layer checks it against what the key accepts.
    value: Mapped[object] = mapped_column(JSONB)
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), onupdate=func.clock_timestamp()
    )


# A right has one subject at most: a person, a role, or Domo (``for_domo``).
AT_MOST_ONE_SUBJECT = (
    "NOT (person_id IS NOT NULL AND role IS NOT NULL) "
    "AND NOT (for_domo AND (person_id IS NOT NULL OR role IS NOT NULL))"
)
# A right with no subject is a rule for everyone: it says what to ask, and nothing else.
EVERYONE_ONLY_ASKS = (
    "person_id IS NOT NULL OR role IS NOT NULL OR for_domo OR (access IS NULL AND approver IS "
    "NULL AND limit_expression IS NULL)"
)


class PolicyRight(Base):
    """One right of the owner (spec 5.1 and 5.3): about an action type (``action_type``) and a
    subject, a person (``person_id``), a role (``role``) or Domo himself (``for_domo``, the
    owner, 2026-11-18: what he does on his own for the house), one at most; with none, it is a rule
    for everyone, which says what to ask and nothing else (``everyone_only_asks``).

    What it says, each part optional (``None``: the right says nothing of it, and the role's
    right or the catalog does): whether the subject may do it (``access``, ``allow`` or
    ``deny``), whose agreement is needed first (``approver``: ``self``, ``adult`` or ``owner``), a
    limit
    (``limit_expression``, a CEL expression, with the ``judge_question`` it may need), and what
    Domo must ask before the action (``ask``: ``yes``, ``explicit_yes`` or ``proof``), or the
    owner's "never ask me" (``ask``: ``judge``, with his conditions in his words in ``ask_when``,
    or ``never``: not at all). ``summary`` says the right in plain words, as the owner approved it;
    ``owner_words`` are the owner's own words, as Domo passed them on; ``draft_id`` is the draft
    the right came from (``RightsDraft``), if any.

    A right is never deleted: replaced or taken away, it gets ``revoked_at`` (the audit log and
    the history point at it). A person's right is paused when they leave the household, and again if
    they come back to it (``paused_at``, spec 5.7 and 5.12): it no longer counts, until the owner
    gives it back (once the person is back) or takes it away. A rule for everyone is nobody's: it
    is never paused. Only one right that is not revoked exists for a subject and an action type
    (a paused one included; one rule for everyone per action type).

    The defaults are in the code (``policy.catalog``): no row means the default.
    """

    __tablename__ = "policy_rights"
    __table_args__ = (
        CheckConstraint(AT_MOST_ONE_SUBJECT, name="at_most_one_subject"),
        # A rule for everyone says what to ask, and nothing else.
        CheckConstraint(EVERYONE_ONLY_ASKS, name="everyone_only_asks"),
        CheckConstraint("role IN ('owner', 'adult', 'child', 'guest')", name="role_values"),
        CheckConstraint("char_length(action_type) <= 100", name="action_type_length"),
        CheckConstraint("access IN ('allow', 'deny')", name="access_values"),
        CheckConstraint("approver IN ('self', 'adult', 'owner')", name="approver_values"),
        CheckConstraint("char_length(limit_expression) <= 2000", name="limit_expression_length"),
        CheckConstraint("char_length(judge_question) <= 300", name="judge_question_length"),
        # A question to judge only goes with a limit (it is asked when the limit says "judge").
        CheckConstraint(
            "judge_question IS NULL OR limit_expression IS NOT NULL", name="judge_needs_a_limit"
        ),
        CheckConstraint(
            "ask IN ('judge', 'never', 'yes', 'explicit_yes', 'proof')", name="ask_values"
        ),
        CheckConstraint("char_length(ask_when) <= 300", name="ask_when_length"),
        # The owner's conditions go with "judge": leave it to Domo's judge, with these. (Not
        # "ask = 'judge'": a check passes on null, and a null ask would let conditions in.)
        CheckConstraint(
            "ask_when IS NULL OR ask IS NOT DISTINCT FROM 'judge'", name="ask_when_needs_judge"
        ),
        CheckConstraint("char_length(summary) BETWEEN 1 AND 300", name="summary_length"),
        CheckConstraint("char_length(owner_words) <= 1000", name="owner_words_length"),
        # One right in force per subject and action type: a new one replaces it (the old one is
        # revoked first). The subject is one of the two columns, the other is null (both, for a
        # rule for everyone or a right of Domo's, which ``for_domo`` tells apart): nulls count
        # as equal here.
        Index(
            "uq_policy_rights_subject_action_type",
            "person_id",
            "role",
            "for_domo",
            "action_type",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    role: Mapped[str | None]
    for_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    action_type: Mapped[str]
    access: Mapped[str | None]
    approver: Mapped[str | None]
    limit_expression: Mapped[str | None]
    judge_question: Mapped[str | None]
    ask: Mapped[str | None]
    ask_when: Mapped[str | None]
    summary: Mapped[str]
    owner_words: Mapped[str]
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policy_rights_drafts.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    revoked_at: Mapped[datetime | None]
    paused_at: Mapped[datetime | None]


class RightsDraft(Base):
    """A draft of rights (spec 5.6): what the rights writer made of the owner's words, once the
    code checked it on its examples. It is stored as it was checked and never changes: what the
    owner says yes to is exactly what is applied.

    ``created_by`` is the owner who asked; ``owner_words`` their words, as Domo passed them on;
    ``content`` the changes and the examples with the outcome the code worked out, the names of
    the people they speak of at that moment, and the rights in force then
    (``policy.rights_writer.draft_as_json``). ``applied_at`` is set when the owner's yes applied
    it: a draft is applied once.
    """

    __tablename__ = "policy_rights_drafts"
    __table_args__ = (
        CheckConstraint("char_length(owner_words) BETWEEN 1 AND 1000", name="owner_words_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    owner_words: Mapped[str]
    content: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    applied_at: Mapped[datetime | None]
