"""The assistant sends an invitation's link through one of his own tools, without the link ever
passing under a model's or a program's eyes (design 6.5): ``invitation_send``.

The call names a record with an open invitation that gives it an account (kind ``record``) or
brings it back (kind ``return``), one of his tools, the operation of it that sends, and the
arguments of the launch, where the text ``{link}`` stands for the link. The code makes the
invitation's token (a new one: an older link stops working once the new one is sent), seals the
link (``people.sealed``), puts the marker where ``{link}`` was, and launches the tool for the
house. The marker opens only in the body of one request out of that tool under that operation, as
the gatekeeper is called; the program reads only that request's status, and what else comes back
is scrubbed of the link and its token (``madetools.means``).

When the launch is done and the address took the link (a 2xx answer), the result is
``{"sent": true}`` and the act ``link_sent`` is traced (``people.acts``). Otherwise the new token
is unmade and the invitation's link is as it was before the call (an older link works again; one
that waited behind the "show the link" button waits there still), and the result is
``{"sent": false, "why": <code>}``: the launch's status, ``needs_yes`` when it stopped for a yes
(no stop is kept: a yes could only ever send the marker), ``not_placed`` when the launch was done
but the link went out in no request a 2xx answered, ``no_such_tool`` when he has no tool of
that name, ``no_address`` when the call has no address to start the link with (no request, and
no site address configured: ``config.site_origin``; nothing is made), ``unreachable`` when no
tool can run now, ``owner_directive`` when the owner withdrew or refused the record (even on the
owner's own word, which the barrier lets through). The result, the card, the stored arguments and
the audit lines never hold the link, the token nor the marker's value.

Its type (``household.send_link``) gives a way in (``policy.catalog.GIVES_ACCESS``): the owner's
withdrawal or refusal of the record is applied before any judgement, so nothing is launched
(``owner_directive``). Whose act it is follows ``people.whose``: a call carries ``reason`` exactly
when it is the assistant's own.
"""

import secrets
from datetime import datetime
from typing import Final, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.household.models import Invitation
from domo_core.household.service import hash_token, open_at
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.madetools.launch import HOUSE, LaunchDeps, Launched, launch
from domo_core.madetools.service import tool_named
from domo_core.people import directives, whose
from domo_core.people.acts import note_act
from domo_core.people.directives import under_directive
from domo_core.people.sealed import SealedLinks
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.household_admin import has_an_account, person_named, record_locked
from domo_core.tools.people_tools import record_named

# The text of the launch's arguments that stands for the link.
LINK_PLACE: Final = "{link}"
_ARGUMENTS: Final = frozenset(
    {"person", "tool", "operation", "arguments", "reason", "exception_of"}
)
_NEEDED: Final = frozenset({"person", "tool", "operation", "arguments"})
_DONE: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_FAILED: Final[JsonObject] = {"key": "action.card.failed", "params": {}}
_NO_INVITATION: Final = (
    "no invitation that waits gives this person an account or brings them back: invite them, or "
    "bring them back, first"
)


def _read(arguments: JsonObject) -> tuple[str, str, str, JsonObject]:
    """The record named, the tool, its operation and the launch's arguments; ``InvalidArguments``
    for anything else."""
    keys = frozenset(arguments)
    if not keys >= _NEEDED or not keys <= _ARGUMENTS:
        raise InvalidArguments(
            "invalid", "the arguments are person, tool, operation, arguments and, if needed, reason"
        )
    texts: list[str] = []
    for name in ("person", "tool", "operation"):
        value = arguments[name]
        if not isinstance(value, str) or not value.strip():
            raise InvalidArguments("invalid", f"{name} is a text")
        texts.append(value.strip())
    given = arguments["arguments"]
    if not isinstance(given, dict):
        raise InvalidArguments("invalid", "arguments is an object: the arguments of the launch")
    return texts[0], texts[1], texts[2], cast(JsonObject, given)


