"""The tools of each member's own keys (0.6.0, spec 4.2, 4.3, 4.7): add one, remove one, say who
else may use it, limit what it spends. Giving a key again is ``tools.model_key``; the rules of the
house on the keys are ``tools.keys_house``.

``key_add`` (``key.add``, effect ``secrets``) adds a key of the person's own: a cloud provider
(``kind``), or a server of their own (``openai_compatible``, at ``base_url``, ``has_key`` when it
takes a key). The vault form opens as for ``model_rekey`` (``llm.key_flow.start_key``): shown once,
in a frame under the card, in the answer that ran it (``ToolOutcome.card_once``), never stored and
never given to the model; the page that follows it checks the key once it is in
(``channels.page.key_routes``, ``llm.key_flow.finish_key``). When someone else said yes, when no
page reads the turn, or in a chain (``ToolContext.leave_the_form_on_their_card``, which also holds
on a channel that cannot open a safe screen), nothing is asked of the vault: the
card of the one who asked has a button that opens the same form as the first launch, on the page
``KEY_NEW_PATH`` (``"key_new"`` on the card). A server without a key has no form: the key is
recorded and its card has a button that checks it (``"key_check"``, the key's id).

``key_remove`` (``key.remove``, effect ``secrets``) removes one of the person's own keys. When it is
the last key of the household that works (``last_key``), or the general key and the one who gave
it has no other key that works (``general_key_without_heir``), it says so in its facts: the judge
weighs them (a silent judge asks a plain yes, ``catalog.WARNED_FACTS``), Domo tells them he could
no longer answer whoever depends on it, and if they still want it, it is removed (the owner's
decision D6: never a flat refusal). One that became so after the yes or the judge was given what
it knew is not removed (``changed``): they are asked again. Otherwise the general key goes to the
oldest other key of the one who gave it (a key that lists models). Before the row goes, the key's
route and its admin route are declared to the gatekeeper again without a secret
(``secret_ref=None``), so that no call can use the secret again, and once more after the row is
gone (a pass that declared a route with its secret in between, ``provider_keys.key_still_there``,
is undone); the secret itself stays in the vault (the
gatekeeper can remove none), which the result says, with the advice to revoke the key at the
provider. The row's models, lines of access, limits and choices go with it; the calls counted on it
keep their cost, with no key (``model_calls``).

``key_share`` (``key.share``, effect ``permissions``) says whether someone may use one of the
person's own keys: one person (``with`` ``person``, by their name), the people they invited
(``my_invitees``) or the whole household (``household``). It writes a line of ``provider_users``
(``set_by`` the person), which replaces the one of the same scope and person: a line that lets
someone use a key counts only when its giver wrote it (``router.allowed``).

``key_limit`` (``spending.limit``, effect ``permissions``) sets the firm limit of what one of the
person's own keys spends (spec 4.7), for everyone or for one person of the household (``person``,
by their name) or for a role (``role``: each adult, child or guest, now and later, with ``split``:
``each`` of them has the amount, or all of them ``together`` share it): ``amount`` ``currency`` per
``period`` (a calendar month by default), with the
threshold ``warn_at_percent`` only when the person asks to be told; ``remove`` takes it away. It
writes the row ``keys_house`` writes for the general key (``llm.spending.set_spending_limit``). A
key the person did not give is not theirs to limit (``not_possible``: a right call the household
refuses); an id that names no key is ``not_found``. The arguments of a limit are read here for both
tools (``limit_asked``), each under its own names.

Every change is audited: ``key.removed`` (``{"provider_id"}``), ``key.shared``
(``{"provider_id", "scope", "person", "allowed"}``) and ``spending.limit_set`` or
``spending.limit_removed`` (``set_spending_limit``); ``key.added`` is written once the key works
(``llm.key_flow``). No key value is ever in a result, a card or an entry: only references.
"""

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from urllib.parse import urlencode
from uuid import UUID

from domo_common.gatekeeper_api import ProviderRoute
from sqlalchemy import Select, delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.household.service import resolve_person
from domo_core.llm.choices import provider_scope
from domo_core.llm.key_flow import (
    KEY_NEW_PATH,
    KeyChoiceError,
    check_choice,
    new_key_status_url,
    start_key,
)
from domo_core.llm.models import ModelChoices, Provider, ProviderModel, ProviderUser
from domo_core.llm.provider_keys import (
    GatekeeperFailure,
    admin_route_id,
    gatekeeper_failure,
    key_form,
    provider_kind,
)
from domo_core.llm.provider_kinds import PROVIDER_KINDS, ProviderKind
from domo_core.llm.speech.home_key import HOME_SPEECH_ID
from domo_core.llm.spending import (
    CURRENCIES,
    LIMIT_ROLES,
    LIMIT_SPLITS,
    LIMIT_STEP,
    PERIODS,
    Currency,
    LimitRole,
    LimitSplit,
    Period,
    amount_text,
    kept_amount,
    set_spending_limit,
)
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.roles import Actor
from domo_core.text import is_plain_line
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.model_key import REKEY_REQUEST_PARAM, key_named, vault_form_shown

