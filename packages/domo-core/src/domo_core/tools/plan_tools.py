"""``plan_set``, ``plan_change`` and ``plan_cancel``: the plans Domo sets for himself (spec 6.9).

A plan is the person's of the turn: the one who asks for it, or the one Domo plans it for ("remind
Sam tomorrow" stays the plan of whoever asks; when it is due, Domo leaves Sam a note in their
turn). Each tool sees and changes that person's own plans only: someone else's plan id, or the id
of a plan that no longer waits, is ``not_found``.

They are everyone's, a guest's too, and nothing about them is judged or shown
(``policy.catalog.SILENT_ACTIONS``): setting a plan changes nothing by itself, and what Domo does
once it is due is checked then, with the rights of that day (spec 6.9). The model reads the result
of each call, or why it was refused, in the turn.

``when`` is an ISO 8601 date and time, with its offset or without one (then read in the
household's time zone, UTC when the owner set none); a time already past is refused. ``repeat``
is an iCalendar rule (``plans.schedule``) computed in the household's time zone. ``on_return``
makes the plan due when the person comes back too. ``after_build`` makes it wait for the build of
a tool (spec 5.5, the id ``tool_build`` returned): it is due when that build ends, or at its time
if that comes first; only a build the person opened, one for them or one of the house may be
waited for, and never one that was abandoned. The exact words of the person it comes from
(``their_words``) are kept only when they are a passage of their message, word for word
(``tools.notes.exact_text_in``; otherwise the result says they were ``not_kept``), whatever
else the turn holds: what they ask beside a note someone left them is their request (the owner,
2026-11-27), and words that are only in the note, or in what a tool gave, are in no message of
theirs, so never kept as theirs. A new ``why`` takes the passage of their message it comes from
in the same way (``plan_change``'s ``their_words``, in place of the words the plan kept); given
without one in a turn that is not their own answer (``ToolContext.answers_them``: nobody wrote, or
text that is not theirs is in it), or with one that is not theirs, it empties the words the plan
kept, since it no longer comes from them. A passage given without a new ``why`` is not read. None
are kept by what he does as his own (``acts_alone``). Neither refuses anything: the plan is set
or changed all the same.
A plan due counts as asked only by the words it keeps (``conversation.prepare``). Without them,
in a turn that answers no message of theirs (a plan's turn included), the plan is Domo's own
(``made_by_domo``), unless someone spoke or words or text from outside are in it (``_his_alone``).
What has the shape of a secret is refused in why it is set and in those words
(``memory.service.refuses_secret_shapes``).

The audit log keeps that a plan was set (``plan.made``), changed (``plan.changed``) or cancelled
(``plan.cancelled``), with its id alone, never why nor the person's words.
"""

import uuid
from datetime import UTC, datetime
from types import EllipsisType
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.madetools.models import ToolBuild
from domo_core.memory.service import SecretRefused, refuses_secret_shapes
from domo_core.plans.models import Plan
from domo_core.plans.schedule import MAX_REPEAT, as_local, valid_repeat
from domo_core.plans.service import MAX_WHY, cancel, change, make, waiting_plan_of
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.text import is_plain_line
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome, acts_alone
from domo_core.tools.notes import MAX_NOTE_LENGTH, exact_text_in

# What a call leaves under the reply: a card nobody reads (the plans are never shown,
# ``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_UTC: Final = ZoneInfo("UTC")
SECRET_REFUSED: Final = (
    "this looks like a secret: secrets are never kept in a plan. Nothing was set. If it is not "
    "a secret, say it differently (in words, without a long code, a long number or a run of "
    "letters and digits) and try again"
)
PAST: Final = "this time is already past"
# What the result says of exact words that are no passage of the person's message: not kept.
NOT_KEPT: Final = "not_kept"
_NO_MOMENT: Final = (
    "a plan needs a time (when), a rule (repeat), the person's return (on_return) or the build "
    "of a tool (after_build)"
)
_NO_BUILD: Final = (
    "no build you may wait for has this id: give the id tool_build or tool_change returned. "
    "Nothing was set"
)
_BUILD_GAVE_UP: Final = (
    "this build was abandoned: no such tool will be made. Do the request another way. Nothing "
    "was set"
)
_BAD_RULE: Final = (
    "repeat is an iCalendar RRULE such as FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0, never more "
    "often than every minute; a rule of weeks, days, hours or minutes is not limited by "
    "BYMONTHDAY, BYYEARDAY, BYWEEKNO or BYSETPOS (say it with FREQ=MONTHLY or FREQ=YEARLY)"
)
_NO_TIME_TO_COME: Final = "this rule gives no time to come"
_NOT_FOUND: Final = "no plan of the person's that waits has this id"