def _placed(value: JsonValue, marker: str) -> JsonValue:
    """``value`` with every ``{link}`` of its texts replaced by ``marker``, at any depth."""
    if isinstance(value, str):
        return value.replace(LINK_PLACE, marker)
    if isinstance(value, list):
        return [_placed(item, marker) for item in value]
    if isinstance(value, dict):
        return {key: _placed(item, marker) for key, item in value.items()}
    return value


async def _waiting(
    session: AsyncSession, record: Person, now: datetime, *, lock: bool = False
) -> Invitation:
    """The open invitation that gives ``record`` an account or brings it back; ``not_found`` when
    there is none, or when it gives an account to a record that has one already (a record merged
    or forgotten is never named: ``record_named``). The owner's withdrawal or refusal is not read
    here: the barrier applies it before any judgement, and ``execute`` before any launch."""
    query = (
        select(Invitation)
        .where(
            Invitation.person_id == record.id,
            Invitation.kind.in_(("record", "return")),
            open_at(now),
        )
        .order_by(Invitation.created_at.desc(), Invitation.id)
        .limit(1)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    invitation = (await session.execute(query)).scalar_one_or_none()
    if invitation is None or (
        invitation.kind == "record" and await has_an_account(session, record)
    ):
        raise InvalidArguments("not_found", _NO_INVITATION)
    return invitation


def _origin_of(ctx: ToolContext, deps: LaunchDeps) -> str | None:
    """The address the link starts with: the one the person reached the assistant at, else (a
    review, a turn he started: no request) the house's configured address
    (``LaunchDeps.house_origin``); ``None`` when there is neither."""
    origin = (ctx.origin or deps.house_origin or "").rstrip("/")
    return origin or None


def _not_sent(why: str) -> ToolOutcome:
    return ToolOutcome(status="failed", model_result={"sent": False, "why": why}, card=_FAILED)


class InvitationSend:
    """Send a record's invitation link through one of the assistant's own tools."""

    definition = ToolDefinition(
        name="invitation_send",
        description=(
            "Send the link of an invitation that waits (one that gives someone you know an "
            "account, or brings them back) through one of your own tools that writes to them: "
            "a message to an address or a number you know or were given. Name the person, the "
            "tool, the operation of it that sends, and the arguments of its launch, writing "
            "{link} where the link goes (in the text of the message, say): you never see the "
            "link, and neither does your tool; it is put into what the tool sends, and a new link "
            "is made (an older one stops working once it is sent). The result says sent true, or "
            "sent false with why, and then the link is as it was. The tool reads only whether "
            "its message was taken, never what the address answered to it. Without a tool of "
            "yours to send with, do not call this: the link is given to the person who asked, as "
            "when inviting. " + whose.HIS_OWN_IDEA
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name, or person:<id>."},
                "tool": {"type": "string", "description": "The name of your tool that sends."},
                "operation": {
                    "type": "string",
                    "description": "The operation of that tool that sends the message.",
                },
                "arguments": {
                    "type": "object",
                    "description": "The arguments of the launch; write {link} where the link goes.",
                },
                "reason": whose.REASON_PROPERTY,
                "exception_of": whose.EXCEPTION_PROPERTY,
            },
            "required": ["person", "tool", "operation", "arguments"],
            "additionalProperties": False,
        },
    )
    action_type = "household.send_link"
    person_arguments: Final = ("person",)
    names_among: Final = "records"

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names, found by the name alone; nobody when the arguments are not
        accepted."""
        try:
            name, _, _, _ = _read(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx, "records")

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        whose.check(arguments, actor)
        name, _, _, _ = _read(arguments)
        async with ctx.session_factory() as session:
            # Its invitation is looked for when it runs: the owner's withdrawal or refusal of the
            # record, which ends its invitations, is the barrier's to refuse first.
            record = await record_named(session, name, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{record.id}",
            effect="permissions",
            destination="household",
            facts={
                "exception_of": whose.exception_of(arguments, actor),
                # The record the link lets in is the person whose turn it is: saying who one is
                # gives nothing by itself, and the judge is shown it.
                "serves_the_speaker": record.id == ctx.person.id,
            },
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, tool_name, operation, given = _read(arguments)
        by = whose.by(arguments, ctx)
        now = ctx.clock()
        deps = ctx.launch_deps
        async with ctx.session_factory() as session:
            record = await record_locked(session, name, ctx)
            if await under_directive(session, record.id, now) is not None:
                # The owner's own word passes the barrier, yet a link to someone the owner
                # withdrew or refused opens nothing: nothing is made nor sent.
                return _not_sent("owner_directive")
            invitation = await _waiting(session, record, now, lock=True)
            tool = await tool_named(session, tool_name)
            if deps is None or tool is None:
                return _not_sent("unreachable" if deps is None else "no_such_tool")
            origin = _origin_of(ctx, deps)
            if origin is None:
                # A link without an address is never sent: nothing is made.
                return _not_sent("no_address")
            # Made as ``household.service.reveal_link`` makes it: stored as its SHA-256 alone.
            token = secrets.token_urlsafe(32)
            made, before = hash_token(token), invitation.token_hash
            # Committed before the launch, which the address needs to reach a working link: a
            # process killed while the tool runs leaves this token working (and the older link
            # dead), whether or not the link went out; giving the link again ends it.
            invitation.token_hash = made
            invitation_id, record_id, tool_id = invitation.id, record.id, tool.id
            await session.commit()
        sealed = SealedLinks(tool_name, operation)
        marker = sealed.seal(f"{origin}/invite/{token}", scrub_too=(token,))
        placed = _placed(given, marker)
        assert isinstance(placed, dict)
        try:
            came = await launch(
                deps, tool_id, arguments=placed, for_whom=HOUSE, cause="turn", sealed=sealed
            )
        except BaseException:
            # Nothing is left that works when the launch did not come to an end.
            async with ctx.session_factory() as session:
                await _unmade(session, invitation_id, made, before)
                await session.commit()
            raise
        sent = came.status == "done" and sealed.used > 0
        async with ctx.session_factory() as session:
            if not sent:
                await _unmade(session, invitation_id, made, before)
                await session.commit()
                return _not_sent(_why(came))
            await note_act(
                session,
                act="link_sent",
                by=by,
                reason=whose.reason_of(arguments),
                turn_of=whose.turn_of(arguments, ctx),
                person_id=record_id,
                # The link dates from now (``household.service.nobody_new_since``).
                invitation_id=invitation_id,
                now=now,
            )
            await directives.note_exceptions(
                session,
                whose.exception_of(arguments, ctx.acting),
                person_id=record_id,
                invitation_id=invitation_id,
                turn_of=whose.turn_of(arguments, ctx),
                now=now,
            )
            await session.commit()
        return ToolOutcome(status="done", model_result={"sent": True}, card=_DONE)


def _why(came: Launched) -> str:
    """Why a launch did not send the link: its status, ``needs_yes`` when it stopped for one, or
    ``not_placed`` when it was done without the link going out."""
    if came.status == "done":
        return "not_placed"
    result = came.result
    if (
        came.status == "stopped"
        and isinstance(result, dict)
        and result.get("reason") == "needs_yes"
    ):
        return "needs_yes"
    return came.status


async def _unmade(
    session: AsyncSession, invitation_id: UUID, made: str, before: str | None
) -> None:
    """The token made for this send unmade, unless another was made since: the invitation's link
    is what it was before (``before``: the older token's hash, or none). Flushed."""
    locked = await session.execute(
        select(Invitation)
        .where(Invitation.id == invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    invitation = locked.scalar_one_or_none()
    if invitation is not None and invitation.token_hash == made:
        invitation.token_hash = before
        await session.flush()
