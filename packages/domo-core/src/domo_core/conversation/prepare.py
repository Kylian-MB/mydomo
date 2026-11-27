"""Preparing a turn: everything it needs, read before any model is called, for any channel.

``prepare_turn`` is given the reply a turn writes (``streaming``, claimed by the caller in
``TurnDeps.streaming_replies``: the turn lets go of it when it ends) and what the channel says of
the person and of itself (``TurnRequest``: who speaks, what the channel can do, the address the
person reached Domo at). It reads, in a transaction of its own that is over before the model is
called, what the turn needs, and gives back the ``conversation.turn.Turn`` to run. No connection
is kept while the model thinks. Nothing here knows a page: the person's texts are read from the
database (``texts.view.person_texts``), never from a request.

Who speaks
----------

The speaker the channel gives is completed with the clues kept for the person on that channel and
device (``identity.clues.kept_clues``: a proof that succeeded lately). The barrier sees the person
as the speaker makes them (``speaker.actor_of``: trusted when the code is sure of them), and the
yes-judge reads the speaker (``ToolContext.speaker``).

What the model is shown
-----------------------

The conversation without the replies that failed or said nothing, with the messages of one side
that are then next to each other made into one (a conversation sent to a model alternates). It
ends with the person's turn: a wake, which no message of the person follows, ends it with a fixed
line of the code (``WAKE_MARKER``), an arrival with another (``ARRIVAL_MARKER``), a turn a plan
of Domo woke him for with a third (``PLAN_MARKER``), a turn a tool of his woke him for with a
fourth (``TOOL_MARKER``), never with what was decided, what is new, what is due or what a tool
says (that is in the context, as data). Between turns the calls are not
replayed: each reply sent back carries one line ``[action <type>: <status>]`` per action it took
(decision D7, ``action_line``). The line of an action that asked for a yes (stored ``waiting``)
says what became of its request since (R73), with the note of an approver who decided it with one
(``approver_note``, spec 5.1), as data. An action given up on because it took too long reads
``failed (outcome unknown)`` (R71).

The key and the money
---------------------

Every call of the turn goes on one key, chosen among the person's (``router.usable_keys``) rank by
rank (``router.choose_key``): a key with no margin left under one of its limits for the request's
first call is passed over, and the context says so (``prompts.KeysNote``). When no key is left,
the turn calls no model: its reply is the fixed message of ``limit_reached`` or ``no_model``. The
person who set limits is told where each stands (a threshold passed being marked as told), and who
began using a key they gave (once). An arrival, which may say nothing, is told those without
marking them: they count as told once it answered in words. The levers Domo chose on the key (a
shorter history, shorter answers, how much of his memory a turn gives him) apply, held in their
bounds.

Someone without a key
---------------------

A person who has no key Domo may use for them is answered by an upkeep turn on the general key,
when the rules of the house let it serve for that (``upkeep_key``): its instructions are
``prompts/upkeep.md``, it is sent their last message alone (``ARRIVAL_MARKER`` at their arrival),
answers in ``UPKEEP_OUTPUT_TOKENS`` at most, and its one tool is ``key_add``. ``NoKey`` is raised
otherwise, and for a wake.

The context
-----------
The requests that wait for the person's own answer in this conversation, those of others that wait
for their yes (spec 5.1), their chains paused until someone's arrival and their requests that wait
for someone else's yes (spec 5.2); the notes left for them that Domo has not told yet (spec 4.8);
the moments he could not answer them since his last complete reply (spec 4.10); what Domo knows
(spec 6.4, ``memory_shown``): his core, and what the person's last messages bring back, with why he
began the turn first when he did (``recall_query``), never what he keeps to himself of someone else,
which never makes the turn one of words from outside (``ToolContext.told_outside``), and which an
upkeep turn is not given; what the person's word has been worth so far, and next to each unconfirmed
memory what the word of who said it has (spec 6.4, ``trust.shown``: data, never read by the barrier;
the yes-judge weighs the facts in its own case); the plans Domo set for the person that wait (spec
6.9, ``plans_shown``), theirs alone; and, for the owner alone, the writer's time, the note of a role
handed over and the list of successors. An arrival (spec 4.9) is offered the person's tools, as any
turn of theirs: each call goes through the barrier and the judge as any call of a turn Domo started.
The ready tools Domo made that the person may see (theirs and the house's) join the tools of the
code as ``t_<name>`` (``madetools.offered.offers``: the barrier says who may use one), and the
context names those of theirs, or that they asked for, that are set aside, with why
(``madetools.offered.set_aside_for``), and the builds they opened that are in progress
(``madetools.offered.being_built_for``).

What a channel can do decides what a tool may show: a channel that cannot show something once to
the person alone (``Abilities.shows_once``) has it left on their card (``ToolContext.no_reader``),
and one line of the context tells Domo how he is heard here (``prompts.abilities_line``).
"""

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from functools import partial
from itertools import takewhile
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.passkeys import PasskeySite
from domo_core.chat.context_window import merge_consecutive_roles, select_history
from domo_core.chat.models import Message, MessageAction
from domo_core.chat.store import ConversationStore
from domo_core.config import CoreSettings
from domo_core.confirmations.describe import steps_told_of, waiting_names_of
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import (
    MAX_APPROVALS_SHOWN,
    action_type_of,
    awaiting_approval,
    confirmations_by_id,
    pending_for,
    request_state,
)
from domo_core.confirmations.store import paused_chains_of, pending_for_others
from domo_core.conversation.changes import changes_for
from domo_core.conversation.channel import Abilities
from domo_core.conversation.kept import KeptMessages
from domo_core.conversation.speaker import Speaker, actor_of
from domo_core.conversation.turn import Answer, CardHeads, News, Prepared, Prompt, Turn
from domo_core.conversation.visits import ArrivalTurns, unanswered_in, untold_decision
from domo_core.db import SessionFactory, read_or_nothing
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.household import nicknames
from domo_core.household.service import name_in_use, ownership_note
from domo_core.household.succession import list_successors
from domo_core.identity.clues import kept_clues
from domo_core.learning.shown import went_wrong_shown
from domo_core.llm.catalog import Catalog
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.choices import (
    MIN_HISTORY_CHARS,
    MIN_OUTPUT_TOKENS,
    Choices,
    bounded,
    load_choices,
    warn_thresholds,
)
from domo_core.llm.costs import estimate_tokens, max_cost_usd
from domo_core.llm.factory import ModelFactory
from domo_core.llm.lessons import recent_lessons
from domo_core.llm.listing import ModelLister
from domo_core.llm.models import Provider
from domo_core.llm.newcomers import newcomers_told
from domo_core.llm.router import KeyChoice, Route, choose_key, key_label, usable_keys
from domo_core.llm.spending import LimitNote, at_limit, limit_notes
from domo_core.llm.types import ChatMessage, ErrorKind
from domo_core.madetools.launch import LaunchDeps
from domo_core.madetools.offered import being_built_for, offer_named, offers, set_aside_for
from domo_core.madetools.risks import operations_of, reaches_told
from domo_core.madetools.service import ready_tools
from domo_core.madetools.settings import SETTINGS, value
from domo_core.memory.embedder import NO_EMBEDDER, Embedder
from domo_core.memory.models import Memory
from domo_core.memory.search import recalled
from domo_core.memory.service import core_of, source_of, told_unconfirmed, who_said
from domo_core.plans.models import Plan
from domo_core.plans.schedule import as_local
from domo_core.plans.service import builds_waited_on, waiting_of
from domo_core.policy.catalog import PolicyTables, on_his_own_tables, shown
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Role, as_role
from domo_core.policy.settings import PolicySettings, load_policy_settings
from domo_core.prompts import (
    AboutToExpire,
    ArrivalNote,
    DuePlan,
    HouseholdLine,
    JustDecided,
    KeyShown,
    KeysNote,
    LessonNote,
    MemoryBlock,
    MemoryLine,
    ModelsNote,
    NewcomerNote,
    PlanLine,
    SpendingNote,
    ToolWake,
    WriterTime,
    today_of,
)
from domo_core.sandbox_client import NO_SANDBOX, Sandbox
from domo_core.texts.scheduler import TextWriting
from domo_core.texts.view import Texts, person_texts
from domo_core.tools.agreements import agreements_of
from domo_core.tools.context import Notify, ToolContext, WakeStarter, tools_kept_from
from domo_core.tools.notes import notes_to_tell
from domo_core.tools.registry import InternalTool, tools_for
from domo_core.tools.rights_tools import writer_timeouts
from domo_core.toolsdb.store import ToolsBase
from domo_core.trust.service import Trust
from domo_core.trust.shown import trust_shown