async def household_zone(session: AsyncSession) -> ZoneInfo:
    """The household's time zone, UTC when the owner set none."""
    return (await load_policy_settings(session)).time_zone or _UTC


def _only(arguments: JsonObject, required: frozenset[str], allowed: frozenset[str]) -> None:
    if not required <= frozenset(arguments) <= allowed:
        names = ", ".join(sorted(allowed))
        raise InvalidArguments("invalid", f"the arguments are among {names}")


def _why(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "why is a text")
    text = value.replace("\r\n", "\n").strip()
    if not text:
        raise InvalidArguments("invalid", "why is empty")
    if len(text) > MAX_WHY:
        raise InvalidArguments("too_long", f"why has more than {MAX_WHY} characters")
    if not all(is_plain_line(part) for part in text.split("\n")):
        raise InvalidArguments(
            "invalid", "why has a control character, a separator or a direction mark"
        )
    if refuses_secret_shapes(text):
        raise InvalidArguments("invalid", SECRET_REFUSED)
    return text


def _when(value: object) -> datetime:
    """``value`` read as a date and a time: aware when it has its offset, else on the household's
    clock (made aware by ``_in_zone`` once the zone is read)."""
    if not isinstance(value, str) or ("T" not in value and " " not in value.strip()):
        raise InvalidArguments("invalid", "when is an ISO 8601 date and time")
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise InvalidArguments("invalid", "when is an ISO 8601 date and time") from None


def _in_zone(moment: datetime, zone: ZoneInfo, now: datetime) -> datetime:
    """``moment`` aware (on the household's clock when it has no offset), in UTC; a time already
    past is refused."""
    aware = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment
    if aware <= now:
        raise InvalidArguments("invalid", PAST)
    return aware.astimezone(UTC)


