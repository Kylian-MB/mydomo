"""``address_open``, ``address_close``, ``addresses_look``, ``account_ask``, ``account_remove``,
``account_share`` and ``accounts_look``: Domo opens and closes the addresses his programs reach,
looks at them, asks someone for an account, and the accounts are removed, shared and looked at
(step 2, spec 13.3 to 13.5, 13.10; the rules are ``outward.service``'s).

**Addresses** are Domo's own acts, like his acts on his tools (``tools.context.ActsAsDomo``):
judged with his own rights in anyone's turn, a guest's too, and on his own. Opening one is shown
and judged (a rule of the owner may ask him first, approve or limit it: ruling R8); closing one and
looking are silent. An address is opened for one tool (``tool``, by name: it outlives the tool's
versions, and the tool need not be built yet) or for the programs run by hand (no ``tool``); a tool
closed to the person of the turn has none opened or closed in their conversation (``not_possible``).
A port of the machine that hosts Domo (``on_host``) is a right apart (``net.open_host``): with
it in Domo's own rights, the call is judged as ``net.open_host``; without it, it is judged as an
opening (``net.open``) that takes the owner's yes: in the owner's own turn the yes-judge weighs his
words (ruling R23: "do" opens it at once, "ask" shows him a request), and in anyone else's turn or
on his own it is Domo's request waiting for the owner's yes (``AsksItsYes``, ruling R24), never
opened without it. What the owner closed is closed for every tool and for hand runs (ruling R20),
and opening it again is Domo's judgement (ruling R24): judged like any other opening, its facts
saying that the owner closed it and when (``closed_by_owner``, ``closed_on``), so that the judge
weighs whether things changed; in the owner's turn, "ask" shows him a request, in anyone else's or
on his own, it waits for the owner's yes as Domo's request (``AsksWhomItsYes``). The owner makes
a closing absolute with a rule of his on ``net.open`` (a deny, or "ask me first"), which the
barrier applies first. Neither kind is ever a step of a chain (``NeverAStep``, ruling R19), and
either opens on a yes only when it is the owner's own on that very request.

**Accounts** are the person's to give (spec 5.8: "who gives an account opens the form
themselves"): ``account_ask`` is judged as the person of the turn (a child asks for an account of
their own, a guest for none; a house account is given by the owner or an adult), and as Domo on
his own. Its account is described for good when it is asked (its name, whose, its hosts and its
place: ``outward.service.ask_account``), the form shows its name, its hosts and where it is placed
(a text Domo writes, ``chat.account.purpose.<kind>``, with the giver's form texts,
``llm.provider_keys.KeyForm``), and the form's request is made from the row, never from what a model
says afterwards. The form's link is shown once, to the giver, in the answer that ran the call
(``ToolOutcome.card_once``): never stored, never told to the model. When no one may see it now
(someone else said yes, nobody reads the turn, a step of a chain, no address to reach Domo at),
the account waits, and the same call later opens its form. On his own, Domo names who gives it
(``giver``), the account waits, and a note of his tells them (``tools.notes``: his own word). An
account the owner removed is no longer refused by who asks (Task 27): the call is judged, its facts
saying that the owner removed it and when (``removed_by_owner``, ``removed_on``); a yes asked
before a house account is asked again is the owner's, as Domo's request (``AsksWhomItsYes``); the
owner's deny on ``account.ask`` holds first. ``account_remove`` is whoever gave the account's, or
the owner's, and Domo's on his own for one that no longer serves (the owner's decision D8), judged
like his other acts of his own; ``account_share`` (ruling P2) opens a house account to guests or
closes it to Domo on his own, the owner's alone. ``accounts_look`` shows the accounts the person
of the turn has (theirs and the house's; a guest's, the house's only when open to guests; the
owner's, all), and on his own the house's.

Every act is audited, metadata only (``outward.address_opened``, ``outward.address_closed``,
``outward.account_asked``, ``outward.account_removed``, ``outward.account_shared``): the host, the
port, the tool, the account's name, the kind of its place, for whom; never a value, a path, a form's
token or its link. The barrier's decision is audited for every call (``policy.decision``).
"""

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, Literal, cast

from domo_common.gatekeeper_api import (
    BasicLocation,
    HeaderLocation,
    InjectionLocation,
    LoginLocation,
)
from domo_common.gatekeeper_api import OpenAddress as AddressToReach
from domo_common.text_rules import plain_problem
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import is_child, is_visiting
from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.confirmations.models import Confirmation
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.household.service import resolve_person
from domo_core.llm.provider_keys import key_form, vault_form_path
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.madetools.models import NAME_PATTERN
from domo_core.madetools.service import tool_named
from domo_core.memory.service import refuses_secret_shapes
from domo_core.notes.models import Note
from domo_core.outward.literal import literal_zone, open_risks
from domo_core.outward.models import ACCOUNT_NAME_PATTERN, ServiceAccount
from domo_core.outward.service import (
    PLACE,
    AccountExists,
    RemovedByOwner,
    ask_account,
    closed_by_owner_now,
    closed_by_the_owner,
    given_refs,
    is_the_owner,
    kept_hosts,
    live_account,
    live_addresses,
    open_address,
    owner_s_closing,
    owner_s_removal,
    place_of,
    remove_account,
    who_of,
)
from domo_core.outward.service import close_address as close_the_address
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.asks import Asked
from domo_core.policy.catalog import MADE_PREFIX, NET_OPEN_HOST
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import Approver, load_rights
from domo_core.policy.roles import Actor, actor_label, domo_actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome, WaitsForAYes
from domo_core.tools.notes import MAX_NOTE_LENGTH, note_text_from

# What a silent call leaves under the reply: a card nobody reads.
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
_FAILED_CARD: Final[JsonObject] = {"key": "action.card.failed", "params": {}}

# The longest host a call names (a fact of text holds 200 characters at most).
MAX_HOST: Final = 200
# How many hosts an account is bound to at most.
MAX_HOSTS: Final = 10
# The longest header name, header template, login path and field name a place may say.
MAX_HEADER_NAME: Final = 64
MAX_TEMPLATE: Final = 100
MAX_LOGIN_PATH: Final = 80
MAX_FIELD: Final = 64
# Headers an account is never put in: what frames a request or travels hop by hop.
_RESERVED_HEADERS: Final = frozenset(
    {
        "host",
        "via",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
        "proxy-connection",
    }
)

_NAME: Final = re.compile(NAME_PATTERN)
_ACCOUNT_NAME: Final = re.compile(ACCOUNT_NAME_PATTERN)