KEY_ADD_ACTION_TYPE: Final = "key.add"
KEY_REMOVE_ACTION_TYPE: Final = "key.remove"
KEY_SHARE_ACTION_TYPE: Final = "key.share"
SPENDING_LIMIT_ACTION_TYPE: Final = "spending.limit"
# Where a button checks a new key of a server without a key, or with the name of its model.
KEY_CHECK_PATH: Final = KEY_NEW_PATH + "/{provider_id}/check"

# The longest address of a server of one's own.
MAX_BASE_URL_LENGTH: Final = 2048
# The largest firm limit (``spending_limits.amount`` keeps twelve digits, four of them
# decimals: ``llm.spending.LIMIT_STEP``).
MAX_LIMIT_AMOUNT: Final = Decimal("99999999")

_KINDS: Final[tuple[ProviderKind, ...]] = tuple(PROVIDER_KINDS)
_WITH: Final = ("person", "household", "my_invitees")
# The scope of a line of ``provider_users`` that each ``with`` writes.
_SCOPE_OF: Final[Mapping[str, str]] = {
    "person": "person",
    "household": "household",
    "my_invitees": "invitees",
}

_CHOICE_ERRORS: Final[Mapping[str, str]] = {
    "base_url": (
        "base_url must be the address of the server, starting with http:// or https://, with no "
        "spaces (for openai_compatible only)"
    ),
    "insecure_transport": (
        "an API key is only sent over https, unless the server is on the local network: use an "
        "https address, or a server on the network"
    ),
}
_FAILURES: Final[Mapping[GatekeeperFailure, str]] = {
    "gatekeeper": "the secret vault did not open a form; try again in a moment",
    "binding_conflict": "the vault keeps a key of that name for other destinations",
    "insecure_transport": "the vault will not send a key to this server without https",
}
_NOT_THEIRS: Final = (
    "the person gave no key with that id: only the one who gave a key removes or shares it (the "
    "Keys line of the context says which are theirs)"
)
_NOT_THE_SAME: Final = (
    "that key is no longer the one the yes was given for, or no longer theirs, so nothing was "
    "done: ask again"
)
_BECAME_WARNED: Final = (
    "since it was asked, this key became the last one of the household that works, or the "
    "general key with no other key of its giver's to take over, so it was not removed: tell them "
    "that you could no longer answer whoever depends on it, and ask again if they still want it"
)
_STILL_IN_THE_VAULT: Final = (
    "the key itself stays in the vault, but nothing can use it any more; to be sure, revoke it "
    "at the provider"
)
_NO_KEY_LEFT: Final = (
    "no key that works is left: nobody can talk with you until someone gives one (key_add, or "
    "the page of a new key)"
)
_NO_GENERAL_KEY: Final = (
    "the household has no general key any more: whoever used it can talk with you only once a "
    "key is given for them"
)

# What removing a key warns about (facts of ``key.remove``, ``catalog.WARNED_FACTS``): the last
# key of the household that works, or the general key with no other key of its giver's to take
# over. Never a refusal: the judge weighs them, and a yes removes it all the same.
RemovalWarning = Literal["last_key", "general_key_without_heir"]
_WARNINGS: Final[tuple[RemovalWarning, ...]] = ("last_key", "general_key_without_heir")


def key_add_action() -> DescribedAction:
    """The action of adding a key of one's own, as the barrier is asked about it: by the tool
    (``KeyAdd.describe``) and by the page of a new key (``channels.page.key_routes``). The key does
    not exist yet: no target, and nothing of the call (the page does not know it yet)."""
    return DescribedAction(
        connector="core",
        action_type=KEY_ADD_ACTION_TYPE,
        target=None,
        effect="secrets",
        destination="household",
    )


def key_new_button_of(card: Mapping[str, object], confirmation_id: UUID | None) -> str | None:
    """Where a button on a card opens the form of a new key (``KEY_NEW_PATH``), if the card says
    it left one (``"key_new"``), with the request the yes was given to (``confirmation_id``) when
    there is one: the page opens the form for that yes. A page never goes anywhere else."""
    if card.get("key_new") is not True:
        return None
    if confirmation_id is None:
        return KEY_NEW_PATH
    return f"{KEY_NEW_PATH}?{urlencode({REKEY_REQUEST_PARAM: str(confirmation_id)})}"


def key_check_button_of(card: Mapping[str, object]) -> str | None:
    """Where a button on a card checks a new key of a server without a key (``"key_check"``, the
    key's id), if the card names one."""
    provider_id = card.get("key_check")
    if not isinstance(provider_id, str) or not _is_key_id(provider_id):
        return None
    return KEY_CHECK_PATH.format(provider_id=provider_id)


def _is_key_id(value: str) -> bool:
    return (
        len(value) == 10
        and value.startswith("p-")
        and all(character in "0123456789abcdef" for character in value[2:])
    )


# --- key_add -------------------------------------------------------------------------------------


