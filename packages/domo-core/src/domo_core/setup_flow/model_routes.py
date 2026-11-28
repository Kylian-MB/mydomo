"""The model step of the first launch: choose a provider, enter the provider's key in the
gatekeeper's vault form, and check that a model answers.

The owner picks a kind of provider; no model name is asked. For a provider that needs a key, the
core asks the gatekeeper for a one-time form (``llm.provider_keys.request_key``) and shows it, in a
frame, on its own page: the key goes from the browser to the vault and never through the core. The
page then polls the request's status. Once the owner has filled the form in, the core tells the
gatekeeper where the provider is, lists the models the key opens and asks the cheapest one that
calls tools one question through the gatekeeper's relay (``llm.provider_keys.check_key``). A
server that gives no list of its models is asked for the name of the one it serves first
(``model_name.html``, then ``POST .../name``). If the check fails, the provider is kept and the
owner is offered a way out: to enter the key again (a new request for the same reference and the
same destination, which the gatekeeper treats as a rotation), to try again (a provider with no
key), or to start over with another provider.

When the key works, the setup is done at once (0.7.1 spec 5.1): the house's language becomes the
main language of the owner's browser, whatever it is (``house.language``, ``texts.languages``),
the key is the household's general one (each call chooses its model among the ones it lists,
``llm.router``), and the owner is sent to the chat. Nothing is written for it: the pages read the
default phrases of their reader's language, and the upkeep writes the house's version of a
language that has none (``texts.upkeep``).

A provider with no key (a server of one's own) skips the form and goes straight to the check.

Everything is htmx fragments in one container, ``#model-step``: the form, the wait for the key,
the errors. The poll is a fragment of its own, apart from the frame of the vault form, so that a
"still waiting" answer does not put the frame (and the key being typed) back.

Rules that hold throughout:

* A key is only ever sent to the destination its kind of provider says: the address of a cloud
  provider is not the visitor's to choose, and the key's binding is built from the address that is
  recorded. A key over plain http to a host that is not local is refused before any request is
  made (``domo_common.transport``); the gatekeeper applies the same rule and stays the
  authority, and what it refuses is shown the same way.
* The gatekeeper's errors are shown as messages. None of them is a 500. Each one is logged
  (WARNING: the operation, the status and the code only) where it is raised, in
  ``gatekeeper_client``.
* Transactions: what a step records is committed together with its audit entry, written last
  (the audit log holds a lock until the transaction ends). The gatekeeper is asked first, so a
  refusal leaves no row behind. A failed check leaves the setup at this step and the provider in
  place; finishing is idempotent (the setup records the end of the step once, whichever request
  gets there first).
* Only the owner may use these pages, and only while the setup is going on (the setup gate).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final
from urllib.parse import quote, urlsplit

from domo_common.gatekeeper_api import SecretRequestView
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from domo_core.accounts.models import Person
from domo_core.accounts.routes import HOME_PATH
from domo_core.audit.log import append_audit
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.choice import candidates, pick_model
from domo_core.llm.factory import ModelFactory
from domo_core.llm.key_flow import (
    MAX_MODEL_NAME_LENGTH,
    ChosenKey,
    KeyChoiceError,
    check_choice,
)
from domo_core.llm.listing import ModelLister, has_control_characters
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.provider_keys import (
    check_key,
    gatekeeper_failure,
    install_key_form,
    provider_kind,
    request_key,
    vault_form_path,
)
from domo_core.llm.provider_kinds import PROVIDER_KINDS
from domo_core.llm.speech.home_key import HOME_SPEECH_ID
from domo_core.policy.settings import (
    HOUSE_LANGUAGE,
    load_policy_settings,
    update_policy_setting,
)
from domo_core.setup_flow.models import SetupState
from domo_core.setup_flow.state import STEP_PATHS, setup_stage
from domo_core.texts.languages import browser_language
from domo_core.web.deps import (
    SessionFactory,
    get_clock,
    get_gatekeeper,
    get_model_factory,
    get_model_lister,
    get_session,
    get_session_factory,
    require_owner,
)
from domo_core.web.language import request_texts
from domo_core.web.templating import render

router = APIRouter()

MODEL_PATH: Final = STEP_PATHS["model"]
# Where the step's fragments go, and what replaces them (see ``static/model-step.js``).
STEP_TARGET: Final = "#model-step"

_PROVIDER_ID = re.compile(r"p-[0-9a-f]{8}")
_TRUE_VALUES: Final = frozenset({"on", "true", "1", "yes"})

# What the owner can do after a failure, by what failed: a new key does not help when the vault
# refuses the destination itself.
_DEAD_ENDS: Final = frozenset({"insecure_transport", "binding_conflict"})


@dataclass(frozen=True)
class _FormError:
    """What is wrong with the form: a catalog key and its parameters."""

    key: str
    params: dict[str, str]


def _parse_choice(kind_value: str, base_url_value: str, has_key: bool) -> ChosenKey | _FormError:
    """Check what the owner typed (``llm.key_flow.check_choice``, the same check as for a key added
    from the chat). A cloud provider's address is the one its kind says; the address of another
    server must be one the gatekeeper accepts, and a key must not cross the network in
    cleartext."""
    kind = provider_kind(kind_value)
    if kind is None:
        return _FormError("setup.model.error.kind", {})
    try:
        return check_choice(kind, base_url_value, has_key)
    except KeyChoiceError as wrong:
        return _FormError(f"setup.model.error.{wrong.what}", {})


def _form(
    request: Request,
    *,
    status_code: int = 200,
    error: _FormError | None = None,
    kind: str = "",
    base_url: str = "",
    has_key: bool = False,
) -> Response:
    """The form (a fragment: the page includes it), with what was typed and what is wrong."""
    return render(
        request,
        "fragments/model_form.html",
        status_code=status_code,
        kinds=list(PROVIDER_KINDS),
        kind=kind,
        base_url=base_url,
        has_key=has_key,
        error_key=error.key if error else None,
        error_params=error.params if error else {},
    )


def _ask_the_name(
    request: Request,
    provider: Provider,
    *,
    whole_step: bool,
    status_code: int = 200,
    error: bool = False,
) -> Response:
    """The question of a server that gives no list of its models: the name of the one it serves.
    A poll's answer (``whole_step``) replaces the whole step."""
    headers = {"HX-Retarget": STEP_TARGET, "HX-Reswap": "innerHTML"} if whole_step else None
    return render(
        request,
        "fragments/model_name.html",
        status_code=status_code,
        headers=headers,
        provider_id=provider.id,
        error_key="setup.model.error.model" if error else None,
        error_params={"max": str(MAX_MODEL_NAME_LENGTH)},
        max_model_length=MAX_MODEL_NAME_LENGTH,
    )