_CLOSED_TOOL: Final = (
    "this tool is not open to the person you are talking with: no address is opened or closed for "
    "it in their conversation. Nothing was done"
)
_HOST_NEEDS_THE_OWNER: Final = (
    "a port of the machine that hosts you is opened with the owner's yes, unless he gives you that "
    "right: ask it with this same call, which waits for it. Nothing was done"
)
_NO_OWNER_YES: Final = (
    "this address opens only with the owner's yes, and he gave none. Nothing was done"
)
_EXISTS: Final = (
    "exists: an account of this name is there already for whom it would be (given, or asked of "
    "someone else or with other hosts or another place): nothing replaces it. Use it, or give it "
    "another name. Nothing was done"
)
_REMOVED_SINCE: Final = (
    "removed_by_owner: the owner removed this account since it was asked: call account_ask "
    "again, and it is weighed with that removal. Nothing was done"
)
_HOUSE_ACCOUNT: Final = (
    "a house account is given by the owner or an adult: ask one of them, or ask for an account of "
    'the person\'s own (for: "me"). Nothing was done'
)
_NO_GIVER: Final = (
    "you act on your own: say who gives the account (giver), someone of the household who is not "
    "a guest"
)
_GIVER_IS_THEM: Final = (
    "in someone's conversation the account is theirs to give: leave giver and note out (on your "
    "own, name who gives it and write them the note)"
)
_NO_NOTE: Final = (
    "you act on your own: write the note that tells the giver (note), in your words and in their "
    "language"
)
_NOT_A_STEP: Final = (
    "an opening of an address the owner closed, or of a port of the machine that hosts you without "
    "that right, is never a step of a chain: call it on its own, before or after this chain, in "
    "the same answer"
)
_NOTE_SECRET: Final = "the note looks like it holds a secret: none goes in a note. Nothing was done"
_NOT_THEIR_OWN: Final = (
    "a child removes an account of their own only, never the house's nor someone else's. Nothing "
    "was done"
)
_GUEST_GIVES_NONE: Final = "a guest gives no account. Nothing was done"
_NO_ACCOUNT: Final = "no account of this name is there for whom you said"
_NOT_THEIRS: Final = "only whoever gave this account removes it, or the owner. Nothing was done"
_NOBODY_S_OWN: Final = (
    'on your own, name the account by whose it is: for "house", or "person" with of'
)
_NOTHING_TO_SHARE: Final = "say guests, domo, or both"
_GATEKEEPER: Final = "the secret vault did not answer as it should; try again in a moment"
# What the model is told when the form waits.
_FORM_WAITS: Final = (
    "the account is asked, but its form could not be shown to them now: when they want to give "
    "it, call account_ask again with the same arguments in their conversation"
)

For = Literal["me", "house"]


# --- the arguments --------------------------------------------------------------------------------


def _only(arguments: JsonObject, required: frozenset[str], allowed: frozenset[str]) -> None:
    if not required <= frozenset(arguments) <= allowed:
        raise InvalidArguments("invalid", f"the arguments are among {', '.join(sorted(allowed))}")


def _host(value: object) -> str:
    """A host as an address names it: a name or an IP literal, in ASCII, ``MAX_HOST`` at most."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_HOST:
        raise InvalidArguments("invalid", f"host is a name or an IP address, {MAX_HOST} at most")
    try:
        return AddressToReach(host=value, port=1).host
    except ValidationError:
        raise InvalidArguments(
            "invalid", "host is a name or an IP address, in ASCII, without a scheme or a port"
        ) from None


def _port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise InvalidArguments("invalid", "port is a whole number from 1 to 65535")
    return value


def _tool(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise InvalidArguments(
            "invalid", "tool is the name of a tool of yours: a to z, 0 to 9 and _, a letter first"
        )
    return value


def _boolean(value: object, what: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise InvalidArguments("invalid", f"{what} is true or false")
    return value


class _Address:
    """The arguments of a call about one address, checked."""

    def __init__(self, arguments: JsonObject, *, with_on_host: bool) -> None:
        allowed = frozenset({"host", "port", "tool", *(("on_host",) if with_on_host else ())})
        _only(arguments, frozenset({"host", "port"}), allowed)
        self.host = _host(arguments["host"])
        self.port = _port(arguments["port"])
        self.tool = _tool(arguments.get("tool"))
        self.on_host = bool(_boolean(arguments.get("on_host"), "on_host"))

    @property
    def target(self) -> str:
        return f"address:{self.host}:{self.port}"


async def _tool_open_to_them(session: AsyncSession, ctx: ToolContext, tool: str | None) -> None:
    """``not_possible`` when ``tool`` is a tool of Domo's closed to the person of the turn (to
    Domo himself, on his own): who may not launch it has no address opened or closed for it. A
    name that is no tool yet is open to all (the tool is built later)."""
    if tool is None or await tool_named(session, tool) is None:
        return
    policy = await load_policy(session)
    rights = await load_rights(session)
    if not rights.resolve(ctx.turn_subject, f"{MADE_PREFIX}{tool}", policy).allowed:
        raise InvalidArguments("not_possible", _CLOSED_TOOL)


def _by(ctx: ToolContext) -> str:
    """Who acts, as a row says it: Domo on his own, else the person of the turn."""
    return who_of(None if ctx.on_his_own else ctx.person)


async def _domo_opens_the_host(session: AsyncSession, ctx: ToolContext) -> bool:
    """Whether Domo's own rights give him ``net.open_host`` (a right of the owner's)."""
    rights = await load_rights(session)
    return rights.resolve(domo_actor(), NET_OPEN_HOST, ctx.policy).allowed


async def _the_owner_s_yes(session: AsyncSession, ctx: ToolContext) -> uuid.UUID | None:
    """The owner who said yes to the very request this call runs for (ruling R19): his button or
    his words on a request of ``address_open`` itself, now executing, decided by him (the owner of
    now). ``None`` for anything else: no request, one nobody decided, one the judge let through
    (``decided_via`` ``judge``), a chain's, or a yes of someone who is not the owner now."""
    approver, request_id = ctx.approved_by, ctx.request_id
    if approver is None or request_id is None:
        return None
    request = await session.get(Confirmation, request_id)
    if (
        request is None
        or request.tool_name != "address_open"
        or request.chain_basis is not None
        or request.status != "executing"
        or request.decided_by != approver
        or request.decided_via in (None, "judge")
    ):
        return None
    return approver if await is_the_owner(session, str(approver)) else None


