"""A person's agreed means of proof, what a place offers of them, and proving (spec 4.5, 6.4).

A request asks a proof (``ask`` ``proof``) when the yes-judge, a rule of the owner or Domo doubts
that the one who answers is really the person. Words never give one, nor does the button
(``confirmations.service._not_enough``): the place offers the kinds this person has agreed and the
place can carry (``offers``), the person gives one there, and the code checks it (``prove``). A
proof is worth at least an explicit yes (``decided_via`` ``proof``), and it is spent on this
request only. The rule every kind keeps is written in ``proofs.kinds``.

A kind that can tell an answer that is not an attempt at all (``proofs.kinds.Weighs``: an ordinary
sentence heard where a spoken phrase was awaited) is weighed: ``prove`` raises ``NotAnAnswer`` for
it, before any audit line and any count, and the sentence goes its usual way. While such a kind is
held back for the person (the gatekeeper holds their phrase back), it answers nothing of any text:
``prove`` raises ``NotWeighed``, a ``NotAnAnswer`` (the text was NOT taken as a proof and goes its
usual way, an ordinary sentence as much as an attempt), and asks the gatekeeper nothing more until
the wait is over; meanwhile ``offers`` leaves the kind out and the other means stay offered
(``could_offer`` is the same read with the wait left aside). The voice channel weighs a sentence
only inside a proof window of its microphone (``channels.voice.session``: the assistant spoke to
the person there lately), and drops it, whatever it is, while the phrase is held back there.

The means
---------
A person's means are rows (``proofs.models.ProofMean``), each pointing to a kind. A ``password``
means is agreed by the code with every account, a ``passkey`` one with the first passkey added and
taken away with the last (``accounts``). A ``spoken_phrase`` means is the one Domo has actions
for: the person agrees, changes or removes it by voice through his tools
(``tools.voice_tools``: their own wish only, never his idea), and the voice channel agrees it once
the gatekeeper holds the phrase (``channels.voice.proof``). ``agree`` and ``remove`` are for the
code that knows the kind.

Proving
-------
``prove`` reads, in this order (F10): the request's state (``open_for_a_proof``: one already
answered or expired says so, as ``decide_confirmation`` does, and a second proof of it, from another
tab, is told it was answered before any means is read); whether the person may answer it
(``may_decide``); whether the means is one the place offers this person (agreed, and carried
here); then, for a kind that can be guessed, the attempt is counted (F11); then the kind checks. A
wrong proof changes nothing (``proof_failed``, audited ``confirmation.proof_failed`` with the
request and the means, never what was given). A right one is the person's yes:
``decide_confirmation`` with ``via`` ``proof``, whose ``confirmation.decided`` names the means.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.throttle import LoginThrottle, password_check_key
from domo_core.audit.log import append_audit
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.service import DecideOutcome, decide_confirmation, open_for_a_proof
from domo_core.confirmations.store import ask_of
from domo_core.conversation.speaker import Speaker
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import PhraseHeldBack
from domo_core.policy.catalog import PolicyTables
from domo_core.proofs.kinds import PROOF_KINDS, ProofKind, ProofPlace, Weighs, login_account_of
from domo_core.proofs.models import ProofMean
from domo_core.tools.context import InternalTool, ToolContext

__all__ = [
    "NotAnAnswer",
    "NotWeighed",
    "ProofHeldBack",
    "agree",
    "could_offer",
    "means_of",
    "offers",
    "prove",
    "remove",
]

# Who can propose a means: the code, Domo, or the person.
PROPOSERS: Final = ("code", "domo", "person")


class ProofHeldBack(Exception):
    """Too many wrong answers of a kind that can be guessed (a password, counted by ``prove``):
    the proof is not checked; ``wait`` is how long to wait, in seconds. A weighed kind held back
    raises ``NotWeighed`` instead."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"held back for {wait:.0f} s")
        self.wait = wait


class NotAnAnswer(Exception):
    """This text was NOT taken as a proof; it goes its usual way (an ordinary sentence where a
    phrase was awaited, a blank one, or any text while the phrase is held back: ``NotWeighed``).
    Nothing was audited or counted here, and the request still waits. It holds nothing of what
    was given."""


