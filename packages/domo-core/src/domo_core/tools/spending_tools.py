"""The tools of what Domo costs (0.6.0, spec 4.8): what was spent, and the admin key that reads
the real spending.

``spending_show`` (``spending.show``, effect ``read``, everyone's, the judge not asked: it changes
nothing) tells what the person cost over a period (``period``: the day, the week or the calendar
month, at the household's hour; the month by default), and, for the keys they gave, what each
person cost on them (``per_person``): amounts only, never a conversation. With ``person``, it tells
someone else's: themselves, or a person who may use a key they gave, or used one in the period,
and then only on those keys (``not_possible`` for anyone else, ``not_found`` for a name that means
nobody). The figures are ``llm.real_spend.spent_by``'s: the providers' own where a key's admin key
told them (``real_usd``), else Domo's estimate corrected by the key's factor; ``estimated`` says
whether any of it is an estimate, ``unknown_prices`` whether a call was counted at a fall-back
price (``model_calls.price_unknown``), and the euros are at the latest rate
(``spending.usd_per``).

``key_admin`` (``key.admin``, effect ``secrets``, every member's on their own key, an adult agrees
for a child) opens the vault form of the admin key of one of the person's own keys, Anthropic or
OpenAI (``llm.real_spend.request_admin_key``), shown once under the card as for ``model_rekey``
(``ToolOutcome.card_once``), never stored and never given to the model. A key that is not theirs
is ``not_found``; a kind that gives no spending to a key (Google, a server of one's own) is
``not_possible``: it stays an estimate. When someone else said yes, when no page reads the turn,
or in a chain (``ToolContext.leave_the_form_on_their_card``, which also holds
on a channel that cannot open a safe screen), nothing is asked of the vault: the card of the
one who asked has the button that opens the form (``"admin_key"``, ``KEY_ADMIN_FORM_PATH``), under
that yes, once (``channels.page.key_routes``). Once the form is filled in, the page that follows it
(``KEY_ADMIN_STATUS_PATH``) declares the admin route and reads the real spending a first time;
the upkeep reads it again once a day (``upkeep.real_spend_pass``). The form is audited
``secret.requested`` (the reference, never the form's path).
"""

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from urllib.parse import quote, urlencode
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.household.service import resolve_person
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.models import Provider, ProviderUser
from domo_core.llm.provider_keys import (
    GatekeeperFailure,
    gatekeeper_failure,
    key_form,
    provider_kind,
)
from domo_core.llm.real_spend import (
    Spent,
    admin_ref,
    real_spend_supported,
    request_admin_key,
    spent_by,
)
from domo_core.llm.router import allowed, household_people
from domo_core.llm.spending import PERIODS, Period, amount_text, period_start, usd_per
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.text import MAX_DISPLAY_NAME_LENGTH, is_plain_line
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.model_key import (
    KEY_ADMIN_FORM_PATH,
    KEY_ADMIN_STATUS_PATH,
    REKEY_REQUEST_PARAM,
    key_named,
    vault_form_shown,
)

SPENDING_SHOW_ACTION_TYPE: Final = "spending.show"
KEY_ADMIN_ACTION_TYPE: Final = "key.admin"

_PROVIDER_ID: Final = re.compile(r"p-[0-9a-f]{8}")
# Amounts are told to the ten-thousandth of their currency.
_SHOWN_STEP: Final = Decimal("0.0001")

_NOT_THEIRS: Final = (
    "the person gave no key with that id: only the one who gave a key gives its admin key (the "
    "Keys line of the context says which are theirs)"
)
_NOT_THE_SAME: Final = (
    "that key is no longer the one the yes was given for, or no longer theirs, so nothing was "
    "done: ask again"
)
_NO_REAL_SPEND: Final[Mapping[str, str]] = {
    "google": "Google gives no spending to a key: estimates only",
}
_NO_REAL_SPEND_ELSE: Final = "this kind of key gives no spending to an admin key: estimates only"
_NOT_YOURS_TO_SEE: Final = (
    "that person uses no key the person you are talking with gave: what they cost is not theirs "
    "to see"
)
_FAILURES: Final[Mapping[GatekeeperFailure, str]] = {
    "gatekeeper": "the secret vault did not open a form; try again in a moment",
    "binding_conflict": "the vault keeps an admin key of that name for other destinations",
    "insecure_transport": "the vault will not send a key to this server without https",
}


