"""The assistant opens, completes, looks at, erases and forgets the record of a person, and takes
back an access given by mistake: ``record_open``, ``record_complete``, ``record_look``,
``record_erase``, ``record_forget``, ``forget_me`` and ``access_mistake``.

A record is someone the assistant knows by name, with or without a login: opening one gives them
nothing. These tools are silent: nobody is asked, nothing is shown (``policy.catalog``); the code's
own guards hold on them (the owner's directives, the child mark, a record that was ever given an
access is never erased), and an idea of the assistant's still reaches them
(``SILENT_YET_HIS_IDEA``). No tool here checks the speaker's rights: a person's own request is the
rights' to refuse (the gestures are in no role), a call the assistant marked as his own is the
judge's to weigh.

Whose act it is follows ``people.whose``: a call carries ``reason`` exactly when it is the
assistant's own, and the reason is kept with the act (``people.acts``). What the tools give back
is data for the assistant: he writes every text himself.

Erasing a record he opened, or taking back an access he gave, as his own act is a mistake of his
he corrects: the fact is noted here (``learning.undone.note_his_mistake``); a person who does it in
their own turn is found by ``learning.undone.undoes_his``, with nothing in these tools.

Forgetting someone (``people.forget``, ``people.forget_me``) cannot be undone: it is shown and
judged as any act that cannot be undone (the yes-judge, and the rule that it is said once to
whoever does not know it). ``forget_me`` is the person of the turn's own request, everyone's, and
never waits for the owner's yes; ``record_forget`` is the assistant's gesture when someone of the
household relays the wish of someone else, which he judges.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final, cast, get_args
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person, Standing
from domo_core.accounts.sessions import is_active
from domo_core.audit.models import AuditEntry
from domo_core.household.service import resolve_person
from domo_core.learning.undone import note_his_mistake
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.memory.models import Memory
from domo_core.memory.service import free_text_about
from domo_core.people import directives, forget, hooks, made_roles, records, whose
from domo_core.people.acts import note_act, tell_owner, why
from domo_core.people.models import PersonAct, PersonLink
from domo_core.people.reach import known_records
from domo_core.policy.actions import DescribedAction
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor
from domo_core.policy.subjects import UNDOES
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome, acts_alone
from domo_core.tools.profile import display_name_value

# What a call leaves under the reply: a card nobody reads (``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}

# What is never said between people (spec 6.8), in the descriptions that show records.
BETWEEN_PEOPLE: Final = (
    "Between people: nothing one confided in private goes to another, nor the detail of others' "
    "access, nor the house's life to a guest; the household's owner may always know who has a "
    "record and which access."
)

STANDINGS: Final = ("lives_here", "visiting", "elsewhere")
# Why someone gets a record at all: what makes it worth one. A name that only came up in passing
# stays a memory.
GROUNDS: Final = ("comes", "lives_here", "will_come", "gets_access", "matters")

_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_REASON_PROPERTY: Final = whose.REASON_PROPERTY
# What each refusal of the records says to the assistant.
_WHY_NOT: Final = {
    "not_a_record": "this record is not one any more",
    "child_role": "the child role carries the mark: change their role to lift it",
    "not_by_owner": "the mark of a child is lifted by the owner, or by you on a date of birth",
    "no_birth_date": "no date of birth is known for them: ask, or let the owner lift the mark",
    "too_young": "their date of birth does not make them of age yet",
    "memory_taken": "one of these memories is about someone else, or does not exist",
    "has_been_here": "they have been given an access: a record that was ever in the house stays",
    "under_directive": "a rule of the owner's applies to this record: it stays",
    "no_access": "they have no access to take back",
    "the_owner": "the household's owner keeps their access",
    "owner_no_mark": "the household's owner never carries the mark of a child",
}
# What each refusal of forgetting says to the assistant.
_WHY_NOT_FORGOTTEN: Final = {
    **_WHY_NOT,
    "the_owner": "the household's owner is never forgotten",
}


def _refused(error: records.NotPossible) -> InvalidArguments:
    return InvalidArguments("not_possible", _WHY_NOT.get(error.code, error.code))


def _only(arguments: JsonObject, allowed: frozenset[str], needed: frozenset[str]) -> None:
    keys = frozenset(arguments)
    if not needed <= keys or not keys <= allowed:
        raise InvalidArguments(
            "invalid",
            f"the arguments are {', '.join(sorted(needed))} and, if needed, "
            f"{', '.join(sorted(allowed - needed))}",
        )


def _standing(value: object) -> Standing:
    for standing in get_args(Standing):
        if value == standing:
            return standing
    raise InvalidArguments("invalid", f"standing is one of {', '.join(STANDINGS)}")


def _boolean(value: object, argument: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArguments("invalid", f"{argument} is true or false")
    return value


def _day(value: object, argument: str) -> date:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise InvalidArguments("invalid", f"{argument} is a day, written YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidArguments("invalid", f"{argument} is a day, written YYYY-MM-DD") from None


def _uuids(value: object) -> list[UUID]:
    if not isinstance(value, list):
        raise InvalidArguments("invalid", "memories is a list of memory identifiers")
    try:
        return [UUID(str(item)) for item in cast("list[object]", value)]
    except ValueError:
        raise InvalidArguments("invalid", "memories is a list of memory identifiers") from None


def _name(value: object, argument: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", f"{argument} is a name")
    return value


async def record_named(
    session: AsyncSession, name: str, ctx: ToolContext, *, refused_forgotten: bool = False
) -> Person:
    """The one record ``name`` means, whatever its state: ``person:<id>``, else what the name
    means among every record the assistant may name (``household.service.resolve_person``: the
    whole name, a nickname, a first name, a link of the speaker: "my mother"). Two records may have
    the same name: ``InvalidArguments("ambiguous")`` says how to tell them apart and never picks
    one. ``InvalidArguments("not_found")`` when nobody. ``refused_forgotten``: ``person:<id>`` may
    also name a record forgotten whose withdrawal or refusal is in force (what is kept to know
    them again: ``records_merge``)."""
    if name.startswith("person:"):
        try:
            found = await session.scalar(
                select(Person).where(
                    Person.id == UUID(name.removeprefix("person:")), known_records()
                )
            )
            if found is None and refused_forgotten:
                found = await _refused_forgotten(session, UUID(name.removeprefix("person:")), ctx)
        except ValueError:
            found = None
        if found is None:
            raise InvalidArguments("not_found", "no record has this identifier")
        return found
    detailed = whose.may_see_every_record(ctx, ctx.acting)
    return await resolve_person(
        session,
        name,
        now=ctx.clock(),
        speaker=ctx.person.id,
        among="records",
        with_ids=detailed,
        detailed=detailed,
    )


async def _refused_forgotten(
    session: AsyncSession, person_id: UUID, ctx: ToolContext
) -> Person | None:
    """The forgotten record ``person_id``, when a withdrawal or a refusal is in force for it."""
    found = await session.get(Person, person_id)
    if found is None or found.forgotten_at is None or found.merged_into_id is not None:
        return None
    if await directives.under_directive(session, person_id, ctx.clock()) is None and not (
        await forget.owners_deny_kept(session, person_id)
    ):
        return None
    return found


class RecordOpen:
    """Open the record of someone, with no access and no account."""

    definition = ToolDefinition(
        name="record_open",
        description=(
            "Open the record of someone you now know by name, when they come, live here, will "
            "come, are about to be given an access, or count for the household and come up often "
            "in conversation. It gives them nothing: no access, no account. A name that only "
            "came up in passing stays a memory; if it comes up again, open the record then and "
            "attach those memories (record_complete). Look first: complete a record that exists, "
            "never open a second. The ground is your own judgement of what you heard, read or "
            "were told, never a name alone; weigh each word with the trust you have in its "
            "speaker (a child's 'my friend may do anything' is at most a record). Mark a child "
            "as one (is_child), even one passing by: they keep a child's protections. Two "
            "records may have the same name. The result gives the record's identifier. "
            + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name the household calls them by.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "standing": {
                    "type": "string",
                    "enum": list(STANDINGS),
                    "description": (
                        "Where they stand towards the house: lives_here, visiting (comes, came "
                        "or will come) or elsewhere (counts for the house without coming)."
                    ),
                },
                "is_child": {
                    "type": "boolean",
                    "description": "True for a child. Leave it out for anyone else.",
                },
                "ground": {
                    "type": "string",
                    "enum": list(GROUNDS),
                    "description": "Why they deserve a record: what makes them more than a name.",
                },
                "reason": _REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
            },
            "required": ["name", "standing", "ground"],
            "additionalProperties": False,
        },
    )
    action_type = "people.open"

    @staticmethod
    def _read(arguments: JsonObject) -> tuple[str, Standing, bool]:
        _only(
            arguments,
            frozenset({"name", "standing", "is_child", "ground", "reason", "exception_of"}),
            frozenset({"name", "standing", "ground"}),
        )
        if arguments["ground"] not in GROUNDS:
            raise InvalidArguments(
                "invalid",
                f"ground is one of {', '.join(GROUNDS)}: a name that only came up stays a memory",
            )
        is_child = _boolean(arguments["is_child"], "is_child") if "is_child" in arguments else False
        return (
            display_name_value(arguments["name"]),
            _standing(arguments["standing"]),
            is_child,
        )

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
            facts={"exception_of": whose.exception_of(arguments, actor)},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, standing, is_child = self._read(arguments)
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            person = await records.open_record(
                session,
                name=name,
                standing=standing,
                is_child=is_child,
                by=by,
                reason=whose.reason_of(arguments),
                turn_of=whose.turn_of(arguments, ctx),
                now=ctx.clock(),
            )
            await directives.note_exceptions(
                session,
                whose.exception_of(arguments, ctx.acting),
                person_id=person.id,
                turn_of=whose.turn_of(arguments, ctx),
                now=ctx.clock(),
            )
            # What was said of them before they had a record, as free text: candidates only, the
            # assistant decides which speak of this person (two people may be called Tom).
            free = await free_text_about(
                session, person.display_name, seen_by=None if acts_alone(ctx) else ctx.person.id
            )
            opened: JsonObject = {
                "id": str(person.id),
                "person": person.display_name,
                "state": person.state,
                "free_memories": [{"id": str(m.id), "subject": m.subject} for m in free],
            }
            await session.commit()
        return ToolOutcome(status="done", model_result=opened, card=_CARD)


@dataclass(frozen=True)
class _Changes:
    """What ``record_complete`` says of a record: each ``None`` (or nothing) is left as it is."""

    name: str | None = None
    standing: Standing | None = None
    is_child: bool | None = None
    born_on: date | None = None
    access_returns: bool | None = None
    stay_until: datetime | None = None
    memories: tuple[UUID, ...] = ()


def _stay_until(value: object, now: datetime) -> datetime:
    """The end of a stay a call gives: the end of the day it names (``YYYY-MM-DD``), or ``now``
    for ``"now"`` (the assistant judges that they left)."""
    if value == "now":
        return now
    return datetime.combine(_day(value, "stay_until"), time(23, 59, 59), tzinfo=UTC)


# What the assistant is told when stay_until names someone whose stay is not a visit's.
_NOT_A_STAY: Final = (
    "the end of a stay is only for a guest or a visitor without an account; their stay was left "
    "as it is (to end a member's access, that is household_remove)"
)


class RecordComplete:
    """Say more of a record."""

    definition = ToolDefinition(
        name="record_complete",
        description=(
            "Say more of a record, rather than open a second one: their name, where they stand, "
            "whether they are a child, their date of birth, whether their access wakes at their "
            'next visit, how long they stay (stay_until "now" when you judge they left: their '
            "access sleeps, nothing of them is lost), and which memories are about them (those "
            "you kept as free text). stay_until is for a guest or a visiting record with no "
            "account, and only ends or shortens a stay that runs; for anyone else the result says "
            "stay_not_ended (ending a member's access is household_remove); a later end, or a "
            "stay that is over, gives an access and is left as it is: the result says "
            "stay_not_changed and which gesture gives it. That someone passing by now lives "
            "here is never said here: household_change_role with lives_here. Name them, or "
            "person:<id> when two records share a name. A child's mark is lifted by the "
            "household's owner, or by you once a date of birth you know makes them of age; a "
            "date of birth the child gives in their own words is kept as what they said, never "
            "used to lift it. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "name": {
                    "type": "string",
                    "description": "The name the household calls them by from now on.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "standing": {"type": "string", "enum": list(STANDINGS)},
                "is_child": {
                    "type": "boolean",
                    "description": "True to mark a child, false to lift the mark.",
                },
                "born_on": {"type": "string", "description": "Their date of birth, YYYY-MM-DD."},
                "access_returns": {
                    "type": "boolean",
                    "description": "Whether their access wakes at their next visit.",
                },
                "stay_until": {
                    "type": "string",
                    "description": (
                        'The last day of their stay, YYYY-MM-DD, or "now": they left. For a '
                        "guest or a visiting record, to end or shorten a stay that runs."
                    ),
                },
                "memories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Identifiers of memories that are about them.",
                },
                "reason": _REASON_PROPERTY,
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "people.complete"
    person_arguments: Final = ("person",)
    names_among: Final = "records"
    _CHANGES: Final = frozenset(
        {"name", "standing", "is_child", "born_on", "access_returns", "stay_until", "memories"}
    )

    def _read(self, arguments: JsonObject, now: datetime) -> _Changes:
        _only(arguments, self._CHANGES | {"person", "reason"}, frozenset({"person"}))
        if not self._CHANGES & frozenset(arguments):
            raise InvalidArguments("invalid", "say what to complete: name, standing, is_child...")
        return _Changes(
            name=display_name_value(arguments["name"]) if "name" in arguments else None,
            standing=_standing(arguments["standing"]) if "standing" in arguments else None,
            is_child=_boolean(arguments["is_child"], "is_child")
            if "is_child" in arguments
            else None,
            born_on=_day(arguments["born_on"], "born_on") if "born_on" in arguments else None,
            access_returns=(
                _boolean(arguments["access_returns"], "access_returns")
                if "access_returns" in arguments
                else None
            ),
            stay_until=(
                _stay_until(arguments["stay_until"], now) if "stay_until" in arguments else None
            ),
            memories=tuple(_uuids(arguments["memories"])) if "memories" in arguments else (),
        )

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        self._read(arguments, ctx.clock())
        async with ctx.session_factory() as session:
            person = await record_named(session, _name(arguments["person"], "person"), ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        changes = self._read(arguments, ctx.clock())
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            person = await record_named(session, _name(arguments["person"], "person"), ctx)
            stays = changes.stay_until is None or await records.stay_may_end(session, person)
            # A later end, or a stay that is over woken, gives an access: the judged gesture does.
            gives_access = (
                stays
                and changes.stay_until is not None
                and not records.only_shortens(person, changes.stay_until, ctx.clock())
            )
            runs = is_active(person, ctx.clock())
            # Someone passing by who now lives here is never noted silently: it changes whose
            # keys they use and whether they may agree for a child (the owner's answer of
            # 2026-11-27: judged and told, ``household_change_role`` with lives_here).
            moves_in = (
                changes.standing == "lives_here"
                and person.standing != "lives_here"
                and person.state == "has_access"
            )
            standing = None if moves_in else changes.standing
            their_word = (
                changes.born_on is not None
                and (person.is_child or changes.is_child is True)
                and whose.turn_of(arguments, ctx) == person.id
            )
            try:
                await records.complete(
                    session,
                    person,
                    name=changes.name,
                    standing=standing,
                    is_child=changes.is_child,
                    born_on=changes.born_on,
                    access_returns=changes.access_returns,
                    stay_until=changes.stay_until,
                    memories=changes.memories,
                    seen_by=None if acts_alone(ctx) else ctx.person.id,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                )
            except records.NotPossible as error:
                raise _refused(error) from None
            completed: JsonObject = {"id": str(person.id), "person": person.display_name}
            if their_word:
                # Their own word: kept as what they said, never used to lift the mark.
                completed["born_on_kept_as_their_word"] = True
            if moves_in:
                # Left as it is: the judged gesture says it.
                completed["standing_not_changed"] = True
                completed["lives_here_by"] = "household_change_role"
            if not stays:
                completed["stay_not_ended"] = _NOT_A_STAY
            elif gives_access:
                # Left as it is: the gesture that gives an access, judged with the owner's limits.
                completed["stay_not_changed"] = True
                completed["gives_access_by"] = (
                    "household_change_role" if runs else "household_bring_back"
                )
            await session.commit()
        return ToolOutcome(status="done", model_result=completed, card=_CARD)


async def _may_look_at_others(session: AsyncSession, ctx: ToolContext, actor: Actor) -> bool:
    """Whether this call may look at another's record: ``whose.may_see_every_record``, or the
    person of the turn holds a right the owner wrote on ``people.look`` for them or their role
    (the owner's answer of 2026-11-27: the right he gives works; what is said of it stays the
    assistant's judgement, between people)."""
    if whose.may_see_every_record(ctx, actor):
        return True
    if actor.subject != "person":
        return False
    given = await session.scalar(
        select(PolicyRight.id)
        .where(
            PolicyRight.action_type == "people.look",
            PolicyRight.access == "allow",
            PolicyRight.for_domo.is_(False),
            PolicyRight.revoked_at.is_(None),
            PolicyRight.paused_at.is_(None),
            or_(
                PolicyRight.person_id == ctx.person.id,
                (PolicyRight.person_id.is_(None)) & (PolicyRight.role == ctx.person.role),
            ),
        )
        .limit(1)
    )
    return given is not None


class RecordLook:
    """Look at a record: one's own, or, for the owner's request or his own idea, anyone's."""

    definition = ToolDefinition(
        name="record_look",
        description=(
            "Look at a record. Without person: the record of the person you are talking with, "
            "about themselves: their name, where they stand, their role, and the links they said "
            "or confirmed. With person (their name, or person:<id>): only when the household's "
            "owner asks it, the person was given that right, or the idea is yours (a turn "
            "of your own, or after act_on_my_own, to check for a duplicate before opening a "
            "record: the look takes the mark, so mark the "
            "gesture that follows again): it adds their state, where the record comes from and "
            "why, and what was done to it and why. " + BETWEEN_PEOPLE
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Their name, or person:<id>. Leave it out for oneself.",
                },
            },
            "additionalProperties": False,
        },
    )
    action_type = "people.look"
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _asks_for_another(arguments: JsonObject) -> str | None:
        _only(arguments, frozenset({"person"}), frozenset())
        return None if "person" not in arguments else _name(arguments["person"], "person")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        other = self._asks_for_another(arguments)
        target = f"person:{ctx.person.id}"
        if other is not None:
            async with ctx.session_factory() as session:
                if not await _may_look_at_others(session, ctx, actor):
                    raise InvalidArguments(
                        "not_possible",
                        "another record is looked at for the owner, on your own, or for someone "
                        "the owner gave that right",
                    )
                target = f"person:{(await record_named(session, other, ctx)).id}"
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=target,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        other = self._asks_for_another(arguments)
        async with ctx.session_factory() as session:
            if other is None:
                shown = await self._about_oneself(session, ctx.person.id)
            else:
                if not await _may_look_at_others(session, ctx, ctx.acting):
                    raise InvalidArguments("not_possible", "another record is looked at on request")
                shown = await self._about_another(session, await record_named(session, other, ctx))
        return ToolOutcome(status="done", model_result=shown, card=_CARD)

    @staticmethod
    async def _links(
        session: AsyncSession, person: Person, *, theirs_only: bool
    ) -> list[JsonObject]:
        """The links of the record, with the name of the other: all of them, or, for the person
        themselves, those they said and those about them they confirmed."""
        condition = or_(PersonLink.from_id == person.id, PersonLink.to_id == person.id)
        if theirs_only:
            condition = condition & (
                (PersonLink.said_by_id == person.id) | PersonLink.confirmed_at.is_not(None)
            )
        found = (await session.scalars(select(PersonLink).where(condition))).all()
        shown: list[JsonObject] = []
        for link in found:
            mine = link.from_id == person.id
            other = await session.get(Person, link.to_id if mine else link.from_id)
            if other is None or other.forgotten_at is not None:
                continue
            shown.append(
                {
                    "relation": link.relation,
                    "direction": "from_them" if mine else "to_them",
                    "other": other.display_name,
                }
            )
        return shown

    async def _about_oneself(self, session: AsyncSession, person_id: UUID) -> JsonObject:
        person = await session.get(Person, person_id)
        if person is None:
            raise InvalidArguments("not_found", "no record")
        kept = await session.scalars(
            select(Memory)
            .where(
                Memory.about_id == person.id,
                (Memory.kept_private.is_(False)) | (Memory.said_by_id == person.id),
            )
            .order_by(Memory.created_at.desc())
            .limit(20)
        )
        return {
            "name": person.display_name,
            "standing": person.standing,
            "role": person.role,
            "is_child": person.is_child,
            "links": await self._links(session, person, theirs_only=True),
            "memories": [
                {
                    "subject": m.subject,
                    "content": m.content,
                    "said_by_them": m.said_by_id == person.id,
                }
                for m in kept
            ],
        }

    async def _about_another(self, session: AsyncSession, person: Person) -> JsonObject:
        has_account = await session.scalar(select(Account.id).where(Account.person_id == person.id))
        acts: list[JsonObject] = []
        for act in await why(session, person.id):
            who = act.by
            if who.startswith("person:"):
                named = await session.get(Person, UUID(who.removeprefix("person:")))
                who = "someone" if named is None else named.display_name
            acts.append(
                {
                    "act": act.act,
                    "by": who,
                    "reason": act.reason,
                    "at": act.at.isoformat(),
                }
            )
        return {
            "id": str(person.id),
            "name": person.display_name,
            "standing": person.standing,
            "role": person.role,
            "is_child": person.is_child,
            "state": person.state,
            "origin": person.origin,
            "origin_reason": person.origin_reason,
            "has_account": has_account is not None,
            "links": await self._links(session, person, theirs_only=False),
            "acts": acts,
        }