if TYPE_CHECKING:
    # Only for the types of ``TurnDeps``: the turns Domo starts are prepared here.
    from domo_core.conversation.listeners import Listeners
    from domo_core.conversation.starts import Wakes

logger = logging.getLogger(__name__)

# The clock a turn reads the time from, and the tools it may offer, by the name the model uses.
Clock = Callable[[], datetime]
ToolRegistry = Mapping[str, InternalTool]

# The last message of the person in the conversation a wake sends to the model, which must end
# with the person's turn (a provider continues a final reply of Domo, or refuses it). A fixed
# line of the code: what was decided is told in the context, as data, never here.
WAKE_MARKER: Final = "[a request of yours was just decided: see the context]"
# The person's turn of the conversation an arrival turn sends (``starts``): they just came back
# and wrote nothing. A fixed line of the code, never shown: what is new is told in the
# context, as data.
ARRIVAL_MARKER: Final = "[they just came back: see the context]"
# The person's turn of the conversation a turn a plan of Domo woke him for sends
# (``starts.plan_turn``, spec 6.9): nobody wrote anything. A fixed line of the code, never shown:
# the plans due are told in the context, as data.
PLAN_MARKER: Final = "[a plan of yours is due: see the context]"
# The person's turn of the conversation a turn a tool of Domo woke him for sends
# (``tool_wakes.tool_wake_turn``, spec 5.4): nobody wrote anything. A fixed line of the code, never
# shown: what the tool says is in the context, as data.
TOOL_MARKER: Final = "[a tool of yours has something for you: see the context]"
# The person's turn of the conversation a turn a request about to lapse woke him for sends
# (``starts.wake_before_expiry``, spec 11.2): nobody wrote anything. A fixed line of the code, never
# shown: the request is told in the context, as data.
EXPIRY_MARKER: Final = "[a request is about to lapse: see the context]"
# The longest answer of an upkeep turn, in tokens: a sentence or two that asks for a key.
UPKEEP_OUTPUT_TOKENS: Final = 300
# The one tool of an upkeep turn: the form of a key of one's own.
_UPKEEP_TOOL: Final = "key_add"
# What the line of an action says when the action was given up on because it took too long: it
# may have happened all the same, and the later turns must keep that doubt (R71).
_OUTCOME_UNKNOWN: Final = "failed (outcome unknown)"
_NOTE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# How many of the person's last messages, and of their characters at most (the latest kept), the
# recall of a turn starts from (spec 6.4).
RECALL_MESSAGES: Final = 3
RECALL_QUERY_CHARS: Final = 1000
# How many of the person's plans the context of a turn gives, the soonest first (spec 6.9).
PLANS_SHOWN: Final = 10
# The actions on a note left for the person (``tools.notes``): their line in the history names the
# note, so that Domo may show its exact words once it left his context.
_NOTE_ACTIONS: Final = frozenset({"note.told", "note.show"})


@dataclass(frozen=True)
class TurnDeps:
    """What every turn needs of the application, whatever the channel: its database, how a model
    is built and its models listed, the replies being written (``streaming_replies``: one process
    writes them, claimed there), the settings, the gatekeeper, the clock, the tools and the
    catalog of the barrier (``policy``: each turn lays the open kinds over it once,
    ``policy.open_catalog.load_policy``, and keeps that in ``ToolContext.policy``), what writes
    people's texts (``None``: nothing is written), the tasks
    the turns run in (``wakes``), what writes the heads of the cards a turn shows (``None``: none
    is written), the arrival turns that run (``None``: Domo is offered none) and whoever
    listens to the people (``listeners``: ``None``, what Domo says on his own goes to nobody as
    it comes). The last three are the application's, shared by every request
    (``conversation.starts``), and so is the local model that gives the meaning of memories
    (``embedder``: ``NO_EMBEDDER``, they are found by their words alone). So is the sandbox that
    runs the code of Domo's tools (``sandbox``: ``NO_SANDBOX``, every call fails as a sandbox that
    is down), and the tools' own database their programs read and write (``tools_base``: ``None``,
    there is none, and each such request is refused). So are the messages people wrote while a
    turn of theirs ran (``kept``, ``conversation.kept``)."""

    session_factory: SessionFactory
    model_factory: ModelFactory
    model_lister: ModelLister
    streaming_replies: set[uuid.UUID]
    settings: CoreSettings
    gatekeeper: GatekeeperClient
    clock: Clock
    registry: ToolRegistry
    policy: PolicyTables
    text_writing: TextWriting | None
    wakes: "Wakes"
    card_heads: CardHeads | None = None
    arrival_turns: ArrivalTurns | None = None
    listeners: "Listeners | None" = None
    embedder: Embedder = NO_EMBEDDER
    sandbox: Sandbox = NO_SANDBOX
    tools_base: ToolsBase | None = None
    kept: KeptMessages = field(default_factory=KeptMessages)


@dataclass(frozen=True)
class TurnRequest:
    """What a channel says of a turn: who speaks (``speaker``), what it can do (``abilities``),
    the address the person reached Domo at (``origin``, for the links of an invitation; ``""``
    when Domo starts the turn) and, for the proofs a request may ask, where the person can give a
    passkey (``passkey_site``; ``None`` off the page)."""

    speaker: Speaker
    abilities: Abilities
    origin: str = ""
    passkey_site: PasskeySite | None = None


class NoKey(Exception):
    """The person has no key Domo may use for them, and no upkeep turn answers them: a turn
    cannot be written (``prepare_turn``)."""


def made_tool_finder(deps: TurnDeps) -> Callable[[str], Awaitable[InternalTool | None]]:
    """What finds a tool of Domo's by the name it is offered under (``t_<name>``), with the
    application's launch deps (``made_tool_deps``): for a turn (``ToolContext.made_tool_named``),
    and for a yes given outside one, by a button, the API or a proof (the stored call of a launch
    that carries a yes, ``confirmations.service.decide_confirmation``)."""
    return partial(offer_named, deps.session_factory, made_tool_deps(deps))


