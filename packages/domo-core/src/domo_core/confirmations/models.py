"""Confirmations: what a person is asked to approve before Domo acts, and the steps of a chain
of actions that waits for one yes."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class Confirmation(Base):
    """A request that waits for a person's yes or no.

    It stores the action exactly as it was described when the barrier asked for approval
    (``tool_name``, ``arguments`` and the described ``action``): approving executes that, or
    ``amended_arguments`` when the person asked for a change, and never anything else. Once a
    change has run, ``action`` is the action the barrier allowed for it, and ``arguments`` still
    hold what was first asked (the card says what changed). A change that must be asked again
    makes a new request instead (``replaced_by``).

    ``approver_kind`` says who may decide: the requester themselves (``self``), an adult
    (``adult``, for a child's request) or the owner (``owner``, when a right of the owner says so),
    never the requester for the last two. ``ask`` is what the request asks for (``yes``, a yes in
    words or the button; ``explicit_yes``, a firm yes in their own words, read by its meaning, or
    the button; ``proof``, a proof that it is the person), ``asked_by`` who asked it (``rule``: a
    rule of the owner; ``judge``: the yes-judge; ``domo``: Domo himself; ``approver``:
    someone else's agreement the rights ask for, with a plain yes) and ``ask_why`` why, in one line
    (the judge's reason, or Domo's). All three are ``None`` for a chain the judge let through
    that keeps a request only for the upkeep to resume it (``decided_via`` ``judge``: nobody said
    yes). ``status`` follows the request: ``pending``, then
    ``executing`` while the tool runs, then ``executed`` or ``failed``; or ``refused``,
    ``expired`` or ``replaced`` (by the confirmation in ``replaced_by``; by none for a chain taken
    up again whose rest ran at once, ``tools.chain.ChainContinue``). A chain whose step waits for
    someone's arrival is ``waiting_arrival`` once it did its steps up to that one (spec 4.3b): it
    waits for the invitation in ``waiting_for_invitation_id``, and the upkeep resumes it
    (``executing`` again) once that is accepted, or stops it when it ends
    (``confirmations.arrivals``). ``decided_via`` is how the decision was recognised (a button, a
    phrase the code knows, the classifier, a change, a proof), or ``judge`` for a chain that left
    without a yes (the judge let it go).

    There is no expiry date: the ceiling is worked out at every read from ``created_at`` and
    the setting the owner has chosen, so that changing the setting also applies to the requests
    already waiting. ``expired_by`` tells whether Domo dropped the request (it no longer
    applied) or its lifetime ran out.

    ``ask_reason`` is what Domo could not tell when a limit sent the request to the owner (the
    right's question, spec 5.7): the owner reads it on the card.

    Someone else's yes (an adult's for a child, the owner's when a right says so) may be said in
    words too, in the approver's own conversation (spec 5.1): ``shown_to_approver_message_id`` is
    the approver's reply that last showed the request, apart from the one who asked's
    (``last_shown_message_id``). ``approver_note`` is what the approver said with a change, and
    how firm they were, in a few words of Domo, for the one who asked (data). The one who asked
    may nudge the approver of their request that waits for someone else's yes (spec 5.2):
    ``nudged_at`` is when they last did; the request then comes first in the approver's zone, and
    their Domo is told. Their own words never answer it.

    A chain of actions (``do_in_order``, spec 4) is one request: its ``action`` is the chain's
    (``chain.run``), its steps are rows of their own (``ConfirmationStep``), and ``chain_basis`` is
    what it remembers of the one who asked, their role and their own rights then
    (``policy.decision.ChainBasis.as_json``). It is set for a chain and for nothing else: that is
    how a request is known to be one.

    A request Domo made on his own (``by_domo``, Task 9: a turn a launch for the house woke
    him for) is his, not the person's whose conversation it is (``requester_id``, the owner, where
    it waits): it is judged again and run as his whenever it is decided, an adult's agreement is
    any adult's to give, the owner's included, the owner's the owner's, and nobody gives his own
    yes (``confirmations.store.requester_of``). In a turn of his own, he drops it or changes it
    himself (spec 12.5); a person answers it yes or no.

    What his history counts (the limits on his own acts read it: ``policy.facts``) is the
    ``allow`` lines of the barrier in his name, in the audit. A request of his, or a note in his
    name a person asked for (``DescribedAction.also_domo``), is counted once:
    ``counted_for_domo`` says that a line was written for it already, when it was made (the
    barrier let it through and only a yes was asked) or at the yes that let it through, however
    it was sent from one approver to another on the way.

    ``arguments``, ``action``, ``amended_arguments`` and ``result`` hold no secret, token or link.
    """

    __tablename__ = "confirmations"
    __table_args__ = (
        CheckConstraint("approver_kind IN ('self', 'adult', 'owner')", name="approver_kind_values"),
        CheckConstraint("ask IN ('yes', 'explicit_yes', 'proof')", name="ask_values"),
        CheckConstraint(
            "asked_by IN ('rule', 'judge', 'domo', 'approver')", name="asked_by_values"
        ),
        # The judge's reason, or Domo's (``confirmations.judge.MAX_JUDGE_REASON_LENGTH``).
        CheckConstraint("char_length(ask_why) <= 200", name="ask_why_length"),
        CheckConstraint(
            "status IN ('pending', 'executing', 'executed', 'refused', 'expired', 'failed', "
            "'replaced', 'waiting_arrival')",
            name="status_values",
        ),
        CheckConstraint(
            "decided_via IN ('button', 'explicit_phrase', 'phrase', 'classifier', 'change', "
            "'proof', 'judge')",
            name="decided_via_values",
        ),
        CheckConstraint("expired_by IN ('domo', 'lifetime')", name="expired_by_values"),
        # A right's question (``policy_rights.judge_question``) is 300 characters at most.
        CheckConstraint("char_length(ask_reason) <= 300", name="ask_reason_length"),
        # An approver's note (``confirmations.service.MAX_APPROVER_NOTE_LENGTH``).
        CheckConstraint("char_length(approver_note) <= 300", name="approver_note_length"),
        # The assistant's reason (``confirmations.judge.MAX_JUDGE_REASON_LENGTH``).
        CheckConstraint("char_length(idea_reason) <= 200", name="idea_reason_length"),
        # What a person said when they refused it.
        CheckConstraint("char_length(refusal_words) <= 300", name="refusal_words_length"),
        # "The requests of this person that are still waiting".
        Index("ix_confirmations_requester_id_status", "requester_id", "status"),
        # "The requests that wait in this conversation" (every turn of the chat).
        Index("ix_confirmations_conversation_id", "conversation_id"),
        # "The request this one replaced" (every card of a request, on the page and the stream).
        Index("ix_confirmations_replaced_by", "replaced_by"),
        # "The requests that wait for an adult, or the owner" (the approvals zone, every 15
        # seconds for each adult).
        Index("ix_confirmations_status_approver_kind", "status", "approver_kind"),
        # "What this person asked of this type" (the history the yes-judge reads, at every call).
        Index(
            "ix_confirmations_requester_id_action_type",
            "requester_id",
            text("(action->>'action_type')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    approver_kind: Mapped[str]
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    # The message of the conversation in which the request was made, when there is one.
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    # The reply that last showed the request to the person: the one that made it, or one that
    # presented it again. Only the person's answer to that reply can decide it in words (R74).
    last_shown_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    tool_name: Mapped[str]
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB)
    action: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    reminder_count: Mapped[int] = mapped_column(server_default=text("0"))
    last_reminded_at: Mapped[datetime | None]
    # What the request asks for, who asked it and why (``None`` for a chain let through).
    ask: Mapped[str | None]
    asked_by: Mapped[str | None]
    ask_why: Mapped[str | None]
    # What Domo could not tell when a limit asked the owner (the right's question).
    ask_reason: Mapped[str | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    decided_at: Mapped[datetime | None]
    decided_via: Mapped[str | None]
    decided_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    amended_arguments: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("confirmations.id"))
    expired_by: Mapped[str | None]
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    # What a chain remembers of the one who asked; ``None`` (SQL ``NULL``, never a JSON null) for
    # a request that is no chain.
    chain_basis: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    # The invitation a chain paused at a wait waits for (``waiting_arrival``): the one of its last
    # wait; ``None`` for a request that never paused.
    waiting_for_invitation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invitations.id")
    )
    # The reply of an approver (someone else than the one who asked) that last showed the request
    # in their own conversation: only their answer to that reply can decide it in words (R74).
    shown_to_approver_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id")
    )
    # What the approver said with a change, and how firm they were, for the one who asked.
    approver_note: Mapped[str | None]
    # Domo's own request, made in a turn he acted in on his own (see the class docstring).
    by_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    # His history counted it already: an ``allow`` of the barrier in his name was written for it,
    # when it was made or at the yes that let it through (see the class docstring).
    counted_for_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    # When the one who asked last nudged the approver of their request that waits for someone
    # else's yes: it comes first in the approver's zone, and their Domo is told (spec 5.2).
    nudged_at: Mapped[datetime | None]
    # The judge warned the one who asked (``warn``): the yes is asked only because of something
    # they must know first, which ``ask_why`` tells. Their answer to it is remembered as what
    # Domo told them (``memory.told``, spec 6.8).
    warned: Mapped[bool] = mapped_column(server_default=text("false"))
    # What the request is about (``policy.subjects.subject_of``); ``None``: not known.
    subject_key: Mapped[str | None]
    # Why the assistant asks, when it was his own idea; ``None`` for what a person asked.
    idea_reason: Mapped[str | None]
    # The person the thing concerns: they alone answer the request (``confirmations.service``,
    # ``may_decide``); ``None``: its approver kind says who answers.
    concerned_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    # What the person said when they refused it.
    refusal_words: Mapped[str | None]
    # When its expiry woke the assistant, who is told once.
    expiry_woken_at: Mapped[datetime | None]
    # A request of his (``by_domo``) born of the owner's rule on his act in a person's turn: the
    # owner answers it, and it is replayed in that turn, for that person, never on his own
    # (``store.replayed_on_his_own``).
    in_their_turn: Mapped[bool] = mapped_column(server_default=text("false"), default=False)


class ConfirmationStep(Base):
    """A step of a chain of actions: one call, at its ``position`` in the chain (from 0).

    It stores the call as Domo put it in the chain (``tool_name`` and ``arguments``), its
    ``action_type``, and the ``action`` its tool described when the chain was asked for; ``None``
    for a step that could not be described then, only because an earlier step changes whom it
    acts on: it is checked in its turn. ``status`` follows the step: ``planned``, then ``done`` or
    ``failed``, or ``stopped`` when the chain stopped before it; ``result`` is the card of what
    came of it, and ``done_at`` when it ended.

    ``with_basis`` says whether the step is judged with what the chain remembers of the one who
    asked (``Confirmation.chain_basis``), as it was asked for. ``after_arrival_of`` is the name of
    the invitation whose arrival the step waits for, if it waits for one (spec 4.3b): the chain
    pauses before it until that invitation is accepted.

    ``arguments``, ``action`` and ``result`` hold no secret, token or link, as those of the
    request do.
    """

    __tablename__ = "confirmation_steps"
    __table_args__ = (
        CheckConstraint("status IN ('planned', 'done', 'failed', 'stopped')", name="status_values"),
        CheckConstraint("position >= 0", name="position_from_zero"),
        # A display name is 60 characters at most (``text.MAX_DISPLAY_NAME_LENGTH``).
        CheckConstraint("char_length(after_arrival_of) <= 60", name="after_arrival_of_length"),
    )

    confirmation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("confirmations.id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    tool_name: Mapped[str]
    action_type: Mapped[str]
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB)
    # ``None`` is SQL ``NULL``, never a JSON null.
    action: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    status: Mapped[str]
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    done_at: Mapped[datetime | None]
    with_basis: Mapped[bool] = mapped_column(server_default=text("true"))
    after_arrival_of: Mapped[str | None]
