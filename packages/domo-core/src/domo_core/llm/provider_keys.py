"""The steps that put a model provider's key to use: asking the owner for it, in the gatekeeper's
vault form, and checking that it works.

The core never holds a key. It asks the gatekeeper for a one-time form (``request_key``), which
the owner fills in, in a frame of a page of the core: the key goes from the browser to the vault.
What the form says is in the words of the one who gives the key (``KeyForm``, spec 4.5): once
Domo is installed, the texts they read (``key_form``: their own, else their language's, else the
default phrases), whoever asks for the form and whatever texts the asking reads (a wake reads the
reference texts); during the first launch, the fixed texts of the installation, and the
gatekeeper's own on the form (``install_key_form``). As a page does, opening a form asks for the
texts it showed that are not the giver's own yet, with the writing note Domo keeps for them:
written in the background when he has a reason to (``scheduler.TextWriting``, never waited for),
the next form is in their own words. Their language
goes with the texts as a tag, and as ``und`` (undetermined) should it ever be something else (the
house's language, which they read until a page of theirs keeps one, takes only a tag).
Once the form is filled in, the core tells the gatekeeper where the provider is and which secret
unlocks it, lists the models the key opens and asks the cheapest one that calls tools one question
through the gatekeeper's relay (``check_key``). That question is counted on Domo's account like
any call (kind of task ``key_check``, for the one who gave the key), and is not asked when it would
pass a firm limit of the key (``limit_reached``). The first launch (``setup_flow.model_routes``)
and the chat (``tools.model_key`` and ``channels.page.key_routes``) take the same steps.

A key is only ever sent to the destination its kind of provider says (``binding_for``): the binding
is built from the address that is recorded, never chosen by a visitor. A key that is asked for
again keeps its reference and its binding, which the gatekeeper treats as a rotation (the bindings
of a secret never change).

The gatekeeper's refusals are ``GatekeeperError`` (each one logged where it is raised, in
``gatekeeper_client``); ``gatekeeper_failure`` says which of them a person can be told about.

A key's routes are ``<id>`` and ``<id>-admin`` (``admin_route_id``, for the admin key that reads
the real spending). A key removed has both declared again without a secret
(``declare_without_key``), so that no call can use its keys any more. What declares a route with
its secret and then talks through it without the person who gave it there (a pass of the upkeep,
say) reads the key again after the call (``key_still_there``): a key removed meanwhile has its
routes declared again without a secret, so that the route declared during the call does not keep
it.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from domo_common.gatekeeper_api import (
    FORM_TEXT_KEYS,
    Binding,
    ProviderRoute,
    SecretFields,
    SecretRequestCreate,
    SecretRequestView,
)
from domo_common.text_rules import LANGUAGE_TAG
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import BindingConflictError, GatekeeperClient, GatekeeperError
from domo_core.llm.catalog import Catalog
from domo_core.llm.choice import candidates, pick_model
from domo_core.llm.factory import ModelFactory
from domo_core.llm.listing import ModelLister
from domo_core.llm.metered import MeteredModel
from domo_core.llm.models import Provider, ProviderModel
from domo_core.llm.probe import probe_model
from domo_core.llm.provider_kinds import PROVIDER_KINDS, ProviderKind
from domo_core.llm.router import Route, owner_levels
from domo_core.llm.types import ErrorKind, ModelCallError
from domo_core.texts.store import Reader
from domo_core.texts.view import RecueilTexts, Texts, person_texts, shown_groups

if TYPE_CHECKING:
    from domo_core.texts.scheduler import TextWriting

logger = logging.getLogger(__name__)

# Where the gatekeeper serves its one-time forms (and nowhere else may a frame point).
VAULT_FORM_PATH: Final = re.compile(r"/vault/r/[A-Za-z0-9_-]+")
# The text that says what a key's form is for: Domo's words once he is installed, the fixed
# text of the first launch before.
PURPOSE_KEY: Final = "chat.key.secret_purpose"
INSTALL_PURPOSE_KEY: Final = "setup.model.secret_purpose"
# The operation of the gatekeeper's API that opens a vault form.
_SECRET_REQUESTS: Final = "POST /internal/secret-requests"
# The language of texts whose language has no tag (BCP 47: undetermined).
UNDETERMINED: Final = "und"

# What a refusal of the gatekeeper's API comes to, for the person: a destination the vault keeps
# for other bindings, a key it will not send in cleartext, or any other failure.
GatekeeperFailure = Literal["binding_conflict", "insecure_transport", "gatekeeper"]


def provider_kind(value: str) -> ProviderKind | None:
    """The kind of provider named ``value``, if it is one."""
    return next((kind for kind in PROVIDER_KINDS if kind == value), None)


def known_kind(provider: Provider) -> ProviderKind:
    """The kind of a recorded provider (it was checked when it was recorded)."""
    kind = provider_kind(provider.kind)
    if kind is None:
        raise RuntimeError(f"the provider {provider.id} has an unknown kind: {provider.kind!r}")
    return kind


def binding_for(kind: ProviderKind, base_url: str) -> Binding:
    """Where the key of a provider of ``kind`` at ``base_url`` goes: its host (lower case), its
    port if the address names one, and the place its API expects the key.

    Raises ``pydantic.ValidationError`` for a host a binding cannot name (an IPv6 literal, a
    non-ASCII name)."""
    parts = urlsplit(base_url)
    return Binding(
        host=parts.hostname or "", port=parts.port, location=PROVIDER_KINDS[kind].key_location
    )


@dataclass(frozen=True)
class KeyForm:
    """Whose words the vault form of a key is in (see the module docstring): ``texts``, what the
    one who gives the key reads (``key_form``); during the first launch, the fixed texts of the
    installation (``installing``, ``install_key_form``), and the gatekeeper's own on the form.
    ``giver`` is the one who gives the key, whose texts ``writing`` writes in the background, with
    the writing note Domo keeps for them (``giver_note``)."""

    texts: Texts
    installing: bool = False
    giver: UUID | None = None
    writing: "TextWriting | None" = None
    giver_note: str | None = None

    def request(self, ref: str, binding: Binding, purpose_key: str) -> SecretRequestCreate:
        """What the gatekeeper is asked for: a form for the secret ``ref``, sent only to
        ``binding``, which says what it is for (the text of ``purpose_key``) and, once Domo is
        installed, the texts of the form, their holes left in for the gatekeeper to fill. A text
        the one who gives the key has none of yet is the sign.

        Raises ``GatekeeperError`` (``invalid_request``) for a request the gatekeeper would
        refuse, so that it fails as the gatekeeper's refusals do: nothing is asked of it."""
        return self.request_for(ref, [binding], self.texts(purpose_key))

    def request_for(
        self,
        ref: str,
        bindings: Sequence[Binding],
        purpose: str,
        *,
        fields: SecretFields = "value",
    ) -> SecretRequestCreate:
        """What the gatekeeper is asked for, as ``request`` says, for a secret sent only to
        ``bindings``, made of ``fields`` (one value, or an account's user name and password), whose
        form says ``purpose``, a text already in the words of the one who gives it (an account
        Domo asks for, ``outward.service``: its name and where it is placed). Raises
        ``GatekeeperError`` (``invalid_request``) as ``request`` does."""
        texts = self.texts
        language = texts.language if LANGUAGE_TAG.fullmatch(texts.language) else UNDETERMINED
        try:
            if self.installing:
                return SecretRequestCreate(
                    ref=ref, purpose=purpose, bindings=list(bindings), fields=fields
                )
            request = SecretRequestCreate(
                ref=ref,
                purpose=purpose,
                bindings=list(bindings),
                fields=fields,
                language=language,
                texts={key: texts.template(key) for key in sorted(FORM_TEXT_KEYS)},
            )
        except ValidationError:
            # That it failed only, never the texts or what was wrong with them.
            logger.error("A request for a vault form could not be made from its texts")
            raise GatekeeperError(_SECRET_REQUESTS, code="invalid_request") from None
        self._ask_writing()
        return request

    def _ask_writing(self) -> None:
        """Have the texts the form showed that are not the giver's own yet written for them, in
        the background; nothing without ``writing``. A failure to ask is logged, and the form is
        asked for all the same."""
        texts, writing, giver = self.texts, self.writing, self.giver
        if writing is None or giver is None or not isinstance(texts, RecueilTexts):
            return
        try:
            groups = shown_groups(texts)
            if groups:
                writing.ask(Reader(giver, texts.language), groups, writing_note=self.giver_note)
        except Exception as error:
            # The kind of error only.
            logger.error("Asking for the texts of a vault form failed (%s)", type(error).__name__)