def made_tool_deps(deps: TurnDeps) -> LaunchDeps:
    """What a launch of a tool Domo made needs of the application (``madetools.launch``), with
    what wakes him for someone when a tool asks it: a turn in their conversation
    (``tool_wakes.tool_wake_turn``, whether it ran; on his own for the house), which
    ``madetools.handover`` runs in a task of ``deps.wakes``; the tools' database its program
    reads and writes; and the gatekeeper its requests out go through."""

    async def wake(person_id: uuid.UUID, wakes: Sequence[ToolWake], *, on_his_own: bool) -> bool:
        # Imported here, not at the top: the turns Domo starts are prepared with this module.
        from domo_core.conversation.tool_wakes import tool_wake_turn

        return await tool_wake_turn(deps, person_id, wakes, on_his_own=on_his_own)

    return LaunchDeps(
        session_factory=deps.session_factory,
        sandbox=deps.sandbox,
        clock=deps.clock,
        policy_base=deps.policy,
        wakes=deps.wakes,
        waker=wake,
        tools_base=deps.tools_base,
        gatekeeper=deps.gatekeeper,
    )


async def household_shown(
    session: AsyncSession, person_id: uuid.UUID, now: datetime
) -> tuple[HouseholdLine, ...]:
    """The active people of the household in the order they arrived (the ones
    ``household_list`` calls ``active`` or a guest until a date: not removed, no guest whose stay
    is over), the one with ``person_id`` marked as the person Domo talks with."""
    found = await session.execute(
        select(Person.id, Person.display_name, Person.role)
        .where(
            Person.disabled_at.is_(None),
            or_(Person.expires_at.is_(None), Person.expires_at > now),
        )
        .order_by(Person.created_at)
    )
    return tuple(
        HouseholdLine(name=name, role=as_role(role), is_you=found_id == person_id)
        for found_id, name, role in found.all()
    )


def recall_query(
    messages: Sequence[Message], reply_id: uuid.UUID, turn_reason: str | None = None
) -> str:
    """What the recall of a turn looks for: the person's last ``RECALL_MESSAGES`` messages before
    the reply, oldest first, ``RECALL_QUERY_CHARS`` characters at most (the latest kept). In a
    turn Domo started himself, why he did (``turn_reason``) comes first, and the messages fill
    what is left (LRN-7): what he learned is looked for from why he acts too."""
    said = "\n".join(
        [m.content for m in messages if m.role == "user" and m.id != reply_id][-RECALL_MESSAGES:]
    )
    if not turn_reason:
        return said[-RECALL_QUERY_CHARS:]
    reason = turn_reason[:RECALL_QUERY_CHARS]
    room = RECALL_QUERY_CHARS - len(reason) - 1
    if room <= 0 or not said:
        return reason
    return f"{reason}\n{said[-room:]}"


async def memory_and_trust_shown(
    session: AsyncSession,
    person_id: uuid.UUID,
    query: str,
    *,
    choices: Choices,
    embedder: Embedder,
    now: datetime,
    with_speaker: bool,
    by_name: bool = False,
) -> tuple[MemoryBlock, Trust | None]:
    """What the turn of ``person_id`` gives Domo of his memory (spec 6.4): his core, as much as
    ``choices.memory_core_size`` lets in, and what ``query`` (the person's last messages) finds
    besides, as much as ``choices.memory_recall_size`` lets in (0: nothing of either, and nothing
    said of what did not fit); never what he keeps to himself of someone else
    (``memory.service.visible_to``). Whether the search finds by meaning is what the recall saw of
    the model; without a recall, whether there is one.

    With it, in one read (``trust.shown.trust_shown``), from the household's start
    (``choices.trust_start``): the trust of whoever said each unconfirmed memory of someone else,
    on its line, and, ``with_speaker``, what the person's own word has been worth so far (``None``
    otherwise), for those who are in the household at ``now``; no figure when that read fails.
    ``by_name`` (a review: nobody talks there) gives what the person said under their name, as
    ``who_said`` does then."""
    core: tuple[Memory, ...] = ()
    left_out = 0
    if choices.memory_core_size > 0:
        found = await core_of(session, person_id, max_chars=choices.memory_core_size)
        core, left_out = found.items, found.left_out
    remembered: tuple[Memory, ...] = ()
    by_meaning = bool(embedder.name)
    if choices.memory_recall_size > 0 and query.strip():
        found_now = await recalled(
            session,
            person_id,
            query,
            embedder=embedder,
            max_chars=choices.memory_recall_size,
            exclude={m.id for m in core},
        )
        remembered, by_meaning = found_now.memories, found_now.by_meaning
    said_by = await who_said(session, (*core, *remembered), person_id, by_name=by_name)
    trusts = await trust_shown(
        session,
        (*core, *remembered),
        speaker=person_id,
        with_speaker=with_speaker,
        start=choices.trust_start,
        now=now,
    )

    def line(memory: Memory) -> MemoryLine:
        return MemoryLine(
            id=str(memory.id),
            subject=memory.subject,
            content=memory.content,
            kind=memory.kind,
            said_by=said_by[memory.id],
            when=memory.created_at.astimezone(UTC).isoformat(),
            private=memory.kept_private,
            unconfirmed=told_unconfirmed(memory),
            trust=trusts.of_memories.get(memory.id),
            source=source_of(memory),
        )

    block = MemoryBlock(
        core=tuple(line(m) for m in core),
        recalled=tuple(line(m) for m in remembered),
        left_out=left_out,
        search="full" if by_meaning else "words_only",
    )
    return block, trusts.of_speaker


async def memory_shown(
    session: AsyncSession,
    person_id: uuid.UUID,
    query: str,
    *,
    choices: Choices,
    embedder: Embedder,
    now: datetime,
) -> MemoryBlock:
    """``memory_and_trust_shown`` without the speaker's own figure: what a review of what was said
    with ``person_id`` is given of his memory (``memory.review``). Nobody talks in a review: what
    that person said is under their name."""
    block, _ = await memory_and_trust_shown(
        session,
        person_id,
        query,
        choices=choices,
        embedder=embedder,
        now=now,
        with_speaker=False,
        by_name=True,
    )
    return block


async def plans_shown(
    session: AsyncSession, person_id: uuid.UUID, zone: ZoneInfo | None
) -> tuple[PlanLine, ...]:
    """The plans Domo set for ``person_id`` that wait, as the turn gives them to him (spec 6.9):
    the ``PLANS_SHOWN`` soonest, their times on the household's clock (``zone``, UTC when it has
    none), and for one that waits for the build of a tool, that tool's name. Theirs alone:
    nobody else's plan is in their turn."""
    plans = await waiting_of(session, person_id, limit=PLANS_SHOWN)
    builds = await builds_waited_on(session, plans)
    return tuple(
        PlanLine(
            id=str(plan.id),
            why=plan.why,
            when=as_local(plan.due_at, zone),
            repeat=plan.repeat,
            on_return=plan.on_return,
            after_build=None
            if plan.after_build_id is None or plan.after_build_id not in builds
            else builds[plan.after_build_id][1],
            asked=plan.their_words is not None,
        )
        for plan in plans
    )


