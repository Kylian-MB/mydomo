"""The tools with which Domo manages the person's pending requests (decision D2).

* ``confirmation_remind``: show a request again, when the person has not answered it (its card
  comes back, buttons included, under the new reply, which their next words may answer); or a
  request of someone else that waits for the person's yes (spec 5.1: an adult's for a child's
  request, the owner's when a right says so), which their next words may then answer too; on a
  request of the person that waits for someone else's yes (spec 5.2), it nudges its approver: the
  request comes first in their zone "To approve", and their Domo is told;
* ``confirmation_expire``: drop a request that has waited too long or no longer makes sense (one
  that waits for someone else's yes too);
* ``confirmation_require_explicit``: ask for an explicit yes (a firm yes in the person's own
  words, read by its meaning, or the button where there is one) for a request Domo himself
  judges needs one, and say why on its card; a request that asks as much or more already stays
  as it is (the result says so: ``"already"``);
* ``ask_before_acting``: ask more than the judge would (a yes, an explicit yes or a proof) for the
  next action Domo calls in the same answer; never less, and never what the judge refused;
* ``act_on_my_own``: say that the next action Domo calls in the same answer is his own idea,
  judged with his own rights and what he knows (``conversation.execution.handle_tool_call``);
* ``asking_agreed``: after the person answered something that could not be undone, keep what
  they agreed about being asked again the next times (``memory.told.agreed_asking``), for them
  alone and only in a turn that answers them; the judge weighs it, no yes is imposed by code;
* ``confirmation_amend``: apply the change the person asked for when they answered a request
  with a yes and a change; only then, in that turn (``ToolContext.change_grant``). An approver's
  change of someone else's request comes with a note of what they said, for the one who asked,
  who is woken (``ToolContext.wake``), as after a button. A change of the person's own request
  that waits for someone else's yes needs no answer of theirs (spec 5.2): nothing runs, it makes
  a new request for the same approver. A change of a request of Domo's own, by him on his own or
  by a person who answered it "yes, but", stays his and follows the verdict (it runs, or waits for
  a yes again). A change of rights is changed in words: the rights writer
  drafts it again, and the new draft is judged like any other change (applied at once, or a new
  request), or the writer asks the owner a question (spec 5.3).

They act on the person's own requests that wait in this conversation, and on nothing else (but
the two above, on a request that waits for the person's yes): an id that names anything else is
``not_found``. In a turn Domo acts in on his own, ``confirmation_expire`` and
``confirmation_amend`` act on his own requests that wait in this conversation, and on the
person's only by a right the owner gives him (spec 12.5, ruling R30:
``policy.catalog.ON_THEIR_REQUESTS``); showing a request again (``confirmation_remind``) and
asking more of it (``confirmation_require_explicit``) are his own on his own too, and the owner
takes them back by a rule (``policy.catalog.NOT_ON_HIS_OWN`` holds only the person's own).
None of them approves or refuses a request: that only comes from the person (a button, their
words read by the code or the isolated classifier). The work is done by
``confirmations.service``; these tools check their arguments, describe the call for the barrier
(``own_requests``, on ``confirmation:<id>``) and say what came of it. Their card is the request's
own card, as it is now (``ToolOutcome.confirmation_id``).
"""

import uuid
from typing import Final, cast

from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.service import (
    MAX_APPROVER_NOTE_LENGTH,
    MAX_EXPLICIT_REASON_LENGTH,
    AmendOutcome,
    amend_confirmation,
    approver_note_from,
    asker_on_his_own,
    expire_by_domo,
    explicit_reason_from,
    remind,
    require_explicit,
)
from domo_core.confirmations.store import approves, ask_of, how_to_answer, how_to_approve
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.memory.told import agreed_asking
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import ASKS, Ask
from domo_core.policy.roles import Actor
from domo_core.tools.context import (
    MAX_IDEA_REASON_LENGTH,
    InvalidArguments,
    OwnIdea,
    ToolContext,
    ToolOutcome,
)
from domo_core.tools.notes import MAX_NOTE_LENGTH, exact_text_in

_REQUEST_ID: Final[JsonObject] = {
    "type": "string",
    "description": (
        "The request_id of one of the pending requests, or of the requests of others, listed in "
        "the context."
    ),
}

# The card of a request tool that did its job: the stream and the page show the request's own
# card instead (the action is stored with it), so this one is only read if the request is gone.
_DONE: Final[JsonObject] = {"key": "action.card.done", "params": {}}