def _new_key_of(arguments: JsonObject) -> tuple[ProviderKind, str | None, bool]:
    """The key a ``key_add`` call asks for, checked as the page checks it. Raises
    ``InvalidArguments``."""
    if set(arguments) - {"kind", "base_url", "has_key"}:
        raise InvalidArguments("invalid", "key_add takes kind, base_url and has_key")
    asked = arguments.get("kind")
    kind = provider_kind(asked) if isinstance(asked, str) else None
    if kind is None:
        raise InvalidArguments("invalid", f"kind must be one of: {', '.join(_KINDS)}")
    base_url = arguments.get("base_url")
    if base_url is not None and (
        not isinstance(base_url, str)
        or len(base_url) > MAX_BASE_URL_LENGTH
        or not is_plain_line(base_url)
    ):
        raise InvalidArguments("invalid", _CHOICE_ERRORS["base_url"])
    has_key = arguments.get("has_key", False)
    if not isinstance(has_key, bool):
        raise InvalidArguments("invalid", "has_key is true or false")
    return kind, base_url, has_key


def _checked_choice(arguments: JsonObject) -> tuple[ProviderKind, str | None, bool]:
    """``_new_key_of``, and what it asks for checked as the page checks it (``check_choice``)."""
    kind, base_url, has_key = _new_key_of(arguments)
    try:
        check_choice(kind, base_url, has_key)
    except KeyChoiceError as wrong:
        raise InvalidArguments("invalid", _CHOICE_ERRORS[wrong.what]) from None
    return kind, base_url, has_key


def _add_failed(failure: GatekeeperFailure) -> ToolOutcome:
    """The vault opened no form: nothing was added."""
    return ToolOutcome(
        status="failed",
        model_result={"reason": failure, "detail": _FAILURES[failure]},
        card={"key": f"action.card.failed.key.add.{failure}", "params": {}, "reason": failure},
    )


class KeyAdd:
    """Add a key of the person's own: the vault form opens, and its models are listed once it
    is in."""

    definition = ToolDefinition(
        name="key_add",
        description=(
            "Add an API key of the person you are talking with, for an AI provider (kind), or a "
            "server of their own (kind openai_compatible, at base_url; has_key when it takes a "
            "key), when that is what they want: the key is theirs, and the models it opens are "
            "listed once it is in. They type it in a safe form, where the context says a key is "
            "typed (say where): it goes from them straight into the encrypted vault, you never "
            "see it, so never ask for it in the conversation. The result says whether it was "
            "done, waits for a yes, or was not done. If someone else said yes, or in a chain, the "
            "form waits until the person who asked opens it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(_KINDS),
                    "description": "The provider: anthropic, openai, google, or openai_compatible "
                    "for a server of their own.",
                },
                "base_url": {
                    "type": "string",
                    "maxLength": MAX_BASE_URL_LENGTH,
                    "description": "The address of a server of their own (openai_compatible).",
                },
                "has_key": {
                    "type": "boolean",
                    "description": "Whether that server takes an API key (openai_compatible).",
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    )
    action_type = KEY_ADD_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _checked_choice(arguments)
        return key_add_action()

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        kind, base_url, has_key = _checked_choice(arguments)
        if ctx.leave_the_form_on_their_card:
            # The form is for the one who asked, and none is opened that nobody would see: their
            # card has the button that opens the page of a new key.
            return ToolOutcome(
                status="done",
                model_result={"key_form": "waits_for_them"},
                card={"key": ctx.form_left_card("key.add"), "params": {}, "key_new": True},
            )
        async with ctx.session_factory() as reader:
            # In the words of the one who gives the key, never the turn's (a wake's are the
            # reference texts); read first, so that no transaction waits for the vault.
            form = await key_form(reader, ctx.person, writing=ctx.text_writing)
        async with ctx.session_factory() as session:
            try:
                provider, view = await start_key(
                    session,
                    ctx.gatekeeper,
                    person=ctx.person,
                    kind=kind,
                    base_url=base_url,
                    has_key=has_key,
                    form=form,
                    # A stored request said yes to: its one key is this one (one yes, one key).
                    confirmation=None if ctx.request_id is None else str(ctx.request_id),
                )
            except GatekeeperError as error:
                return _add_failed(gatekeeper_failure(error))
            if view is None:
                # A server without a key: nothing to type, a button checks it.
                await session.commit()
                return ToolOutcome(
                    status="done",
                    model_result={"key": provider.id, "key_check": "waits_for_them"},
                    card={
                        "key": "action.card.done.key.add.check",
                        "params": {},
                        "key_check": provider.id,
                    },
                )
            shown = vault_form_shown(view, new_key_status_url(provider.id, view.id))
            if shown is None:
                # An answer of the vault that the page would not show: no key, no form.
                await session.rollback()
                return _add_failed("gatekeeper")
            # Last, just before the commit: the reference, never the form's path.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="secret.requested",
                details={"ref": provider.secret_ref},
            )
            await session.commit()
        form_path, status_url = shown
        return ToolOutcome(
            status="done",
            model_result={"key_form_shown": True, "key": provider.id},
            card={"key": "action.card.done.key.add", "params": {}},
            # Shown once, to the one who asked, in the answer: never stored, never told to the
            # model.
            card_once={
                "key": "action.card.once.key.add",
                "params": {},
                "form_path": form_path,
                "status_url": status_url,
            },
        )