class NotWeighed(NotAnAnswer):
    """The kind's answers are held back for ``wait`` seconds (the gatekeeper said so, or said so
    lately): this text was NOT taken as a proof, it goes its usual way, whatever it was (an
    ordinary sentence as much as an attempt). Meanwhile the kind is not offered to the person
    nor weighed; the other means stay offered."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"not weighed for {wait:.0f} s")
        self.wait = wait


async def means_of(session: AsyncSession, person: Person, now: datetime) -> tuple[ProofMean, ...]:
    """The means ``person`` has agreed and not removed, oldest first. ``now`` is the moment asked
    about, kept for the kinds of means that will not hold for ever."""
    found = await session.execute(
        select(ProofMean)
        .where(ProofMean.person_id == person.id, ProofMean.removed_at.is_(None))
        .order_by(ProofMean.agreed_at, ProofMean.id)
        .execution_options(populate_existing=True)
    )
    return tuple(found.scalars())


async def agree(
    session: AsyncSession, person_id: uuid.UUID, kind: str, *, proposed_by: str, now: datetime
) -> None:
    """``person_id`` has agreed the means of this ``kind`` (proposed by ``proposed_by``: ``code``,
    ``domo`` or ``person``). Idempotent: one already agreed stays as it is. Raises
    ``ValueError`` for a kind the code does not know or an unknown proposer."""
    if kind not in PROOF_KINDS:
        raise ValueError(f"unknown kind of proof: {kind!r}")
    if proposed_by not in PROPOSERS:
        raise ValueError(f"unknown proposer of a means of proof: {proposed_by!r}")
    await session.execute(
        insert(ProofMean)
        .values(person_id=person_id, kind=kind, proposed_by=proposed_by, agreed_at=now)
        .on_conflict_do_nothing(
            index_elements=[ProofMean.person_id, ProofMean.kind],
            index_where=ProofMean.removed_at.is_(None),
        )
    )
    _forget_wait(person_id, kind)


def _forget_wait(person_id: uuid.UUID, kind: str) -> None:
    """A means agreed (afresh) or removed starts with no wait remembered (``Weighs``). Done before
    the caller commits: should it roll back, one comparison more is spent to learn the wait
    again."""
    found = PROOF_KINDS.get(kind)
    if isinstance(found, Weighs):
        found.forget_wait(person_id)


async def remove(session: AsyncSession, person_id: uuid.UUID, kind: str, now: datetime) -> None:
    """The means of this ``kind`` is taken away from ``person_id`` at ``now``: its row stays, with
    ``removed_at``. Nothing happens if it is not agreed."""
    await session.execute(
        update(ProofMean)
        .where(
            ProofMean.person_id == person_id,
            ProofMean.kind == kind,
            ProofMean.removed_at.is_(None),
        )
        .values(removed_at=now)
        .execution_options(synchronize_session=False)
    )
    _forget_wait(person_id, kind)


async def offers(
    session: AsyncSession, person: Person, place: ProofPlace, now: datetime
) -> tuple[str, ...]:
    """The kinds ``place`` can ask of ``person``, in the order of ``PROOF_KINDS``: those they have
    agreed, that the kind offers here and that the place's abilities hold (``proof_kinds``), but
    for a weighed kind held back for them (``Weighs.held_back_for``) until the wait is over."""
    return tuple(
        [
            name
            for name in await could_offer(session, person, place, now)
            if not _waiting(PROOF_KINDS[name], person, now)
        ]
    )


def _waiting(kind: ProofKind, person: Person, now: datetime) -> bool:
    return isinstance(kind, Weighs) and kind.held_back_for(person.id, now) is not None


async def could_offer(
    session: AsyncSession, person: Person, place: ProofPlace, now: datetime
) -> tuple[str, ...]:
    """``offers``, a kind's wait left aside: what the place could ask were nothing held back."""
    agreed = {mean.kind for mean in await means_of(session, person, now)}
    return tuple(
        [
            name
            for name, kind in PROOF_KINDS.items()
            if name in agreed
            and name in place.abilities.proof_kinds
            and await kind.offered_to(session, person, place, now)
        ]
    )


