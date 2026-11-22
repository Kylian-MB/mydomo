"""The tools with which the owner reads and changes their own rules (decision D8).

The settings are how long a request may wait, the rights writer's time, the household's time zone
and how big the tools' own database may grow (``tools.base_mb``). Whether an action is confirmed,
and how, is no setting: Domo judges it (the yes-judge), and the owner's rules "always ask me
before..." are rights (``rights_propose``).

* ``policy_show`` (the owner only, changes nothing): what is set now, each setting with where its
  value comes from (``default`` or ``owner``); the household's time zone (``None`` until the
  owner sets it); and the owner's rights (spec 5.6), those in force then the paused ones, each
  with whom it is for (a person's name, a role, Domo himself under ``rights.subject.domo``, or
  ``everyone``), its action type, what it says
  to ask (``ask``, ``ask_when``) and its summary;
* ``policy_update`` (the owner only): change one setting, or clear it with a ``null`` value
  (back to the default). The judge decides whether it is asked first, like any action. The
  change and its audit entry (``policy.updated``: the key, the old and the new value) are one
  transaction.

Who may use them is the catalog's business (``policy.catalog``): the barrier refuses them to
anyone but the owner, and the model of anyone else is not shown them.
"""

import uuid
from typing import Final

from sqlalchemy import select

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.audit.log import append_audit
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.rights import Right, load_rights, paused_rights
from domo_core.policy.roles import Actor
from domo_core.policy.settings import (
    KEYS_SETTINGS,
    LIFETIME_KEY,
    TIME_ZONE_KEY,
    TOOLS_BASE_KEY,
    WRITER_SECONDS_KEY,
    InvalidSetting,
    SettingValue,
    load_policy_settings,
    update_policy_setting,
    validate_setting,
)
from domo_core.texts.view import Texts
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

_UPDATE_ARGUMENTS: Final = frozenset({"key", "value"})


def _setting_from(arguments: JsonObject) -> tuple[str, SettingValue | None]:
    """The ``key`` and the ``value`` of a ``policy_update`` call. ``InvalidArguments`` unless
    they are the only two arguments and the settings accept them (``validate_setting``)."""
    if frozenset(arguments) != _UPDATE_ARGUMENTS:
        raise InvalidArguments("invalid", "the arguments are key and value")
    key: object = arguments["key"]
    value: object = arguments["value"]
    if not isinstance(key, str):
        raise InvalidArguments("invalid", "the key is a text")
    if key in KEYS_SETTINGS:
        # A right of its own, which the owner may give: one way to change them.
        raise InvalidArguments("invalid", "the rules of the keys are changed with keys_house")
    if value is not None and not isinstance(value, str | int):
        raise InvalidArguments("invalid", "the value is a text, a whole number or null")
    try:
        validate_setting(key, value)
    except InvalidSetting as refused:
        # The settings' own words: they never repeat the key or the value.
        raise InvalidArguments("invalid", str(refused)) from None
    return key, value


