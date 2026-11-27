"""The speech service of the house, declared as a provider like the others, without a key.

The service (``DOMO_SPEECH_URL``) hears, says and prints voices on the machine that hosts the
assistant. At the installation, once there is an owner, it is recorded as one key of kind
``openai_compatible`` with no secret, given by the owner and usable by the whole household (the
line of access a general key has), declared to the gatekeeper, and its models are listed through
the relay like any key's. No model is chosen here: the service lists the candidates that hold on
its machine, and the assistant chooses among them (``llm.router``).

``ensure_home_speech`` is idempotent: the key of this address is found, not doubled, and a service
whose address changed has its row updated: its id does not depend on the address
(``HOME_SPEECH_ID``), so a changed address is the same key. ``speech_home_pass`` is the pass of
the upkeep that makes it, at the start and again until the service has listed its models (it may
still be preparing): the service's own failure is tried again, never an error. Once it has a
list, the list is read again when a use of sound was found missing or a call to the service
failed (``SPEECH_LIST_WANTED``: a service that started again, or finished measuring its models
later, is used without restarting the core), at most every ``RELIST_AT_MOST_EVERY``; sooner
(``RELIST_SOON_EVERY``) while a use is still missing in the first minutes after the service first
listed models, when it may still be measuring them.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.listing import ModelLister
from domo_core.llm.models import Provider, ProviderModel, ProviderUser
from domo_core.llm.provider_keys import declare_without_key, known_kind
from domo_core.llm.refresh import relist_key
from domo_core.llm.speech.catalog import does_of
from domo_core.llm.speech.home_key import HOME_SPEECH_ID as HOME_SPEECH_ID
from domo_core.llm.tasks import Does
from domo_core.upkeep import UpkeepPass

logger = logging.getLogger(__name__)

_KIND: Final = "openai_compatible"
# How often, at most, the list of the service's models is read again once it has one, when a use
# of sound was found missing or a call to the service failed (final fix wave C).
RELIST_AT_MOST_EVERY: Final = timedelta(minutes=1)
# While a use of sound (``SOUND_USES``) was missing from the last list read, for a while after the
# service first listed models (a fresh start: it may still be measuring the others), the list
# asked for again is read sooner. Past that while, a use still missing is one the service does not
# have on this machine, and the minute holds.
RELIST_SOON_EVERY: Final = timedelta(seconds=5)
RELIST_SOON_FOR: Final = timedelta(minutes=10)
SOUND_USES: Final[frozenset[Does]] = frozenset({"hear", "say", "voice_print"})


class SpeechListWanted:
    """Whether the list of the speech service's models should be read again: a use of sound was
    found missing (``MeteredSpeechOpener.open``) or a call of sound failed (``MeteredSpeech``).
    Kept in the process; read by ``speech_home_pass``."""

    def __init__(self) -> None:
        self._wanted = False

    @property
    def wanted(self) -> bool:
        return self._wanted

    def want(self) -> None:
        self._wanted = True

    def clear(self) -> None:
        self._wanted = False


SPEECH_LIST_WANTED: Final = SpeechListWanted()


async def _the_key(session: AsyncSession, url: str, owner_id: UUID) -> Provider | None:
    """The speech service's own key (its fixed id, whatever its address), else a keyless key the
    owner gave that is at ``url``: a row someone else recorded at that address is not it."""
    own = await session.get(Provider, HOME_SPEECH_ID)
    if own is not None:
        return own
    at_url = await session.execute(
        select(Provider)
        .where(
            Provider.kind == _KIND,
            Provider.secret_ref.is_(None),
            Provider.base_url == url,
            Provider.given_by == owner_id,
        )
        .order_by(Provider.created_at, Provider.id)
    )
    return at_url.scalars().first()


async def _recorded(session: AsyncSession, url: str, owner_id: UUID, now: datetime) -> Provider:
    """The key of the service at ``url``, recorded or updated and let to the whole household.
    Committed."""
    key = await _the_key(session, url, owner_id)
    if key is None:
        key = Provider(
            id=HOME_SPEECH_ID,
            kind=_KIND,
            base_url=url,
            secret_ref=None,
            given_by=owner_id,
            is_general=False,
        )
        session.add(key)
        await session.flush()
    else:
        key.base_url = url
    line = await session.execute(
        select(ProviderUser).where(
            ProviderUser.provider_id == key.id, ProviderUser.scope == "household"
        )
    )
    if line.scalars().first() is None:
        session.add(
            ProviderUser(
                provider_id=key.id,
                scope="household",
                person_id=None,
                allowed=True,
                set_by=key.given_by,
                set_at=now,
            )
        )
    await session.commit()
    return key


async def ensure_home_speech(
    session_factory: SessionFactory,
    gatekeeper: GatekeeperClient,
    lister: ModelLister,
    *,
    url: str | None,
    owner_id: UUID | None,
    now: datetime,
) -> Provider | None:
    """The key of the house's speech service at ``url``, recorded, declared to the gatekeeper and
    its models listed; ``None`` without an address or without an owner yet. A gatekeeper that
    refuses, or a service that does not list yet, leaves the key recorded with the models it had
    (none at first): the next pass tries again."""
    if not url or owner_id is None:
        return None
    async with session_factory() as session:
        key = await _recorded(session, url, owner_id, now)
    try:
        await declare_without_key(gatekeeper, key)
    except GatekeeperError:
        return key  # logged where it is raised
    async with session_factory() as session:
        await relist_key(session, gatekeeper, key, lister, now=now)
    return key


async def _the_owner(session_factory: SessionFactory) -> UUID | None:
    async with session_factory() as session:
        found = await session.execute(
            select(Person.id)
            .where(Person.role == "owner", Person.disabled_at.is_(None))
            .order_by(Person.created_at)
        )
        return found.scalars().first()


async def _listed(session_factory: SessionFactory, provider: Provider) -> frozenset[Does]:
    """The uses of the models the key ``provider`` lists (``does_of``: ``chat`` for one that is
    not a model of sound)."""
    kind = known_kind(provider)
    async with session_factory() as session:
        found = await session.execute(
            select(ProviderModel.model_id).where(ProviderModel.provider_id == provider.id)
        )
        return frozenset(does_of(kind, model_id) for model_id in found.scalars())


def speech_home_pass(
    session_factory: SessionFactory,
    gatekeeper: Callable[[], GatekeeperClient],
    lister: Callable[[], ModelLister],
    *,
    url: str | None,
    wanted: SpeechListWanted = SPEECH_LIST_WANTED,
) -> UpkeepPass:
    """The pass of the upkeep that declares the house's speech service (``ensure_home_speech``)
    as soon as there is an owner, and tries again each round until it has listed its models (it
    may still be preparing). Once it has, its list is read again only when ``wanted`` asks it, at
    most every ``RELIST_AT_MOST_EVERY``, or every ``RELIST_SOON_EVERY`` while a use of sound was
    missing from the last list, for ``RELIST_SOON_FOR`` after the first; the other keys' lists
    are kept by ``model_lists_pass``."""
    done = False
    read_at: datetime | None = None
    # When the service first listed models, and whether a use of sound was missing at the last read.
    first_listed_at: datetime | None = None
    missing = False

    def gap(now: datetime) -> timedelta:
        soon = missing and first_listed_at is not None and now < first_listed_at + RELIST_SOON_FOR
        return RELIST_SOON_EVERY if soon else RELIST_AT_MOST_EVERY

    async def declare_the_service(now: datetime) -> None:
        nonlocal done, read_at, first_listed_at, missing
        if not url:
            return
        if done:
            if not wanted.wanted or (read_at is not None and now < read_at + gap(now)):
                return
            # Cleared before the read: a call that fails meanwhile asks again.
            wanted.clear()
        read_at = now
        owner_id = await _the_owner(session_factory)
        key = await ensure_home_speech(
            session_factory, gatekeeper(), lister(), url=url, owner_id=owner_id, now=now
        )
        if key is None:
            return
        uses = await _listed(session_factory, key)
        missing = not uses >= SOUND_USES
        if not done and uses:
            done = True
            first_listed_at = now
            logger.info("The speech service of the house is declared")

    return declare_the_service
