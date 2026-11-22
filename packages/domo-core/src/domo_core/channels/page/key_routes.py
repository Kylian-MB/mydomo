"""The forms for a key, from the chat: one given again, and a new one (0.6.0).

A key given again: ``GET /chat/key/status/{provider_id}/{request_id}`` follows a form, and
``POST /chat/key/{provider_id}/form`` opens a new one. Both are for the one who gave the key
(``Provider.given_by``: only the giver gives a key again, spec 4.2), as long as the right to give
one's key again is theirs (``web.deps.require_action``, ``model.rekey``: every member's, unless the
owner took it away), and 403 for anyone else. A new key is the page ``KEY_NEW_PATH``, after the
part on a key given again.

When the person said yes to ``model_rekey`` (``tools.model_key``), the answer to the yes shows the
gatekeeper's form in a frame, and under it a poll that asks the status route, every two seconds,
where the form stands. When someone else said yes (the owner, to an adult's request), the card of
the one who asked has the "Enter the key" button instead, which opens the form (the route below):
the form is theirs, not the one who said yes's.

Opening a form is not skipped by posting to the route: the owner's choice of who says yes, and
their limits, hold here as for Domo. Who may have a form (without a yes, after someone else's
yes named in the address by ``?confirmation=``, or from the button of a step of a chain) is
``policy.key_forms.may_open_a_form``; the route answers 403 to anyone else.

A new key (``llm.key_flow``): ``GET KEY_NEW_PATH`` shows the same form as the first launch (the
kind of provider, the address of a server of one's own, whether it takes a key), and ``POST
KEY_NEW_PATH`` takes it: the key is the person's (``start_key``), its vault form is shown in its
frame, with its poll (``GET KEY_NEW_STATUS_PATH/{provider_id}/{request_id}``), and once it is
filled in the key is checked (``finish_key``: ``key.added`` once it works). A server without a key
is checked at once. A server that gives no list of its models is asked the name of the one it
serves (``fragments/model_name.html``, ``POST KEY_NEW_PATH/{provider_id}/check``, which also checks
again a server without a key, from the button of its card). A form that ran out, or a key that does
not work, opens a new form for the same new key (``POST KEY_NEW_PATH/{provider_id}/form``). The
routes are for whoever may add a key (``key.add``), and a new key's for the one who gave it; a key
that was not started from the chat (``key.started``) has none of them.

The page is guarded like a form of a key given again (``policy.key_forms.may_add_a_key``). One yes,
one key: the key started under a yes is audited with it (``key.started``), and the page starts no
other one under that yes, nor under a request a replacement links to it (two forms posted at once
start one, under the audit log's lock); only that key's vault form opens again under it.

The routes record what spends a yes: the form opened under it (``secret.requested`` carries
``confirmation`` and the gatekeeper's ``request_id``), and the entry ``provider.rekeyed`` written
when that form's key is saved. When a yes is spent, and which requests are one yes, is
``policy.key_forms``. Until then, a form that ran out unfilled can be opened again, and the "Enter
the key again" button of its answer carries the same request.

* still waiting: the poll again (only the poll: the frame, and the key being typed in it, are
  left alone). So is a gatekeeper that does not answer for a moment (unreachable, or a 5xx), with
  a short note: the form may still be good, and only a definitive state ends it;
* filled in: the key is checked (``llm.provider_keys.check_key``: the gatekeeper is told the route
  again, the models the key opens are listed and the cheapest that calls tools is asked one
  question through it), the entry ``provider.rekeyed`` is
  audited (the provider, the request and what the check said, last, just before the commit: the
  vault holds the new key whatever the check said) and what came of it is shown, in the owner's
  language. This is done once per request: the audit entry is the record of it, and a later
  answer for the same request shows what it says, without asking the model or auditing again;
* over without a key (the time ran out, or a newer form replaced it): it says so, and that Domo
  can open a new one (the key did not change), or the owner, here;
* unknown to the gatekeeper (it lost the request): it says so. Like the other final answers, this
  ends the poll: a 4xx would not (htmx does not swap it), and the poll would go on for good.

A final answer takes the place of the frame and the poll (``HX-Retarget``): nothing polls any
more. A failure is a message with ``data-model-error`` (its kind), never a 500.

A key that is saved but may not work (the provider refused it, or it could not be checked) would
leave the owner with a Domo that cannot answer, and nobody to ask for a new form. So every
answer but a key that works offers a button, "Enter the key again", which opens a new form for
the same key
(``POST /chat/key/{provider_id}/form``); the chat's own error for a key the provider refuses
offers the same button to the owner (``channels.page.routes``). It is the person's own act in the
page, like the first launch's "Enter the key again" and the account page: not an action of Domo,
but it is checked as above (those buttons name no request: they are for whoever the first case lets
through, and someone whose form came from a card's button uses that button again). The new form
is asked for the same reference and the same destination (``request_key``, in the words of the
one who gives the key: ``provider_keys.key_form``), audited (``secret.requested``) and shown in
its frame with its poll: everything after that is the status route's. The form's path is a
one-time token: it is in this answer and nowhere else.

The admin key of one's own key (``tools.spending_tools.key_admin``, Anthropic and OpenAI), with
which the real spending is read, has the same two routes: ``GET KEY_ADMIN_STATUS_PATH/{provider_id}
/{request_id}`` follows its form, and ``POST KEY_ADMIN_FORM_PATH`` opens one (the button of the card
of the one who asked, after someone else's yes or in a chain, and "Enter the key again"), for the
one who gave the key, as long as ``key.admin`` is theirs, guarded as a key given again
(``may_open_a_form`` on the action ``key_admin`` describes: one yes, one admin key). Once its form
is filled in, the key's admin reference is recorded (``Provider.admin_secret_ref``), its admin
route declared and the real spending read a first time (``llm.real_spend.read_real_spend``), once
per form: the entry ``key.admin_added`` (the provider, the request, what the read said, and the
request whose yes it was opened under) is the record of it.

The routes never see the key: only the request's state, which the gatekeeper gives without its
form. A request is only followed for the provider in the address, which must have a key, and only
if the gatekeeper's request is for that key (its reference); anything else is a 404. A
destination the vault refuses (``binding_conflict``, ``insecure_transport``) offers no new form:
a new key would not help.
"""