def _request_id(arguments: JsonObject, expected: frozenset[str]) -> uuid.UUID:
    """The ``request_id`` of the arguments, which must hold exactly the ``expected`` names."""
    if frozenset(arguments) != expected:
        raise InvalidArguments("invalid", f"the arguments are {', '.join(sorted(expected))}")
    value = arguments["request_id"]
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "request_id is the id of a pending request, as a text")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidArguments(
            "invalid", "request_id is the id of a pending request, from the context"
        ) from None


def _on_request(action_type: str, request_id: uuid.UUID) -> DescribedAction:
    """What a request tool does, for the barrier: an action on the person's own request."""
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=f"confirmation:{request_id}",
        effect="own_requests",
        destination="household",
    )


class ConfirmationRemind:
    """Show a pending request to the person again."""

    _ARGUMENTS: Final = frozenset({"request_id"})

    definition = ToolDefinition(
        name="confirmation_remind",
        description=(
            "Show one of the person's pending requests again, or one of the requests of others "
            "that wait for their yes: it comes back with your reply, as it is now, and if it is "
            "the only request your reply shows, their next answer can confirm it. "
            "Use it when they have not answered and the moment is right, when they seem to "
            "answer a request that was not shown just before, or to show the one they mean when "
            "several wait. On one of their own requests that waits for an adult's or the owner's "
            "yes, it nudges that approver instead: the request comes first in their list, and "
            "their assistant is told; the person's own words never answer it."
        ),
        parameters={
            "type": "object",
            "properties": {"request_id": _REQUEST_ID},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.remind"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        return _on_request(self.action_type, _request_id(arguments, self._ARGUMENTS))

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        request_id = _request_id(arguments, self._ARGUMENTS)
        async with ctx.session_factory() as session:
            c = await remind(
                session,
                confirmation_id=request_id,
                requester_id=ctx.person.id,
                conversation_id=ctx.conversation_id,
                now=ctx.clock(),
                shown_in=ctx.domo_message_id,
                approver=ctx.person,
            )
            result: JsonObject = {"reminders": c.reminder_count}
            if c.requester_id == ctx.person.id and c.approver_kind != "self":
                # The person's own request that waits for someone else's yes: its approver is
                # nudged (spec 5.2).
                result["nudged"] = True
            await session.commit()
        return ToolOutcome("done", result, dict(_DONE), confirmation_id=request_id)


class ConfirmationExpire:
    """Drop a pending request: it can no longer be confirmed."""

    _ARGUMENTS: Final = frozenset({"request_id"})

    definition = ToolDefinition(
        name="confirmation_expire",
        description=(
            "Drop one of the person's pending requests that has waited too long or no longer "
            "makes sense, one that waits for someone else's yes too: it can no longer be "
            "confirmed, and nothing is done. Tell the person. In a turn of your own, a request "
            "you made on your own is yours to drop (among the requests of others, one asked by "
            "Domo: you); the person's, only if you were given that right."
        ),
        parameters={
            "type": "object",
            "properties": {"request_id": _REQUEST_ID},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.expire"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        return _on_request(self.action_type, _request_id(arguments, self._ARGUMENTS))

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        request_id = _request_id(arguments, self._ARGUMENTS)
        async with ctx.session_factory() as session:
            # On his own, his own requests, and the person's only by the owner's right (R30).
            requester_id = (
                await asker_on_his_own(session, ctx, request_id, self.action_type)
                if ctx.on_his_own
                else ctx.person.id
            )
            await expire_by_domo(
                session,
                confirmation_id=request_id,
                requester_id=requester_id,
                conversation_id=ctx.conversation_id,
                now=ctx.clock(),
            )
            await session.commit()
        return ToolOutcome("done", {}, dict(_DONE), confirmation_id=request_id)


class ConfirmationRequireExplicit:
    """Ask for an explicit yes to a pending request, and say why."""

    _ARGUMENTS: Final = frozenset({"request_id", "reason"})

    definition = ToolDefinition(
        name="confirmation_require_explicit",
        description=(
            'Ask for a firm yes (in their own words: "I confirm", "yes, I am sure") on one of the '
            "person's pending requests, only for a reason of your own that your judge cannot see, "
            "or that would make a sensible person doubt: it does not sound like them, something "
            "contradicts what you know, it looks like a mistake. That an action is serious or "
            "cannot be undone is no such reason: telling it once to someone who does not know is "
            "your judge's business, and someone who says they know is not asked again. It never "
            "asks less than what is asked already. The reason is shown on the request: write it "
            "for the person, in their language, in a few plain words."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request_id": _REQUEST_ID,
                "reason": {
                    "type": "string",
                    "description": "Why you ask for this check, for the person.",
                    "minLength": 1,
                    "maxLength": MAX_EXPLICIT_REASON_LENGTH,
                },
            },
            "required": ["request_id", "reason"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.require_explicit"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        request_id = _request_id(arguments, self._ARGUMENTS)
        explicit_reason_from(arguments["reason"])
        return _on_request(self.action_type, request_id)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        request_id = _request_id(arguments, self._ARGUMENTS)
        reason = explicit_reason_from(arguments["reason"])
        async with ctx.session_factory() as session:
            c, raised = await require_explicit(
                session,
                confirmation_id=request_id,
                requester_id=ctx.person.id,
                conversation_id=ctx.conversation_id,
                reason=reason,
                now=ctx.clock(),
                shown_in=ctx.domo_message_id,
            )
            already = c.ask
            await session.commit()
        # A request that asks as much or more already stays as it is: Domo asks more, never less.
        result: JsonObject = {} if raised else {"already": already}
        return ToolOutcome("done", result, dict(_DONE), confirmation_id=request_id)


class AskBeforeActing:
    """Ask more than the judge would for the next action called in this answer."""

    _ARGUMENTS: Final = frozenset({"ask", "reason"})

    definition = ToolDefinition(
        name="ask_before_acting",
        description=(
            "Ask more than the assistant's judge would for the next action you call in this "
            "answer: a "
            'yes, a firm yes (in their own words: "I confirm", "yes, I am sure"), or a proof that '
            "it is them. Only for a reason of your own that your judge cannot see, or that would "
            "make a sensible person doubt: it does not sound like them, something contradicts "
            "what you know, it looks like a mistake. That an action is serious or cannot be "
            "undone is no such reason: telling it once to someone who does not know is your "
            "judge's business, and someone who says they know is not asked again. Call it right "
            "before that action, with the reason, for the person. It never asks less, and it "
            "never lets you do what the judge refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ask": {"type": "string", "enum": list(ASKS)},
                "reason": {
                    "type": "string",
                    "description": "Why you ask for this, for the person.",
                    "minLength": 1,
                    "maxLength": MAX_EXPLICIT_REASON_LENGTH,
                },
            },
            "required": ["ask", "reason"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.ask_before"

    def _read(self, arguments: JsonObject) -> tuple[Ask, str]:
        if frozenset(arguments) != self._ARGUMENTS:
            raise InvalidArguments("invalid", "give exactly ask and reason")
        for known in ASKS:
            if arguments["ask"] == known:
                return known, explicit_reason_from(arguments["reason"])
        raise InvalidArguments("invalid", "ask is yes, explicit_yes or proof")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        self._read(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="own_requests",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        ctx.ask_more = self._read(arguments)
        return ToolOutcome(
            "done",
            {"applies_to": "the next action you call in this answer"},
            {"key": "action.card.done.confirmation.ask_before", "params": {}},
        )


class ActOnMyOwn:
    """Say that the next action called in this answer is Domo's own idea."""

    _ARGUMENTS: Final = frozenset({"reason"})

    definition = ToolDefinition(
        name="act_on_my_own",
        description=(
            "Say that the next action you call in this answer is your own idea: it is judged with "
            "your own rights and what you know, done when your judge finds nothing wrong, and "
            "kept as yours; tell the person afterwards if that is useful. For what nobody asked "
            "you, or what a person may not do themselves but you judge right to do (a guest says "
            "a friend comes tonight: you may invite them). Never to get around what a person may "
            "not do: what was refused to them on purpose stays refused. Call it right before "
            "that action, with your reason in one sentence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why you do it, in one sentence.",
                    "minLength": 1,
                    "maxLength": MAX_IDEA_REASON_LENGTH,
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.own_idea"

    def _read(self, arguments: JsonObject) -> str:
        if frozenset(arguments) != self._ARGUMENTS:
            raise InvalidArguments("invalid", "give exactly reason")
        reason = arguments["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidArguments("invalid", "reason is one sentence")
        if len(reason) > MAX_IDEA_REASON_LENGTH:
            raise InvalidArguments("too_long", f"reason is {MAX_IDEA_REASON_LENGTH} characters")
        return reason.strip()

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        self._read(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="own_requests",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        # Born of the person's words only when the turn is their request, with nothing from
        # outside (``ToolContext.they_asked``).
        born_of = ctx.person.id if ctx.they_asked else None
        ctx.own_idea = OwnIdea(self._read(arguments), born_of=born_of)
        return ToolOutcome(
            "done",
            {"applies_to": "the next action you call in this answer"},
            {"key": "action.card.done.confirmation.own_idea", "params": {}},
        )


class AskingAgreed:
    """Keep what the person agreed about being asked again before an action that cannot be
    undone, after their answer to one (``memory.told.agreed_asking``)."""

    _ARGUMENTS: Final = frozenset({"action_type", "keep_asking", "their_words"})
    _MAX_TYPE: Final = 100

    definition = ToolDefinition(
        name="asking_agreed",
        description=(
            "After the person answered something that could not be undone, see with them once "
            "whether to keep asking them the next times, and keep what they say: keep_asking "
            "false when they need not be asked again, true when they want to be. action_type is "
            "the type of that action, as the context gives it (could_not_be_undone). their_words "
            "is the passage of their message of now that says it, copied character for character. "
            "Only in a turn that answers them, for what you told them before such an action."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "minLength": 1, "maxLength": _MAX_TYPE},
                "keep_asking": {"type": "boolean"},
                "their_words": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_NOTE_LENGTH,
                    "description": "Their words of now that say it, copied exactly.",
                },
            },
            "required": ["action_type", "keep_asking", "their_words"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.agree_asking"

    def _read(self, arguments: JsonObject) -> tuple[str, bool, str]:
        if frozenset(arguments) != self._ARGUMENTS:
            raise InvalidArguments(
                "invalid", "give exactly action_type, keep_asking and their_words"
            )
        action_type, keep_asking = arguments["action_type"], arguments["keep_asking"]
        if not isinstance(action_type, str) or not 0 < len(action_type.strip()) <= self._MAX_TYPE:
            raise InvalidArguments("invalid", "action_type is the type of the action, a text")
        if not isinstance(keep_asking, bool):
            raise InvalidArguments("invalid", "keep_asking is true or false")
        their_words = arguments["their_words"]
        if not isinstance(their_words, str) or not their_words.strip():
            raise InvalidArguments("invalid", "their_words is a passage of their message")
        return action_type.strip(), keep_asking, their_words

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        self._read(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="own_requests",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        action_type, keep_asking, their_words = self._read(arguments)
        # Tied to their own words of now (ruling R68): a turn that answers them with nothing from
        # outside, and a passage of one of their messages of this turn, never across two.
        if ctx.on_his_own or not ctx.answers_them:
            raise InvalidArguments("not_possible", "only in a turn that answers the person")
        if (
            len(their_words) > MAX_NOTE_LENGTH
            or exact_text_in(ctx.said_messages, their_words) is None
        ):
            raise InvalidArguments(
                "not_possible", "their_words is not a passage of their message of now: nothing kept"
            )
        async with ctx.session_factory() as session:
            kept = await agreed_asking(
                session,
                person_id=ctx.person.id,
                action_type=action_type,
                keep_asking=keep_asking,
                now=ctx.clock(),
            )
            if not kept:
                raise InvalidArguments(
                    "not_found", "you told them nothing before an action of this type"
                )
            await session.commit()
        return ToolOutcome(
            "done",
            {"keep_asking": keep_asking},
            {"key": "action.card.done", "params": {}},
        )


# What the model is told when a change made a new request (R64: plainly, nothing has happened).
_ASKED_AGAIN: Final = (
    "nothing has happened yet: the changed action is a new request that waits for a yes"
)
# The same, when the yes it waits for is now someone else's than the one it waited for (Ruling
# R27): the person's own, the approver's who changed it, or, for a request of the person that
# waited for someone else's yes (spec 5.2), that approver's.
_APPROVER_CHANGED: Final = (
    "nothing has happened yet: the changed action is a new request, and its yes is now someone "
    "else's than before: tell the person whose it is (answer_with)"
)
# The same, when the rights writer asks the owner a question instead of drafting a change of
# rights again.
_WRITER_ASKS: Final = (
    "nothing has changed: the request still waits as it was. Ask the owner the question; with "
    "their answer, propose the rights again (and drop this request)"
)
# The card kept with the reply for each outcome that has no record of its own.
_OUTCOME_CARDS: Final[dict[str, JsonObject]] = {
    "asked_again": {"key": "action.card.waiting", "params": {}},
    "approver_changed": {"key": "action.card.waiting", "params": {}},
    "expired": {"key": "confirmation.card.expired", "params": {}},
    "already_decided": {"key": "confirmation.outcome.already_decided", "params": {}},
}


class ConfirmationAmend:
    """Apply a "yes, but change X" to a pending request, the person's own or someone else's that
    waits for their yes (spec 5.1).

    It runs the changed call to its record, with a time limit on each step
    (``confirmations.service.amend_confirmation``): no limit is put around the whole
    (``BoundsItsOwnSteps``). Once someone else's request is decided, its requester is woken
    (``ToolContext.wake``), as after a button."""

    bounds_its_own_steps: Final = True

    _ARGUMENTS: Final = frozenset({"request_id", "arguments", "ask_again"})
    # What an approver said with the change, for the one who asked (optional).
    _NOTE: Final = "note"

    definition = ToolDefinition(
        name="confirmation_amend",
        description=(
            "Apply the change the person asked for when they answered a pending request with a "
            "yes and a change (the request is marked answered_with_change): the same action, "
            "with some arguments changed. Give only the arguments that change, with their new "
            "values; the others stay as they were. The changed action is judged again with what "
            "changed: it runs, or becomes a new request; set ask_again when you are not sure "
            "you understood. The result says whether it ran, was not done and why, or became a "
            "new request that waits for a yes. For a change of rights, arguments "
            'is {"change": "<what they want different, in their words>"}: the writer drafts '
            "the rights again and the new draft is a new request (they read its examples "
            "before saying yes); if the writer asks a question instead, the result says it, and "
            'the request still waits. For a chain of actions, arguments is {"steps": '
            "[...]}, the whole chain as the person wants it now: change a step's arguments, "
            "remove, add or reorder steps, the steps already done first, as they were; it is "
            "judged again as a whole. Only when the person answers someone "
            "else's request (one of the requests of others that wait for their yes) with a change, "
            "give note: what they said and how firm they were, in a few plain words, for the one "
            "who asked (it reaches them as data; never a secret or a link). In someone else's "
            "request a note is the one who asked's: give a note step back as it is, to the same "
            "recipient, or take it away; never change its words or its recipient. On one of the "
            "person's own requests that waits for an adult's or the owner's yes, use it whenever "
            "they ask for a change (no answer of theirs is needed): nothing runs, it becomes a "
            "new request for the same approver. You were told its notes without their words: to "
            "keep a note's words, give its text back exactly as you were told of it (or, for a "
            "single note, leave text out). In a turn of your own, change a request you made on "
            "your own whenever you judge it (among the requests of others, one asked by the "
            "assistant: "
            "you): it is judged again as yours, and runs or waits for a yes as the verdict says. "
            "When a person answers one of yours with a change, apply it the same way: it stays "
            "yours, and their yes covers it unless it needs more. The person's, only if you were "
            "given that right."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request_id": _REQUEST_ID,
                "arguments": {
                    "type": "object",
                    "description": (
                        "The arguments of the action that change, with their new values; for a "
                        'chain, {"steps": [...]}, the whole chain; for a change of rights, '
                        '{"change": "..."}.'
                    ),
                },
                "ask_again": {
                    "type": "boolean",
                    "description": "Ask the person again before the changed action runs.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Only for someone else's request: what the person said with the change "
                        'and how firm they were ("3 days, not more: firm"); never a link.'
                    ),
                    "minLength": 1,
                    "maxLength": MAX_APPROVER_NOTE_LENGTH,
                },
            },
            "required": ["request_id", "arguments", "ask_again"],
            "additionalProperties": False,
        },
    )
    action_type = "confirmation.amend"

    def _read(self, arguments: JsonObject) -> tuple[uuid.UUID, JsonObject, bool, str | None]:
        expected = self._ARGUMENTS | {self._NOTE} if self._NOTE in arguments else self._ARGUMENTS
        request_id = _request_id(arguments, expected)
        changes: object = arguments["arguments"]
        ask_again: object = arguments["ask_again"]
        if not isinstance(changes, dict):
            raise InvalidArguments("invalid", "arguments is an object: the arguments that change")
        if not isinstance(ask_again, bool):
            raise InvalidArguments("invalid", "ask_again is true or false")
        note = approver_note_from(arguments[self._NOTE]) if self._NOTE in arguments else None
        return request_id, cast(JsonObject, changes), ask_again, note

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        request_id, _, _, _ = self._read(arguments)
        return _on_request(self.action_type, request_id)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        request_id, changes, ask_again, note = self._read(arguments)
        outcome, record, request = await amend_confirmation(
            session_factory=ctx.session_factory,
            confirmation_id=request_id,
            new_arguments=changes,
            ask_again=ask_again,
            ctx=ctx,
            now=ctx.clock(),
            note=note,
        )
        if request is not None and request.requester_id != ctx.person.id and ctx.wake is not None:
            # Someone else's request, decided by this person's words: its requester is woken, as
            # after a button (only for an outcome that decided something:
            # ``conversation.starts``).
            ctx.wake(request.id, outcome, record, ctx.person)
        shown = request.id if request is not None else request_id
        answer_with: str | None = None
        if outcome in ("asked_again", "approver_changed") and request is not None:
            # How the new request is answered (final review Important 1): as it asks (a plain yes
            # does not give an explicit yes, nor a proof).
            if request.requester_id != ctx.person.id and approves(
                request.approver_kind, ctx.person, request.requester_id
            ):
                # It still waits for this person's yes, as an approver (spec 5.1).
                answer_with = how_to_approve(ask_of(request))
            else:
                answer_with = how_to_answer(request)
        theirs = request is not None and request.requester_id != ctx.person.id
        asked: tuple[str | None, str | None] = (None, None)
        if outcome in ("asked_again", "approver_changed") and request is not None:
            # Who asked the new request's yes, and why (the judge's reason, or Domo's).
            asked = (request.asked_by, request.ask_why)
        return _amend_outcome(
            outcome, record, shown, answer_with, someone_elses=theirs, asked=asked
        )


def _amend_outcome(
    outcome: AmendOutcome,
    record: ActionRecord | None,
    request_id: uuid.UUID,
    answer_with: str | None = None,
    *,
    someone_elses: bool = False,
    asked: tuple[str | None, str | None] = (None, None),
) -> ToolOutcome:
    """What the model and the people are told of a change: the outcome; what the changed call
    gave back when it ran, or why it was refused (for a chain, with the step that refused it; for
    the judge's refusal, its reason and what Domo is to do); the new request when it asks again,
    how it is answered (``answer_with``), who asked its yes and why (``asked``); the rights
    writer's question (``writer_asks``), whose card names no request: the one that still waits is
    not presented again. For a request of someone else (``someone_elses``: an approver changed
    it, spec 5.1), the model of the approver is never told what the changed call gave back: it is
    the one who asked's (what the steps of a chain read, a note they left included: D4), told to
    their Domo when he is woken."""
    result: JsonObject = {"outcome": outcome}
    if outcome == "writer_asks" and record is not None:
        result["question"] = record.model_result.get("question")
        result["detail"] = _WRITER_ASKS
        return ToolOutcome("done", result, dict(record.card))
    card: JsonObject = dict(_OUTCOME_CARDS.get(outcome, _DONE))
    card_once: JsonObject | None = None
    if record is not None:
        card, card_once = dict(record.card), record.card_once
        if outcome == "refused":
            reason = record.model_result.get("reason")
            result["reason"] = reason
            # The judge's refusal says why, and what Domo is to do (spec 4.6).
            for told in ("step", "why", "detail") if reason == "judge" else ("step",):
                value = record.model_result.get(told)
                if value is not None:
                    result[told] = value
        elif not someone_elses:
            result["result"] = dict(record.model_result)
    if outcome in ("asked_again", "approver_changed"):
        result["request_id"] = str(request_id)
        result["detail"] = _ASKED_AGAIN if outcome == "asked_again" else _APPROVER_CHANGED
        if answer_with is not None:
            result["answer_with"] = answer_with
        asked_by, why = asked
        if asked_by is not None:
            result["asked_by"] = asked_by
        if why is not None:
            result["why"] = why
    return ToolOutcome("done", result, card, card_once, confirmation_id=request_id)