def key_admin_status_url(provider_id: str, request_id: str) -> str:
    """The address that follows the form ``request_id`` of the admin key of ``provider_id``."""
    return f"{KEY_ADMIN_STATUS_PATH}/{provider_id}/{quote(request_id, safe='')}"


def admin_key_action(provider: Provider, giver_name: str) -> DescribedAction:
    """The action of giving the admin key of ``provider`` (given by ``giver_name``), as the barrier
    is asked about it: by the tool (``KeyAdmin.describe``) and by the route of its button
    (``channels.page.key_routes``). Whoever says yes reads which key: its kind and who gave it."""
    return DescribedAction(
        connector="core",
        action_type=KEY_ADMIN_ACTION_TYPE,
        target=f"provider:{provider.id}",
        effect="secrets",
        destination="household",
        facts={"key_kind": provider.kind, "key_of": giver_name},
    )


def own_admin_button_of(card: Mapping[str, object]) -> str | None:
    """Where a button on a card opens the form of the admin key of the key the card names
    (``"admin_key"``), with no request in it (a chain that ran at once: the route asks the
    barrier for whoever posts). A page never posts anywhere else."""
    provider_id = card.get("admin_key")
    if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
        return None
    return KEY_ADMIN_FORM_PATH.format(provider_id=provider_id)


def admin_button_of(card: Mapping[str, object], confirmation_id: UUID | None) -> str | None:
    """Where a button on the card of the request ``confirmation_id`` opens the form of the admin
    key the card names, for the yes given to that request."""
    path = own_admin_button_of(card)
    if confirmation_id is None or path is None:
        return None
    return f"{path}?{urlencode({REKEY_REQUEST_PARAM: str(confirmation_id)})}"


def no_real_spend(provider: Provider) -> str | None:
    """Why the key ``provider`` has no real spending to read (``None`` when it has)."""
    kind = provider_kind(provider.kind)
    if kind is not None and real_spend_supported(kind):
        return None
    return _NO_REAL_SPEND.get(provider.kind, _NO_REAL_SPEND_ELSE)


# --- spending_show -------------------------------------------------------------------------------


def _show_args(arguments: JsonObject) -> tuple[Period, str | None]:
    if set(arguments) - {"period", "person"}:
        raise InvalidArguments("invalid", "spending_show takes period and person")
    asked: object = arguments.get("period", "month")
    period: Period | None = next((known for known in PERIODS if known == asked), None)
    if period is None:
        raise InvalidArguments("invalid", f"period must be one of: {', '.join(PERIODS)}")
    name = arguments.get("person")
    if name is not None and (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_DISPLAY_NAME_LENGTH
        or not is_plain_line(name)
    ):
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    return period, name if isinstance(name, str) else None


async def _keys_given_by(session: AsyncSession, person_id: UUID) -> list[Provider]:
    found = await session.execute(
        select(Provider).where(Provider.given_by == person_id).order_by(Provider.created_at)
    )
    return list(found.scalars().all())


async def _may_see(
    session: AsyncSession, keys: list[Provider], person: Person, since: datetime
) -> bool:
    """Whether ``person`` may use one of ``keys``, or used one since ``since``."""
    if not keys:
        return False
    lines = await session.execute(
        select(ProviderUser).where(ProviderUser.provider_id.in_([key.id for key in keys]))
    )
    users = list(lines.scalars().all())
    people = await household_people(session)
    if any(allowed(key, users, person, people=people) for key in keys):
        return True
    used = await session.execute(
        select(
            exists().where(
                ModelCall.person_id == person.id,
                ModelCall.provider_id.in_([key.id for key in keys]),
                ModelCall.started_at >= since,
            )
        )
    )
    return bool(used.scalar_one())