async def _turn_of_the_request(session: AsyncSession, ctx: ToolContext) -> str:
    """Whose turn the request this call runs for came from, as the stored request says it (never
    the context of the yes that runs it): the person who asked; for a request of Domo's own
    (``by_domo``), the person whose turn it was made in, or Domo himself when it waits with
    the owner (a turn he acted in on his own: in a turn of the owner's, a request is never his)."""
    request = None if ctx.request_id is None else await session.get(Confirmation, ctx.request_id)
    if request is None:
        return actor_label(ctx.turn_subject)
    if request.by_domo and await is_the_owner(session, str(request.requester_id)):
        return actor_label(domo_actor())
    return f"person:{request.requester_id}"


def _day_of(moment: datetime | None) -> str | None:
    """``moment``'s day in UTC, as a fact of a date says it (``YYYY-MM-DD``)."""
    return None if moment is None else moment.astimezone(UTC).date().isoformat()


def _owners_turn(ctx: ToolContext) -> bool:
    """Whether this call runs in the owner's own turn, as the turn's context says it (set by the
    code that runs the turn, never by the call's arguments): his conversation, not a turn Domo
    acts in on his own, not a step of a chain, and no request it runs for."""
    return (
        not ctx.on_his_own
        and ctx.person.role == "owner"
        and not ctx.in_a_chain
        and ctx.request_id is None
        and ctx.approved_by is None
    )


def _zone_of(given: "_Address") -> str:
    """The zone an address is judged and audited in: a port of the machine that hosts Domo is
    an address of the house (spec 13.10), whatever its name says; any other, as written
    (``outward.literal.literal_zone``)."""
    return "house" if given.on_host else literal_zone(given.host)


def _leaves_the_house(given: "_Address") -> bool:
    """Whether opening the address leaves the house (``outward.literal.open_risks``): never a port
    of the machine that hosts Domo."""
    return not given.on_host and open_risks(given.host).outward


# --- address_open ---------------------------------------------------------------------------------

_HOST_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": "The host: a name or an IP address, without a scheme, a port or a path.",
    "minLength": 1,
    "maxLength": MAX_HOST,
}
_PORT_PROPERTY: Final[JsonObject] = {
    "type": "integer",
    "description": "Its port (80 for http and 443 for https, unless the address says another).",
    "minimum": 1,
    "maximum": 65535,
}
_TOOL_PROPERTY: Final[JsonObject] = {
    "type": "string",
    "description": "The name of the tool of yours it is for (built yet or not); leave it out for "
    "the programs you run by hand.",
    "pattern": NAME_PATTERN,
}
_SILENT_WORDS: Final = (
    "Nobody is told that you do this unless they ask: your words say nothing of an address or of "
    "a tool."
)


