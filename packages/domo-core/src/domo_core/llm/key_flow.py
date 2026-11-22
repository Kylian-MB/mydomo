"""Adding a key of one's own, from the chat (0.6.0, spec 4.2): the steps the page ``KEY_NEW_PATH``
(``channels.page.key_routes``) and the tool ``key_add`` (``tools.key_tools``) share. The first
launch keeps its own steps and its own audit entry (``setup_flow.model_routes``,
``provider.added``); it shares only the check of what was chosen (``check_choice``).

* ``check_choice``: what the person chose (a kind of provider, the address of a server of their
  own, whether it takes a key) is checked as at the first launch. A cloud provider's address is
  the one its kind says; a key never crosses the network in cleartext, unless the server is on the
  local network (``domo_common.transport``).
* ``start_key``: the key is created for the person who gives it (``Provider.given_by``: it is
  theirs), never the household's general key. For a key in the vault, the gatekeeper is asked for
  its one-time form first (``provider_keys.request_key``, in the words of the one who gives it:
  ``provider_keys.key_form``), so that a refusal leaves no row behind.
  Its start is audited (``key.started``: ``{"provider_id", "kind", "host"}``, and the request
  whose yes it was started under, when there is one): it marks the keys that come from the chat
  (``start_of``), and binds a yes to the one key started under it (``started_under``): one yes,
  one key. The caller audits the form (``secret.requested``: the reference, never the form's path)
  and commits.
* ``finish_key``: once the form is filled in (or at once, for a server without a key), the key is
  checked as at the first launch (``provider_keys.check_key``: its route, the models it opens, one
  question). A key that works is audited ``key.added`` (``{"provider_id", "kind", "host",
  "models"}``, and the request whose yes it was started under, when there was one), once whatever
  the number of answers that ask at the same time. A key that does not work keeps no model: no
  request goes on it until a key that works is entered (the choice of a request passes over a key
  without a model, ``router.choose_key``).

The core never holds the key: it goes from the browser to the vault. Only its reference
(``provider:<id>:api_key``) is ever written.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import quote, urlsplit

from domo_common.gatekeeper_api import Binding, ProviderRoute, SecretRequestView
from domo_common.transport import cleartext_allowed
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import APPEND_LOCK_KEY, append_audit
from domo_core.audit.models import AuditEntry
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.catalog import Catalog
from domo_core.llm.factory import ModelFactory
from domo_core.llm.listing import ModelLister
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.provider_keys import KeyCheck, KeyForm, binding_for, check_key, request_key
from domo_core.llm.provider_kinds import PROVIDER_KINDS, ProviderKind

# The page of a new key (``channels.page.key_routes``): its form, and the routes that follow it.
KEY_NEW_PATH: Final = "/chat/keys/new"
# Where the page follows the vault form of a new key: ``<KEY_NEW_STATUS_PATH>/<id>/<request id>``.
KEY_NEW_STATUS_PATH: Final = f"{KEY_NEW_PATH}/status"
# The audit entries of a key started from the chat, and of a key added, once it works.
KEY_STARTED: Final = "key.started"
KEY_ADDED: Final = "key.added"
# The longest name of the model of a server that gives no list.
MAX_MODEL_NAME_LENGTH: Final = 200


class KeyChoiceError(Exception):
    """What was chosen cannot be a key: ``what`` is ``base_url`` (an address the gatekeeper would
    not take) or ``insecure_transport`` (a key in cleartext to a server off the local network), as
    the messages name them (``setup.model.error.<what>`` at the first launch,
    ``chat.key.new.error.<what>`` once Domo is installed)."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


@dataclass(frozen=True)
class ChosenKey:
    """A choice of key, checked: the kind of provider, its address, and where its key goes
    (``binding``: ``None`` for a server that takes no key)."""

    kind: ProviderKind
    base_url: str
    binding: Binding | None


def check_choice(kind: ProviderKind, base_url: str | None, has_key: bool) -> ChosenKey:
    """``kind`` at ``base_url`` (only for a server of one's own: a cloud provider's address is the
    one its kind says), with a key (``has_key``; a cloud provider always takes one), checked as
    the gatekeeper checks a route. Raises ``KeyChoiceError``."""
    spec = PROVIDER_KINDS[kind]
    address = spec.default_base_url if spec.default_base_url is not None else (base_url or "")
    # The gatekeeper's own rule for an address (http or https, a host, no credentials, no query,
    # no space or control character), so that what is accepted here is accepted there.
    try:
        ProviderRoute(id="p-00000000", base_url=address, secret_ref=None)
    except ValidationError:
        raise KeyChoiceError("base_url") from None
    if not (spec.key_required or has_key):
        return ChosenKey(kind, address, None)
    parts = urlsplit(address)
    if parts.scheme != "https" and not cleartext_allowed(parts.hostname or ""):
        raise KeyChoiceError("insecure_transport")
    try:
        binding = binding_for(kind, address)
    except ValidationError:  # a host a binding cannot name (an IPv6 literal, a non-ASCII name)
        raise KeyChoiceError("base_url") from None
    return ChosenKey(kind, address, binding)


def new_key_status_url(provider_id: str, request_id: str) -> str:
    """The address that follows the vault form ``request_id`` of the new key ``provider_id``."""
    return f"{KEY_NEW_STATUS_PATH}/{provider_id}/{quote(request_id, safe='')}"