async def key_form(
    session: AsyncSession, giver: Person, *, writing: "TextWriting | None" = None
) -> KeyForm:
    """The vault form of a key ``giver`` gives, once Domo is installed: in the texts they read
    (``view.person_texts``), never in those of whoever asks for it; what it shows that is not
    their own yet is written for them by ``writing`` once the form is asked for (none without
    it). Reads only."""
    return KeyForm(
        await person_texts(session, giver),
        giver=giver.id,
        writing=writing,
        giver_note=giver.writing_note,
    )


def install_key_form(texts: Texts) -> KeyForm:
    """The vault form of the first launch: its purpose in ``texts``, the fixed texts of the
    installation, and the gatekeeper's own texts on the form."""
    return KeyForm(texts, installing=True)


async def request_key(
    gatekeeper: GatekeeperClient, provider: Provider, form: KeyForm
) -> SecretRequestView:
    """Ask the gatekeeper for a one-time form where the person types the key of ``provider``:
    for the provider's reference (``secret_ref``) and its binding, in the words of ``form``, which
    says what the key is for (``PURPOSE_KEY``; the first launch's own, ``INSTALL_PURPOSE_KEY``).

    A form asked for again, for the same reference and the same destination, replaces the one that
    is still open, and its value rotates the secret. Raises ``GatekeeperError``
    (``BindingConflictError`` when the reference exists with other destinations), and
    ``ValueError`` for a provider that takes no key."""
    ref = provider.secret_ref
    if ref is None:
        raise ValueError(f"the provider {provider.id} takes no key")
    purpose_key = INSTALL_PURPOSE_KEY if form.installing else PURPOSE_KEY
    return await gatekeeper.create_secret_request(
        form.request(ref, binding_for(known_kind(provider), provider.base_url), purpose_key)
    )