async def woke_today(
    session: AsyncSession, person_id: uuid.UUID, now: datetime, zone: ZoneInfo | None
) -> int:
    """How many times Domo woke by himself for ``person_id`` today, on the household's clock
    (``zone``, UTC when it has none): the distinct moments a plan of theirs was last due
    (``plans.last_fired_at``), since midnight. Plans due together in one turn count once."""
    day = today_of(now, zone).day
    midnight = datetime.combine(day, time(), tzinfo=zone or UTC)
    found = await session.scalar(
        select(func.count(distinct(Plan.last_fired_at))).where(
            Plan.person_id == person_id, Plan.last_fired_at >= midnight
        )
    )
    return found or 0


async def upkeep_key(session: AsyncSession) -> Provider | None:
    """The key an upkeep turn goes on: the household's general key, when the rules of the house let
    it serve for that (``keys.upkeep``: it does unless said otherwise); ``None`` otherwise, or when
    there is no general key."""
    if not (await load_policy_settings(session)).upkeep_on_general:
        return None
    found = await session.execute(select(Provider).where(Provider.is_general))
    return found.scalars().first()


def _timed_out(card: Mapping[str, object] | None) -> bool:
    """Whether a stored card is that of an action given up on because it took too long."""
    return card is not None and card.get("reason") == "timeout"


def action_line(action: MessageAction) -> str:
    """The line that sums up an action in the history sent to the model (decision D7). An action
    given up on because it took too long reads ``failed (outcome unknown)``. A note told, or whose
    exact words were shown, names the note (its card does: ``tools.notes``), never its words:
    ``[action note.told: done; note <id>]``."""
    status = action.status
    if status == "failed" and _timed_out(action.summary):
        status = _OUTCOME_UNKNOWN
    told = f"{action.action_type or 'unknown'}: {status}"
    note = (action.summary or {}).get("note") if action.action_type in _NOTE_ACTIONS else None
    if isinstance(note, str) and _NOTE_ID.fullmatch(note):
        return f"[action {told}; note {note}]"
    return f"[action {told}]"


@dataclass(frozen=True)
class _HistoryRequests:
    """The requests the actions of the history name, and what is needed to tell what became of
    them: the owner's settings (the lifetime), the time, and, for those an approver decided with a
    note, who that approver is, as the line tells it (``noted_by``, by request id)."""

    by_id: Mapping[uuid.UUID, Confirmation]
    settings: PolicySettings
    now: datetime
    noted_by: Mapping[uuid.UUID, str] = field(default_factory=lambda: {})

    def line(self, action: MessageAction) -> str:
        """``action_line``, but for the action that asked for a yes, or a chain the judge let
        through that keeps a request for the upkeep (both stored ``waiting``, with their request):
        the request's type, and what became of it since (R73), however it was decided, with the
        note of the approver who decided it, if they left one (F23), as a quoted string (data)."""
        request = self.by_id.get(action.confirmation_id) if action.confirmation_id else None
        if request is None or action.status != "waiting":
            return action_line(action)
        state = request_state(request, self.settings, self.now)
        if state == "failed" and _timed_out(request.result):
            state = _OUTCOME_UNKNOWN
        told = f"{action_type_of(request) or 'unknown'}: {state}"
        who = self.noted_by.get(request.id)
        if who is None or request.approver_note is None:
            return f"[action {told}]"
        note = json.dumps(request.approver_note, ensure_ascii=False)
        return f"[action {told}; {who} who said yes noted (data): {note}]"


async def _who_noted(
    session: AsyncSession, requests: Iterable[Confirmation]
) -> dict[uuid.UUID, str]:
    """For each of ``requests`` that someone else than the one who asked decided with a note
    (``approver_note``: an approver's change, spec 5.1), who that approver is, as the line of the
    history tells it (``the owner``, else ``the adult``, then the name the household shows for
    them, as a quoted string: data), by request id. Reads only."""
    noted = {
        c.id: c.decided_by
        for c in requests
        if c.approver_note is not None
        and c.decided_by is not None
        and c.decided_by != c.requester_id
    }
    if not noted:
        return {}
    found = await session.execute(
        select(Person.id, Person.role, Person.display_name).where(
            Person.id.in_(set(noted.values()))
        )
    )
    people = {person_id: (role, name) for person_id, role, name in found.all()}

    def told(person_id: uuid.UUID) -> str:
        role, name = people.get(person_id, (None, None))
        who = "the owner" if role == "owner" else "the adult"
        return who if name is None else f"{who} {json.dumps(name, ensure_ascii=False)}"

    return {request_id: told(person_id) for request_id, person_id in noted.items()}


def _as_the_model_sees_it(
    message: Message, actions: Sequence[MessageAction], requests: _HistoryRequests
) -> ChatMessage | None:
    """The message as the model is shown it; none for one it must not see: a message without
    text (providers refuse them) and a reply that failed (what it said before is not an answer).
    A reply cut short is an answer, as far as it went. A reply starts with one line per action it
    took, in order (``_HistoryRequests.line``): those stay even when the reply failed. An action
    that is never shown (Domo's memory, ``policy.catalog.SILENT_ACTIONS``) has no line."""
    if message.role == "user":
        return ChatMessage("user", message.content) if message.content else None
    if message.role != "assistant":
        return None
    lines = [
        requests.line(action) for action in actions if shown(action.action_type, action.status)
    ]
    if message.content and message.status != "error":
        lines.append(message.content)
    return ChatMessage("assistant", "\n".join(lines)) if lines else None


def _last_complete_at(messages: Sequence[Message], reply_id: uuid.UUID) -> datetime | None:
    """When Domo's last complete reply before ``reply_id`` was written, among ``messages``
    (``None``: there is none)."""
    found: datetime | None = None
    for message in messages:
        if message.id == reply_id:
            break
        if message.role == "assistant" and message.status == "complete":
            found = message.created_at
    return found


