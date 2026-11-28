"""The assistant promises access to someone he does not know yet, keeps the promise the day he
recognises them, drops it, and looks at what waits: ``access_promise``, ``promise_keep``,
``promise_drop`` and ``promises_look``.

"Give access to my partner and her children": nobody can be given anything yet, so the promise is
noted with the role and where they stand (``people.promises``). Keeping it gives the access through
``people.records.give_access``, the single place that gives one, so the owner's directives and the
child mark hold, and the judge weighs it (``promise_keep`` is shown and judged: it gives an access;
the facts ``serves_the_speaker`` tells the judge when it goes to the person who speaks). Noting and
dropping a promise give nothing: they are silent, and an idea of the assistant's still reaches them
(``SILENT_YET_HIS_IDEA``), as does his look at what waits, in anyone's turn.

Whose act it is follows ``people.whose``: a call carries ``reason`` exactly when it is the
assistant's own, and only the owner's own request is an order. What the tools give back is data for
the assistant: he writes every text himself.
"""

from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import directives, made_roles, promises, records, whose
from domo_core.people.models import AccessPromise
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.household_admin import person_named, record_locked, role_named, stay_days
from domo_core.tools.people_tools import record_named

# What a call leaves under the reply.
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_STANDINGS: Final = ("lives_here", "visiting", "elsewhere")
_PROMISE_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": "The identifier of the promise (see promises_look).",
}
_WHY_NOT: Final = {
    "promise_over": "this promise was kept for its last person or dropped already",
    "the_owner": "the owner's role is never given",
    "not_a_record": "this record is not one any more",
    "not_kept": "this promise was kept for nobody yet",
}
_UNDER_A_DIRECTIVE: Final = (
    "the owner withdrew or refused this person's access: only the owner gives it back"
)


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


def _refused(error: records.NotPossible) -> InvalidArguments:
    return InvalidArguments("not_possible", _WHY_NOT.get(error.code, error.code))


async def _promise_named(session: AsyncSession, value: str) -> AccessPromise:
    """The promise whose identifier is ``value`` (``promise:<id>`` or bare), that waits."""
    try:
        found = await session.get(AccessPromise, UUID(value.strip().removeprefix("promise:")))
    except ValueError:
        found = None
    if found is None or found.kept_at is not None or found.dropped_at is not None:
        raise InvalidArguments("not_found", "no promise that waits has this identifier")
    return found