def _failure(
    request: Request, kind: str, *, provider: Provider | None, whole_step: bool = False
) -> Response:
    """Why the model is not set up, with the ways out that make sense.

    ``provider`` is the row the owner can still act on (none when nothing was recorded). A
    provider with a key can be given the key again, a provider without one can be checked again,
    and either can be dropped; a destination the vault refuses leaves only the last. A poll's
    answer (``whole_step``) is retargeted: it replaces the whole step, not just the poll.
    """
    keyed = provider is not None and provider.secret_ref is not None
    open_ended = kind not in _DEAD_ENDS
    headers = {"HX-Retarget": STEP_TARGET, "HX-Reswap": "innerHTML"} if whole_step else None
    return render(
        request,
        "fragments/model_error.html",
        headers=headers,
        kind=kind,
        provider_id=provider.id if provider else None,
        can_rekey=keyed and open_ended,
        can_retry=provider is not None and not keyed and open_ended,
    )


def _status_url(provider: Provider, view: SecretRequestView) -> str:
    return f"{MODEL_PATH}/status/{provider.id}/{quote(view.id, safe='')}"


def _waiting(
    request: Request, provider: Provider, view: SecretRequestView, form_path: str
) -> Response:
    """The vault form in its frame, and the poll that waits for it."""
    return render(
        request,
        "fragments/secret_wait.html",
        form_path=form_path,
        status_url=_status_url(provider, view),
    )