# --- the person's own key ------------------------------------------------------------------------


async def _own_key(session: AsyncSession, key_id: str, person: Person) -> Provider:
    """The key ``key_id`` if ``person`` gave it; ``InvalidArguments("not_found")`` otherwise."""
    provider = await session.get(Provider, key_id) if _is_key_id(key_id) else None
    if provider is None or provider.given_by != person.id:
        raise InvalidArguments("not_found", _NOT_THEIRS)
    return provider


def _same_as_described(ctx: ToolContext, provider: Provider) -> None:
    """When a stored request runs, it must be about the key the yes was given for."""
    described = ctx.described_action
    if described is not None and described.target != f"provider:{provider.id}":
        raise InvalidArguments("not_found", _NOT_THE_SAME)


def _key_target(provider: Provider) -> str:
    return f"provider:{provider.id}"


def _for_whom_target(provider: Provider, person_id: UUID | None) -> str:
    """The target of a share or a limit: the key, and the person it is for when it is for one
    (``provider:<id> person:<id>``). A stored request runs for that very key and that very person
    (``_same_for_whom``): a name may mean someone else by the time of the yes (spec 6.7)."""
    target = _key_target(provider)
    return target if person_id is None else f"{target} person:{person_id}"


def _same_for_whom(ctx: ToolContext, provider: Provider, person_id: UUID | None) -> None:
    """When a stored request runs, it must be about the key and the person the yes was given
    for."""
    described = ctx.described_action
    if described is not None and described.target != _for_whom_target(provider, person_id):
        raise InvalidArguments("not_found", _NOT_THE_SAME)


def _key_facts(provider: Provider, giver: Person) -> dict[str, FactValue]:
    """The key a share or a limit is about, as whoever says yes reads it
    (``confirmations.describe``): its kind and the name of who gave it."""
    return {"key_kind": provider.kind, "key_of": giver.display_name}


# --- key_remove ----------------------------------------------------------------------------------


def _working_keys_but(provider: Provider) -> Select[Provider]:
    """The other keys, that list a model, oldest first. The house's speech service
    (``HOME_SPEECH_ID``) is never one: it hears and says, nobody could think with it."""
    return (
        select(Provider)
        .where(
            Provider.id != provider.id,
            Provider.id != HOME_SPEECH_ID,
            exists().where(ProviderModel.provider_id == Provider.id),
        )
        .order_by(Provider.created_at, Provider.id)
    )


async def _heir(session: AsyncSession, provider: Provider) -> Provider | None:
    """The key that becomes the general one when ``provider``, the general key, goes: the oldest
    other key its giver gave that lists a model (after the owner's role was handed over, the
    general key is still the key of the one who gave it)."""
    found = await session.execute(
        _working_keys_but(provider).where(Provider.given_by == provider.given_by)
    )
    return found.scalars().first()


async def _warnings(session: AsyncSession, provider: Provider) -> frozenset[RemovalWarning]:
    """What removing ``provider`` warns about: it is the last key of the household that works
    (``last_key``), or the general key with no other key of its giver's to hand it to
    (``general_key_without_heir``). Neither stops it: they are facts for the judge."""
    found: set[RemovalWarning] = set()
    other = await session.execute(_working_keys_but(provider).limit(1))
    if other.first() is None:
        found.add("last_key")
    if provider.is_general and await _heir(session, provider) is None:
        found.add("general_key_without_heir")
    return frozenset(found)


def _warned_of(ctx: ToolContext, now: frozenset[RemovalWarning]) -> frozenset[RemovalWarning]:
    """What the yes, or the judge, was given to know: the facts of the action described when the
    request was made (``ToolContext.described_action``), else, for a call that runs right after
    it was described, what holds as it starts (``now``)."""
    described = ctx.described_action
    if described is None:
        return now
    return frozenset(fact for fact in _WARNINGS if described.facts.get(fact) is True)


def _remove_args(arguments: JsonObject) -> str:
    if set(arguments) - {"key"}:
        raise InvalidArguments("invalid", "key_remove takes only key")
    key_id = key_named(arguments, "key_remove", required=True)
    assert key_id is not None
    return key_id


async def _declare_routes(
    gatekeeper: GatekeeperClient, provider: Provider, *, keyless: bool
) -> None:
    """Declare the routes of ``provider`` (``<id>`` and ``<id>-admin``) to the gatekeeper: without
    a secret (``keyless``), or again with the ones they had."""
    await gatekeeper.register_provider(
        ProviderRoute(
            id=provider.id,
            base_url=provider.base_url,
            secret_ref=None if keyless else provider.secret_ref,
        )
    )
    await gatekeeper.register_provider(
        ProviderRoute(
            id=admin_route_id(provider.id),
            base_url=provider.base_url,
            secret_ref=None if keyless else provider.admin_secret_ref,
        )
    )