class AddressOpen:
    """Open an address to a tool of Domo's, or to his programs run by hand."""

    # Domo's own act, in anyone's turn: judged with his own rights.
    acts_as_domo: Literal[True] = True

    definition = ToolDefinition(
        name="address_open",
        description=(
            "Open an address (a host and its port) to one of your tools (tool), or to the "
            "programs you run by hand (no tool), when what you are asked needs it: until then a "
            "program reaches no address at all, and an address open to one tool is not open to "
            "another. It is your judgement, in anyone's conversation; an address on the internet "
            "leaves the house, so open only what is needed. Your own services are never reached, "
            "whatever is open. A port of the machine that hosts you counts as the house, and is a "
            "right apart (on_host): without it, it takes the owner's yes, which this same call "
            "asks for (when the person you talk with is the one who gives it, what they said may "
            "be it). An address closed by the owner's rules is closed for all your tools: you may "
            "open it again when you judge things have changed, weighing that closing (the call "
            "says it, and when); when it takes a yes, it is the owner's yes. Neither is ever a "
            "step of a chain. "
            "The result says whether it is open, waits for a yes, or was refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": _HOST_PROPERTY,
                "port": _PORT_PROPERTY,
                "tool": _TOOL_PROPERTY,
                "on_host": {
                    "type": "boolean",
                    "description": "True for a port of the machine that hosts you (a service "
                    "that runs beside you on it).",
                },
            },
            "required": ["host", "port"],
            "additionalProperties": False,
        },
    )
    action_type = "net.open"

    async def _type(self, session: AsyncSession, ctx: ToolContext, given: _Address) -> str:
        """The type the call is judged as (see the module docstring)."""
        if given.on_host and await _domo_opens_the_host(session, ctx):
            return NET_OPEN_HOST
        return "net.open"

    async def _needs_the_owner(
        self, session: AsyncSession, ctx: ToolContext, given: _Address
    ) -> Literal["closed", "host"] | None:
        """What makes the opening take the owner's yes when one is asked, if anything: a port of
        the host without Domo's right (``host``: a right, so his yes unless it is his own turn),
        first, whether the owner also closed it or not; else an address whose last closing was the
        owner's (``closed``, ruling R24: Domo may judge that things changed)."""
        if given.on_host and not await _domo_opens_the_host(session, ctx):
            return "host"
        if await closed_by_the_owner(session, given.host, given.port):
            return "closed"
        return None

    async def never_a_step(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Why the call is never a step of a chain (``NeverAStep``): it takes the owner's yes."""
        given = _Address(arguments, with_on_host=True)
        async with ctx.session_factory() as session:
            needs = await self._needs_the_owner(session, ctx, given)
        return None if needs is None else _NOT_A_STEP

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Address(arguments, with_on_host=True)
        async with ctx.session_factory() as session:
            await _tool_open_to_them(session, ctx, given.tool)
            closed_at = await owner_s_closing(session, given.host, given.port)
            action_type = await self._type(session, ctx, given)
        outward = _leaves_the_house(given)
        facts: dict[str, FactValue] = {
            "host": given.host,
            "port": given.port,
            "tool": given.tool,
            "on_host": given.on_host,
            "zone": _zone_of(given),
            "outward": outward,
            # The owner's closing, for whoever weighs a reopening (the judge, a rule of his).
            "closed_by_owner": closed_at is not None,
            "closed_on": _day_of(closed_at),
        }
        return DescribedAction(
            connector="core",
            action_type=action_type,
            target=given.target,
            effect="self_profile",
            destination="external" if outward else "household",
            facts=facts,
        )

    async def waits_for(self, arguments: JsonObject, ctx: ToolContext) -> WaitsForAYes | None:
        """A port of the host without Domo's right, out of the owner's own turn: Domo's request
        waiting for the owner's yes (ruling R24), never judged. Any other call: ``None``."""
        given = _Address(arguments, with_on_host=True)
        async with ctx.session_factory() as session:
            needs = await self._needs_the_owner(session, ctx, given)
        if needs != "host" or _owners_turn(ctx):
            return None
        return WaitsForAYes(approver="owner", asked=Asked("yes", "approver", None), domo_asks=True)

    async def whose_yes(self, arguments: JsonObject, ctx: ToolContext) -> Approver | None:
        """An address the owner closed, out of his own turn: when a yes is asked before opening it
        again, it is his, as Domo's request (``AsksWhomItsYes``, ruling R24)."""
        given = _Address(arguments, with_on_host=True)
        async with ctx.session_factory() as session:
            needs = await self._needs_the_owner(session, ctx, given)
        return "owner" if needs is not None and not _owners_turn(ctx) else None

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Address(arguments, with_on_host=True)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            await _tool_open_to_them(session, ctx, given.tool)
            by = _by(ctx)
            under: str | None = None
            turn_of = actor_label(ctx.turn_subject)
            needs = await self._needs_the_owner(session, ctx, given)
            # Whether it opens again what the owner closed (a port of the host may be both).
            reopened = needs is not None and await closed_by_the_owner(
                session, given.host, given.port
            )
            if needs is not None:
                if ctx.in_a_chain:
                    raise InvalidArguments("not_possible", _NOT_A_STEP)
                approver = await _the_owner_s_yes(session, ctx)
                if approver is not None:
                    # Opened as the owner's act, on his yes to this very request (ruling R19).
                    by, under = str(approver), "owner_yes"
                    turn_of = await _turn_of_the_request(session, ctx)
                elif ctx.request_id is not None or ctx.approved_by is not None:
                    # A request run without the owner's own yes on it: nothing opens.
                    raise InvalidArguments("not_possible", _NO_OWNER_YES)
                elif _owners_turn(ctx) and await is_the_owner(session, str(ctx.person.id)):
                    # His own turn: the judge weighed his words and let it through (R23).
                    under = "owner_asked"
                elif needs == "host":
                    # Out of his turn a port of the host waits for his yes (``waits_for``).
                    raise InvalidArguments("not_possible", _HOST_NEEDS_THE_OWNER)
                else:
                    # Reopening what the owner closed, judged in this turn (R24).
                    under = "on_his_own" if ctx.on_his_own else "person_asked"
            elif given.on_host:
                under = "domo_right"
            row = await open_address(
                session,
                tool_name=given.tool,
                host=given.host,
                port=given.port,
                on_host=given.on_host,
                by=by,
                now=now,
                reopening=reopened,
            )
            await append_audit(
                session,
                actor=actor_label(ctx.turn_subject),
                action="outward.address_opened",
                details={
                    "tool": given.tool,
                    "host": row.host,
                    "port": row.port,
                    "on_host": row.on_host,
                    "zone": _zone_of(given),
                    "under": under,
                    # Whether it opened again what the owner had closed, and in whose turn.
                    "reopened": reopened,
                    "turn_of": turn_of,
                },
            )
            await session.commit()
        told: JsonObject = {
            "status": "done",
            "host": given.host,
            "port": given.port,
            "tool": given.tool,
            "on_host": given.on_host,
        }
        card: JsonObject = {
            "key": "action.card.done.net.open",
            "params": {"address": f"{given.host}:{given.port}"},
        }
        return ToolOutcome(status="done", model_result=told, card=card)


# --- address_close --------------------------------------------------------------------------------


class AddressClose:
    """Close an address of a tool of Domo's, or of his programs run by hand."""

    # Domo's own act, in anyone's turn: judged with his own rights.
    acts_as_domo: Literal[True] = True

    definition = ToolDefinition(
        name="address_close",
        description=(
            "Close an address you opened (to one of your tools, or to the programs you run by "
            "hand: no tool), when it is no use any more, or when the person you talk with wants it "
            "closed: from the next request on, nothing reaches it. Closed in a conversation with "
            "the person who gives the owner's yes, that host and port is closed for all your tools "
            "and the programs you run by hand at once, and it stays so until you judge that things "
            "have changed (address_open weighs it); any other closing is the one tool's. "
            + _SILENT_WORDS
        ),
        parameters={
            "type": "object",
            "properties": {"host": _HOST_PROPERTY, "port": _PORT_PROPERTY, "tool": _TOOL_PROPERTY},
            "required": ["host", "port"],
            "additionalProperties": False,
        },
    )
    action_type = "net.close"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Address(arguments, with_on_host=False)
        async with ctx.session_factory() as session:
            await _tool_open_to_them(session, ctx, given.tool)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=given.target,
            effect="self_profile",
            destination="household",
            facts={"host": given.host, "port": given.port, "tool": given.tool},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Address(arguments, with_on_host=False)
        async with ctx.session_factory() as session:
            await _tool_open_to_them(session, ctx, given.tool)
            by = _by(ctx)
            closed = await close_the_address(
                session,
                tool_name=given.tool,
                host=given.host,
                port=given.port,
                by=by,
                now=ctx.clock(),
            )
            if closed:
                await append_audit(
                    session,
                    actor=actor_label(ctx.turn_subject),
                    action="outward.address_closed",
                    details={
                        "tool": given.tool,
                        "host": given.host,
                        "port": given.port,
                        "by_owner": await is_the_owner(session, by),
                    },
                )
            await session.commit()
        told: JsonObject = {"status": "done", "closed": closed}
        if not closed:
            told["detail"] = "this address was not open"
        return ToolOutcome(status="done", model_result=told, card=_CARD)


# --- addresses_look -------------------------------------------------------------------------------


class AddressesLook:
    """Look at the addresses open to Domo's tools."""

    # Domo's own act, in anyone's turn: judged with his own rights.
    acts_as_domo: Literal[True] = True

    definition = ToolDefinition(
        name="addresses_look",
        description=(
            "Look at the addresses open to your tools (to one tool, with tool) and to the programs "
            "you run by hand (tool null): each one's host, port and whether it is a port of the "
            "machine that hosts you; and those whose last closing was by the owner's rules "
            "(closed_by_the_owner): open one again only if you judge things have changed. "
            + _SILENT_WORDS
        ),
        parameters={
            "type": "object",
            "properties": {"tool": _TOOL_PROPERTY},
            "additionalProperties": False,
        },
    )
    action_type = "net.look"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _only(arguments, frozenset(), frozenset({"tool"}))
        tool = _tool(arguments.get("tool"))
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None if tool is None else f"tool:{tool}",
            effect="read",
            destination="household",
            facts={"tool": tool},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _only(arguments, frozenset(), frozenset({"tool"}))
        tool = _tool(arguments.get("tool"))
        async with ctx.session_factory() as session:
            live = await live_addresses(session, tool)
            closed = await closed_by_owner_now(session, tool)
        told: JsonObject = {
            "open": [
                {"tool": row.tool_name, "host": row.host, "port": row.port, "on_host": row.on_host}
                for row in live
            ],
            "closed_by_the_owner": [
                {"tool": row.tool_name, "host": row.host, "port": row.port} for row in closed
            ],
        }
        return ToolOutcome(status="done", model_result=told, card=_CARD)


# --- account_ask ----------------------------------------------------------------------------------


def _for(value: object) -> For:
    if value == "me":
        return "me"
    if value == "house":
        return "house"
    raise InvalidArguments("invalid", 'for is "me" (the account of whoever gives it) or "house"')


def _text_of(place: Mapping[str, object], key: str, most: int) -> str:
    value = place.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= most:
        raise InvalidArguments("invalid", f"the place's {key} is a text of 1 to {most} characters")
    if refuses_secret_shapes(value) or plain_problem(value) is not None:
        raise InvalidArguments(
            "not_possible",
            f"the place's {key} cannot hold that: no secret, no markup, no web address",
        )
    return value


def _place(value: object) -> InjectionLocation:
    """Where an account is placed, checked (see the tool's description)."""
    if not isinstance(value, dict):
        raise InvalidArguments("invalid", "place is an object, with its kind")
    place = cast(dict[str, object], value)
    try:
        match place.get("kind"):
            case "header":
                _only(place, frozenset({"kind", "name"}), frozenset({"kind", "name", "template"}))
                name = _text_of(place, "name", MAX_HEADER_NAME).lower()
                if name in _RESERVED_HEADERS:
                    raise InvalidArguments("not_possible", "an account never goes in this header")
                if "template" in place:
                    template = _text_of(place, "template", MAX_TEMPLATE)
                    return HeaderLocation(kind="header", name=name, template=template)
                return HeaderLocation(kind="header", name=name)
            case "basic":
                _only(place, frozenset({"kind"}), frozenset({"kind"}))
                return BasicLocation(kind="basic")
            case "login":
                keys = frozenset({"kind", "path", "encoding", "user_field", "password_field"})
                _only(place, keys, keys)
                encoding = place["encoding"]
                if encoding != "form" and encoding != "json":
                    raise InvalidArguments("invalid", 'the encoding is "form" or "json"')
                return LoginLocation(
                    kind="login",
                    path=_text_of(place, "path", MAX_LOGIN_PATH),
                    encoding=cast(Literal["form", "json"], encoding),
                    user_field=_text_of(place, "user_field", MAX_FIELD),
                    password_field=_text_of(place, "password_field", MAX_FIELD),
                )
            case _:
                raise InvalidArguments(
                    "invalid", 'the kind of a place is "header", "basic" or "login"'
                )
    except ValidationError:
        raise InvalidArguments(
            "invalid",
            "this place cannot be: a header name is a token, a template holds {value}, a login "
            "path starts with / and has no query, and its two fields differ",
        ) from None


def _hosts(value: object) -> list[tuple[str, int]]:
    if not isinstance(value, list) or not 1 <= len(cast(list[object], value)) <= MAX_HOSTS:
        raise InvalidArguments("invalid", f"hosts are 1 to {MAX_HOSTS} {{host, port}}")
    hosts: list[tuple[str, int]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            raise InvalidArguments("invalid", "each host is {host, port}")
        entry = cast(dict[str, object], item)
        _only(entry, frozenset({"host", "port"}), frozenset({"host", "port"}))
        host = _host(entry["host"])
        if ":" in host or "[" in host:
            raise InvalidArguments("invalid", "an account's host is a name or an IPv4 address")
        hosts.append((host, _port(entry["port"])))
    if len(set(hosts)) != len(hosts):
        raise InvalidArguments("invalid", "each host is given once")
    return hosts


class _Ask:
    """The arguments of an ``account_ask`` call, checked."""

    _REQUIRED: Final = frozenset({"name", "for", "hosts", "place"})
    _KEYS: Final = _REQUIRED | {"giver", "note"}

    def __init__(self, arguments: JsonObject) -> None:
        _only(arguments, self._REQUIRED, self._KEYS)
        name = arguments["name"]
        if not isinstance(name, str) or _ACCOUNT_NAME.fullmatch(name) is None:
            raise InvalidArguments(
                "invalid",
                "name is 2 to 40 characters: a to z, 0 to 9 and _, starting with a letter",
            )
        self.name = name
        self.for_: For = _for(arguments["for"])
        self.hosts = _hosts(arguments["hosts"])
        self.place = _place(arguments["place"])
        giver = arguments.get("giver")
        if giver is not None and not isinstance(giver, str):
            raise InvalidArguments("invalid", "giver is the name of someone of the household")
        self.giver_name: str | None = giver
        note = arguments.get("note")
        self.note: str | None = None
        if note is not None:
            self.note = note_text_from(note)
            if refuses_secret_shapes(self.note):
                raise InvalidArguments("not_possible", _NOTE_SECRET)


async def _giver(session: AsyncSession, ctx: ToolContext, given: _Ask) -> Person:
    """Who gives the account: the person of the turn, or, on his own, whom Domo names (never a
    guest); a house account only an owner's or an adult's."""
    if ctx.on_his_own:
        if given.giver_name is None:
            raise InvalidArguments("invalid", _NO_GIVER)
        if given.note is None:
            raise InvalidArguments("invalid", _NO_NOTE)
        giver = await resolve_person(session, given.giver_name, now=ctx.clock())
    else:
        if given.giver_name is not None or given.note is not None:
            raise InvalidArguments("invalid", _GIVER_IS_THEM)
        giver = ctx.person
    # A guest gives none; someone of another role only passing by is the judge's to weigh (the
    # owner's answer of 2026-11-27, "Domo juge": the case says where they stand).
    if giver.role == "guest":
        raise InvalidArguments("not_possible", _GUEST_GIVES_NONE)
    if given.for_ == "house" and (giver.role not in ("owner", "adult") or is_child(giver)):
        raise InvalidArguments("not_possible", _HOUSE_ACCOUNT)
    return giver


async def _askable(
    session: AsyncSession, given: _Ask, giver: Person
) -> tuple[uuid.UUID | None, datetime | None]:
    """Whose the account is (``None``: the house's), once what the database alone says allows it
    (one of that name already there otherwise refuses it), and when the owner removed it, if he
    was the last to remove it (``None`` otherwise): never a refusal, but what the call is judged
    with (Task 27)."""
    person_id = None if given.for_ == "house" else giver.id
    live = await live_account(session, name=given.name, person_id=person_id)
    if live is not None and (
        live.given_by != giver.id
        or live.hosts != kept_hosts(given.hosts)
        or live.place != PLACE.dump_python(given.place, mode="json")
    ):
        raise InvalidArguments("not_possible", _EXISTS)
    removed_at = await owner_s_removal(session, name=given.name, person_id=person_id)
    return person_id, removed_at


def _place_words(place: InjectionLocation) -> dict[str, str]:
    """What the purpose of an account's form says of its place, by its holes."""
    if isinstance(place, HeaderLocation):
        return {"header": place.name}
    if isinstance(place, LoginLocation):
        return {"path": place.path}
    return {}


async def _leave_the_note(
    session: AsyncSession, ctx: ToolContext, giver: Person, given: _Ask
) -> None:
    """Domo's own note to ``giver``, who gives the account he asked for on his own: his words,
    as he wrote them for them (``note``), never a sentence of the code. No link goes in it: the
    form is opened when the giver asks for it in their conversation (a link is shown once, never
    kept)."""
    from domo_core.conversation.visits import news_for

    now = ctx.clock()
    session.add(
        Note(
            author_id=None,
            by_domo=True,
            recipient_id=giver.id,
            text=cast(str, given.note),
            exact_text=None,
            created_at=now,
        )
    )
    await news_for(session, giver.id, now=now)
    await append_audit(
        session,
        actor=actor_label(domo_actor()),
        action="note.left",
        details={"author": actor_label(domo_actor()), "recipient": str(giver.id)},
    )


class AccountAsk:
    """Ask someone for an account (see the module docstring)."""

    person_arguments: Final = ("giver",)

    definition = ToolDefinition(
        name="account_ask",
        description=(
            "Ask for an account at a service or a device, for your programs to use it by its name "
            "without anyone seeing its secret: a token put in a header (place kind header: its "
            "name, and template with {value}, as Bearer {value}), a user name and a password sent "
            "as Basic (kind basic), or put in the body of a login request (kind login: its path, "
            "encoding form or json, user_field and password_field). Describe it for good: its "
            "name, whose it is (for: me, the account of whoever gives it; house, the household's, "
            "which a child does not give), the hosts it is sent to and where it is placed; this "
            "never changes afterwards, and an account given again with other hosts is another "
            "account. The person types it in a safe form shown to them once: it goes from them "
            "straight into the encrypted vault, you never see it, so never ask for it in the "
            "conversation. A name already there is refused (exists): nothing is replaced. When the "
            "form cannot be shown to them now, the same call later shows it. On your own, name who "
            "gives it (giver) and write them a note (note), in your words and their language: it "
            "tells them, and they give it when they talk with you. A removed account may be asked "
            "again when you judge things have changed: the call says so, and when; a yes it then "
            "takes for a house account is the owner's yes. An account given in clear goes only to "
            "the house: towards the internet, its requests use https."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Its name, as your programs name it: 2 to 40 characters, a to "
                    "z, 0 to 9 and _, a letter first.",
                    "pattern": ACCOUNT_NAME_PATTERN,
                },
                "for": {
                    "type": "string",
                    "enum": ["me", "house"],
                    "description": "me: the account of whoever gives it; house: the household's.",
                },
                "hosts": {
                    "type": "array",
                    "description": f"The hosts it is sent to, 1 to {MAX_HOSTS}: each a name or "
                    "an IPv4 address, and its port.",
                    "minItems": 1,
                    "maxItems": MAX_HOSTS,
                    "items": {
                        "type": "object",
                        "properties": {"host": _HOST_PROPERTY, "port": _PORT_PROPERTY},
                        "required": ["host", "port"],
                        "additionalProperties": False,
                    },
                },
                "place": {
                    "type": "object",
                    "description": "Where it is placed: {kind: header, name, template}, {kind: "
                    "basic}, or {kind: login, path, encoding, user_field, password_field}.",
                },
                "giver": {
                    "type": "string",
                    "description": "On your own only: the name of who gives it.",
                },
                "note": {
                    "type": "string",
                    "description": "On your own only: what you write to whoever gives it, in their "
                    "language, to tell them what you ask and why; never a secret.",
                    "minLength": 1,
                    "maxLength": MAX_NOTE_LENGTH,
                },
            },
            "required": ["name", "for", "hosts", "place"],
            "additionalProperties": False,
        },
    )
    action_type = "account.ask"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Ask(arguments)
        async with ctx.session_factory() as session:
            giver = await _giver(session, ctx, given)
            _, removed_at = await _askable(session, given, giver)
        facts: dict[str, FactValue] = {
            "name": given.name,
            "place": given.place.kind,
            "for_house": given.for_ == "house",
            "giver": f"person:{giver.id}",
            "hosts": len(given.hosts),
            "outward": any(literal_zone(host) == "internet" for host, _ in given.hosts),
            # The owner's removal, for whoever weighs asking it again (the judge, a rule of his).
            "removed_by_owner": removed_at is not None,
            "removed_on": _day_of(removed_at),
            # The instant of that removal: what the yes is held against, so a removal asked
            # again and removed again the same day is not taken as the one that was weighed.
            # (Empty when it was not removed: a fact of text is always a text.)
            "removed_at": "" if removed_at is None else removed_at.astimezone(UTC).isoformat(),
        }
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"account:{given.name}",
            effect="secrets",
            destination="household",
            facts=facts,
        )

    async def whose_yes(self, arguments: JsonObject, ctx: ToolContext) -> Approver | None:
        """A house account the owner removed, out of his own turn: when a yes is asked before it
        is asked again, it is the owner's, as Domo's request (``AsksWhomItsYes``; the house's
        thing answers to the owner). Any other call: the usual."""
        given = _Ask(arguments)
        if given.for_ != "house" or _owners_turn(ctx):
            return None
        async with ctx.session_factory() as session:
            removed_at = await owner_s_removal(session, name=given.name, person_id=None)
        return None if removed_at is None else "owner"

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Ask(arguments)
        shows = not ctx.on_his_own and not ctx.leave_on_their_card and bool(ctx.origin)
        async with ctx.session_factory() as reader:
            giver = await _giver(reader, ctx, given)
            # In the words of the one who gives it, read first: no transaction waits for the vault.
            form = None
            if shows:
                words = await key_form(reader, giver, writing=ctx.text_writing)
                purpose = words.texts(
                    f"chat.account.purpose.{given.place.kind}",
                    name=given.name,
                    **_place_words(given.place),
                )
                form = (words, purpose)
        # What the call was judged or agreed with (``described_action``: a stored request, a
        # step, or a call judged in this turn, ``catalog.WARNED_FACTS``): the owner's removal
        # opens it again only when that very removal (and its day) was what was weighed; any
        # other is sent back to be asked again, never run on what nobody weighed.
        described = ctx.described_action
        async with ctx.session_factory() as session:
            person_id, removed_at = await _askable(session, given, giver)
            weighed = removed_at is None or (
                described is not None
                and described.facts.get("removed_by_owner") is True
                and described.facts.get("removed_at") == removed_at.astimezone(UTC).isoformat()
            )
            try:
                asked = await ask_account(
                    session,
                    ctx.gatekeeper,
                    name=given.name,
                    person_id=person_id,
                    giver=giver,
                    hosts=given.hosts,
                    place=given.place,
                    by=_by(ctx),
                    form=form,
                    asking_again=weighed,
                )
            except RemovedByOwner:
                raise InvalidArguments("not_possible", _REMOVED_SINCE) from None
            except AccountExists:
                raise InvalidArguments("not_possible", _EXISTS) from None
            except GatekeeperError:
                await session.rollback()
                told: JsonObject = {"reason": "gatekeeper", "detail": _GATEKEEPER}
                return ToolOutcome(status="failed", model_result=told, card=_FAILED_CARD)
            form_path = None if asked.view is None else vault_form_path(asked.view)
            if shows and form_path is None:
                # An answer of the vault no page would show: nothing is asked.
                await session.rollback()
                told = {"reason": "gatekeeper", "detail": _GATEKEEPER}
                return ToolOutcome(status="failed", model_result=told, card=_FAILED_CARD)
            if ctx.on_his_own:
                await _leave_the_note(session, ctx, giver, given)
            # Last, just before the commit: metadata, never the form's path nor its link.
            await append_audit(
                session,
                actor=actor_label(ctx.turn_subject),
                action="outward.account_asked",
                details={
                    "name": given.name,
                    "for_house": person_id is None,
                    "place": given.place.kind,
                    "hosts": len(given.hosts),
                    "giver": str(giver.id),
                    "again": asked.again,
                    "form": form_path is not None,
                },
            )
            await session.commit()
            shown = giver.display_name
        if ctx.on_his_own and ctx.notify is not None:
            ctx.notify(giver.id)
        card: JsonObject = {"key": "action.card.done.account.ask", "params": {"name": given.name}}
        result: JsonObject = {"status": "done", "name": given.name}
        if form_path is None:
            result["form"] = "waits"
            if ctx.on_his_own:
                result["note_left_for"] = shown
            else:
                result["detail"] = _FORM_WAITS
            return ToolOutcome(status="done", model_result=result, card=card)
        result["form"] = "shown"
        return ToolOutcome(
            status="done",
            model_result=result,
            card=card,
            # Shown once, to the one who gives it, in the answer: never stored, never told to the
            # model.
            card_once={
                "key": "action.card.once.account.ask",
                "params": {},
                "link": f"{ctx.origin.rstrip('/')}{form_path}",
            },
        )