import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Final, Literal, get_args
from urllib.parse import urlencode

from domo_common.gatekeeper_api import SecretRequestView
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from domo_core.accounts.models import Person
from domo_core.audit.log import APPEND_LOCK_KEY, append_audit
from domo_core.audit.models import AuditEntry
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError, new_http_client
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.factory import ModelFactory
from domo_core.llm.key_flow import (
    KEY_NEW_PATH,
    KEY_NEW_STATUS_PATH,
    MAX_MODEL_NAME_LENGTH,
    KeyChoiceError,
    KeyStart,
    finish_key,
    new_key_status_url,
    start_key,
    start_of,
    started_under,
    was_added,
    works,
)
from domo_core.llm.listing import ModelLister, has_control_characters
from domo_core.llm.models import Provider, ProviderModel
from domo_core.llm.provider_keys import (
    GatekeeperFailure,
    check_key,
    gatekeeper_failure,
    key_form,
    provider_kind,
    request_key,
)
from domo_core.llm.provider_kinds import PROVIDER_KINDS
from domo_core.llm.real_spend import admin_ref, read_real_spend, request_admin_key
from domo_core.llm.types import ErrorKind
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.key_forms import (
    ADMIN_ADDED,
    CONFIRMATION_ID,
    GATEKEEPER_REQUEST_ID,
    REKEYED,
    linked_requests,
    may_add_a_key,
    may_open_a_form,
    request_named,
)
from domo_core.texts.scheduler import TextWriting
from domo_core.tools.key_tools import KEY_ADD_ACTION_TYPE, KEY_CHECK_PATH
from domo_core.tools.model_key import (
    KEY_ADMIN_FORM_PATH,
    KEY_ADMIN_STATUS_PATH,
    KEY_FORM_PATH,
    KEY_STATUS_PATH,
    REKEY_REQUEST_PARAM,
    key_status_url,
    vault_form_shown,
)
from domo_core.tools.spending_tools import (
    KEY_ADMIN_ACTION_TYPE,
    key_admin_status_url,
    no_real_spend,
)
from domo_core.web.deps import (
    SessionFactory,
    get_clock,
    get_gatekeeper,
    get_model_factory,
    get_model_lister,
    get_policy,
    get_session,
    get_session_factory,
    require_action,
)
from domo_core.web.language import request_texts
from domo_core.web.templating import render

router = APIRouter()

_PROVIDER_ID = re.compile(r"p-[0-9a-f]{8}")
# The answer that ends the poll takes the place of the form's frame and of the poll.
_ENDS_THE_FORM: Final = {"HX-Retarget": "closest .key-form", "HX-Reswap": "outerHTML"}
# Failures a new key does not help with: the vault refuses the destination itself.
_DEAD_ENDS: Final = frozenset({"binding_conflict", "insecure_transport"})
# What a check may have said, as ``provider.rekeyed`` records it, or a first read of the real
# spending, as ``key.admin_added`` does (``unreadable``: the provider told nothing).
_CHECK_OUTCOMES: Final = frozenset(
    {"ok", "unreadable", *get_args(ErrorKind), *get_args(GatekeeperFailure)}
)
# What records a key given again, and an admin key given, once its form was filled in (the rules of
# ``policy.key_forms`` read them: a key saved under a yes spends it).
RecordedCheck = Literal["provider.rekeyed", "key.admin_added"]
# What a ticked box of the form of a new key sends.
_TRUE_VALUES: Final = frozenset({"on", "true", "1", "yes"})

KeyStatus = Literal["checked", "failed", "expired", "superseded", "lost"]


