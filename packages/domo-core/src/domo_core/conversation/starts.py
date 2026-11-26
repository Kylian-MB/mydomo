"""The turns Domo starts himself (spec 5, 5.1, 4.3b, 4.9, 6.6): a wake after a decision, an
arrival when someone comes back, a news turn when something new came for someone who is there.
No channel asks for them: the core decides that one is due, and what Domo says goes to whoever
listens to the person (``conversation.listeners.Fanout``).

Every such turn runs in a task of the application that outlives what started it (``Wakes``, the
application's ``TurnDeps.wakes``), as if nobody listened (``NOBODY_LISTENS``: what would be shown
once is not made, it waits on the person's card), with no message of the person, and is written
for the person whose conversation it is, with their tools and their rights. Its reply is kept as
any other, once, and a channel that comes later finds it there (the page asks for it, ``GET
/chat/since``); what it says as it comes goes to the person's open listeners that hear what Domo
says unasked. Domo may answer nothing at all: the reply is then taken away (``silent_ok``), and
nothing is shown, unless it presents a request again. It never fails loudly: what goes wrong is
logged by its kind alone (the details may hold text).

A wake
------

A yes or a no given by a button (``confirmations.routes``), or a proof given on the card (``via``
``proof``, ``proofs.service``), that decided something (``WAKE_OUTCOMES``: the request ran,
failed, was refused, now waits for someone else's yes, or ran a chain up to a step that waits for
someone's arrival) starts a wake (``spawn_wake``, ``Conversation.wake_after_decision``). So does
the yes or the no of an approver said in words, in their own conversation (``via`` ``words``, spec
5.1: ``conversation.service`` gives the turn a ``tools.context.WakeStarter``), a yes with a change
included, which may also make a new request of the one who asked (``asked_again``). So does the
upkeep's pass of the arrivals (``confirmations.arrivals``, ``via`` ``arrival``) for a chain paused
until someone's arrival that it resumed (it ran to its end, stopped, waits again, or waits for the
explicit yes again: ``arrived``), or that it stopped because the invitation ended without them.
``wake_after_decision``:

1. reads the request again, as the decision left it, and the person who asked it: nobody wakes
   Domo for someone who is no longer in the household. What Domo is told of the decision is
   read at once (``confirmations.chain.just_decided``, by a button or an arrival, ``woken``): the
   request, what the decision came to, who decided, what the model would have read of the action
   (never a part of its card that is shown once: a link stays out of his sight), and for a chain
   where it stands. It reaches the prompt as data, never as instructions (``prompts.JustDecided``);
2. opens a reply in that person's conversation (the request's), as saying something does: in a
   transaction that locks the conversation, once no reply is being written there, by the same
   rule (``conversation.service.reply_is_alive``: a reply that someone writes, or that is younger
   than ``STALE_AFTER``, is being written; one that nobody writes any more is marked as failed).
   While one is, the wake tries again every ``WAKE_RETRY_SECONDS``, for up to ``WAKE_WAIT_SECONDS``:
   longer than the longest turn (two calls of the rights writer of up to 240 seconds each, and the
   steps of a chain), so that a wake is never dropped because of a reply that lives. Past that it
   is dropped, and the log says so. The reply is claimed in ``streaming_replies`` as a channel's
   reply is: something said meanwhile is refused as busy, as during any reply. A chain that waits
   for the explicit yes again after an arrival (``arrived``) is presented again by that reply, in
   the same transaction (``_present_again``): its card is under it, and the person's next words
   may answer it (R74), as after ``confirmation_remind``;
3. writes the turn (``conversation.prepare.prepare_turn``) for the person who asked, read again
   when the reply is opened.

Only a person's click, an approver's words in their own conversation, or an arrival the upkeep
finds, wakes Domo: nothing he does in a wake starts another one (a wake's turn has no
``WakeStarter``), and nothing he does of his own in a turn either (a chain he continues,
``chain_continue``, runs in the turn that called it, which is told what came of it). A wake costs
one call of the model per click, approver's answer or arrival (more when he acts), even when he
stays silent. The decision, carried out before the wake started, stays as it is whatever the wake
meets.

An arrival
----------

Not to be confused with ``confirmations.arrivals``, which resumes a chain paused until someone joins
the household: here, a member of the household comes back (``came_back``: a channel says so,
``Conversation.seen`` with ``came_back``; the chat page that opens, say). When that counts as an
arrival (``conversation.visits.arrival_is_due``, which notes the time of the visit), Domo is offered
a turn: its reply is opened in the person's conversation (``open_reply``), held in ``ArrivalTurns``
from before it is committed, so that a message of the person stops it (it steps aside). The turn
(``prepare_turn`` with an ``ArrivalNote``) has no message of the person: the conversation it is sent
ends with a line of the code (``ARRIVAL_MARKER``), never shown, and its context says how long the
person was away and what is new. Its main calls are of the kind ``arrival`` (a small model to begin
with), it is offered the person's tools and may act when it is worth it, each call going through the
barrier and the judge as in any turn he starts, and it may answer nothing at all; so is the reply of
an arrival stopped before its end taken away, whatever it began to say, unless it acted. The news
told once (who began using a key the person gave, a threshold passed) count as told only once it
answered in words. It is dropped when a reply is being written in the conversation already (the
person's message came first: it answers nothing), and for someone who left meanwhile. Someone
without a key Domo may use for them gets an upkeep turn instead, on the general key, when the rules
of the house let it serve for that (spec 4.11): its one tool is ``key_add``, and it may answer
nothing too; without that, no arrival turn. When their coming back is no arrival, the news that wait
for them are considered (a news turn).

A news turn
-----------

News for someone (``conversation.visits.news_for``) gives them an arrival turn too, a news turn
(``consider_news``), when they are there: a channel says so (``Conversation.seen``, the page's ask
of what is new every few seconds), or a listener of theirs hears what Domo says unasked, told at
once once the news are kept (``Conversation.notify``). Nothing when their choice is ``never``,
while a reply of Domo is under way for them (never stopped for news: the next sign that they are
there once it is over gives the turn), nor more than once every ``NEWS_TURN_EVERY``, nor again for
news an arrival turn that ended was offered after (``ArrivalTurns.news_turn_due``), nor for someone
with no key Domo may use for them (the news wait for it). A turn that failed, or that a message
of the person erased, ended nothing: the news stay to offer (``conversation.visits``).

A plan's time
-------------

A plan Domo set for someone whose time came (spec 6.9) gives them a turn nobody asked for
(``plan_turn``, started by the upkeep: ``plans.upkeep.plans_pass``), with every plan of theirs due
then under his eyes (``PLANS_AT_ONCE`` at most; ``prepare_turn`` with ``due_plans``): he speaks,
acts with their tools and rights (the barrier judges with the rights of the day, and the judge
reads the exact words the plans keep as what they said), sets one again, or does nothing (a reply
with no words in which nothing that is shown acted is taken away). Each plan is then settled
(``settle_plans``: ``plans.service.fired``, unless he changed it in the turn); after a turn that
failed before it acted, they stay due. No turn, and nothing changed, while a reply of theirs is
under way, without a key Domo may use for them or with every key at its limit: no reply is
written then, not even an error. The plans due at someone's return come with their coming back
after an absence (``ARRIVAL_AFTER``): in the arrival turn when one is due (on the main calls of
a conversation then), else in a plan's turn of their own. A plan that waits for the build of a
tool is told how that build stands (``due_plans_of``: ready, with the tool's name; abandoned; or
still building when its time came first), and is served once (``plans.service``).

A tool of Domo
----------------

A program of a tool Domo made may wake him for whom its launch is (``madetools.means``, given by
``madetools.handover``), and a run a turn handed over wakes him for its person once it ended: those
turns are ``conversation.tool_wakes``'s (a launch for the house wakes him in the owner's
conversation, on his own). A coming back after an absence also launches the tools that wait for
that person's return (``madetools.triggers.returns_seen``, in a task of its own: no model, and
nothing of it touches the coming back).

A request about to lapse
------------------------

A request that waits and is about to lapse wakes Domo once (``wake_before_expiry``, started by the
upkeep's ``confirmations.expiring.expiring_pass``, which has written that it did). The turn is the
one who asked's, in the conversation where the request was first made (``conversation_made_in``:
none when that cannot be known, and then nobody is woken, never a turn of theirs opened in someone
else's conversation). He is told the request, whom it waits for (a role word) and how long it has
left (``prompts.AboutToExpire``, data), and judges: he reminds whoever must answer
(``confirmation_remind``), tells the one who asked, drops it (``confirmation_expire``), or does
nothing and answers nothing (``silent_ok``). Nothing he does in it starts another wake, and none
follows once the request lapsed.

Nobody spoke
------------

Every such turn tells the judge the truth (part 8 of "Domo judges first"): nobody spoke in it
(``unasked``: ``Speaker.nobody_spoke``, while the barrier reads whose turn it is as before), and
why Domo acts (``prepare_turn``'s ``turn_reason``, ``reason_of``): that a request was decided
(``DECISION_REASON``, a fixed sentence: what the request says stays data in the context), that
they came back (``ARRIVAL_REASON``, when no plan is due then), the plans due that keep no words
of the person, in a plan's turn or at their return (``plans_reason``), the tools that woke him
(``conversation.tool_wakes``). When every plan due keeps their words there is no reason: what
he does then is what they asked (ruling R39). Otherwise each call the judge weighs there is an
idea of his with that reason (``tools.context.idea_of``), still
the person's act with their rights unless he marks it his own: a judge that doubts asks the person
it concerns, and one silent twice leaves it undone, asking nobody. What he remembers is looked
for from that reason first (``conversation.prepare.recall_query``).

The tasks
---------

The turn of a reply that a channel streams runs in a task of ``Wakes`` too (spec 5.7,
``Conversation.answer``): the channel only relays it, so that a reader that leaves never stops it.
The application stops the tasks that still run when it stops (``Wakes.stop``, from the lifespan of
``web.app``).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.chat.agent import ActionRecord
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.confirmations.chain import just_decided
from domo_core.confirmations.describe import description_for_a_model, request_description
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import AmendOutcome, DecideOutcome
from domo_core.confirmations.store import (
    action_type_of,
    approver_of,
    confirmations_by_id,
    conversation_made_in,
    is_past_lifetime,
    mark_shown,
    replayed_on_his_own,
    waits_for_of,
)
from domo_core.conversation.channel import NOBODY_LISTENS
from domo_core.conversation.listeners import Fanout
from domo_core.conversation.prepare import (
    NoKey,
    TurnDeps,
    TurnRequest,
    made_tool_deps,
    prepare_turn,
    upkeep_key,
)
from domo_core.conversation.service import reply_is_alive
from domo_core.conversation.speaker import Clue, Speaker
from domo_core.conversation.turn import Turn, TurnEnd
from domo_core.conversation.visits import (
    ARRIVAL_AFTER,
    TAKEN_BACK,
    ArrivalTurns,
    arrival_is_due,
    what_is_new,
)
from domo_core.llm.choices import load_choices
from domo_core.llm.router import usable_keys
from domo_core.madetools.models import BuildState
from domo_core.madetools.triggers import returns_seen
from domo_core.plans.models import Plan
from domo_core.plans.service import (
    builds_waited_on,
    due_on_return,
    fired,
    served_by_its_build,
    time_served,
)
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import AboutToExpire, ArrivalNote, DuePlan
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import MAX_IDEA_REASON_LENGTH, Notify

logger = logging.getLogger(__name__)

# How long a wake waits for the reply being written in the conversation to end, in seconds:
# longer than the longest turn (two rights-writer calls of up to 240 s, plus the steps), so that a
# wake is never dropped for a reply that lives.
WAKE_WAIT_SECONDS: Final = 900.0
# How often a wake looks again whether the conversation is free, in seconds.
WAKE_RETRY_SECONDS: Final = 1.0
# The outcomes of a button, of an approver's words, or of an arrival, that wake Domo: those that
# decided something (``asked_again``: an approver's change made a new request of the one who
# asked). The others (``already_decided``, ``expired``, ``not_yours``, ``needs_explicit``,
# ``needs_proof``, ``proof_failed``) decided nothing.
WAKE_OUTCOMES: Final = frozenset(
    {"executed", "failed", "refused", "approver_changed", "paused", "arrived", "asked_again"}
)
# How the decision that wakes Domo was given (``confirmations.chain.just_decided``): ``proof``
# is a proof that it is the person, given on the card (``proofs.service``).
WakeVia = Literal["words", "button", "arrival", "proof"]
# How long taking back the reply of a wake that could not be written may take, in seconds.
_TAKE_BACK_SECONDS: Final = 15.0
# A news turn counts what is new from a moment just before the news (``consider_news``).
_BEFORE: Final = timedelta(microseconds=1)
# Who speaks in a turn Domo starts: no channel asked for it.
_NO_CHANNEL: Final = "unasked"
# How many plans of a person one turn is given at most (the soonest first): the others wait for a
# later one (spec 6.9).
PLANS_AT_ONCE: Final = 10
# How late a plan is before its turn says so (``DuePlan.late_minutes``).
LATE_AFTER: Final = timedelta(minutes=2)
# What came of a turn plans were due in (``came_to``), as each plan keeps it.
PlanOutcome = Literal["spoke", "acted", "silent", "failed"]
# What a plan's turn gives back (``plan_turn``): what came of it, or ``no_room`` when it could not
# be written because no key of the person has room left (every one at its limit): the plans stay
# due, and the pass waits before it tries again (``plans.upkeep``).
PlanTurnEnd = PlanOutcome | Literal["no_room"]
# Why Domo begins an arrival turn, as the judge is told (``ToolContext.turn_reason``).
ARRIVAL_REASON: Final = "they just came back"


def reason_of(text: str) -> str:
    """Why Domo began a turn himself, as the judge is told it (``prepare_turn``'s
    ``turn_reason``), cut at ``MAX_IDEA_REASON_LENGTH``."""
    return text[:MAX_IDEA_REASON_LENGTH]


def plans_reason(due: Sequence[DuePlan]) -> str | None:
    """Why Domo acts in a turn the plans ``due`` woke him for: the ``why`` of those that keep no
    words of the person, joined by ``"; "`` (``reason_of``); ``None`` when every one keeps their
    words (what he does then is what they asked, ``ToolContext.asked_before``)."""
    alone = [plan.why for plan in due if not plan.their_words]
    return reason_of("; ".join(alone)) if alone else None


# Why Domo acts in a wake after a decision: a fixed sentence of the code. What the request says
# (its description: someone's words, a note's text, a name) stays in what the context and the
# judge read as data, never in his own reason.
DECISION_REASON: Final = "a request was decided"


# Why Domo acts in a wake before a lapse: a fixed sentence of the code, never the words of the
# request (its description goes where the turn reads it, as data).
EXPIRY_REASON: Final = "a request is about to lapse"


class Wakes:
    """The turns of the application that still run (``TurnDeps.wakes``): the turns Domo starts,
    and the turns of the replies the channels stream (``conversation.service.Conversation.answer``,
    spec 5.7). Each one is a task that outlives what started it (a button, a page's connection),
    kept here until it ends, so that it is neither lost nor left behind when the application
    stops."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run ``coroutine`` in a task of its own, kept until it ends."""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def pending(self) -> int:
        """How many turns, and other tasks of the application, still run."""
        return len(self._tasks)

    async def stop(self, seconds: float) -> None:
        """Cancel every task that still runs, and wait up to ``seconds`` for them to end (what a
        turn must store at its end is shielded, and has time limits of its own: a reply cut short
        so is stored ``partial``). Shielded too, so that it finishes even when it is the caller
        that is being cancelled."""
        tasks = set(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with anyio.move_on_after(seconds, shield=True):
                await asyncio.wait(tasks)


def notifier(deps: TurnDeps) -> Notify:
    """What tells the core that news were just kept for someone (``Conversation.notify``), for
    what keeps news outside a channel's request (``tools.context.ToolContext.notify``)."""

    def notify(person_id: uuid.UUID) -> None:
        news_kept(deps, person_id)

    return notify


def unasked(deps: TurnDeps, person: Person) -> TurnRequest:
    """The request of a turn Domo starts for ``person``: no channel asked for it, nobody reads it
    as it comes (``NOBODY_LISTENS``), and it is theirs as their conversation is (the turns a tool
    wakes him for too, ``conversation.tool_wakes``). Nobody spoke in it (``Speaker.nobody_spoke``):
    the judge is told so, while the barrier reads whose it is as before."""
    known = Clue("session", "for", "strong", "code", deps.clock())
    return TurnRequest(
        speaker=Speaker(person.id, _NO_CHANNEL, clues=(known,), nobody_spoke=True),
        abilities=NOBODY_LISTENS,
    )


# --- a wake --------------------------------------------------------------------------------------


def spawn_wake(
    deps: TurnDeps,
    *,
    confirmation_id: uuid.UUID,
    outcome: DecideOutcome | AmendOutcome,
    record: ActionRecord | None,
    decider: Person,
    via: WakeVia = "button",
) -> None:
    """Wake Domo (``wake_after_decision``) in a task of ``deps.wakes`` when ``outcome`` decided
    something (``WAKE_OUTCOMES``); nothing otherwise. A request that still waits then (for
    someone else's yes, or again after an arrival) has its heads asked for what it is now, apart
    from the wake (``TurnDeps.card_heads``)."""
    if outcome not in WAKE_OUTCOMES:
        return
    if deps.card_heads is not None:
        deps.card_heads.ask_for_request(confirmation_id, clock=deps.clock)
    deps.wakes.spawn(
        wake_after_decision(
            deps,
            confirmation_id=confirmation_id,
            outcome=outcome,
            record=record,
            decider=decider,
            via=via,
        )
    )


async def wake_after_decision(
    deps: TurnDeps,
    *,
    confirmation_id: uuid.UUID,
    outcome: DecideOutcome | AmendOutcome,
    record: ActionRecord | None,
    decider: Person,
    via: WakeVia = "button",
) -> None:
    """Wake Domo in the conversation of the person who asked for the request
    ``confirmation_id``, which ``decider`` just decided with a button, or in words as its approver
    (``via`` ``words``; ``outcome``, and the ``record`` of the call that ran, if one did), or which
    went on, or stopped, after someone's arrival (``via`` ``arrival``: ``decider`` is then the one
    whose yes it had): see the module docstring. Never raises: what goes wrong is logged, by its
    kind."""
    try:
        await _wake(
            deps,
            confirmation_id=confirmation_id,
            outcome=outcome,
            record=record,
            decider=decider,
            via=via,
        )
    except Exception as error:
        logger.error("A wake failed (%s)", type(error).__name__)


async def _wake(
    deps: TurnDeps,
    *,
    confirmation_id: uuid.UUID,
    outcome: DecideOutcome | AmendOutcome,
    record: ActionRecord | None,
    decider: Person,
    via: WakeVia,
) -> None:
    async with deps.session_factory() as session:
        request = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
        if request is None:
            return
        requester = await session.get(Person, request.requester_id)
        if requester is None or not is_active(requester, deps.clock()):
            return
        decided = await just_decided(
            session,
            request,
            outcome=outcome,
            record=record,
            decider=decider,
            via=via,
            woken=True,
        )
        # Where the one who asked asked it (for a request that waited for the person it
        # concerns in their own conversation, not that one): they are told there.
        made_in = await conversation_made_in(session, request)
    if made_in is None:
        # Never a turn of theirs opened in someone else's conversation.
        logger.warning("A wake was dropped: the turn the request was made in is not known")
        return
    # A chain that waits for the explicit yes again after an arrival: the reply presents it.
    presents = request.id if outcome == "arrived" else None
    opened = await open_reply(deps, made_in, request.requester_id, presents=presents)
    if opened is None:
        return
    reply, requester, presented = opened
    handed_over = False
    try:
        turn = await prepare_turn(
            deps=deps,
            person=requester,
            reply=reply,
            request=unasked(deps, requester),
            decided=decided,
            # A reply that presents a request again stays, whatever Domo says.
            silent_ok=not presented,
            notify=notifier(deps),
            # A request Domo made on his own wakes him on his own (Task 9); one born in a
            # person's turn (his idea there, the owner's rule on his act there) wakes him in
            # their turn (``store.replayed_on_his_own``).
            on_his_own=replayed_on_his_own(request),
            # Nobody spoke: why he acts is the decision.
            turn_reason=DECISION_REASON,
        )
        handed_over = True  # the turn releases the claim when it ends
    finally:
        if not handed_over:
            deps.streaming_replies.discard(reply.id)
            await take_back(deps, reply.id, presented=presented)
    await turn.run(Fanout(deps.listeners, requester.id, reply.id))


async def wake_before_expiry(deps: TurnDeps, confirmation_id: uuid.UUID) -> None:
    """Wake Domo, once, for the request ``confirmation_id`` that is about to lapse
    (``confirmations.expiring``): see the module docstring. Never raises: what goes wrong is
    logged, by its kind."""
    try:
        await _wake_before_expiry(deps, confirmation_id)
    except Exception as error:
        logger.error("A wake before a lapse failed (%s)", type(error).__name__)


async def _wake_before_expiry(deps: TurnDeps, confirmation_id: uuid.UUID) -> None:
    async with deps.session_factory() as session:
        request = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
        if request is None or request.status != "pending":
            return
        settings = await load_policy_settings(session)
        now = deps.clock()
        if is_past_lifetime(request, settings, now):
            return
        requester = await session.get(Person, request.requester_id)
        if requester is None or not is_active(requester, now):
            return
        # Where the one who asked asked it: never a turn of theirs in someone else's conversation.
        made_in = await conversation_made_in(session, request)
        left = request.created_at + settings.max_lifetime - now
        about = AboutToExpire(
            request_id=str(request.id),
            description=(
                request_description(request, REFERENCE)
                if request.approver_kind == "self" and request.concerned_id is None
                else description_for_a_model(request)
            ),
            waits_for=waits_for_of(request),
            hours_left=round(left / timedelta(hours=1), 1),
        )
    if made_in is None:
        logger.warning("A wake was dropped: the turn the request was made in is not known")
        return
    opened = await open_reply(deps, made_in, request.requester_id)
    if opened is None:
        return
    reply, requester, _ = opened
    turn: Turn | None = None
    try:
        with contextlib.suppress(NoKey):
            turn = await prepare_turn(
                deps=deps,
                person=requester,
                reply=reply,
                request=unasked(deps, requester),
                expiring=about,
                # He may answer nothing: a reply with no words, in which nothing shown acted, is
                # taken away.
                silent_ok=True,
                notify=notifier(deps),
                # His own request made on his own is his to drop or to remind about, in a turn
                # of his own; one born in a person's turn, in theirs (``replayed_on_his_own``).
                on_his_own=replayed_on_his_own(request),
                turn_reason=EXPIRY_REASON,
            )
    finally:
        if turn is None:
            deps.streaming_replies.discard(reply.id)
            await take_back(deps, reply.id, presented=False)
    if turn is None:
        return
    await turn.run(Fanout(deps.listeners, requester.id, reply.id))


async def open_reply(
    deps: TurnDeps,
    conversation_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    presents: uuid.UUID | None = None,
    waits: bool = True,
    upkeep: bool = False,
    hold: Callable[[uuid.UUID], None] | None = None,
) -> tuple[Message, Person, bool] | None:
    """Open the wake's reply in the conversation, claimed, once no reply is being written there,
    trying again every ``WAKE_RETRY_SECONDS`` for up to ``WAKE_WAIT_SECONDS``; with the person who
    asked, as they are at that moment, and whether the reply presents the request ``presents``
    again (``_present_again``: it does while that request waits for a yes). ``None`` when the wake
    is dropped: the conversation stayed busy (logged), the person left meanwhile, or they have no
    key Domo may use for them any more (a wake goes on the key of the one whose conversation it
    is).

    An arrival turn opens its reply here too, with ``waits`` off: it answers nothing, so it is
    dropped at once when a reply is being written (the person's own message came first); with
    ``upkeep``: someone without a key is opened a reply when the general key serves to ask them for
    theirs (``conversation.prepare.upkeep_key``); and with ``hold``, which is given the reply's id
    before it is committed, so that a message the person says from then on finds it held (an
    arrival steps aside for it)."""
    deadline = anyio.current_time() + WAKE_WAIT_SECONDS
    while True:
        async with deps.session_factory() as session:
            store = ConversationStore(session)
            # From here to the commit nobody else adds a message to the conversation: finding
            # it free and opening the reply are one step, as for a message the person says.
            await store.lock_conversation(conversation_id)
            streaming = await store.streaming_message(conversation_id)
            if streaming is None or not reply_is_alive(
                streaming, deps.streaming_replies, deps.clock()
            ):
                requester = await session.get(Person, requester_id)
                if requester is None or not is_active(requester, deps.clock()):
                    return None
                if not await usable_keys(session, requester) and not (
                    upkeep and await upkeep_key(session) is not None
                ):
                    return None
                if streaming is not None:
                    # Nobody writes it any more: it no longer holds anyone.
                    await store.finish_message(
                        streaming.id, content=streaming.content, status="error"
                    )
                # The turn chooses its models call by call: the reply says which at its end.
                reply = await store.add_message(
                    conversation_id,
                    role="assistant",
                    content="",
                    status="streaming",
                    provenance="model",
                )
                presented = presents is not None and await _present_again(
                    session, store, reply.id, presents
                )
                if hold is not None:
                    hold(reply.id)
                await session.commit()
                deps.streaming_replies.add(reply.id)
                return reply, requester, presented
        if not waits:
            return None
        if anyio.current_time() >= deadline:
            logger.warning("A wake was dropped: the conversation stayed busy")
            return None
        await anyio.sleep(WAKE_RETRY_SECONDS)


async def _present_again(
    session: AsyncSession,
    store: ConversationStore,
    reply_id: uuid.UUID,
    confirmation_id: uuid.UUID,
) -> bool:
    """Make the reply ``reply_id`` present the request ``confirmation_id`` again, if it waits for
    a yes: the request's card is under it (an action that waits for that yes, as under the reply
    that asked for it), and it is the reply that last showed the request, so that the person's
    next words may answer it (R74, ``confirmations.store.mark_shown``). Whether it does. Flushed,
    not committed."""
    found = await session.execute(
        select(Confirmation)
        .where(Confirmation.id == confirmation_id)
        .execution_options(populate_existing=True)
    )
    c = found.scalar_one_or_none()
    if c is None or c.status != "pending":
        return False
    await store.add_action(
        reply_id,
        tool_name=c.tool_name,
        action_type=action_type_of(c),
        status="waiting",
        summary={"key": f"action.card.waiting.{approver_of(c)}", "params": {}},
        confirmation_id=c.id,
    )
    await mark_shown(session, c.id, reply_id)
    return True


async def take_back(deps: TurnDeps, reply_id: uuid.UUID, *, presented: bool) -> None:
    """Take away the reply a wake opened but could not write, so that it does not hold the
    conversation; shielded, so that it is done even when the wake is being stopped. A reply that
    presents a request again (``presented``: the request names it as the reply that last showed
    it, and its card is under it) is not taken away but ended in error, empty: the card stays
    where the person's next words may answer it. A failure is logged: the reply is then taken for
    dead after ``STALE_AFTER``, like any reply nobody writes."""
    with anyio.move_on_after(_TAKE_BACK_SECONDS, shield=True):
        try:
            async with deps.session_factory() as session:
                store = ConversationStore(session)
                if presented:
                    # No moment without an answer: it answered no message (``visits``).
                    await store.finish_message(
                        reply_id, content="", status="error", failure=TAKEN_BACK
                    )
                else:
                    await store.drop_reply(reply_id)
                await session.commit()
        except Exception as error:
            logger.error("The reply of a wake could not be taken back (%s)", type(error).__name__)


# --- an arrival, a news turn ---------------------------------------------------------------------


def came_back(deps: TurnDeps, person_id: uuid.UUID, *, now: datetime) -> None:
    """``person_id`` came back at ``now`` (a channel says so): whether that is an arrival is
    weighed, and the time of the visit noted, in a task of ``deps.wakes``, which offers Domo the
    arrival turn when it is one, and considers the news that wait for them when it is not (see
    the module docstring). Nothing where there are no arrival turns."""
    turns = deps.arrival_turns
    if turns is None:
        return
    deps.wakes.spawn(_welcome(deps, turns, person_id, now))


async def _welcome(
    deps: TurnDeps, turns: ArrivalTurns, person_id: uuid.UUID, now: datetime
) -> None:
    """``came_back``'s task. Never raises: what goes wrong is logged, by its kind."""
    try:
        async with deps.session_factory() as session:
            person = await session.get(Person, person_id)
            if person is None:
                return
            due, away = await arrival_is_due(
                session,
                person,
                now=now,
                # News that came after it make the coming back an arrival (``visits``).
                last_page_arrival=turns.last_page_arrival(person_id),
            )
            # Someone without a key, whom the general key does not serve to ask for theirs, is
            # offered no arrival turn.
            keyed = bool(await usable_keys(session, person))
            if due and not keyed:
                due = await upkeep_key(session) is not None
            # Their plans due at their return, when they come back after an absence (a plan goes
            # on their own key: none without one, they wait).
            returned = away is None or away >= ARRIVAL_AFTER
            back = (
                (await due_on_return(session, person_id, now))[:PLANS_AT_ONCE]
                if returned and keyed
                else []
            )
            await session.commit()
    except Exception as error:
        logger.error("A coming back could not be weighed (%s)", type(error).__name__)
        return
    if returned:
        # The tools of Domo that launch at their return, in a task of their own: whatever comes
        # of them never touches the coming back.
        deps.wakes.spawn(returns_seen(made_tool_deps(deps), person_id, now))
    if not due:
        if back:
            deps.wakes.spawn(_plans_on_return(deps, person_id, [plan.id for plan in back]))
        await consider_news(deps, person_id, now=now)
        return
    offered_at = deps.clock()
    turns.offered(person_id, offered_at)
    deps.wakes.spawn(
        _arrive(
            deps,
            turns,
            person_id=person_id,
            away=away,
            # Their last visit, exactly: what is new is counted from it, as when it was weighed.
            since=None if away is None else now - away,
            offered_at=offered_at,
            page=True,
            back=back,
        )
    )


def news_seen(deps: TurnDeps, person: Person, *, now: datetime) -> None:
    """A channel says ``person`` is there at ``now``: when news wait for them that could be told
    now (``ArrivalTurns.could_be_due``, read from the memory alone: news that cannot be offered
    cost no reading of the database), they are considered in a task of ``deps.wakes``
    (``consider_news``)."""
    turns = deps.arrival_turns
    news_at = person.news_at
    if turns is None or news_at is None:
        return
    if not turns.could_be_due(person.id, news_at=news_at, now=now):
        return
    deps.wakes.spawn(consider_news(deps, person.id, now=now))


def news_kept(deps: TurnDeps, person_id: uuid.UUID) -> None:
    """News were just kept for ``person_id`` (committed): when a listener of theirs hears what
    Domo says unasked, they are there, and the news are considered now, in a task of
    ``deps.wakes`` (``consider_news``). A person who is only on a page is told at the page's next
    ask, which says they are there (``news_seen``). It returns at once, and never raises."""
    turns, listeners = deps.arrival_turns, deps.listeners
    if turns is None or listeners is None or not listeners.hearing(person_id):
        return
    try:
        deps.wakes.spawn(consider_news(deps, person_id, now=deps.clock()))
    except Exception as error:
        logger.error("News could not be considered (%s)", type(error).__name__)


async def consider_news(
    deps: TurnDeps, person_id: uuid.UUID, *, now: datetime, news_at: datetime | None = None
) -> None:
    """``person_id`` is there at ``now``, and news may wait for them: when ``Choices.arrival`` is
    not ``never``, offer Domo an arrival turn for them now (a news turn), unless a reply of
    theirs is under way (a reply being written in their conversation, an arrival turn's included:
    never stopped, the news wait for the next sign that they are there) or
    ``ArrivalTurns.news_turn_due`` says no (one a minute, once per piece of news). Nothing for
    someone with no key Domo may use for them (the news wait for it), nor where there are no
    arrival turns. ``news_at``, when the caller knows it, is weighed first against the memory
    alone (``ArrivalTurns.could_be_due``). Never raises: what goes wrong is logged, by its kind."""
    turns = deps.arrival_turns
    if turns is None:
        return
    try:
        if news_at is not None and not turns.could_be_due(person_id, news_at=news_at, now=now):
            return
        async with deps.session_factory() as session:
            found = await session.execute(
                select(Person)
                .where(Person.id == person_id)
                .execution_options(populate_existing=True)
            )
            person = found.scalar_one_or_none()
            if person is None or person.news_at is None or not is_active(person, now):
                return
            last_visit = person.last_visit_at
            if (await load_choices(session, None)).arrival == "never":
                return
            if not await usable_keys(session, person):
                return
            if await reply_under_way(session, deps, person_id, now):
                return
            news_at = person.news_at
        if not turns.news_turn_due(person_id, news_at=news_at, now=now):
            return
        # What is new is counted from their last arrival turn (from their last visit when the
        # process knows of none), and from before these news at the latest.
        counted_from = turns.last_settled(person_id) or last_visit
        before_the_news = news_at - _BEFORE
        offered_at = deps.clock()
        turns.offered(person_id, offered_at)
        deps.wakes.spawn(
            _arrive(
                deps,
                turns,
                person_id=person_id,
                away=None if last_visit is None else now - last_visit,
                since=before_the_news
                if counted_from is None
                else min(counted_from, before_the_news),
                offered_at=offered_at,
                page=False,
            )
        )
    except Exception as error:
        logger.error("A news turn could not be offered (%s)", type(error).__name__)


async def reply_under_way(
    session: AsyncSession, deps: TurnDeps, person_id: uuid.UUID, now: datetime
) -> bool:
    """Whether a reply is being written in the conversation of ``person_id`` (``reply_is_alive``):
    an answer to their message, a wake's, an arrival turn's. A news turn waits for it, and so does
    a review of what was said (``memory.review``)."""
    store = ConversationStore(session)
    conversation = await store.latest_conversation(person_id)
    if conversation is None:
        return False
    streaming = await store.streaming_message(conversation.id)
    return streaming is not None and reply_is_alive(streaming, deps.streaming_replies, now)


async def _arrive(
    deps: TurnDeps,
    turns: ArrivalTurns,
    *,
    person_id: uuid.UUID,
    away: timedelta | None,
    since: datetime | None,
    offered_at: datetime,
    page: bool,
    back: Sequence[Plan] = (),
) -> None:
    """The arrival turn of ``person_id``, offered at ``offered_at`` (``page``: by their coming
    back, not by news), with their plans due at their return (``back``). Once it ended, in words
    or in silence, the news that came before it are settled (``ArrivalTurns.settled``); not when
    it failed, nor when it was dropped or stopped (the plans then wait for their next return).
    Never raises but for its cancellation (a message of the person stopped it): what goes wrong is
    logged, by its kind."""
    held: list[uuid.UUID] = []

    def hold(reply_id: uuid.UUID) -> None:
        turns.hold(reply_id)
        held.append(reply_id)

    try:
        ended = await _arrival(
            deps, person_id=person_id, away=away, since=since, hold=hold, back=back
        )
        if ended in ("spoke", "silent"):
            turns.settled(person_id, offered_at, page=page)
    except Exception as error:
        logger.error("An arrival turn failed (%s)", type(error).__name__)
    finally:
        for reply_id in held:
            turns.release(reply_id)


async def _arrival(
    deps: TurnDeps,
    *,
    person_id: uuid.UUID,
    away: timedelta | None,
    since: datetime | None,
    hold: Callable[[uuid.UUID], None],
    back: Sequence[Plan] = (),
) -> TurnEnd | None:
    """Open the reply of the arrival in the person's conversation (made if they have none yet),
    held by ``hold``, then write the turn, its words to whoever listens to them (``Fanout``): what
    ``_arrive`` runs. ``back`` are the plans of theirs due at their return, given in this turn
    (``Due now``) and settled once it ended (``settle_plans``). How the turn ended (``None``: no
    reply was opened)."""
    async with deps.session_factory() as session:
        conversation = await ConversationStore(session).active_conversation(person_id)
        await session.commit()
    opened = await open_reply(deps, conversation.id, person_id, waits=False, upkeep=True, hold=hold)
    if opened is None:
        return None
    reply, person, _ = opened
    started = deps.clock()
    handed_over = False
    try:
        async with deps.session_factory() as session:
            new = await what_is_new(session, person, since=since)
            due = await due_plans_of(session, back, deps.clock())
        minutes = None if away is None else int(away.total_seconds() // 60)
        turn = await prepare_turn(
            deps=deps,
            person=person,
            reply=reply,
            request=unasked(deps, person),
            arrival=ArrivalNote(away_minutes=minutes, new=new),
            silent_ok=True,
            notify=notifier(deps),
            due_plans=due,
            # Why he acts: their coming back; with plans due, the plans' (none when each keeps
            # their words: it is their request, as in a plan's turn, ruling R39).
            turn_reason=plans_reason(due) if due else ARRIVAL_REASON,
        )
        handed_over = True  # the turn releases the claim when it ends
    finally:
        if not handed_over:
            deps.streaming_replies.discard(reply.id)
            await take_back(deps, reply.id, presented=False)
    ended = await turn.run(Fanout(deps.listeners, person.id, reply.id))
    if back and turn.blocked is None:
        await settle_plans(
            deps, back, came_to(ended, acted=turn.acted, took_effect=turn.took_effect), started
        )
    return ended


# --- a plan's time -------------------------------------------------------------------------------


def due_plan_of(
    plan: Plan, now: datetime, builds: Mapping[uuid.UUID, tuple[BuildState, str]] | None = None
) -> DuePlan:
    """``plan``, due at ``now``, as the turn it wakes Domo for tells it (``prompts.DuePlan``):
    how late it is when that is more than ``LATE_AFTER`` (one due at the person's return alone, or
    woken by the end of its build before its time, is never late), and, for a plan that waits for
    a build (``builds``, ``plans.service.builds_waited_on``), how that build stands and, once it is
    ready, the name of its tool."""
    late = None if plan.due_at is None else now - plan.due_at
    waited = None if plan.after_build_id is None else (builds or {}).get(plan.after_build_id)
    build: Literal["ready", "abandoned", "building"] | None = None
    tool: str | None = None
    if waited is not None:
        state, name = waited
        build = "ready" if state == "ready" else "abandoned" if state == "abandoned" else "building"
        tool = name if state == "ready" else None
    return DuePlan(
        id=str(plan.id),
        why=plan.why,
        their_words=plan.their_words,
        late_minutes=int(late // timedelta(minutes=1))
        if late is not None and late > LATE_AFTER
        else None,
        build=build,
        tool=tool,
    )


async def due_plans_of(
    session: AsyncSession, plans: Sequence[Plan], now: datetime
) -> tuple[DuePlan, ...]:
    """``plans``, due at ``now``, as their turn tells them (``due_plan_of``), with how the builds
    they wait for stand, read in ``session``."""
    builds = await builds_waited_on(session, plans)
    return tuple(due_plan_of(plan, now, builds) for plan in plans)


def came_to(ended: TurnEnd, *, acted: bool, took_effect: bool) -> PlanOutcome:
    """What came of a turn plans were due in, as each one keeps it (``plans.last_outcome``): it
    spoke, it acted (an action that is shown, whatever came of it) without a word, it did
    nothing, or it failed. A turn that failed once an action of it took effect (``took_effect``:
    done, or a request made) counts as having acted: what it did is not done again; one whose
    actions were all refused, invalid or failed, and that then failed, is a failure (the plans
    stay due)."""
    if ended == "failed":
        return "acted" if took_effect else "failed"
    if ended == "spoke":
        return "spoke"
    return "acted" if acted else "silent"


async def settle_plans(
    deps: TurnDeps, given: Sequence[Plan], outcome: PlanOutcome, started: datetime
) -> bool:
    """The plans ``given`` (as they were read before the turn) were due in a turn that started at
    ``started`` (when they were last due: ``last_fired_at``) and came to ``outcome``: each one is
    settled (``plans.service.fired``: a plan that does not repeat is done,
    one that repeats is due at its next time), whatever Domo did, read again first. One that he
    set again, changed or cancelled in the turn (it no longer is as it was read) keeps what he
    did: only when it was last due and what came of it are written, and a time he left at or before
    the turn's start is served (``plans.service.time_served``), as is the end of a build it already
    waited for (``served_by_its_build``). After a turn that ``failed``,
    nothing moves: each one stays due, with ``failed`` as what came of it last.

    Each plan is settled apart (a savepoint of its own): one that cannot be settled stays as it
    was, due, and never holds back the others. Whether every one was settled; never raises: a
    failure is logged, by its kind."""
    if not given:
        return True
    settled = True
    try:
        now = deps.clock()
        async with deps.session_factory() as session:
            zone = (await load_policy_settings(session)).time_zone or ZoneInfo("UTC")
            for read in given:
                try:
                    async with session.begin_nested():
                        await _settle_one(
                            session, read, outcome, now=now, zone=zone, started=started
                        )
                except Exception as error:
                    settled = False
                    logger.error("A plan due could not be settled (%s)", type(error).__name__)
            await session.commit()
    except Exception as error:
        logger.error("The plans due could not be settled (%s)", type(error).__name__)
        return False
    return settled


async def _settle_one(
    session: AsyncSession,
    read: Plan,
    outcome: PlanOutcome,
    *,
    now: datetime,
    zone: ZoneInfo,
    started: datetime,
) -> None:
    """Settle the plan ``read`` (``settle_plans``), read again and locked. Flushed."""
    found = await session.execute(
        select(Plan)
        .where(Plan.id == read.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    plan = found.scalar_one_or_none()
    if plan is None:
        return
    if outcome == "failed":
        plan.last_outcome = outcome
    elif plan.state != "waiting" or plan.changed_at != read.changed_at:
        # Domo set it again, changed it or cancelled it: that stays, but the end of a build it
        # was given no longer wakes it (one he pointed at a build in the turn still waits for it),
        # and a time of it that he left where it was is served.
        plan.last_fired_at = started
        plan.last_outcome = outcome
        if plan.state == "waiting":
            await served_by_its_build(session, plan, started)
            await time_served(session, plan, started=started, now=now, zone=zone)
    else:
        await fired(session, plan, now=now, zone=zone, outcome=outcome, started=started)
    await session.flush()


async def _plans_on_return(
    deps: TurnDeps, person_id: uuid.UUID, plan_ids: Sequence[uuid.UUID]
) -> None:
    """The plan's turn of someone who came back with no arrival turn, for their plans due at
    their return (``_welcome``)."""
    await plan_turn(deps, person_id, plan_ids)


async def plan_turn(
    deps: TurnDeps, person_id: uuid.UUID, plan_ids: Sequence[uuid.UUID]
) -> PlanTurnEnd | None:
    """A turn nobody asked for, for ``person_id``, with the plans ``plan_ids`` of theirs whose
    time came under Domo's eyes (spec 6.9; read again: those that no longer wait are left out),
    all of them in one turn (``Due now``, ``PLAN_MARKER``): he speaks, acts with the person's
    tools and rights, sets one again for later, or does nothing; a reply with no words in which
    nothing that is shown acted is taken away (``silent_ok``). Its words go to whoever listens to
    them (``Fanout``), and wait in the conversation for the others. Each plan is then settled
    (``settle_plans``), and what came of the turn is given back: ``failed`` when a plan could not
    be settled (it is still due).

    ``None`` when no turn ran, and nothing changed: the person left, a reply of theirs is under
    way (``reply_under_way``; one being written in their conversation), or they have no key Domo
    may use for them (``open_reply``). ``no_room`` when every key of theirs is at its limit
    (``Turn.blocked``), or none was left once the reply was opened (``NoKey``): the reply it
    opened is taken back, no reply is written, not even an error, and the plans stay due; the pass
    waits before it tries again. Never raises: what goes wrong is logged, by its kind (a
    ``failed`` turn)."""
    try:
        return await _plan_turn(deps, person_id, plan_ids)
    except Exception as error:
        logger.error("A plan's turn failed (%s)", type(error).__name__)
        return "failed"


async def _plan_turn(
    deps: TurnDeps, person_id: uuid.UUID, plan_ids: Sequence[uuid.UUID]
) -> PlanTurnEnd | None:
    now = deps.clock()
    async with deps.session_factory() as session:
        person = await session.get(Person, person_id)
        if person is None or not is_active(person, now):
            return None
        if await reply_under_way(session, deps, person_id, now):
            return None
        found = await session.scalars(
            select(Plan)
            .where(Plan.id.in_(plan_ids), Plan.person_id == person_id, Plan.state == "waiting")
            .execution_options(populate_existing=True)
        )
        order = {plan_id: place for place, plan_id in enumerate(plan_ids)}
        given = sorted(found.all(), key=lambda plan: order[plan.id])
        if not given:
            return None
        due = await due_plans_of(session, given, now)
        conversation = await ConversationStore(session).active_conversation(person_id)
        await session.commit()
    opened = await open_reply(deps, conversation.id, person_id, waits=False)
    if opened is None:
        return None
    reply, person, _ = opened
    turn: Turn | None = None
    try:
        with contextlib.suppress(NoKey):
            turn = await prepare_turn(
                deps=deps,
                person=person,
                reply=reply,
                request=unasked(deps, person),
                silent_ok=True,
                notify=notifier(deps),
                due_plans=due,
                turn_reason=plans_reason(due),
            )
    finally:
        if turn is None or turn.blocked is not None:
            # No turn, and no reply in error either: the plans wait for a key, or room under
            # a limit.
            deps.streaming_replies.discard(reply.id)
            await take_back(deps, reply.id, presented=False)
    if turn is None or turn.blocked is not None:
        return "no_room"
    ended = await turn.run(Fanout(deps.listeners, person.id, reply.id))
    outcome = came_to(ended, acted=turn.acted, took_effect=turn.took_effect)
    if outcome == "failed":
        # Nobody reads its events: its failure would be seen nowhere.
        logger.warning("A plan's turn of the assistant ended in error")
    if not await settle_plans(deps, given, outcome, now):
        # A plan that could not be settled is still due: a failure, so that it waits once it
        # keeps failing rather than coming back at every pass.
        return "failed"
    return outcome