# --- account_remove -------------------------------------------------------------------------------


class _Whose:
    """Which account a call names: its name, and whose (``me``, ``house`` or ``person``, with the
    name of whose, ``of``)."""

    def __init__(self, arguments: JsonObject, *, extra: frozenset[str] = frozenset()) -> None:
        _only(arguments, frozenset({"name", "for"}), frozenset({"name", "for", "of"}) | extra)
        name = arguments["name"]
        if not isinstance(name, str) or _ACCOUNT_NAME.fullmatch(name) is None:
            raise InvalidArguments("invalid", "name is the name of an account")
        self.name = name
        whose = arguments["for"]
        self.for_: Literal["me", "house", "person"]
        if whose == "me" or whose == "house" or whose == "person":
            self.for_ = whose
        else:
            raise InvalidArguments("invalid", 'for is "me", "house" or "person" (with of)')
        of = arguments.get("of")
        if (self.for_ == "person") != isinstance(of, str):
            raise InvalidArguments("invalid", 'of names the person, with for "person" only')
        self.of: str | None = cast(str | None, of)


async def _account_named(
    session: AsyncSession, ctx: ToolContext, given: _Whose
) -> tuple[ServiceAccount, Person | None]:
    """The live account ``given`` names, and whose it is (``None``: the house's);
    ``not_found`` when there is none."""
    owner: Person | None
    match given.for_:
        case "house":
            owner = None
        case "me":
            owner = ctx.person
        case "person":
            owner = await resolve_person(session, cast(str, given.of), now=ctx.clock())
    account = await live_account(
        session, name=given.name, person_id=None if owner is None else owner.id
    )
    if account is None:
        raise InvalidArguments("not_found", _NO_ACCOUNT)
    return account, owner