def rekey_url(provider: Provider, confirmation: str | None = None) -> str | None:
    """Where a button opens a new form for the key of ``provider`` (``None`` for a provider
    without a key: there is no key to enter again). ``confirmation`` is the request whose yes the
    form was opened under, when it was (``may_open_a_form``): the new form is for the same yes."""
    if provider.secret_ref is None:
        return None
    path = KEY_FORM_PATH.format(provider_id=provider.id)
    if confirmation is None:
        return path
    return f"{path}?{urlencode({REKEY_REQUEST_PARAM: confirmation})}"


async def _keyed_provider(session: AsyncSession, provider_id: str, person: Person) -> Provider:
    """The provider with a key named in the address, or a 404; a 403 when ``person`` did not give
    it. The read transaction ends before the caller goes to the network: a connection must not
    wait, in a transaction, for the gatekeeper or a model."""
    provider = (
        await session.get(Provider, provider_id) if _PROVIDER_ID.fullmatch(provider_id) else None
    )
    await session.commit()
    if provider is None or provider.secret_ref is None:
        raise HTTPException(status_code=404)
    if provider.given_by != person.id:
        raise HTTPException(status_code=403)
    return provider


def _result(
    request: Request,
    status: KeyStatus,
    kind: str | None = None,
    *,
    in_place_of_the_form: bool = True,
    again_url: str | None = None,
    texts: str = "chat.key",
) -> Response:
    """What came of the form, in place of the form (``in_place_of_the_form``) or of the button
    that asked for it: ``kind`` is what failed, for ``failed``, said with the catalog's ``texts``
    (``chat.key``, or ``chat.key.admin`` for an admin key). Anything but a key that works comes
    with the button that opens a new form (``again_url``: a form that ran out may have been
    opened by that button, when Domo could not answer), except a destination the vault
    refuses."""
    message = f"{texts}.error.{kind}" if kind is not None else f"{texts}.{status}"
    again = status != "checked" and kind not in _DEAD_ENDS
    return render(
        request,
        "fragments/key_result.html",
        headers=_ENDS_THE_FORM if in_place_of_the_form else None,
        status=status,
        kind=kind,
        message=request_texts(request)(message),
        rekey_url=again_url if again else None,
    )


def _rekey_result(
    request: Request,
    provider: Provider,
    status: KeyStatus,
    kind: str | None = None,
    *,
    in_place_of_the_form: bool = True,
    confirmation: str | None = None,
    spent: bool = False,
) -> Response:
    """``_result`` for a key given again: the button names the request (``confirmation``) the form
    was opened under, if it was; a key saved under it spent that yes (``spent``), so there is no
    button: the route would answer 403, and a page shows nothing for that."""
    return _result(
        request,
        status,
        kind,
        in_place_of_the_form=in_place_of_the_form,
        again_url=None if spent else rekey_url(provider, confirmation),
    )


def _poll(request: Request, status_url: str, *, note: bool = False) -> Response:
    """The poll again; ``note`` says that the vault did not answer just now."""
    return render(request, "fragments/key_poll.html", status_url=status_url, note=note)


async def _recorded_check(
    session: AsyncSession, provider_id: str, request_id: str, *, action: RecordedCheck = REKEYED
) -> str | None:
    """What the check of this request said, if it was checked already (``provider.rekeyed``, or
    ``key.admin_added`` for the form of an admin key: ``action``)."""
    result = await session.execute(
        select(AuditEntry.details)
        .where(
            AuditEntry.action == action,
            AuditEntry.details.contains({"provider_id": provider_id, "request_id": request_id}),
        )
        .order_by(AuditEntry.id)
        .limit(1)
    )
    details = result.scalar_one_or_none()
    if details is None:
        return None
    check = details.get("check")
    # Never anything but a known outcome on the page.
    return check if isinstance(check, str) and check in _CHECK_OUTCOMES else "gatekeeper"


async def _form_confirmation(session: AsyncSession, request_id: str) -> str | None:
    """The request whose yes the form ``request_id`` was opened under (``may_open_a_form``), if it
    was: read from the audit entry of the form (``secret.requested``), which survives across
    requests. ``None`` for any other form."""
    result = await session.execute(
        select(AuditEntry.details)
        .where(
            AuditEntry.action == "secret.requested",
            AuditEntry.details.contains({"request_id": request_id}),
        )
        .order_by(AuditEntry.id)
        .limit(1)
    )
    details = result.scalar_one_or_none()
    found = None if details is None else details.get("confirmation")
    return found if isinstance(found, str) and CONFIRMATION_ID.fullmatch(found) else None


async def _record_check(
    session: AsyncSession,
    person: Person,
    provider_id: str,
    request_id: str,
    outcome: str,
    confirmation: str | None,
    *,
    action: RecordedCheck = REKEYED,
) -> str:
    """Audit what the check of this request said (``action``: ``provider.rekeyed``, or
    ``key.admin_added``), unless an answer that ran at the same time did (the audit log's lock,
    held until the commit, makes the read and the entry one step); with the request whose yes the
    form was opened under, if it was (that yes is spent). Returns what is recorded."""
    await session.execute(select(func.pg_advisory_xact_lock(APPEND_LOCK_KEY)))
    recorded = await _recorded_check(session, provider_id, request_id, action=action)
    if recorded is None:
        details = {"provider_id": provider_id, "request_id": request_id, "check": outcome}
        if confirmation is not None:
            details["confirmation"] = confirmation
        # Last, just before the commit: the vault holds the new key, whatever the check said.
        await append_audit(session, actor=f"person:{person.id}", action=action, details=details)
        recorded = outcome
    await session.commit()
    return recorded