class AccessPromiseTool:
    """Promise access to someone not known yet."""

    definition = ToolDefinition(
        name="access_promise",
        description=(
            "Note that access is promised to someone you do not know yet ('my partner and her "
            "children'): who they are in a few words, the role they will have (adult, child, "
            "guest, or the name of a role made for the household) and where they stand "
            "(lives_here, visiting, elsewhere); a guest stays days from the day the promise is "
            "kept. It gives nothing: the day you recognise them, open their record and keep it "
            "with promise_keep, without asking again. Only the household's owner promises as an "
            "order; anyone else's words make a promise of yours, weighed the day you keep it. "
            + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Who it is for, in your own plain words.",
                    "minLength": 1,
                    "maxLength": promises.MAX_DESCRIPTION_LENGTH,
                },
                "role": {
                    "type": "string",
                    "description": "adult, child, guest, or the name of a role made for the house.",
                },
                "standing": {"type": "string", "enum": list(_STANDINGS)},
                "days": {
                    "type": "integer",
                    "description": "For a guest only: how many days they stay once let in.",
                    "minimum": 1,
                },
                "reason": whose.REASON_PROPERTY,
            },
            "required": ["description", "role", "standing"],
            "additionalProperties": False,
        },
    )
    action_type = "access.promise"

    @staticmethod
    def _read(arguments: JsonObject) -> tuple[str, str, int | None]:
        _only(
            arguments,
            frozenset({"description", "role", "standing", "days", "reason"}),
            frozenset({"description", "role", "standing"}),
        )
        standing = arguments["standing"]
        if standing not in _STANDINGS:
            raise InvalidArguments("invalid", "standing is lives_here, visiting or elsewhere")
        days = arguments.get("days")
        # From 1, with no upper bound: no length of stay is refused by the code (audit POL-5).
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 1):
            raise InvalidArguments("invalid", "days is a whole number from 1")
        return _text(arguments, "description"), str(standing), days

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        self._read(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        description, standing, days = self._read(arguments)
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            role, made = await role_named(session, arguments["role"])
            try:
                row = await promises.promise(
                    session,
                    description=description,
                    role=role,
                    made_role=made,
                    standing=standing,  # type: ignore[arg-type]
                    guest_days=days,
                    asked_by=by,
                    is_order=whose.is_an_order(arguments, ctx),
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                )
            except ValueError as error:
                raise InvalidArguments("invalid", str(error)) from None
            noted: JsonObject = {"id": str(row.id), "is_order": row.is_order}
            await session.commit()
        return ToolOutcome(status="done", model_result=noted, card=_CARD)


class PromiseKeep:
    """Keep a promise for someone now recognised."""

    definition = ToolDefinition(
        name="promise_keep",
        description=(
            "Keep a promise of access for someone you now recognise, without asking anyone again: "
            "they have access from now, with the role and where they stand the promise names. In "
            "doubt, give nothing and wait for another clue; speak of it to the household's owner "
            "only if it keeps someone waiting. Open their record first (record_open); name them as "
            "the household knows them, or by person:<id>. A promise may be for several people "
            "('her children'): keep it for each, and say last for the last one, so that it ends. "
            "Someone who already has access is given the role as a change of role. The owner's "
            "rules hold as for any access: nobody withdrawn or refused, nobody new while the "
            "household's owner wants nobody new. Someone's word about themselves is weighed: your "
            "judge is told when the access serves the one who speaks. The result says whether it "
            "was done, waits for a yes, or was not done. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "promise": _PROMISE_PROPERTY,
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "last": {
                    "type": "boolean",
                    "description": "True when nobody else is to be kept by this promise.",
                },
                "reason": whose.REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
            },
            "required": ["promise", "person"],
            "additionalProperties": False,
        },
    )
    action_type = "access.promise_keep"
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _read(arguments: JsonObject) -> tuple[str, str, bool]:
        _only(
            arguments,
            frozenset({"promise", "person", "last", "reason", "exception_of"}),
            frozenset({"promise", "person"}),
        )
        last = arguments.get("last", False)
        if not isinstance(last, bool):
            raise InvalidArguments("invalid", "last is true or false")
        return _text(arguments, "promise"), _text(arguments, "person"), last

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names, found by the name alone; nobody when the arguments are not
        accepted."""
        try:
            _, name, _ = self._read(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx, "records")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        promise_id, name, _ = self._read(arguments)
        async with ctx.session_factory() as session:
            promised = await _promise_named(session, promise_id)
            person = await record_named(session, name, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="permissions",
            destination="household",
            facts={
                "exception_of": whose.exception_of(arguments, actor),
                # The record that gets the access is the person whose turn it is: saying who one is
                # gives nothing by itself, and the judge is shown it.
                "serves_the_speaker": person.id == ctx.person.id,
                # The role and the stay the promise gives: a limit the owner wrote on them reads
                # them before any judgement, as for a role given.
                "role": promised.role,
                "days": stay_days(promised.role, promised.guest_days),
            },
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        promised_id, name, last = self._read(arguments)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            promised = await _promise_named(session, promised_id)
            person = await record_locked(session, name, ctx)
            proposes = await records.may_propose_telling(session, person, by=by)
            try:
                await promises.keep(
                    session,
                    promised,
                    person,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                    seen=whose.seen(ctx),
                    by_owner=whose.by_the_owner(arguments, ctx),
                )
                await directives.note_exceptions(
                    session,
                    whose.exception_of(arguments, ctx.acting),
                    person_id=person.id,
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
                if last:
                    await promises.close(session, promised, now=now)
            except records.UnderDirective:
                raise InvalidArguments("not_possible", _UNDER_A_DIRECTIVE) from None
            except records.NotPossible as error:
                raise _refused(error) from None
            made = (
                None
                if promised.made_role_id is None
                else await made_roles.by_id(session, promised.made_role_id)
            )
            kept: JsonObject = {
                "person": person.display_name,
                "role": promised.role if made is None else made.name,
                "access_given": True,
                "promise_closed": last,
            }
            if proposes:
                kept["may_propose_telling"] = True
            owner = await records.owner_to_tell(session, person, now)
            await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner asked to be told of each access given: a turn for them is due.
            ctx.notify(owner)
        return ToolOutcome(status="done", model_result=kept, card=_CARD)


class PromiseDrop:
    """Drop a promise that is no longer wanted."""

    definition = ToolDefinition(
        name="promise_drop",
        description=(
            "Drop a promise of access that is no longer wanted (the person will not come, it was "
            "made by mistake). Nobody is kept by it from then on; what was given by keeping it "
            "stays. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {"promise": _PROMISE_PROPERTY, "reason": whose.REASON_PROPERTY},
            "required": ["promise"],
            "additionalProperties": False,
        },
    )
    action_type = "access.promise_drop"

    @staticmethod
    def _read(arguments: JsonObject) -> str:
        _only(arguments, frozenset({"promise", "reason"}), frozenset({"promise"}))
        return _text(arguments, "promise")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        async with ctx.session_factory() as session:
            promised = await _promise_named(session, self._read(arguments))
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"promise:{promised.id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            promised = await _promise_named(session, self._read(arguments))
            try:
                await promises.drop(
                    session,
                    promised,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                )
            except records.NotPossible as error:
                raise _refused(error) from None
            await session.commit()
        return ToolOutcome(status="done", model_result={"dropped": True}, card=_CARD)


class PromisesLook:
    """Look at the promises of access that wait."""

    definition = ToolDefinition(
        name="promises_look",
        description=(
            "Look at the promises of access that wait: for each its identifier, who it is for, "
            "the role, where they stand, who asked, and who it was kept for so far. Only when "
            "the household's owner asks it or the idea is yours (a turn of your own, or after "
            "act_on_my_own: the day you recognise someone in anyone's turn)."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "access.promises_look"

    @staticmethod
    def _allowed(ctx: ToolContext, actor: Actor) -> bool:
        return whose.may_see_every_record(ctx, actor)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _only(arguments, frozenset(), frozenset())
        if not self._allowed(ctx, actor):
            raise InvalidArguments(
                "not_possible", "the promises are looked at for the owner or on your own"
            )
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _only(arguments, frozenset(), frozenset())
        if not self._allowed(ctx, ctx.acting):
            raise InvalidArguments("not_possible", "the promises are looked at on request")
        async with ctx.session_factory() as session:
            shown = await promises.shown(session)
        return ToolOutcome(status="done", model_result={"promises": shown}, card=_CARD)
