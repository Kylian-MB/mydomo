"""The turns a tool of Domo wakes him for (spec 5.1, 5.4, 5.8; moved from
``conversation.starts``).

A program of a tool Domo made may wake him for whom its launch is (``madetools.means``, given by
``madetools.handover``), and a run a turn handed over wakes him for its person once it ended:
``tool_wake_turn``, a plan's turn in all but its plans (``TOOL_MARKER``; what the tools say in the
context, as data). A tool he launches there may wake him again: what bounds it is the tool's own
count of wakes a day (its ``wakes_per_day``, a setting of Domo's), past which it is set aside. The
runs handed over that a restart left undelivered are delivered by the upkeep (``tool_wakes_pass``).

A launch for a person wakes Domo in their conversation, where he acts with their tools and their
rights of now. A launch for the house wakes him in the owner's conversation, on his own
(``on_his_own``: a wake ``madetools.handover`` reads from ``made_tool_runs.for_house``, or from the
wakes a chain of launches for the house gathered): nobody asked, so every call of that turn is
judged with his own rights (``tools.context.ToolContext.on_his_own``,
``conversation.execution.judged_actor_of``), what is about the owner's own things is not his by
default there (``policy.catalog.NOT_ON_HIS_OWN``), and what he launches there is for the house. What
he says there is said to the owner, whose conversation it is. The same wake of the house tells him,
on his own, that one of his tools is in trouble (``madetools.troubles``: set aside, or a build
abandoned, with why and the last error its program said, as data), so that he looks at it,
corrects it, goes back a version or leaves it.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.chat.store import ConversationStore
from domo_core.conversation.listeners import Fanout
from domo_core.conversation.prepare import NoKey, TurnDeps, made_tool_deps, prepare_turn
from domo_core.conversation.starts import notifier, open_reply, take_back, unasked
from domo_core.conversation.turn import Turn
from domo_core.madetools.handover import WAKE_DROPPED, deliver_runs
from domo_core.prompts import ToolWake
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)


async def tool_wake_turn(
    deps: TurnDeps,
    person_id: uuid.UUID,
    wakes: Sequence[ToolWake],
    *,
    on_his_own: bool = False,
) -> bool:
    """A turn nobody asked for, in the conversation of ``person_id``, because tools of Domo woke
    him with ``wakes`` (one or more tools of the same chain: spec 5.4, 5.8,
    ``madetools.handover``). It is a plan's turn in all but its plans: what each tool says is in
    the context, as data (``prepare_turn`` with ``tool_wakes``), never in the conversation, which
    ends with ``TOOL_MARKER``, nor in what the yes-judge reads; Domo speaks, acts, or does nothing
    (a reply with no words in which nothing that is shown acted is taken away). Its words go to
    whoever listens to them (``Fanout``), and wait in the conversation for the others.

    For a launch made for the person, he acts with their tools and their rights of now. For a
    launch made for the house (``on_his_own``: ``person_id`` is then the owner, who is woken for
    the house), he acts on his own: every call is judged with his own rights, and what he launches
    is for the house (``prepare_turn`` with ``on_his_own``).

    Someone who left the household is woken for nothing: the wake is dropped, and audited
    (``WAKE_DROPPED``: the tools' names alone). A reply of theirs under way is waited for
    (``open_reply``: a tool may wake Domo for the person whose turn launched it). No turn without
    a key Domo may use for them, or with every key at its limit: the reply opened is taken back,
    nothing is written.

    Whether the turn ran: the person was told, or Domo chose to say nothing (``True``, and so
    for a wake dropped, for nobody or no wake at all, for a turn that failed once something of it
    got out, words kept or any action taken, a silent one included: ``Turn.left_anything``, and
    for a turn stopped halfway once something of it got out, whose cancellation ends here);
    ``False`` when it could not run (the conversation stayed busy, no key, every key at its limit,
    or the turn failed with nothing of it out), for the caller to try again
    (``madetools.handover``). Never raises but for a cancellation before anything of the turn got
    out (before it ran, or while nothing of it was out), which goes on untouched: the caller leaves
    the wake to be given again. What goes wrong otherwise is logged, by its kind, and is
    ``False``."""
    if not wakes:
        return True
    try:
        return await _tool_wake(deps, person_id, wakes, on_his_own=on_his_own)
    except Exception as error:
        logger.error("A tool's turn failed (%s)", type(error).__name__)
        return False


async def _tool_wake(
    deps: TurnDeps, person_id: uuid.UUID, wakes: Sequence[ToolWake], *, on_his_own: bool
) -> bool:
    async with deps.session_factory() as session:
        person = await session.get(Person, person_id)
        if person is not None and not is_active(person, deps.clock()):
            tools: list[JsonValue] = [*sorted({wake.tool for wake in wakes})]
            await append_audit(
                session, actor="system", action=WAKE_DROPPED, details={"tools": tools}
            )
            await session.commit()
            return True
        if person is None:
            return True
        conversation = await ConversationStore(session).active_conversation(person_id)
        await session.commit()
    opened = await open_reply(deps, conversation.id, person_id)
    if opened is None:
        return False
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
                tool_wakes=tuple(wakes),
                on_his_own=on_his_own,
            )
    finally:
        if turn is None or turn.blocked is not None:
            # No turn, and no reply in error either: what the tool said waits to be tried again.
            deps.streaming_replies.discard(reply.id)
            await take_back(deps, reply.id, presented=False)
    if turn is None or turn.blocked is not None:
        return False
    try:
        ended = await turn.run(Fanout(deps.listeners, person.id, reply.id))
    except asyncio.CancelledError:
        if not turn.left_anything:
            # Stopped before anything of it got out: given again after the restart.
            raise
        # Stopped halfway (the core stopping) once something of it got out: what it began is
        # kept as it was cut, and it is never given again. The task ends here, its cancellation
        # taken.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        logger.info("A tool's turn of the assistant was stopped before its end")
        return True
    if ended == "failed":
        # Nobody reads its events: its failure would be seen nowhere.
        logger.warning("A tool's turn of the assistant ended in error")
        # Words of it kept, or anything it did (a tool launched silently included): never given
        # again. Only a turn of which nothing got out is tried again.
        return turn.left_anything
    return True


def tool_wakes_pass(deps: TurnDeps) -> UpkeepPass:
    """The pass of the upkeep that delivers the runs of tools a turn handed over that a restart
    left (``madetools.handover.deliver_runs``): those claimed and never delivered, and those that
    never finished (Domo is told the run did not finish); and the wakes whose turn could not
    run, tried again later and later for a day. Delivering only wakes: nothing is carried out
    twice. The turns it wakes run in tasks of ``deps.wakes``."""
    made = made_tool_deps(deps)

    async def deliver_the_runs_left(now: datetime) -> None:
        del now  # each run is delivered at the time of the clock of its delivery
        await deliver_runs(made)

    return deliver_the_runs_left