class PolicyShow:
    """Tell what the owner's rules are now."""

    definition = ToolDefinition(
        name="policy_show",
        description=(
            "See their rules, as they are now: the rights they gave, each with whom it is for "
            "(a person, a role, you yourself on your own, or everyone), its action, what you "
            "must ask first or never ask (ask, with their conditions in ask_when) and its "
            "summary, in force and the paused ones (someone left the household, or came back: "
            "they may give a paused right back to someone who is here, and may take any away; a "
            "right of someone who left is marked left_the_household, and cannot be given back "
            "until they return); the settings, each with its value and where it comes from "
            '("default", or "owner" for their choice); and the household\'s time zone.'
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "policy.show"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        if arguments:
            raise InvalidArguments("invalid", "this tool takes no arguments")
        async with ctx.session_factory() as session:
            settings = await load_policy_settings(session)
            in_force = (await load_rights(session)).rights
            paused = await paused_rights(session)
            people = {r.person_id for r in (*in_force, *paused) if r.person_id is not None}
            found = await session.execute(select(Person).where(Person.id.in_(people)))
            persons = {person.id: person for person in found.scalars()}
        now = ctx.clock()
        names = {person_id: person.display_name for person_id, person in persons.items()}
        left = {person_id for person_id, person in persons.items() if not is_active(person, now)}
        time_zone = settings.time_zone
        return ToolOutcome(
            status="done",
            model_result={
                "settings": settings.rows(),
                "time_zone": None if time_zone is None else time_zone.key,
                "rights": [
                    *(_shown(right, names, left, ctx.texts, paused=False) for right in in_force),
                    *(_shown(right, names, left, ctx.texts, paused=True) for right in paused),
                ],
            },
            card={"key": "action.card.done.policy.show", "params": {}},
        )


def _shown(
    right: Right,
    names: dict[uuid.UUID, str],
    left: set[uuid.UUID],
    texts: Texts,
    *,
    paused: bool,
) -> JsonObject:
    """A right as ``policy_show`` tells it: for whom (a person's name, a role, Domo himself
    under his own heading, ``rights.subject.domo``, or ``everyone`` for a rule for everyone:
    never the one for the other), its action type, what it says to ask (``ask``: ``yes``,
    ``explicit_yes``, ``proof``, or the owner's "never ask me", ``judge`` with his conditions in
    ``ask_when``, or ``never``), its summary, and whether it is paused; and, only when the person
    it is for left the household, ``left_the_household`` (such a right can only be taken away
    until they return)."""
    if right.for_domo:
        subject = texts("rights.subject.domo")
    elif right.person_id is not None:
        subject = names.get(right.person_id)
    else:
        subject = "everyone" if right.role is None else right.role
    shown: JsonObject = {
        "for": subject,
        "action": right.action_type,
        "ask": right.ask,
        "ask_when": right.ask_when,
        "summary": right.summary,
        "paused": paused,
    }
    if right.person_id is not None and right.person_id in left:
        shown["left_the_household"] = True
    return shown


class PolicyUpdate:
    """Change one of the owner's rules, or clear it."""

    definition = ToolDefinition(
        name="policy_update",
        description=(
            "Record their choice: how long a request may wait, the rights writer's time, "
            "the household's time zone, how big your tools' own base may grow. A null value "
            "clears it. Whether an action is confirmed, "
            'and how, is not a setting: your judge decides it, and their rules "always ask me '
            'before…" are rights (rights_propose). Keys: '
            f"{LIFETIME_KEY} (how long a request may wait for a yes before it expires, a whole "
            f"number of hours from 1 to 720); {WRITER_SECONDS_KEY} (how long each call of the "
            "rights writer may take, a whole number of seconds from 30 to 240); "
            f"{TIME_ZONE_KEY} (the household's time zone, its name such as Europe/Paris: a limit "
            "that speaks of the hour or the day needs it, and it is never guessed); "
            f"{TOOLS_BASE_KEY} (how big the base your tools keep their data in may grow, a whole "
            "number of MB from 16 to 1048576, 1024 unless they choose: beyond it a write of a "
            "tool is refused)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The setting to change."},
                "value": {
                    "type": ["string", "integer", "null"],
                    "description": (
                        "A number of hours for the lifetime, a number of seconds for the writer, "
                        "the name of a time zone, a number of MB for the tools' base, or null to "
                        "clear their choice."
                    ),
                },
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    )
    action_type = "policy.update"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        key, _ = _setting_from(arguments)
        # The key is one the settings accept (a name the code defines): it may be audited. The
        # value is not in the action.
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=key,
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        key, value = _setting_from(arguments)
        async with ctx.session_factory() as session:
            try:
                old, new = await update_policy_setting(
                    session, key=key, value=value, by=ctx.person.id, now=ctx.clock()
                )
            except InvalidSetting as refused:
                raise InvalidArguments("invalid", str(refused)) from None
            # Last, just before the commit.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="policy.updated",
                details={"key": key, "old": old, "new": new},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"key": key, "value": new},
            card={"key": "action.card.done.policy.update", "params": {}},
        )