async def _give_back_the_routes(gatekeeper: GatekeeperClient, provider: Provider) -> None:
    """A key that stays gets the secrets of its routes back, as far as the gatekeeper answers (its
    next check declares them again anyway, ``provider_keys.check_key``)."""
    # A refusal is logged where it is raised (``gatekeeper_client``).
    with suppress(GatekeeperError):
        await _declare_routes(gatekeeper, provider, keyless=False)


def _remove_changed() -> ToolOutcome:
    """The key became the last one, or the general key with nobody to take over, after the yes
    or the judge was given what it knew: nothing is removed, and they are asked again."""
    return ToolOutcome(
        status="failed",
        model_result={"reason": "changed", "detail": _BECAME_WARNED},
        card={"key": "action.card.failed.key.remove.changed", "params": {}, "reason": "changed"},
    )


class KeyRemove:
    """Remove one of the person's own keys."""

    definition = ToolDefinition(
        name="key_remove",
        description=(
            "Remove one of the keys the person you are talking with gave (key: its id, from the "
            "Keys line of the context), when that is what they want: only the one who gave a key "
            "removes it. When it is the last key of the household that works, or the household's "
            "general key and they gave no other key that works to take over, tell them first "
            "that you could no longer answer whoever depends on it, and offer to give another "
            "key first; if they still want it, it is removed. Otherwise the general key goes to "
            "another key that works of the one who gave it. The key stays in the vault, unused: "
            "tell them to revoke it at the provider."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The id of the key (p-...), as the Keys line gives it.",
                }
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    )
    action_type = KEY_REMOVE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        key_id = _remove_args(arguments)
        async with ctx.session_factory() as session:
            provider = await _own_key(session, key_id, ctx.person)
            warnings = await _warnings(session, provider)
        return DescribedAction(
            connector="core",
            action_type=KEY_REMOVE_ACTION_TYPE,
            target=_key_target(provider),
            effect="secrets",
            destination="household",
            facts={warning: warning in warnings for warning in _WARNINGS},
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        key_id = _remove_args(arguments)
        async with ctx.session_factory() as session:
            provider = await _own_key(session, key_id, ctx.person)
            _same_as_described(ctx, provider)
            first = await _warnings(session, provider)
        warned = _warned_of(ctx, first)
        if first - warned:
            return _remove_changed()
        # First, no call can use the secret any more: the routes go without it. The vault is
        # asked before the database, so that no transaction waits for it.
        try:
            await _declare_routes(ctx.gatekeeper, provider, keyless=True)
        except GatekeeperError:
            await _give_back_the_routes(ctx.gatekeeper, provider)
            return ToolOutcome(
                status="failed",
                model_result={
                    "reason": "gatekeeper",
                    "detail": "the vault did not answer, so the key was not removed: try again",
                },
                card={"key": "action.card.failed.key.remove.gatekeeper", "params": {}},
            )
        async with ctx.session_factory() as session:
            # The keys are locked: two removals at once never leave the household without a key,
            # or without a general key, unless what was asked or judged said so.
            await session.execute(select(Provider.id).with_for_update())
            still = await session.get(Provider, provider.id, populate_existing=True)
            now = frozenset[RemovalWarning]() if still is None else await _warnings(session, still)
            if still is None or now - warned:
                await session.rollback()
                if still is not None:
                    # It stays: its routes get their secrets back.
                    await _give_back_the_routes(ctx.gatekeeper, still)
                    return _remove_changed()
                raise InvalidArguments("not_found", _NOT_THEIRS)
            heir = await _heir(session, still) if still.is_general else None
            await session.execute(
                delete(ModelChoices).where(ModelChoices.scope == provider_scope(still.id))
            )
            await session.delete(still)
            await session.flush()
            if heir is not None:
                heir.is_general = True
            # Last, just before the commit.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="key.removed",
                details={"provider_id": provider.id},
            )
            await session.commit()
        # Once more, now that the row is gone: a pass that declared a route with its secret after
        # the first declaration (``provider_keys.key_still_there`` found the row then) is undone.
        # A refusal is logged where it is raised (``gatekeeper_client``).
        with suppress(GatekeeperError):
            await _declare_routes(ctx.gatekeeper, provider, keyless=True)
        result: JsonObject = {"removed": provider.id, "note": _STILL_IN_THE_VAULT}
        if heir is not None:
            result["general_key_now"] = heir.id
        if "last_key" in now:
            result["no_key_left"] = _NO_KEY_LEFT
        elif "general_key_without_heir" in now:
            result["no_general_key"] = _NO_GENERAL_KEY
        return ToolOutcome(
            status="done",
            model_result=result,
            card={"key": "action.card.done.key.remove", "params": {}},
        )


# --- key_share -----------------------------------------------------------------------------------


