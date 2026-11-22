"""The tool to enter an API key again, from the chat.

``model_rekey`` (judged, every member's: 0.6.0) is about one of the keys the person gave
(``Provider.given_by``): only the one who gave a key gives it again (spec 4.2). ``key`` names it
(its id, as the context's Keys line tells it); with no ``key``, it is the one key with a key in the
vault that the person gave, and with several, Domo is asked which (``ambiguous``). A key of
someone else's, or one that does not exist, is ``not_found``, and so is a key with nothing in the
vault (a server of one's own may have none: then there is no key to enter again). The request is
described with that key as its target; when it runs after a yes, it must still be that key and
still the person's, or nothing is done.

Once the one who asked has said yes (or the barrier let the call through), the gatekeeper opens a
one-time form for the same reference and the same destination as before
(``llm.provider_keys.request_key``: the new value rotates the secret). The form is shown to them
in a frame under the card, with the address that follows it (``channels.page.key_routes``), in the
answer to the yes only (``ToolOutcome.card_once``): the form's path is a one-time token, so it is
never stored (not with the reply, not in the confirmation, not in the audit log or a log) and never
given to the model, which is only told that a form was shown. The key itself never reaches the
core: it goes from the browser to the vault.

When someone else said yes (the owner, to a request of an adult who has the right), the form is
for the one who asked; when no page reads the turn (a wake of Domo after a button), nobody
would see it; a step of a chain never shows it at once; and a channel that cannot open a safe
screen cannot show it (``ToolContext.leave_the_form_on_their_card``):
nothing is asked of the vault yet, their card has the button that opens it (``KEY_FORM_PATH``,
``channels.page.key_routes``), the same one the owner's page has, and the model is told the form
waits for them (``waits_for_them``), never that it was shown. The button carries the identifier of
the request the yes was given to (``REKEY_REQUEST_PARAM``): the route opens the form for it, for
the person who asked, as ``policy.key_forms`` allows (for a chain, from when its step was done). A
chain that ran at once has no request: its button carries none, and the route asks the barrier for
whoever posts.

The request for the form is audited (``secret.requested``: the reference, as at the first
launch); what came of the new key is audited when it is checked (``provider.rekeyed``).
"""

import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote, urlencode
from uuid import UUID

from domo_common.gatekeeper_api import SecretRequestView
from sqlalchemy import select

from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.llm.key_flow import KEY_NEW_STATUS_PATH
from domo_core.llm.models import Provider
from domo_core.llm.provider_keys import (
    VAULT_FORM_PATH,
    GatekeeperFailure,
    gatekeeper_failure,
    key_form,
    request_key,
    vault_form_path,
)
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome

# Where the page follows a form for the model's key (``channels.page.key_routes``):
# ``<KEY_STATUS_PATH>/<provider id>/<request id>``.
KEY_STATUS_PATH: Final = "/chat/key/status"
# Where the page follows the form of the admin key of a key (``tools.spending_tools``).
KEY_ADMIN_STATUS_PATH: Final = "/chat/key/admin/status"
# A form for a key given again, for a new key (``llm.key_flow``), or for an admin key.
_KEY_STATUS_URL = re.compile(
    "(?:"
    + "|".join(
        re.escape(path) for path in (KEY_STATUS_PATH, KEY_NEW_STATUS_PATH, KEY_ADMIN_STATUS_PATH)
    )
    + ")"
    r"/p-[0-9a-f]{8}/[A-Za-z0-9_-]{1,128}"
)
# Where a button opens a new form for the key of a provider (``channels.page.key_routes``), and for
# its admin key.
KEY_FORM_PATH: Final = "/chat/key/{provider_id}/form"
KEY_ADMIN_FORM_PATH: Final = "/chat/key/{provider_id}/admin/form"
# The query of that address on the card of the one who asked, after someone else's yes: the request
# that yes was given to.
REKEY_REQUEST_PARAM: Final = "confirmation"
# The type of action of the tool, as the barrier knows it.
REKEY_ACTION_TYPE: Final = "model.rekey"
_PROVIDER_ID = re.compile(r"p-[0-9a-f]{8}")

