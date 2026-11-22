"""The rules of the house on the keys (0.6.0, spec 4.3, 4.7): ``keys_house`` (``keys.house``, effect
``permissions``). It is the owner's by default, can be given like any right, and is judged.

One call changes one or more of these:

* who is on the household's general key: ``person`` (by their name) ``on_general_key`` ``allow``
  or ``deny`` (a rule of the house, "Paul brings his own"). It writes a line of ``provider_users``
  for that person (``set_by`` the one who asks), which replaces the one before: on the general
  key, a line counts whoever wrote it (``router.allowed``), since its right was checked here. Never
  the one who gave the general key, who always uses it (``not_possible``);
* what an invitee uses when their invitation does not say (``invitee_default``: ``inviter``, the
  keys of whoever invited them, or ``own``), the setting ``keys.invitee_default``
  (``household.service.accept_invitation``);
* whether invitees who join from now on use the general key when their host does (``follow``, the
  default) or never, whatever their invitation says (``invitee_general_key``), the setting
  ``keys.invitee_general``; those already there keep what they have;
* whether the general key serves, for a few small calls, to ask someone who has no key Domo may
  use for them to give one (``upkeep_on_general_key``), the setting ``keys.upkeep``
  (``conversation.prepare``: the upkeep turn; without it, the page shows a fixed message);
* the firm limit of what the general key spends (``limit_amount`` ``limit_currency`` per
  ``limit_period``, a month by default, with the threshold ``warn_at_percent`` only if they ask;
  ``remove_limit`` takes it away), for ``person`` when one is named, for a role when
  ``limit_role`` (each adult, child or guest, now and later) and ``limit_split`` (``each`` of them,
  or ``together``) are given, else for the whole key. It is the row ``key_limit`` writes
  (``llm.spending.set_spending_limit``, which audits ``spending.limit_set``, or
  ``spending.limit_removed``).

A call that asks no change, or one that makes no sense, is ``invalid`` (a mistake of the call); a
name that means nobody in the household is ``not_found``. The changes and the audit entry
``keys.house_changed`` (``{"changes"}``: the id of the person named, and what changed, a limit as
``set`` or ``removed``) are one transaction, the entry written last. A call that changed nothing
(it takes away a limit there is not) writes no entry, and its card says so
(``action.card.done.keys.house.unchanged``), rather than that the rules changed.
"""

from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.household.service import resolve_person
from domo_core.llm.models import Provider, ProviderUser
from domo_core.llm.spending import set_spending_limit
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.policy.settings import (
    INVITEE_DEFAULT_KEY,
    INVITEE_DEFAULTS,
    INVITEE_GENERAL_KEY,
    INVITEE_GENERALS,
    UPKEEP_KEY,
    InviteeDefault,
    InviteeGeneral,
    update_policy_setting,
)
from domo_core.text import is_plain_line
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.key_tools import LimitAsked, limit_asked, limit_parameters, one_of

KEYS_HOUSE_ACTION_TYPE: Final = "keys.house"

OnGeneralKey = Literal["allow", "deny"]
_ON_GENERAL_KEY: Final[tuple[OnGeneralKey, ...]] = ("allow", "deny")
_ARGUMENTS: Final = frozenset(
    {
        "person",
        "on_general_key",
        "invitee_default",
        "invitee_general_key",
        "upkeep_on_general_key",
        "limit_amount",
        "limit_currency",
        "limit_period",
        "limit_role",
        "limit_split",
        "warn_at_percent",
        "remove_limit",
    }
)
_NO_GENERAL_KEY: Final = "the household has no general key now"
_THE_GIVER: Final = (
    "that person gave the household's general key: they always use it, so there is nothing to "
    "change for them"
)
_NOT_THE_SAME: Final = (
    "this name no longer means the one the request was about, so nothing was done: ask again"
)


@dataclass(frozen=True)
class _Change:
    """What a ``keys_house`` call asks for (each ``None`` when it does not ask it)."""

    person: str | None
    on_general_key: OnGeneralKey | None
    invitee_default: InviteeDefault | None
    invitee_general: InviteeGeneral | None
    upkeep: bool | None
    limit: LimitAsked | None

    @property
    def on_the_general_key(self) -> bool:
        """Whether it changes something of the general key itself (a line, a limit)."""
        return self.on_general_key is not None or self.limit is not None