def _checked(
    request: Request, provider: Provider, outcome: str, confirmation: str | None
) -> Response:
    if outcome == "ok":
        return _rekey_result(request, provider, "checked")
    # A key was saved, so a yes the form was opened under is spent: no button for it.
    return _rekey_result(request, provider, "failed", outcome, spent=confirmation is not None)


@router.get(KEY_STATUS_PATH + "/{provider_id}/{request_id}")
async def key_status(
    request: Request,
    provider_id: str,
    request_id: str,
    person: Annotated[Person, Depends(require_action("model.rekey"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Response:
    """Where the form for a key given again stands (see the module docstring)."""
    provider = await _keyed_provider(session, provider_id, person)
    if GATEKEEPER_REQUEST_ID.fullmatch(request_id) is None:
        raise HTTPException(status_code=404)
    try:
        view = await gatekeeper.get_secret_request(request_id)
    except GatekeeperError as error:
        if error.status == 404:
            confirmation = await _form_confirmation(session, request_id)
            return _rekey_result(request, provider, "lost", confirmation=confirmation)
        if error.status is None or error.status >= 500:
            return _poll(request, key_status_url(provider.id, request_id), note=True)
        confirmation = await _form_confirmation(session, request_id)
        return _rekey_result(
            request, provider, "failed", gatekeeper_failure(error), confirmation=confirmation
        )
    if view.ref != provider.secret_ref or view.id != request_id:
        raise HTTPException(status_code=404)  # another key's request
    match view.status:
        case "pending":
            return _poll(request, key_status_url(provider.id, request_id))
        case "expired" | "superseded":
            confirmation = await _form_confirmation(session, request_id)
            return _rekey_result(request, provider, view.status, confirmation=confirmation)
        case "fulfilled":
            recorded = await _recorded_check(session, provider.id, request_id)
            confirmation = await _form_confirmation(session, request_id)
            await session.commit()
            if recorded is None:
                try:
                    checked = await check_key(
                        session,
                        gatekeeper,
                        provider,
                        model_factory,
                        model_lister,
                        catalog=shipped_catalog(),
                        now=clock(),
                        session_factory=session_factory,
                        # A server that gives no list is asked with the model the key has.
                        named_model=await _a_listed_model(session, provider.id),
                    )
                    # Nothing probed (no model to ask) is not a key that works.
                    failure = checked.error or (
                        None if checked.checked_with is not None else "no_model"
                    )
                except GatekeeperError as error:
                    failure = gatekeeper_failure(error)
                recorded = await _record_check(
                    session, person, provider.id, request_id, failure or "ok", confirmation
                )
            return _checked(request, provider, recorded, confirmation)


async def _a_listed_model(session: AsyncSession, provider_id: str) -> str | None:
    """A model the key lists (for a server that gives no list, the one it was named with)."""
    found = await session.execute(
        select(ProviderModel.model_id)
        .where(ProviderModel.provider_id == provider_id)
        .order_by(ProviderModel.model_id)
        .limit(1)
    )
    return found.scalar_one_or_none()


def _text_writing(request: Request) -> TextWriting | None:
    """The writing of people's texts of the application (``app.state.text_writing``), if any."""
    return getattr(request.app.state, "text_writing", None)


@router.post(KEY_FORM_PATH)
async def new_key_form(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_action("model.rekey"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    confirmation: Annotated[str | None, Query(alias=REKEY_REQUEST_PARAM)] = None,
) -> Response:
    """A new form for the key of the provider, opened by the person's button: the frame and its
    poll, in place of the button (see the module docstring)."""
    provider = await _keyed_provider(session, provider_id, person)
    allowed = await may_open_a_form(
        session, gatekeeper, person, provider, confirmation, policy, clock()
    )
    form = await key_form(session, person, writing=_text_writing(request))
    # The reads end here: no transaction waits for the vault.
    await session.commit()
    if not allowed:
        raise HTTPException(status_code=403)
    try:
        view = await request_key(gatekeeper, provider, form)
    except GatekeeperError as error:
        return _rekey_result(
            request,
            provider,
            "failed",
            gatekeeper_failure(error),
            in_place_of_the_form=False,
            confirmation=confirmation,
        )
    shown = await _show_the_vault_form(
        request,
        session,
        person,
        provider.secret_ref,
        view,
        key_status_url(provider.id, view.id),
        confirmation,
    )
    if shown is None:
        return _rekey_result(
            request,
            provider,
            "failed",
            "gatekeeper",
            in_place_of_the_form=False,
            confirmation=confirmation,
        )
    return shown


# --- a new key (0.6.0) ---------------------------------------------------------------------------


async def _another_key_started_under(
    session: AsyncSession, person: Person, request_id: str, provider: Provider
) -> bool:
    """Whether a key other than ``provider`` was started under the yes of ``request_id`` (and the
    requests a replacement links it to): read after ``provider``'s own start was audited, under
    the audit log's lock, so that two forms posted at once under one yes start one key."""
    request = await request_named(session, person, request_id)
    if request is None:
        return True
    started = await started_under(session, await linked_requests(session, request))
    return any(other != provider.id for other in started)


def _with_request(path: str, confirmation: str | None) -> str:
    """``path``, naming the request ``confirmation`` whose yes it is for, if there is one."""
    if confirmation is None:
        return path
    return f"{path}?{urlencode({REKEY_REQUEST_PARAM: confirmation})}"


def _new_key_again_url(provider: Provider, confirmation: str | None) -> str | None:
    """Where the button opens a new vault form for the new key ``provider`` (none for a server
    without a key)."""
    if provider.secret_ref is None:
        return None
    return _with_request(f"{KEY_NEW_PATH}/{provider.id}/form", confirmation)


def _new_key_result(
    request: Request,
    provider: Provider,
    status: KeyStatus,
    kind: str | None = None,
    *,
    confirmation: str | None = None,
    spent: bool = False,
    in_place_of_the_form: bool = True,
) -> Response:
    """``_result`` for a new key: its button opens a new vault form for it, under the same yes
    (``confirmation``); a key saved under that yes spent it (``spent``): no button then."""
    return _result(
        request,
        status,
        kind,
        in_place_of_the_form=in_place_of_the_form,
        again_url=None if spent else _new_key_again_url(provider, confirmation),
    )


def _new_key_page(
    request: Request,
    confirmation: str | None,
    *,
    status_code: int = 200,
    error_key: str | None = None,
    kind: str = "",
    base_url: str = "",
    has_key: bool = False,
) -> Response:
    """The form of a new key (a fragment), with what was typed and what is wrong."""
    return render(
        request,
        "fragments/key_new.html",
        status_code=status_code,
        kinds=list(PROVIDER_KINDS),
        kind=kind,
        base_url=base_url,
        has_key=has_key,
        error_key=error_key,
        error_params={},
        form_url=_with_request(KEY_NEW_PATH, confirmation),
    )


def _ask_the_name(
    request: Request, provider: Provider, *, in_place_of_the_form: bool, error: bool = False
) -> Response:
    """The question of a server that gives no list of its models: the name of the one it serves.
    It takes its own place when it is answered."""
    return render(
        request,
        "fragments/model_name.html",
        status_code=400 if error else 200,
        headers=_ENDS_THE_FORM if in_place_of_the_form else None,
        provider_id=provider.id,
        error_key="chat.key.new.error.model" if error else None,
        error_params={"max": str(MAX_MODEL_NAME_LENGTH)},
        max_model_length=MAX_MODEL_NAME_LENGTH,
        form_texts="chat.key.new",
        name_url=KEY_CHECK_PATH.format(provider_id=provider.id),
        name_target="this",
        name_swap="outerHTML",
    )


async def _a_new_key(
    session: AsyncSession, provider_id: str, person: Person, *, keyed: bool | None
) -> tuple[Provider, KeyStart, bool]:
    """The new key named in the address, how it was started (``key.started``) and whether it was
    added already (it worked): a 404 for no such key, a key with (``keyed``) or without a key in
    the vault when it should not, or a key that was not started from the chat (the first launch's,
    say); a 403 when ``person`` did not give it. The read transaction ends here."""
    provider = (
        await session.get(Provider, provider_id) if _PROVIDER_ID.fullmatch(provider_id) else None
    )
    start = None if provider is None else await start_of(session, provider.id)
    added = provider is not None and await was_added(session, provider.id)
    await session.commit()
    if (
        provider is None
        or start is None
        or (keyed is not None and (provider.secret_ref is not None) != keyed)
    ):
        raise HTTPException(status_code=404)
    if provider.given_by != person.id:
        raise HTTPException(status_code=403)
    return provider, start, added


async def _check_new_key(
    request: Request,
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    model_factory: ModelFactory,
    model_lister: ModelLister,
    session_factory: SessionFactory,
    now: datetime,
    provider: Provider,
    start: KeyStart,
    *,
    in_place_of_the_form: bool,
    named_model: str | None = None,
) -> Response:
    """Check the new key ``provider`` (``finish_key``, with the request whose yes it was started
    under, ``start``) and say what came of it, or ask the name of the model a server that gives no
    list serves. A key that does not work, started under someone's yes, offers no new form under
    it: the key was saved, so that yes is spent (whatever the check said)."""
    catalog = await current_catalog(session)
    await session.commit()  # no transaction waits for the gatekeeper or the model
    spent = start.confirmation is not None
    try:
        checked = await finish_key(
            session,
            gatekeeper,
            provider,
            model_factory,
            model_lister,
            catalog=catalog,
            now=now,
            session_factory=session_factory,
            named_model=named_model,
            confirmation=start.confirmation,
        )
    except GatekeeperError as error:
        return _new_key_result(
            request,
            provider,
            "failed",
            gatekeeper_failure(error),
            spent=spent,
            in_place_of_the_form=in_place_of_the_form,
        )
    if works(checked):
        return _new_key_result(
            request, provider, "checked", in_place_of_the_form=in_place_of_the_form
        )
    if checked.error is None and not checked.models:
        return _ask_the_name(request, provider, in_place_of_the_form=in_place_of_the_form)
    return _new_key_result(
        request,
        provider,
        "failed",
        checked.error or "no_model",
        spent=spent,
        in_place_of_the_form=in_place_of_the_form,
    )


async def _show_the_vault_form(
    request: Request,
    session: AsyncSession,
    person: Person,
    ref: str | None,
    view: SecretRequestView,
    status_url: str,
    confirmation: str | None,
) -> Response | None:
    """The vault form ``view`` of the reference ``ref`` in its frame, with its poll at
    ``status_url``, the form audited (the reference, and the request it was opened under, never
    its path) and committed; ``None`` when the vault's answer is not one a page shows
    (``vault_form_shown``: nothing is committed then). Every form a page opens goes through it: a
    key given again, a new key, an admin key."""
    shown = vault_form_shown(view, status_url)
    if shown is None:
        await session.rollback()
        return None
    details: dict[str, str | None] = {"ref": ref}
    if confirmation is not None:
        # The form is opened under the yes of someone else: the audit trail remembers which, with
        # the form's request (not its path), so that a key saved under it spends that yes.
        details.update({"confirmation": confirmation, "request_id": view.id})
    # Last, just before the commit.
    await append_audit(
        session, actor=f"person:{person.id}", action="secret.requested", details=details
    )
    await session.commit()
    return render(request, "fragments/key_form.html", form_path=shown[0], status_url=shown[1])


@router.get(KEY_NEW_PATH)
async def new_key_page(
    request: Request,
    person: Annotated[Person, Depends(require_action(KEY_ADD_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    confirmation: Annotated[str | None, Query(alias=REKEY_REQUEST_PARAM)] = None,
) -> Response:
    """The form of a new key, for whoever may have it (``may_add_a_key``)."""
    allowed = await may_add_a_key(session, gatekeeper, person, confirmation, policy, clock())
    await session.commit()
    if not allowed:
        raise HTTPException(status_code=403)
    return _new_key_page(request, confirmation)


@router.post(KEY_NEW_PATH)
async def add_a_key(
    request: Request,
    person: Annotated[Person, Depends(require_action(KEY_ADD_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    confirmation: Annotated[str | None, Query(alias=REKEY_REQUEST_PARAM)] = None,
    kind: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    has_key: Annotated[str | None, Form()] = None,
) -> Response:
    """The form of a new key, filled in: the key is the person's, started under the yes the
    address names, if any (the one key of that yes), and its vault form is shown (a server without
    a key is checked at once). What is wrong is said with the form again (400), and nothing is
    recorded."""
    allowed = await may_add_a_key(session, gatekeeper, person, confirmation, policy, clock())
    form = await key_form(session, person, writing=_text_writing(request))
    await session.commit()
    if not allowed:
        raise HTTPException(status_code=403)
    ticked = has_key is not None and has_key.strip().lower() in _TRUE_VALUES

    def wrong(error_key: str) -> Response:
        return _new_key_page(
            request,
            confirmation,
            status_code=400,
            error_key=error_key,
            kind=kind,
            base_url=base_url,
            has_key=ticked,
        )

    chosen = provider_kind(kind)
    if chosen is None:
        return wrong("chat.key.new.error.kind")
    try:
        provider, view = await start_key(
            session,
            gatekeeper,
            person=person,
            kind=chosen,
            base_url=base_url,
            has_key=ticked,
            form=form,
            confirmation=confirmation,
        )
    except KeyChoiceError as error:
        return wrong(f"chat.key.new.error.{error.what}")
    except GatekeeperError as error:
        return wrong(f"chat.key.new.error.{gatekeeper_failure(error)}")
    # Its start is audited, and the audit log's lock held: another form posted at the same time
    # under the same yes sees it, or this one sees that one's.
    if confirmation is not None and await _another_key_started_under(
        session, person, confirmation, provider
    ):
        await session.rollback()
        raise HTTPException(status_code=403)
    if view is None:
        await session.commit()
        return await _check_new_key(
            request,
            session,
            gatekeeper,
            model_factory,
            model_lister,
            session_factory,
            clock(),
            provider,
            KeyStart(confirmation),
            in_place_of_the_form=False,
        )
    shown = await _show_the_vault_form(
        request,
        session,
        person,
        provider.secret_ref,
        view,
        new_key_status_url(provider.id, view.id),
        confirmation,
    )
    return wrong("chat.key.new.error.gatekeeper") if shown is None else shown


@router.get(KEY_NEW_STATUS_PATH + "/{provider_id}/{request_id}")
async def new_key_status(
    request: Request,
    provider_id: str,
    request_id: str,
    person: Annotated[Person, Depends(require_action(KEY_ADD_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Response:
    """Where the vault form of a new key stands: still waiting (the poll again), over without a
    key, or filled in (the key is checked, once: a key added says so without asking again)."""
    provider, start, added = await _a_new_key(session, provider_id, person, keyed=True)
    if GATEKEEPER_REQUEST_ID.fullmatch(request_id) is None:
        raise HTTPException(status_code=404)
    if added:
        return _new_key_result(request, provider, "checked")
    try:
        view = await gatekeeper.get_secret_request(request_id)
    except GatekeeperError as error:
        if error.status is not None and error.status < 500 and error.status != 404:
            failure: str | None = gatekeeper_failure(error)
        elif error.status == 404:
            failure = None
        else:
            return _poll(request, new_key_status_url(provider.id, request_id), note=True)
        confirmation = await _form_confirmation(session, request_id)
        await session.commit()
        if failure is None:
            return _new_key_result(request, provider, "lost", confirmation=confirmation)
        return _new_key_result(request, provider, "failed", failure, confirmation=confirmation)
    if view.ref != provider.secret_ref or view.id != request_id:
        raise HTTPException(status_code=404)  # another key's request
    confirmation = await _form_confirmation(session, request_id)
    await session.commit()
    match view.status:
        case "pending":
            return _poll(request, new_key_status_url(provider.id, request_id))
        case "expired" | "superseded":
            return _new_key_result(request, provider, view.status, confirmation=confirmation)
        case "fulfilled":
            return await _check_new_key(
                request,
                session,
                gatekeeper,
                model_factory,
                model_lister,
                session_factory,
                clock(),
                provider,
                start,
                in_place_of_the_form=True,
            )


@router.post(KEY_NEW_PATH + "/{provider_id}/form")
async def new_key_form_again(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_action(KEY_ADD_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    confirmation: Annotated[str | None, Query(alias=REKEY_REQUEST_PARAM)] = None,
) -> Response:
    """A new vault form for a new key whose form ran out, or whose key did not work, in place of
    the button: under a yes, only for the key started under it, and only until a key was saved
    under it. A key that works is given again with its own button (``KEY_FORM_PATH``)."""
    provider, _, added = await _a_new_key(session, provider_id, person, keyed=True)
    if added:
        raise HTTPException(status_code=404)
    allowed = await may_add_a_key(
        session, gatekeeper, person, confirmation, policy, clock(), key=provider
    )
    form = await key_form(session, person, writing=_text_writing(request))
    await session.commit()
    if not allowed:
        raise HTTPException(status_code=403)
    try:
        view = await request_key(gatekeeper, provider, form)
    except GatekeeperError as error:
        return _new_key_result(
            request,
            provider,
            "failed",
            gatekeeper_failure(error),
            confirmation=confirmation,
            in_place_of_the_form=False,
        )
    shown = await _show_the_vault_form(
        request,
        session,
        person,
        provider.secret_ref,
        view,
        new_key_status_url(provider.id, view.id),
        confirmation,
    )
    if shown is None:
        return _new_key_result(
            request,
            provider,
            "failed",
            "gatekeeper",
            confirmation=confirmation,
            in_place_of_the_form=False,
        )
    return shown


@router.post(KEY_CHECK_PATH)
async def check_a_new_key(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_action(KEY_ADD_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    model: Annotated[str | None, Form()] = None,
) -> Response:
    """Check a new key again: a server without a key (its card's button), or a server that gives
    no list, with the name of the model it serves (``model``). It changes nothing in the vault, and
    only ever checks the key it names, started by the person (the yes it was started under, if
    any, is the one recorded with the key added)."""
    provider, start, added = await _a_new_key(session, provider_id, person, keyed=None)
    if added:
        return _new_key_result(request, provider, "checked", in_place_of_the_form=False)
    name = None if model is None else model.strip()
    if name is not None and (
        not name or len(name) > MAX_MODEL_NAME_LENGTH or has_control_characters(name)
    ):
        return _ask_the_name(request, provider, in_place_of_the_form=False, error=True)
    return await _check_new_key(
        request,
        session,
        gatekeeper,
        model_factory,
        model_lister,
        session_factory,
        clock(),
        provider,
        start,
        in_place_of_the_form=False,
        named_model=name,
    )


# --- the admin key of one's own key (0.6.0) ------------------------------------------------------


def _admin_result(
    request: Request,
    provider: Provider,
    status: KeyStatus,
    kind: str | None = None,
    *,
    in_place_of_the_form: bool = True,
    confirmation: str | None = None,
    spent: bool = False,
) -> Response:
    """``_result`` for the form of an admin key, in its own words: the button opens a new form of
    the admin key, under the yes it was opened under (``confirmation``); an admin key saved under
    that yes spent it (``spent``): no button then."""
    again = _with_request(KEY_ADMIN_FORM_PATH.format(provider_id=provider.id), confirmation)
    return _result(
        request,
        status,
        kind,
        in_place_of_the_form=in_place_of_the_form,
        again_url=None if spent else again,
        texts="chat.key.admin",
    )


async def _admin_key_of(session: AsyncSession, provider_id: str, person: Person) -> Provider:
    """The key named in the address, if ``person`` gave it (403 otherwise) and its provider tells
    the real spending (404 for any other, and for no such key)."""
    provider = await _keyed_provider(session, provider_id, person)
    if no_real_spend(provider) is not None:
        raise HTTPException(status_code=404)
    return provider


async def _take_the_admin_key(
    session: AsyncSession, gatekeeper: GatekeeperClient, provider: Provider, now: datetime
) -> str:
    """The admin key of ``provider`` is in the vault: its reference is recorded, its admin route
    declared and the real spending read a first time (``read_real_spend``). What came of it:
    ``ok``, ``unreadable`` (the provider told nothing: not an admin key of the organization, say)
    or the gatekeeper's failure."""
    provider.admin_secret_ref = admin_ref(provider.id)
    await session.commit()
    try:
        async with new_http_client() as http:
            days = await read_real_spend(session, gatekeeper, provider, now=now, http=http)
    except GatekeeperError as error:
        return gatekeeper_failure(error)
    return "unreadable" if days is None else "ok"


@router.get(KEY_ADMIN_STATUS_PATH + "/{provider_id}/{request_id}")
async def admin_key_status(
    request: Request,
    provider_id: str,
    request_id: str,
    person: Annotated[Person, Depends(require_action(KEY_ADMIN_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    """Where the form of an admin key stands: still waiting (the poll again), over without a key,
    or filled in (its route is declared and the real spending read, once: a later answer says what
    the first one found, without reading again)."""
    provider = await _admin_key_of(session, provider_id, person)
    if GATEKEEPER_REQUEST_ID.fullmatch(request_id) is None:
        raise HTTPException(status_code=404)
    try:
        view = await gatekeeper.get_secret_request(request_id)
    except GatekeeperError as error:
        if error.status is not None and error.status < 500 and error.status != 404:
            failure: str | None = gatekeeper_failure(error)
        elif error.status == 404:
            failure = None
        else:
            return _poll(request, key_admin_status_url(provider.id, request_id), note=True)
        confirmation = await _form_confirmation(session, request_id)
        await session.commit()
        if failure is None:
            return _admin_result(request, provider, "lost", confirmation=confirmation)
        return _admin_result(request, provider, "failed", failure, confirmation=confirmation)
    if view.ref != admin_ref(provider.id) or view.id != request_id:
        raise HTTPException(status_code=404)  # another key's request
    match view.status:
        case "pending":
            return _poll(request, key_admin_status_url(provider.id, request_id))
        case "expired" | "superseded":
            confirmation = await _form_confirmation(session, request_id)
            await session.commit()
            return _admin_result(request, provider, view.status, confirmation=confirmation)
        case "fulfilled":
            recorded = await _recorded_check(session, provider.id, request_id, action=ADMIN_ADDED)
            confirmation = await _form_confirmation(session, request_id)
            await session.commit()
            if recorded is None:
                outcome = await _take_the_admin_key(session, gatekeeper, provider, clock())
                recorded = await _record_check(
                    session,
                    person,
                    provider.id,
                    request_id,
                    outcome,
                    confirmation,
                    action=ADMIN_ADDED,
                )
            if recorded == "ok":
                return _admin_result(request, provider, "checked")
            # An admin key was saved: a yes the form was opened under is spent.
            return _admin_result(
                request, provider, "failed", recorded, spent=confirmation is not None
            )


@router.post(KEY_ADMIN_FORM_PATH)
async def admin_key_form(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_action(KEY_ADMIN_ACTION_TYPE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    confirmation: Annotated[str | None, Query(alias=REKEY_REQUEST_PARAM)] = None,
) -> Response:
    """A form for the admin key of one's own key, opened by the person's button (the one of their
    card, or "Enter the key again"): the frame and its poll, in place of the button, guarded as a
    key given again (``may_open_a_form``, ``admin``)."""
    provider = await _admin_key_of(session, provider_id, person)
    allowed = await may_open_a_form(
        session, gatekeeper, person, provider, confirmation, policy, clock(), admin=True
    )
    form = await key_form(session, person, writing=_text_writing(request))
    # The reads end here: no transaction waits for the vault.
    await session.commit()
    if not allowed:
        raise HTTPException(status_code=403)
    try:
        view = await request_admin_key(gatekeeper, provider, form)
    except GatekeeperError as error:
        return _admin_result(
            request,
            provider,
            "failed",
            gatekeeper_failure(error),
            in_place_of_the_form=False,
            confirmation=confirmation,
        )
    shown = await _show_the_vault_form(
        request,
        session,
        person,
        admin_ref(provider.id),
        view,
        key_admin_status_url(provider.id, view.id),
        confirmation,
    )
    if shown is None:
        return _admin_result(
            request,
            provider,
            "failed",
            "gatekeeper",
            in_place_of_the_form=False,
            confirmation=confirmation,
        )
    return shown
