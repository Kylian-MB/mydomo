"""``successors_show``, ``successors_set``, ``successors_note_death`` and
``successors_undo_death``: the owner's ordered list of who takes over the owner's role if the owner
can no longer (spec 7), kept in ``household.succession``.

All four are the owner's alone, whatever any right says (``catalog.OWNER_ONLY_ACTIONS``): the
barrier refuses them to anyone else, and the model of anyone else is not shown them. Nothing their
description reads tells anyone else what the list holds: a change looks for its names only when
the owner asks (someone who left the household is found on the list alone, so the answer to anyone
else would tell who is on it), and a death only reads its name as a name, until it is noted.

* ``successors_show`` (it changes nothing: never judged): the list, each place as
  ``SuccessorShown.as_json`` tells it: the rank among the living, the name, whether the person is in
  the household and their role, the day they left it, the day they died, the relation and the way
  noted to open the door;
* ``successors_set`` (judged like any action: nothing is asked by default): replace the whole
  list. Each place is a person of the household
  (``{"person": <name>}``, found by their display name, a child accepted) or someone outside it
  (``{"name": <name>, "relation": <what the owner says of them>}``), with an optional ``"access"``
  (``personal_access`` or ``paper_code``). Domo proposes the way to open the door and recommends
  the better one for each person; it is only noted. The card shows the list before and after
  (``confirmations.describe.successor_lines_of``). The change and its audit entry
  (``successors.changed``: the names, before and after, in order) are one transaction, the audit
  entry written last;
* ``successors_note_death`` (judged): the owner says that a successor died. Nobody is asked to
  confirm a death they have just announced (the judge is told so), and it gives
  no right to anyone, it only takes the successor out of the running (the next one comes first).
  The audit entry is ``successors.death_noted`` (the name);
* ``successors_undo_death`` (judged like any action): the owner says that a death was noted by
  mistake (spec 5.4), and the successor is put back in the running. Unlike the death, it gives a
  place back on the list. Its audit entry is
  ``successors.death_undone`` (the name). Whether the death is noted is read before anything is
  asked, for the owner (``describe``: a death that was never noted is refused at once, spec
  5.5.3; for anyone else the list is not looked at, the barrier refuses them), and again when the
  request runs.

When a stored request to change the list runs after the yes, the names are found again: a person
who has left meanwhile is ``not_found`` and nothing is changed.
"""

from typing import Final

from pydantic import JsonValue

from domo_core.audit.log import append_audit
from domo_core.household.succession import (
    MAX_RELATION_LENGTH,
    MAX_SUCCESSORS,
    SuccessorShown,
    entries_from,
    list_successors,
    note_death,
    noted_death,
    replace_successors,
    undo_death,
)
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import InvalidDisplayName, display_name_value
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

_SET_ARGUMENTS: Final = frozenset({"successors"})
_DEATH_ARGUMENTS: Final = frozenset({"name"})
# What every place of the list is described with in the card of a change and in the audit.
_TARGET: Final = "successors"

_NO_ARGUMENTS: Final[JsonObject] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _listed(shown: list[SuccessorShown]) -> list[JsonObject]:
    return [place.as_json() for place in shown]


def _names(shown: list[SuccessorShown]) -> list[JsonValue]:
    """The names of the places, in order (what the card says and the audit holds)."""
    return [place.name for place in shown]


def _items_from(arguments: JsonObject) -> object:
    """The places of a ``successors_set`` call. ``InvalidArguments`` unless ``successors`` is the
    only argument."""
    if frozenset(arguments) != _SET_ARGUMENTS:
        raise InvalidArguments("invalid", "the only argument is successors, a list")
    return arguments["successors"]


def _name_from(arguments: JsonObject) -> str:
    """The name of a ``successors_note_death`` or ``successors_undo_death`` call, without the
    spaces around it. ``InvalidArguments`` unless it is the only argument and a plain name."""
    if frozenset(arguments) != _DEATH_ARGUMENTS:
        raise InvalidArguments("invalid", 'the only argument is "name"')
    try:
        return display_name_value(arguments["name"])
    except InvalidDisplayName as refused:
        raise InvalidArguments(refused.code, refused.detail) from None


class SuccessorsShow:
    """Read the owner's list of successors."""

    definition = ToolDefinition(
        name="successors_show",
        description=(
            "See their list of successors: who takes over their role if they can no longer, "
            "in order. Each place has its rank among the living (null once the "
            "successor died, then the next one comes first), the name, whether the person is "
            "in the household and their role (a child will have to be an adult by then), the "
            "day they left the household or died if they did, what they say of them, and "
            "the way noted to open the door the day it comes. Someone who left the household "
            "stays on the list until they decide otherwise: tell them, without pressing."
        ),
        parameters=_NO_ARGUMENTS,
    )
    action_type = "successors.show"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")
        async with ctx.session_factory() as session:
            shown = await list_successors(session, ctx.clock())
        return ToolOutcome(
            status="done",
            model_result={"successors": _listed(shown)},
            card={"key": "action.card.done.successors.show", "params": {}},
        )


