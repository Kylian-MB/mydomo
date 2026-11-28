"""The assistant makes two records of one person one, and splits them again: ``records_merge`` and
``records_split`` (``people.merge``).

A merge may move an access (the absorbed record's, or a directive of the owner's onto the kept
one) and may close an account: it is shown and judged. When each record has an account (the
owner's decision of 2026-11-26), the kept record's account stays and the other is closed: hard to
undo, so the assistant must be sure, which is his judgement and his judge's, never a code gate;
the owner is told whenever it is not his own request (the act is ``owed_to_owner``, a turn for
him is due). Splitting it again only puts back what moved, and is silent. Once either is
committed, the channels are told (``people.hooks``: the references of a channel that moved).
Whose act it is follows ``people.whose``: a call carries ``reason`` exactly when it is the
assistant's own. When he splits a merge as his own, it is a mistake of his he corrects: the fact
is noted (``learning.undone.note_his_mistake``); a person who splits it in their own turn is found
by ``learning.undone.undoes_his``, with nothing here. What the tools give back is data for the
assistant: he writes every text himself.
"""

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.learning.undone import note_his_mistake
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.people import hooks, merge, records, whose
from domo_core.people.acts import tell_owner
from domo_core.people.models import PersonAct, RecordMerge
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.people_tools import record_named

# What a call leaves under the reply.
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_MERGE: Final = "people.merge"


def _only(arguments: JsonObject, allowed: frozenset[str], needed: frozenset[str]) -> None:
    keys = frozenset(arguments)
    if not needed <= keys or not keys <= allowed:
        raise InvalidArguments(
            "invalid",
            f"the arguments are {', '.join(sorted(needed))} and, if needed, "
            f"{', '.join(sorted(allowed - needed))}",
        )


def _name(arguments: JsonObject, argument: str) -> str:
    value = arguments.get(argument)
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", f"{argument} is a name, or person:<id>")
    return value


async def _has_account(session: AsyncSession, person: Person) -> bool:
    found = await session.scalar(select(Account.id).where(Account.person_id == person.id))
    return found is not None


async def _the_two(
    session: AsyncSession, arguments: JsonObject, ctx: ToolContext
) -> tuple[Person, Person, bool]:
    """The record kept and the one absorbed, as the merge will make them (the owner's record is
    always the kept one, then the account's record), and whether an account is closed (each has
    one)."""
    # Someone refused or withdrawn, then forgotten, is found again by the person:<id> of the
    # directive kept for them (``people.forget``).
    kept = await record_named(session, _name(arguments, "keep"), ctx, refused_forgotten=True)
    absorbed = await record_named(session, _name(arguments, "absorb"), ctx, refused_forgotten=True)
    if kept.id == absorbed.id:
        raise InvalidArguments("invalid", "keep and absorb are two different records")
    kept_account, absorbed_account = (
        await _has_account(session, kept),
        await _has_account(session, absorbed),
    )
    closes = kept_account and absorbed_account
    if merge.kept_first(
        kept, absorbed, kept_account=kept_account, absorbed_account=absorbed_account
    ):
        return kept, absorbed, closes
    return absorbed, kept, closes