async def _subject(
    session: AsyncSession, ctx: ToolContext, name: str | None, period: Period
) -> tuple[Person, bool]:
    """Whose spending a call asks for, and whether it is someone else's (then only on the keys the
    person gave). Raises ``InvalidArguments``."""
    if name is None:
        return ctx.person, False
    now = ctx.clock()
    found = await resolve_person(session, name, now=now, speaker=ctx.person.id)
    if found.id == ctx.person.id:
        return ctx.person, False
    settings = await load_policy_settings(session)
    since = period_start(now, settings.time_zone, period)
    if not await _may_see(session, await _keys_given_by(session, ctx.person.id), found, since):
        raise InvalidArguments("not_possible", _NOT_YOURS_TO_SEE)
    return found, True


def _money(amount: Decimal) -> str:
    """An amount as it is told: to the ten-thousandth, the nearest (``0.5``, never ``5E-1``)."""
    return amount_text(amount.quantize(_SHOWN_STEP, ROUND_HALF_UP))


class SpendingShow:
    """Tell what Domo cost: the person's own spending, and person by person on their keys."""

    definition = ToolDefinition(
        name="spending_show",
        description=(
            "Tell what the assistant cost the person you are talking with over a period (period: "
            "day, "
            "week or month, the calendar month by default), and, for the keys they gave, what "
            "each person cost on them (per_person). With person, someone else's: only for "
            "someone who uses a key they gave, and only on those keys. It tells amounts, never "
            "what anyone said. estimate_usd is the best figure: the providers' own where an "
            "admin key told them (real_usd), else your estimate, corrected by what the real bill "
            "taught you; estimated says whether any of it is an estimate, unknown_prices whether "
            "a model was counted at a fallback price. An estimate is not the real bill: always "
            "say which one you give, in the person's currency (estimate_eur for euros). For the "
            "real spending, an admin key is needed (key_admin)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": list(PERIODS),
                    "description": "The day, the week or the month so far (month by default).",
                },
                "person": {
                    "type": "string",
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                    "description": "The name of someone else of the household, if asked.",
                },
            },
            "additionalProperties": False,
        },
    )
    action_type = SPENDING_SHOW_ACTION_TYPE
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        period, name = _show_args(arguments)
        async with ctx.session_factory() as session:
            subject, _ = await _subject(session, ctx, name, period)
        return DescribedAction(
            connector="core",
            action_type=SPENDING_SHOW_ACTION_TYPE,
            target=None if name is None else f"person:{subject.id}",
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        period, name = _show_args(arguments)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            subject, someone_else = await _subject(session, ctx, name, period)
            settings = await load_policy_settings(session)
            since = period_start(now, settings.time_zone, period)
            given = [key.id for key in await _keys_given_by(session, ctx.person.id)]
            theirs = (
                await spent_by(
                    session,
                    since=since,
                    person_id=subject.id,
                    keys=given if someone_else else None,
                )
            ).get(subject.id, Spent())
            per = await spent_by(session, since=since, keys=given) if given else {}
            named = await session.execute(
                select(Person.id, Person.display_name).where(
                    Person.id.in_([who for who in per if who is not None])
                )
            )
            names = dict(named.all())
            usd_per_eur = await usd_per(session, "EUR", await current_catalog(session))
        result: JsonObject = {
            "period": period,
            "estimate_usd": _money(theirs.usd),
            "estimate_eur": _money(theirs.usd / usd_per_eur),
        }
        if theirs.real_usd is not None:
            result["real_usd"] = _money(theirs.real_usd)
        result["estimated"] = theirs.estimated
        result["unknown_prices"] = theirs.unknown_prices or any(
            spent.unknown_prices for spent in per.values()
        )
        lines = sorted(
            (
                (names[who], spent.usd)
                for who, spent in per.items()
                if who is not None and who in names
            ),
            key=lambda line: (-line[1], line[0]),
        )
        result["per_person"] = [
            {"name": display_name, "estimate_usd": _money(usd)} for display_name, usd in lines
        ]
        return ToolOutcome(
            status="done",
            model_result=result,
            card={"key": "action.card.done.spending.show", "params": {}},
        )


# --- key_admin -----------------------------------------------------------------------------------


def _admin_args(arguments: JsonObject) -> str:
    if set(arguments) - {"key"}:
        raise InvalidArguments("invalid", "key_admin takes only key")
    key_id = key_named(arguments, "key_admin", required=True)
    assert key_id is not None
    return key_id


async def _own_key(ctx: ToolContext, key_id: str) -> Provider:
    """The key ``key_id`` if the person gave it, with real spending to read; ``not_found`` for a
    key that is not theirs, ``not_possible`` for one whose provider tells none."""
    async with ctx.session_factory() as session:
        provider = await session.get(Provider, key_id) if _PROVIDER_ID.fullmatch(key_id) else None
    if provider is None or provider.given_by != ctx.person.id:
        raise InvalidArguments("not_found", _NOT_THEIRS)
    refusal = no_real_spend(provider)
    if refusal is not None:
        raise InvalidArguments("not_possible", refusal)
    return provider


def _failed(failure: GatekeeperFailure) -> ToolOutcome:
    """The vault did not open a form: nothing was asked of the person."""
    return ToolOutcome(
        status="failed",
        model_result={"reason": failure, "detail": _FAILURES[failure]},
        card={"key": f"action.card.failed.key.admin.{failure}", "params": {}, "reason": failure},
    )


class KeyAdmin:
    """Open a form where the person types the admin key of one of their keys."""

    definition = ToolDefinition(
        name="key_admin",
        description=(
            "Have the person you are talking with give the admin key of one of the keys they "
            "gave (key: its id, from the Keys line of the context), for Anthropic or OpenAI, "
            "when they want the real spending: with it, you read what the provider says the key "
            "really spent, each day, and your estimate is corrected. An admin key is made in the "
            "provider's console, for an organization (not for a personal account). Google gives "
            "no spending to a key: there it stays an estimate. They type it in a safe form, "
            "where the context says a key is typed (say where): it goes from them straight into "
            "the encrypted vault, you never see it, so never ask for it in the conversation. The "
            "result says whether it was done, waits for a yes, or was not done. If someone else "
            "said yes, or in a chain, the form waits until the person who asked opens it."
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
    action_type = KEY_ADMIN_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        provider = await _own_key(ctx, _admin_args(arguments))
        return admin_key_action(provider, ctx.person.display_name)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        key_id = _admin_args(arguments)
        described = ctx.described_action
        try:
            provider = await _own_key(ctx, key_id)
        except InvalidArguments as error:
            if described is None or error.code == "not_possible":
                raise
            raise InvalidArguments("not_found", _NOT_THE_SAME) from None
        if described is not None and described.target != f"provider:{provider.id}":
            raise InvalidArguments("not_found", _NOT_THE_SAME)
        if ctx.leave_the_form_on_their_card:
            # The form is for the one who asked, and no form is opened that nobody would see:
            # their card has the button that opens it. The card keeps the key's id, no secret.
            return ToolOutcome(
                status="done",
                model_result={"admin_form": "waits_for_them"},
                card={
                    "key": ctx.form_left_card("key.admin"),
                    "params": {},
                    "admin_key": provider.id,
                },
            )
        async with ctx.session_factory() as reader:
            # In the words of the one who gives the key, never the turn's (a wake's are the
            # reference texts).
            form = await key_form(reader, ctx.person, writing=ctx.text_writing)
        try:
            view = await request_admin_key(ctx.gatekeeper, provider, form)
        except GatekeeperError as error:
            return _failed(gatekeeper_failure(error))
        shown = vault_form_shown(view, key_admin_status_url(provider.id, view.id))
        if shown is None:
            # An answer of the vault that the page would not show: no form.
            return _failed("gatekeeper")
        form_path, status_url = shown
        async with ctx.session_factory() as session:
            # Last, just before the commit: the reference, never the form's path.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="secret.requested",
                details={"ref": admin_ref(provider.id)},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"admin_form_shown": True},
            card={"key": "action.card.done.key.admin", "params": {}},
            # Shown once, to the one who asked, in the answer: never stored, never told to the
            # model.
            card_once={
                "key": "action.card.once.key.admin",
                "params": {},
                "form_path": form_path,
                "status_url": status_url,
            },
        )
