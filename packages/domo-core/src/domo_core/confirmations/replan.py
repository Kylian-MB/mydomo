"""A chain of actions that waits for its yes, changed as a whole (spec 4.1).

The person answers the chain with a change ("yes, but..."), and Domo gives the whole chain as he
understood it (``confirmations.service.amend_confirmation``): its calls, in order, the steps
already done first, as they were. This module reads those calls and says where each step still to
do comes from; ``confirmations.service`` then plans the chain again (``tools.chain.DoInOrder.plan``)
and the yes-judge judges it again; it runs at once, or becomes a new request
(``replace_chain_request``):

* ``calls_of``: the calls, each a tool and its arguments (``{"tool", "arguments"}``, the form a
  request stores, read by ``step_call`` as ``tools.chain`` reads the steps of a new chain), with
  the name of the invitation whose arrival it waits for when it waits for one
  (``"after_arrival_of"``, spec 4.3b); a chain inside the chain is not taken here: Domo gives its
  steps one by one;
* ``done_prefix_holds``: the steps already done are the first calls, as they were (the same tool,
  the same arguments and the same wait, in order): what was done cannot change;
* ``unchanged``: the calls are the chain as it is, in the same order: that is no change (it is
  refused, ``invalid``), never run as the person's change;
* ``match_steps``: each call still to do, matched with the steps still to do. A call that is a step
  as it was asked for (the same tool, the same arguments and the same wait: a wait changed is a step
  changed) is ``kept``, wherever it stands now: a step taken away or put in another order does not
  change those around it. Each other call comes from the first step not matched yet with the same
  tool, ``changed``; a call with no such step is ``added``: the person never saw it on a card, and
  the judge is told it with the rest (the owner's rules and default yeses count for it). A step
  kept as it was asked for is judged with what the chain remembers of the one who asked, a step
  changed or added with the role of now (spec 4.1, last point: the role a chain remembers holds
  only for what was asked when the person had it);
* ``replace_chain_request``: the new request that replaces the chain, its steps done copied as they
  are (their wait included), then its steps as planned again (each with its wait);
* ``keep_notes_as_written``: an approver who changes someone else's chain (spec 5.1) was never
  told the words of its notes (D4), and never changes a note, neither its words nor its recipient
  (the foundation, spec 6.7: a note is its author's alone): each note step they give back is a
  note of the chain to the same recipient, as it was written, whatever words the call carries;
  they may only take one away;
* ``notes_as_told``: the one who asked, who changes their own chain while it waits for someone
  else's yes (spec 5.2), was told its notes without their words too (a waiting note reaches no
  model): a note they give back as their Domo was told of it keeps its words as they wrote them,
  to whomever they send it now; other words are theirs, new.

A chain that stopped is taken up again the same way (``tools.chain.ChainContinue``, spec 4.4,
4.7): its steps left (the one that failed, then those it stopped) are the rows ``match_steps``
matches the calls Domo gives with (``call_of``: a row as such a call); when the judge, the owner's
rules on what changed, or the accord the rest needs ask a new yes, it is asked for with
``replace_chain_request``.
"""

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.steps import add_steps
from domo_core.confirmations.store import create_confirmation
from domo_core.llm.types import JsonObject
from domo_core.policy.asks import Asked
from domo_core.policy.barrier import Refuse
from domo_core.policy.decision import ChainBasis
from domo_core.policy.rights import Approver
from domo_core.policy.roles import Actor
from domo_core.text import InvalidDisplayName, display_name_value, name_key
from domo_core.tools.context import InvalidArguments

if TYPE_CHECKING:
    # Only a type here: the chain's module imports the confirmations' service, which imports this.
    from domo_core.tools.chain import ChainPlan

# Where a step of a changed chain comes from (see the module docstring).
StepKind = Literal["kept", "changed", "added"]

# The keys of a step of a chain: a tool and its arguments, and, for a step that waits for
# someone's arrival, the name of their invitation (``WAITS_FOR``); nothing else.
STEP_KEYS: Final = frozenset({"tool", "arguments"})
WAITS_FOR: Final = "after_arrival_of"
# The chain's own tool: a step that calls it is flattened in a new chain (``tools.chain``), and
# not taken in a change.
CHAIN_TOOL: Final = "do_in_order"
# The tool of a note, which is its author's alone (``keep_notes_as_written``).
NOTE_TOOL: Final = "note_leave"
# What Domo is told when an approver's change would change a note, its words or its recipient.
NOTE_IS_THE_AUTHORS: Final = (
    "a note is its author's alone, its words and its recipient: give it back as it is, to the same "
    "recipient (its words stay as they were), or take it away; never change its words or its "
    "recipient"
)
# What starts the line of a step as Domo is told of a chain (``describe.steps_told_of``): its
# number, and "(done) " before a step already done. A note step given back as its whole line is
# matched without them (``notes_as_told``).
_TOLD_LINE_START: Final = re.compile(r"\s*(?:\d+\.\s*)?(?:\(done\)\s*)?")