class RecordErase:
    """Erase a record made by mistake."""

    definition = ToolDefinition(
        name="record_erase",
        description=(
            "Erase a record that was made by mistake or that nothing comes of, as long as the "
            "person was never given an access and has no account (anyone else is forgotten, "
            "which is another gesture). The memories about them become free text again, kept; "
            "their links, nicknames and trust facts go. Their identifier is no longer a record. "
            "When the record was a mistake of yours, keep what you learn from it (memory_keep, a "
            "lesson with lesson_of). " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "reason": _REASON_PROPERTY,
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "people.erase"
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _read(arguments: JsonObject) -> str:
        _only(arguments, frozenset({"person", "reason"}), frozenset({"person"}))
        return _name(arguments["person"], "person")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        name = self._read(arguments)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._read(arguments)
        by = whose.by(arguments, ctx)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            # What a channel held for them goes with the record: it is told so once committed.
            refs = await hooks.refs_held(session, person.id)
            try:
                freed = await records.erase(
                    session,
                    person,
                    by=by,
                    reason=whose.reason_of(arguments),
                    turn_of=whose.turn_of(arguments, ctx),
                    now=ctx.clock(),
                )
            except records.NotPossible as error:
                raise _refused(error) from None
            if whose.is_his_own(arguments, ctx):
                # No reason kept: it would name someone whose record is emptied.
                await note_his_mistake(
                    session,
                    wrong_types=frozenset({"people.open"}),
                    wrong_type="people.open",
                    subject_key=f"person:{person.id}",
                    now=ctx.clock(),
                    reason=None,
                )
            erased = {"erased": True, "memories_now_free_text": [str(memory) for memory in freed]}
            told = hooks.RecordEvent(
                kind="erased",
                person_id=person.id,
                into_id=None,
                refs=refs,
                keep_minimum=False,
                at=ctx.clock(),
            )
            await session.commit()
        await ctx.record_listeners.emit(told)
        return ToolOutcome(status="done", model_result=erased, card=_CARD)


class AccessMistake:
    """Take back at once an access given to the wrong person."""

    definition = ToolDefinition(
        name="access_mistake",
        description=(
            "Take back at once an access given by mistake: the person is not the one you or the "
            "household thought. Their access ends now (sessions, devices, what waits for them), "
            "as when you remove someone, and the household's owner is owed the story: the result "
            "gives the role and accesses that were open, since when and for how long, and what "
            "they did meanwhile (how many actions of each type), for you to tell in your own "
            "words; when their access already ended, nothing is removed and the story is the "
            "same. Then correct the record, and when the mistake was yours keep what you learn "
            "from it (memory_keep, a lesson with lesson_of). " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "reason": _REASON_PROPERTY,
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "people.mistake"
    person_arguments: Final = ("person",)
    names_among: Final = "records"
    # What gave an access that a mistake takes back (``policy.subjects.UNDOES``), and the type
    # its fact names when none is found.
    _GAVE: Final = UNDOES["people.mistake"]
    _GAVE_BY_DEFAULT: Final = "household.change_role"

    @staticmethod
    def _read(arguments: JsonObject) -> str:
        _only(arguments, frozenset({"person", "reason"}), frozenset({"person"}))
        return _name(arguments["person"], "person")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        name = self._read(arguments)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="self_profile",
            destination="household",
        )

    @staticmethod
    async def _what_was_open(session: AsyncSession, person: Person, now: datetime) -> JsonObject:
        """The facts of the story: the role and the rights that were open, since when and for how
        long, and how many actions of each type the barrier let them do meanwhile (the audit
        log's ``policy.decision``: numbers and types only)."""
        given = await session.scalar(
            select(func.max(PersonAct.at)).where(
                PersonAct.person_id == person.id, PersonAct.act == "access_given"
            )
        )
        since = given or person.first_access_at or now
        made = (
            None
            if person.made_role_id is None
            else await made_roles.by_id(session, person.made_role_id)
        )
        rights = await session.scalars(
            select(PolicyRight.action_type).where(
                PolicyRight.person_id == person.id,
                PolicyRight.access == "allow",
                PolicyRight.revoked_at.is_(None),
                PolicyRight.paused_at.is_(None),
            )
        )
        action_type = AuditEntry.details["action_type"].astext
        did = await session.execute(
            select(action_type, func.count())
            .where(
                AuditEntry.actor == f"person:{person.id}",
                AuditEntry.action == "policy.decision",
                AuditEntry.details["decision"].astext == "allow",
                AuditEntry.ts >= since,
            )
            .group_by(action_type)
        )
        return {
            "person": f"person:{person.id}",
            "name": person.display_name,
            "role": person.role,
            "made_role": None if made is None else made.name,
            "rights": sorted(set(rights)),
            "since": since.isoformat(),
            "open_for_minutes": max(0, int((now - since).total_seconds() // 60)),
            "did": {kind: count for kind, count in did.all() if kind is not None},
        }

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._read(arguments)
        by = whose.by(arguments, ctx)
        reason = whose.reason_of(arguments)
        his_own = whose.is_his_own(arguments, ctx)
        # The owner's own removal is his withdrawal (spec 6.9): nobody but him gives it back, and
        # he is not told what he did himself.
        by_owner = whose.by_the_owner(arguments, ctx)
        owed = not by_owner
        turn = whose.turn_of(arguments, ctx)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            open_now = person.state == "has_access"
            # Someone whose access already ended: nothing to remove, the story is still told.
            ended = now if open_now else (person.disabled_at or now)
            told = await self._what_was_open(session, person, ended)
            told["removed_now"] = open_now
            if open_now:
                try:
                    await records.remove(
                        session,
                        person,
                        by=by,
                        by_owner=by_owner,
                        reason=reason,
                        turn_of=turn,
                        now=now,
                    )
                except records.NotPossible as error:
                    raise _refused(error) from None
            mistake = await note_act(
                session,
                act="access_mistake",
                by=by,
                reason=reason,
                turn_of=turn,
                person_id=person.id,
                owed_to_owner=owed,
                now=now,
            )
            if his_own:
                await note_his_mistake(
                    session,
                    wrong_types=self._GAVE,
                    wrong_type=self._GAVE_BY_DEFAULT,
                    subject_key=f"person:{person.id}",
                    now=now,
                    reason=reason,
                )
            # The single way the owner is told: his news kept, unless he saw it or agreed himself.
            owner = (
                await tell_owner(
                    session, mistake, knows=lambda one: whose.owner_knows(ctx, one), now=now
                )
                if owed
                else None
            )
            await session.commit()
        if owner is not None and ctx.notify is not None:
            # The owner is owed the story: a turn for them is due.
            ctx.notify(owner)
        return ToolOutcome(status="done", model_result=told, card=_CARD)


async def _forgotten(
    session: AsyncSession, person: Person, arguments: JsonObject, ctx: ToolContext
) -> tuple[JsonObject, UUID | None, hooks.RecordEvent]:
    """Forget ``person`` as the call's act, with the owner to tell once committed, if any
    (``forget.forget``), and what the channels are told once committed (``people.hooks``: the
    references they held for them, and whether those stay, ``forget.recognition_kept``). When
    they are the person of the call, what it runs in (their conversations, requests and account)
    goes once it is over: the turn finishes it (``ToolContext.forgotten_in_turn``), else the
    upkeep (``forget.finish_the_forgotten``). Flushed, not committed."""
    theirs = person.id == ctx.person.id
    now = ctx.clock()
    refs = await hooks.refs_held(session, person.id)
    try:
        owner = await forget.forget(
            session,
            person,
            by=whose.by(arguments, ctx),
            turn_of=whose.turn_of(arguments, ctx),
            now=now,
            finish_later=theirs,
            by_owner=whose.by_the_owner(arguments, ctx),
            owner_knows=lambda owner: whose.owner_knows(ctx, owner),
        )
    except records.NotPossible as error:
        raise InvalidArguments(
            "not_possible", _WHY_NOT_FORGOTTEN.get(error.code, error.code)
        ) from None
    if theirs:
        ctx.forgotten_in_turn.add(person.id)
    told = hooks.RecordEvent(
        kind="forgotten",
        person_id=person.id,
        into_id=None,
        refs=refs,
        keep_minimum=await forget.recognition_kept(session, person.id, now),
        at=now,
    )
    return {"forgotten": True}, owner, told


def _told(owner: UUID | None, ctx: ToolContext) -> None:
    """Once committed: the owner owed the telling of a forgetting has a turn due."""
    if owner is not None and ctx.notify is not None:
        ctx.notify(owner)


def _forgetting(person: Person, action_type: str) -> DescribedAction:
    """The action of forgetting ``person``, with what the judge weighs: whether it takes away the
    access of someone who lives here (``forget.loses_access``)."""
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=f"person:{person.id}",
        effect="self_profile",
        destination="household",
        facts={"loses_access": forget.loses_access(person)},
    )


def _may_be_forgotten(person: Person) -> None:
    """``InvalidArguments("not_possible")`` when ``person`` cannot be forgotten
    (``forget.why_not``): nothing is asked of anyone then."""
    refused = forget.why_not(person)
    if refused is not None:
        raise InvalidArguments("not_possible", _WHY_NOT_FORGOTTEN.get(refused, refused))


class ForgetMe:
    """Forget the person of the turn, at their own request."""

    definition = ToolDefinition(
        name="forget_me",
        description=(
            "Forget the person you are talking with, for good, when they ask you to: their "
            "record is emptied; their links, nicknames, trust, the memories about them, their "
            "conversations and their account go. What others said that is not about them stays. "
            "It cannot be undone: make sure they know it, once. Someone who lives here and has "
            "access loses it too, and the household's owner is told afterwards; the household's "
            "owner is never forgotten. Only for themselves: for someone else, record_forget. "
            "For a child it is your judgement alone; as it is hard to undo, you may first ask an "
            "adult to confirm when you judge it wise."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "people.forget_me"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _only(arguments, frozenset(), frozenset())
        if actor.subject == "domo":
            # Their own request has no reason to give: an idea of the assistant's is another tool.
            raise InvalidArguments(
                "invalid",
                "forget_me is the person's own request: call it without act_on_my_own; when the "
                "idea is yours, record_forget with its reason",
            )
        async with ctx.session_factory() as session:
            person = await session.get(Person, ctx.person.id)
        if person is None:
            raise InvalidArguments("not_found", "no record")
        _may_be_forgotten(person)
        return _forgetting(person, self.action_type)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _only(arguments, frozenset(), frozenset())
        async with ctx.session_factory() as session:
            person = await session.get(Person, ctx.person.id)
            if person is None:
                raise InvalidArguments("not_found", "no record")
            forgotten, owner, told = await _forgotten(session, person, arguments, ctx)
            await session.commit()
        await ctx.record_listeners.emit(told)
        _told(owner, ctx)
        return ToolOutcome(status="done", model_result=forgotten, card=_CARD)


class RecordForget:
    """Forget someone else, when someone of the household relays their wish."""

    definition = ToolDefinition(
        name="record_forget",
        description=(
            "Forget someone for good when someone of the household tells you they want it and "
            "you judge it is really their wish: their record is emptied; their links, nicknames, "
            "trust, the memories about them, their conversations and their account, if any, go. "
            "What others said that is not about them stays, and a withdrawal or a refusal of the "
            "household's owner stays, with a description only, to know them again. It cannot be "
            "undone: say it once to whoever does not know it, then do it. Someone who lives here "
            "and has access loses it too, and the household's owner is told afterwards; the "
            "household's owner is never forgotten. Even as your own idea, it is still their wish "
            "you carry out, never a whim. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "reason": _REASON_PROPERTY,
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    )
    action_type = "people.forget"
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    @staticmethod
    def _read(arguments: JsonObject) -> str:
        _only(arguments, frozenset({"person", "reason"}), frozenset({"person"}))
        return _name(arguments["person"], "person")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        name = self._read(arguments)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
        _may_be_forgotten(person)
        return _forgetting(person, self.action_type)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = self._read(arguments)
        async with ctx.session_factory() as session:
            person = await record_named(session, name, ctx)
            forgotten, owner, told = await _forgotten(session, person, arguments, ctx)
            await session.commit()
        await ctx.record_listeners.emit(told)
        _told(owner, ctx)
        return ToolOutcome(status="done", model_result=forgotten, card=_CARD)
