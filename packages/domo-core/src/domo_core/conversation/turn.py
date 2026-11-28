"""A turn of Domo, run: from the person's words to the audit entry, as events in data.

A turn is prepared first (``conversation.prepare.prepare_turn``: everything it needs is read
there, in a transaction that is over before any model is called). ``Turn.run`` then gives whoever
listens (``conversation.channel.Listener``) what Domo says and does, as it comes: ``Words`` (plain
text, the marks of formatting already removed: ``text.PlainStream``), ``Acted`` (an action and its
card, in data), ``Decided`` (a request the person's words just answered), ``Failed`` (why the turn
could not go on) and, last, ``Ended``. It never puts anything in a form: a channel does
(``channels.page.render`` for the page).

The person's words, read first
------------------------------

A turn that answers the person's message starts by reading it as an answer to the requests that
wait for them (decision D2, spec 11.1): those the reply just before their message showed (their
own in this conversation, R74, or someone else's that waits for their yes as its approver, spec
5.1), one or several ("yes, both", "yes, the first one"). The words are read by the code and,
failing that, by the isolated reader (``confirmations.intent.read_replies``), given who speaks,
the requests shown and the person's few messages before, never by the main model. Each yes or no
is carried out (``decide_confirmation``), in the order shown, and recorded with the reply, as one
step shielded from the cancellation of the turn, and ``Decided`` is given; someone else's request
decided so also wakes Domo in the conversation of the one who asked (``ToolContext.wake``), as a
button does. The turn then goes on to Domo, who answers it himself, told what was just decided
(``prompts.JustDecided``) and the requests that wait without those. The sentences of the catalog
that used to answer only stand in when the model fails (a failure of any kind, an unexpected
exception included) or says nothing at all (no text and no action), or when what was decided
cannot be read to tell him: they are then the reply, given as any words and stored ``complete``,
and the audit entry says so (``"fallback": true``, with the status the model's turn came to).

A yes with a change to the one request shown goes on to Domo with the request marked
``answered_with_change``, and the right to apply that change in this turn
(``ToolContext.change_grant``); to one of several, nothing is decided and every request shown is
marked so, without that right: Domo asks what they meant. A yes that does not give what a request
asks (an explicit yes, a proof) goes on with the request marked ``plain_yes_not_enough``, and the
reply presents it again. A no in words from a speaker the code is not sure of is weighed by the
judge (``_a_no_taken``): taken for theirs, the request is refused; otherwise it stays waiting,
the reply presents it again and Domo is told the no was not taken (``NO_NOT_TAKEN``). Anything
else goes on as a plain turn.

A wake (``decided``: a button, an approver's words or an arrival decided a request) has no words
of the person to read: Domo is told what was decided, and nothing answers in his stead. An
arrival (the person just came back) has none either, nor a turn a plan of his woke him for (spec
6.9). Each may end in silence (``silent_ok``): when Domo writes nothing and does nothing, the
reply is taken away rather than settled, and the audit entry says ``"status": "silent"``. Their
failures are logged (the kind only): nobody reads them.

Acting
------

Each call of the model goes through ``conversation.execution.handle_tool_call``, to its end even
if the turn is cancelled meanwhile (each step has its own time limit). Before a call, once nobody
reads the turn any more (``Listener.reading``), what the call would show only once is left on the
person's card (``ToolContext.no_reader``). The head of a card Domo writes on the moment is
written before its event is given (``TurnDeps.card_heads``: a hook of the application, the
heads being texts of the page), whoever listens. An action that is never shown (Domo's memory,
``policy.catalog.SILENT_ACTIONS``) gives no event and no head at all; once a turn that kept or
changed a memory is over, the vectors the memories lack are computed in a task of the application
(``memory_vectors``), never in the turn.

What the person writes while the turn runs is kept (spec 11.3, ``conversation.kept``): between
two rounds of calls, Domo is given it as their own words (``chat.agent.KEPT_MARKER``, never words
from outside), and the reply answers the last of it from then on; he judges whether to stop,
change or go on, and what was done before stays done. What the turn had no round left to read is
answered once it is over, by a reply of its own (``Conversation.answer_kept``). Whoever the
channel of such words says hears them besides (``TurnRequest.also_hears``: the voice that heard
them) is given the reply's events from when it reads them (``Turn.hear_also``). A yes said so is
read as any yes in words only at the start of a turn: the one after.

The end of the turn
-------------------

The end is stored whatever happens (``_settle``): the text received so far and a status
(``complete``; ``error``, with why in ``failure``; ``partial`` when the turn was stopped before its
end), together with the audit entry ``chat.turn`` (metadata only, never any text), before
``Ended``. It is shielded from cancellation and has time limits of its own; should the audit
entry fail, the reply is settled without it. Then, for a turn that answered in words: the notes it
was given count as told when Domo could not say so himself, the news told once of an arrival
count as told, and the news it read are taken away.
"""

import logging
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import anyio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.chat.agent import (
    ActionRecord,
    ActionStatus,
    ActionTaken,
    TextDelta,
    TurnDone,
    TurnFailed,
    run_turn,
)
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.case import RightsSay
from domo_core.confirmations.chain import just_decided, request_told
from domo_core.confirmations.describe import (
    description_for_a_model,
    request_description,
    waiting_since,
)
from domo_core.confirmations.intent import (
    ReplySource,
    ShownRequest,
    read_replies,
    reader_needed,
)
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import (
    MAX_APPROVALS_SHOWN,
    AmendOutcome,
    DecideOutcome,
    action_type_of,
    asked_of_a_yes,
    awaiting_approval,
    confirmations_by_id,
    decide_confirmation,
    mark_shown,
    mark_shown_to_approver,
    requester_context,
    what_the_yes_gave,
)
from domo_core.confirmations.store import (
    ANSWER_AS_APPROVER_EXPLICITLY,
    ANSWER_AS_APPROVER_WITH_A_PROOF,
    ANSWER_EXPLICITLY,
    ANSWER_WHEN_THEY_JOIN,
    ANSWER_WITH_A_PROOF,
    NO_NOT_TAKEN,
    ask_of,
    asked_by_of,
    how_to_answer,
    how_to_approve,
    still_nudged,
    who_answers,
)
from domo_core.conversation.cards import card_sentence, made_good, storable
from domo_core.conversation.channel import NOBODY_LISTENS, Abilities, Listener, ListenerGone
from domo_core.conversation.events import (
    Acted,
    Decided,
    Ended,
    Event,
    Failed,
    FailureReason,
    Words,
)
from domo_core.conversation.execution import (
    consult_the_judge,
    failed_record,
    handle_tool_call,
    read_the_person_again,
    store_action,
)
from domo_core.conversation.once import said_once
from domo_core.conversation.speaker import joined_by
from domo_core.conversation.visits import clear_news
from domo_core.db import SessionFactory
from domo_core.llm.catalog import Catalog
from domo_core.llm.costs import estimate_tokens
from domo_core.llm.models import Provider
from domo_core.llm.newcomers import Newcomer, mark_newcomers_told
from domo_core.llm.picker import ModelPicker
from domo_core.llm.spending import LimitNote, mark_thresholds_told
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import (
    ChatMessage,
    ChatUsage,
    ErrorKind,
    JsonObject,
    ToolCall,
    ToolDefinition,
)
from domo_core.memory.embedder import Embedder
from domo_core.memory.search import embed_missing
from domo_core.memory.service import mark_served
from domo_core.people.directives import for_others
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, most_demanding
from domo_core.policy.catalog import is_silent
from domo_core.policy.catalog import shown as action_shown
from domo_core.policy.decision import doubted
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor, Role
from domo_core.policy.settings import PolicySettings
from domo_core.prompts import (
    NO_MEMORY,
    AboutToExpire,
    AgreementLine,
    ArrivalNote,
    BeingBuilt,
    ChangeLine,
    DuePlan,
    HouseholdLine,
    JustDecided,
    KeysNote,
    LessonNote,
    MemoryBlock,
    ModelsNote,
    NewcomerNote,
    NoteToTell,
    OthersRequest,
    OwnershipNote,
    PendingRequest,
    PlanLine,
    SpendingNote,
    Today,
    ToolAside,
    ToolWake,
    UnansweredMoment,
    WentWrongShown,
    WriterTime,
    build_system_prompt,
    build_upkeep_prompt,
)
from domo_core.text import PlainStream
from domo_core.texts.view import REFERENCE, Texts
from domo_core.tools.context import ToolContext, tools_for, tools_kept_from
from domo_core.tools.notes import mark_told
from domo_core.trust.service import Trust

if TYPE_CHECKING:
    # Only for the type of what a turn is given: the preparation makes the turn.
    from domo_core.conversation.prepare import TurnDeps, TurnRequest

logger = logging.getLogger(__name__)

# How a reply is stored once its turn is over.
ReplyStatus = Literal["complete", "error", "partial"]
# How a turn ended (``Turn.run``): in words, in silence (no words, no error), or it failed.
TurnEnd = Literal["spoke", "silent", "failed"]

# How long each attempt to store the end of a turn may take, and marking a request as shown again
# (see ``Turn._settle``).
_SETTLE_SECONDS: Final = 15.0
# What separates the sentence that answers in Domo's stead from the text he had begun (as the
# text of one round of a turn is from the round before).
_FALLBACK_SEPARATOR: Final = "\n\n"
# The tool with which Domo says he told a note: a turn without it leaves that to the code.
_NOTE_TOLD_TOOL: Final = "note_told"
# The actions that write the words of a memory: their vector is computed once the turn is over.
_WRITES_MEMORY: Final = frozenset({"memory.keep", "memory.change"})


def _a_failed_attempt(item: object) -> bool:
    """Whether ``item``, held in an answer, is a call refused for its arguments that a later call
    of the same tool may still make good (spec 6.7); what is never shown has no card to hold."""
    return (
        isinstance(item, ActionRecord)
        and item.status == "invalid"
        and not is_silent(item.action_type or "")
    )


type Given = Words | ActionRecord