def _share_args(arguments: JsonObject) -> tuple[str, str, str | None, bool]:
    """``key_share``'s call: the key, the scope it writes, the name of the person (for
    ``with`` ``person``) and whether they may use it. Raises ``InvalidArguments``."""
    if set(arguments) - {"key", "with", "person", "allowed"}:
        raise InvalidArguments("invalid", "key_share takes key, with, person and allowed")
    key_id = key_named(arguments, "key_share", required=True)
    assert key_id is not None
    with_ = arguments.get("with")
    if not isinstance(with_, str) or with_ not in _WITH:
        raise InvalidArguments("invalid", f"with must be one of: {', '.join(_WITH)}")
    name = arguments.get("person")
    if with_ == "person":
        if not isinstance(name, str) or not name.strip() or not is_plain_line(name):
            raise InvalidArguments("invalid", "with person goes with person, their name")
    elif name is not None:
        raise InvalidArguments("invalid", "person is only for with person")
    allowed = arguments.get("allowed", True)
    if not isinstance(allowed, bool):
        raise InvalidArguments("invalid", "allowed is true or false")
    return key_id, _SCOPE_OF[with_], name if isinstance(name, str) else None, allowed


async def _shared_with(
    session: AsyncSession, key_id: str, name: str | None, ctx: ToolContext
) -> tuple[Provider, UUID | None]:
    provider = await _own_key(session, key_id, ctx.person)
    if name is None:
        return provider, None
    return provider, (
        await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
    ).id


class KeyShare:
    """Say who else may use one of the person's own keys."""

    definition = ToolDefinition(
        name="key_share",
        description=(
            "Say whether others may use one of the keys the person you are talking with gave "
            "(key: its id, from the Keys line of the context), when that is what they want: one "
            "person (with person, and person their name), the people they invited "
            "(my_invitees) or the whole household (household); allowed false takes it away. "
            "Only the one who gave a key says who uses it. It replaces what was said before for "
            "the same people."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The id of the key (p-...), as the Keys line gives it.",
                },
                "with": {"type": "string", "enum": list(_WITH), "description": "Who."},
                "person": {
                    "type": "string",
                    "description": "The name of the person, for with person.",
                },
                "allowed": {
                    "type": "boolean",
                    "description": "Whether they may use it (true by default).",
                },
            },
            "required": ["key", "with"],
            "additionalProperties": False,
        },
    )
    action_type = KEY_SHARE_ACTION_TYPE
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        key_id, _, name, _ = _share_args(arguments)
        async with ctx.session_factory() as session:
            provider, person_id = await _shared_with(session, key_id, name, ctx)
        return DescribedAction(
            connector="core",
            action_type=KEY_SHARE_ACTION_TYPE,
            target=_for_whom_target(provider, person_id),
            effect="permissions",
            destination="household",
            facts=_key_facts(provider, ctx.person),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        key_id, scope, name, allowed = _share_args(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            provider, person_id = await _shared_with(session, key_id, name, ctx)
            _same_for_whom(ctx, provider, person_id)
            statement = insert(ProviderUser).values(
                provider_id=provider.id,
                scope=scope,
                person_id=person_id,
                allowed=allowed,
                set_by=ctx.person.id,
                set_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_provider_users_provider_scope_person",
                    set_={"allowed": allowed, "set_by": ctx.person.id, "set_at": now},
                )
            )
            # Last, just before the commit.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="key.shared",
                details={
                    "provider_id": provider.id,
                    "scope": scope,
                    "person": None if person_id is None else str(person_id),
                    "allowed": allowed,
                },
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"key": provider.id, "with": scope, "allowed": allowed},
            card={"key": "action.card.done.key.share", "params": {}},
        )


# --- the arguments of a limit (key_limit, keys_house) --------------------------------------------


@dataclass(frozen=True)
class LimitAsked:
    """A firm limit a call asks for: ``amount`` ``currency`` per ``period``, with the threshold of
    the one who sets it (``warn_at_percent``, ``None`` for none); ``amount`` ``None`` asks for the
    limit to be taken away. ``role`` and ``split`` say it is for a role (both or neither)."""

    amount: Decimal | None
    currency: Currency = "USD"
    period: Period = "month"
    warn_at_percent: int | None = None
    role: LimitRole | None = None
    split: LimitSplit | None = None

    def told(self, changed: bool) -> JsonObject:
        """What the model is told of it once it is done (``changed`` false: there was no limit to
        take away)."""
        told: JsonObject = {}
        if self.role is not None:
            told = {"role": self.role, "split": self.split}
        if self.amount is None:
            if changed:
                return {**told, "removed": True}
            return {**told, "removed": False, "note": "there was none"}
        told |= {
            "amount": amount_text(self.amount),
            "currency": self.currency,
            "period": self.period,
        }
        if self.warn_at_percent is not None:
            told["warn_at_percent"] = self.warn_at_percent
        return told

    def card_key(self, changed: bool) -> str:
        """The card of a limit set, taken away, or that there was none to take away."""
        if self.amount is not None:
            return "action.card.done.spending.limit"
        if changed:
            return "action.card.done.spending.limit.removed"
        return "action.card.done.spending.limit.none"


