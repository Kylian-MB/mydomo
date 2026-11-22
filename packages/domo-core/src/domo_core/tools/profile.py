"""``profile_rename``: the person who speaks changes the name Domo and the household call them.
``domo_name``: they give Domo a name of their own, or call him Domo again (spec 6.7).

The name is the only argument, and the rule of every display name (``text.display_name_value``):
1 to 60 characters once the spaces around it are removed, one plain line (it is shown on its own
line, to other people too), with no bidirectional control (they can make a name look like another
one to the people who read it). It must not be the name of someone else in the household, or of
an invitation that waits: a copy would let a person speak as another ("From Alex"). That is
checked when the call is described and again when it runs, under the lock of the display names.
The new name is never in the described action, which goes to the audit log: only who is renamed
is.
"""

from typing import Final

from sqlalchemy import update

from domo_core.accounts.models import Person
from domo_core.household.service import NameInUse, name_in_use, rename_person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import (
    MAX_DISPLAY_NAME_LENGTH,
    InvalidDisplayName,
    cleaned_name,
    is_plain_line,
)
from domo_core.text import display_name_value as _display_name_value
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

_NAME_IN_USE: Final = (
    "someone in the household, or an invitation that waits, already has this name: choose a "
    "name that tells them apart"
)


def display_name_from(arguments: JsonObject) -> str:
    """The name of ``{"name": ...}``, without the spaces around it. ``InvalidArguments`` unless
    it is the only argument and a text of 1 to ``MAX_DISPLAY_NAME_LENGTH`` plain characters."""
    if set(arguments) != {"name"}:
        raise InvalidArguments("invalid", 'the only argument is "name"')
    return display_name_value(arguments["name"])


def display_name_value(value: object) -> str:
    """A name people are called by, without the spaces around it: ``text.display_name_value``,
    with a refusal worded for a tool call (``InvalidArguments``)."""
    try:
        return _display_name_value(value)
    except InvalidDisplayName as refused:
        raise InvalidArguments(refused.code, refused.detail) from None


class ProfileRename:
    """Rename the person who speaks (never anyone else)."""

    definition = ToolDefinition(
        name="profile_rename",
        description=(
            "Change the name of the person you are talking with: the name you and the household "
            "use for them. Only their own name, and only when they ask for it. Never the name "
            "they call you by: that is domo_name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The new name, as the person wants to be called.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    action_type = "profile.rename"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name = display_name_from(arguments)
        async with ctx.session_factory() as session:
            if await name_in_use(session, name, now=ctx.clock(), excluding=actor.person_id):
                raise InvalidArguments("invalid", _NAME_IN_USE)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{actor.person_id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = display_name_from(arguments)
        async with ctx.session_factory() as session:
            try:
                renamed = await rename_person(session, ctx.person, name, now=ctx.clock())
            except NameInUse:
                raise InvalidArguments("invalid", _NAME_IN_USE) from None
            if not renamed:
                return ToolOutcome("failed", {}, {"key": "action.card.failed", "params": {}})
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"name": name},
            card={"key": "action.card.done.profile.rename", "params": {"name": name}},
        )


# The longest name a person gives Domo (the ``persons`` table says the same).
MAX_DOMO_NAME_LENGTH: Final = 40
# What a call leaves under the reply: a card nobody reads (``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}


def domo_name_from(arguments: JsonObject) -> str | None:
    """The name of ``{"name": ...}`` as it is kept: ``None`` for ``null`` or a name that shows
    nothing (he is Domo again); else the name read like a name (``text.cleaned_name``: NFKC, no
    invisible character, one space between words). ``InvalidArguments`` unless it is the only
    argument, one plain line (no control character, line break or direction mark) and
    ``MAX_DOMO_NAME_LENGTH`` characters at most."""
    if set(arguments) != {"name"}:
        raise InvalidArguments("invalid", 'the only argument is "name"')
    value = arguments["name"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "the name is a text, or null")
    if not is_plain_line(value.strip()):
        raise InvalidArguments(
            "invalid", "the name has a control character, a line break or a direction mark"
        )
    name = cleaned_name(value)
    if len(name) > MAX_DOMO_NAME_LENGTH:
        raise InvalidArguments(
            "too_long", f"the name has more than {MAX_DOMO_NAME_LENGTH} characters"
        )
    return name or None


class DomoName:
    """The name the person who speaks calls Domo by (never anyone else's): theirs alone, shown
    to them where the code writes "Domo" itself (their page's header and title, the program they
    talk to him with) and told to him in their turns. Never judged, never shown: it changes
    nothing in the household (``policy.catalog.SILENT_ACTIONS``)."""

    definition = ToolDefinition(
        name="domo_name",
        description=(
            "Change the name the person you are talking with calls YOU by, for them alone (never "
            "their own name: that is profile_rename). Whenever they say they want to call you "
            "something, would like you to be called something, or ask whether they may "
            '("I would '
            'like to call you Friday", "appelle-toi Brenda", "can I call you Max?"), call it in '
            "that same answer, then simply answer to that name. Agreeing in words without "
            "calling it changes nothing. Do not explain how it works, do not mention tools, and "
            "do not say it holds for them alone unless they ask about the others; if they want "
            "the name for the whole household, say simply that each person chooses their own "
            "name for you and that you took it for them. Null: they call you the assistant again. "
            "You "
            "stay the same, whatever the name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": ["string", "null"],
                    "description": "The name they call you by, or null for the assistant.",
                    "maxLength": MAX_DOMO_NAME_LENGTH,
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    action_type = "profile.name_domo"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        domo_name_from(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{actor.person_id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = domo_name_from(arguments)
        async with ctx.session_factory() as session:
            await session.execute(
                update(Person).where(Person.id == ctx.person.id).values(calls_domo=name)
            )
            await session.commit()
        return ToolOutcome(
            status="done", model_result={"they_call_you": name or "Domo"}, card=_CARD
        )
