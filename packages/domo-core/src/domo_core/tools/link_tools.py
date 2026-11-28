"""The assistant notes what one person is to another, and takes it back: ``link_note`` and
``link_drop``.

A link is "Dana is the mother of Alex" (``people.links``). Both tools are silent: nobody is asked,
nothing is shown (``policy.catalog``); the code's own guards hold on them (a link stands only by
the word of who can know), and an idea of the assistant's still reaches them
(``SILENT_YET_HIS_IDEA``). No tool here checks the speaker's rights: a person's own request is the
rights' to refuse (the gestures are in no role), a call the assistant marked as his own is the
judge's to weigh.

Whose act it is follows ``people.whose``: a call carries ``reason`` exactly when it is the
assistant's own. What the tools give back is data for the assistant: he writes every text himself.
"""

from typing import Final

from domo_core.accounts.models import Person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import links, records, whose
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.people_tools import record_named

# What a call leaves under the reply: a card nobody reads (``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_REASON_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": (
        "Only when the idea is yours (after act_on_my_own): why you do it, in one sentence. "
        "Leave it out when the person asked for it."
    ),
    "minLength": 1,
    "maxLength": whose.MAX_REASON_LENGTH,
}
_RELATION_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": (
        "What the first is to the second, in your own plain words, so that the sentence "
        "'<person> is <relation> <to>' reads right: 'mother of', 'partner of', 'friend of'."
    ),
    "minLength": 1,
    "maxLength": links.MAX_RELATION_LENGTH,
}
_WHY_NOT: Final = {
    "not_a_record": "this record is not one any more",
    "same_record": "a link is between two different records",
    "bad_relation": f"the relation is one plain line, {links.MAX_RELATION_LENGTH} letters at most",
}


def _only(arguments: JsonObject, allowed: frozenset[str], needed: frozenset[str]) -> None:
    keys = frozenset(arguments)
    if not needed <= keys or not keys <= allowed:
        raise InvalidArguments(
            "invalid",
            f"the arguments are {', '.join(sorted(needed))} and, if needed, "
            f"{', '.join(sorted(allowed - needed))}",
        )


def _text(arguments: JsonObject, argument: str) -> str:
    value = arguments.get(argument)
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", f"{argument} is a text")
    return value


def _relation(arguments: JsonObject) -> str:
    try:
        return links.plain_relation(_text(arguments, "relation"))
    except records.NotPossible as error:
        raise InvalidArguments("not_possible", _WHY_NOT.get(error.code, error.code)) from None


def _refused(error: records.NotPossible) -> InvalidArguments:
    return InvalidArguments("not_possible", _WHY_NOT.get(error.code, error.code))


class _LinkTool:
    """What both tools share: the two records a call is about, and the facts it describes."""

    action_type: str
    person_arguments: Final = ("person", "to", "said_by")
    names_among: Final = "records"

    async def _ends(self, arguments: JsonObject, ctx: ToolContext) -> tuple[Person, str, Person]:
        async with ctx.session_factory() as session:
            start = await record_named(session, _text(arguments, "person"), ctx)
            end = await record_named(session, _text(arguments, "to"), ctx)
        return start, _relation(arguments), end

    async def _described(self, arguments: JsonObject, ctx: ToolContext) -> DescribedAction:
        start, _, end = await self._ends(arguments, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{start.id}",
            effect="self_profile",
            destination="household",
            facts={"from": f"person:{start.id}", "to": f"person:{end.id}"},
        )


