"""Who speaks, and how sure Domo is of it, from clues instead of a name."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from domo_core.accounts.actors import actor_of as accounts_actor_of
from domo_core.accounts.models import Person
from domo_core.llm.types import JsonObject
from domo_core.policy.roles import Actor


@dataclass(frozen=True)
class Clue:
    """One thing that points toward, or away from, a person being the one who speaks.

    ``kind`` is ``session``, ``device`` or ``proof`` here; ``means`` is the kind of proof for a
    proof. ``with_person`` and ``place`` are for clues that rest on someone else or a place.
    ``source`` is ``code`` with ``strength`` ``strong`` only for what the code itself checked (a
    session, a device, a proof). A channel's own "it is probably this record" (a voice it
    recognised, later) is a ``weak`` clue: it never establishes anyone (``established``), and a
    turn it alone ties to a person gets none of the clues kept for them (an earlier proof:
    ``conversation.prepare._speaking``).
    """

    kind: str
    weighs: Literal["for", "against"]
    strength: Literal["strong", "weak"]
    source: Literal["code", "domo"]
    at: datetime
    means: str | None = None
    with_person: UUID | None = None
    place: str | None = None


@dataclass(frozen=True)
class Speaker:
    """Who speaks. ``channel`` is data for the judge and the audit, never branched on.
    ``person_id`` is a record of ``persons``, with or without an account: the one who speaks gives
    the rights and the discussion, by their id alone.

    ``nobody_spoke`` is set for a turn the assistant started, that answers no words (a wake, an
    arrival, a plan): who it is for stays as the clues say, but nobody is said to speak.
    ``last_heard_at`` is when the person was heard before in this conversation (their message
    before the one the turn answers), ``None`` when never."""

    person_id: UUID | None
    channel: str
    device_id: UUID | None = None
    clues: tuple[Clue, ...] = ()
    nobody_spoke: bool = False
    last_heard_at: datetime | None = None

    def as_json(self, now: datetime) -> JsonObject:
        """What the judge reads, never an identifier: the channel, and that nobody spoke, alone,
        in a turn nobody spoke in; else the clues, each with its real age, and how long ago the
        person was heard before here (``None`` when never)."""
        if self.nobody_spoke:
            return {"channel": self.channel, "nobody_spoke": True}
        return {
            "channel": self.channel,
            "clues": [
                {
                    "kind": clue.kind,
                    "weighs": clue.weighs,
                    "strength": clue.strength,
                    "source": clue.source,
                    "minutes_ago": _minutes_before(clue.at, now),
                }
                for clue in self.clues
            ],
            "last_heard_minutes_ago": (
                None if self.last_heard_at is None else _minutes_before(self.last_heard_at, now)
            ),
        }


def _minutes_before(at: datetime, now: datetime) -> int:
    """How many whole minutes ``at`` is before ``now`` (0 for a moment after it)."""
    return max(0, int((now - at).total_seconds() // 60))


def established(speaker: Speaker) -> bool:
    """Whether the code is sure of the person: one strong clue for them that the code made."""
    return speaker.person_id is not None and any(
        clue.source == "code" and clue.strength == "strong" and clue.weighs == "for"
        for clue in speaker.clues
    )


def actor_of(speaker: Speaker, person: Person) -> Actor:
    """The speaker as the barrier sees them, with the marks of their record (their role, made
    role, child mark and where they stand, account or not); trusted only when established. An
    actor that is not trusted is refused all but ``barrier.UNTRUSTED_MAY`` (rule 1 of
    ``barrier.decide``)."""
    if speaker.person_id is None:
        raise ValueError("Nobody is known to speak")
    return accounts_actor_of(person, trusted=established(speaker))