async def _load_provider(
    session: AsyncSession, provider_id: str, *, keyed: bool | None
) -> Provider:
    """The provider row, or a 404. ``keyed`` says whether it must have a key (``None``: either).

    The read transaction ends before the caller goes to the network: a connection must not wait
    idle, in a transaction, for a gatekeeper or a model.
    """
    provider = (
        await session.get(Provider, provider_id) if _PROVIDER_ID.fullmatch(provider_id) else None
    )
    await session.commit()
    if provider is None or (keyed is not None and (provider.secret_ref is not None) != keyed):
        raise HTTPException(status_code=404)
    return provider


async def _ask_for_key(
    request: Request,
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    person: Person,
    provider: Provider,
    *,
    recorded: bool,
) -> Response:
    """Ask the gatekeeper for a form for the key of ``provider`` and show it.

    ``recorded`` says whether the provider row exists: a new one is only saved once the gatekeeper
    has agreed (a refusal leaves nothing behind), together with the audit entry. A key that was
    asked for before is asked for again with the same reference and destination
    (``request_key`` builds the binding from the recorded kind and address).
    """
    ref = provider.secret_ref
    assert ref is not None
    try:
        # Before Domo can write: the fixed texts, the gatekeeper's own on its form.
        view = await request_key(gatekeeper, provider, install_key_form(request_texts(request)))
    except GatekeeperError as error:
        return _failure(request, gatekeeper_failure(error), provider=provider if recorded else None)
    form_path = vault_form_path(view)
    if form_path is None:
        return _failure(request, "gatekeeper", provider=provider if recorded else None)
    if not recorded:
        session.add(provider)
    # Last, just before the commit: the audit log holds a lock until the transaction ends.
    await append_audit(
        session, actor=f"person:{person.id}", action="secret.requested", details={"ref": ref}
    )
    await session.commit()
    return _waiting(request, provider, view, form_path)


async def _still_there(session: AsyncSession, provider_id: str) -> bool:
    found = await session.execute(select(Provider.id).where(Provider.id == provider_id))
    return found.scalar_one_or_none() is not None


async def _take_the_browser_language(
    request: Request, session: AsyncSession, person: Person, now: datetime
) -> None:
    """Make the house's language the main language of the browser of the owner, whatever it is
    (``texts.languages.browser_language``; a browser that names none leaves it as it is). A
    change is committed with its audit entry."""
    house = (await load_policy_settings(session)).house_language
    language = browser_language(request.headers.get("accept-language"))
    if language is None or language == house:
        await session.commit()
        return
    old, new = await update_policy_setting(
        session, key=HOUSE_LANGUAGE, value=language, by=person.id, now=now
    )
    # Last, just before the commit: the audit log holds a lock until the transaction ends.
    await append_audit(
        session,
        actor=f"person:{person.id}",
        action="policy.updated",
        details={"key": HOUSE_LANGUAGE, "old": old, "new": new},
    )
    await session.commit()