class SuccessorsSet:
    """Replace the owner's list of successors."""

    definition = ToolDefinition(
        name="successors_set",
        description=(
            "Change their list of successors, when that is what they want: it "
            "replaces the whole list, so give every successor, in order, first one first (0 to "
            f"{MAX_SUCCESSORS}; an empty list clears it). A successor is someone of the "
            'household ({"person": the name the household knows them by}; a child is fine) or '
            'an outsider ({"name": their name, "relation": what they say of them, such as '
            '"my brother"}); no contact details yet. For each one, propose the way to open the '
            "door the day it comes and recommend the best one for that person: a personal access "
            "that only claims the role (if they may know about it) or a paper code kept with the "
            'will (if they must not); their choice goes in "access": "personal_access" or '
            '"paper_code". It only notes the way: nothing is built or given now. When a '
            "successor left the household or is a child, you may suggest a replacement, at a "
            "moment that suits the link between them and the successor, never pressing. "
            "A request that waits shows the list before and after."
        ),
        parameters={
            "type": "object",
            "properties": {
                "successors": {
                    "type": "array",
                    "maxItems": MAX_SUCCESSORS,
                    "description": "Every successor, first one first.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "person": {
                                "type": "string",
                                "description": "The name of someone of the household.",
                            },
                            "name": {
                                "type": "string",
                                "description": "The name of someone outside the household.",
                            },
                            "relation": {
                                "type": "string",
                                "maxLength": MAX_RELATION_LENGTH,
                                "description": "What they are to the person, such as 'my brother'.",
                            },
                            "access": {
                                "type": "string",
                                "enum": ["personal_access", "paper_code"],
                                "description": "How the door is opened the day it comes.",
                            },
                        },
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["successors"],
            "additionalProperties": False,
        },
    )
    action_type = "successors.set"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        items = _items_from(arguments)
        count = 0
        if actor.role == "owner":
            # The names are looked for only for the owner: someone who left is found on the list
            # alone, so whoever else asks would learn from the answer who is on it. They are
            # refused by the barrier (``not_allowed``), whatever names they gave.
            async with ctx.session_factory() as session:
                count = len(await entries_from(session, items, ctx.clock()))
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=_TARGET,
            effect="permissions",
            destination="household",
            facts={"count": count},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        items = _items_from(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            entries = await entries_from(session, items, now)
            try:
                before, after = await replace_successors(session, entries, now)
            except ValueError as refused:
                raise InvalidArguments("invalid", str(refused)) from None
            # Last, just before the commit: the names, in order.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="successors.changed",
                details={"before": _names(before), "after": _names(after)},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"successors": _listed(after)},
            # The people of the list before and after, in order: each reader's card says them in
            # their own texts (``conversation.execution``).
            card={
                "key": "action.card.done.successors.set",
                "params": {},
                "successors": {"before": _names(before), "after": _names(after)},
            },
        )


class SuccessorsNoteDeath:
    """Note that a successor died (no confirmation: the next one comes first)."""

    definition = ToolDefinition(
        name="successors_note_death",
        description=(
            "Note that one of their successors died, when they tell you so. Do it "
            "with tact: be kind and unhurried, and say you are sorry before anything else. It "
            "takes no confirmation: your judge never asks one for a bereavement. The successor "
            "stays on the list, marked as deceased, and the next one comes first. Name them as "
            "they are on the list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name of the successor who died."}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    action_type = "successors.note_death"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = _name_from(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=_TARGET,
            effect="permissions",
            destination="household",
            facts={"name": name},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = _name_from(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            died = await note_death(session, name, now)
            shown = await list_successors(session, now)
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="successors.death_noted",
                details={"name": died.name},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"successors": _listed(shown)},
            card={
                "key": "action.card.done.successors.note_death",
                "params": {"name": died.name},
            },
        )


class SuccessorsUndoDeath:
    """Put a successor whose death was noted by mistake back in the running."""

    definition = ToolDefinition(
        name="successors_undo_death",
        description=(
            "Put one of their successors back in the running, when they say their "
            "death was noted by mistake (a slip of the tongue, a wrong name). Be kind and "
            "unhurried about it. The successor takes their place on the list again and counts in "
            "the order. Unlike a death, it gives a place back. "
            "Only a successor "
            "whose death is noted can be put back: look at the list first (successors_show)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the successor whose death was noted by mistake.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    action_type = "successors.undo_death"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = _name_from(arguments)
        if actor.role == "owner":
            # Only a death that is noted can be undone: refused before anything is asked (spec
            # 5.5.3), with what undoing it would say (``invalid``, ``not_found``, ``ambiguous``).
            # The list is the owner's alone: for anyone else it is not looked at, and the barrier
            # refuses them, so that nothing they are told says whether a name is on it.
            async with ctx.session_factory() as session:
                await noted_death(session, name, ctx.clock())
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=_TARGET,
            effect="permissions",
            destination="household",
            facts={"name": name},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = _name_from(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            back = await undo_death(session, name, now)
            shown = await list_successors(session, now)
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="successors.death_undone",
                details={"name": back.name},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"successors": _listed(shown)},
            card={
                "key": "action.card.done.successors.undo_death",
                "params": {"name": back.name},
            },
        )
