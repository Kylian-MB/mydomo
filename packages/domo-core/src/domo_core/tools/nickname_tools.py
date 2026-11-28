"""``nickname_keep`` and ``nickname_forget``: the names the person who speaks uses for the others
("Sam" for Samuel, "Juju" for someone else), theirs alone (spec 6.7, ``household.nicknames``).

Once kept, a nickname finds its person in that person's turns (``household.service.resolve_person``,
after the whole name): Domo acts on it directly, and the card, the judge and the stored request
read the whole name (``household.service.whole_names``). Whether to ask first ("Sam, is that
Samuel?") is Domo's judgement; the description says when it is worth it.

A nickname is that person's own way of naming someone. Whether it is theirs is Domo's judgement,
in any turn: the code refuses none for where a text of the turn comes from, nor because nobody
wrote in it; the description tells him that what a note, someone else or a tool says they call
someone is a hint, not their word. A
whole name always means its owner: a nickname that reads as the whole name of someone in the
household, or of an invitation that waits, is refused.

Both are silent (``policy.catalog.SILENT_ACTIONS``): never judged, never shown. The nickname is
never in the described action, which goes to the audit log.
"""

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.household import nicknames
from domo_core.household.service import active_persons, open_invitations, resolve_person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import whose
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import cleaned_name, is_plain_line, name_key
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

MAX_NICKNAME_LENGTH: Final = nicknames.MAX_NICKNAME_LENGTH
# What a call leaves under the reply: a card nobody reads (``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}

_A_WHOLE_NAME: Final = (
    "this is the whole name of someone in the household, or of an invitation that waits: it "
    "always means them, there is nothing to keep"
)


def nickname_from(value: object) -> str:
    """A nickname as it is kept: read like a name (``text.cleaned_name``: NFKC, no invisible
    character, one space between words). ``InvalidArguments`` unless it is a text of one plain
    line (no control character, line break or direction mark), of 1 to ``MAX_NICKNAME_LENGTH``
    characters once cleaned."""
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "the nickname is a text")
    if not is_plain_line(value.strip()):
        raise InvalidArguments(
            "invalid", "the nickname has a control character, a line break or a direction mark"
        )
    nickname = cleaned_name(value)
    if not nickname:
        raise InvalidArguments("invalid", "the nickname is empty")
    if len(nickname) > MAX_NICKNAME_LENGTH:
        raise InvalidArguments(
            "too_long", f"the nickname has more than {MAX_NICKNAME_LENGTH} characters"
        )
    return nickname


def _keep_arguments(arguments: JsonObject) -> tuple[str, str]:
    if set(arguments) != {"nickname", "person"}:
        raise InvalidArguments("invalid", 'the arguments are "nickname" and "person"')
    person = arguments["person"]
    if not isinstance(person, str) or not person.strip():
        raise InvalidArguments("invalid", "the person is the name of someone in the household")
    return nickname_from(arguments["nickname"]), person


def _forget_argument(arguments: JsonObject) -> str:
    """The nickname to forget. Forgetting changes whom a short name means (it falls back to a
    first name or a beginning): whether the person wants it is Domo's judgement."""
    if set(arguments) != {"nickname"}:
        raise InvalidArguments("invalid", 'the only argument is "nickname"')
    return nickname_from(arguments["nickname"])


def _described(action_type: str, actor: Actor) -> DescribedAction:
    """Only whose nicknames change: never the nickname, nor whom it means."""
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=f"person:{actor.person_id}",
        effect="self_profile",
        destination="household",
    )


async def _keepable(session: AsyncSession, nickname: str, person: str, ctx: ToolContext) -> Person:
    """The one person in the household ``person`` means to the person who speaks, once
    ``nickname`` may be kept for them: it is no whole name of someone in the household or of an
    invitation that waits. ``InvalidArguments`` otherwise."""
    now = ctx.clock()
    key = name_key(nickname)
    names = [p.display_name for p in await active_persons(session, now)]
    names += [i.display_name for i in await open_invitations(session, now)]
    if any(name_key(name) == key for name in names):
        raise InvalidArguments("invalid", _A_WHOLE_NAME)
    return await resolve_person(
        session,
        person,
        now=now,
        speaker=ctx.person.id,
        among="records",
        detailed=whose.may_see_every_record(ctx, ctx.acting),
    )


class NicknameKeep:
    """Keep the name the person who speaks uses for someone of the household (theirs alone)."""

    definition = ToolDefinition(
        name="nickname_keep",
        description=(
            "Keep the short name or nickname the person you are talking with uses for someone of "
            'the household ("Sam" for Samuel, "Juju"), for them alone: from then on that name '
            "finds that person in their requests. When they use a short name or a nickname for "
            "someone that you have not heard from them before, and you are not sure who is meant "
            "or it matters who (something that touches that person, or that cannot be undone), "
            "ask once, in plain words and in their language, whether they mean that person, and "
            "keep "
            "their answer with this tool; when it is plain who is meant, keep it without asking. "
            "Once it is kept, act on it directly and never ask again. A nickname is this person's "
            "own way of naming someone: others may use other names, or the same one for someone "
            "else. So keep it from how they name people themselves. "
            "What a note, someone else or a tool says they call someone is a hint, not their "
            "word: ask them when it matters who is meant. Never a whole name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "nickname": {
                    "type": "string",
                    "description": "The name they use, as they say it.",
                    "minLength": 1,
                    "maxLength": MAX_NICKNAME_LENGTH,
                },
                "person": {
                    "type": "string",
                    "description": "The whole name of the one they mean.",
                    "minLength": 1,
                },
            },
            "required": ["nickname", "person"],
            "additionalProperties": False,
        },
    )
    action_type = "people.nickname_keep"
    # The argument that names someone of the household: given its whole name before the call is
    # described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        nickname, person = _keep_arguments(arguments)
        async with ctx.session_factory() as session:
            await _keepable(session, nickname, person, ctx)
        return _described(self.action_type, actor)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        nickname, person = _keep_arguments(arguments)
        async with ctx.session_factory() as session:
            target = await _keepable(session, nickname, person, ctx)
            await nicknames.keep(
                session,
                person_id=ctx.person.id,
                nickname=nickname,
                target_id=target.id,
                now=ctx.clock(),
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"nickname": nickname, "means": target.display_name},
            card=_CARD,
        )


class NicknameForget:
    """Forget a name the person who speaks used for someone (theirs alone)."""

    definition = ToolDefinition(
        name="nickname_forget",
        description=(
            "Forget a nickname the person you are talking with kept for someone (with "
            "nickname_keep), when they no longer use it or it was wrong. Only their own."
        ),
        parameters={
            "type": "object",
            "properties": {
                "nickname": {
                    "type": "string",
                    "description": "The nickname to forget.",
                    "minLength": 1,
                    "maxLength": MAX_NICKNAME_LENGTH,
                }
            },
            "required": ["nickname"],
            "additionalProperties": False,
        },
    )
    action_type = "people.nickname_forget"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _forget_argument(arguments)
        return _described(self.action_type, actor)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        nickname = _forget_argument(arguments)
        async with ctx.session_factory() as session:
            forgotten = await nicknames.forget(session, person_id=ctx.person.id, nickname=nickname)
            await session.commit()
        return ToolOutcome(status="done", model_result={"forgotten": forgotten}, card=_CARD)