@dataclass(frozen=True)
class StepMatch:
    """Where a step still to do of a changed chain comes from: ``kind`` (see ``match_steps``),
    and ``old_position``, the position of the row it comes from when it is kept or changed
    (``None`` when it is added)."""

    kind: StepKind
    old_position: int | None


def step_call(item: object, number: int) -> tuple[str, JsonObject, str | None]:
    """A step as the arguments of a chain give it: the name of a tool, its arguments, and the name
    of the invitation whose arrival it waits for (``WAITS_FOR``, a display name: one plain line of
    1 to 60 characters, without the spaces around it), or ``None`` when it waits for nobody.
    ``InvalidArguments`` for anything else (``number`` is the step's, from 1, which the detail
    says, never what it holds)."""
    keys = frozenset(cast(dict[object, object], item)) if isinstance(item, dict) else None
    if keys is None or keys - {WAITS_FOR} != STEP_KEYS:
        raise InvalidArguments(
            "invalid",
            f"step {number}: a step is a tool and its arguments (and, when it waits for someone's "
            f"arrival, {WAITS_FOR}), and nothing else",
        )
    step = cast(dict[str, object], item)
    name, arguments = step["tool"], step["arguments"]
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise InvalidArguments(
            "invalid", f"step {number}: the tool is a name, and its arguments a JSON object"
        )
    if WAITS_FOR not in step:
        return name, cast(JsonObject, arguments), None
    try:
        after = display_name_value(step[WAITS_FOR])
    except InvalidDisplayName as refused:
        raise InvalidArguments(
            refused.code,
            f"step {number}: {WAITS_FOR} is the name of an invitation ({refused.detail})",
        ) from None
    return name, cast(JsonObject, arguments), after


def as_call(name: str, arguments: JsonObject, after: str | None) -> JsonObject:
    """A step as Domo gives it, and as a request stores it: ``{"tool", "arguments"}``, with
    ``WAITS_FOR`` when it waits for someone's arrival."""
    call: JsonObject = {"tool": name, "arguments": dict(arguments)}
    if after is not None:
        call[WAITS_FOR] = after
    return call


def calls_of(steps: object) -> list[JsonObject]:
    """The calls of a changed chain (``steps``, as Domo gives them), each ``{"tool",
    "arguments"}`` and the arrival it waits for, if any (``as_call``), in order.
    ``InvalidArguments`` unless ``steps`` is a list of tools, each with its arguments and nothing
    else but a wait (``step_call``), none of them a chain (its steps are given one by one). How
    many there may be is the plan's to say (``tools.chain``)."""
    if not isinstance(steps, list):
        raise InvalidArguments("invalid", "steps is a list of tools, each with its arguments")
    calls: list[JsonObject] = []
    for number, item in enumerate(cast(list[object], steps), start=1):
        name, arguments, after = step_call(item, number)
        if name == CHAIN_TOOL:
            raise InvalidArguments(
                "invalid",
                f"step {number}: a chain inside the chain is not taken here: give the steps one by "
                "one",
            )
        calls.append(as_call(name, arguments, after))
    return calls


def _text_of(arguments: object, name: str) -> str | None:
    """The text ``name`` of a call's arguments, if they hold one."""
    if not isinstance(arguments, dict):
        return None
    value = cast(dict[str, object], arguments).get(name)
    return value if isinstance(value, str) else None


def same_recipient(given: object, stored: object) -> bool:
    """Whether the arguments of a note ``given`` name the recipient of the note ``stored`` (the
    same name, ``text.name_key``)."""
    first, second = _text_of(given, "recipient"), _text_of(stored, "recipient")
    return first is not None and second is not None and name_key(first) == name_key(second)


