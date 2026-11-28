"""Whose act a call of a people tool is: the assistant's own, or a person's.

A people tool is silent and never shown: the code does not ask the person anything about it. What
it must know is who acts, since the trace of the act (``people.acts``) names who did it and why.
The rule lives in the arguments, which travel unchanged from ``describe`` to ``execute`` and with a
request that is stored and run again: **a call carries ``reason`` exactly when the act is the
assistant's own** (every act of his has its reason, socle 8). ``describe`` enforces it
(``check``); ``execute`` then reads who acts from the context (``ToolContext.acting``: who the
call was judged as), and the two agree.

When the idea is his in a person's turn, the reason is said twice: to ``act_on_my_own``, for the
judge and the action's ``idea_reason``, and to the tool, for the trace of the person.
"""

from typing import Final
from uuid import UUID

from domo_core.llm.types import JsonObject
from domo_core.policy.roles import Actor, actor_label
from domo_core.tools.context import InvalidArguments, ToolContext, acts_alone

__all__ = [
    "EXCEPTION_PROPERTY",
    "HIS_OWN_IDEA",
    "TELL_OWNER_IS_YOURS",
    "TELL_OWNER_PROPERTY",
    "MAX_REASON_LENGTH",
    "REASON_PROPERTY",
    "by",
    "by_the_owner",
    "check",
    "exception_of",
    "is_an_order",
    "is_his_own",
    "may_see_every_record",
    "reason_of",
    "seen",
    "tells_the_owner",
    "turn_of",
]

# The longest reason of an act (``person_acts.reason`` and ``persons.origin_reason`` say the same).
MAX_REASON_LENGTH: Final = 300
# The ``reason`` argument of a tool whose act may be the assistant's own.
REASON_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": (
        "Only when the idea is yours (after act_on_my_own): why you do it, in one sentence. "
        "Leave it out when the person asked for it."
    ),
    "minLength": 1,
    "maxLength": MAX_REASON_LENGTH,
}
# The closing sentence of every tool of ``policy.catalog.PEOPLE_GESTURES``, written once: a
# gesture on people nobody asked for is his own idea, marked first in a person's turn.
HIS_OWN_IDEA: Final = (
    "When nobody asked for it, the idea is yours: in someone's turn call act_on_my_own right "
    "before, and give reason."
)

# The ``exception_of`` argument of a tool that gives an access or opens a record: the owner's rule
# whose words the assistant judged make this call an exception (``directives.lets_through``).
EXCEPTION_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": (
        "Only when the idea is yours and one of the owner's rules on new people has words that "
        "make this an exception: the identifier of that rule, as the rules are shown to you "
        "(several: separated by commas)."
    ),
    "maxLength": 300,
}
_EXCEPTION_IS_YOURS: Final = (
    "exception_of is for your own acts only (after act_on_my_own, with reason): it names the "
    "owner's rule whose words you judged make this an exception"
)

# The ``tell_owner`` argument of a gesture of his own whose telling is his judgement (the owner's
# answer of 2026-11-27: told "if the assistant judges I would like to be"), and the sentence of
# its tool's description.
TELL_OWNER_PROPERTY: Final[JsonObject] = {
    "type": "boolean",
    "description": (
        "Only when the idea is yours: true when you judge the household's owner would want to "
        "know of it; he is then told. Leave it out otherwise."
    ),
}
TELL_OWNER_IS_YOURS: Final = (
    "When the idea is yours, whether the household's owner is told is your judgement of what he "
    "would want to know (tell_owner)."
)
_TELL_IS_YOURS: Final = "tell_owner is for your own acts only (after act_on_my_own, with reason)"

_NEEDS_A_REASON: Final = "an act of yours has its reason, in one sentence: give reason"
_NOT_YOUR_IDEA: Final = (
    "reason is for your own acts only: when the idea is yours, call act_on_my_own first, then this "
    "tool with the reason; when the person asked, leave reason out"
)