def _reply_before(
    messages: Sequence[Message], person_message_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The id of the last reply of Domo before the person's message ``person_message_id``."""
    found: uuid.UUID | None = None
    for message in messages:
        if message.id == person_message_id:
            return found
        if message.role == "assistant":
            found = message.id
    return None


def _before_message(
    messages: Sequence[Message], person_message_id: uuid.UUID | None
) -> tuple[tuple[uuid.UUID, str, str], ...]:
    """The messages before the person's message ``person_message_id``, oldest first, as their id,
    role and words (none when there is no such message: nobody spoke)."""
    if person_message_id is None:
        return ()
    earlier: list[tuple[uuid.UUID, str, str]] = []
    for message in messages:
        if message.id == person_message_id:
            return tuple(earlier)
        earlier.append((message.id, message.role, str(message.content)))
    return ()


def _person_message_before(messages: Sequence[Message], reply_id: uuid.UUID) -> uuid.UUID | None:
    """The id of the person's message that the reply ``reply_id`` answers: the last one of the
    person before it."""
    found: uuid.UUID | None = None
    for message in messages:
        if message.id == reply_id:
            return found
        if message.role == "user":
            found = message.id
    return found


@dataclass(frozen=True)
class _Money:
    """What a turn knows of the money of its request: the key it goes on (``choice``), how it
    ends when no key is left (``blocked``: no model is asked then), and what Domo is told of it:
    the key and the model the request talks on (``models``), the keys the person may use and the
    ones passed over at their limit (``keys``), where the limits the person set stand
    (``spending``) and who began using a key the person gave (``newcomers``). ``unmarked`` are
    the news among them that are not marked told yet (``_money_of`` without ``mark``)."""

    choice: KeyChoice
    blocked: ErrorKind | None = None
    keys: KeysNote | None = None
    spending: tuple[SpendingNote, ...] = ()
    newcomers: tuple[NewcomerNote, ...] = ()
    models: ModelsNote | None = None
    unmarked: News | None = None


async def _speaking(
    session: AsyncSession, speaker: Speaker, conversation_id: uuid.UUID, now: datetime
) -> Speaker:
    """``speaker`` with the clues kept for the person on that channel and that device
    (``identity.clues.kept_clues``) added to those the channel brings, and, when someone speaks,
    when they were heard before in the conversation ``conversation_id`` (``last_heard_at``: their
    message before their last one, ``None`` when there is none)."""
    if speaker.person_id is None:
        return speaker
    kept = await kept_clues(
        session,
        person_id=speaker.person_id,
        channel=speaker.channel,
        device_id=speaker.device_id,
        now=now,
    )
    heard = None
    if not speaker.nobody_spoke:
        heard = await session.scalar(
            select(Message.created_at)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "user",
                Message.provenance == "person",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .offset(1)
            .limit(1)
        )
    return replace(speaker, clues=(*speaker.clues, *kept), last_heard_at=heard)


async def _display_names(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(Person.id, Person.display_name).where(Person.id.in_(set(ids)))
    )
    return {person_id: name for person_id, name in rows.all()}


def _spending_note(note: LimitNote) -> SpendingNote:
    return SpendingNote(
        percent=note.percent,
        amount=note.amount,
        currency=note.currency,
        period=note.period,
        key_label=note.key_label,
        threshold_reached=note.threshold_reached,
        threshold_by=note.threshold_by,
        person_name=note.person_name,
        role=note.role,
        split=note.split,
        top_person_name=note.top_person_name,
    )


async def _money_of(
    session: AsyncSession,
    person: Person,
    keys: Sequence[Provider],
    *,
    now: datetime,
    settings: PolicySettings,
    catalog: Catalog,
    history: Sequence[ChatMessage],
    max_output_tokens: int,
    mark: bool = True,
) -> _Money:
    """The key of a request of ``person`` among ``keys`` (``router.choose_key``: a key with no
    margin left for the request's first call is passed over), and what Domo is told of the
    money of the request. Thresholds told, and newcomers, are marked in ``session`` (flushed: the
    caller commits); without ``mark`` they are not, and are given back to be marked later
    (``_Money.unmarked``)."""
    zone = settings.time_zone
    # The part of the request known here: the system text is only built by the turn.
    size = estimate_tokens(sum(len(message.content) for message in history))

    async def at_its_limit(key: Provider, route: Route) -> bool:
        # The most the first call on that route may cost: the request, and the longest answer.
        need = max_cost_usd(route.prices, input_tokens=size, max_output_tokens=max_output_tokens)
        return await at_limit(
            session,
            provider=key,
            person_id=person.id,
            need=need,
            now=now,
            catalog=catalog,
            zone=zone,
        )

    choice = await choose_key(
        session, person, keys, catalog=catalog, at_limit=at_its_limit, min_context=size
    )
    if choice.key is None:
        # No model will be told anything: no threshold is marked as told either.
        return _Money(choice, blocked="limit_reached" if choice.at_limit else "no_model")
    used = choice.key
    names = await _display_names(session, (key.given_by for key in (*keys, *choice.at_limit, used)))
    route = choice.route
    talk = (
        None
        if route is None
        else ModelsNote(
            key_label=key_label(used, names[used.given_by]),
            model_id=route.model_id,
            level=route.level,
            price_known=route.price_known,
        )
    )
    # The keys the person may use (their own among them, said so): the tools of keys name one by
    # its id. And the ones passed over at their limit, when there are.
    told = KeysNote(
        listed=tuple(
            KeyShown(key_label(key, names[key.given_by]), key.id, yours=key.given_by == person.id)
            for key in keys
        ),
        at_limit=tuple(key_label(key, names[key.given_by]) for key in choice.at_limit),
        used=key_label(used, names[used.given_by]) if choice.at_limit else None,
    )
    notes = await limit_notes(
        session,
        person_id=person.id,
        now=now,
        catalog=catalog,
        zone=zone,
        # The threshold Domo gave himself on each key: its own, else the household's.
        own_thresholds=await warn_thresholds(session),
        mark=mark,
    )
    news = await newcomers_told(
        session, giver_id=person.id, now=now, zone=zone, catalog=catalog, mark=mark
    )
    return _Money(
        choice,
        keys=told,
        spending=tuple(_spending_note(note) for note in notes),
        newcomers=tuple(
            NewcomerNote(
                key_label=new.key_label,
                person_name=new.person_name,
                role=new.role,
                host_name=new.host_name,
                spent_usd=new.spent_usd,
                spent_eur=new.spent_eur,
            )
            for new in news
        ),
        models=talk,
        unmarked=None if mark else News(limits=tuple(notes), newcomers=tuple(news)),
    )


async def prepare_turn(
    deps: TurnDeps,
    *,
    person: Person,
    reply: Message,
    request: TurnRequest,
    decided: JustDecided | None = None,
    silent_ok: bool = False,
    wake: WakeStarter | None = None,
    arrival: ArrivalNote | None = None,
    notify: Notify | None = None,
    due_plans: Sequence[DuePlan] = (),
    tool_wakes: Sequence[ToolWake] = (),
    on_his_own: bool = False,
    turn_reason: str | None = None,
    expiring: AboutToExpire | None = None,
) -> Turn:
    """The turn that writes ``reply``, a reply of ``person``'s conversation that is ``streaming``
    and that the caller claimed in ``deps.streaming_replies`` (see the module docstring).

    ``due_plans`` are the plans of the person whose time came (spec 6.9), for a turn one woke
    Domo for (``starts.plan_turn``: no words of theirs, its conversation ends with
    ``PLAN_MARKER``) or an arrival that carries them: the context tells them (``Due now``) and how
    many times he woke by himself for the person today, the judge reads their exact words, and
    the turn counts as what the person asked only when every plan due keeps some
    (``ToolContext.asked_before``); such an arrival goes on the main calls of a conversation.
    The news told once are marked told only once it spoke, as for an arrival. Never an upkeep
    turn: ``NoKey`` without a key.

    ``tool_wakes`` are what tools of Domo woke him with, for a turn they woke him for
    (``tool_wakes.tool_wake_turn``, spec 5.4: no words of the person, its conversation ends with
    ``TOOL_MARKER``): the context tells them, as data, and nothing else reads them (the judge
    reads the conversation, never them). Like a plan's turn, it may say nothing, and goes on no
    upkeep turn. With ``on_his_own`` (a launch for the house woke him, in the owner's
    conversation: spec 5.1, 5.8) Domo acts on his own: every call of the turn is judged with his
    own rights (``ToolContext.on_his_own``), what is about the person's own things is not his by
    default there (``policy.catalog.on_his_own_tables``), the tools offered are those his rights
    give him, the notes left for the person wait for a turn of theirs, and what he launches is for
    the house.

    ``decided`` is what was just decided, for a turn that wakes Domo: there are no words of the
    person to read then, and he is told that instead. ``arrival`` is what he is told when the
    person just came back (spec 4.9): no words either, its main calls are of the kind
    ``arrival``. With ``silent_ok``, a turn that ends with no text and no action takes its reply
    away. ``wake`` wakes Domo in the conversation of the one who asked for a request that the
    person decides in words as its approver (spec 5.1, ``ToolContext.wake``): a turn that answers
    the person's message has one, a wake's none. ``notify`` tells the core that the turn kept news
    for someone (``ToolContext.notify``).

    ``expiring`` is a request of the person that is about to lapse, for a turn it woke him for
    (``starts.wake_before_expiry``: no words of theirs, its conversation ends with
    ``EXPIRY_MARKER``): the context tells it, as data, with what he may do; he may say nothing,
    and goes on no upkeep turn.

    ``turn_reason`` is why Domo began this turn himself (``starts``, ``tool_wakes``: a plan he set
    alone, an arrival, a decision, a tool of his), ``None`` in a turn that answers someone and in a
    plan's turn, or an arrival, whose plans due all keep the person's words: every judged call of
    the turn is then an idea of his with that reason, as the judge is told
    (``ToolContext.turn_reason``, ``tools.context.idea_of``), though it stays the person's act, with
    their rights, unless he marks it; and what he remembers is looked for from it first
    (``recall_query``). Raises
    ``NoKey``."""
    async with deps.session_factory() as session:
        # The catalog of this turn, with the kinds Domo opened for his tools: read once, used
        # for the whole turn (never again mid-turn).
        policy = await load_policy(session, deps.policy)
        if on_his_own:
            # Nobody asked: what is about the person's own things is not his by default here.
            policy = on_his_own_tables(policy)
        keys = await usable_keys(session, person)
        texts = await person_texts(session, person)
        speaker = await _speaking(session, request.speaker, reply.conversation_id, deps.clock())
        if not keys:
            # A message the person wrote, or their arrival: a wake, or a plan, goes on no key.
            general = (
                None
                if decided is not None or due_plans or tool_wakes or expiring is not None
                else await upkeep_key(session)
            )
            if general is None:
                raise NoKey
            return await _upkeep_turn(
                session,
                deps,
                policy=policy,
                general=general,
                person=person,
                reply=reply,
                request=request,
                speaker=speaker,
                texts=texts,
                arrival=arrival,
                silent_ok=silent_ok,
            )
        # The news for the person as the turn reads them, first: what it reads next is told them,
        # and a turn that speaks takes them away (``visits.clear_news``).
        news_seen = await session.scalar(select(Person.news_at).where(Person.id == person.id))
        messages = await ConversationStore(session).recent_messages(reply.conversation_id)
        actions = await ConversationStore(session).actions_of(
            m.id for m in messages if m.role == "assistant" and m.id != reply.id
        )
        # The owner's settings (read again at every turn: a change applies at once).
        policy_settings = await load_policy_settings(session)
        now = deps.clock()
        # The requests the history's actions made, or managed (R73).
        named = await confirmations_by_id(
            session,
            (a.confirmation_id for taken in actions.values() for a in taken if a.confirmation_id),
        )
        requests = _HistoryRequests(
            named, policy_settings, now, await _who_noted(session, named.values())
        )
        if (
            news_seen is not None
            and arrival is None
            and await untold_decision(
                session,
                person.id,
                since=_last_complete_at(messages, reply.id),
                told={*named, *([] if decided is None else [uuid.UUID(decided.request_id)])},
            )
        ):
            # A request of theirs someone else decided, which this turn is not told: whatever it
            # says, those news stay for an arrival turn (``visits``).
            news_seen = None
        # The conversation up to the reply: what the person wrote after it was opened is read by
        # the turn as it runs (``conversation.kept``), or by the turn after it.
        conversation = [
            seen
            for message in takewhile(lambda m: m.id != reply.id, messages)
            if (seen := _as_the_model_sees_it(message, actions.get(message.id, ()), requests))
            is not None
        ]
        if decided is not None:
            # A wake: no message of the person follows, and a conversation sent to a model ends
            # with the person's turn.
            conversation.append(ChatMessage("user", WAKE_MARKER))
        elif arrival is not None:
            # An arrival: the same, the person having just come back.
            conversation.append(ChatMessage("user", ARRIVAL_MARKER))
        elif due_plans:
            # A plan's time came: the same, nobody having written anything.
            conversation.append(ChatMessage("user", PLAN_MARKER))
        elif tool_wakes:
            # A tool of his woke him: the same.
            conversation.append(ChatMessage("user", TOOL_MARKER))
        elif expiring is not None:
            # A request is about to lapse: the same.
            conversation.append(ChatMessage("user", EXPIRY_MARKER))
        alternating = merge_consecutive_roles(conversation)
        settings = deps.settings
        history = select_history(alternating, max_chars=settings.max_history_chars)
        # The key of the request, and what the person is told of the money (read again at every
        # turn: what was spent changes with each call).
        catalog = await current_catalog(session)
        money = await _money_of(
            session,
            person,
            keys,
            now=deps.clock(),
            settings=policy_settings,
            catalog=catalog,
            history=history,
            max_output_tokens=settings.max_output_tokens,
            # An arrival, a plan's turn or a tool's, may say nothing: the news told once count as
            # told once it spoke.
            mark=arrival is None and not due_plans and not tool_wakes and expiring is None,
        )
        # The levers Domo chose on that key: a shorter history, shorter answers, how much of
        # his memory the turn gives him.
        max_output_tokens = settings.max_output_tokens
        chosen = await load_choices(
            session, None if money.choice.key is None else money.choice.key.id
        )
        if money.choice.key is not None:
            history_chars = bounded(
                chosen.history_chars, least=MIN_HISTORY_CHARS, most=settings.max_history_chars
            )
            if history_chars < settings.max_history_chars:
                history = select_history(alternating, max_chars=history_chars)
            max_output_tokens = bounded(
                chosen.output_tokens, least=MIN_OUTPUT_TOKENS, most=settings.max_output_tokens
            )
        # What Domo learned of the models lately.
        lessons = tuple(
            LessonNote(
                model_id=lesson.model_id,
                task=lesson.task,
                failures=lesson.failures,
                reason=lesson.last_reason,
            )
            for lesson in await recent_lessons(session, now=deps.clock())
        )
        # What went wrong lately, for this person and in his own acts (data).
        went_wrong = await read_or_nothing(
            session,
            "what went wrong",
            lambda: went_wrong_shown(
                session,
                person,
                policy_settings.time_zone,
                now=deps.clock(),
                on_his_own=on_his_own,
            ),
            (),
        )
        # The requests that wait for this person's own answer in this conversation.
        pending = await pending_for(
            session, person.id, reply.conversation_id, policy_settings, deps.clock()
        )
        # The requests of others that wait for this person's yes (spec 5.1), each with the name of
        # who asked.
        to_approve, _ = await awaiting_approval(
            session, person, policy_settings, deps.clock(), limit=MAX_APPROVALS_SHOWN
        )
        # The chains of this person that paused until someone's arrival, and whom each waits for
        # (spec 4.3b).
        paused = await paused_chains_of(session, person.id, reply.conversation_id)
        waiting_for = {
            request_id: name
            for request_id, (name, _) in (await waiting_names_of(session, paused)).items()
        }
        # The requests of this person that wait for someone else's yes (spec 5.2).
        for_others = await pending_for_others(
            session, person.id, reply.conversation_id, policy_settings, deps.clock()
        )
        # The steps of the chains among them: theirs with their own words, the others' and their
        # own that wait for someone else's yes without the words of a note (D4).
        steps = {
            **await steps_told_of(session, [*pending, *paused], someone_else=False),
            **await steps_told_of(
                session, [*for_others, *(c for c, _ in to_approve)], someone_else=True
            ),
        }
        # The owner's rights (read again at every turn): the tools shown are this person's.
        rights = await load_rights(session)
        role = as_role(person.role)
        owner = role == "owner"
        same_name = await _same_name(
            session, person, role, policy_settings, now=now, on_his_own=on_his_own
        )
        # The owner, and nobody else, is told the writer's time and how often it ran out of it
        # lately, whether the role was handed to them lately, and their list of successors.
        writer_time = (
            WriterTime(
                seconds=policy_settings.writer_seconds,
                timeouts=await writer_timeouts(session, deps.clock()),
            )
            if owner
            else None
        )
        ownership = await ownership_note(session, person.id, deps.clock()) if owner else None
        successors = (
            tuple([place.as_json() for place in await list_successors(session, deps.clock())])
            if owner
            else None
        )
        # The notes left for the person that Domo has not told them yet, as data (spec 4.8):
        # theirs alone.
        # On his own, Domo tells none: they wait for a turn of theirs.
        notes = () if on_his_own else await notes_to_tell(session, person.id)
        # Who is in the household, for everyone: Domo knows the names without a tool call.
        household = await household_shown(session, person.id, deps.clock())
        # The names the person uses for people of the household, theirs alone (spec 6.7).
        nicknames_of = await read_or_nothing(
            session,
            "nicknames",
            lambda: nicknames.of(session, person.id, now=deps.clock()),
            [],
        )
        # What Domo knows, for everyone: his core, and what the person's last messages bring
        # back, never what he keeps to himself of someone else; and, in the same read, what the
        # word of each person in it has been worth so far, and the person's own, as data, from
        # the household's start (spec 6.4). A turn no model is asked for (no key left) is given
        # none of it. Each of the reads below is held apart (``db.read_or_nothing``): one that
        # fails gives nothing, and the turn goes on.
        memory, trust = (
            await read_or_nothing(
                session,
                "memories",
                lambda: memory_and_trust_shown(
                    session,
                    person.id,
                    recall_query(messages, reply.id, turn_reason),
                    choices=chosen,
                    embedder=deps.embedder,
                    now=deps.clock(),
                    with_speaker=True,
                ),
                (MemoryBlock(), None),
            )
            if money.blocked is None
            else (MemoryBlock(), None)
        )
        # The plans Domo set for this person that wait, theirs alone (spec 6.9).
        plans = await read_or_nothing(
            session,
            "plans",
            lambda: plans_shown(session, person.id, policy_settings.time_zone),
            (),
        )
        # A plan's turn is told how many times Domo woke by himself for them today.
        woken = (
            await read_or_nothing(
                session,
                "wakes of the day",
                lambda: woke_today(session, person.id, deps.clock(), policy_settings.time_zone),
                0,
            )
            if due_plans
            else 0
        )
        # What someone else changed for them since Domo last spoke with them (their role, a
        # right of theirs...): the context says it, as the truth of now.
        changed = await read_or_nothing(
            session,
            "changes",
            lambda: changes_for(session, person.id, _last_complete_at(messages, reply.id)),
            (),
        )
        # The tools Domo made, all his, offered as any tool of the turn (``t_<name>``: the
        # barrier says who may use one); and those the person asked for that were set aside, for
        # the context to say why one is gone. Built here: the rows are read before the session
        # ends.
        # The tool a plan due now is told is ready is always among them.
        ready = await read_or_nothing(session, "tools made", lambda: ready_tools(session), [])
        # What each is built to reach, as declared on its row: said with what one woke him with.
        reaches = {tool.name: tuple(reaches_told(operations_of(tool.operations))) for tool in ready}
        made = offers(
            ready,
            made_tool_deps(deps),
            # How many a turn offers is Domo's setting (the others wait for ``tool_find``).
            most=await read_or_nothing(
                session,
                "tools offered",
                lambda: value(session, "tools_offered"),
                SETTINGS["tools_offered"].start,
            ),
            first=(plan.tool for plan in due_plans if plan.tool is not None),
        )
        aside = await read_or_nothing(
            session, "tools set aside", lambda: set_aside_for(session, person, deps.clock()), ()
        )
        # The builds they opened that are in progress: one build per tool, and Domo judges when
        # a request has waited for one long enough.
        building = await read_or_nothing(
            session,
            "builds in progress",
            lambda: being_built_for(session, person, policy_settings.time_zone),
            (),
        )
        # The standing agreements in force: theirs, and the house's for the owner (data).
        agreements = await read_or_nothing(
            session,
            "standing agreements",
            lambda: agreements_of(session, person.id, owner=owner),
            (),
        )
        # The thresholds of the limits that this turn tells, marked as told.
        await session.commit()
    # The tools of the turn: the code's, and the ones Domo made (no name of the code starts
    # with ``t_``). The turn's context keeps this registry: what it re-reads mid-turn sees them.
    registry: ToolRegistry = {**deps.registry, **made}
    # A tool of Domo past the ones offered is found when the model calls it.
    made_tool_named: Callable[[str], Awaitable[InternalTool | None]] | None = made_tool_finder(deps)
    tools = tools_for(
        role, registry, policy, person_id=person.id, rights=rights, on_his_own=on_his_own
    )
    # What exists in the household and the rights keep from them: Domo says whose it is, never
    # that a tool is missing (the owner's trial of 0.9.1).
    kept = tools_kept_from(
        role, registry, policy, person_id=person.id, rights=rights, on_his_own=on_his_own
    )
    # The person's message the reply answers: none when Domo speaks first (a wake, an arrival,
    # a plan).
    unprompted = (
        decided is not None
        or arrival is not None
        or bool(due_plans)
        or bool(tool_wakes)
        or expiring is not None
    )
    answered = None if unprompted else _person_message_before(messages, reply.id)
    # Their message, as it is stored: a tool keeps it as they wrote it (``ToolContext.said``).
    said = next((m.content for m in messages if m.id == answered), None)
    # Words that are neither theirs nor Domo's are told in this turn: a note someone left them,
    # the requests of others that wait for their yes (what each asks, and why, was written by or
    # for the one who asked), what an approver said of a request of theirs, while it waits and,
    # once it is decided, in the line of the history that quotes it. What Domo does in such a
    # turn is not known to be what they asked for (``ToolContext.they_asked``).
    told_outside = (
        bool(notes)
        or bool(to_approve)
        or bool(requests.noted_by)
        or any(c.approver_note is not None for c in (*pending, *paused, *for_others))
    )
    tool_context = ToolContext(
        session_factory=deps.session_factory,
        actor=actor_of(speaker, role),
        person=person,
        conversation_id=reply.conversation_id,
        origin=request.origin,
        gatekeeper=deps.gatekeeper,
        clock=deps.clock,
        registry=registry,
        made_tool_named=made_tool_named,
        # What a program Domo runs by hand needs (``tools.program_run``).
        launch_deps=made_tool_deps(deps),
        policy=policy,
        reply_message_id=answered,
        domo_message_id=reply.id,
        texts=texts,
        no_reader=not request.abilities.shows_once,
        wake=wake,
        notify=notify,
        text_writing=deps.text_writing,
        model_lister=deps.model_lister,
        embedder=deps.embedder,
        said=said,
        abilities=request.abilities,
        speaker=speaker,
        told_outside=told_outside,
        tool_woke=bool(tool_wakes) or (decided is not None and decided.from_outside),
        due_plans=tuple(due_plans),
        # The plans due are what they asked for only when each one keeps their exact words: an
        # action of the turn may be for any of them, and one Domo made alone keeps none.
        asked_before=bool(due_plans) and all(plan.their_words for plan in due_plans),
        on_his_own=on_his_own,
        turn_reason=turn_reason,
    )
    prepared = Prepared(
        person_id=person.id,
        reply_id=reply.id,
        # No key left: the turn is refused, and the picker on the first key is never asked.
        provider=money.choice.key or keys[0],
        blocked=money.blocked,
        catalog=catalog,
        prompt=Prompt(
            display_name=person.display_name,
            role=role,
            can_act=bool(tools),
            # The name they call him by, theirs alone (spec 6.7).
            calls_domo=person.calls_domo,
            house_calls=policy_settings.house_domo_name,
            house_called_before=policy_settings.house_domo_name_before,
            same_name=same_name,
            writer_time=writer_time,
            ownership=ownership,
            successors=successors,
            # The day it is, for everyone: a model has none of its own.
            today=today_of(deps.clock(), policy_settings.time_zone),
            household=household,
            nicknames=tuple(nicknames_of),
            memory=memory,
            plans=plans,
            spending=money.spending,
            newcomers=money.newcomers,
            keys=money.keys,
            models=money.models,
            lessons=lessons,
            went_wrong=went_wrong,
            notes_to_tell=notes,
            arrival=arrival,
            unanswered=unanswered_in(messages),
            offered=frozenset(tool.name for tool in tools),
            kept=kept,
            reads=person.language or policy_settings.house_language,
            abilities=request.abilities,
            due_plans=tuple(due_plans),
            woke_today=woken,
            changes=changed,
            trust=trust,
            aside=aside,
            # A program run by hand, or a tool no longer ready, says nothing of what it reaches.
            tool_wakes=tuple(
                wake if wake.run is not None else replace(wake, reaches=reaches.get(wake.tool))
                for wake in tool_wakes
            ),
            building=building,
            agreements=agreements,
            expiring=expiring,
        ),
        history=history,
        tools=tools,
        tool_context=tool_context,
        answer=Answer(
            text=said or "",
            pending=pending,
            settings=policy_settings,
            last_reply_id=_reply_before(messages, answered),
            paused=paused,
            waiting_for=waiting_for,
            to_approve=to_approve,
            steps=steps,
            waiting_for_others=for_others,
            earlier=_before_message(messages, answered),
        ),
        max_output_tokens=max_output_tokens,
        texts=texts,
        decided=decided,
        silent_ok=silent_ok,
        main_task="arrival" if arrival is not None and not due_plans else "conversation",
        arrival=arrival is not None,
        unmarked=money.unmarked,
        news_seen=news_seen,
    )
    return Turn(deps, prepared)


async def _same_name(
    session: AsyncSession,
    person: Person,
    role: Role,
    settings: PolicySettings,
    *,
    now: datetime,
    on_his_own: bool,
) -> Literal["house", "theirs", "both"] | None:
    """Whose name for Domo someone of the house has too, as this turn is told. Never in a turn he
    acts in on his own (``on_his_own``): the offer is made to a person, in a turn of theirs. The
    house's name, in the owner's turn alone (they alone change it); the name this person gave him,
    in their own turn."""
    if on_his_own:
        return None
    house = (
        role == "owner"
        and settings.house_domo_name is not None
        and await name_in_use(session, settings.house_domo_name, now=now)
    )
    theirs = person.calls_domo is not None and await name_in_use(
        session, person.calls_domo, now=now
    )
    if house and theirs:
        return "both"
    return "house" if house else "theirs" if theirs else None


async def _upkeep_turn(
    session: AsyncSession,
    deps: TurnDeps,
    *,
    policy: PolicyTables,
    general: Provider,
    person: Person,
    reply: Message,
    request: TurnRequest,
    speaker: Speaker,
    texts: Texts,
    arrival: ArrivalNote | None = None,
    silent_ok: bool = False,
) -> Turn:
    """The upkeep turn of ``person``, who has no key Domo may use for them, on the general key
    ``general`` (see the module docstring), read in ``session``. It is never a conversation: the
    picker's main task is ``upkeep``, the instructions are the upkeep's, the model reads their last
    message alone (``ARRIVAL_MARKER`` at their ``arrival``) and answers in ``UPKEEP_OUTPUT_TOKENS``
    at most, and ``key_add`` is all it may call (if the rights let them). Raises ``NoKey`` when
    the reply answers no message of theirs."""
    messages = await ConversationStore(session).recent_messages(reply.conversation_id)
    answered = None if arrival is not None else _person_message_before(messages, reply.id)
    text = (
        ARRIVAL_MARKER
        if arrival is not None
        else next((m.content for m in messages if m.id == answered), None)
    )
    if text is None:
        raise NoKey
    policy_settings = await load_policy_settings(session)
    catalog = await current_catalog(session)
    rights = await load_rights(session)
    role = as_role(person.role)
    only = {name: tool for name, tool in deps.registry.items() if name == _UPKEEP_TOOL}
    tools = tools_for(role, only, policy, person_id=person.id, rights=rights)
    tool_context = ToolContext(
        session_factory=deps.session_factory,
        actor=actor_of(speaker, role),
        person=person,
        conversation_id=reply.conversation_id,
        origin=request.origin,
        gatekeeper=deps.gatekeeper,
        clock=deps.clock,
        registry=only,
        policy=policy,
        reply_message_id=answered,
        domo_message_id=reply.id,
        texts=texts,
        text_writing=deps.text_writing,
        no_reader=not request.abilities.shows_once,
        abilities=request.abilities,
        speaker=speaker,
    )
    prepared = Prepared(
        person_id=person.id,
        reply_id=reply.id,
        # Counted for them on the general key, under its limits.
        provider=general,
        main_task="upkeep",
        catalog=catalog,
        prompt=Prompt(
            display_name=person.display_name,
            role=role,
            can_act=bool(tools),
            upkeep=person.language or policy_settings.house_language,
            arrival=arrival,
            abilities=request.abilities,
        ),
        history=[ChatMessage("user", text)],
        tools=tools,
        tool_context=tool_context,
        # No words of theirs decide a request here: nothing waits to be read.
        answer=Answer(text=text, pending=(), settings=policy_settings, last_reply_id=None),
        max_output_tokens=UPKEEP_OUTPUT_TOKENS,
        texts=texts,
        silent_ok=silent_ok,
        arrival=arrival is not None,
    )
    return Turn(deps, prepared)
