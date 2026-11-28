"""Reading and writing confirmations, on the caller's session (the caller commits).

``confirmations.service`` re-exports these functions with the decision itself; they live here,
below the chat's handling of tool calls (``conversation.execution`` creates the requests), so that
the service can in turn run an approved action through that handling without an import cycle.

The lifetime of a request is not stored: it is worked out at every read, from ``created_at`` and
the owner's setting (``PolicySettings.max_lifetime``), so that a change of the setting also applies
to the requests already waiting (decision D8). A chain paused until someone's arrival
(``waiting_arrival``) has none: it lives while its invitation can be accepted (spec 4.3b), and its
lifetime starts again at the arrival (``confirmations.arrivals``).

The requests that wait for an adult (``approver_kind`` ``adult``: a child's) are listed for the
adults and the owner who may answer them, and those that wait for the owner (``owner``: a right of
the owner says so) for the owner alone (``awaiting_approval``), those the one who asked nudged
first (``nudged_at``, spec 5.2). The one who asked sees them too, in their own conversation
(``pending_for_others``), and their words never answer them.

A request remembers the reply that last showed it (``last_shown_message_id``): the person's words
can only answer a request that the reply just before them showed (R74, ``mark_shown``), and only
when that reply showed no other request of theirs that waits (R93). Someone else's yes (an adult's
for a child, the owner's when a right says so) may be said in words too, in the approver's own
conversation (spec 5.1): the request remembers the approver's reply that last showed it apart
(``shown_to_approver_message_id``, ``mark_shown_to_approver``), and ``pending_to_approve`` finds
it, locked, for an approver who may still say yes to it.

What a request does is its ``arguments``, or the ``amended_arguments`` a change gave it
(``effective_arguments``): what a card describes and what a yes runs are always the same (R75).

Who may say yes to a request (``approves``) is here too, below the decision itself, for the runner
of a chain of actions (``confirmations.chain``) as well as for the service: so is whether a yes
covers a step of a chain whose barrier now asks for someone's agreement (``covers``, spec 4.3).

A request keeps what it asks for (``ask``: a yes, an explicit yes or a proof), who asked it and
why (``asked_by``, ``ask_why``: ``ask_of``, ``asked_by_of``), as the yes-judge and the owner's rules
decided it when it was made (``conversation.execution``); Domo is told how it is answered from
that (``how_to_answer``, ``how_to_approve``).
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at, is_active
from domo_core.chat.models import Conversation, Message
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.who_agrees import agrees_for_children, may_agree_for_a_child
from domo_core.llm.types import JsonObject
from domo_core.naming import name_shown
from domo_core.people import reach
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, Asked, AskedBy, as_ask, as_asked_by
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.decision import ChainBasis
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import Approver, RightsTable, load_rights
from domo_core.policy.roles import DOMO_SUBJECT_ID, Actor
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.tools.context import InvalidArguments

if TYPE_CHECKING:
    from domo_core.confirmations.judge import Judgement


async def create_confirmation(
    session: AsyncSession,
    *,
    requester: Actor,
    tool_name: str,
    arguments: JsonObject,
    action: DescribedAction,
    approver: Approver,
    asked: Asked | None,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    now: datetime,
    question: str | None = None,
    chain_basis: ChainBasis | None = None,
    shown_to_approver: uuid.UUID | None = None,
    approver_note: str | None = None,
    by_domo: bool = False,
    counted_for_domo: bool = False,
    subject_key: str | None = None,
    idea_reason: str | None = None,
    concerned_id: uuid.UUID | None = None,
    in_their_turn: bool = False,
) -> Confirmation:
    """Store a request that waits for a yes: exactly the call that would run (``tool_name`` and
    ``arguments``) and the action the barrier saw, who may answer (``approver``), what it asks for,
    who asked it and why (``asked``: ``None`` for a chain the judge let through that keeps a
    request for the upkeep, ``let_through``; its ``warned`` is kept), and what Domo could not
    tell when a limit sent it to the owner (``question``, kept as ``ask_reason``). ``created_at``
    is ``now``, the injected clock (never the database's default), since the lifetime is counted
    from it. ``message_id`` (the reply that makes it) is also the reply that last showed it
    (``last_shown_message_id``).
    ``chain_basis`` is, for a chain of actions, what it remembers of the one who asked (its steps
    are stored after it: ``confirmations.steps.add_steps``); ``None`` for any other request.
    ``shown_to_approver`` is the reply of an approver (someone else than the one who asked) that
    shows it, in their own conversation, when an approver's change made it
    (``shown_to_approver_message_id``), and ``approver_note`` what the approver said with that
    change. ``by_domo``: Domo made it on his own (``Confirmation.by_domo``; ``requester`` is
    then the person whose conversation it waits in). ``counted_for_domo``: the decision that made
    it wrote an ``allow`` in Domo's name, so his history counts it already
    (``Confirmation.counted_for_domo``). ``subject_key``: what it is about (``policy.subjects``);
    ``idea_reason``: why he asks, when it was his own idea; ``concerned_id``: the person who alone
    answers it (``None``: its approver kind says); ``in_their_turn``: his request born of the
    owner's rule in a person's turn (``Confirmation.in_their_turn``). Flushed, not committed."""
    confirmation = Confirmation(
        requester_id=requester.person_id,
        approver_kind=approver,
        conversation_id=conversation_id,
        message_id=message_id,
        last_shown_message_id=message_id,
        tool_name=tool_name,
        arguments=dict(arguments),
        action=dict(action.as_json()),
        status="pending",
        created_at=now,
        ask=None if asked is None else asked.ask,
        asked_by=None if asked is None else asked.by,
        ask_why=None if asked is None else asked.why,
        warned=asked is not None and asked.warned,
        ask_reason=question,
        chain_basis=None if chain_basis is None else chain_basis.as_json(),
        shown_to_approver_message_id=shown_to_approver,
        approver_note=approver_note,
        by_domo=by_domo,
        counted_for_domo=counted_for_domo,
        subject_key=subject_key,
        idea_reason=idea_reason,
        concerned_id=concerned_id,
        in_their_turn=in_their_turn,
    )
    session.add(confirmation)
    await session.flush()
    return confirmation


def let_through(c: Confirmation, person_id: uuid.UUID, now: datetime) -> None:
    """The chain ``c`` the judge let through (nobody said yes) is on its way: ``executing``,
    decided by the one who asked (``person_id``) at ``now``, ``decided_via`` ``judge``. Its run is
    the one who asked's own, as an action done at once is."""
    c.status = "executing"
    c.decided_by = person_id
    c.decided_at = now
    c.decided_via = "judge"


def ask_of(c: Confirmation) -> Ask | None:
    """What the request asks for, as stored (``None`` for a chain let through). Anything else is a
    ``ValueError``: it is never guessed."""
    return as_ask(c.ask)


def asked_by_of(c: Confirmation) -> AskedBy | None:
    """Who asked it, as stored."""
    return as_asked_by(c.asked_by)


def asked_of(c: Confirmation) -> Asked | None:
    """What the request asks for, who asked it and why, together (``None`` when it asks
    nothing), and whether the judge warned the person with it."""
    ask, by = ask_of(c), asked_by_of(c)
    if ask is None or by is None:
        return None
    return Asked(ask, by, c.ask_why, bool(c.warned))


def is_past_lifetime(c: Confirmation, settings: PolicySettings, now: datetime) -> bool:
    """Whether the request has waited as long as the owner allows (it then expires by itself).
    Never for a chain paused until someone's arrival: it lives while its invitation can be
    accepted (spec 4.3b)."""
    if c.status == "waiting_arrival":
        return False
    return now >= c.created_at + settings.max_lifetime


def effective_arguments(c: Confirmation) -> JsonObject:
    """What the request does: the ``amended_arguments`` a change gave it, else its
    ``arguments``. A card describes these, and a yes runs these."""
    arguments = c.amended_arguments if c.amended_arguments is not None else c.arguments
    return dict(arguments)


# What became of a request, in the words of the actions of the chat (the history's lines, D7):
# it waits for a yes, then a yes runs it (``running``: what came of it is not known yet, or could
# not be recorded) until it is done or failed, or, for a chain, it pauses until someone's arrival;
# or it was refused, expired or replaced.
_STATES: Final[Mapping[str, str]] = {
    "pending": "waiting",
    "executing": "running",
    "waiting_arrival": "paused",
    "executed": "done",
    "failed": "failed",
    "refused": "refused",
    "expired": "expired",
    "replaced": "replaced",
}


def request_state(c: Confirmation, settings: PolicySettings, now: datetime) -> str:
    """What became of the request so far: ``waiting`` for a yes; ``running`` once a yes runs it
    and until what came of it is recorded (if that failed, it stays so: never ``waiting``, the
    person already said yes); ``paused``, a chain that waits for someone's arrival; then ``done``
    or ``failed``; or ``refused``, ``expired`` (a request past its lifetime is, whatever the row
    says yet) or ``replaced``."""
    if c.status == "pending" and is_past_lifetime(c, settings, now):
        return "expired"
    return _STATES.get(c.status, c.status)


def approver_of(c: Confirmation) -> Approver:
    """Who says yes to the request, as stored: the requester (``self``), an adult other than them
    (``adult``) or the owner (``owner``). The column's constraint keeps it to these three: any
    other value is a bug (or a database edited by hand), and ``ValueError`` then, never a guess."""
    match c.approver_kind:
        case "self":
            return "self"
        case "adult":
            return "adult"
        case "owner":
            return "owner"
        case other:
            raise ValueError(f"Not an approver of a request: {other!r}")


def action_type_of(c: Confirmation) -> str:
    """The type of the stored action, as written by ``create_confirmation``."""
    action_type = c.action.get("action_type")
    return action_type if isinstance(action_type, str) else ""


def requester_of(c: Confirmation) -> uuid.UUID:
    """Who asked for ``c``, as its approvers are told apart from them: the person who asked, or,
    for a request Domo made on his own (``by_domo``), Domo (``DOMO_SUBJECT_ID``, nobody's
    id): any adult then gives an adult's agreement, the owner included, the owner the owner's, and
    nobody gives the requester's own yes."""
    return DOMO_SUBJECT_ID if c.by_domo else c.requester_id


# What a request that waits with the person concerned says of a warning of the judge's: the code's
# own words, never the judge's reason, written from someone else's case (rulings R29, R69, R72). A
# card shows it in the reader's language (``confirmation.asked.domo.cannot_be_undone``).
CANNOT_BE_UNDONE_REASON: Final = "It cannot be undone"


def for_the_person_concerned(asked: Asked) -> Asked:
    """What a request asks, as it waits with the person concerned, someone else than the person of
    the turn: without the judge's reason; when the judge warned, with the code's own line that it
    cannot be undone (``CANNOT_BE_UNDONE_REASON``), so that they are told before they answer."""
    return replace(asked, why=CANNOT_BE_UNDONE_REASON if asked.warned else None)


def judgement_told_to(
    judgement: "Judgement", *, case_of: uuid.UUID | None, reader: uuid.UUID
) -> "Judgement":
    """The judge's verdict as ``reader`` may be told it (ruling R75): its reason is said only to
    the person whose case it was (``case_of``: whose private memories it read; ``None`` when that
    is not known). Anyone else gets the code's own line instead, that it cannot be undone when the
    judge warned (``CANNOT_BE_UNDONE_REASON``), else none: on a card, a tool's result, a request's
    ``ask_why`` or a fact about them. The reason kept for the journal (``noted``) stays, for the
    audit alone."""
    if case_of is not None and case_of == reader:
        return judgement
    return replace(
        judgement,
        reason=CANNOT_BE_UNDONE_REASON if judgement.warns else "",
        noted=judgement.noted,
    )


def warned_one(c: Confirmation) -> uuid.UUID | None:
    """Whom the judge warned with ``c`` (``warned``, with its reason ``ask_why``): the person the
    thing belongs to, the person it concerns (``concerned_id``: for an idea of Domo's, the one
    who alone answers it) or else the one who asked. What he told them is theirs alone, kept when
    they answer it (``service._mark_decided``). ``None`` when nobody was warned. A request that
    waits with the person concerned keeps the fact that it was warned, never the judge's words
    (ruling R69): ``ask_why`` is then the code's own line (``for_the_person_concerned``)."""
    if not c.warned:
        return None
    return c.concerned_id if c.concerned_id is not None else c.requester_id


def replayed_on_his_own(c: Confirmation) -> bool:
    """Whether a yes replays ``c`` as an act of his on his own (on his own rights, for the house):
    a request of his (``by_domo``) made in a turn of his own. Never one born of his mark in a
    person's turn (``made_in_a_turn``), nor one born of the owner's rule on his act there
    (``in_their_turn``): those are replayed in that turn, for that person."""
    return c.by_domo and not made_in_a_turn(c) and not c.in_their_turn


def made_in_a_turn(c: Confirmation) -> bool:
    """Whether ``c`` is a request of Domo's (``by_domo``) born of his explicit mark
    (``act_on_my_own``) in a person's turn: judged again and run as his in that person's turn
    (the requester's), never as a turn of his own. Only such a request keeps his reason
    (``idea_reason``, set by ``conversation.execution`` for a marked call and nothing else: not
    for a call of a turn he began, which stays the person's, nor on his own, replayed on his
    own)."""
    return c.by_domo and c.idea_reason is not None


async def conversation_made_in(session: AsyncSession, c: Confirmation) -> uuid.UUID | None:
    """The conversation of the turn the request was first made in, the one who asked's
    (``requester_id``): where it runs after a yes, and where they are woken and told what came of
    it. It is the one it waits in (``conversation_id``) when that one is theirs; else (it waits
    for the person it concerns, in their own conversation) the conversation of the reply that
    made it (``message_id``, which every request born of a change of it keeps), when that one is
    theirs. A request made where no reply was written (a review of what was said:
    ``memory.review``, ``message_id`` ``None``) was made in the requester's conversation: their
    latest one. ``None`` when that cannot be known: nothing is then run or woken anywhere, never
    in someone else's conversation as theirs. Reads only."""
    whose = await session.scalar(
        select(Conversation.person_id).where(Conversation.id == c.conversation_id)
    )
    if whose == c.requester_id:
        return c.conversation_id
    if c.message_id is None:
        return await session.scalar(
            select(Conversation.id)
            .where(Conversation.person_id == c.requester_id)
            .order_by(Conversation.created_at.desc(), Conversation.id)
            .limit(1)
        )
    found = (
        await session.execute(
            select(Conversation.id, Conversation.person_id)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(Message.id == c.message_id)
        )
    ).first()
    if found is None or found.person_id != c.requester_id:
        return None
    return found.id


def decided_for_him(c: Confirmation) -> bool:
    """Whether ``c`` is a request Domo made on his own (``by_domo``, no mark) that waited for
    someone else than the person whose turn he made it in (``concerned_id``): what that person
    decided wakes him, on his own, and is no news for the one in whose conversation he made it,
    who asked nothing (ruling R33)."""
    return (
        c.by_domo
        and not made_in_a_turn(c)
        and c.concerned_id is not None
        and c.concerned_id != c.requester_id
    )


def approves(
    approver_kind: str,
    person: Person,
    requester_id: uuid.UUID,
    *,
    rights: RightsTable,
    policy: PolicyTables,
) -> bool:
    """Whether ``person`` is an approver of that kind for a request of ``requester_id``, under
    the ``rights`` in force and the catalog ``policy``: the requester for ``self``; someone who
    may agree for them (``who_agrees.may_agree_for_a_child``) for ``adult``; the owner, not the
    requester, for ``owner``. Any other kind has no approver."""
    match approver_kind:
        case "self":
            return person.id == requester_id
        case "adult":
            # The single rule of who may agree for a child (the judge's case marks by it too).
            return may_agree_for_a_child(
                actor_of(person), requester_id, rights=rights, policy=policy
            )
        case "owner":
            return person.role == "owner" and person.id != requester_id
        case _:
            return False


# Who says yes, from the least demanding to the most.
_APPROVERS: Final[tuple[Approver, ...]] = ("self", "adult", "owner")


def covers(
    given: str,
    needed: Approver,
    person: Person,
    requester_id: uuid.UUID,
    *,
    rights: RightsTable,
    policy: PolicyTables,
) -> bool:
    """Whether the yes ``person`` gave to a chain of ``requester_id``, as an approver of the kind
    ``given`` (the one the request waited for), covers a step for which the barrier now asks for
    the agreement of the kind ``needed``. The yes of a chain covers each of its steps: only a more
    demanding approver than the one who said yes stops it (spec 4.3). So it covers the step when
    ``person`` is an approver of the kind ``needed``, or when ``needed`` is less demanding than
    ``given`` and
    ``person`` is still an approver of that kind (a yes is not covered by someone who no longer
    could give it). ``rights`` and ``policy`` are those in force (``approves``)."""
    if approves(needed, person, requester_id, rights=rights, policy=policy):
        return True
    if given not in _APPROVERS:
        return False
    return _APPROVERS.index(needed) < _APPROVERS.index(given) and approves(
        given, person, requester_id, rights=rights, policy=policy
    )


# What Domo is told of who says yes to a request and how, so that it tells the person plainly
# (final review Important 1). In English, the prompt's language: Domo says it in the person's.
# True on every channel: the words answer everywhere, and a button only where there is one (the
# context says whether there is: ``prompts.abilities_line``).
ANSWER_IN_WORDS: Final = "a yes in their own words, or its button where there is one"
ANSWER_EXPLICITLY: Final = (
    'a firm yes in their own words ("I confirm", "yes, I am sure": no fixed words), or its button '
    "where there is one (a plain yes is not enough)"
)
ANSWER_WITH_A_PROOF: Final = (
    "a proof that it is them: one of their agreed means (never in the conversation)"
)
ANSWER_BY_AN_ADULT: Final = (
    "an adult, with the button on their own chat page or in words to their own assistant (never "
    "the "
    "words of this conversation)"
)
ANSWER_BY_THE_OWNER: Final = (
    "the owner, with the button on their own chat page or in words to their own assistant (never "
    "the "
    "words of this conversation)"
)
# How the person answers a request of someone else that waits for their yes (spec 5.1): their
# words once Domo showed it alone in his reply just before, or its button where there is one.
ANSWER_AS_APPROVER: Final = (
    "your words after you showed it (confirmation_remind), or its button where there is one"
)
ANSWER_AS_APPROVER_EXPLICITLY: Final = (
    'a firm yes in their own words ("I confirm", "yes, I am sure": no fixed words) after you '
    "showed it (confirmation_remind), or its button where there is one; a plain yes is not enough"
)
ANSWER_AS_APPROVER_WITH_A_PROOF: Final = (
    "a proof that it is the person you talk with (not the one who asked): one of their agreed "
    "means (never in words)"
)
# A chain paused until someone's arrival waits for no answer (spec 4.3b).
ANSWER_WHEN_THEY_JOIN: Final = (
    "nothing: it runs by itself once they join; you may drop it with confirmation_expire"
)

# Who says yes, as Domo is told, by ``approver_kind``.
_WHO: Final[Mapping[Approver, str]] = {
    "self": "the person",
    "adult": "an adult",
    "owner": "the owner",
}


def who_answers(c: Confirmation) -> str:
    """Who says yes to the request, as Domo is told: ``the person`` who asked, ``an adult``
    (for a child's request) or ``the owner`` (when a right of the owner says so)."""
    return _WHO[approver_of(c)]


# Who a request waits for, as a word for a role: its own requester, the person it concerns, or the
# approver the barrier named. Never a name, never a detail of a person (``waits_for_of``).
_WAITS_FOR: Final[Mapping[Approver, str]] = {
    "self": "the person themself",
    "adult": "an adult",
    "owner": "the owner",
}
WAITS_FOR_THE_CONCERNED: Final = "the person it concerns"


def waits_for_of(c: Confirmation) -> str:
    """Who must answer the request, as a role word: the person it concerns when it is not the one
    who asked (``concerned_id``: they alone answer it), else the approver's kind (the person
    themself, an adult, the owner). It names no one."""
    if c.concerned_id is not None and c.concerned_id != c.requester_id:
        return WAITS_FOR_THE_CONCERNED
    return _WAITS_FOR[approver_of(c)]


def how_to_answer(c: Confirmation) -> str:
    """How that yes can be given, as the Domo of the one who asked is told: someone else's yes
    (an adult's for a child, or the owner's) by the button of their own page or in words to their
    own Domo, never the words of this conversation; the person's own request as it asks
    (``ask_of``): a yes in words, a firm yes in words (or its button where there is one), or a
    proof.
    ``how_to_approve`` says it to the approver's Domo."""
    match approver_of(c):
        case "adult":
            return ANSWER_BY_AN_ADULT
        case "owner":
            return ANSWER_BY_THE_OWNER
        case "self":
            match ask_of(c):
                case "explicit_yes":
                    return ANSWER_EXPLICITLY
                case "proof":
                    return ANSWER_WITH_A_PROOF
                case "yes" | None:
                    return ANSWER_IN_WORDS


def how_to_approve(ask: Ask | None) -> str:
    """How the approver of someone else's request answers it, as their Domo is told: the button,
    or their words once he showed it, as the request asks (``ask``: ``ask_of``), or a proof on its
    card."""
    match ask:
        case "explicit_yes":
            return ANSWER_AS_APPROVER_EXPLICITLY
        case "proof":
            return ANSWER_AS_APPROVER_WITH_A_PROOF
        case "yes" | None:
            return ANSWER_AS_APPROVER


async def pending_for(
    session: AsyncSession,
    person_id: uuid.UUID,
    conversation_id: uuid.UUID,
    settings: PolicySettings,
    now: datetime,
) -> list[Confirmation]:
    """The requests this person answers themselves (``self``) that wait in this conversation and
    are still under the lifetime, oldest first: their own, and those of Domo's that concern them
    (``concerned_id``: they alone answer it); never one of his that waits for someone else's yes
    there."""
    result = await session.execute(
        select(Confirmation)
        .where(
            or_(
                Confirmation.concerned_id == person_id,
                (Confirmation.requester_id == person_id)
                & Confirmation.by_domo.is_(False)
                & Confirmation.concerned_id.is_(None),
            ),
            Confirmation.conversation_id == conversation_id,
            Confirmation.approver_kind == "self",
            Confirmation.status == "pending",
            Confirmation.created_at > now - settings.max_lifetime,
        )
        .order_by(Confirmation.created_at, Confirmation.id)
    )
    return list(result.scalars())


async def pending_for_others(
    session: AsyncSession,
    person_id: uuid.UUID,
    conversation_id: uuid.UUID,
    settings: PolicySettings,
    now: datetime,
) -> list[Confirmation]:
    """The requests of this person in this conversation that wait for someone else's yes (an
    adult's for a child's request, the owner's when a right says so: ``adult`` or ``owner``) and
    are still under the lifetime, oldest first (spec 5.2). The one who asked sees them, and may
    drop, change or nudge them; their words never answer them (``pending_for`` lists those). A
    request Domo made on his own in their conversation is not theirs (``awaiting_approval``
    lists it to its approvers, the owner included)."""
    result = await session.execute(
        select(Confirmation)
        .where(
            Confirmation.requester_id == person_id,
            Confirmation.by_domo.is_(False),
            Confirmation.conversation_id == conversation_id,
            Confirmation.approver_kind.in_(("adult", "owner")),
            Confirmation.status == "pending",
            Confirmation.created_at > now - settings.max_lifetime,
        )
        .order_by(Confirmation.created_at, Confirmation.id)
    )
    return list(result.scalars())


def still_nudged(c: Confirmation) -> bool:
    """Whether the one who asked nudged the approvers of ``c`` (``nudged_at``, spec 5.2) since an
    approver's Domo last showed it to them (``last_reminded_at``, which a nudge does not move):
    their Domo is told so until he shows it; a new nudge tells him again."""
    return c.nudged_at is not None and (
        c.last_reminded_at is None or c.last_reminded_at < c.nudged_at
    )


async def paused_chains_of(
    session: AsyncSession, person_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[Confirmation]:
    """The chains of this person in this conversation that paused until someone's arrival
    (``waiting_arrival``), whoever's yes they waited for, oldest first: the one who asked sees
    them, and may drop them (spec 4.3b). None of them is answered in words (``pending_for`` lists
    those)."""
    result = await session.execute(
        select(Confirmation)
        .where(
            Confirmation.requester_id == person_id,
            Confirmation.conversation_id == conversation_id,
            Confirmation.status == "waiting_arrival",
        )
        .order_by(Confirmation.created_at, Confirmation.id)
    )
    return list(result.scalars())


async def awaiting_approval(
    session: AsyncSession, approver: Person, settings: PolicySettings, now: datetime, *, limit: int
) -> tuple[list[tuple[Confirmation, str]], int]:
    """The requests that wait for ``approver``'s yes: those that wait for an adult's (a
    child's, by default) and, for the owner, those that wait for the owner's; still under the
    lifetime, made by someone who is still in the household; those the one who asked nudged first
    (``nudged_at``, spec 5.2), then the oldest first. The first ``limit`` of them, each with the
    requester's display name, and how many there are in all.

    Only who may agree for a child has any (``who_agrees.agrees_for_children``), and
    only the owner has the owner's; the approver's own requests are never among them (nobody
    approves their own), but a request Domo made on his own is never the approver's own, even
    in their conversation: its name is Domo's, as the approver reads it. A child or a guest gets an
    empty list."""
    if (
        not is_active(approver, now)
        or not agrees_for_children(
            actor_of(approver),
            rights=await load_rights(session),
            policy=await load_policy(session),
        )
        or not await reach.can_answer(session, approver.id, now)
    ):
        return [], 0
    kinds = ("adult", "owner") if approver.role == "owner" else ("adult",)
    waiting = (
        Confirmation.approver_kind.in_(kinds),
        Confirmation.status == "pending",
        Confirmation.created_at > now - settings.max_lifetime,
        or_(Confirmation.by_domo, Confirmation.requester_id != approver.id),
        active_at(now),
    )
    found = await session.execute(
        select(Confirmation, Person.display_name)
        .join(Person, Person.id == Confirmation.requester_id)
        .where(*waiting)
        # ``false`` before ``true``: a nudged request (``nudged_at`` set) first.
        .order_by(Confirmation.nudged_at.is_(None), Confirmation.created_at, Confirmation.id)
        .limit(limit)
    )
    total = await session.execute(
        select(func.count())
        .select_from(Confirmation)
        .join(Person, Person.id == Confirmation.requester_id)
        .where(*waiting)
    )
    domo = name_shown(approver.calls_domo, settings.house_domo_name)
    return [(c, domo if c.by_domo else name) for c, name in found.all()], total.scalar_one()


async def mark_shown(
    session: AsyncSession, confirmation_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    """Record that the reply ``message_id`` presented the request again: the person's answer to
    that reply may decide it in words (R74). Flushed, not committed."""
    await session.execute(
        update(Confirmation)
        .where(Confirmation.id == confirmation_id)
        .values(last_shown_message_id=message_id)
        .execution_options(synchronize_session=False)
    )


async def mark_shown_to_approver(
    session: AsyncSession, confirmation_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    """Record that the reply ``message_id``, in the conversation of an approver of the request
    (not the one who asked), presented it again: the approver's answer to that reply may decide it
    in words (R74, for them). What the one who asked was shown does not move. Flushed, not
    committed."""
    await session.execute(
        update(Confirmation)
        .where(Confirmation.id == confirmation_id)
        .values(shown_to_approver_message_id=message_id)
        .execution_options(synchronize_session=False)
    )


# What Domo is told when an id names no request that waits for the person's yes (whatever the
# reason: nobody learns whether the id exists, nor whose it is).
_NOT_TO_APPROVE: Final = "no request that waits for this person's yes has this id"


async def pending_to_approve(
    session: AsyncSession, confirmation_id: uuid.UUID, approver: Person, now: datetime
) -> Confirmation:
    """The request, locked, if it waits for ``approver``'s yes: someone else's (its
    ``approver_kind`` is not ``self``), ``pending``, still under its lifetime, made by someone who
    is still in the household (as ``awaiting_approval`` lists them), and ``approver`` may say yes
    to it now (active, and an approver of its kind: ``approves``, never the one who asked).
    ``InvalidArguments("not_found")`` otherwise, whatever the reason."""
    found = await session.execute(
        select(Confirmation)
        .where(Confirmation.id == confirmation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    c = found.scalar_one_or_none()
    if (
        c is None
        or c.approver_kind == "self"
        or c.status != "pending"
        or not is_active(approver, now)
        or not await reach.can_answer(session, approver.id, now)
        or not approves(
            c.approver_kind,
            approver,
            requester_of(c),
            rights=await load_rights(session),
            policy=await load_policy(session),
        )
        or is_past_lifetime(c, await load_policy_settings(session), now)
    ):
        raise InvalidArguments("not_found", _NOT_TO_APPROVE)
    requester = await session.get(Person, c.requester_id)
    if requester is None or not is_active(requester, now):
        raise InvalidArguments("not_found", _NOT_TO_APPROVE)
    return c


async def confirmations_by_id(
    session: AsyncSession, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Confirmation]:
    """These confirmations, by id (the ones that do not exist are left out)."""
    wanted = list(set(ids))
    if not wanted:
        return {}
    result = await session.execute(select(Confirmation).where(Confirmation.id.in_(wanted)))
    return {c.id: c for c in result.scalars()}


async def predecessors_of(
    session: AsyncSession, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Confirmation]:
    """For each of these requests that replaced another one (a change made it a new request),
    the request it replaced."""
    wanted = list(set(ids))
    if not wanted:
        return {}
    result = await session.execute(select(Confirmation).where(Confirmation.replaced_by.in_(wanted)))
    return {c.replaced_by: c for c in result.scalars() if c.replaced_by is not None}