def reason_of(arguments: JsonObject) -> str | None:
    """The reason the call carries, cleaned; ``None`` when it carries none."""
    reason = arguments.get("reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def check(arguments: JsonObject, actor: Actor) -> None:
    """``InvalidArguments("invalid")`` unless the call carries a reason exactly when ``actor``,
    who the call is judged as, is the assistant: his own act has its reason (one sentence of at
    most ``MAX_REASON_LENGTH`` characters), and a person's has none (when the idea is his, he
    calls ``act_on_my_own`` first)."""
    if actor.subject != "domo":
        if "reason" in arguments:
            raise InvalidArguments("invalid", _NOT_YOUR_IDEA)
        return
    reason = arguments.get("reason")
    if not isinstance(reason, str) or reason_of(arguments) is None:
        raise InvalidArguments("invalid", _NEEDS_A_REASON)
    if len(reason.strip()) > MAX_REASON_LENGTH:
        raise InvalidArguments("too_long", f"the reason is {MAX_REASON_LENGTH} characters at most")


def exception_of(arguments: JsonObject, actor: Actor) -> str | None:
    """The owner's rules the call names as the exception the assistant judged (``exception_of``,
    the fact the barrier reads), ``None`` when it names none. ``InvalidArguments("invalid")`` for
    a value that is not a text of at most 300 characters, or given in a call that is not his own
    (``actor``, who the call is judged as)."""
    value = arguments.get("exception_of")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise InvalidArguments("invalid", "exception_of is the identifiers of the rules")
    if actor.subject != "domo":
        raise InvalidArguments("invalid", _EXCEPTION_IS_YOURS)
    return value


def tells_the_owner(arguments: JsonObject, actor: Actor) -> bool:
    """Whether the assistant judged that the owner would want to know of this act of his own
    (``tell_owner``). ``InvalidArguments("invalid")`` for a value that is not true or false, or
    given in a call that is not his own (``actor``, who the call is judged as)."""
    value = arguments.get("tell_owner")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InvalidArguments("invalid", "tell_owner is true or false")
    if actor.subject != "domo":
        raise InvalidArguments("invalid", _TELL_IS_YOURS)
    return value


def is_his_own(arguments: JsonObject, ctx: ToolContext) -> bool:
    """Whether the call that runs is the assistant's own act: it was judged as him
    (``ToolContext.acting``). ``InvalidArguments("invalid")`` when that disagrees with the reason
    the call carries, which ``check`` already prevents."""
    his = ctx.acting.subject == "domo"
    if his != ("reason" in arguments):
        raise InvalidArguments("invalid", _NEEDS_A_REASON if his else _NOT_YOUR_IDEA)
    return his


def by(arguments: JsonObject, ctx: ToolContext) -> str:
    """Who acts, as the trace writes it: ``domo``, or ``person:<id>``."""
    is_his_own(arguments, ctx)
    return actor_label(ctx.acting)


def is_an_order(arguments: JsonObject, ctx: ToolContext) -> bool:
    """Whether the act is the owner's own order: not the assistant's idea, in the owner's turn, in
    answer to their words."""
    return not is_his_own(arguments, ctx) and ctx.person.role == "owner" and _answers_the_owner(ctx)


def _answers_the_owner(ctx: ToolContext) -> bool:
    """Whether the turn answers the owner's own message (``reply_message_id``): words from
    outside in the same turn (a note told, someone's request waiting for his yes) do not make his
    request someone else's; the assistant judges what is his."""
    return ctx.person.role == "owner" and ctx.reply_message_id is not None


def by_the_owner(arguments: JsonObject, ctx: ToolContext) -> bool:
    """Whether the act is the owner's own: the call is not judged as the assistant's idea and the
    person it is judged as (``ToolContext.acting``) is the owner, whether it runs at once or after
    the owner's own yes on a stored request or a chain. What the owner takes away this way is a
    directive (spec 6.9), and only the owner's act lifts the mark of a child."""
    if is_his_own(arguments, ctx):
        return False
    acting = ctx.acting
    return acting.subject == "person" and acting.role == "owner"


def turn_of(arguments: JsonObject, ctx: ToolContext) -> UUID | None:
    """Whose turn the act is made in, ``None`` in a turn he acts in on his own."""
    return None if ctx.on_his_own else ctx.person.id


def seen(ctx: ToolContext) -> bool:
    """Whether the person of the turn sees the act happen: the turn answers a message of theirs
    or writes them a reply (``ToolContext.reply_message_id``, ``domo_message_id``), or runs on a
    yes of theirs (``approved_by``). Never in a review of what was said, which nobody reads."""
    return (
        ctx.reply_message_id is not None
        or ctx.domo_message_id is not None
        or ctx.approved_by == ctx.person.id
    )


def owner_knows(ctx: ToolContext, owner: UUID) -> bool:
    """Whether the owner knows of an act of this call without being told again: he sees it happen
    in his own turn (``seen``, not a turn the assistant acts in alone), or it runs on his own yes
    (``approved_by``)."""
    sees = owner == ctx.person.id and seen(ctx) and not acts_alone(ctx)
    return sees or ctx.approved_by == owner


def may_see_every_record(ctx: ToolContext, actor: Actor) -> bool:
    """Whether what is read in this call may show every record, and not only the people who have
    access: the turn is the assistant's own, the call was judged as his (``actor``: he marked it
    ``act_on_my_own``, in anyone's turn: to check for a duplicate before opening a record), or the
    owner asked for it. What it shows is data for the assistant, never for the person."""
    return ctx.on_his_own or actor.subject == "domo" or _answers_the_owner(ctx)
