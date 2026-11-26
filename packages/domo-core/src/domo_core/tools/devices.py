"""``device_entrust``, ``device_list`` and ``device_remove``: a person entrusts a device to Domo,
sees theirs and takes one back, by talking (spec 6.5).

``device_entrust`` makes the code a person gives to the program on their own computer (twelve
characters in three groups, valid ``PAIRING_FOR`` and served once: ``devices.service``). It is
shown once, as an invitation's link is (``tools.household_admin``): in the answer to the request
that made it (``ToolOutcome.card_once``), never in the stored card, the model's result, the
history, the audit log or a log; the model reads that a code was shown (``"code": "shown_once"``),
never the code. When nobody could be shown it now (``ToolContext.leave_on_their_card``: someone
else's yes, a turn no page reads, a step of a chain), no code is made at all and the model is told
to say it again when the person is there (``"code": "ask_again_when_they_are_there"``), so that a
code never sits in a place it could be read from. The tool takes no argument: the device is named
by the person when they pair it.

``device_list`` gives the devices of the person who speaks, with their names and dates, and never
a secret (the database keeps none). It changes nothing.

``device_remove`` takes one of those devices back, so that its secret no longer works. The device
is named by its name, as the person said it when pairing; the owner adds the name of the person
whose device it is (``person``) and may take back anyone's. Nobody else may touch a device that
is not theirs: that is the tool's own refusal (``fixed_refusal``), whatever the rights say. Two
devices of one person with the same name are ``ambiguous``, and nothing is guessed; a request
that waits for a yes is about the device it named then (``described_target``). The audit entries
(``device.entrusted``, ``device.paired``, ``device.removed``, ``devices.service``) hold
identifiers only: never a code, a secret or a device's name.

Who may use them is the catalog's business (``policy.catalog``): the owner and the adults may
entrust a device, a child or a guest only by a right the owner gives; everyone lists their own
and takes their own back.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.devices.models import Device
from domo_core.devices.service import (
    MAX_DEVICE_NAME,
    PAIRING_FOR,
    begin_pairing,
    devices_of,
    remove_device,
)
from domo_core.household.service import resolve_person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.roles import Actor, actor_label
from domo_core.text import name_key
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.household_admin import card_date, described_target, name_from

_MINUTES: Final = int(PAIRING_FOR.total_seconds() // 60)
_REMOVE_ARGUMENTS: Final = frozenset({"device", "person"})
_NOT_YOURS: Final = "only the owner takes back the device of someone else"
_NO_DEVICE: Final = "this person has no device with this name"
_SEVERAL_DEVICES: Final = "this person has several devices with this name: ask which one is meant"


def _no_arguments(arguments: JsonObject) -> None:
    if arguments:
        raise InvalidArguments("invalid", "this tool takes no argument")


class DeviceEntrust:
    """Make the code with which the person entrusts a device to Domo (shown once)."""

    definition = ToolDefinition(
        name="device_entrust",
        description=(
            "Entrust a device to the assistant, when that is what the person you are talking with "
            "wants "
            "(a small program on their own computer, which then speaks with you as them). This "
            "makes a short code, valid for "
            f"{_MINUTES} minutes and usable once, that the person "
            "gives to the program on the device. The code is shown to the person only once, to "
            "them alone: you never see it, so never say it and never invent one. The result says "
            "the code was shown; if it says to ask again when they are there, nothing was made: "
            "tell the person, and make it again when they ask. The device is named when it is "
            "paired, not here."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "device.entrust"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _no_arguments(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="permissions",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _no_arguments(arguments)
        if ctx.leave_on_their_card:
            # Nobody would see the code: none is made, the person asks again when they are there.
            return ToolOutcome(
                status="done",
                model_result={"code": "ask_again_when_they_are_there"},
                card={"key": "action.card.done.device.entrust.later", "params": {}},
            )
        async with ctx.session_factory() as session:
            code = await begin_pairing(session, ctx.person.id, ctx.clock())
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"code": "shown_once"},
            card={"key": "action.card.done.device.entrust", "params": {}},
            # Shown once, to the one who asked, in the answer to this request: never stored,
            # never told to the model.
            card_once={
                "key": "action.card.once.device.entrust",
                "params": {"code": code, "minutes": str(_MINUTES)},
            },
        )


class DeviceList:
    """The devices the person who speaks entrusted to Domo."""

    definition = ToolDefinition(
        name="device_list",
        description=(
            "List the devices the person you are talking with entrusted to the assistant: for "
            "each, its "
            "name, when it was entrusted (added) and when it last spoke (last_seen, empty if "
            "never). Use it when they want to see their devices, or to find the name of one to "
            "take back."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "device.list"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _no_arguments(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _no_arguments(arguments)
        async with ctx.session_factory() as session:
            devices = await devices_of(session, ctx.person.id)
        listed: list[JsonObject] = [
            {
                "name": device.name,
                "added": card_date(device.created_at),
                "last_seen": None
                if device.last_seen_at is None
                else card_date(device.last_seen_at),
            }
            for device in devices
        ]
        return ToolOutcome(
            status="done",
            model_result={"devices": listed},
            card={"key": "action.card.done.device.list", "params": {}},
        )


class DeviceRemove:
    """Take back a device: its secret no longer works."""

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): a device that is not the one's who
        asks is the owner's to take back, nobody else's; and the assistant's, as an act of his
        own (``Actor.subject`` ``domo``), which the barrier and his judge then weigh: a doubt
        waits for the person it belongs to."""
        if facts.get("is_self") is True or actor.role == "owner" or actor.subject == "domo":
            return None
        return _NOT_YOURS

    definition = ToolDefinition(
        name="device_remove",
        description=(
            "Take back a device the person you are talking with entrusted to the assistant, when "
            "that is "
            "what they want: it can no longer speak with you. Name the device by the name they "
            "gave it (find it with device_list if you do not know it). The device of someone "
            "else is for the owner only to take back, giving that person's name in person; "
            "everyone else takes back their own. If two of their devices have the same name, ask "
            "which one is meant."
        ),
        parameters={
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "The name of the device.",
                    "minLength": 1,
                    "maxLength": MAX_DEVICE_NAME,
                },
                "person": {
                    "type": "string",
                    "description": "The owner only: the name of the person whose device it is.",
                    "minLength": 1,
                },
            },
            "required": ["device"],
            "additionalProperties": False,
        },
    )
    action_type = "device.remove"
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    @staticmethod
    def _arguments(arguments: JsonObject) -> tuple[str, str | None]:
        if not {"device"} <= frozenset(arguments) <= _REMOVE_ARGUMENTS:
            raise InvalidArguments("invalid", "the arguments are device and, for the owner, person")
        device = name_from(arguments["device"], "device")
        if len(device.strip()) > MAX_DEVICE_NAME:
            raise InvalidArguments(
                "too_long", f"a device name is {MAX_DEVICE_NAME} characters at most"
            )
        person = arguments.get("person")
        return device, None if person is None else name_from(person, "person")

    async def _device_of_the_call(
        self,
        session: AsyncSession,
        arguments: JsonObject,
        actor: Actor,
        ctx: ToolContext,
        now: datetime,
    ) -> tuple[Device, Person]:
        """The device the call names and whose it is, once ``fixed_refusal`` lets it through."""
        name, whose = self._arguments(arguments)
        owner_of_it = (
            ctx.person
            if whose is None
            else await resolve_person(session, whose, now=now, speaker=ctx.person.id)
        )
        facts: dict[str, FactValue] = {
            "person": f"person:{owner_of_it.id}",
            "is_self": owner_of_it.id == actor.person_id,
        }
        refused = self.fixed_refusal(actor, facts, {})
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        key = name_key(name)
        matches = [d for d in await devices_of(session, owner_of_it.id) if name_key(d.name) == key]
        if not matches:
            raise InvalidArguments("not_found", _NO_DEVICE)
        if len(matches) > 1:
            raise InvalidArguments("ambiguous", _SEVERAL_DEVICES)
        return matches[0], owner_of_it

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        async with ctx.session_factory() as session:
            device, whose = await self._device_of_the_call(
                session, arguments, actor, ctx, ctx.clock()
            )
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"device:{device.id}",
            effect="permissions",
            destination="household",
            facts={"person": f"person:{whose.id}", "is_self": whose.id == actor.person_id},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        now = ctx.clock()
        # Who it was judged as: the assistant for an act of his own, else the person who asks.
        acting = ctx.judged_as or ctx.actor
        async with ctx.session_factory() as session:
            device, _ = await self._device_of_the_call(session, arguments, acting, ctx, now)
            described_target(ctx, f"device:{device.id}")
            name = device.name
            device_id: uuid.UUID = device.id
            if not await remove_device(session, device_id, now, actor=actor_label(acting)):
                raise InvalidArguments("not_found", _NO_DEVICE)
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"removed": name},
            card={"key": "action.card.done.device.remove", "params": {"name": name}},
        )