def _repeat(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", _BAD_RULE)
    if len(value) > MAX_REPEAT:
        raise InvalidArguments("too_long", f"repeat has more than {MAX_REPEAT} characters")
    rule = value.strip()
    if not valid_repeat(rule):
        raise InvalidArguments("invalid", _BAD_RULE)
    return rule


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArguments("invalid", f"{name} is true or false")
    return value


def _plan_id(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "a plan's id is a text")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidArguments("not_found", _NOT_FOUND) from None


def _build_id(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "after_build is the id of a build, a text")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidArguments("not_found", _NO_BUILD) from None


def _his_alone(ctx: ToolContext) -> bool:
    """Whether a plan set or given a new why in this turn is the assistant's alone: it answers no
    message of theirs (or he acts alone), and nobody spoke meanwhile, no sentence was heard and no
    words came from outside (Task 27: such words never become his)."""
    speaker = ctx.speaker
    return (
        (ctx.reply_message_id is None or acts_alone(ctx))
        and not ctx.outside_text
        and not ctx.heard_meanwhile.raised
        and (speaker is None or speaker.nobody_spoke)
    )


async def _build_to_wait_for(session: AsyncSession, build_id: uuid.UUID) -> uuid.UUID:
    """``build_id`` when it is a build the person may wait for: any build of Domo's tools (they
    are all his), not abandoned (``not_possible`` otherwise). One that is ready already is
    accepted: the plan is due at the next pass."""
    build = await session.get(ToolBuild, build_id)
    if build is None:
        raise InvalidArguments("not_possible", _NO_BUILD)
    if build.state == "abandoned":
        raise InvalidArguments("not_possible", _BUILD_GAVE_UP)
    return build.id


def _their_passage(ctx: ToolContext, given: str | None) -> str | None:
    """``given`` as it stands in one of the person's own messages of this turn, copied by the
    code (``tools.notes.exact_text_in``); ``None`` when there is none, it is too long, or it is no
    passage of a message of theirs: words of a note someone left them, of a request that waits or
    of a tool are in none of their messages, and nobody's are in a turn nobody wrote in. What
    else the turn holds changes nothing to words that are theirs (the owner, 2026-11-27). None
    are kept by what he does as his own (``acts_alone``, or a call judged as his: an idea of his
    is not asked), whatever the context still holds."""
    if given is None or len(given) > MAX_NOTE_LENGTH:
        return None
    if acts_alone(ctx) or (ctx.judged_as is not None and ctx.judged_as.subject == "domo"):
        return None
    return exact_text_in(ctx.said_messages, given)


def _result(plan: Plan, zone: ZoneInfo) -> JsonObject:
    result: JsonObject = {
        "id": str(plan.id),
        "when": as_local(plan.due_at, zone),
        "repeat": plan.repeat,
        "on_return": plan.on_return,
    }
    if plan.after_build_id is not None:
        result["after_build"] = str(plan.after_build_id)
    return result


class _Set:
    """The arguments of a ``plan_set`` call, checked."""

    _ALLOWED: Final = frozenset(
        {"why", "when", "repeat", "on_return", "their_words", "after_build"}
    )

    def __init__(self, arguments: JsonObject) -> None:
        _only(arguments, frozenset({"why"}), self._ALLOWED)
        self.why = _why(arguments["why"])
        when, repeat = arguments.get("when"), arguments.get("repeat")
        self.when = None if when is None else _when(when)
        self.repeat = None if repeat is None else _repeat(repeat)
        on_return = arguments.get("on_return")
        self.on_return = False if on_return is None else _flag(on_return, "on_return")
        after_build = arguments.get("after_build")
        self.after_build = None if after_build is None else _build_id(after_build)
        if (
            self.when is None
            and self.repeat is None
            and not self.on_return
            and self.after_build is None
        ):
            raise InvalidArguments("invalid", _NO_MOMENT)
        words = arguments.get("their_words")
        if words is not None and not isinstance(words, str):
            raise InvalidArguments("invalid", "their_words is a text")
        self.their_words: str | None = words


_WHEN: Final[JsonObject] = {
    "type": "string",
    "description": "An ISO 8601 date and time (2027-03-28T18:00): with its offset, or without "
    "one for the household's time.",
}
_REPEAT: Final[JsonObject] = {
    "type": "string",
    "description": "An iCalendar RRULE, computed on the household's clock: "
    "FREQ=DAILY;BYHOUR=8;BYMINUTE=0 is every day at 08:00, FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;"
    "BYMINUTE=0 every Monday at 09:00, FREQ=MONTHLY;BYMONTHDAY=1 the 1st of each month. COUNT "
    "is how many times it is still due, UNTIL the last day. What it does not say (the hour, the "
    "weekday) comes from its first time.",
    "minLength": 1,
    "maxLength": MAX_REPEAT,
}


_AFTER_BUILD: Final[JsonObject] = {
    "type": "string",
    "description": "The id of the build of a tool (tool_build or tool_change gives it): the plan "
    "is due when that build ends.",
}


class PlanSet:
    """Set a plan for the person of the turn."""

    definition = ToolDefinition(
        name="plan_set",
        description=(
            "Plan something for later, for the person you are talking with: remind them, ask "
            "them how something went, do something they asked for at a given time, come back to "
            "a subject, check something later. Set it when they ask, or yourself when you judge "
            "it useful. Give a time (when), a rule that repeats (repeat), their return "
            "(on_return: when they come back), or a time and their return (whichever comes "
            "first). A request that waits for a tool you build waits for its build (after_build: "
            "the id tool_build returned): it is due when the build ends, ready or abandoned, or "
            "at its time if that comes first, and you are told which. In why, write for yourself "
            "what you will do and why, in a sentence or two. "
            "If it comes from their message, give in their_words the passage it comes from, "
            "copied word for word. Never put a password, a key or a code in it. Nobody is shown "
            "that you plan it: say so in your words if it helps. Your plans for the person are "
            "in your context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "why": {
                    "type": "string",
                    "description": "What you will do then, and why, for yourself.",
                    "minLength": 1,
                    "maxLength": MAX_WHY,
                },
                "when": _WHEN,
                "repeat": _REPEAT,
                "on_return": {
                    "type": "boolean",
                    "description": "Whether it is due when the person comes back.",
                },
                "after_build": _AFTER_BUILD,
                "their_words": {
                    "type": "string",
                    "description": "The passage of the person's own message it comes from, "
                    "copied character for character.",
                    "minLength": 1,
                    "maxLength": MAX_NOTE_LENGTH,
                },
            },
            "required": ["why"],
            "additionalProperties": False,
        },
    )
    action_type = "plan.set"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _Set(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Set(arguments)
        # Their exact words are a passage of one of their own messages of this turn, copied by
        # the code: a note someone left them, in the same turn, takes nothing from what they asked
        # themselves (the owner, 2026-11-27), and gives nothing either (its words are in no
        # message of theirs).
        words = _their_passage(ctx, given.their_words)
        if words is not None and refuses_secret_shapes(words):
            raise InvalidArguments("invalid", SECRET_REFUSED)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            zone = await household_zone(session)
            due_at = None if given.when is None else _in_zone(given.when, zone, now)
            after_build = (
                None
                if given.after_build is None
                else await _build_to_wait_for(session, given.after_build)
            )
            try:
                plan = await make(
                    session,
                    person_id=ctx.person.id,
                    why=given.why,
                    now=now,
                    zone=zone,
                    due_at=due_at,
                    repeat=given.repeat,
                    on_return=given.on_return,
                    their_words=words,
                    # The turn answers no message of theirs, and it comes from no words of
                    # theirs: his own (one he sets in a turn a plan woke him for, to put something
                    # off, too; on his own it is the conversation's person's, as the keys are, but
                    # his). Never where someone spoke or words came from outside (Task 27): in a
                    # turn they wrote in, it follows who began the turn, so it is theirs.
                    made_by_domo=words is None and _his_alone(ctx),
                    after_build_id=after_build,
                )
            except SecretRefused:
                raise InvalidArguments("invalid", SECRET_REFUSED) from None
            except ValueError:
                raise InvalidArguments("invalid", _NO_TIME_TO_COME) from None
            result = _result(plan, zone)
            if given.their_words is not None and words is None:
                # Not a passage of a message of theirs (words of a note, of a tool, or a turn
                # nobody wrote in): Domo is told they were not kept.
                result["their_words"] = NOT_KEPT
            # Last, just before the commit. Never why, nor their words.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="plan.made",
                details={"plan": str(plan.id)},
            )
            await session.commit()
        return ToolOutcome(status="done", model_result=result, card=_CARD)