async def _removable(session: AsyncSession, ctx: ToolContext, given: _Whose) -> ServiceAccount:
    """The account to remove: whoever gave it, or the owner; on his own, Domo's act on any one
    he names by whose it is (the owner, D8: one that no longer serves), judged with his own
    rights like his other acts of his own, a doubt waiting for whom it belongs to."""
    if ctx.on_his_own:
        if given.for_ == "me":
            raise InvalidArguments("invalid", _NOBODY_S_OWN)
        account, _ = await _account_named(session, ctx, given)
        return account
    account, owner = await _account_named(session, ctx, given)
    theirs = account.given_by == ctx.person.id or (owner is not None and owner.id == ctx.person.id)
    if is_child(ctx.person) and account.person_id != ctx.person.id:
        raise InvalidArguments("not_possible", _NOT_THEIR_OWN)
    if not theirs and ctx.person.role != "owner":
        raise InvalidArguments("not_possible", _NOT_THEIRS)
    return account


class AccountRemove:
    """Remove an account."""

    person_arguments: Final = ("of",)

    definition = ToolDefinition(
        name="account_remove",
        description=(
            "Remove an account when whoever gave it wants it gone (the owner only, for an account "
            "someone else gave): nothing uses it any more, at once, and its secret stays in the "
            "vault, where nothing can use it; to be sure, they change it at the service. It "
            "cannot be undone: an account given again is another. When it was the owner only who "
            "removed it, that is weighed whenever it is asked again. On your own, you may remove "
            "one that no longer "
            "serves (for: house, or person with of), when you judge it right."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name of the account."},
                "for": {
                    "type": "string",
                    "enum": ["me", "house", "person"],
                    "description": "me: the account of the person you talk with; house: the "
                    "household's; person: someone else's (of).",
                },
                "of": {
                    "type": "string",
                    "description": 'With for "person": the name of whose account it is.',
                },
            },
            "required": ["name", "for"],
            "additionalProperties": False,
        },
    )
    action_type = "account.remove"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Whose(arguments)
        async with ctx.session_factory() as session:
            account = await _removable(session, ctx, given)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"account:{account.id}",
            effect="secrets",
            destination="household",
            facts={
                "name": account.name,
                "for_house": account.person_id is None,
                "of": None if account.person_id is None else f"person:{account.person_id}",
            },
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Whose(arguments)
        async with ctx.session_factory() as session:
            account = await _removable(session, ctx, given)
            described = ctx.described_action
            if described is not None and described.target != f"account:{account.id}":
                raise InvalidArguments(
                    "not_found", "this is no longer the account the yes was given for: ask again"
                )
            # Who it was judged as: the assistant for an idea of his, never the person whose
            # turn it is (the owner's removal is his own, not Domo's).
            acting = ctx.acting
            by = who_of(None if acting.subject == "domo" else ctx.person)
            await remove_account(session, account=account, by=by, now=ctx.clock())
            await append_audit(
                session,
                actor=actor_label(acting),
                action="outward.account_removed",
                details={
                    "name": account.name,
                    "for_house": account.person_id is None,
                    "of": None if account.person_id is None else str(account.person_id),
                    "by_owner": account.removed_by_owner,
                },
            )
            await session.commit()
        told: JsonObject = {
            "status": "done",
            "name": given.name,
            "detail": "nothing uses it any more; its secret stays in the vault, unusable",
        }
        card: JsonObject = {
            "key": "action.card.done.account.remove",
            "params": {"name": given.name},
        }
        return ToolOutcome(status="done", model_result=told, card=card)