def told_here(request: Confirmation, *, conversation_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    """Whether the decision of ``request``, in words, in a turn of ``person_id`` in
    ``conversation_id``, is told by that turn's reply alone (``Turn._told_here``): a request of
    Domo's own that waits there, made in that person's own turn. Any other (someone else's, or one
    made in someone else's turn that waited for the person it concerns) wakes Domo in the turn it
    was made in."""
    return (
        request.by_domo
        and request.conversation_id == conversation_id
        and request.requester_id == person_id
    )


class HeldBack:
    """What a turn gives the person while a failed attempt may still be made good (spec 6.7).

    From a call refused for its arguments on, what Domo says and does is held, in its order,
    for as long as the model round that follows that refusal may still call the same tool again:
    a call of that tool done or waiting makes the attempt good, and its card is then never given;
    once that next round is over without one (its last call handled: ``ActionTaken.closes_round``,
    whether it said words, called other tools, or both), the attempt goes out, with what came
    after it, and the answer flows again. Whatever is held goes out at the end of the answer. The
    person sees what was done in the order it came, as the page shows it once stored
    (``conversation.cards.shown_actions``).

    The rounds are the engine's own (``chat.agent.ActionTaken.round``): nothing is guessed from
    the words. Each method gives what may go out now, in order."""

    def __init__(self) -> None:
        self._held: list[Given] = []
        # The round of each failed attempt held, by the identity of its record.
        self._round_of: dict[int, int] = {}

    def words(self, words: Words) -> list[Given]:
        """The words of the answer, as they come."""
        if self._held:
            self._held.append(words)
            return []
        return [words]

    def action(self, record: ActionRecord, *, round: int, closes_round: bool) -> list[Given]:
        """An action of the answer, as it comes: of the round of calls ``round``, and the last one
        of it when ``closes_round``."""
        self._held = [
            item
            for item in self._held
            if not (isinstance(item, ActionRecord) and made_good(item, record))
        ]
        self._held.append(record)
        if _a_failed_attempt(record):
            self._round_of[id(record)] = round
        if not any(_a_failed_attempt(item) for item in self._held):
            return self._taken()
        if not closes_round:
            return []
        # The round ``round`` is over: the attempts of the rounds before it had their next round,
        # and what is held up to the first attempt of this very round goes out.
        kept = len(self._held)
        for at, item in enumerate(self._held):
            if _a_failed_attempt(item) and self._round_of[id(item)] >= round:
                kept = at
                break
        given, self._held = self._held[:kept], self._held[kept:]
        return given

    def end(self) -> list[Given]:
        """The answer is over: whatever was held."""
        return self._taken()

    def _taken(self) -> list[Given]:
        given, self._held = self._held, []
        self._round_of.clear()
        return given


async def memory_vectors(session_factory: SessionFactory, embedder: Embedder) -> None:
    """Compute the vectors the memories lack (``memory.search.embed_missing``), in a task of its
    own after a turn that kept or changed one. Never raises: a failure is logged by its kind, and
    the upkeep's pass computes them later (``upkeep.memory_vectors_pass``)."""
    try:
        await embed_missing(session_factory, embedder)
    except Exception as error:
        logger.error("The vectors of the memories could not be computed (%s)", type(error).__name__)


async def memories_served(
    session_factory: SessionFactory, ids: Sequence[uuid.UUID], now: datetime
) -> None:
    """Mark these memories as served to Domo at ``now`` (``memory.service.mark_served``), in a
    task of its own once the turn that gave them is over. Never raises: a failure is logged by its
    kind, and they are only served a little less recently."""
    try:
        async with session_factory() as session:
            await mark_served(session, ids, now)
            await session.commit()
    except Exception as error:
        logger.error("The memories given could not be marked as served (%s)", type(error).__name__)


# The sentence that answers a turn whose decision was taken from the person's words when Domo
# cannot (the model fails, or says nothing). A ``failed`` one answers with what its card says, an
# ``approver_changed`` one with whose yes the request waits for now (``_REROUTED_REPLIES``). The
# other outcomes missing here (``needs_explicit``, ``needs_proof``, ``not_yours``) decide nothing:
# Domo answers as in any turn.
_DECIDED_REPLIES: Final[Mapping[str, str]] = {
    "executed": "confirmation.reply.executed",
    "refused": "confirmation.reply.refused",
    "expired": "confirmation.reply.expired",
    "already_decided": "confirmation.reply.already_decided",
    "paused": "confirmation.reply.paused",
}
# The fixed sentence of a yes the replay refused, by the reason its record gives.
_REFUSAL_REPLIES: Final = frozenset({"not_allowed", "no_longer_possible", "limit", "limit_error"})
# The fixed sentence of a yes that was not the one the request needs now (``approver_changed``,
# Ruling R27), by whose yes it waits for, as its record gives it.
_REROUTED_REPLIES: Final[Mapping[object, str]] = {
    approver: f"confirmation.reply.approver_changed.{approver}"
    for approver in ("self", "adult", "owner")
}
# What the reply's action says of a decision that changed the request.
_DECIDED_ACTIONS: Final[Mapping[str, ActionStatus]] = {
    "executed": "done",
    "failed": "failed",
    "refused": "refused",
}


class CardHeads(Protocol):
    """What writes the heads of the cards a turn shows, when Domo writes them on the moment
    (spec 4.4): the application gives it (``texts.card_heads.CardHeadWriting``), the core only says
    when. None of it raises."""

    async def of_request(
        self, confirmation_id: uuid.UUID, *, now: datetime, wait_for: uuid.UUID
    ) -> None:
        """Have the head of a request that waits for a yes written for each of its readers,
        waiting a moment at most for ``wait_for``'s (the person of the turn)."""
        ...

    async def of_action(self, record: ActionRecord, *, reader: Person, texts: Texts) -> None:
        """Have the head of the card of an action of the turn written for ``reader``, whose texts
        are ``texts``, waiting a moment at most."""
        ...

    def ask_for_request(self, confirmation_id: uuid.UUID, *, clock: Callable[[], datetime]) -> None:
        """Have the heads of a request that a decision left waiting written in the background."""
        ...


@dataclass(frozen=True)
class News:
    """The news told once that a turn was given without marking them told (an arrival's, which
    may say nothing): the limits whose threshold it told, and the newcomers. They are marked once
    the turn answered in words (``Turn._mark_news_told``)."""

    limits: tuple[LimitNote, ...] = ()
    newcomers: tuple[Newcomer, ...] = ()


@dataclass(frozen=True)
class Prompt:
    """What the system prompt of a turn is built from, but for the pending requests: those are
    only known once the person's words are read (one may be ``answered_with_change``).

    ``calls_domo`` is the name the person calls Domo by, ``None`` when they gave him none
    (spec 6.7); ``house_calls`` and ``house_called_before`` are the names the house calls him by
    now and before, ``same_name`` says whose name a person of the house has too.

    ``writer_time`` (the time the rights writer has, and how often it ran out of it), ``ownership``
    (the note for a new owner) and ``successors`` (the list of successors, each place as
    ``SuccessorShown.as_json`` tells it, empty when the owner chose nobody) are the owner's only:
    ``None`` for anyone else; so are ``promises``, the promises of access that wait
    (``people.promises.shown``), empty for anyone else. ``directives`` are the owner's directives
    in force (``people.directives.shown``): all of them for him, only that nobody new is kept for
    anyone else (``people.directives.for_others``). ``today`` is the day and hour of the turn, in
    the household's time zone or in UTC, ``household`` the active people of the house (with where
    each stands, the child mark, the role made for them and whether they have an account in the
    owner's turns and his own), ``known`` the records he only knows, with no access (those turns
    alone, empty otherwise), ``nicknames`` the names the person uses for some of them, each with the
    whole name it means (spec 6.7), and ``memory`` what Domo is given of his memory (his core, what
    he remembers about what the person said lately, spec 6.4), ``plans`` the plans he set for the
    person that wait, the soonest first (spec 6.9).
    ``spending`` is where the limits the person set stand, ``newcomers`` who began using a key
    they gave (told once), ``keys`` the keys they may use and those the request passed over at
    their limit, ``models`` the key and the model the request talks on, ``lessons`` what Domo
    learned of the models lately, ``went_wrong`` the last things that went wrong for the person
    and in his own acts (``prompts.WentWrongShown``).
    ``notes_to_tell`` are the notes left for the person that Domo has not told them yet (spec
    4.8), the oldest first, and ``unanswered`` the moments he could not answer them since his last
    complete reply (spec 4.10). ``arrival`` is what he is told when the person just came back (spec
    4.9). ``reads`` is the language the person reads (theirs, else the house's, spec 5.3).
    ``upkeep`` is that language for an upkeep turn (``None`` for any other): its prompt is the
    upkeep's (``prompts.build_upkeep_prompt``). ``offered`` are the names of the tools the turn
    offers, ``kept`` those of the household the rights keep from them (``tools_kept_from``), and
    ``abilities`` what the channel of the request can do (the context says how Domo is
    heard there: ``prompts.abilities_line``). ``due_plans`` are the plans whose time came, for a
    turn one woke Domo for (spec 6.9), and ``woke_today`` how many times he woke by himself for
    the person today. ``changes`` is what someone else changed for them since Domo last spoke
    with them (``conversation.changes``). ``trust`` is what the person's word has been worth so
    far (``trust.shown.trust_shown``, spec 6.4), ``None`` when it could not be read. ``aside`` are
    the tools Domo made for them, or that they asked for, that are set aside, with why
    (``madetools.offered.set_aside_for``). ``tool_wakes`` are what tools of Domo woke him with,
    for a turn they woke him for (``conversation.tool_wakes.tool_wake_turn``). ``building`` are the
    builds of tools they opened that are in progress (``madetools.offered.being_built_for``).
    ``speaking`` is who probably speaks, when the code is not sure of it
    (``prompts.speaking_line``), ``None`` when it is."""

    display_name: str
    role: Role
    can_act: bool
    child: bool = False
    calls_domo: str | None = None
    house_calls: str | None = None
    house_called_before: str | None = None
    same_name: Literal["house", "theirs", "both"] | None = None
    writer_time: WriterTime | None = None
    ownership: OwnershipNote | None = None
    successors: tuple[JsonObject, ...] | None = None
    promises: tuple[JsonObject, ...] = ()
    directives: tuple[JsonObject, ...] = ()
    owed: tuple[JsonObject, ...] = ()
    owed_more: int = 0
    today: Today | None = None
    household: tuple[HouseholdLine, ...] = ()
    known: tuple[JsonObject, ...] = ()
    nicknames: tuple[tuple[str, str], ...] = ()
    memory: MemoryBlock = NO_MEMORY
    plans: tuple[PlanLine, ...] = ()
    spending: tuple[SpendingNote, ...] = ()
    newcomers: tuple[NewcomerNote, ...] = ()
    keys: KeysNote | None = None
    models: ModelsNote | None = None
    lessons: tuple[LessonNote, ...] = ()
    went_wrong: tuple[WentWrongShown, ...] = ()
    notes_to_tell: tuple[NoteToTell, ...] = ()
    arrival: ArrivalNote | None = None
    unanswered: tuple[UnansweredMoment, ...] = ()
    upkeep: str | None = None
    offered: frozenset[str] = frozenset()
    kept: tuple[str, ...] = ()
    reads: str | None = None
    abilities: Abilities = NOBODY_LISTENS
    due_plans: tuple[DuePlan, ...] = ()
    woke_today: int = 0
    changes: tuple[ChangeLine, ...] = ()
    trust: Trust | None = None
    aside: tuple[ToolAside, ...] = ()
    tool_wakes: tuple[ToolWake, ...] = ()
    building: tuple[BeingBuilt, ...] = ()
    agreements: tuple[AgreementLine, ...] = ()
    expiring: AboutToExpire | None = None
    speaking: str | None = None

    def build(
        self,
        pending: Sequence[PendingRequest],
        *,
        to_approve: Sequence[OthersRequest] = (),
        can_act: bool | None = None,
        decided: Sequence[JustDecided] = (),
    ) -> str:
        if self.upkeep is not None:
            return build_upkeep_prompt(
                display_name=self.display_name,
                language=self.upkeep,
                arrival=self.arrival is not None,
                abilities=self.abilities,
            )
        return build_system_prompt(
            display_name=self.display_name,
            role=self.role,
            can_act=self.can_act if can_act is None else can_act,
            child=self.child,
            calls_domo=self.calls_domo,
            house_calls=self.house_calls,
            house_called_before=self.house_called_before,
            same_name=self.same_name,
            pending=pending,
            writer_time=self.writer_time,
            ownership=self.ownership,
            successors=self.successors,
            promises=self.promises,
            directives=self.directives,
            owed=self.owed,
            owed_more=self.owed_more,
            decided=decided,
            to_approve=to_approve,
            today=self.today,
            household=self.household,
            known=self.known,
            nicknames=self.nicknames,
            memory=self.memory,
            plans=self.plans,
            spending=self.spending,
            newcomers=self.newcomers,
            keys=self.keys,
            models=self.models,
            lessons=self.lessons,
            went_wrong=self.went_wrong,
            notes_to_tell=self.notes_to_tell,
            arrival=self.arrival,
            unanswered=self.unanswered,
            offered=self.offered,
            kept=self.kept,
            reads=self.reads,
            abilities=self.abilities,
            due_plans=self.due_plans,
            woke_today=self.woke_today,
            changes=self.changes,
            trust=self.trust,
            aside=self.aside,
            tool_wakes=self.tool_wakes,
            building=self.building,
            agreements=self.agreements,
            expiring=self.expiring,
            speaking=self.speaking,
        )


def prompted_as(prompt: Prompt, actor: Actor) -> bool:
    """Whether ``prompt`` already tells who ``actor`` is now: their role and their child mark
    (a change of either makes the rest of the turn that of who they are now)."""
    return prompt.role == actor.role and prompt.child == actor.child


@dataclass(frozen=True)
class Answer:
    """The person's words of the turn (``text``) and the requests they may answer.

    ``pending`` are the requests that wait for their own answer in this conversation (oldest
    first, each saying what it asks), ``settings`` the owner's settings read with them, and
    ``last_reply_id`` the reply that came just before the person's message. ``to_approve`` are the
    requests of others that wait for the person's yes (spec 5.1), those the one who asked nudged
    first (spec 5.2), then the oldest, each with the display name of who asked. ``paused`` are the
    person's chains in this conversation that paused until someone's arrival, and ``waiting_for``
    whom each waits for (by id); ``waiting_for_others`` the person's requests here that wait for
    someone else's yes (spec 5.2): Domo is told of both, words never answer them. ``steps`` are
    the steps of the chains among them, numbered sentences, by id (``describe.steps_told_of``).
    ``earlier`` are the messages of the conversation before the person's, oldest first, each as
    its id, its role and its words: the reader of their reply is given those they wrote since the
    requests it reads were asked (``Turn._said_since``, ruling R59)."""

    text: str
    pending: Sequence[Confirmation]
    settings: PolicySettings
    last_reply_id: uuid.UUID | None
    paused: Sequence[Confirmation] = ()
    waiting_for: Mapping[uuid.UUID, str] = field(default_factory=lambda: {})
    to_approve: Sequence[tuple[Confirmation, str]] = ()
    steps: Mapping[uuid.UUID, tuple[str, ...]] = field(default_factory=lambda: {})
    waiting_for_others: Sequence[Confirmation] = ()
    earlier: Sequence[tuple[uuid.UUID, str, str]] = ()


@dataclass(frozen=True)
class Prepared:
    """What ``prepare_turn`` read for a turn: whose it is (``person_id``) and the reply it writes,
    the key it goes on (``provider``; ``blocked`` says how it ends at once when no key may take it,
    with no model asked), the prompt, the history, the tools and their context, the person's words
    and the requests they may answer, the longest answer, the person's texts (for the sentence that
    may answer in Domo's stead), and what kind of turn it is: a wake (``decided``), an arrival
    (``arrival``), one that may end in silence (``silent_ok``), with the kind of task of its main
    calls (``main_task``). ``unmarked`` are the news told once that it was given without marking
    them, and ``news_seen`` the news for the person as it read them."""

    person_id: uuid.UUID
    reply_id: uuid.UUID
    provider: Provider
    prompt: Prompt
    history: list[ChatMessage]
    tools: Sequence[ToolDefinition]
    tool_context: ToolContext
    answer: Answer
    max_output_tokens: int
    texts: Texts = REFERENCE
    catalog: Catalog | None = None
    blocked: ErrorKind | None = None
    decided: JustDecided | None = None
    silent_ok: bool = False
    main_task: TaskKind = "conversation"
    arrival: bool = False
    unmarked: News | None = None
    news_seen: datetime | None = None


# What a yes that was not enough lacked: a firm yes, or a proof.
Needed = Literal["explicit_yes", "proof"]


@dataclass(frozen=True)
class _Read:
    """What the person's message came to, read as an answer to the requests the reply just
    before showed: what Domo is told of the requests it decided (``decided``, in the order they
    were decided), with the sentences that answer in his stead if he cannot (``fallback``); the
    requests it answered with a yes and a change (``changed``), with the right to apply that change
    in this turn (``change_grant``) when it was the one request shown; and those it answered with
    a yes that did not give what they ask (``not_enough``, each with what it asks then); and
    those it answered with a no the judge did not take for the person's (``no_not_taken``: they
    still wait). Nothing set: it answered no request.

    A ``fallback`` without ``decided`` is a decision that was carried out but that Domo could
    not be told of: the sentences answer alone, he is not asked."""

    decided: tuple[JustDecided, ...] = ()
    fallback: str | None = None
    changed: tuple[uuid.UUID, ...] = ()
    change_grant: uuid.UUID | None = None
    not_enough: tuple[tuple[uuid.UUID, Needed], ...] = ()
    no_not_taken: tuple[uuid.UUID, ...] = ()

    def needed_by(self, c: Confirmation) -> Needed | None:
        """What ``c`` asks that this message's yes did not give; ``None`` when it gave it, or
        did not answer ``c``."""
        return next((needed for rid, needed in self.not_enough if rid == c.id), None)

    def answer_with(self, c: Confirmation) -> str | None:
        """How ``c`` is answered when this message's yes did not give what it asks (its own
        request's words), or after a no that was not taken (``NO_NOT_TAKEN``, then how it is
        answered); ``None`` for any other request."""
        if c.id in self.no_not_taken:
            return f"{NO_NOT_TAKEN}; it is answered with {how_to_answer(c)}"
        needed = self.needed_by(c)
        if needed is None:
            return None
        return ANSWER_WITH_A_PROOF if needed == "proof" else ANSWER_EXPLICITLY

    def approve_with(self, c: Confirmation) -> str | None:
        """The same, for a request of someone else the person answers as its approver."""
        if c.id in self.no_not_taken:
            return f"{NO_NOT_TAKEN}; it is answered with {how_to_approve(ask_of(c))}"
        needed = self.needed_by(c)
        if needed is None:
            return None
        if needed == "proof":
            return ANSWER_AS_APPROVER_WITH_A_PROOF
        return ANSWER_AS_APPROVER_EXPLICITLY


@dataclass
class _SoFar:
    """What a turn has come to so far: the text received, the status it would be stored with
    (``partial`` until the model's turn ends, as when the core stops it halfway), why it failed,
    the usage, whether the model answered without tools (D6: it could not say it told a note), and
    whether it acted (an action that is never shown does not count: ``catalog.SILENT_ACTIONS``),
    and whether the model's answer stopped at the token limit.

    ``rounds_at`` are where each round of calls after the first began to speak, as indices of
    ``received``, and ``new_round`` says the next words begin a round; ``words_end`` is where the
    model's words end and a sentence of the code begins (``None``: none was added). The stored
    reply says each thing once (``text``, ``conversation.once``)."""

    received: list[str] = field(default_factory=lambda: [])
    status: ReplyStatus = "partial"
    failure: ErrorKind | None = None
    usage: ChatUsage | None = None
    without_tools: bool = False
    acted: bool = False
    cut_short: bool = False
    rounds_at: list[int] = field(default_factory=lambda: [])
    new_round: bool = False
    words_end: int | None = None

    def text(self) -> str:
        """The text the reply is stored with: the model's words, a thing said once across the
        rounds of the answer (``said_once``: what was streamed live cannot be taken back), then
        the sentences of the code, as they are."""
        end = len(self.received) if self.words_end is None else self.words_end
        bounds = [0, *(at for at in self.rounds_at if at < end), end]
        rounds = ["".join(self.received[a:b]) for a, b in zip(bounds, bounds[1:], strict=False)]
        return said_once(rounds) + "".join(self.received[end:])


class _Joined:
    """The listener of a turn (``primary``) and those who hear it besides (``also``: joined
    while it runs, for words of theirs the turn answers, ``Turn.hear_also``). Each event goes to
    the others first, then to ``primary``, as it would alone (whether someone reads, an error it
    raises). What the others do never stops the turn: one that fails is dropped; what is shown
    only once stays the primary's; the end is given to them apart (``spawn``), so that a voice
    still saying the reply never holds the turn back."""

    def __init__(
        self,
        primary: Listener,
        also: list[Listener],
        spawn: Callable[[Coroutine[Any, Any, None]], object],
    ) -> None:
        self._primary = primary
        self._also = also
        self._spawn = spawn

    @property
    def reading(self) -> bool:
        return self._primary.reading

    async def open(self) -> None:
        await self._primary.open()

    async def emit(self, event: Event) -> None:
        for other in list(self._also):
            if isinstance(event, Acted) and event.record.card_once is not None:
                continue
            if isinstance(event, Ended):
                self._also.remove(other)
                self._spawn(_given_apart(other, event))
                continue
            try:
                await other.emit(event)
            except Exception as error:
                if not isinstance(error, ListenerGone):
                    logger.error("A listener besides failed (%s)", type(error).__name__)
                self._also.remove(other)
        await self._primary.emit(event)


async def _given_apart(listener: Listener, event: Event) -> None:
    """Give ``event`` to ``listener``, never raising: what fails is logged by its kind."""
    try:
        await listener.emit(event)
    except ListenerGone:
        pass
    except Exception as error:
        logger.error("A listener besides failed (%s)", type(error).__name__)


def _decided_card(
    request: Confirmation, outcome: DecideOutcome, record: ActionRecord | None
) -> JsonObject:
    """The card the deciding reply keeps of a decision, from what is in hand: what came of the
    call (never its ``card_once``), or why a yes was refused, or that the request was refused."""
    if record is not None:
        return dict(record.card)
    if outcome == "failed":
        return dict(failed_record(request.tool_name, action_type_of(request)).card)
    return {"key": "confirmation.card.refused", "params": {}}


class Turn:
    """One turn, prepared (``Prepared``), to be run once (``run``). See the module docstring."""

    def __init__(self, deps: "TurnDeps", prepared: Prepared) -> None:
        self._deps = deps
        self._p = prepared
        # The context of the turn's calls: given the turn's models once the words are read, and a
        # change to apply when they asked for one; told when nobody reads any more.
        self._ctx = prepared.tool_context
        # Whoever listens, once the turn runs, and who hears it besides (``hear_also``).
        self._listener: Listener | None = None
        self._also: list[Listener] = []
        # How the turn ended, once it ran, whether it acted (an action that is never shown does
        # not count), and whether such an action took effect (done, or waiting for a yes).
        self._ended: TurnEnd = "failed"
        self._acted = False
        self._took_effect = False
        # Whether anything of the turn is left once it ran: words kept in its reply, or an action
        # record of any kind, a silent one included (``left_anything``).
        self._any_action = False
        self._left_anything = False
        # Whether a call of the turn kept or changed a memory: its vector is computed once the turn
        # is over (``_after_memories``).
        self._memories_written = False
        # The models of the turn, on its key, each call counted, the main calls of the kind
        # ``main_task``. The size of the request of the main calls is told to it once the text
        # they are sent is built (``_write``).
        self._models = ModelPicker(
            session_factory=deps.session_factory,
            model_factory=deps.model_factory,
            provider=prepared.provider,
            person_id=prepared.person_id,
            clock=prepared.tool_context.clock,
            main_task=prepared.main_task,
            catalog=prepared.catalog,
            # Someone's first call on a key another gave is news for the giver.
            notify=prepared.tool_context.notify,
        )

    async def run(self, listener: Listener) -> TurnEnd:
        """Run the turn, giving its events to ``listener``; let go of the claim on the reply
        however it ends. How it ended: in words, in silence, or it failed. However it ends, but
        stopped with the core (cancelled), what the person wrote meanwhile and it did not read is
        answered after it (``_answer_what_was_kept``); stopping, nothing new is started, and it
        stays stored, as after a restart."""
        listener = _Joined(listener, self._also, self._deps.wakes.spawn)
        self._listener = listener
        stopped = False
        try:
            if self._p.blocked is not None:
                await self._refuse(listener, self._p.blocked)
            else:
                await self._write(listener)
        except anyio.get_cancelled_exc_class():
            stopped = True
            raise
        finally:
            self._deps.streaming_replies.discard(self._p.reply_id)
            self._after_memories()
            # Those who heard it besides and were not given its end (the turn's own listener left
            # before it): it is over for them too.
            while self._also:
                self._deps.wakes.spawn(_given_apart(self._also.pop(), Ended("partial")))
            if self._ctx.forgotten_in_turn:
                self._after_forgetting()
            elif not stopped:
                self._answer_what_was_kept()
        return self._ended

    def _after_forgetting(self) -> None:
        """Once a turn in which the person was forgotten at their own request is over (its reply
        settled, its journal lines written): what it ran in goes, in a task of the application
        (``people.forget.finish_after_the_turn``), and what they wrote meanwhile is never
        answered."""
        # Imported here, not at the top: forgetting reaches the household, which reads turns.
        from domo_core.people.forget import finish_after_the_turn

        self._deps.kept.take(self._ctx.conversation_id)
        self._deps.wakes.spawn(
            finish_after_the_turn(
                self._deps.session_factory,
                sorted(self._ctx.forgotten_in_turn),
                self._ctx.clock(),
            )
        )

    async def hear_also(self, requests: Sequence["TurnRequest"]) -> None:
        """Words of ``requests`` are answered by this turn's reply: whoever else they say hears
        it (``TurnRequest.also_hears``) is given its events from now on, beside the turn's own
        listener. Never raises: one that cannot be had is logged by its kind and left out."""
        for request in requests:
            if request.also_hears is None:
                continue
            try:
                other = await request.also_hears(self._p.reply_id)
                if other is not None:
                    await other.open()
                    self._also.append(other)
            except Exception as error:
                logger.error("A listener besides could not be had (%s)", type(error).__name__)

    def _answer_what_was_kept(self) -> None:
        """Once the turn is over (its reply settled, its claim let go): what the person wrote
        meanwhile that it had no round left to read is answered by a reply of its own, in a task
        of the application (``Conversation.answer_kept``, spec 11.3)."""
        conversation_id = self._ctx.conversation_id
        taken = self._deps.kept.take(conversation_id)
        if not taken:
            return
        # Imported here, not at the top: the conversation prepares its turns.
        from domo_core.conversation.service import Conversation

        self._deps.wakes.spawn(
            Conversation(self._deps).answer_kept(self._p.person_id, conversation_id, taken)
        )

    async def _read_what_was_kept(self) -> list[str]:
        """What the person wrote in this conversation while the turn ran, oldest first: taken
        (``TurnDeps.kept``) and read as they stored it, theirs alone. From then on the reply
        answers the last of them (``ToolContext.reply_message_id``, ``said``): their words, never
        words from outside. When they cannot be read, they stay kept, for after the turn.

        In a turn that answers a message of theirs, the reply answers the last of them from then
        on (``ToolContext.reply_message_id``), and their texts are added to what they said in the
        turn (``said_later``: an exact passage of any one of their messages is theirs, never one
        across two). In a turn Domo
        started himself, nothing of whose acts those are changes (ruling R61): he is only handed
        their words.

        A turn of someone heard at a page signed in as another person knows only what was said
        there: what they wrote meanwhile (elsewhere, with their whole context) is not read by it
        nor said at that page; it stays kept, answered after it by a turn of their own
        (``_answer_what_was_kept``)."""
        deps = self._deps
        speaker = self._ctx.speaker
        if speaker is not None and speaker.page_person_id is not None:
            return []
        conversation_id = self._ctx.conversation_id
        taken = deps.kept.take(conversation_id)
        if not taken:
            return []
        try:
            async with deps.session_factory() as session:
                found = await session.execute(
                    select(Message.id, Message.content).where(
                        Message.id.in_([message_id for message_id, _ in taken]),
                        Message.conversation_id == conversation_id,
                        Message.role == "user",
                        Message.provenance == "person",
                    )
                )
                stored: dict[uuid.UUID, str] = {row.id: row.content for row in found.all()}
        except Exception as error:
            logger.error(
                "What was written during a turn could not be read (%s)", type(error).__name__
            )
            for message_id, request in taken:
                deps.kept.keep(conversation_id, message_id, request)
            return []
        theirs = [message_id for message_id, _ in taken if message_id in stored]
        if theirs:
            # Someone spoke while the turn ran, in every copy of its context.
            self._ctx.heard_meanwhile.raised = True
        # Whoever spoke them hears what answers them from now on.
        joined = [request for message_id, request in taken if message_id in stored]
        await self.hear_also(joined)
        # From now on the turn is as sure as the least sure of those whose words it read
        # (ruling C1): an unsure or too short voice joined makes every decision after it doubted.
        actor, speaker = joined_by(
            self._ctx.actor, self._ctx.speaker, [request.speaker for request in joined]
        )
        self._ctx = replace(self._ctx, actor=actor, speaker=speaker)
        ctx = self._ctx
        if theirs and ctx.reply_message_id is not None:
            texts = tuple(stored[message_id] for message_id in theirs)
            self._ctx = replace(
                ctx, reply_message_id=theirs[-1], said_later=(*ctx.said_later, *texts)
            )
        return [stored[message_id] for message_id in theirs]

    @property
    def blocked(self) -> ErrorKind | None:
        """How the turn would end at once, with no model asked, when no key of the person may
        take it (``Prepared.blocked``); ``None`` when one may."""
        return self._p.blocked

    @property
    def acted(self) -> bool:
        """Whether the turn, once it ran, took an action that is shown (an action that is never
        shown, ``policy.catalog.SILENT_ACTIONS``, does not count), whatever came of it."""
        return self._acted

    @property
    def took_effect(self) -> bool:
        """Whether an action of the turn that is shown took effect: it was done, or it waits for
        a yes (a request was made). One that was refused, invalid or failed did not."""
        return self._took_effect

    @property
    def left_anything(self) -> bool:
        """Whether anything of the turn got out once it ran: words of Domo kept in its reply
        (even one that ended in error), or an action record of any kind, a silent one included (a
        tool of Domo launched). A turn that failed with nothing of it out may be tried again
        without telling or doing anything twice."""
        return self._left_anything

    def _after_memories(self) -> None:
        """Once a turn that kept or changed a memory is over, the vectors the memories lack are
        computed in a task of the application (``TurnDeps.wakes``), never in the turn: the local
        model may be slow, and a person's reply never waits for it. Nothing without a model.

        The memories the turn gave Domo (its context's, and what his searches found) are marked
        as served in such a task too: a write that fails never fails the turn. A turn no model was
        asked for (``Prepared.blocked``) gave nothing."""
        if self._memories_written and self._deps.embedder.name:
            self._deps.wakes.spawn(memory_vectors(self._deps.session_factory, self._deps.embedder))
        if self._p.blocked is not None:
            return
        given = {uuid.UUID(memory_id) for memory_id in self._p.prompt.memory.given}
        given |= self._ctx.memories_found
        if given:
            self._deps.wakes.spawn(
                memories_served(self._deps.session_factory, sorted(given), self._ctx.clock())
            )

    async def _refuse(self, listener: Listener, failure: ErrorKind) -> None:
        """A turn that no key of the person may take: each one they may use is at the end of a
        limit, or has no model. No model is asked, not even to read the person's words (a request
        waits for its button): the reply is the fixed message of ``failure``, stored as a failed
        turn (taken away for a turn that may stay silent, as when its model fails)."""
        try:
            await listener.open()
        finally:
            await self._settle("", "error", None, dropped=self._p.silent_ok, failure=failure)
        self._log_failure(failure)
        await listener.emit(self._failed(failure))
        await listener.emit(Ended("error"))

    def _failed(self, reason: FailureReason) -> Failed:
        """The event of a failure: with the key of the turn when the person may give it again (they
        gave it, and it has a secret: only the giver gives a key again, spec 4.2)."""
        key = self._models.provider
        mendable = key.given_by == self._p.person_id and key.secret_ref is not None
        return Failed(reason, provider_id=key.id if mendable else None)

    def _mind_the_reader(self) -> None:
        """Before an action: once nobody reads the turn any more (the page left), what the action
        would show only once is left on the person's card (``ToolContext.no_reader``), and Domo
        is told it waits there."""
        if self._listener is not None and not self._listener.reading:
            self._ctx.no_reader = True

    async def _write(self, listener: Listener) -> None:
        so_far = _SoFar()
        # What the model writes is given and kept without its markdown marks, piece by piece.
        plain = PlainStream()
        # What the model's turn came to, when the sentence of the catalog answered in its stead.
        replaced: ReplyStatus | None = None
        read: _Read | None = None
        # Whether Domo answered in words, and whether the code says he told the notes he was
        # given (``_after_words``).
        notes_told_by_the_code = spoke = False
        try:
            try:
                await listener.open()
                # A wake has no words of the person to read: what was decided is told as given.
                read = (
                    _Read(decided=(self._p.decided,))
                    if self._p.decided is not None
                    else await self._read_the_answer(listener)
                )
                if not read.decided and read.fallback is not None:
                    # A decision was carried out but Domo could not be told of it: asking him to
                    # answer blind is worse than the sentence, which answers alone below.
                    so_far.status, so_far.failure = "error", "upstream"
                else:
                    await self._talk(listener, read, so_far, plain)
            except ListenerGone:
                raise
            except Exception as error:
                # A bug, or a model that raised something no adapter should let through. Only
                # the kind of error is logged: the details of a failed call may hold text.
                logger.error("A chat turn failed unexpectedly (%s)", type(error).__name__)
                so_far.status, so_far.failure = "error", "upstream"
            # What was kept back of the last pieces (stars that closed nothing, say).
            rest = plain.flush()
            if rest:
                so_far.received.append(rest)
                await listener.emit(Words(rest))
            # The sentences of the code come after this: they are never compared.
            so_far.words_end = len(so_far.received)
            silent = not "".join(so_far.received).strip()
            spoke = so_far.status == "complete" and not silent
            # Domo answered in words, and could not say he told a note: the code says so.
            notes_told_by_the_code = spoke and (so_far.without_tools or not self._tells_notes)
            if (
                read is not None
                and read.fallback is not None
                and (so_far.failure is not None or (silent and not so_far.acted))
            ):
                # The decision was carried out and its card given, but Domo did not say so (the
                # model failed, in whatever way, or said nothing at all): the sentence of the
                # catalog answers, after what he managed to write, if any.
                came_to, so_far.status = so_far.status, "partial"
                sentence = read.fallback if silent else f"{_FALLBACK_SEPARATOR}{read.fallback}"
                so_far.received.append(sentence)
                try:
                    await listener.emit(Words(sentence))
                except ListenerGone:
                    raise
                except Exception as error:
                    logger.error("The fixed sentence could not be sent (%s)", type(error).__name__)
                    so_far.status, so_far.failure = "error", "upstream"
                else:
                    so_far.status, so_far.failure, replaced = "complete", None, came_to
            if so_far.cut_short and so_far.status == "complete" and not self._p.silent_ok:
                # The answer stopped at the token limit, and more room did not help: what Domo
                # was writing is lost, a call included. The code says so, after what he managed
                # to write, if any: never an empty reply that looks like an answer. A turn that
                # may stay silent (a wake, an arrival) stays as it is. This sentence of the code
                # does not count as Domo speaking: only what he wrote before it does (``spoke``,
                # read above), and the notes and news are marked told only then.
                sentence = self._p.texts("chat.cut_short")
                if "".join(so_far.received).strip():
                    sentence = f"{_FALLBACK_SEPARATOR}{sentence}"
                so_far.received.append(sentence)
                try:
                    await listener.emit(Words(sentence))
                except ListenerGone:
                    raise
                except Exception as error:
                    logger.error("The fixed sentence could not be sent (%s)", type(error).__name__)
        finally:
            self._acted = so_far.acted
            text = so_far.text()
            self._left_anything = self._any_action or bool(text.strip())
            # A turn that may stay silent, and did: nothing written, nothing done. An arrival
            # stopped before its end (``partial``: the person's message stopped it, or the core)
            # that did nothing goes too, whatever it began to say: the person's words come next,
            # and they answer what was shown before it (R74).
            stopped = self._p.arrival and so_far.status == "partial"
            dropped = self._p.silent_ok and not so_far.acted and (stopped or not text.strip())
            await self._settle(
                text,
                so_far.status,
                so_far.usage,
                replaced=replaced,
                dropped=dropped,
                failure=so_far.failure if so_far.status == "error" else None,
            )
        status, failure = so_far.status, so_far.failure
        self._ended = "spoke" if spoke else "silent" if status == "complete" else "failed"
        await self._after_words(spoke=spoke, notes_told_by_the_code=notes_told_by_the_code)
        if failure is not None:
            self._log_failure(failure)
            await listener.emit(self._failed(failure))
        await listener.emit(Ended(status))

    async def _talk(
        self, listener: Listener, read: _Read, so_far: _SoFar, plain: PlainStream
    ) -> None:
        """Have Domo answer (``chat.agent.run_turn``), giving what he says and does as it comes,
        told of the requests that wait as the person's words left them (``read``). What the
        model's turn comes to is kept in ``so_far`` as it comes, so that a turn stopped halfway
        stores what it had."""
        if read.decided and self._p.decided is None:
            # Their words just decided a request, after the turn was prepared: what it did may
            # have changed who they are (the hand-over of the owner's role).
            await self._as_they_are_now()
        pending = self._pending_requests(read)
        others = self._requests_of_others(read)
        # The models of the turn, for what calls one apart (the yes judge, the rights writer).
        # Not before: a yes that the person's words carried out runs without them.
        self._ctx = replace(self._ctx, models=self._models)
        if read.change_grant is not None:
            # The person answered that request, the one shown, with a change: Domo may apply it,
            # in this turn only (the context is the turn's), once.
            self._ctx = replace(self._ctx, change_grant=read.change_grant)
        system = self._p.prompt.build(pending, to_approve=others, decided=read.decided)
        # The main calls are sent that text and the history: the size of their request, which a
        # model's known context must hold.
        self._models.min_context = estimate_tokens(
            len(system) + sum(len(message.content) for message in self._p.history)
        )
        turn = run_turn(
            models=self._models,
            system_prompt=system,
            history=self._p.history,
            max_output_tokens=self._p.max_output_tokens,
            tools=self._p.tools,
            handle_tool_call=self._handle if self._p.tools else None,
            system_prompt_without_tools=self._p.prompt.build(
                pending, to_approve=others, can_act=False, decided=read.decided
            ),
            # What the person writes meanwhile, read between two rounds (spec 11.3).
            between_rounds=self._read_what_was_kept,
        )
        # What waits behind a failed attempt the next round may make good (spec 6.7).
        held = HeldBack()
        async with aclosing(turn) as events:
            async for event in events:
                match event:
                    case TextDelta(text=raw):
                        piece = plain.feed(storable(raw))
                        if piece:
                            if so_far.new_round:
                                # The first words of a round that follows a round of calls.
                                so_far.rounds_at.append(len(so_far.received))
                                so_far.new_round = False
                            so_far.received.append(piece)
                            await self._let_out(listener, held.words(Words(piece)))
                    case ActionTaken(record=record, round=round_, closes_round=closes):
                        self._any_action = True
                        if action_shown(record.action_type or "", record.status):
                            # What is never shown does not count: a turn that may stay silent
                            # and only kept a memory leaves no reply (the audit log keeps it).
                            so_far.acted = True
                            if record.status in ("done", "waiting"):
                                self._took_effect = True
                        if record.status == "done" and record.action_type in _WRITES_MEMORY:
                            self._memories_written = True
                        if closes:
                            so_far.new_round = True
                        given = held.action(record, round=round_, closes_round=closes)
                        await self._let_out(listener, given)
                    case TurnDone(usage=reported, without_tools=refused, cut_short=cut):
                        so_far.status, so_far.usage = "complete", reported
                        so_far.without_tools, so_far.cut_short = refused, cut
                    case TurnFailed(kind=kind):
                        so_far.status, so_far.failure = "error", kind
        # What is still held goes out once the answer is over, in its order.
        await self._let_out(listener, held.end())
        # What the turn learned and has not written yet (it never raises).
        await self._models.write_lessons()

    async def _as_they_are_now(self) -> None:
        """Read the person again once their words decided a request (``read_the_person_again``:
        the context of the turn's calls is theirs as they are now). When their role changed (they
        handed the owner's role over), the rest of the turn is the turn of who they are now:
        Domo is told that role and offered its tools, and what is told to the owner alone (the
        writer's time, the note of a new owner, the successors) is no longer told. The turn was
        prepared before the yes: in the owner's trial of 0.8.0, Domo, still told "owner" and
        offered the hand-over, called it again, and the barrier refused it. The requests of
        others that wait for their yes are read again too: those that waited for the owner are
        no longer theirs to answer. What is told to the person whatever their role (the keys they
        may use, where their limits stand, the model of the turn) stays as it was read.

        When they are no longer in the household at all (what they asked removed them: "give my
        role to Alex and remove me"), nothing of the household is theirs any more: the rest of
        the turn has no tool, and nothing of what was read for them before (``_as_someone_gone``).

        A failure is logged (its kind only) and leaves the turn as it was prepared: the barrier
        reads the person again at each call all the same."""
        ctx = self._ctx
        try:
            match await read_the_person_again(ctx):
                case "gone":
                    self._as_someone_gone()
                    return
                case "unknown":
                    return
                case "active":
                    pass
            role = ctx.actor.role
            if prompted_as(self._p.prompt, ctx.actor) or self._p.prompt.upkeep is not None:
                return
            async with self._deps.session_factory() as session:
                rights = await load_rights(session)
                to_approve, _ = await awaiting_approval(
                    session,
                    ctx.person,
                    self._p.answer.settings,
                    ctx.clock(),
                    limit=MAX_APPROVALS_SHOWN,
                )
        except Exception as error:
            logger.error("The person's role could not be read again (%s)", type(error).__name__)
            return
        tools = tools_for(ctx.actor, ctx.registry, ctx.policy, rights=rights)
        kept = tools_kept_from(ctx.actor, ctx.registry, ctx.policy, rights=rights)
        prompt = replace(
            self._p.prompt,
            role=role,
            child=ctx.actor.child,
            can_act=bool(tools),
            offered=frozenset(tool.name for tool in tools),
            kept=kept,
        )
        if role != "owner":
            prompt = replace(
                prompt,
                writer_time=None,
                ownership=None,
                successors=None,
                promises=(),
                directives=for_others(prompt.directives),
                owed=(),
                owed_more=0,
            )
        self._p = replace(
            self._p,
            prompt=prompt,
            tools=tools,
            answer=replace(self._p.answer, to_approve=to_approve),
        )

    def _as_someone_gone(self) -> None:
        """The rest of the turn, for someone who is no longer in the household: Domo may still
        say what was just done, with no tool to call (each would be refused, ``not_allowed``),
        and without what was read for them while they were in it: what is the owner's alone, the
        keys and where their limits stand, who began using a key of theirs, what was learned of
        the models, the notes left for them (not told, so not marked as told) and the requests
        of others that waited for their yes."""
        prompt = replace(
            self._p.prompt,
            can_act=False,
            offered=frozenset(),
            kept=(),
            writer_time=None,
            ownership=None,
            successors=None,
            promises=(),
            directives=(),
            owed=(),
            owed_more=0,
            spending=(),
            newcomers=(),
            keys=None,
            lessons=(),
            notes_to_tell=(),
            aside=(),
        )
        self._p = replace(
            self._p,
            prompt=prompt,
            tools=(),
            answer=replace(self._p.answer, to_approve=()),
            unmarked=None,
        )

    async def _let_out(self, listener: Listener, held: Sequence[Given]) -> None:
        """Give what may go out now (``HeldBack``), in its order: the words as they came, each
        action as ``_show`` gives it."""
        for item in held:
            if isinstance(item, Words):
                await listener.emit(item)
            else:
                await self._show(listener, item)

    async def _show(self, listener: Listener, record: ActionRecord) -> None:
        """Give the action of ``record``. Its head is written first, when Domo writes such cards
        on the moment (``TurnDeps.card_heads``): a request's for each of its readers, the person of
        this turn waiting a moment at most for theirs; an action's for the person of this turn. An
        action that is never shown (``policy.catalog.SILENT_ACTIONS``) gives nothing."""
        if not action_shown(record.action_type or "", record.status):
            # Never shown, whatever came of it (spec 6.2): no head asked of a model, no event.
            return
        ctx = self._ctx
        heads = self._deps.card_heads
        if heads is not None and record.confirmation_id is not None:
            await heads.of_request(record.confirmation_id, now=ctx.clock(), wait_for=ctx.person.id)
        elif heads is not None:
            await heads.of_action(record, reader=ctx.person, texts=self._p.texts)
        await listener.emit(Acted(record))

    async def _after_words(self, *, spoke: bool, notes_told_by_the_code: bool) -> None:
        """What a turn that answered in words counts as told: the notes it was given, when Domo
        could not say so himself; the news told once of an arrival; the news it read."""
        if notes_told_by_the_code and self._p.prompt.notes_to_tell:
            await self._mark_notes_told()
        if spoke and self._p.unmarked is not None:
            await self._mark_news_told(self._p.unmarked)
        owed = [uuid.UUID(str(row["act_id"])) for row in self._p.prompt.owed]
        if spoke and (self._p.news_seen is not None or owed):
            await clear_news(
                self._deps.session_factory,
                self._p.person_id,
                seen=self._p.news_seen,
                told_acts=owed,
                now=self._ctx.clock(),
            )

    def _log_failure(self, failure: ErrorKind) -> None:
        """Nobody reads the events of a wake (one that presents a request again included), nor of
        an arrival: their failure would be seen nowhere, so it is logged (its kind only)."""
        if self._p.decided is not None:
            logger.warning("A wake of the assistant ended in error (%s)", failure)
        elif self._p.arrival:
            logger.warning("An arrival turn of the assistant ended in error (%s)", failure)

    @property
    def _tells_notes(self) -> bool:
        """Whether Domo may say himself, in this turn, that he told a note (``note_told`` is
        among the tools offered)."""
        return any(tool.name == _NOTE_TOLD_TOOL for tool in self._p.tools)

    async def _mark_notes_told(self) -> None:
        """The notes the turn was given count as told, as ``note_told`` would have said
        (``tools.notes.mark_told``). A failure is logged (its kind only): the notes are given again
        at the next turn."""
        try:
            await mark_told(
                self._deps.session_factory,
                person_id=self._ctx.person.id,
                note_ids=[note.id for note in self._p.prompt.notes_to_tell],
                now=self._ctx.clock(),
            )
        except Exception as error:
            logger.error("The notes told could not be marked (%s)", type(error).__name__)

    async def _mark_news_told(self, news: News) -> None:
        """The thresholds and the newcomers the turn was given without marking them count as told
        now. A failure is logged (its kind only): they are given again at the next turn."""
        try:
            async with self._deps.session_factory() as session:
                now = self._ctx.clock()
                await mark_thresholds_told(session, news.limits, now=now)
                await mark_newcomers_told(session, news.newcomers, now=now)
                await session.commit()
        except Exception as error:
            logger.error("The news told could not be marked (%s)", type(error).__name__)

    def _pending_requests(self, read: _Read) -> list[PendingRequest]:
        """The requests waiting for the person, as Domo is told about them (in English, the
        prompt's language): who says yes and how, and what the person's message did to them
        (``read``); then the person's chains that paused until someone's arrival, each with whom it
        waits for (nothing answers them); then the person's requests that wait for someone else's
        yes (spec 5.2), told without the words of a note (``description_for_a_model``, D4). What an
        approver said when they asked again with a change comes with each (``approver_note``,
        data). The request just decided is not among them (``prompts.JustDecided``)."""
        now = self._ctx.clock()
        answer = self._p.answer
        decided = {d.request_id for d in read.decided}
        waiting = [
            PendingRequest(
                request_id=str(c.id),
                description=request_description(c, REFERENCE),
                created_at=waiting_since(c.created_at, now),
                reminders=c.reminder_count,
                answered_with_change=c.id in read.changed,
                answered_by=who_answers(c),
                answer_with=read.answer_with(c) or how_to_answer(c),
                plain_yes_not_enough=read.needed_by(c) is not None,
                asked_by=c.asked_by,
                ask_why=c.ask_why,
                steps=answer.steps.get(c.id, ()),
                approver_note=c.approver_note,
            )
            for c in answer.pending
            if str(c.id) not in decided
        ]
        paused = [
            PendingRequest(
                request_id=str(c.id),
                description=request_description(c, REFERENCE),
                created_at=waiting_since(c.created_at, now),
                reminders=c.reminder_count,
                answered_by=who_answers(c),
                answer_with=ANSWER_WHEN_THEY_JOIN,
                waiting_for_arrival_of=answer.waiting_for.get(c.id),
                steps=answer.steps.get(c.id, ()),
                approver_note=c.approver_note,
            )
            for c in answer.paused
            if str(c.id) not in decided
        ]
        for_others = [
            PendingRequest(
                request_id=str(c.id),
                description=description_for_a_model(c),
                created_at=waiting_since(c.created_at, now),
                reminders=c.reminder_count,
                answered_by=who_answers(c),
                answer_with=how_to_answer(c),
                asked_by=c.asked_by,
                ask_why=c.ask_why,
                steps=answer.steps.get(c.id, ()),
                nudged=c.nudged_at is not None,
                approver_note=c.approver_note,
            )
            for c in answer.waiting_for_others
            if str(c.id) not in decided
        ]
        return waiting + paused + for_others

    def _requests_of_others(self, read: _Read) -> list[OthersRequest]:
        """The requests of others that wait for the person's yes (spec 5.1), as Domo is told
        about them, those the one who asked nudged first: who asked, what it does without the words
        of a note (D4), its steps, how the person answers it and what their message did to it
        (``read``). The request just decided is not among them."""
        now = self._ctx.clock()
        decided = {d.request_id for d in read.decided}
        return [
            OthersRequest(
                request_id=str(c.id),
                requester=requester,
                description=description_for_a_model(c),
                created_at=waiting_since(c.created_at, now),
                reminders=c.reminder_count,
                answered_with_change=c.id in read.changed,
                nudged=still_nudged(c),
                answer_with=read.approve_with(c) or how_to_approve(ask_of(c)),
                plain_yes_not_enough=read.needed_by(c) is not None,
                asked_by=c.asked_by,
                ask_why=c.ask_why,
                steps=self._p.answer.steps.get(c.id, ()),
            )
            for c, requester in self._p.answer.to_approve
            if str(c.id) not in decided
        ]

    async def _read_the_answer(self, listener: Listener) -> _Read:
        """Read the person's message as an answer to the requests the reply just before showed,
        and carry out each yes or no, in the order shown (see the module docstring).

        Words answer a request only when the reply just before the person's message showed it
        (R74): their own in this conversation (``last_shown_message_id``), and those of others
        that wait for their yes, which only a reply of theirs shows
        (``shown_to_approver_message_id``, spec 5.1); never a chain paused until someone's
        arrival. Every request shown is read, numbered as shown (``confirmations.intent``: "yes,
        both" answers two, "yes, the first one" one), and who may decide each is the code's to say
        (``decide_confirmation``), never the reader's. A yes with a change is taken only when one
        request was shown; with several, none is decided and Domo is told the words asked a change
        of them, which he asks about. A yes that does not give what a request asks is an answer
        all the same: this turn's reply presents that request again (to the approver, for someone
        else's), and Domo is told that the yes was not enough. Someone else's request that the
        words decided wakes Domo in the conversation of the one who asked (``_wake``), but a
        request of Domo's own waiting in this very conversation, which this reply tells
        (``_told_here``)."""
        answer = self._p.answer
        last_reply = answer.last_reply_id
        if last_reply is None:
            return _Read()
        own = [c for c in answer.pending if c.last_shown_message_id == last_reply]
        of_others = [
            c for c, _ in answer.to_approve if c.shown_to_approver_message_id == last_reply
        ]
        shown = own + of_others
        if not shown:
            return _Read()
        # Someone else's requests, which the person answers as their approver.
        as_approver = {c.id for c in of_others}
        told = [
            ShownRequest(
                number=number,
                says=description_for_a_model(c),
                # A raise for a doubted yes binds only a yes that is not sure (case 10).
                ask=asked_of_a_yes(c, self._ctx.actor.trusted),
                asked_by=asked_by_of(c),
            )
            for number, c in enumerate(shown, start=1)
        ]
        # The reading of a yes in words never takes a model under medium: without one, the words
        # are unclear and the requests wait for their buttons. A phrase the code knows, that says
        # all there is to say of the one request shown, needs none.
        model = await self._models.model_for("intent") if reader_needed(answer.text, told) else None
        speaker = self._ctx.speaker
        readings = await read_replies(
            answer.text,
            told,
            speaker=None if speaker is None else speaker.as_json(self._ctx.clock()),
            before=self._said_since(shown),
            model=model,
        )
        if any(reading.verdict == "change" for reading in readings):
            if len(shown) == 1:
                return _Read(changed=(shown[0].id,), change_grant=shown[0].id)
            # Which request the change is for, and what it is, is Domo's to ask: none is taken.
            return _Read(changed=tuple(c.id for c in shown))
        decided: list[JustDecided] = []
        sentences: list[str] = []
        not_enough: list[tuple[uuid.UUID, Needed]] = []
        no_not_taken: list[uuid.UUID] = []
        untold = False
        # Whether a decision was carried out already: a later one that fails leaves it told.
        carried_out = False
        for request, reading in zip(shown, readings, strict=True):
            theirs_to_approve = request.id in as_approver
            if reading.verdict == "approve" and reading.source is not None:
                asks = await self._who_agrees_weighed(request)
                given = what_the_yes_gave(reading.source)
                if asks is not None and most_demanding(asks, given) != given:
                    # Not this person's agreement by itself: the request now asks what the
                    # judge asks of them, and is shown again.
                    await self._ask_at_least(request.id, asks)
                    await self._present_again(request.id, as_approver=theirs_to_approve)
                    needs: Literal["needs_explicit", "needs_proof"] = (
                        "needs_proof" if asks == "proof" else "needs_explicit"
                    )
                    # Told as a decision that needs more: a channel that hears the next words
                    # as a proof (the voice) opens its moment now, before the reply ends.
                    await listener.emit(Decided(request.id, None, needs))
                    not_enough.append((request.id, "proof" if asks == "proof" else "explicit_yes"))
                    continue
            if reading.not_enough:
                await self._present_again(request.id, as_approver=theirs_to_approve)
                needed = asked_of_a_yes(request, self._ctx.actor.trusted)
                not_enough.append((request.id, "proof" if needed == "proof" else "explicit_yes"))
                continue
            if reading.verdict not in ("approve", "refuse") or reading.source is None:
                continue
            if reading.verdict == "refuse" and not await self._a_no_taken(
                request, as_approver=theirs_to_approve
            ):
                # Not taken for theirs: nothing is refused, the request waits and is shown again,
                # and the assistant may ask them again (the owner, 2026-11-27).
                await self._present_again(request.id, as_approver=theirs_to_approve)
                no_not_taken.append(request.id)
                continue
            try:
                outcome, record = await self._decide_and_record(
                    request, approve=reading.verdict == "approve", via=reading.source
                )
            except Exception as error:
                if not carried_out:
                    # Nothing was decided yet: the turn fails as it always did.
                    raise
                # What was decided before stays decided, and is told; this one is not. Only
                # the decision itself is contained: telling one that was made fails the turn.
                logger.error(
                    "A request answered in words could not be decided (%s)",
                    type(error).__name__,
                )
                continue
            carried_out = True
            # Someone else's request, or one made in someone else's turn that the person it
            # concerns answered here, wakes its turn, as a button does.
            theirs = request.requester_id != self._ctx.person.id
            if (theirs_to_approve or theirs) and not self._told_here(request):
                # The one who asked is woken, as after a button.
                self._wake(request.id, outcome, record)
            if outcome in ("needs_explicit", "needs_proof"):
                # What the request asks was raised after the words were read (a rule added
                # since): the same yes that is not enough.
                await self._present_again(request.id, as_approver=theirs_to_approve)
                await listener.emit(Decided(request.id, record, outcome))
                not_enough.append(
                    (request.id, "proof" if outcome == "needs_proof" else "explicit_yes")
                )
                continue
            rerouted = (
                _REROUTED_REPLIES.get(record.card.get("approver"))
                if outcome == "approver_changed" and record is not None
                else None
            )
            if outcome not in _DECIDED_REPLIES and outcome != "failed" and rerouted is None:
                continue
            ctx = self._ctx
            # Still waiting (for someone else's yes, say): its heads are written for what it is
            # now.
            if self._deps.card_heads is not None:
                self._deps.card_heads.ask_for_request(request.id, clock=ctx.clock)
            await listener.emit(Decided(request.id, record, outcome))
            # The sentence is chosen first: it answers alone if Domo cannot be told
            # (``_told_of``).
            sentences.append(await self._sentence_of(request, outcome, record, rerouted))
            said = await self._told_of(request, outcome, record)
            if said is None:
                untold = True
            else:
                decided.append(said)
        # The same sentence for two decisions is said once.
        fallback = " ".join(dict.fromkeys(sentences)) if sentences else None
        if untold:
            # A decision Domo cannot be told of: the sentences answer alone, he is not asked.
            return _Read(fallback=fallback)
        return _Read(
            decided=tuple(decided),
            fallback=fallback,
            not_enough=tuple(not_enough),
            no_not_taken=tuple(no_not_taken),
        )

    async def _who_agrees_weighed(self, request: Confirmation) -> Ask | None:
        """What an agreement in words to ``request`` asks more, because the code is not sure that
        the one who said it is the person of the turn (``Actor.trusted``: unsure, or a voice too
        short to tell): ``None`` for a sure speaker, and for what is ordinary as the barrier reads
        it (``policy.decision.doubted``: the owner's ``"judge"`` setting included). Otherwise the
        judge weighs who agrees from the clues (``consult_the_judge`` with the doubt on who asks):
        what his verdict asks (a ``do_not`` asks a proof: the request stays theirs); silent, a
        proof. A request whose action cannot be read asks a proof."""
        ctx = self._ctx
        if ctx.actor.trusted:
            return None
        try:
            action = DescribedAction.from_json(request.action)
        except ValueError:
            return "proof"
        async with ctx.session_factory() as session:
            if not await doubted(session, ctx.actor, action, ctx.policy):
                return None
            told = await request_told(session, request, before=(), after=(), now=ctx.clock())
        judgement = await consult_the_judge(
            self._weighing(),
            actor=ctx.actor,
            action=action,
            says=description_for_a_model(request),
            rights=RightsSay(allowed=True, needs=None, question=None),
            request=told,
            identity_doubted=True,
        )
        return "proof" if judgement.verdict == "do_not" else judgement.as_ask()

    async def _a_no_taken(self, request: Confirmation, *, as_approver: bool) -> bool:
        """Whether a no in words to ``request`` is taken for the one whose answer it is, the
        mirror of ``_who_agrees_weighed`` (the owner, 2026-11-27: "the assistant judges"): at once
        from a speaker the code is sure of; otherwise (unsure, a voice too short to tell, to be
        learnt again, at another person's page) the judge weighs, with the clues of the speaker,
        that dropping it is really their wish (``confirmation.expire`` on that request). Only his
        ``do`` takes it; any other verdict, his silence or a judge that fails leaves it waiting
        (a waiting request does nothing by itself: it lapses or is answered again)."""
        ctx = self._ctx
        if ctx.actor.trusted:
            return True
        action = DescribedAction(
            connector="core",
            action_type="confirmation.expire",
            target=f"confirmation:{request.id}",
            effect="own_requests",
            destination="household",
        )
        whose = (
            "a request of someone else that waits for their yes"
            if as_approver
            else "their own waiting request"
        )
        judgement = await consult_the_judge(
            self._weighing(),
            actor=ctx.actor,
            action=action,
            says=f"Refuses with a no in words {whose}, which would: "
            + description_for_a_model(request),
            rights=RightsSay(allowed=True, needs=None, question=None),
            identity_doubted=True,
        )
        return judgement.verdict == "do" and judgement.fallback is None

    def _weighing(self) -> ToolContext:
        """The turn's context with its models, for the judge that weighs who said a yes or a no
        in words while the answer is read: the turn's own context gets them only after (a yes
        carried out runs without them), and without them the judge would always be silent."""
        return replace(self._ctx, models=self._models)

    async def _ask_at_least(self, confirmation_id: uuid.UUID, asks: Ask) -> None:
        """The request ``confirmation_id``, while it waits, asks at least ``asks``, as the judge
        asked it (``asked_by`` ``judge``, no reason kept). What a rule asked before is not kept
        beside it: the request has one record of who asks, and the rule is read again at the yes
        (``decide_confirmation``)."""
        async with self._deps.session_factory() as session:
            c = await session.get(Confirmation, confirmation_id, with_for_update=True)
            if c is None or c.status != "pending":
                return
            stored = ask_of(c)
            if most_demanding(stored, asks) == stored:
                return
            if c.ask_before_doubt is None:
                # What it asked before a doubt raised it: a later sure yes is held to that.
                c.ask_before_doubt = stored
            c.ask = asks
            c.asked_by = "judge"
            c.ask_why = None
            await session.commit()

    def _said_since(self, shown: Sequence[Confirmation]) -> tuple[str, ...]:
        """The person's own messages since the requests ``shown`` were asked, oldest first, but
        the one this turn answers: the reader of their reply may read the last few of them
        (``confirmations.intent.READ_BEFORE``). Words said before a request existed never count
        for it (ruling R59); with several requests, since the latest was asked (R60). A request is
        dated by the reply that asked it, in this conversation; one asked elsewhere (someone
        else's, or made in someone else's turn) by the reply here that showed it last, so only
        the person's words since then count. Only their words: never Domo's, nor someone
        else's."""
        earlier = self._p.answer.earlier
        position = {message_id: at for at, (message_id, _, _) in enumerate(earlier)}
        here = self._ctx.conversation_id

        def asked_at(c: Confirmation) -> int:
            if c.conversation_id == here and c.message_id is not None:
                # Asked here, before the messages kept: all of them come after it.
                return position.get(c.message_id, -1)
            shown_here = c.shown_to_approver_message_id or c.last_shown_message_id
            return position.get(shown_here, len(earlier)) if shown_here else len(earlier)

        # Words must come after every request they could make firm (ruling R60).
        since = max(asked_at(c) for c in shown)
        return tuple(words for _, role, words in earlier[since + 1 :] if role == "user")

    async def _sentence_of(
        self,
        request: Confirmation,
        outcome: DecideOutcome,
        record: ActionRecord | None,
        rerouted: str | None,
    ) -> str:
        """The sentence of the catalog that answers a decision in Domo's stead: for a call that
        failed, what its card says now (the request read again, as the decision left it)."""
        texts = self._p.texts
        if outcome == "failed":
            async with self._deps.session_factory() as session:
                now = (await confirmations_by_id(session, [request.id]))[request.id]
            said = card_sentence(now.result or {}, texts) if now.status == "failed" else None
            return said or texts("action.card.failed")
        if rerouted is not None:
            # Nothing was done: the request now waits for someone else's yes (Ruling R27).
            return texts(rerouted)
        reason = record.card.get("reason") if record is not None else None
        key = (
            f"confirmation.reply.{reason}"
            if outcome == "refused" and reason in _REFUSAL_REPLIES
            else _DECIDED_REPLIES[outcome]
        )
        return texts(key)

    async def _told_of(
        self, request: Confirmation, outcome: DecideOutcome, record: ActionRecord | None
    ) -> JustDecided | None:
        """What Domo is told of the request the person's words just decided
        (``confirmations.chain.just_decided``), the request read again as the database holds it
        after the decision. ``None`` if that cannot be read (logged by its kind only): the
        sentence of the catalog answers."""
        try:
            async with self._deps.session_factory() as session:
                decided = (await confirmations_by_id(session, [request.id]))[request.id]
                return await just_decided(
                    session,
                    decided,
                    outcome=outcome,
                    record=record,
                    decider=self._ctx.person,
                    via="words",
                    woken=False,
                )
        except Exception as error:
            logger.error("What was just decided could not be read (%s)", type(error).__name__)
            return None

    def _told_here(self, request: Confirmation) -> bool:
        """Whether the decision of ``request`` in this turn is told by this turn's reply alone: a
        request Domo asked himself (``Confirmation.by_domo``), waiting in this very
        conversation, the one of the person who answered it. Its asker is Domo answering them
        here: nobody else is to be woken for it (fix round 4 of step 2's Task 5). A request of
        someone else, or one decided from elsewhere, still wakes Domo where it was asked."""
        return told_here(
            request, conversation_id=self._ctx.conversation_id, person_id=self._ctx.person.id
        )

    def _wake(
        self,
        confirmation_id: uuid.UUID,
        outcome: DecideOutcome | AmendOutcome,
        record: ActionRecord | None,
    ) -> None:
        """Wake Domo in the conversation of the one who asked for the request the person of this
        turn decided in words as its approver (spec 5.1), as a button does: only when the turn was
        given a way to (``ToolContext.wake``: never a wake's), and only for an outcome that decided
        something (``starts.spawn_wake`` checks it)."""
        ctx = self._ctx
        if ctx.wake is not None:
            ctx.wake(confirmation_id, outcome, record, ctx.person)

    async def _present_again(
        self, confirmation_id: uuid.UUID, *, as_approver: bool = False
    ) -> None:
        """Record that this turn's reply presents the request again (``mark_shown``; for someone
        else's request answered ``as_approver``, ``mark_shown_to_approver``): the person's answer
        to it may decide the request. A failure is logged; the button still works."""
        mark = mark_shown_to_approver if as_approver else mark_shown
        with anyio.move_on_after(_SETTLE_SECONDS, shield=True):
            try:
                async with self._deps.session_factory() as session:
                    await mark(session, confirmation_id, self._p.reply_id)
                    await session.commit()
                return
            except Exception as error:
                logger.error("A request could not be marked as shown (%s)", type(error).__name__)
                return
        logger.error("Marking a request as shown took too long")

    async def _decide_and_record(
        self, request: Confirmation, *, approve: bool, via: ReplySource
    ) -> tuple[DecideOutcome, ActionRecord | None]:
        """``decide_confirmation`` for the person's words, and the reply's record of it, as one
        step shielded from the cancellation of the turn (the core stopping): a yes that was read is
        carried out and recorded before anything else. A no keeps their message as the words they
        refused it with (``Confirmation.refusal_words``). A failure of the request was noted when
        it was settled: its record, which names the request, notes none again."""
        self._mind_the_reader()
        ctx = self._ctx
        with anyio.CancelScope(shield=True):
            outcome, record = await decide_confirmation(
                session_factory=self._deps.session_factory,
                confirmation_id=request.id,
                decider=ctx.person,
                approve=approve,
                via=via,
                reply_message_id=ctx.reply_message_id,
                ctx_factory=requester_context(ctx),
                registry=ctx.registry,
                policy=ctx.policy,
                now=ctx.clock(),
                words=self._p.answer.text,
                # A yes of a voice the code is not sure of is held to a raise of its doubt.
                sure=ctx.actor.trusted,
            )
            action_status = _DECIDED_ACTIONS.get(outcome)
            if action_status is not None:
                await store_action(
                    ctx,
                    self._p.reply_id,
                    ActionRecord(
                        tool_name=request.tool_name,
                        action_type=action_type_of(request),
                        status=action_status,
                        confirmation_id=request.id,
                        model_result={},
                        card=_decided_card(request, outcome, record),
                    ),
                )
            return outcome, record
        # A shield without a deadline that nobody cancels never stops what it holds.
        raise AssertionError("unreachable")

    async def _handle(self, call: ToolCall, *, over_limit: bool = False) -> ActionRecord:
        """Handle a call of the model (``conversation.execution.handle_tool_call``) to its end,
        even if the turn is cancelled meanwhile (the core stopping): an action that ran is recorded
        and audited. Each step of it has a time limit of its own, so the shield cannot hold the
        turn for ever. A call made once nobody reads leaves what it would show only once on the
        person's card (``_mind_the_reader``)."""
        self._mind_the_reader()
        with anyio.CancelScope(shield=True):
            return await handle_tool_call(
                call, ctx=self._ctx, reply_id=self._p.reply_id, over_limit=over_limit
            )
        # A shield without a deadline that nobody cancels never stops what it holds.
        raise AssertionError("unreachable")

    async def _settle(
        self,
        content: str,
        status: ReplyStatus,
        usage: ChatUsage | None,
        *,
        replaced: ReplyStatus | None = None,
        dropped: bool = False,
        failure: ErrorKind | None = None,
    ) -> None:
        """Store the end of the turn: the reply and its audit entry, in one transaction. When the
        sentence of the catalog answered in the model's stead (``replaced``: what the model's turn
        came to), the audit entry keeps that status and says ``"fallback": true``; the reply is
        stored as it is (``status``), with why it failed for one that ends in error
        (``failure``). A turn that stayed silent (``dropped``) takes its reply away instead
        (``ConversationStore.drop_reply``), and the audit entry says ``"status": "silent"`` (or the
        status the turn came to, when it did not complete); unless a request names the reply (it
        showed one again), which keeps it: it is then stored as it is.

        The audit entry is only written when this turn is the one that settled the reply.

        Each attempt is shielded from cancellation and has a time limit of its own. Should the
        first one fail or hang (the audit log is locked, say), a second one settles the reply
        without the audit entry: a reply must not stay ``streaming`` once its turn is over, it
        would block the conversation for two minutes."""
        came_to: str = status if replaced is None else replaced
        if dropped and status == "complete":
            came_to = "silent"
        # The model of the conversation's last call: the one that wrote the reply (none when no
        # call found a model).
        route = self._models.conversation_route
        model = None if route is None else route.model_id
        details: dict[str, str | int | bool | None] = {
            # No key when none could take the turn.
            "provider_id": None if self._p.blocked is not None else self._models.provider.id,
            "model": model,
            "level": None if route is None else route.level,
            "status": came_to,
            "input_tokens": 0 if usage is None else usage.input_tokens,
            "output_tokens": 0 if usage is None else usage.output_tokens,
            # What every call of the turn cost on Domo's account (the judge's, the reading's
            # and the writer's included), and whether tokens of one were estimated.
            "cost_usd": f"{self._models.cost_usd:.6f}",
            "estimated": self._models.estimated,
        }
        if replaced is not None:
            details["fallback"] = True
        for audited in (True, False):
            stored = False
            with anyio.move_on_after(_SETTLE_SECONDS, shield=True) as attempt:
                try:
                    async with self._deps.session_factory() as session:
                        store = ConversationStore(session)
                        settled, kept = False, not dropped
                        if dropped:
                            try:
                                async with session.begin_nested():
                                    settled = await store.drop_reply(self._p.reply_id)
                            except IntegrityError:
                                # A request names the reply (an arrival stopped as it showed one
                                # again, before its card was stored): it stays, ended as it
                                # stands, as ``visits.step_aside`` keeps it.
                                kept = True
                        if kept:
                            settled = await store.finish_message(
                                self._p.reply_id,
                                content=content,
                                status=status,
                                model=model,
                                failure=failure,
                                # His own answer to their message, with nothing from outside in
                                # the turn, what a tool he made or a program said included
                                # (``answers_them``, read once the whole turn ran): the yes-judge
                                # and the review of what was said read any other reply as relayed.
                                own_answer=self._ctx.answers_them,
                            )
                        if audited and settled:
                            # Last, just before the commit: the audit log holds a lock until the
                            # transaction ends.
                            await append_audit(
                                session,
                                actor=f"person:{self._p.person_id}",
                                action="chat.turn",
                                details=details,
                            )
                        await session.commit()
                    stored = True
                except Exception as error:
                    logger.error(
                        "The end of a chat turn could not be stored%s (%s)",
                        " with its audit entry" if audited else "",
                        type(error).__name__,
                    )
            if stored:
                return
            if attempt.cancelled_caught:
                logger.error(
                    "The end of a chat turn took too long to store%s",
                    ", trying again without its audit entry" if audited else "",
                )