def vault_form_path(view: SecretRequestView) -> str | None:
    """The path of the request's form, if it is the one of a pending request on the gatekeeper."""
    path = view.form_path
    if view.status == "pending" and path is not None and VAULT_FORM_PATH.fullmatch(path):
        return path
    return None


@dataclass(frozen=True)
class KeyCheck:
    """What the check of a key found: why it failed (``None`` if it did not), the models the key
    lists (none when the server gives no list and none was named) and the one the probe used."""

    error: ErrorKind | None
    models: tuple[str, ...]
    checked_with: str | None


async def general_when_none(session: AsyncSession, provider: Provider) -> None:
    """``provider``, a key that works (it lists models), becomes the household's general key when
    there is none and the one who gave it is the owner of now (ruling R65: the general key removed
    with no other key of its giver's to take over leaves none, and the next key of the owner's that
    works takes its place). Never a key of anyone else. In the caller's transaction, uncommitted."""
    if provider.is_general:
        return
    general = await session.scalar(select(Provider.id).where(Provider.is_general).limit(1))
    if general is not None:
        return
    giver_role = await session.scalar(select(Person.role).where(Person.id == provider.given_by))
    if giver_role == "owner":
        await session.execute(
            update(Provider).where(Provider.id == provider.id).values(is_general=True)
        )
        provider.is_general = True