async def start_key(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    *,
    person: Person,
    kind: ProviderKind,
    base_url: str | None,
    has_key: bool,
    form: KeyForm,
    confirmation: str | None = None,
) -> tuple[Provider, SecretRequestView | None]:
    """A new key of ``person`` (``check_choice``), and, for a key in the vault, the one-time form
    the gatekeeper opened for it, in the words of ``form`` (``provider_keys.key_form`` of
    ``person``); ``None`` for a server without a key. The key is added to ``session`` with its
    start (``key.started``, with ``confirmation``, the request whose yes it is started under, if
    any), flushed, not committed: the caller audits the form and commits, or rolls back.

    Raises ``KeyChoiceError``, and ``GatekeeperError`` when the vault opens no form: then nothing is
    added."""
    choice = check_choice(kind, base_url, has_key)
    provider_id = new_provider_id()
    provider = Provider(
        id=provider_id,
        kind=choice.kind,
        base_url=choice.base_url,
        given_by=person.id,
        secret_ref=None if choice.binding is None else f"provider:{provider_id}:api_key",
    )
    view = None if provider.secret_ref is None else await request_key(gatekeeper, provider, form)
    session.add(provider)
    await session.flush()
    details: dict[str, str] = {
        "provider_id": provider.id,
        "kind": provider.kind,
        "host": urlsplit(provider.base_url).hostname or "",
    }
    if confirmation is not None:
        details["confirmation"] = confirmation
    # After the vault, so that no transaction holds the audit log's lock while it answers.
    await append_audit(session, actor=f"person:{person.id}", action=KEY_STARTED, details=details)
    return provider, view


@dataclass(frozen=True)
class KeyStart:
    """How a key was started from the chat: under the yes of the request ``confirmation``, or
    under none (``None``)."""

    confirmation: str | None


async def start_of(session: AsyncSession, provider_id: str) -> KeyStart | None:
    """How the key ``provider_id`` was started from the chat (``key.started``); ``None`` for a key
    that was not (the first launch's, say)."""
    found = await session.execute(
        select(AuditEntry.details)
        .where(
            AuditEntry.action == KEY_STARTED,
            AuditEntry.details.contains({"provider_id": provider_id}),
        )
        .order_by(AuditEntry.id)
        .limit(1)
    )
    details = found.scalar_one_or_none()
    if details is None:
        return None
    confirmation = details.get("confirmation")
    return KeyStart(confirmation if isinstance(confirmation, str) else None)


async def started_under(session: AsyncSession, request_ids: Sequence[str]) -> list[str]:
    """The keys started under the yes of one of the requests ``request_ids`` (their ids, oldest
    first): one yes, one key."""
    if not request_ids:
        return []
    found = await session.execute(
        select(AuditEntry.details)
        .where(
            AuditEntry.action == KEY_STARTED,
            or_(*(AuditEntry.details.contains({"confirmation": one}) for one in request_ids)),
        )
        .order_by(AuditEntry.id)
    )
    started: list[str] = []
    for details in found.scalars():
        provider_id = details.get("provider_id")
        if isinstance(provider_id, str) and provider_id not in started:
            started.append(provider_id)
    return started


async def was_added(session: AsyncSession, provider_id: str) -> bool:
    """Whether the key ``provider_id`` was added from the chat and worked (``key.added``)."""
    found = await session.execute(
        select(AuditEntry.id)
        .where(
            AuditEntry.action == KEY_ADDED,
            AuditEntry.details.contains({"provider_id": provider_id}),
        )
        .limit(1)
    )
    return found.first() is not None


def works(checked: KeyCheck) -> bool:
    """Whether the check found a key that works: a model was asked, and answered."""
    return checked.error is None and checked.checked_with is not None


async def finish_key(
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
    confirmation: str | None = None,
) -> KeyCheck:
    """Check the new key ``provider`` (``provider_keys.check_key``, with ``named_model`` for a
    server that gives no list; its question counted in sessions of ``session_factory``) and say
    what came of it. A key that works is audited ``key.added``, once, with ``confirmation``, the
    request whose yes it was started under, if any; one that does not keeps no model.
    Committed.
    Raises ``GatekeeperError`` when the gatekeeper refuses the key's route (nothing is asked)."""
    checked = await check_key(
        session,
        gatekeeper,
        provider,
        model_factory,
        model_lister,
        catalog=catalog,
        now=now,
        session_factory=session_factory,
        named_model=named_model,
    )
    # The audit log's lock, held until the commit, makes the read and the entry one step: two
    # answers that check the key at the same time add it once.
    await session.execute(select(func.pg_advisory_xact_lock(APPEND_LOCK_KEY)))
    added = await was_added(session, provider.id)
    if works(checked) and not added:
        details: dict[str, str | int] = {
            "provider_id": provider.id,
            "kind": provider.kind,
            "host": urlsplit(provider.base_url).hostname or "",
            "models": len(checked.models),
        }
        if confirmation is not None:
            details["confirmation"] = confirmation
        # Last, just before the commit.
        await append_audit(
            session, actor=f"person:{provider.given_by}", action=KEY_ADDED, details=details
        )
    elif not works(checked) and not added and checked.models:
        # A key that does not work is chosen for no request.
        await session.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider.id))
    await session.commit()
    return checked