class _Change:
    """The arguments of a ``plan_change`` call, checked."""

    _CHANGES: Final = frozenset({"why", "when", "repeat", "on_return", "after_build"})

    def __init__(self, arguments: JsonObject) -> None:
        _only(arguments, frozenset({"id"}), self._CHANGES | {"id", "their_words"})
        if all(arguments.get(name) is None for name in self._CHANGES):
            raise InvalidArguments(
                "invalid", "say what changes: why, when, repeat, on_return or after_build"
            )
        self.id = _plan_id(arguments["id"])
        why, when = arguments.get("why"), arguments.get("when")
        self.why = None if why is None else _why(why)
        self.when = None if when is None else _when(when)
        repeat = arguments.get("repeat")
        # An empty text stops it repeating; nothing leaves it as it is.
        self.repeat: str | None | EllipsisType = (
            ...
            if repeat is None
            else None
            if isinstance(repeat, str) and not repeat.strip()
            else _repeat(repeat)
        )
        on_return = arguments.get("on_return")
        self.on_return = None if on_return is None else _flag(on_return, "on_return")
        after_build = arguments.get("after_build")
        # An empty text: it no longer waits for a build; nothing leaves it as it is.
        self.after_build: uuid.UUID | None | EllipsisType = (
            ...
            if after_build is None
            else None
            if isinstance(after_build, str) and not after_build.strip()
            else _build_id(after_build)
        )
        words = arguments.get("their_words")
        if words is not None and not isinstance(words, str):
            raise InvalidArguments("invalid", "their_words is a text")
        # Read with a new why only (``PlanChange.execute``): without one it is not kept.
        self.their_words: str | None = words


