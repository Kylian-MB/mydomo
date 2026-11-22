"""The proof a card of the page offers its viewer: the means they have agreed that the page can
carry (``proofs.service.offers``), and where the card asks for a passkey's options and posts the
proof."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.passkeys import PasskeySite
from domo_core.channels.page.abilities import PAGE
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import is_past_lifetime, may_decide
from domo_core.confirmations.store import ask_of
from domo_core.policy.settings import load_policy_settings
from domo_core.proofs.kinds import ProofPlace
from domo_core.proofs.service import offers

__all__ = [
    "PROOF_OPTIONS_PATH",
    "PROVE_PATH",
    "ProofOffer",
    "page_place",
    "proof_offer",
    "proof_offers_of",
]

# Where the card asks for a passkey's options, and where it posts a proof.
PROOF_OPTIONS_PATH: Final = "/confirmations/{confirmation_id}/proof/options"
PROVE_PATH: Final = "/confirmations/{confirmation_id}/prove"


def page_place(site: PasskeySite | None) -> ProofPlace:
    """Where a proof is asked on the page: what the page carries, and the site where it was
    opened, if passkeys work there."""
    return ProofPlace(PAGE, site)


@dataclass(frozen=True)
class ProofOffer:
    """The proof a card offers its viewer: the ``means`` they may use (kinds of
    ``proofs.kinds.PROOF_KINDS``, ``passkey`` first when it is), where the card asks for a
    passkey's options (``options_url``) and where it posts the proof (``prove_url``)."""

    means: tuple[str, ...]
    options_url: str
    prove_url: str


async def proof_offers_of(
    session: AsyncSession,
    requests: Iterable[Confirmation],
    viewer: Person,
    site: PasskeySite | None,
    now: datetime,
) -> dict[uuid.UUID, ProofOffer]:
    """The proof each of ``requests`` offers ``viewer``, by request id: only a request that asks
    a proof, waits (within its lifetime) and that ``viewer`` may answer (``may_decide``) offers
    one. Reads only."""
    asking = [
        c
        for c in requests
        if ask_of(c) == "proof" and c.status == "pending" and may_decide(c, viewer, now)
    ]
    if not asking:
        return {}
    settings = await load_policy_settings(session)
    asking = [c for c in asking if not is_past_lifetime(c, settings, now)]
    # The means depend on the viewer and the page, not on the request.
    means = await offers(session, viewer, page_place(site), now) if asking else ()
    if not means:
        return {}
    return {
        c.id: ProofOffer(
            means=means,
            options_url=PROOF_OPTIONS_PATH.format(confirmation_id=c.id),
            prove_url=PROVE_PATH.format(confirmation_id=c.id),
        )
        for c in asking
    }


async def proof_offer(
    session: AsyncSession,
    c: Confirmation,
    viewer: Person,
    site: PasskeySite | None,
    now: datetime,
) -> ProofOffer | None:
    """The proof the request ``c`` offers ``viewer`` (``proof_offers_of``), or ``None``."""
    return (await proof_offers_of(session, [c], viewer, site, now)).get(c.id)