def keep_notes_as_written(
    calls: Sequence[JsonObject], rows: Sequence[ConfirmationStep]
) -> list[JsonObject]:
    """The ``calls`` of a chain whose steps are ``rows``, as an approver changed it (spec 5.1),
    with each note as the one who asked wrote it: an approver never changes a note, neither its
    words, which their Domo was never told (D4), nor its recipient (spec 6.7: a note is its
    author's alone, and their words go only to whom they chose). A note step given back with the
    recipient of a note of the chain not matched yet (in order: ``same_recipient``) is that note,
    as it was written (its arguments as stored), whatever words the call carries.
    ``InvalidArguments`` for any other note step (``NOTE_IS_THE_AUTHORS``, with the number of its
    step): one with another recipient, or one more than the chain had. A note taken away is
    simply gone. The other calls are as they were given."""
    left = [row for row in rows if row.tool_name == NOTE_TOOL]
    kept: list[JsonObject] = []
    for number, call in enumerate(calls, start=1):
        if call["tool"] != NOTE_TOOL:
            kept.append(call)
            continue
        same = next((row for row in left if same_recipient(call["arguments"], row.arguments)), None)
        if same is None:
            raise InvalidArguments("invalid", f"step {number}: {NOTE_IS_THE_AUTHORS}")
        left.remove(same)
        kept.append({**call, "arguments": dict(same.arguments)})
    return kept


def notes_as_told(
    calls: Sequence[JsonObject],
    rows: Sequence[ConfirmationStep],
    told: Callable[[ConfirmationStep], Collection[str]],
) -> list[JsonObject]:
    """The ``calls`` of a chain whose steps are ``rows``, as the one who asked changed it while it
    waits for someone else's yes (spec 5.2): their Domo was told its notes without their words
    (D4: a waiting note reaches no model), each in the sentences ``told`` gives for its row ("a
    note to Owner (2 characters)", with or without whom it waits for). A note step whose words are
    what Domo was told of a note of the chain not matched yet keeps that note's words, as they
    were written; among notes told alike, the one with the same recipient (``same_recipient``),
    else the first, in order. Only its words are taken back: its recipient, and the rest of the
    call, are as given (the one who asked is the author of their notes, and may send one to
    someone else). A note given back as the whole line Domo was told of it, with its number and
    "(done) " (``_TOLD_LINE_START``), is matched as the sentence it holds. Any other call is as it
    was given, new words included."""
    left = [row for row in rows if row.tool_name == NOTE_TOOL]
    kept: list[JsonObject] = []
    for call in calls:
        given = _text_of(call["arguments"], "text") if call["tool"] == NOTE_TOOL else None
        alike = [row for row in left if given is not None and _told_of(given, told(row))]
        same = next(
            (row for row in alike if same_recipient(call["arguments"], row.arguments)),
            alike[0] if alike else None,
        )
        words = None if same is None else _text_of(same.arguments, "text")
        if same is None or words is None:
            kept.append(call)
            continue
        left.remove(same)
        kept.append({**call, "arguments": {**call["arguments"], "text": words}})
    return kept


def _told_of(given: str, sentences: Collection[str]) -> bool:
    """Whether the words ``given`` for a note are one of the ``sentences`` Domo was told of it:
    as they are, or as the whole line of its step (``_TOLD_LINE_START``)."""
    if given in sentences:
        return True
    start = _TOLD_LINE_START.match(given)
    return start is not None and given[start.end() :].rstrip() in sentences


def call_of(row: ConfirmationStep) -> JsonObject:
    """The call of the step ``row``, as Domo gives a step (``as_call``)."""
    return as_call(row.tool_name, dict(row.arguments), row.after_arrival_of)


def _same_call(row: ConfirmationStep, call: JsonObject) -> bool:
    """Whether ``call`` is the call of ``row``: the same tool, the same arguments and the same wait
    (a wait changed, added or taken away is a change of the step)."""
    return (
        row.tool_name == call["tool"]
        and dict(row.arguments) == call["arguments"]
        and row.after_arrival_of == call.get(WAITS_FOR)
    )


def done_prefix_holds(done: Sequence[ConfirmationStep], new: Sequence[JsonObject]) -> bool:
    """Whether the steps ``done`` (in order) are the first of the calls ``new``, as they were."""
    return len(new) >= len(done) and all(
        _same_call(row, call) for row, call in zip(done, new, strict=False)
    )