async def _end_the_step(session: AsyncSession, person: Person, provider: Provider) -> Response:
    """The key ``provider`` works: the setup is done, once (a request that finds it done says
    nothing more and just moves on). The provider dropped meanwhile (another tab) is a 404."""
    still_there = await session.execute(
        select(Provider.id).where(Provider.id == provider.id).with_for_update()
    )
    if still_there.scalar_one_or_none() is None:
        raise HTTPException(status_code=404)
    ended = await session.execute(
        update(SetupState)
        .where(SetupState.id == 1, SetupState.model_configured_at.is_(None))
        .values(model_configured_at=datetime.now(UTC))
        .returning(SetupState.id)
    )
    if ended.scalar_one_or_none() is not None:
        # The attempts the owner abandoned must not outlive the setup: one provider of theirs is
        # left, and it is the household's general key. The house's speech service
        # (``HOME_SPEECH_ID``) is no attempt: the upkeep recorded it once the owner existed and
        # reads its list again only when asked, so a key dropped here would never come back.
        await session.execute(
            delete(Provider).where(Provider.id != provider.id, Provider.id != HOME_SPEECH_ID)
        )
        await session.execute(
            update(Provider).where(Provider.id == provider.id).values(is_general=True)
        )
        listed = await session.execute(
            select(func.count())
            .select_from(ProviderModel)
            .where(ProviderModel.provider_id == provider.id)
        )
        await append_audit(
            session,
            actor=f"person:{person.id}",
            action="provider.added",
            details={
                "provider_id": provider.id,
                "kind": provider.kind,
                "host": urlsplit(provider.base_url).hostname or "",
                "models": listed.scalar_one(),
            },
        )
    await session.commit()
    return Response(headers={"HX-Redirect": HOME_PATH})


async def _finish(
    request: Request,
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    model_factory: ModelFactory,
    model_lister: ModelLister,
    person: Person,
    provider: Provider,
    *,
    whole_step: bool,
    session_factory: SessionFactory,
    clock: Callable[[], datetime],
    named_model: str | None = None,
) -> Response:
    """Tell the gatekeeper where the provider is, list its models, ask the cheapest one that calls
    tools one question and, if it answers, take the house's language from the browser and end the
    step (``_end_the_step``). Otherwise show why not (the provider is kept). A server with no list
    of models is asked for a name first. The question is counted in sessions of
    ``session_factory`` (``provider_keys.check_key``). A provider dropped meanwhile (another tab)
    is a 404, whatever the check found; a setup another request ended meanwhile moves on."""
    catalog = shipped_catalog()
    try:
        checked = await check_key(
            session,
            gatekeeper,
            provider,
            model_factory,
            model_lister,
            catalog=catalog,
            now=datetime.now(UTC),
            session_factory=session_factory,
            named_model=named_model,
        )
    except GatekeeperError as error:
        return _failure(
            request, gatekeeper_failure(error), provider=provider, whole_step=whole_step
        )
    if checked.error is not None:
        # The provider may have been dropped (another tab) during the check: the account of the
        # question then found it gone (``no_model``).
        if not await _still_there(session, provider.id):
            raise HTTPException(status_code=404)
        return _failure(request, checked.error, provider=provider, whole_step=whole_step)
    if not checked.models:
        return _ask_the_name(request, provider, whole_step=whole_step)
    # The key must hold a conversation (the check found a model that calls tools, so it does).
    conversation = pick_model(
        candidates(catalog, provider, checked.models, {}), "conversation", wanted="medium"
    )
    if conversation is None:
        return _failure(request, "no_model", provider=provider, whole_step=whole_step)

    # Another request may have ended the step while the model was answering: nothing more to do.
    if await setup_stage(session) == "done":
        await session.commit()
        return Response(headers={"HX-Redirect": HOME_PATH})
    # The provider may have been dropped (another tab) while the model was answering.
    if not await _still_there(session, provider.id):
        raise HTTPException(status_code=404)
    await _take_the_browser_language(request, session, person, clock())
    return await _end_the_step(session, person, provider)


@router.get(MODEL_PATH)
async def model_page(
    request: Request,
    person: Annotated[Person, Depends(require_owner)],
) -> Response:
    """The step: the form."""
    # No ``person``: the setup pages have no navigation (like the owner step), only their step.
    return render(
        request,
        "setup_model.html",
        kinds=list(PROVIDER_KINDS),
        kind="",
        base_url="",
        has_key=False,
        error_key=None,
        error_params={},
    )


