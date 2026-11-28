"""The kinds of proof the code knows how to verify, and the rule every kind keeps.

A person's agreed means (``proofs.models.ProofMean``) each point to one of these kinds: the kind is
what knows how a proof of its sort is asked and checked. There are three today, ``passkey``,
``password`` and ``spoken_phrase``; the list of kinds is open: a kind is a name and a check by code.

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
* ``spoken_phrase`` (``SpokenPhraseProof``, spec 6.9): a phrase said aloud, agreed with Domo, kept
  encrypted by the gatekeeper and compared by it (``ProofPlace.phrase_check``, the gatekeeper's
  ``phrase_compare`` on ``phrase_ref(person)``). Offered where the place can have it compared. What
  was heard only crosses the core in memory on its way there, like a typed password: it is never
  stored, logged, audited or shown to a model. The gatekeeper answers in three ways (``Weighs``):
  ``it_is`` holds, ``almost`` is a failed try, ``nothing_like`` was no answer at all (an ordinary
  sentence, which goes its usual way); it counts the tries itself and may hold them back
  (``PhraseHeldBack``), so the kind is not ``throttled`` here. While held back it answers nothing
  of any text: the kind is then neither offered to the person nor weighed for them until the wait
  is over (``SpokenPhraseProof.held_back_for``), and what they say is not taken as a proof. The
  voice channel weighs a sentence only inside a proof window of the microphone that heard it
  (``channels.voice.session.PROOF_WINDOW_S``: the person has a request waiting for this proof
  and Domo spoke to them at that microphone in the last 90 seconds), never every sentence said
  while a proof waits; inside a window, while the phrase is held back, what is heard there is
  dropped rather than taken its usual way.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from domo_common.gatekeeper_api import PhraseResult
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
from domo_core.gatekeeper_client import PhraseHeldBack
from domo_core.llm.types import JsonObject

__all__ = [
    "PROOF_KINDS",
    "PasskeyProof",
    "PasswordProof",
    "PhraseCheck",
    "ProofKind",
    "ProofPlace",
    "SpokenPhraseProof",
    "Weighs",
    "login_account_of",
    "phrase_ref",
]

logger = logging.getLogger(__name__)

# The longest answer of a browser that is read: an assertion is a few hundred bytes.
MAX_PASSKEY_ANSWER_LENGTH: Final = 64 * 1024

# The gatekeeper's comparison of a heard text with a person's phrase: (person id, heard text) to its
# answer; raises ``gatekeeper_client.PhraseHeldBack`` while the person's phrase is held back.
PhraseCheck = Callable[[uuid.UUID, str], Awaitable[PhraseResult]]


@dataclass(frozen=True)
class ProofPlace:
    """Where a proof is asked: what the channel can carry (``abilities``, whose ``proof_kinds``
    are the kinds that can be given here) and, where the page can use passkeys, the site they work
    for (``passkey_site``: a host name of the household's, over https; ``None`` elsewhere), and,
    where a phrase can be heard, the gatekeeper's comparison of it (``phrase_check``)."""

    abilities: Abilities
    passkey_site: PasskeySite | None = None
    phrase_check: PhraseCheck | None = None


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


@runtime_checkable
class Weighs(Protocol):
    """A kind that can tell an answer that is not an attempt at all from a wrong one: ``prove``
    weighs it instead of checking it. ``holds`` and ``fails`` are a check's ``True`` and
    ``False``; ``not_an_answer`` leaves no trace, is not counted and proves nothing.

    Such a kind can also be held back for a person (``held_back_for``): the answers it would weigh
    then are not taken, whatever they are, and ``proofs.service.offers`` leaves it out until the
    wait is over (``offered_to`` itself says only whether the place could carry it, wait or not).
    While held back, ``weigh`` raises ``gatekeeper_client.PhraseHeldBack`` without asking anyone.
    Agreeing or removing the means forgets the wait (``forget_wait``, called by
    ``proofs.service.agree`` and ``remove``).
    """

    def held_back_for(self, person_id: uuid.UUID, now: datetime) -> float | None:
        """The seconds left before this kind weighs ``person_id``'s answers again, else ``None``."""
        ...

    def forget_wait(self, person_id: uuid.UUID) -> None:
        """``person_id``'s means of this kind was agreed or removed: no wait is kept for it."""
        ...

    async def weigh(
        self,
        session: AsyncSession,
        person: Person,
        place: ProofPlace,
        answer: str,
        now: datetime,
    ) -> Literal["holds", "fails", "not_an_answer"]: ...


def phrase_ref(person_id: uuid.UUID) -> str:
    """The reference under which the gatekeeper keeps ``person_id``'s spoken phrase."""
    return f"phrase.{person_id}"


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


# The longest text a phrase is compared with (``gatekeeper_api.PhraseSaid``): anything longer is
# no phrase, and is not sent.
MAX_HEARD_LENGTH: Final = 400


class SpokenPhraseProof:
    """A phrase said aloud, compared by the gatekeeper (see the module docstring): offered where
    the place can have it compared. ``answer`` is what was heard, in memory only.

    When the gatekeeper holds a person's phrase back, it answers nothing of any text (an ordinary
    sentence as much as an attempt), and each text sent would spend one of its comparisons. So
    the kind remembers until when (``_held_until``, by person id, in this process's memory): until
    then it is not offered to that person and sends the gatekeeper nothing of theirs; agreeing or
    removing the phrase forgets it. Cost: a restart forgets it, and the first text weighed after
    one spends one comparison more to learn it again."""

    name: str = "spoken_phrase"
    # The gatekeeper counts the tries, where it compares them.
    throttled: bool = False

    def __init__(self) -> None:
        self._held_until: dict[uuid.UUID, datetime] = {}

    def held_back_for(self, person_id: uuid.UUID, now: datetime) -> float | None:
        until = self._held_until.get(person_id)
        if until is None:
            return None
        if until <= now:
            del self._held_until[person_id]
            return None
        return (until - now).total_seconds()

    def forget_wait(self, person_id: uuid.UUID) -> None:
        self._held_until.pop(person_id, None)

    def forget_waits(self) -> None:
        """Forget every remembered wait (a fresh start, as after a restart)."""
        self._held_until.clear()

    def _hold(self, person_id: uuid.UUID, wait: float, now: datetime) -> None:
        # Those whose wait is over are forgotten on the way: the map holds only who waits.
        for other, until in list(self._held_until.items()):
            if until <= now:
                del self._held_until[other]
        self._held_until[person_id] = now + timedelta(seconds=wait)

    async def offered_to(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> bool:
        return place.phrase_check is not None

    async def begin(
        self, session: AsyncSession, person: Person, place: ProofPlace, now: datetime
    ) -> JsonObject:
        return {}

    async def weigh(
        self,
        session: AsyncSession,
        person: Person,
        place: ProofPlace,
        answer: str,
        now: datetime,
    ) -> Literal["holds", "fails", "not_an_answer"]:
        """The gatekeeper's answer: ``it_is`` holds, ``almost`` fails, ``nothing_like`` is no
        answer; so is a text no phrase can be (blank, or longer than any), and a text where
        nothing can compare it, which are not sent. The read is over before the gatekeeper is
        asked: nothing is held while it answers, as for a password. ``PhraseHeldBack`` passes
        through, remembered (see the class), and is raised again without asking the gatekeeper
        while the wait lasts. Any other error of the gatekeeper passes through, remembering
        nothing."""
        wait = self.held_back_for(person.id, now)
        if wait is not None:
            raise PhraseHeldBack(wait)
        compare = place.phrase_check
        if compare is None or not answer.strip() or len(answer) > MAX_HEARD_LENGTH:
            return "not_an_answer"
        await session.commit()
        try:
            result = await compare(person.id, answer)
        except PhraseHeldBack as held:
            self._hold(person.id, held.wait, now)
            raise
        match result:
            case "it_is":
                return "holds"
            case "almost":
                return "fails"
            case _:
                return "not_an_answer"

    async def check(
        self,
        session: AsyncSession,
        person: Person,
        place: ProofPlace,
        answer: str,
        now: datetime,
    ) -> bool:
        return await self.weigh(session, person, place, answer, now) == "holds"


# The kinds there are, in the order a place offers them: the passkey first, where it is offered,
# the phrase last.
PROOF_KINDS: Final[Mapping[str, ProofKind]] = {
    "passkey": PasskeyProof(),
    "password": PasswordProof(),
    "spoken_phrase": SpokenPhraseProof(),
}