class RecordsMerge:
    """Make two records of one person one."""

    definition = ToolDefinition(
        name="records_merge",
        description=(
            "Make two records of one and the same person one, as soon as you understand they are "
            "('mamie and my mother are the same'). Their memories, nicknames, links, trust, "
            "access and what waits for them move to the record kept; the other is no longer a "
            "record. When in doubt, keep both. The household's owner's record, else the one that "
            "has an account, is always the one kept (the result says which). Someone the "
            "household's owner refused or withdrew who was forgotten is named by the person:<id> "
            "of the directive kept for them, and the record found again is the one kept. "
            "When both have one, the kept one's account stays and the other is closed: that is "
            "hard to undo, so do it only when you are sure; the household's owner is told, "
            "unless it was their own request. A merge can be split again for 90 days. When the "
            "two records were a mistake of yours, keep what you learn from it (memory_keep, a "
            "lesson with lesson_of). " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "keep": {
                    "type": "string",
                    "description": "The record kept: a name, or person:<id>.",
                },
                "absorb": {
                    "type": "string",
                    "description": "The record made one with it: a name, or person:<id>.",
                },
                "reason": whose.REASON_PROPERTY,
            },
            "required": ["keep", "absorb"],
            "additionalProperties": False,
        },
    )
    action_type = "people.merge"
    person_arguments: Final = ("keep", "absorb")
    names_among: Final = "records"

    @staticmethod
    def _read(arguments: JsonObject) -> None:
        _only(arguments, frozenset({"keep", "absorb", "reason"}), frozenset({"keep", "absorb"}))

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        self._read(arguments)
        async with ctx.session_factory() as session:
            kept, absorbed, _ = await _the_two(session, arguments, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{kept.id}",
            effect="permissions",
            destination="household",
            facts={"kept": f"person:{kept.id}", "absorbed": f"person:{absorbed.id}"},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        self._read(arguments)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            kept, absorbed, closes = await _the_two(session, arguments, ctx)
            # Closing an account is told to the owner, unless it is his own request.
            owed = closes and not whose.by_the_owner(arguments, ctx)
            was_a_child = kept.is_child
            try:
                trace = await merge.merge(
                    session,
                    kept=kept,
                    absorbed=absorbed,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                    owed_to_owner=owed,
                )
            except records.NotPossible as error:
                raise InvalidArguments("not_possible", error.code) from None
            person = await session.get(Person, trace.kept_id)
            made: JsonObject = {
                "kept": f"person:{trace.kept_id}",
                "absorbed": f"person:{trace.absorbed_id}",
                "person": None if person is None else person.display_name,
                "state": None if person is None else person.state,
                "can_be_split_until": trace.expires_at.isoformat(),
            }
            if person is not None and person.is_child and not was_a_child:
                # The protections of a child the other record had now hold for the record kept.
                made["child_mark_added"] = True
            if closes:
                made["account_kept"] = f"person:{trace.kept_id}"
                made["account_closed"] = f"person:{trace.absorbed_id}"
            ended = trace.moved.get("ended_directives", [])
            if ended:
                # The household's owner is never under a directive: the other record's ended, and
                # a split gives it back.
                made["directives_ended"] = len(ended)
            dropped = trace.moved.get("dropped_limits", [])
            if dropped:
                # A limit in another currency than the kept record's: the kept one holds, and the
                # household's owner is told of the other.
                made["spending_limits_dropped"] = len(dropped)
            # The single way the owner is told (``acts.tell_owner``): the merge's act, when it is
            # owed to him, keeps his news unless he saw it or agreed to it himself.
            act = await session.scalar(
                select(PersonAct)
                .where(
                    PersonAct.act == "merged",
                    PersonAct.person_id == trace.kept_id,
                    PersonAct.other_id == trace.absorbed_id,
                    PersonAct.at == now,
                    PersonAct.owed_to_owner.is_(True),
                )
                .limit(1)
            )
            owner = (
                None
                if act is None
                else await tell_owner(
                    session, act, knows=lambda one: whose.owner_knows(ctx, one), now=now
                )
            )
            told = hooks.merged(trace, now)
            await session.commit()
        await ctx.record_listeners.emit(told)
        if owner is not None and ctx.notify is not None:
            # The owner is owed the story: a turn for them is due.
            ctx.notify(owner)
        return ToolOutcome(status="done", model_result=made, card=_CARD)


async def _merge_of(session: AsyncSession, person: Person) -> RecordMerge:
    """The latest merge into ``person`` that can still be split."""
    found = await session.scalar(
        select(RecordMerge)
        .where(
            RecordMerge.kept_id == person.id,
            RecordMerge.split_at.is_(None),
            RecordMerge.moved != {},
        )
        .order_by(RecordMerge.merged_at.desc(), RecordMerge.id.desc())
        .limit(1)
    )
    if found is None:
        raise InvalidArguments(
            "not_found", "no merge into this record can be split: none, or its 90 days are past"
        )
    return found


class RecordsSplit:
    """Make two records again of two that were made one."""

    definition = ToolDefinition(
        name="records_split",
        description=(
            "Split again two records that were made one, when they are two people after all. "
            "Name the record that was kept. What moved goes back to the other record; what was "
            "added since stays. Possible for 90 days after the merge; an account closed by the "
            "merge is open again. When the merge was yours, keep afterwards what you learn from "
            "it (memory_keep, a lesson with lesson_of). " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "The record kept by the merge: a name, or person:<id>.",
                },
                "reason": whose.REASON_PROPERTY,
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "people.split"
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _read(arguments: JsonObject) -> str:
        _only(arguments, frozenset({"person", "reason"}), frozenset({"person"}))
        return _name(arguments, "person")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        name = self._read(arguments)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            trace = await _merge_of(session, person)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{trace.kept_id}",
            effect="self_profile",
            destination="household",
            facts={"kept": f"person:{trace.kept_id}", "absorbed": f"person:{trace.absorbed_id}"},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._read(arguments)
        by = whose.by(arguments, ctx)
        reason = whose.reason_of(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            trace = await _merge_of(session, person)
            try:
                done = await merge.split(
                    session,
                    trace,
                    by=by,
                    reason=reason,
                    turn_of=whose.turn_of(arguments, ctx),
                    now=now,
                )
            except merge.TraceGone:
                raise InvalidArguments(
                    "not_possible", "this merge can no longer be split: its 90 days are past"
                ) from None
            except records.NotPossible:
                raise InvalidArguments(
                    "not_possible",
                    "the kept record was merged into another since: split that merge first",
                ) from None
            if whose.is_his_own(arguments, ctx):
                await note_his_mistake(
                    session,
                    wrong_types=frozenset({_MERGE}),
                    wrong_type=_MERGE,
                    subject_key=f"person:{trace.kept_id}",
                    now=now,
                    reason=reason,
                )
            back = done.person
            split: JsonObject = {
                "kept": f"person:{trace.kept_id}",
                "back": f"person:{back.id}",
                "person": back.display_name,
                "state": back.state,
            }
            if done.stayed:
                # What could not go back, by table: it stays on the record kept.
                split["stayed_on_the_kept_record"] = dict(done.stayed)
            if done.without_access is not None:
                split["access_not_given_back"] = done.without_access
            if done.account_back:
                # The account signs in again; its sessions and devices the merge ended do not
                # come back.
                split["account_open_again"] = True
                split["sign_in_again"] = True
            if done.devices_stay_removed:
                split["devices_stay_removed"] = done.devices_stay_removed
            told = hooks.RecordEvent(
                kind="split",
                person_id=back.id,
                into_id=trace.kept_id,
                refs=done.refs_back,
                keep_minimum=False,
                at=now,
            )
            await session.commit()
        await ctx.record_listeners.emit(told)
        return ToolOutcome(status="done", model_result=split, card=_CARD)