def _maybe_one_of[T: str](value: object, known: tuple[T, ...], name: str) -> T | None:
    """``value`` if it is one of ``known``, ``None`` when it is not given."""
    return None if value is None else one_of(value, known, name)


def _change_of(arguments: JsonObject) -> _Change:
    """The changes a call asks for. ``InvalidArguments("invalid")`` for none, or for one that
    makes no sense."""
    if set(arguments) - _ARGUMENTS:
        raise InvalidArguments("invalid", f"keys_house takes {', '.join(sorted(_ARGUMENTS))}")
    name = arguments.get("person")
    if name is not None and (
        not isinstance(name, str) or not name.strip() or not is_plain_line(name)
    ):
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    upkeep = arguments.get("upkeep_on_general_key")
    if upkeep is not None and not isinstance(upkeep, bool):
        raise InvalidArguments("invalid", "upkeep_on_general_key is true or false")
    change = _Change(
        person=name if isinstance(name, str) else None,
        on_general_key=_maybe_one_of(
            arguments.get("on_general_key"), _ON_GENERAL_KEY, "on_general_key"
        ),
        invitee_default=_maybe_one_of(
            arguments.get("invitee_default"), INVITEE_DEFAULTS, "invitee_default"
        ),
        invitee_general=_maybe_one_of(
            arguments.get("invitee_general_key"), INVITEE_GENERALS, "invitee_general_key"
        ),
        upkeep=upkeep,
        limit=limit_asked(
            arguments,
            amount="limit_amount",
            currency="limit_currency",
            period="limit_period",
            remove="remove_limit",
            role="limit_role",
            split="limit_split",
        ),
    )
    if change.limit is not None and change.limit.role is not None and change.person is not None:
        raise InvalidArguments("invalid", "a limit is for a person or for a role, not both")
    if change.on_general_key is not None and change.person is None:
        raise InvalidArguments("invalid", "on_general_key goes with person, their name")
    if change.person is not None and not change.on_the_general_key:
        raise InvalidArguments("invalid", "person goes with on_general_key, or with a limit")
    if not (
        change.on_the_general_key
        or change.invitee_default is not None
        or change.invitee_general is not None
        or change.upkeep is not None
    ):
        raise InvalidArguments("invalid", "say at least one change")
    return change


async def _general_key(session: AsyncSession) -> Provider:
    found = await session.execute(select(Provider).where(Provider.is_general))
    general = found.scalars().first()
    if general is None:
        raise InvalidArguments("not_possible", _NO_GENERAL_KEY)
    return general


async def _subject(
    session: AsyncSession, change: _Change, general: Provider | None, ctx: ToolContext
) -> Person | None:
    """The person the call names, in the household now (``None`` when it names nobody). Never
    the one who gave the general key, for a line of it."""
    if change.person is None:
        return None
    person = await resolve_person(session, change.person, now=ctx.clock(), speaker=ctx.person.id)
    if change.on_general_key is not None and general is not None and general.given_by == person.id:
        raise InvalidArguments("not_possible", _THE_GIVER)
    return person


def _target(person: Person | None) -> str | None:
    return None if person is None else f"person:{person.id}"


