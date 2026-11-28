"""What an action is about, whom the thing belongs to, and what it undoes ("Domo judges first").

One registry, keyed by action type, and nothing else builds these: the subject key (the same thing
reached by two actions has the same key, so that a close and an open of one address, or a cancel and
the plan it cancels, are known to be about one thing), whom the thing belongs to, and the types an
action undoes. A tool never builds a key itself.

``subject_of`` is pure and never raises: a fact that is missing gives ``key=None`` (an unknown
subject, not a wrong one). ``concerned_of`` reads the database to say who answers for the thing; it
reads facts as values only, so a text a person wrote in a fact (a name, a note) never moves who is
asked.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import is_child
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at, is_active
from domo_core.llm.types import JsonObject
from domo_core.people import reach
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import MADE_PREFIX, an_adult_agrees_for, is_made

HOUSE: Final = "house"


@dataclass(frozen=True)
class Subject:
    """What an action is about: ``key`` (``None``: not known, yet or at all) and whom the thing
    belongs to, a person or the house."""

    key: str | None
    belongs_to: UUID | Literal["house"]


Keys = Callable[[DescribedAction, JsonObject | None], tuple[str, ...]]


@dataclass(frozen=True)
class SubjectRule:
    """How the subject of one action type is built.

    ``keys`` gives the canonical key first, then aliases (other keys the same thing is known by);
    the second argument is the tool's ``model_result`` once it ran (``None`` before), for what has
    no id before it exists. ``owner`` says whom the thing belongs to: the house, the person of the
    turn, or the person a fact names (``person_fact``; a fact ``for_house`` set makes it the
    house's). ``the_turn_person`` makes the person of the turn the thing itself (its key
    ``person:<id>``), for what a person asks about themselves."""

    keys: Keys
    owner: Literal["house", "turn_person", "person_fact"]
    person_fact: str | None = None
    the_turn_person: bool = False


# --- the keys ------------------------------------------------------------------------------------


def _text(action: DescribedAction, name: str) -> str | None:
    value = action.facts.get(name)
    return value if isinstance(value, str) and value else None


def _port(action: DescribedAction) -> str | None:
    value = action.facts.get("port")
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _result_id(result: JsonObject | None) -> str | None:
    value = None if result is None else result.get("id")
    return value if isinstance(value, str) and value else None


def _address(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    host, port = _text(action, "host"), _port(action)
    return () if host is None or port is None else (f"address:{host}:{port}",)


def _account(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    name = _text(action, "name")
    if name is None:
        return ()
    if action.facts.get("for_house") is True:
        return (f"account:{HOUSE}:{name}",)
    whose = _text(action, "giver") or _text(action, "of")
    return () if whose is None else (f"account:{whose}:{name}",)


def _invitee(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    name = _text(action, "display_name")
    return () if name is None else (f"invitee:{name.casefold()}",)


def _invitee_or_record(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    """The invitee's name, then, when the invitation is for a record that exists already (its
    target, ``person:<id>``), that record too."""
    record = (
        () if action.target is None or not action.target.startswith(_PERSON) else (action.target,)
    )
    return (*_invitee(action, result), *record)


def _target(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    return () if action.target is None else (action.target,)


_PERSON: Final = "person:"


def _id_in(action: DescribedAction, name: str) -> str | None:
    """The id a fact ``name`` holds, written ``person:<id>`` or bare."""
    value = _text(action, name)
    return None if value is None else value.removeprefix(_PERSON) or None


def _link(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    """A link between two records (facts ``from`` and ``to``), then the record it starts from."""
    start, end = _id_in(action, "from"), _id_in(action, "to")
    if start is None or end is None:
        return ()
    return (f"link:{start}:{end}", f"{_PERSON}{start}")


def _merge(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    """Two records made one (facts ``kept`` and ``absorbed``), or split again: the kept one, then
    the absorbed one."""
    kept, absorbed = _id_in(action, "kept"), _id_in(action, "absorbed")
    return tuple(f"{_PERSON}{named}" for named in (kept, absorbed) if named is not None)


def _promise_kept(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    """A promise kept for a record: the record (the target), then the promise (fact
    ``promise``)."""
    promise = _text(action, "promise")
    kept = () if promise is None else (f"promise:{promise.removeprefix('promise:')}",)
    return (*_target(action, result), *kept)


def _directive(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    """An owner's directive: about the record it names (the target), else about its kind (fact
    ``kind``)."""
    if action.target is not None:
        return (action.target,)
    kind = _text(action, "kind")
    return () if kind is None else (f"directive:{kind}",)


def _target_or_invitee(named: str) -> Keys:
    """The target, then, once the tool ran, the invitee its result names (``named``, their
    display name): the person someone invited is known by the name they were invited with
    (``household.invite``: ``invitee:<name>``), so that removing them or cancelling their
    invitation is about what the invitation was about, and so is a change of their role."""

    def keys(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
        name = None if result is None else result.get(named)
        invitee = (f"invitee:{name.casefold()}",) if isinstance(name, str) and name else ()
        return (*_target(action, result), *invitee)

    return keys


def _device(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    if action.target is not None:
        return (action.target,)
    person = _text(action, "person")
    return () if person is None else (f"device:of:{person}",)


def _result_of(kind: str) -> Keys:
    """``<kind>:<result id>`` once the tool ran, else nothing (the thing has no id before)."""

    def keys(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
        given = _result_id(result)
        return () if given is None else (f"{kind}:{given}",)

    return keys


def _note_left(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    recipient = _text(action, "recipient")
    to = () if recipient is None else (f"person:{recipient}",)
    return (*_result_of("note")(action, result), *to)


def _tool(action: DescribedAction, result: JsonObject | None) -> tuple[str, ...]:
    return (f"tool:{action.action_type.removeprefix(MADE_PREFIX)}",)


SUBJECT_RULES: Final[Mapping[str, SubjectRule]] = MappingProxyType(
    {
        **{
            kind: SubjectRule(_address, "house")
            for kind in ("net.open", "net.open_host", "net.close")
        },
        "account.ask": SubjectRule(_account, "person_fact", "giver"),
        "account.remove": SubjectRule(_account, "person_fact", "of"),
        "household.invite": SubjectRule(_invitee_or_record, "house"),
        "household.invite_again": SubjectRule(_invitee, "house"),
        "household.cancel_invitation": SubjectRule(_target_or_invitee("cancelled"), "house"),
        # The invitee its result names (``person``) too, so that a role given at the invitation
        # and changed later is known to be undone (R55's cost, ``UNDOES``).
        "household.change_role": SubjectRule(_target_or_invitee("person"), "house"),
        "household.remove": SubjectRule(_target_or_invitee("removed"), "house"),
        "household.bring_back": SubjectRule(_target, "house"),
        "device.entrust": SubjectRule(_device, "person_fact", "person"),
        "device.remove": SubjectRule(_device, "person_fact", "person"),
        "plan.set": SubjectRule(_result_of("plan"), "turn_person"),
        "plan.change": SubjectRule(_target, "turn_person"),
        "plan.cancel": SubjectRule(_target, "turn_person"),
        "memory.keep": SubjectRule(_result_of("memory"), "turn_person"),
        "memory.change": SubjectRule(_target, "turn_person"),
        "memory.forget": SubjectRule(_target, "turn_person"),
        "note.leave": SubjectRule(_note_left, "turn_person"),
        "note.withdraw": SubjectRule(_target, "turn_person"),
        "note.keep": SubjectRule(_target, "turn_person"),
        # The people of the house: everything of the house is the owner's to answer, never the
        # person a doubted idea is about.
        "people.open": SubjectRule(_result_of("person"), "house"),
        **{
            kind: SubjectRule(_target, "house")
            for kind in (
                "people.complete",
                "people.erase",
                "people.forget",
                "people.look",
                "people.mistake",
                "household.send_link",
                "household.show_link",
                "role.change",
                "role.delete",
                "role.look",
                "access.promise_drop",
                "access.promises_look",
            )
        },
        "people.forget_me": SubjectRule(_target, "turn_person", the_turn_person=True),
        "people.link": SubjectRule(_link, "house"),
        "people.unlink": SubjectRule(_link, "house"),
        "people.merge": SubjectRule(_merge, "house"),
        "people.split": SubjectRule(_merge, "house"),
        "role.create": SubjectRule(_result_of("role"), "house"),
        "access.promise": SubjectRule(_result_of("promise"), "house"),
        "access.promise_keep": SubjectRule(_promise_kept, "house"),
        **{
            kind: SubjectRule(_directive, "house")
            for kind in ("people.directive_set", "people.directive_lift", "people.directives_look")
        },
        **{
            kind: SubjectRule(_target, "turn_person")
            for kind in (
                "key.add",
                "key.remove",
                "key.share",
                "key.admin",
                "model.rekey",
                "spending.limit",
            )
        },
    }
)

# The judged types that have no subject of their own, each with the reason: what they are about
# is the person's turn as a whole, or nothing a second action could reach.
NO_SUBJECT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "household.leave": "the person acts on themselves alone; nothing else reaches it",
        "household.transfer_ownership": "the owner's role itself, owner only; it has no twin",
        "account.share": "opens or closes a house account to guests or Domo; no second action",
        "agreement.keep": "an agreement is about the program's own bounds, not a thing to reach",
        "agreement.drop": "an agreement is about the program's own bounds, not a thing to reach",
        "chain.run": "carries steps that are judged inside, each with its own subject",
        "keys.house": "the house's rules on keys as a whole; one thing, no key to share",
        "models.set_level": "the level of a model, owner only; no second action reaches it",
        "policy.update": "the rules of the house as a whole, owner only",
        "rights.apply": "the owner's rights as a whole, owner only",
        "successors.set": "the owner's list of successors as a whole, owner only",
        "successors.note_death": "the owner's own act on the list of successors, owner only",
        "successors.undo_death": "the owner's own act on the list of successors, owner only",
        "profile.rename": "a person's own name; nothing else reaches it",
        "texts.for_someone": "a person's own texts; nothing else reaches them",
        "texts.language": "a person's own language; nothing else reaches it",
        "texts.language_version": "a person's own texts; nothing else reaches them",
        "texts.note": "a person's own writing note; nothing else reaches it",
        "texts.restore": "a person's own texts; nothing else reaches them",
        "texts.rewrite": "a person's own texts; nothing else reaches them",
    }
)

# What an action undoes: the types whose effect it takes back.
UNDOES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "plan.cancel": frozenset({"plan.set", "plan.change"}),
        "net.close": frozenset({"net.open", "net.open_host"}),
        "account.remove": frozenset({"account.ask"}),
        "household.cancel_invitation": frozenset({"household.invite", "household.invite_again"}),
        # A role changed takes back a role given before: at the invitation, at a return, by a
        # promise kept, or by another change (R55 left the invitation out: a role given at the
        # invitation and changed later was not noted; the rule above now knows the invitee).
        "household.change_role": frozenset(
            {
                "household.change_role",
                "household.invite",
                "household.bring_back",
                "access.promise_keep",
            }
        ),
        "household.remove": frozenset(
            {"household.invite", "household.bring_back", "access.promise_keep"}
        ),
        "memory.forget": frozenset({"memory.keep", "memory.change"}),
        "note.withdraw": frozenset({"note.leave"}),
        "people.erase": frozenset({"people.open"}),
        "people.split": frozenset({"people.merge"}),
        "people.unlink": frozenset({"people.link"}),
        "role.delete": frozenset({"role.create"}),
        "access.promise_drop": frozenset({"access.promise"}),
        # An access given by mistake, or one the owner's directive withdraws: what gave it.
        **{
            kind: frozenset(
                {
                    "household.invite",
                    "household.change_role",
                    "household.bring_back",
                    "access.promise_keep",
                }
            )
            for kind in ("people.mistake", "people.directive_set")
        },
    }
)


# --- the subject ---------------------------------------------------------------------------------


def _belongs_to(
    rule: SubjectRule, action: DescribedAction, turn_person_id: UUID
) -> UUID | Literal["house"]:
    match rule.owner:
        case "house":
            return HOUSE
        case "turn_person":
            return turn_person_id
        case "person_fact":
            if action.facts.get("for_house") is True:
                return HOUSE
            named = None if rule.person_fact is None else _text(action, rule.person_fact)
            try:
                # A fact of kind ``person`` is written ``person:<id>`` (``catalog.FactType``).
                return turn_person_id if named is None else UUID(named.removeprefix("person:"))
            except ValueError:
                return turn_person_id


def _rule_of(action: DescribedAction) -> SubjectRule | None:
    """The rule of ``action``'s type; for a tool Domo made, the house's when the facts say the
    launch is for the house; ``None`` for a type with no rule."""
    kind = action.action_type
    rule = SUBJECT_RULES.get(kind)
    if rule is None and is_made(kind):
        house = action.facts.get("for_house") is True
        rule = SubjectRule(_tool, "house" if house else "turn_person")
    return rule


def keys_of(
    action: DescribedAction, result: JsonObject | None, turn_person_id: UUID | None = None
) -> tuple[str, ...]:
    """Every key ``action`` is about, the canonical one first (``subject_of``'s), then its
    aliases; ``result`` is the tool's ``model_result`` once it ran. A type with no rule is about
    its target; one whose rule makes the person of the turn the thing itself is about
    ``turn_person_id`` (``person:<id>``), when it is given. Pure; never raises (nothing known gives
    ``()``)."""
    rule = _rule_of(action)
    if rule is None:
        return () if action.target is None else (action.target,)
    if rule.the_turn_person and turn_person_id is not None:
        return (f"{_PERSON}{turn_person_id}",)
    return rule.keys(action, result)


def subject_of(action: DescribedAction, result: JsonObject | None, turn_person_id: UUID) -> Subject:
    """What ``action`` is about and whom the thing belongs to; ``result`` is the tool's
    ``model_result`` once it ran. A type with no rule is about its target, the turn person's.
    Pure; never raises (a missing fact gives ``key=None``)."""
    rule = _rule_of(action)
    if rule is None:
        return Subject(action.target, turn_person_id)
    if rule.the_turn_person:
        return Subject(f"{_PERSON}{turn_person_id}", turn_person_id)
    keys = rule.keys(action, result)
    return Subject(keys[0] if keys else None, _belongs_to(rule, action, turn_person_id))


# --- who answers ---------------------------------------------------------------------------------


async def owner_of_now(session: AsyncSession, now: datetime) -> UUID:
    """The owner who is in the household at ``now``; ``LookupError`` when there is none. The one
    way the owner of now is found (``concerned_of``, the judge's case). Reads only."""
    owner = await session.scalar(
        select(Person.id).where(Person.role == "owner", active_at(now)).limit(1)
    )
    if owner is None:
        raise LookupError("the household has no owner now")
    return owner


async def concerned_of(
    session: AsyncSession, subject: Subject, action: DescribedAction, now: datetime
) -> UUID:
    """The person who answers for the thing ``subject`` stands for: whom it belongs to when that
    is a person who is in the household now and can answer (has an account), is not the one
    ``action`` removes (``household.remove``, fact ``person``) and is not someone for whom an
    adult agrees on this type (``catalog.an_adult_agrees_for``, the rule the barrier reads); the
    owner of now otherwise, and for the house. Reads only."""
    belongs_to = subject.belongs_to
    if belongs_to != HOUSE:
        person = await session.get(Person, belongs_to)
        removed = action.action_type == "household.remove" and _text(action, "person") == str(
            belongs_to
        )
        adult_agrees = person is not None and an_adult_agrees_for(
            is_child(person), action.action_type
        )
        if (
            person is not None
            and is_active(person, now)
            and not removed
            and not adult_agrees
            and await reach.can_answer(session, person.id, now)
        ):
            return person.id
    return await owner_of_now(session, now)