class LinkNote(_LinkTool):
    """Note what one person is to another."""

    definition = ToolDefinition(
        name="link_note",
        description=(
            "Note what one person is to another: Dana is the mother of Alex, Sam is a friend of "
            "Paul. Say it in your own words, and who said it (said_by): leave it out when a "
            "person asks you, it is then their own word; when you deduced it yourself, it has "
            "nobody. A link stands only when the household's owner said it, or one of the two "
            "people said it while having access: what someone else says, or a newcomer says of "
            "themselves, is only their word until the household's owner or the one it is about "
            "confirms. "
            "The same link is kept once. Name them as the household knows them, or by "
            "their identifier when two records have the same name. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "The first one: name, or person:<id>."},
                "relation": _RELATION_PROPERTY,
                "to": {"type": "string", "description": "The second one: name, or person:<id>."},
                "said_by": {
                    "type": "string",
                    "description": "Who said it: their name, or person:<id>.",
                },
                "reason": _REASON_PROPERTY,
            },
            "required": ["person", "relation", "to"],
            "additionalProperties": False,
        },
    )
    action_type = "people.link"

    @staticmethod
    def _read(arguments: JsonObject) -> None:
        _only(
            arguments,
            frozenset({"person", "relation", "to", "said_by", "reason"}),
            frozenset({"person", "relation", "to"}),
        )
        _relation(arguments)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        self._read(arguments)
        return await self._described(arguments, ctx)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        self._read(arguments)
        by = whose.by(arguments, ctx)
        his_own = whose.is_his_own(arguments, ctx)
        async with ctx.session_factory() as session:
            start = await record_named(session, _text(arguments, "person"), ctx)
            end = await record_named(session, _text(arguments, "to"), ctx)
            said_by: Person | None
            heard_from: Person | None = None
            if "said_by" in arguments:
                said_by = await record_named(session, _text(arguments, "said_by"), ctx)
                if not his_own and said_by.id != ctx.person.id:
                    # "Sam told me he is Paul's uncle": the word is the speaker's, heard from
                    # someone else; it stands as theirs, and the result says from whom.
                    heard_from = said_by
                    said_by = await session.get(Person, ctx.person.id)
            else:
                said_by = None if his_own else await session.get(Person, ctx.person.id)
            now = ctx.clock()
            try:
                link = await links.note_link(
                    session,
                    from_person=start,
                    relation=_relation(arguments),
                    to_person=end,
                    said_by=said_by,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
            except records.NotPossible as error:
                raise _refused(error) from None
            noted: JsonObject = {
                "id": str(link.id),
                "relation": link.relation,
                "stands": link.confirmed_at is not None,
            }
            if heard_from is not None:
                noted["heard_from"] = heard_from.display_name
            await session.commit()
        return ToolOutcome(status="done", model_result=noted, card=_CARD)


class LinkDrop(_LinkTool):
    """Take back a link."""

    definition = ToolDefinition(
        name="link_drop",
        description=(
            "Take back a link that was wrong or is no longer true: name the same two people and "
            "the same words as when it was noted. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "The first one: name, or person:<id>."},
                "relation": _RELATION_PROPERTY,
                "to": {"type": "string", "description": "The second one: name, or person:<id>."},
                "reason": _REASON_PROPERTY,
            },
            "required": ["person", "relation", "to"],
            "additionalProperties": False,
        },
    )
    action_type = "people.unlink"

    @staticmethod
    def _read(arguments: JsonObject) -> None:
        _only(
            arguments,
            frozenset({"person", "relation", "to", "reason"}),
            frozenset({"person", "relation", "to"}),
        )
        _relation(arguments)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        self._read(arguments)
        return await self._described(arguments, ctx)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        self._read(arguments)
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            start = await record_named(session, _text(arguments, "person"), ctx)
            end = await record_named(session, _text(arguments, "to"), ctx)
            link = await links.find(session, start.id, end.id, _relation(arguments))
            if link is None:
                raise InvalidArguments("not_found", "no such link between these two records")
            await links.drop(
                session,
                link,
                by=by,
                reason=whose.reason_of(arguments),
                turn_of=whose.turn_of(arguments, ctx),
                now=ctx.clock(),
            )
            await session.commit()
        return ToolOutcome(status="done", model_result={"dropped": True}, card=_CARD)