# --- account_share --------------------------------------------------------------------------------


class AccountShare:
    """Open a house account to guests, or to Domo on his own, or close it to them."""

    definition = ToolDefinition(
        name="account_share",
        description=(
            "Say who else a house account serves: the guests (guests: true opens it to them, false "
            "closes it), and you on your own, for the house (domo: true opens it to you, false "
            "closes it). A house account serves you on your own and no guest until this says "
            "otherwise."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name of the house account."},
                "guests": {"type": "boolean", "description": "Whether it serves the guests."},
                "domo": {
                    "type": "boolean",
                    "description": "Whether it serves you on your own, for the house.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    action_type = "account.share"

    @staticmethod
    def _given(arguments: JsonObject) -> tuple[str, bool | None, bool | None]:
        _only(arguments, frozenset({"name"}), frozenset({"name", "guests", "domo"}))
        name = arguments["name"]
        if not isinstance(name, str) or _ACCOUNT_NAME.fullmatch(name) is None:
            raise InvalidArguments("invalid", "name is the name of a house account")
        guests = _boolean(arguments.get("guests"), "guests")
        domo = _boolean(arguments.get("domo"), "domo")
        if guests is None and domo is None:
            raise InvalidArguments("invalid", _NOTHING_TO_SHARE)
        return name, guests, domo

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name, guests, domo = self._given(arguments)
        async with ctx.session_factory() as session:
            if await live_account(session, name=name, person_id=None) is None:
                raise InvalidArguments("not_found", _NO_ACCOUNT)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"account:{name}",
            effect="secrets",
            destination="household",
            facts={"name": name, "guests": guests, "domo": domo},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, guests, domo = self._given(arguments)
        async with ctx.session_factory() as session:
            account = await live_account(session, name=name, person_id=None)
            if account is None:
                raise InvalidArguments("not_found", _NO_ACCOUNT)
            if guests is not None:
                account.for_guests = guests
            if domo is not None:
                account.for_domo = domo
            await session.flush()
            await append_audit(
                session,
                actor=actor_label(ctx.turn_subject),
                action="outward.account_shared",
                details={"name": name, "guests": guests, "domo": domo},
            )
            await session.commit()
            told: JsonObject = {
                "status": "done",
                "name": name,
                "for_guests": account.for_guests,
                "for_domo": account.for_domo,
            }
        card: JsonObject = {"key": "action.card.done.account.share", "params": {"name": name}}
        return ToolOutcome(status="done", model_result=told, card=card)


# --- accounts_look --------------------------------------------------------------------------------


class AccountsLook:
    """Look at the accounts the person of the turn has, and the house's."""

    definition = ToolDefinition(
        name="accounts_look",
        description=(
            "Look at the accounts: those of the person you talk with and the household's (a "
            "guest's own, and the household's they may use; on your own, the household's): each "
            "one's name, whose it is, the hosts it is sent to, where it is placed, whether it was "
            "given yet (given: false while its form waits), and for a house account whether it "
            "serves the guests and you on your own. Never its secret: nobody sees it. "
            "Nobody is told that you do this unless they ask."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "account.look"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _only(arguments, frozenset(), frozenset())
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _only(arguments, frozenset(), frozenset())
        async with ctx.session_factory() as session:
            query = select(ServiceAccount, Person.display_name).outerjoin(
                Person, Person.id == ServiceAccount.given_by
            )
            query = query.where(ServiceAccount.removed_at.is_(None))
            if ctx.on_his_own:
                query = query.where(ServiceAccount.person_id.is_(None))
            elif ctx.person.role != "owner":
                house = ServiceAccount.person_id.is_(None)
                if is_visiting(ctx.person):
                    house = house & ServiceAccount.for_guests.is_(True)
                query = query.where(house | (ServiceAccount.person_id == ctx.person.id))
            found = (await session.execute(query.order_by(ServiceAccount.name))).all()
            owners = {
                person.id: person.display_name
                for person in await session.scalars(
                    select(Person).where(
                        Person.id.in_({row.person_id for row, _ in found if row.person_id})
                    )
                )
            }
        given = await given_refs(ctx.gatekeeper)
        accounts: list[JsonObject] = []
        for account, giver_name in found:
            if account.person_id is None:
                whose = "house"
            elif not ctx.on_his_own and account.person_id == ctx.person.id:
                whose = "yours"
            else:
                whose = owners.get(account.person_id, "someone")
            told: JsonObject = {
                "name": account.name,
                "whose": whose,
                "hosts": account.hosts,
                "place": cast(JsonObject, PLACE.dump_python(place_of(account), mode="json")),
                "given": None if given is None else account.ref in given,
                "given_by": giver_name,
            }
            if account.person_id is None:
                told["for_guests"] = account.for_guests
                told["for_domo"] = account.for_domo
            accounts.append(told)
        return ToolOutcome(status="done", model_result={"accounts": accounts}, card=_CARD)