def unchanged(rows: Sequence[ConfirmationStep], new: Sequence[JsonObject]) -> bool:
    """Whether the calls ``new`` are the chain whose steps are ``rows`` (all of them, in order), as
    it is: the same steps, with the same arguments, in the same order."""
    return len(new) == len(rows) and all(
        _same_call(row, call) for row, call in zip(rows, new, strict=True)
    )


def match_steps(
    old: Sequence[ConfirmationStep], new: Sequence[JsonObject]
) -> tuple[StepMatch, ...]:
    """Where each of the calls ``new`` comes from among the rows ``old`` (the steps still to do of
    the chain, in order), in two passes (see the module docstring): first each call that is a row
    as it was asked for (the first such row not matched yet), ``kept``; then each other call, in
    order, from the first row not matched yet with the same tool, ``changed``, else ``added``. A
    row is matched once at most, so the calls ``added`` of a tool are as many as the calls of that
    tool beyond its rows."""
    left = list(old)
    found: list[StepMatch | None] = []
    for call in new:
        same = next((row for row in left if _same_call(row, call)), None)
        if same is not None:
            left.remove(same)
        found.append(None if same is None else StepMatch("kept", same.position))
    matches: list[StepMatch] = []
    for call, match in zip(new, found, strict=True):
        if match is None:
            other = next((row for row in left if row.tool_name == call["tool"]), None)
            if other is None:
                match = StepMatch("added", None)
            else:
                left.remove(other)
                match = StepMatch("changed", other.position)
        matches.append(match)
    return tuple(matches)


def _copy_of(row: ConfirmationStep, confirmation_id: UUID, position: int) -> ConfirmationStep:
    """A step done, as it is, for the request ``confirmation_id`` at ``position``."""
    return ConfirmationStep(
        confirmation_id=confirmation_id,
        position=position,
        tool_name=row.tool_name,
        action_type=row.action_type,
        arguments=dict(row.arguments),
        action=None if row.action is None else dict(row.action),
        status=row.status,
        result=None if row.result is None else dict(row.result),
        done_at=row.done_at,
        with_basis=row.with_basis,
        after_arrival_of=row.after_arrival_of,
    )


async def replace_chain_request(
    session: AsyncSession,
    old: Confirmation,
    *,
    requester: Actor,
    plan: "ChainPlan",
    calls: Sequence[JsonObject],
    done: Sequence[ConfirmationStep],
    approver: Approver,
    asked: Asked | None,
    question: str | None,
    message_id: UUID | None,
    now: datetime,
    shown_to_approver: UUID | None = None,
    approver_note: str | None = None,
) -> Confirmation:
    """The new request of the chain ``old``, changed (``calls``, the steps ``done`` first) and
    planned again for ``requester`` (``plan``): its call with all of its steps and the chain's own
    action; whose yes it waits for (``approver``), what it asks (``asked``) and the question a
    limit asked (``question``); what ``old`` remembers of the one who asked (``ChainBasis``: the
    role a kept step is judged with stays the one of the request), and its conversation;
    ``message_id`` is the reply that shows it to the one who asked, and ``shown_to_approver`` the
    reply of an approver that shows it, in their own conversation, when an approver changed it
    (spec 5.1), with what they said (``approver_note``). Its steps: those ``done``, copied as they
    are, from position 0, then the steps of the plan. The invitation ``old`` waited for last
    (``waiting_for_invitation_id``) is the new request's too: its card says who joined to whoever
    gives its yes (spec 4.3b), and its run finds that invitation first. ``old`` is ``replaced`` by
    it. Flushed, not committed: the caller audits and commits.
    ``ValueError`` when the plan was refused, or ``old`` is no chain."""
    if isinstance(plan.decision, Refuse):
        raise ValueError("a request is made for a chain that may be done")
    new = await create_confirmation(
        session,
        requester=requester,
        tool_name=old.tool_name,
        arguments={"steps": [dict(call) for call in calls]},
        action=plan.action,
        approver=approver,
        asked=asked,
        conversation_id=old.conversation_id,
        message_id=message_id,
        now=now,
        question=question,
        chain_basis=ChainBasis.from_json(old.chain_basis),
        shown_to_approver=shown_to_approver,
        approver_note=approver_note,
    )
    new.waiting_for_invitation_id = old.waiting_for_invitation_id
    for position, row in enumerate(done):
        session.add(_copy_of(row, new.id, position))
    await add_steps(session, new.id, plan.steps, start=len(done))
    old.status = "replaced"
    old.replaced_by = new.id
    await session.flush()
    return new
