"""A person's agreed means of proof, what a place offers of them, and proving (spec 4.5, 6.4).

A request asks a proof (``ask`` ``proof``) when the yes-judge, a rule of the owner or Domo doubts
that the one who answers is really the person. Words never give one, nor does the button
(``confirmations.service._not_enough``): the place offers the kinds this person has agreed and the
place can carry (``offers``), the person gives one there, and the code checks it (``prove``). A
proof is worth at least an explicit yes (``decided_via`` ``proof``), and it is spent on this
request only. The rule every kind keeps is written in ``proofs.kinds``.

The means
---------
A person's means are rows (``proofs.models.ProofMean``), each pointing to a kind. A ``password``
means is agreed by the code with every account, a ``passkey`` one with the first passkey added and
taken away with the last (``accounts``). There is no action to agree or remove one in Domo's
hands yet (spec 6.4): ``agree`` and ``remove`` are for the code that knows the kind.

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
from collections.abc import Callable, Mapping
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
from domo_core.policy.catalog import PolicyTables
from domo_core.proofs.kinds import PROOF_KINDS, ProofPlace, login_account_of
from domo_core.proofs.models import ProofMean
from domo_core.tools.context import InternalTool, ToolContext

__all__ = ["ProofHeldBack", "agree", "means_of", "offers", "prove", "remove"]

# Who can propose a means: the code, Domo, or the person.
PROPOSERS: Final = ("code", "domo", "person")


class ProofHeldBack(Exception):
    """Too many wrong answers of a kind that can be guessed: the proof is not checked; ``wait``
    is how long to wait, in seconds."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"held back for {wait:.0f} s")
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


async def offers(
    session: AsyncSession, person: Person, place: ProofPlace, now: datetime
) -> tuple[str, ...]:
    """The kinds ``place`` can ask of ``person``, in the order of ``PROOF_KINDS``: those they have
    agreed, that the kind offers here and that the place's abilities hold (``proof_kinds``)."""
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
) -> tuple[DecideOutcome, ActionRecord | None]:
    """``person``'s proof, by ``means``, of the request ``confirmation_id`` (see the module
    docstring for the order of what is read). ``answer`` is what the place posted (a passkey's
    answer, a password), ``place`` where it was asked, ``throttle`` the count of the password
    checks (``account_routes.get_password_check_throttle``); ``ctx_factory``, ``registry`` and
    ``policy`` are ``decide_confirmation``'s. ``speaker`` is who gave the proof and where (the
    channel, the device): a proof that holds is then kept as one more clue that it is them, for a
    while (``identity.clues``).

    Returns what ``decide_confirmation`` returns for a proof that holds; ``proof_failed`` for one
    that does not (nothing changes, but what a passkey's answer spent); ``not_yours`` for a request
    the person may not answer, one that asks no proof, or a means the place does not offer them (one
    they have not agreed, or took away, or that the place cannot carry); ``already_decided`` or
    ``expired`` for a request that no longer waits. Raises ``ProofHeldBack`` when the means'
    attempts are held back, and lets ``Argon2Busy`` through (nothing was verified)."""
    chosen = PROOF_KINDS.get(means)
    async with session_factory() as session:
        opened = await open_for_a_proof(session, confirmation_id, person.id, now)
        if isinstance(opened, str):
            return opened, None
        c, current = opened
        if chosen is None or ask_of(c) != "proof":
            return "not_yours", None
        if chosen.name not in await offers(session, current, place, now):
            return "not_yours", None
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
        if not await chosen.check(session, current, place, answer, now):
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
