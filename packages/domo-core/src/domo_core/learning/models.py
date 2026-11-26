"""The table of what went wrong (``went_wrong``).

A row is a fact noted by code about a mistake: a request a person refused or let expire, a check
the judge refused, a warning declined, an act undone, a complaint, a verdict that proved wrong, a
request that asked for nothing, a failure, a build abandoned, a launch refused (``Kind``). It
holds no judgement of anyone: ``by_person_id`` is who refused, undid or complained, ``by_domo``
says the judge or the assistant himself did, both empty is the code (an expiry, a failure).
``of_domo`` says the thing that went wrong was his idea, act, request or verdict;
``about_person_id`` whose request or turn it was. ``subject_key`` is what the action was about
(``policy.subjects``), ``reason`` a short plain line (300 characters at most, never a secret) and
``asked_why_at`` when someone was asked why (never twice).

A person's deletion takes the facts about them and only empties the place of whoever else acted.
"""

import uuid
from datetime import datetime
from typing import Final, Literal, get_args

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

Kind = Literal[
    "refused",
    "expired",
    "judge_refused",
    "warning_declined",
    "undone",
    "complaint",
    "wrong_verdict",
    "asked_for_nothing",
    "failed",
    "build_abandoned",
    "launch_refused",
]
KINDS: Final = get_args(Kind)

# The longest reason kept.
MAX_REASON_LENGTH: Final = 300


class WentWrong(Base):
    """One fact of something that went wrong (see the module's text)."""

    __tablename__ = "went_wrong"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in KINDS) + ")", name="kind_values"
        ),
        CheckConstraint(f"char_length(reason) <= {MAX_REASON_LENGTH}", name="reason_length"),
        Index("ix_went_wrong_action_type_at", "action_type", text("at DESC")),
        Index("ix_went_wrong_subject_key_at", "subject_key", text("at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    at: Mapped[datetime]
    kind: Mapped[str]
    action_type: Mapped[str]
    subject_key: Mapped[str | None]
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_actions.id", ondelete="SET NULL")
    )
    confirmation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("confirmations.id", ondelete="SET NULL")
    )
    by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    by_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    of_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    about_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    reason: Mapped[str | None]
    asked_why_at: Mapped[datetime | None]