async def prove(
    *,
    session_factory: SessionFactory,
    confirmation_id: uuid.UUID,
    person: Person,
    means: str,
    answer: str,
    place: ProofPlace,
    throttle: LoginThrottle,
    now: datetime,
    ctx_factory: Callable[[Person, uuid.UUID], ToolContext],
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    speaker: Speaker,
    weighed_after: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[DecideOutcome, ActionRecord | None]:
    """``person``'s proof, by ``means``, of the request ``confirmation_id`` (see the module
    docstring for the order of what is read). ``answer`` is what the place posted (a passkey's
    answer, a password), ``place`` where it was asked, ``throttle`` the count of the password
    checks (``account_routes.get_password_check_throttle``); ``ctx_factory``, ``registry`` and
    ``policy`` are ``decide_confirmation``'s. ``speaker`` is who gave the proof and where (the
    channel, the device, the microphone, passed on as they are): a proof that holds is then kept
    as one more clue that it is them, for a while, for that place (``identity.clues``).

    Returns what ``decide_confirmation`` returns for a proof that holds; ``proof_failed`` for one
    that does not (nothing changes, but what a passkey's answer spent); ``not_yours`` for a request
    the person may not answer, one that asks no proof, or a means the place does not offer them (one
    they have not agreed, or took away, or that the place cannot carry); ``already_decided`` or
    ``expired`` for a request that no longer waits. Raises ``ProofHeldBack`` when the means'
    attempts are held back by the throttle; ``NotAnAnswer`` when this text was NOT taken as a
    proof and goes its usual way (nothing written, nothing counted): no attempt at all, or, as
    ``NotWeighed`` with its ``wait``, any text while a weighed kind is held back for the person
    (the gatekeeper is then not asked again until the wait is over). Lets ``Argon2Busy`` and a
    gatekeeper's other errors through (nothing was verified, nothing written).

    ``weighed_after``: asked once the proof holds, before anything is decided (a proof given by a
    voice that weighs against the person goes to the judge with that clue, spec 6.9): ``False``
    decides nothing, and the request still waits for a proof (``needs_proof``)."""
    chosen = PROOF_KINDS.get(means)
    async with session_factory() as session:
        opened = await open_for_a_proof(session, confirmation_id, person.id, now)
        if isinstance(opened, str):
            return opened, None
        c, current = opened
        if chosen is None or ask_of(c) != "proof":
            return "not_yours", None
        # A place that could not take this means (not agreed, not carried, nothing to compare
        # it) refuses it as always, wait or not; one that could, during a wait, does not weigh it.
        if chosen.name not in await could_offer(session, current, place, now):
            return "not_yours", None
        if isinstance(chosen, Weighs):
            wait = chosen.held_back_for(current.id, now)
            if wait is not None:
                raise NotWeighed(wait)
        held_key: str | None = None
        if chosen.throttled:
            account = await login_account_of(session, current)
            if account is None:
                return "not_yours", None
            held_key = password_check_key(account)
            # Counted now, before the slow check (see ``LoginThrottle``); forgotten on success.
            wait = throttle.register_attempt(held_key)
            if wait is not None:
                raise ProofHeldBack(wait)
        try:
            if isinstance(chosen, Weighs):
                weighed = await chosen.weigh(session, current, place, answer, now)
                if weighed == "not_an_answer":
                    raise NotAnAnswer
                holds = weighed == "holds"
            else:
                holds = await chosen.check(session, current, place, answer, now)
        except PhraseHeldBack as held:
            raise NotWeighed(held.wait) from None
        if not holds:
            # What the check spent (a passkey's challenge) is kept with the entry.
            await append_audit(
                session,
                actor=f"person:{current.id}",
                action="confirmation.proof_failed",
                details={"confirmation_id": str(c.id), "means": chosen.name},
            )
            await session.commit()
            return "proof_failed", None
        # The proof holds: what it spent is kept.
        await session.commit()
    if held_key is not None:
        throttle.clear(held_key)
    if weighed_after is not None and not await weighed_after():
        return "needs_proof", None
    return await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=confirmation_id,
        decider=person,
        approve=True,
        via="proof",
        reply_message_id=None,
        ctx_factory=ctx_factory,
        registry=registry,
        policy=policy,
        now=now,
        means=chosen.name,
        speaker=speaker,
    )