_NO_KEY: Final = (
    "that key has no API key in the vault (a server of the household's own, say): there is no "
    "key to enter again"
)
_NONE_OF_THEIRS: Final = (
    "the person gave no key with that id: only the one who gave a key gives it again (the Keys "
    "line of the context says which are theirs)"
)
_SEVERAL: Final = "the person gave several keys: name the one (key, its id from the Keys line)"
_NOT_THE_SAME: Final = (
    "that key is no longer the one the yes was given for, or no longer theirs, so nothing was "
    "done: ask again"
)
_FAILURES: Final[Mapping[GatekeeperFailure, str]] = {
    "gatekeeper": "the secret vault did not open a form; try again in a moment",
    "binding_conflict": "the vault keeps this key for other destinations, which cannot change",
    "insecure_transport": "the vault will not send a key to this server without https",
}


def key_status_url(provider_id: str, request_id: str) -> str:
    """The address that follows the form of the request ``request_id`` for the key of the
    provider ``provider_id``."""
    return f"{KEY_STATUS_PATH}/{provider_id}/{quote(request_id, safe='')}"


def vault_form_shown(view: SecretRequestView, status_url: str) -> tuple[str, str] | None:
    """The path of the form the gatekeeper just opened (``view``) and ``status_url``, the address
    that follows it, if a page may show them (``key_form_of``): the one check every opening of a
    vault form goes through (a key given again, a new key, an admin key)."""
    return key_form_of({"form_path": vault_form_path(view), "status_url": status_url})


def key_form_for(provider: Provider, view: SecretRequestView) -> tuple[str, str] | None:
    """The path of the form the gatekeeper just opened for the key of ``provider`` (``view``) and
    the address that follows it, if a page may show them (``vault_form_shown``)."""
    return vault_form_shown(view, key_status_url(provider.id, view.id))


def key_form_of(card_once: Mapping[str, object]) -> tuple[str, str] | None:
    """The vault form (its path) and the address that follows it that a part of a card shown
    once carries (``"form_path"``, ``"status_url"``), if both are what this tool, ``key_add`` or
    ``key_admin`` puts there: a page never frames, or polls, anything else."""
    form_path = card_once.get("form_path")
    status_url = card_once.get("status_url")
    if not isinstance(form_path, str) or not isinstance(status_url, str):
        return None
    if VAULT_FORM_PATH.fullmatch(form_path) and _KEY_STATUS_URL.fullmatch(status_url):
        return form_path, status_url
    return None


def rekey_action(provider: Provider) -> DescribedAction:
    """The action of entering the key of ``provider`` again, as the barrier is asked about it: by
    the tool (``ModelRekey.describe``), and by the route of the button
    (``channels.page.key_routes``)."""
    # The provider's id, never its key: the target is audited.
    return DescribedAction(
        connector="core",
        action_type=REKEY_ACTION_TYPE,
        target=f"provider:{provider.id}",
        effect="secrets",
        destination="household",
    )


def rekey_button_of(card: Mapping[str, object], confirmation_id: UUID | None) -> str | None:
    """The address a button on a card posts to, to open a new form for the model's key, if the
    card names the provider whose key it is for (``"rekey"``) and is the card of a request
    (``confirmation_id``, which the address carries: the route opens the form for that request's
    yes). A page never posts anywhere else."""
    path = own_rekey_button_of(card)
    if confirmation_id is None or path is None:
        return None
    return f"{path}?{urlencode({REKEY_REQUEST_PARAM: str(confirmation_id)})}"


def own_rekey_button_of(card: Mapping[str, object]) -> str | None:
    """The address a button on a card posts to, to open a new form for the key of the provider the
    card names (``"rekey"``), with no request in it: a chain that ran at once has no request whose
    yes the form would be for, so the route asks the barrier for whoever posts, as for the owner's
    own button (``channels.page.key_routes``). A page never posts anywhere else."""
    provider_id = card.get("rekey")
    if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
        return None
    return KEY_FORM_PATH.format(provider_id=provider_id)


def key_named(arguments: JsonObject, tool: str, *, required: bool) -> str | None:
    """The id of the key a call names (``key``), if it names one; ``InvalidArguments`` for a value
    that is no text (or none, when it is ``required``)."""
    if "key" not in arguments and not required:
        return None
    value = arguments.get("key")
    if not isinstance(value, str):
        raise InvalidArguments("invalid", f"{tool} takes key, the id of a key, as a text")
    return value


def _key_of(arguments: JsonObject) -> str | None:
    if set(arguments) - {"key"}:
        raise InvalidArguments("invalid", "model_rekey takes only key")
    return key_named(arguments, "model_rekey", required=False)


