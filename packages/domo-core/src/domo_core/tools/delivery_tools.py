"""``delivery_look``: Domo looks at what really reached someone of his replies (spec 6.10).

Without ``person``, it is about the person who speaks: his last ``RECEIPTS`` replies to them that
are over, the latest first, each with the first ``EXTRACT_LENGTH`` characters of his own words,
how it ended (``complete``, ``partial``, ``error``), whether it said nothing (``empty``), why it
failed when the code knows (``messages.failure``), and where, how and when it reached them (none:
nobody saw it). So "I saw nothing" can be understood: the page was closed, the reply failed, it
was cut.

With ``person``, about someone else of the household ("Sam says she saw nothing"): only the fact,
for the last ``REACHED_DAYS`` days. When each reply was said, whether it reached them, on which
channels, whether whole: never a word of it, nor of their messages. The name is given that
person's whole name before the call is described (``tools.context.NamesPeople``); naming the person
who speaks is the same as naming nobody.

It is everyone's, a guest's too, changes nothing, and is never judged nor shown
(``policy.catalog.SILENT_ACTIONS``): the model reads its result in the turn. What reached someone
else still tells when they were there: the description asks Domo to judge whether the person
asking should know it, and the audit log keeps that he looked (``delivery.looked``: who was
speaking, about whom, how many lines; never a word).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select

from domo_core.audit.log import append_audit
from domo_core.deliveries.service import (
    EXTRACT_LENGTH,
    ReachLine,
    ReplyReceipt,
    reached,
    received_of,
)
from domo_core.devices.models import Device
from domo_core.household.service import resolve_person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

# How many of his last replies to the person who speaks Domo is given.
RECEIPTS: Final = 10
# How many days back what reached someone else is told.
REACHED_DAYS: Final = 7
# What a call leaves under the reply: a card nobody reads (``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}


def _person_named(arguments: JsonObject) -> str | None:
    """The name of ``{"person": ...}``, or ``None`` for ``{}``: ``InvalidArguments`` for anything
    else."""
    if not set(arguments) <= {"person"}:
        raise InvalidArguments("invalid", 'the only argument is "person"')
    value = arguments.get("person")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    return value


def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _receipt(receipt: ReplyReceipt, devices: dict[uuid.UUID, str]) -> JsonObject:
    return {
        "said_at": _when(receipt.said_at),
        "extract": receipt.extract,
        "status": receipt.status,
        "empty": receipt.empty,
        "failure": receipt.failure,
        "reached": [
            {
                "channel": delivery.channel,
                "device": None if delivery.device_id is None else devices.get(delivery.device_id),
                "how": delivery.how,
                "extent": delivery.extent,
                "at": _when(delivery.at),
            }
            for delivery in receipt.deliveries
        ],
    }


def _line(line: ReachLine) -> JsonObject:
    return {
        "said_at": _when(line.said_at),
        "reached": bool(line.channels),
        "channels": list(line.channels),
        "whole": line.whole,
    }


class DeliveryLook:
    """Look at what reached the person who speaks, or the fact of what reached someone else."""

    definition = ToolDefinition(
        name="delivery_look",
        description=(
            "Look at what really reached someone of your replies: when they say they saw nothing, "
            "got no answer, or when you wonder whether they read what you said. Without person: "
            "your last replies to the person you are talking with, the latest first, each with "
            f"its first {EXTRACT_LENGTH} characters, how it ended (complete, partial, error, or "
            "empty), why it failed when that is known, and where, how (live as you said it, or "
            "fetched later) and when it reached them, whole or cut because they left; none means "
            "it reached nobody. With person, someone else of the household: only whether your "
            f"replies to them in the last {REACHED_DAYS} days reached them, when, on which "
            "channels and whether whole, never what they said. That still tells when someone "
            "else was there: judge whether the person asking should know it. Nobody is shown "
            "that you look."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Someone else of the household, by name.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    action_type = "delivery.look"
    # The argument that names someone of the household: given its whole name before the call is
    # described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _person_named(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = _person_named(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            other = (
                None
                if name is None
                else await resolve_person(session, name, now=now, speaker=ctx.person.id)
            )
            if other is not None and other.id != ctx.person.id:
                lines = await reached(session, other.id, now - timedelta(days=REACHED_DAYS))
                result: JsonObject = {
                    "person": other.display_name,
                    "days": REACHED_DAYS,
                    "replies": [_line(line) for line in lines],
                }
                # He looked at when someone else was there: who was speaking, about whom, how many
                # lines. Never what was said.
                await append_audit(
                    session,
                    actor=f"person:{ctx.person.id}",
                    action="delivery.looked",
                    details={"about": str(other.id), "count": len(lines)},
                )
                await session.commit()
                return ToolOutcome(status="done", model_result=result, card=_CARD)
            receipts = await received_of(session, ctx.person.id, limit=RECEIPTS)
            ids = {d.device_id for r in receipts for d in r.deliveries if d.device_id is not None}
            devices: dict[uuid.UUID, str] = {}
            if ids:
                rows = await session.execute(
                    select(Device.id, Device.name).where(
                        Device.id.in_(ids), Device.person_id == ctx.person.id
                    )
                )
                devices = {device_id: device_name for device_id, device_name in rows.all()}
        result = {"replies": [_receipt(receipt, devices) for receipt in receipts]}
        return ToolOutcome(status="done", model_result=result, card=_CARD)
