"""The kinds of proof the code knows how to verify, and the rule every kind keeps.

A person's agreed means (``proofs.models.ProofMean``) each point to one of these kinds: the kind is
what knows how a proof of its sort is asked and checked. There are two today, ``passkey`` and
``password``.

The rule that holds for every kind, today's and tomorrow's
-----------------------------------------------------------
* A proof is verified by the code, never by a model. A kind receives the person and the database,
  never a text of the conversation: what it is given is ``(session, person, place, now)``, and
  ``answer`` for ``check``. No word of the person to Domo, no reply of Domo, no tool result can
  reach it.
* The answer is given where the place carries it (a form of the page, ``POST
  /confirmations/{id}/prove``), never through the model: Domo never sees a password, a passkey's
  answer or a code (spec 6.2), and it never reaches a kept message, the audit or the logs.
* A kind that sends something (an SMS, a mail, a notification: one day) reads its destination only
  in what the database holds of the person, rows the person registered themselves, never a
  destination that came from the conversation or from an outside content: otherwise someone could
  have the proof of another sent to themselves. It sends in ``begin``, and gives back only what
  the place shows (a masked destination).
* The button is not a proof and is no kind: a click never satisfies a demand for a proof.

The kinds of now
----------------
* ``passkey`` (``PasskeyProof``): the device's passkey, with the person verified (fingerprint,
  face, the device's PIN): ``accounts.passkeys.proof_options`` and ``finish_proof``. Offered where
  the place has a site for passkeys (``ProofPlace.passkey_site``: a host name of the household's,
  over https) to someone who has one. Its challenge belongs to the person's account, and is spent
  in the same transaction as the check: an answer is good once.
* ``password`` (``PasswordProof``): the person's password, verified as at the login (argon2, on its
  limited path). Offered everywhere. It can be guessed, so ``proofs.service.prove`` counts its
  attempts with the account page's (``accounts.throttle.password_check_key``): five wrong ones in
  fifteen minutes, then it holds back (``ProofHeldBack``), as for the account page and the password
  asked before adding a passkey: going from one to the other gives no more guesses.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkeys import (
    PasskeyRejected,
    PasskeySite,
    finish_proof,
    passkeys_of,
    proof_options,
)
from domo_core.accounts.passwords import verify_password_limited
from domo_core.conversation.channel import Abilities
from domo_core.llm.types import JsonObject

__all__ = [
    "PROOF_KINDS",
    "PasskeyProof",
    "PasswordProof",
    "ProofKind",
    "ProofPlace",
    "login_account_of",
]

logger = logging.getLogger(__name__)

# The longest answer of a browser that is read: an assertion is a few hundred bytes.
MAX_PASSKEY_ANSWER_LENGTH: Final = 64 * 1024


@dataclass(frozen=True)
class ProofPlace:
    """Where a proof is asked: what the channel can carry (``abilities``, whose ``proof_kinds``
    are the kinds that can be given here) and, where the page can use passkeys, the site they work
    for (``passkey_site``: a host name of the household's, over https; ``None`` elsewhere)."""

    abilities: Abilities
    passkey_site: PasskeySite | None = None


class ProofKind(Protocol):
    """A sort of proof the code verifies (see the module docstring for the rule every kind
    keeps). ``name`` is what a person's means points to and what the place posts (``means``);
    ``throttled`` is ``True`` for a kind whose answer can be guessed (a password): ``prove``
    counts and holds back its attempts."""

    name: str
    throttled: bool

    async def offered_to(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> bool:
        """Whether this kind can be asked of ``person`` in ``place`` (whether they have agreed it
        is for ``proofs.service.offers`` to say)."""
        ...

    async def begin(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> JsonObject:
        """What the place needs to start: the passkey's options; ``{}`` for a password. A kind that
        sends something sends it here, to a destination it reads in the person's own rows, and
        gives back only what the place shows (a masked destination)."""
        ...

    async def check(
        self,
        session: AsyncSession,
        person: Person,
        place: ProofPlace,
        answer: str,
        now: datetime,
    ) -> bool:
        """Whether ``answer``, what the place posted, proves it is ``person``: verified by code,
        never by a model. What it spends (a challenge) is written in ``session``, which the caller
        commits whatever the answer."""
        ...


async def login_account_of(session: AsyncSession, person: Person) -> Account | None:
    """The login account of ``person`` (everyone who can sign in has one)."""
    found = await session.execute(select(Account).where(Account.person_id == person.id))
    return found.scalar_one_or_none()


def _assertion_of(answer: str) -> JsonObject | None:
    """A passkey's answer, as the page posts it (JSON in a form field): an object, or ``None``."""
    if not answer or len(answer) > MAX_PASSKEY_ANSWER_LENGTH:
        return None
    try:
        value: Any = json.loads(answer)
    except (ValueError, RecursionError):
        return None
    return cast(JsonObject, value) if isinstance(value, dict) else None


class PasskeyProof:
    """The device's passkey, the person verified (``accounts.passkeys``): offered in a place where
    passkeys work, to someone who has one."""

    name: str = "passkey"
    throttled: bool = False

    async def offered_to(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> bool:
        if place.passkey_site is None:
            return False
        account = await login_account_of(session, person)
        return account is not None and bool(await passkeys_of(session, account.id))

    async def begin(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> JsonObject:
        """The options of ``navigator.credentials.get()`` for a ``proof`` challenge of the person's
        account (``proof_options``); ``PasskeyRejected("no_passkey")`` when none can be used."""
        account = await login_account_of(session, person)
        if place.passkey_site is None or account is None:
            raise PasskeyRejected("no_passkey")
        return await proof_options(session, account.id, place.passkey_site.rp_id, now)

    async def check(
        self,
        session: AsyncSession,
        person: Person,
        place: ProofPlace,
        answer: str,
        now: datetime,
    ) -> bool:
        """``finish_proof`` on the posted answer: the challenge is spent in ``session`` (F10), and
        an answer that is not a JSON object, or that ``finish_proof`` refuses, proves nothing."""
        account = await login_account_of(session, person)
        assertion = _assertion_of(answer)
        site = place.passkey_site
        if site is None or account is None or assertion is None:
            return False
        try:
            await finish_proof(session, site.rp_id, site.origin, assertion, account.id, now)
        except PasskeyRejected as rejected:
            # The reason is for the log, never for the person.
            logger.info("A proof by passkey was refused (%s)", rejected.reason)
            return False
        return True


class PasswordProof:
    """The person's password, verified as at the login: offered everywhere."""

    name: str = "password"
    throttled: bool = True

    async def offered_to(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> bool:
        return await login_account_of(session, person) is not None

    async def begin(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> JsonObject:
        return {}

    async def check(
        self,
        session: AsyncSession,
        person: Person,
        place: ProofPlace,
        answer: str,
        now: datetime,
    ) -> bool:
        """``verify_password_limited`` against the account's hash. The read is over before argon2
        runs: its transaction ends (the request's lock with it), so that nothing is held while
        argon2 runs or waits for its turn (R87). ``Argon2Busy`` passes through: nothing was
        verified."""
        account = await login_account_of(session, person)
        if account is None:
            return False
        password_hash = account.password_hash
        await session.commit()
        return await verify_password_limited(password_hash, answer)


# The kinds there are, in the order a place offers them: the passkey first, where it is offered.
PROOF_KINDS: Final[Mapping[str, ProofKind]] = {
    "passkey": PasskeyProof(),
    "password": PasswordProof(),
}