async def _keyed_provider(ctx: ToolContext, key_id: str | None) -> Provider:
    """The key the person gave that ``key_id`` names, or their one key with a key in the vault
    when it names none."""
    async with ctx.session_factory() as session:
        found = await session.execute(
            select(Provider)
            .where(Provider.given_by == ctx.person.id)
            .order_by(Provider.created_at, Provider.id)
        )
        theirs = list(found.scalars().all())
    if key_id is not None:
        named = [key for key in theirs if key.id == key_id]
        if not named:
            raise InvalidArguments("not_found", _NONE_OF_THEIRS)
        if named[0].secret_ref is None:
            raise InvalidArguments("not_found", _NO_KEY)
        return named[0]
    keyed = [key for key in theirs if key.secret_ref is not None]
    if len(keyed) > 1:
        raise InvalidArguments("ambiguous", _SEVERAL)
    if not keyed:
        raise InvalidArguments("not_found", _NO_KEY if theirs else _NONE_OF_THEIRS)
    return keyed[0]


def _failed(failure: GatekeeperFailure) -> ToolOutcome:
    """The vault did not open a form: nothing was asked of the owner."""
    return ToolOutcome(
        status="failed",
        model_result={"reason": failure, "detail": _FAILURES[failure]},
        card={"key": f"action.card.failed.model.rekey.{failure}", "params": {}, "reason": failure},
    )


class ModelRekey:
    """Open a form where the person types the model's API key again."""

    definition = ToolDefinition(
        name="model_rekey",
        description=(
            "Have the person you are talking with type again the API key of one of the keys "
            "they gave (a key that no longer works, say), when that is what they want: only the "
            "one who gave a key gives it again. key is its id, from the Keys line of the "
            "context; it is needed when they gave several. The result says whether it was done, "
            "waits for a yes, or was not done. They type it in a safe form, where the context "
            "says a key is typed (say where): it goes from them straight into the encrypted "
            "vault, you never see it, so never ask for it in the conversation. If "
            "someone else said yes, or in a chain, the form waits until the person who asked "
            "opens it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The id of the key (p-...), as the Keys line gives it.",
                }
            },
            "additionalProperties": False,
        },
    )
    action_type = REKEY_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        return rekey_action(await _keyed_provider(ctx, _key_of(arguments)))

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        key_id = _key_of(arguments)
        described = ctx.described_action
        if described is not None and key_id is None:
            # A stored request: the key it was described with, which must still be theirs.
            key_id = (described.target or "").removeprefix("provider:")
        try:
            provider = await _keyed_provider(ctx, key_id)
        except InvalidArguments:
            if described is None:
                raise
            raise InvalidArguments("not_found", _NOT_THE_SAME) from None
        if described is not None and described.target != f"provider:{provider.id}":
            raise InvalidArguments("not_found", _NOT_THE_SAME)
        if ctx.leave_the_form_on_their_card:
            # The form is for the one who asked, and no form is opened that nobody would see:
            # nothing is asked of the vault until they press the button on their card (the same
            # one the owner's page has). The card keeps the provider's id, which is no secret.
            return ToolOutcome(
                status="done",
                model_result={"key_form": "waits_for_them"},
                card={
                    "key": ctx.form_left_card("model.rekey"),
                    "params": {},
                    "rekey": provider.id,
                },
            )
        async with ctx.session_factory() as reader:
            # In the words of the one who gives the key, never the turn's (a wake's are the
            # reference texts).
            form = await key_form(reader, ctx.person, writing=ctx.text_writing)
        try:
            view = await request_key(ctx.gatekeeper, provider, form)
        except GatekeeperError as error:
            return _failed(gatekeeper_failure(error))
        shown = key_form_for(provider, view)
        if shown is None:
            # An answer of the vault that the page would not show: no form for the owner.
            return _failed("gatekeeper")
        form_path, status_url = shown
        card_once: JsonObject = {
            "key": "action.card.once.model.rekey",
            "params": {},
            "form_path": form_path,
            "status_url": status_url,
        }
        async with ctx.session_factory() as session:
            # Last, just before the commit: the reference, never the form's path.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="secret.requested",
                details={"ref": provider.secret_ref},
            )
            await session.commit()
        return ToolOutcome(
            status="done",
            model_result={"key_form_shown": True},
            card={"key": "action.card.done.model.rekey", "params": {}},
            # Shown once, to the one who asked, in the answer to the yes: never stored, never told
            # to the model.
            card_once=card_once,
        )