async def check_key(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    provider: Provider,
    model_factory: ModelFactory,
    model_lister: ModelLister,
    *,
    catalog: Catalog,
    now: datetime,
    session_factory: SessionFactory,
    named_model: str | None = None,
) -> KeyCheck:
    """Tell the gatekeeper where ``provider`` is and which secret unlocks it (none for a provider
    without a key), list the models the key opens, record them (``provider_models``, listed at
    ``now``) and ask the cheapest one that calls tools one question through the relay.

    A server that gives no list is given ``named_model`` instead; without a name it gives
    ``KeyCheck(None, (), None)`` and nothing is asked. A key of the owner's that lists models
    becomes the general key when there is none (``general_when_none``). A list with no model that
    calls tools is
    ``no_model``. Otherwise the error is the kind of the failure of the listing or of the question
    (``auth`` for a key the provider refuses, ``limit_reached`` for a question that would pass a
    firm limit of the key: it is not asked). Raises ``GatekeeperError`` when the gatekeeper
    refuses the route (see ``gatekeeper_failure``): then nothing is asked.

    The database work is committed before the question, so that no transaction waits for the
    model. The question's account is kept in sessions of its own, from ``session_factory``
    (``metered.MeteredModel``): whatever it meets, the caller's session and what it holds are
    left as they are."""
    await gatekeeper.register_provider(
        ProviderRoute(id=provider.id, base_url=provider.base_url, secret_ref=provider.secret_ref)
    )
    try:
        models = await model_lister(provider)
    except ModelCallError as error:
        return KeyCheck(error.kind, (), None)
    if not models and named_model:
        models = (named_model,)
    if not models:
        return KeyCheck(None, (), None)
    await session.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider.id))
    session.add_all(
        ProviderModel(provider_id=provider.id, model_id=model_id, listed_at=now)
        for model_id in models
    )
    await general_when_none(session, provider)
    levels = await owner_levels(session, provider.id)
    await session.commit()
    chosen = pick_model(candidates(catalog, provider, models, levels), "key_check", wanted="small")
    if chosen is None:
        return KeyCheck("no_model", models, None)
    route = Route(
        provider=provider,
        model_id=chosen.model_id,
        level=chosen.level,
        prices=chosen.prices,
        task="key_check",
        price_known=chosen.price_known,
    )
    probe = MeteredModel(
        model_factory(provider, chosen.model_id),
        route=route,
        person_id=provider.given_by,
        session_factory=session_factory,
        clock=lambda: now,
        catalog=catalog,
    )
    try:
        await probe_model(probe)
    except ModelCallError as error:
        return KeyCheck(error.kind, models, chosen.model_id)
    return KeyCheck(None, models, chosen.model_id)


def admin_route_id(provider_id: str) -> str:
    """The id of the gatekeeper's route that injects the admin key of the key ``provider_id``."""
    return f"{provider_id}-admin"


async def declare_without_key(gatekeeper: GatekeeperClient, provider: Provider) -> None:
    """Declare both routes of the key ``provider`` (``<id>`` and ``<id>-admin``) again without a
    secret: no call can use its keys any more. Raises ``GatekeeperError``."""
    for route_id in (provider.id, admin_route_id(provider.id)):
        await gatekeeper.register_provider(
            ProviderRoute(id=route_id, base_url=provider.base_url, secret_ref=None)
        )


async def key_still_there(
    session: AsyncSession, gatekeeper: GatekeeperClient, provider: Provider
) -> bool:
    """Whether the key ``provider`` is still there, read again after a call through one of its
    routes, and then locked until the caller's transaction ends: a removal waits for it, so what
    the caller writes goes away with the key. A key removed meanwhile has both of its routes
    declared again without a secret (``declare_without_key``), since the call declared one with
    its secret, and the caller writes nothing. A gatekeeper that refuses is logged, and the caller
    still writes nothing.

    The caller has written nothing yet in its transaction: when the key is gone, the transaction
    is ended by a commit, which leaves the session's objects readable (a rollback would expire
    them)."""
    found = await session.execute(
        select(Provider.id).where(Provider.id == provider.id).with_for_update()
    )
    if found.first() is not None:
        return True
    await session.commit()
    try:
        await declare_without_key(gatekeeper, provider)
    except GatekeeperError:
        logger.error("The routes of a key removed meanwhile could not be declared without it")
    return False


def gatekeeper_failure(error: GatekeeperError) -> GatekeeperFailure:
    """What a failure of the gatekeeper's API comes to, for the person."""
    if isinstance(error, BindingConflictError):
        return "binding_conflict"
    if error.code == "insecure_transport":
        return "insecure_transport"
    return "gatekeeper"