@router.post(MODEL_PATH)
async def choose_model(
    request: Request,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    kind: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    has_key: Annotated[str | None, Form()] = None,
) -> Response:
    key_ticked = has_key is not None and has_key.strip().lower() in _TRUE_VALUES
    choice = _parse_choice(kind, base_url, key_ticked)
    if isinstance(choice, _FormError):
        return _form(
            request,
            status_code=400,
            error=choice,
            kind=kind,
            base_url=base_url,
            has_key=key_ticked,
        )

    provider_id = new_provider_id()
    provider = Provider(
        id=provider_id,
        kind=choice.kind,
        base_url=choice.base_url,
        given_by=person.id,
        secret_ref=None if choice.binding is None else f"provider:{provider_id}:api_key",
    )
    if choice.binding is not None:
        return await _ask_for_key(request, session, gatekeeper, person, provider, recorded=False)
    # No key to wait for: record the provider (so that a retry has it) and check the model.
    session.add(provider)
    await session.commit()
    return await _finish(
        request,
        session,
        gatekeeper,
        model_factory,
        model_lister,
        person,
        provider,
        whole_step=False,
        session_factory=session_factory,
        clock=clock,
    )


@router.get(MODEL_PATH + "/status/{provider_id}/{request_id}")
async def secret_status(
    request: Request,
    provider_id: str,
    request_id: str,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    """Where the key's request stands: still waiting (the poll goes on), over without a key (a new
    form can be opened), or filled in (the model is checked)."""
    provider = await _load_provider(session, provider_id, keyed=True)
    try:
        view = await gatekeeper.get_secret_request(request_id)
    except GatekeeperError as error:
        if error.status == 404:
            raise HTTPException(status_code=404) from None
        return _failure(request, gatekeeper_failure(error), provider=provider, whole_step=True)
    if view.ref != provider.secret_ref:
        raise HTTPException(status_code=404)  # another provider's request
    match view.status:
        case "pending":
            return render(
                request, "fragments/secret_poll.html", status_url=_status_url(provider, view)
            )
        case "expired" | "superseded":
            return _failure(request, view.status, provider=provider, whole_step=True)
        case "fulfilled":
            return await _finish(
                request,
                session,
                gatekeeper,
                model_factory,
                model_lister,
                person,
                provider,
                whole_step=True,
                session_factory=session_factory,
                clock=clock,
            )


@router.post(MODEL_PATH + "/{provider_id}/rekey")
async def rekey(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
) -> Response:
    """A new form for the same key (same reference, same destination): the gatekeeper replaces
    the earlier request and, when the new value is saved, rotates the secret."""
    provider = await _load_provider(session, provider_id, keyed=True)
    return await _ask_for_key(request, session, gatekeeper, person, provider, recorded=True)


@router.post(MODEL_PATH + "/{provider_id}/retry")
async def retry(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Response:
    """Check a provider without a key again. A provider with a key is checked when its key is in:
    its check cannot be asked for here."""
    provider = await _load_provider(session, provider_id, keyed=False)
    return await _finish(
        request,
        session,
        gatekeeper,
        model_factory,
        model_lister,
        person,
        provider,
        whole_step=False,
        session_factory=session_factory,
        clock=clock,
    )


@router.post(MODEL_PATH + "/{provider_id}/name")
async def name_model(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    model: Annotated[str, Form()] = "",
) -> Response:
    """The name of the model a server that gives no list serves: the provider is checked with it."""
    provider = await _load_provider(session, provider_id, keyed=None)
    name = model.strip()
    if not name or len(name) > MAX_MODEL_NAME_LENGTH or has_control_characters(name):
        return _ask_the_name(request, provider, whole_step=False, status_code=400, error=True)
    return await _finish(
        request,
        session,
        gatekeeper,
        model_factory,
        model_lister,
        person,
        provider,
        whole_step=False,
        session_factory=session_factory,
        clock=clock,
        named_model=name,
    )


@router.post(MODEL_PATH + "/{provider_id}/discard")
async def discard(
    request: Request,
    provider_id: str,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Forget the provider and show the form again. The secret stays in the vault, unused."""
    provider = await _load_provider(session, provider_id, keyed=None)
    await session.delete(provider)
    await session.commit()
    return _form(request)