class KeysHouse:
    """Change the rules of the house on the keys."""

    definition = ToolDefinition(
        name="keys_house",
        description=(
            "Change the rules of the household's keys, when that is what the person you are "
            "talking with wants: whether someone (person, their name) may use the household's "
            "general key, or must bring a key of their own (on_general_key); what an invitee "
            "uses when their invitation does not say (invitee_default: the keys of whoever "
            "invited them, or their own); whether invitees who join from now on use the general "
            "key when their host does or never (invitee_general_key); whether the general key may "
            "serve, for a few small calls, to ask someone who has no key you may use for them to "
            "give one "
            "(upkeep_on_general_key); and a firm limit on what the general key spends, for one "
            "person, for a role (limit_role and limit_split: each adult, child or guest, now "
            "and later) or for the whole household (limit_amount, limit_currency, limit_period, a "
            "calendar month unless they say otherwise; warn_at_percent only if they ask to be "
            "told; remove_limit takes it away). You never make a call that would pass a limit. "
            "One call may make several changes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "The name of the person, for on_general_key or a limit of "
                    "their own on the general key.",
                },
                "on_general_key": {
                    "type": "string",
                    "enum": list(_ON_GENERAL_KEY),
                    "description": "Whether that person may use the general key (deny: they "
                    "bring their own).",
                },
                "invitee_default": {
                    "type": "string",
                    "enum": list(INVITEE_DEFAULTS),
                    "description": "What an invitee uses when their invitation does not say: "
                    "their inviter's keys (inviter), or a key of their own (own).",
                },
                "invitee_general_key": {
                    "type": "string",
                    "enum": list(INVITEE_GENERALS),
                    "description": "Whether invitees who join from now on may use the general key "
                    "when their host does (follow, the default) or never, whatever their "
                    "invitation says (never); those already there keep what they have.",
                },
                "upkeep_on_general_key": {
                    "type": "boolean",
                    "description": "Whether the general key serves to ask someone without a key "
                    "for one (it does unless they say otherwise).",
                },
                **limit_parameters(
                    amount="limit_amount",
                    currency="limit_currency",
                    period="limit_period",
                    remove="remove_limit",
                    role="limit_role",
                    split="limit_split",
                ),
            },
            "additionalProperties": False,
        },
    )
    action_type = KEYS_HOUSE_ACTION_TYPE
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        change = _change_of(arguments)
        async with ctx.session_factory() as session:
            general = await _general_key(session) if change.on_the_general_key else None
            person = await _subject(session, change, general, ctx)
        return DescribedAction(
            connector="core",
            action_type=KEYS_HOUSE_ACTION_TYPE,
            target=_target(person),
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        change = _change_of(arguments)
        now = ctx.clock()
        by = ctx.person.id
        async with ctx.session_factory() as session:
            general = await _general_key(session) if change.on_the_general_key else None
            person = await _subject(session, change, general, ctx)
            described = ctx.described_action
            if described is not None and described.target != _target(person):
                raise InvalidArguments("not_found", _NOT_THE_SAME)
            audited: JsonObject = {}
            told: JsonObject = {}
            if person is not None:
                audited["person"] = str(person.id)
                told["person"] = person.display_name
            if change.on_general_key is not None and general is not None and person is not None:
                allowed = change.on_general_key == "allow"
                statement = insert(ProviderUser).values(
                    provider_id=general.id,
                    scope="person",
                    person_id=person.id,
                    allowed=allowed,
                    set_by=by,
                    set_at=now,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_provider_users_provider_scope_person",
                        set_={"allowed": allowed, "set_by": by, "set_at": now},
                    )
                )
                audited["on_general_key"] = told["on_general_key"] = change.on_general_key
            if change.invitee_default is not None:
                await update_policy_setting(
                    session, key=INVITEE_DEFAULT_KEY, value=change.invitee_default, by=by, now=now
                )
                audited["invitee_default"] = told["invitee_default"] = change.invitee_default
            if change.invitee_general is not None:
                await update_policy_setting(
                    session,
                    key=INVITEE_GENERAL_KEY,
                    value=change.invitee_general,
                    by=by,
                    now=now,
                )
                audited["invitee_general_key"] = told["invitee_general_key"] = (
                    change.invitee_general
                )
            if change.upkeep is not None:
                await update_policy_setting(
                    session, key=UPKEEP_KEY, value=change.upkeep, by=by, now=now
                )
                audited["upkeep_on_general_key"] = told["upkeep_on_general_key"] = change.upkeep
            limit = change.limit
            if limit is not None and general is not None:
                changed = await set_spending_limit(
                    session,
                    provider_id=general.id,
                    person_id=None if person is None else person.id,
                    amount=limit.amount,
                    currency=limit.currency,
                    period=limit.period,
                    warn_at_percent=limit.warn_at_percent,
                    by=by,
                    now=now,
                    role=limit.role,
                    split=limit.split,
                )
                if changed:
                    audited["limit"] = "set" if limit.amount is not None else "removed"
                told["limit"] = limit.told(changed)
            changed_something = bool(set(audited) - {"person"})
            if changed_something:
                # Last, just before the commit.
                await append_audit(
                    session,
                    actor=f"person:{by}",
                    action="keys.house_changed",
                    details={"changes": audited},
                )
            await session.commit()
        card_key = (
            "action.card.done.keys.house"
            if changed_something
            else "action.card.done.keys.house.unchanged"
        )
        return ToolOutcome(
            status="done",
            model_result={"changed": told},
            card={"key": card_key, "params": {}},
        )
