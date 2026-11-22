"""Who speaks, and how sure Domo is of it, from clues instead of a name."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from domo_core.llm.types import JsonObject
from domo_core.policy.roles import Actor, Role


@dataclass(frozen=True)
class Clue:
    """One thing that points toward, or away from, a person being the one who speaks.

    ``kind`` is ``session``, ``device`` or ``proof`` here; ``means`` is the kind of proof for a
    proof. ``with_person`` and ``place`` are for clues that rest on someone else or a place.
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
    """Who speaks. ``channel`` is data for the judge and the audit, never branched on."""

    person_id: UUID | None
    channel: str
    device_id: UUID | None = None
    clues: tuple[Clue, ...] = ()

    def as_json(self, now: datetime) -> JsonObject:
        """What the judge reads: the channel and the clues, never an identifier."""
        return {
            "channel": self.channel,
            "clues": [
                {
                    "kind": clue.kind,
                    "weighs": clue.weighs,
                    "strength": clue.strength,
                    "source": clue.source,
                    "minutes_ago": max(0, int((now - clue.at).total_seconds() // 60)),
                }
                for clue in self.clues
            ],
        }


def established(speaker: Speaker) -> bool:
    """Whether the code is sure of the person: one strong clue for them that the code made."""
    return speaker.person_id is not None and any(
        clue.source == "code" and clue.strength == "strong" and clue.weighs == "for"
        for clue in speaker.clues
    )


def actor_of(speaker: Speaker, role: Role) -> Actor:
    """The speaker as the barrier sees them; trusted only when established."""
    if speaker.person_id is None:
        raise ValueError("Nobody is known to speak")
    return Actor(speaker.person_id, role, trusted=established(speaker))
