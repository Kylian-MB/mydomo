"""Who speaks, and how sure Domo is of it, from clues instead of a name."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from domo_core.accounts.actors import actor_of as accounts_actor_of
from domo_core.accounts.models import Person
from domo_core.llm.types import JsonObject
from domo_core.policy.roles import Actor, Standing


@dataclass(frozen=True)
class Clue:
    """One thing that points toward, or away from, a person being the one who speaks.

    ``kind`` is ``session``, ``device`` or ``proof`` (the code's, strong when they point to the
    person; a ``session`` clue against, for someone heard at a page signed in as another person,
    is weak), ``voice`` or ``intonation`` (the code's, always weak: a voice alone never establishes
    anybody) or ``manner`` (Domo's own reading, weak); ``means`` is the kind of proof for a proof.
    ``weighs`` is ``unknown`` when it points nowhere: a voice not learnt yet, too short to tell, or
    that gave no print. ``degree`` (0 to 1) is how much it points, for a clue that is measured.
    ``with_person`` and ``place`` are for clues that rest on someone else or a place.
    """

    kind: str
    weighs: Literal["for", "against", "unknown"]
    strength: Literal["strong", "weak"]
    source: Literal["code", "domo"]
    at: datetime
    means: str | None = None
    with_person: UUID | None = None
    place: str | None = None
    degree: float | None = None


@dataclass(frozen=True)
class Speaker:
    """Who speaks. ``channel`` is data for the judge and the audit, never branched on.
    ``person_id`` is a record of ``persons``, with or without an account: the one who speaks gives
    the rights and the discussion, by their id alone.

    ``nobody_spoke`` is set for a turn the assistant started, that answers no words (a wake, an
    arrival, a plan): who it is for stays as the clues say, but nobody is said to speak.
    ``last_heard_at`` is when the person was heard before in this conversation (their message
    before the one the turn answers), ``None`` when never. ``microphone`` is the microphone that
    heard them, when one did (data, like ``channel``). ``voice_too_short``: the sentence was heard,
    but its voice gave no print long enough to tell who said it, even joined with the same voice
    just before (``channels.voice.speaker``): it changes nothing for what is ordinary, and makes
    the speaker unsure for what is sensitive (``policy.barrier.decide``). ``page_person_id``: the
    words were heard at a page signed in as someone else (that person), when the voice names
    another: nothing is ordinary then, and the page person's rules count too (``Actor``)."""

    person_id: UUID | None
    channel: str
    device_id: UUID | None = None
    clues: tuple[Clue, ...] = ()
    nobody_spoke: bool = False
    last_heard_at: datetime | None = None
    microphone: str | None = None
    voice_too_short: bool = False
    page_person_id: UUID | None = None

    def as_json(self, now: datetime) -> JsonObject:
        """What the judge reads, never an identifier: the channel, and that nobody spoke, alone,
        in a turn nobody spoke in; else the clues, each with its real age and, when measured, its
        degree, how long ago the person was heard before here (``None`` when never), and that a
        microphone heard them when one did (never which)."""
        if self.nobody_spoke:
            return {"channel": self.channel, "nobody_spoke": True}
        seen: JsonObject = {
            "channel": self.channel,
            "clues": [_clue_as_json(clue, now) for clue in self.clues],
            "last_heard_minutes_ago": (
                None if self.last_heard_at is None else _minutes_before(self.last_heard_at, now)
            ),
        }
        if self.microphone is not None:
            seen["microphone"] = True
        return seen


def _clue_as_json(clue: Clue, now: datetime) -> JsonObject:
    seen: JsonObject = {
        "kind": clue.kind,
        "weighs": clue.weighs,
        "strength": clue.strength,
        "source": clue.source,
        "minutes_ago": _minutes_before(clue.at, now),
    }
    if clue.degree is not None:
        seen["degree"] = clue.degree
    return seen


def _minutes_before(at: datetime, now: datetime) -> int:
    """How many whole minutes ``at`` is before ``now`` (0 for a moment after it)."""
    return max(0, int((now - at).total_seconds() // 60))


def established(speaker: Speaker) -> bool:
    """Whether the code is sure of the person: one strong clue for them that the code made."""
    return speaker.person_id is not None and any(
        clue.source == "code" and clue.strength == "strong" and clue.weighs == "for"
        for clue in speaker.clues
    )


def _contradicted(clues: tuple[Clue, ...]) -> bool:
    """Whether a voice clue the code made weighs against the person."""
    return any(
        clue.source == "code" and clue.kind == "voice" and clue.weighs == "against"
        for clue in clues
    )


def voice_against(speaker: Speaker) -> bool:
    """Whether a voice the code heard weighs against the speaker's probable person."""
    return _contradicted(speaker.clues)


def standing(speaker: Speaker) -> Standing:
    """How sure the code is of who speaks, a fact from its own clues (a ``domo`` clue never
    changes it): ``unknown`` when nobody is known to speak; ``sure`` when the person is
    ``established`` and no voice the code heard weighs against them; else ``unsure``. Heard at a
    page signed in as someone else (``page_person_id``), a proof kept for them is not enough:
    this sentence's voice must also read clearly for them (``CLEARLY_AT``), else they are unsure
    there, and judged (final fix wave B, I-3)."""
    if speaker.person_id is None:
        return "unknown"
    if established(speaker) and not _contradicted(speaker.clues):
        if speaker.page_person_id is not None and not _clearly_theirs(speaker.clues):
            return "unsure"
        return "sure"
    return "unsure"


def heard_unsure(speaker: Speaker) -> bool:
    """Whether a sentence heard with ``speaker`` is not surely the person's own: the code is not
    sure of them (``standing``), the voice was too short to tell, or it was heard at another
    person's page. Such a sentence is marked so where it is stored (``Message.heard_unsure``) and
    is never their own words: what is kept from it keeps that source (origin ``unsure_voice``,
    ``tools.memory_tools.origin_for``)."""
    return (
        standing(speaker) != "sure" or speaker.voice_too_short or speaker.page_person_id is not None
    )


# From this degree, the voice of a sentence reads clearly for a person: a DEGREE (0 to 1, linear
# between a print model's ``print_low`` and ``print_high``: ``channels.voice.prints.degree_of``),
# not a similarity; 1.0 is ``print_high``.
CLEARLY_AT: Final = 0.75


def _clearly_theirs(clues: tuple[Clue, ...]) -> bool:
    """Whether a voice clue the code made reads clearly for the person (``CLEARLY_AT``)."""
    return any(
        clue.source == "code"
        and clue.kind == "voice"
        and clue.weighs == "for"
        and clue.degree is not None
        and clue.degree >= CLEARLY_AT
        for clue in clues
    )


def heeded_proofs(clues: tuple[Clue, ...], proofs: tuple[Clue, ...]) -> tuple[Clue, ...]:
    """The clues kept for the person (``proofs``) that count beside ``clues``: no proof when a
    voice the code heard weighs against the person it was given by (spec 6.5, "Après une
    preuve"), and none already among ``clues``. Domo's own reading of how they speak (``manner``)
    always counts: it never changes the standing, and the judge weighs it beside the voice."""
    contradicted = _contradicted(clues)
    return tuple(
        proof
        for proof in proofs
        if proof not in clues and not (contradicted and proof.kind == "proof")
    )


_SURER: Final[dict[Standing, int]] = {"unknown": 0, "unsure": 1, "sure": 2}


def _sureness(of: Standing, voice_too_short: bool) -> tuple[int, bool]:
    """How sure the code is, as an order: the standing first, then a voice long enough."""
    return _SURER[of], not voice_too_short


def least_sure(speakers: Sequence[Speaker]) -> Speaker:
    """The one of ``speakers`` (at least one) the code is least sure of, the first such one, with
    ``voice_too_short`` when any of them had it: a turn that reads the words of all of them is as
    sure as the least sure one (final fix wave, ruling C1)."""
    weakest = min(speakers, key=lambda s: _sureness(standing(s), s.voice_too_short))
    return replace(weakest, voice_too_short=any(s.voice_too_short for s in speakers))


def joined_by(
    actor: Actor, speaker: Speaker | None, others: Sequence[Speaker]
) -> tuple[Actor, Speaker | None]:
    """The ``actor`` and ``speaker`` of a turn once it reads the words of ``others`` too (words
    said while it ran): the least sure standing of all, a voice too short when any was, and the
    speaker the judge reads is the least sure one. Every decision after that in the turn carries
    their doubt, as if the whole turn were theirs (ruling C1)."""
    if not others:
        return actor, speaker
    weakest = least_sure(others)
    theirs = standing(weakest)
    short = actor.voice_too_short or weakest.voice_too_short
    if speaker is None or _sureness(theirs, weakest.voice_too_short) < _sureness(
        actor.standing, actor.voice_too_short
    ):
        speaker = weakest
    lower = min(actor.standing, theirs, key=_SURER.__getitem__)
    return replace(actor, standing=lower, voice_too_short=short), replace(
        speaker, voice_too_short=short
    )


def actor_of(speaker: Speaker, person: Person) -> Actor:
    """The speaker as the barrier sees them, with the marks of their record ``person`` (their
    role, made role, child mark and where they stand, account or not:
    ``accounts.actors.actor_of``): their ``standing``, and whether their voice was too short to
    tell (``Speaker.voice_too_short``). Someone nobody knows (``unknown``) is refused everything
    (rule 1 of ``barrier.decide``)."""
    if speaker.person_id is None:
        raise ValueError("Nobody is known to speak")
    return replace(
        accounts_actor_of(person),
        standing=standing(speaker),
        voice_too_short=speaker.voice_too_short,
        page_person_id=speaker.page_person_id,
    )