class PlanChange:
    """Change a plan of the person of the turn."""

    definition = ToolDefinition(
        name="plan_change",
        description=(
            "Change one of your plans for the person you are talking with, by its id: why, when "
            "(a new time), repeat (a new rule, or an empty text to stop it repeating), on_return "
            "(whether it is due when they come back), after_build (the build of a tool it waits "
            "for, or an empty text to stop waiting for one). Put a plan off by giving it a new "
            "time. When a new why comes from their message, give in their_words the passage it "
            "comes from, copied word for word: it replaces the words the plan kept (without a "
            "new why it is not kept)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The id of the plan."},
                "why": {
                    "type": "string",
                    "description": "What you will do then, and why.",
                    "minLength": 1,
                    "maxLength": MAX_WHY,
                },
                "when": _WHEN,
                "repeat": {**_REPEAT, "minLength": 0},
                "on_return": {
                    "type": "boolean",
                    "description": "Whether it is due when the person comes back.",
                },
                "after_build": {
                    **_AFTER_BUILD,
                    "description": "The id of the build of a tool it "
                    "waits for, or an empty text to stop waiting for one.",
                },
                "their_words": {
                    "type": "string",
                    "description": "With a new why: the passage of the person's own message it "
                    "comes from, copied character for character.",
                    "minLength": 1,
                    "maxLength": MAX_NOTE_LENGTH,
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    action_type = "plan.change"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Change(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"plan:{given.id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Change(arguments)
        # The passage of their own message a new why comes from, as for a plan that is set;
        # without a new why a passage is not read (nothing is refused: the result says so).
        words = None if given.why is None else _their_passage(ctx, given.their_words)
        if words is not None and refuses_secret_shapes(words):
            raise InvalidArguments("invalid", SECRET_REFUSED)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            plan = await waiting_plan_of(session, ctx.person.id, given.id)
            if plan is None:
                raise InvalidArguments("not_found", _NOT_FOUND)
            zone = await household_zone(session)
            due_at = None if given.when is None else _in_zone(given.when, zone, now)
            after_build = (
                await _build_to_wait_for(session, given.after_build)
                if isinstance(given.after_build, uuid.UUID)
                else given.after_build
            )
            try:
                await change(
                    session,
                    plan,
                    now=now,
                    zone=zone,
                    why=given.why,
                    due_at=due_at,
                    repeat=given.repeat,
                    on_return=given.on_return,
                    after_build_id=after_build,
                    # A new why that comes with a passage of their own message keeps it, in
                    # place of the old words, whatever else the turn holds (the owner,
                    # 2026-11-27). Without one, in a turn that is not their own answer (a plan's
                    # turn, a wake, or one with text that is not theirs), it no longer comes from
                    # their words alone: the words it kept go, and it will not count as asked
                    # (final review I4). A passage given with it that is not theirs empties
                    # them anywhere: he said himself that the words changed, and the result
                    # ("not_kept") must be true of the plan. The change itself is made.
                    their_words=words,
                    forget_their_words=given.why is not None
                    and words is None
                    and (given.their_words is not None or not ctx.answers_them),
                    # A new why where someone spoke is no longer his alone (Task 27), nor is
                    # a plan that now keeps a person's words.
                    no_longer_his=given.why is not None
                    and (words is not None or not _his_alone(ctx)),
                )
            except SecretRefused:
                raise InvalidArguments("invalid", SECRET_REFUSED) from None
            except ValueError:
                raise InvalidArguments(
                    "invalid", f"{_NO_TIME_TO_COME}, or the plan would be left with none"
                ) from None
            result = _result(plan, zone)
            if given.their_words is not None and words is None:
                result["their_words"] = NOT_KEPT
            # Last, just before the commit. Never why, nor their words.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="plan.changed",
                details={"plan": str(given.id)},
            )
            await session.commit()
        return ToolOutcome(status="done", model_result=result, card=_CARD)


def _cancel_id(arguments: JsonObject) -> uuid.UUID:
    _only(arguments, frozenset({"id"}), frozenset({"id"}))
    return _plan_id(arguments["id"])


class PlanCancel:
    """Cancel a plan of the person of the turn."""

    definition = ToolDefinition(
        name="plan_cancel",
        description=(
            "Cancel one of your plans for the person you are talking with, by its id: when they "
            "ask, or when it is no longer of use."
        ),
        parameters={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The id of the plan."}},
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    action_type = "plan.cancel"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        plan_id = _cancel_id(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"plan:{plan_id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        plan_id = _cancel_id(arguments)
        async with ctx.session_factory() as session:
            plan = await waiting_plan_of(session, ctx.person.id, plan_id)
            if plan is None:
                raise InvalidArguments("not_found", _NOT_FOUND)
            await cancel(session, plan, ctx.clock())
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="plan.cancelled",
                details={"plan": str(plan_id)},
            )
            await session.commit()
        return ToolOutcome(status="done", model_result={"id": str(plan_id)}, card=_CARD)


# The forms of a time and of a repetition a plan takes, for the tools that give a time to
# something else (``tools.made_tools``: ``tool_launch_when``).
parse_when = _when
in_zone = _in_zone
parse_repeat = _repeat
WHEN_SCHEMA: Final = _WHEN
REPEAT_SCHEMA: Final = _REPEAT