def _limit_amount(value: object, name: str) -> Decimal:
    """A limit's amount: a number from ``LIMIT_STEP`` to ``MAX_LIMIT_AMOUNT`` (above 0: the
    schema says so, and this holds it), kept to four decimals (``kept_amount``).
    ``InvalidArguments("invalid")`` otherwise."""
    wrong = InvalidArguments(
        "invalid", f"{name} is a number from {LIMIT_STEP} to {MAX_LIMIT_AMOUNT}"
    )
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise wrong
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise wrong from None
    if not amount.is_finite() or not LIMIT_STEP <= amount <= MAX_LIMIT_AMOUNT:
        raise wrong
    return kept_amount(amount)


def one_of[T: str](value: object, known: tuple[T, ...], name: str) -> T:
    for each in known:
        if value == each:
            return each
    raise InvalidArguments("invalid", f"{name} is one of: {', '.join(known)}")


def _threshold(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise InvalidArguments("invalid", "warn_at_percent is a whole number from 1 to 100")
    return value


def _limit_role(value: object, name: str) -> LimitRole:
    for each in LIMIT_ROLES:
        if value == each:
            return each
    raise InvalidArguments("invalid", f"{name} is one of: {', '.join(LIMIT_ROLES)}")


def _limit_split(value: object, name: str) -> LimitSplit:
    for each in LIMIT_SPLITS:
        if value == each:
            return each
    raise InvalidArguments("invalid", f"{name} is one of: {', '.join(LIMIT_SPLITS)}")


def limit_asked(
    arguments: JsonObject,
    *,
    amount: str,
    currency: str,
    period: str,
    remove: str,
    role: str,
    split: str,
) -> LimitAsked | None:
    """The limit a call asks for, read from its arguments under the names its tool gives them
    (``amount``, ``currency``, ``period``, ``remove``, ``role``, ``split`` and
    ``warn_at_percent``): ``None`` when it asks none. ``InvalidArguments("invalid")`` for anything
    that makes no sense: an amount without its currency, a currency, a period or a threshold
    without an amount, a removal with them, a role without its split (or the split without its
    role), or either of them without an amount or a removal. (That a role is not for a person is
    for the tool to say: it is the one that has a person.)"""
    removing = arguments.get(remove, False)
    if not isinstance(removing, bool):
        raise InvalidArguments("invalid", f"{remove} is true or false")
    given = arguments.get(amount)
    asked_role = arguments.get(role)
    asked_split = arguments.get(split)
    if (asked_role is None) != (asked_split is None):
        raise InvalidArguments("invalid", f"{role} goes with {split}, and {split} with {role}")
    role_asked: LimitRole | None = None
    split_asked: LimitSplit | None = None
    if asked_role is not None:
        role_asked = _limit_role(asked_role, role)
        split_asked = _limit_split(asked_split, split)
    if role_asked is not None and given is None and not removing:
        raise InvalidArguments(
            "invalid", f"{role} and {split} go with {amount}, or with {remove} true"
        )
    with_it = [
        name for name in (currency, period, "warn_at_percent") if arguments.get(name) is not None
    ]
    if removing:
        if given is not None or with_it:
            raise InvalidArguments("invalid", f"{remove} goes alone, without {amount}")
        return LimitAsked(None, role=role_asked, split=split_asked)
    if given is None:
        if with_it:
            raise InvalidArguments(
                "invalid", f"{currency}, {period} and warn_at_percent go with {amount}"
            )
        return None
    if arguments.get(currency) is None:
        raise InvalidArguments("invalid", f"{amount} goes with {currency}")
    asked_period = arguments.get(period)
    return LimitAsked(
        amount=_limit_amount(given, amount),
        currency=one_of(arguments.get(currency), CURRENCIES, currency),
        period="month" if asked_period is None else one_of(asked_period, PERIODS, period),
        warn_at_percent=_threshold(arguments.get("warn_at_percent")),
        role=role_asked,
        split=split_asked,
    )


def limit_parameters(
    *, amount: str, currency: str, period: str, remove: str, role: str, split: str
) -> JsonObject:
    """The parameters of a limit, as a tool declares them under its own names."""
    return {
        amount: {
            "type": "number",
            # ``minimum``, never ``exclusiveMinimum``: every provider reads it, as it is given.
            "minimum": float(LIMIT_STEP),
            "maximum": int(MAX_LIMIT_AMOUNT),
            "description": "The most it may spend in the period.",
        },
        currency: {
            "type": "string",
            "enum": list(CURRENCIES),
            "description": "The currency of the amount, the one the person speaks of.",
        },
        period: {
            "type": "string",
            "enum": list(PERIODS),
            "description": "The period of the limit, in the household's time (month if they do "
            "not say).",
        },
        "warn_at_percent": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Only if they ask to be told: the share of the limit spent at which "
            "you tell them.",
        },
        role: {
            "type": "string",
            "enum": list(LIMIT_ROLES),
            "description": "A limit for a role instead of one person: each adult, child or guest "
            "(with split), now and later.",
        },
        split: {
            "type": "string",
            "enum": list(LIMIT_SPLITS),
            "description": "With the role: each (every one of them has the amount) or together "
            "(the amount is shared).",
        },
        remove: {
            "type": "boolean",
            "description": "Take the limit away (with the role and split for a limit of a role).",
        },
    }


# --- key_limit -----------------------------------------------------------------------------------


_LIMIT_ARGUMENTS: Final = frozenset(
    {"key", "person", "amount", "currency", "period", "warn_at_percent", "remove", "role", "split"}
)
_NOT_YOURS_TO_LIMIT: Final = (
    "only the one who gave a key limits what it spends (the Keys line of the context says which "
    "are theirs); the limits of the household's general key are keys_house's"
)


def _key_limit_args(arguments: JsonObject) -> tuple[str, str | None, LimitAsked]:
    """``key_limit``'s call: the key, the name of the one person the limit is for (``None``: the
    whole key) and the limit. Raises ``InvalidArguments``."""
    if set(arguments) - _LIMIT_ARGUMENTS:
        raise InvalidArguments("invalid", f"key_limit takes {', '.join(sorted(_LIMIT_ARGUMENTS))}")
    key_id = key_named(arguments, "key_limit", required=True)
    assert key_id is not None
    name = arguments.get("person")
    if name is not None and (
        not isinstance(name, str) or not name.strip() or not is_plain_line(name)
    ):
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    asked = limit_asked(
        arguments,
        amount="amount",
        currency="currency",
        period="period",
        remove="remove",
        role="role",
        split="split",
    )
    if asked is None:
        raise InvalidArguments(
            "invalid", "say the limit (amount and currency), or remove true to take it away"
        )
    if asked.role is not None and name is not None:
        raise InvalidArguments("invalid", "a limit is for a person or for a role, not both")
    return key_id, name if isinstance(name, str) else None, asked


async def _limited_key(session: AsyncSession, key_id: str, person: Person) -> Provider:
    """The key ``key_id``, if ``person`` gave it: ``not_found`` for an id that names no key,
    ``not_possible`` for a key someone else gave."""
    provider = await session.get(Provider, key_id) if _is_key_id(key_id) else None
    if provider is None:
        raise InvalidArguments("not_found", "no key has that id (the Keys line of the context)")
    if provider.given_by != person.id:
        raise InvalidArguments("not_possible", _NOT_YOURS_TO_LIMIT)
    return provider


class KeyLimit:
    """Set, change or take away the firm limit of one of the person's own keys."""

    definition = ToolDefinition(
        name="key_limit",
        description=(
            "Set a firm limit on what one of the keys the person you are talking with gave may "
            "spend (key: its id, from the Keys line of the context), when that is what they "
            "want: an amount in a currency per period, a calendar month unless they say "
            "otherwise, for everyone, for one person (person, their name) or for a role (role "
            "and split: each adult, child or guest, now and later). You never make a call that "
            "would pass it. warn_at_percent only if they ask to be told when that "
            "share is spent. remove true takes the limit away. It replaces the limit set before "
            "for the same people. Only the one who gave a key limits it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The id of the key (p-...), as the Keys line gives it.",
                },
                "person": {
                    "type": "string",
                    "description": (
                        "The name of the one person the limit is for. With no person and no "
                        "role, the limit is for the whole key."
                    ),
                },
                **limit_parameters(
                    amount="amount",
                    currency="currency",
                    period="period",
                    remove="remove",
                    role="role",
                    split="split",
                ),
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    )
    action_type = SPENDING_LIMIT_ACTION_TYPE
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        key_id, name, _ = _key_limit_args(arguments)
        async with ctx.session_factory() as session:
            provider = await _limited_key(session, key_id, ctx.person)
            person = (
                None
                if name is None
                else await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
            )
        return DescribedAction(
            connector="core",
            action_type=SPENDING_LIMIT_ACTION_TYPE,
            target=_for_whom_target(provider, None if person is None else person.id),
            effect="permissions",
            destination="household",
            facts=_key_facts(provider, ctx.person),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        key_id, name, asked = _key_limit_args(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            provider = await _limited_key(session, key_id, ctx.person)
            person = (
                None
                if name is None
                else await resolve_person(session, name, now=now, speaker=ctx.person.id)
            )
            _same_for_whom(ctx, provider, None if person is None else person.id)
            changed = await set_spending_limit(
                session,
                provider_id=provider.id,
                person_id=None if person is None else person.id,
                amount=asked.amount,
                currency=asked.currency,
                period=asked.period,
                warn_at_percent=asked.warn_at_percent,
                by=ctx.person.id,
                now=now,
                role=asked.role,
                split=asked.split,
            )
            await session.commit()
        told: JsonObject = {"key": provider.id}
        if asked.role is None:
            # A limit for a role says whom it is for with its role and split (``asked.told``).
            told["for"] = "everyone" if person is None else person.display_name
        return ToolOutcome(
            status="done",
            model_result={**told, **asked.told(changed)},
            card={"key": asked.card_key(changed), "params": {}},
        )
