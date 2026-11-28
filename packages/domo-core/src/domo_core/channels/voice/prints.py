"""Voice prints by reference and by microphone: compared, learnt slowly, forgotten (spec 6.5).

A print is a vector made by a print model; it is never compared to a print of another model, and
what a similarity means (the bounds of its degree) is the model's: the caller hands them in
(``llm.speech.client.PrintThresholds``). With none known (or malformed ones), the voice is "not
learned yet": never "against", nothing learnt. A vector that is not finite, is all zeros or is
too long is no print at all. This module gives plain results (a ``Reading``, the words
``nothing``, ``candidate``, ``learnt``, ``updated``, ``passing``, a reference); what the page, a
person or the judge makes of them is for the speaker (Part 2).

What a consumer should use, after ``read_voice`` for the person expected (read in this order):

=============================================  ==============================================
The voice...                                   ``Reading``
=============================================  ==============================================
confirms the person                            ``weighs == "for"``
is someone else we know                        ``weighs == "against"``; who: ``best_other``
                                               (never ``None`` then, and nearer than the
                                               person)
is not learnt yet for the person               ``not learnt`` (``weighs == "unknown"``)
is an unknown voice                            ``resembles_nobody`` (never for a ``short``
                                               sound)
is unsure (between the thresholds, a cold, a   anything else: ``weighs == "unknown"``,
``short`` sound, a tie with a known other)     ``learnt``, not ``resembles_nobody``; a tie
                                               names the other in ``best_other``
=============================================  ==============================================

``resembles_nobody`` is this module's narrow sense (no known other even named, under
``AGAINST_AT``): it decides only under which reference an unknown voice is kept
(``reference_for``). Who probably speaks reads wider (``channels.voice.speaker``, ruling A19): a
voice learnt far from the page's person (under ``AGAINST_AT``, not a passing print) whom no known
other reads "for" is taken for nobody known, even when a known other is named between the
thresholds (``weighs == "against"`` here); a known other reading "for" and nearer is that person.

What is reported is never lifted to "for" by the utterances just before: a print of at least
``MIN_SECONDS`` is read alone; a shorter one is joined only with the earlier prints that read
"for" against it (a degree of ``FOR_AT`` or more).

A voice is learnt from several like utterances on two visits, and only when something other than
the voice says who speaks (``sure_otherwise``): two people alternating at one page never make a
voice. ``learn`` decides on the single print it stores, never on utterances joined to it. The base
print moves slowly and at most once a visit (a visit ends after ``VISIT_GAP`` with nothing heard on
that microphone), the recent one follows every sure utterance but only when the voice reads "for"
against the base (so the recent print never walks away from it), and a cold goes to a passing
print that dies alone and never teaches the base or the recent print. "The nearest known other" is
one thing everywhere: the best degree of any other known person's base and recent prints, on any
microphone, with no discount (``_nearest_other``); a passing print never names anybody.
"""

import math
import secrets
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.channels.voice.models import (
    MAX_DIMENSIONS,
    REFERENCE_PREFIX,
    VoiceMoment,
    VoicePrint,
    VoiceRef,
)
from domo_core.channels.voice.thresholds import (
    AGAINST_AT,
    BASE_WEIGHT,
    FOR_AT,
    HABIT_FIRST_SPREAD,
    HABIT_SPREAD_FLOOR,
    INTONATION_ADJUST,
    INTONATION_SPREADS,
    LEARN_UTTERANCES,
    LEARN_VISITS,
    MIN_SECONDS,
    OTHER_MICROPHONE,
    PASSING_LIVES,
    RECENT_WEIGHT,
    SHORT_CAP,
    STARTING_BOUNDS,
    UNKNOWN_LIVES,
    VISIT_GAP,
)
from domo_core.llm.speech.client import Print, PrintThresholds

Weighs = Literal["for", "against", "unknown"]
Learning = Literal["nothing", "candidate", "learnt", "updated", "passing"]

# The highest degree still under a threshold: the top of the band below it.
_JUST_UNDER_FOR = math.nextafter(FOR_AT, 0.0)
_JUST_UNDER_AGAINST = math.nextafter(AGAINST_AT, 0.0)


@dataclass(frozen=True, kw_only=True)
class Reading:
    """A voice read against one person's prints (the table at the top of the module says how to
    use it). A field is REPORTED when it is what the voice is said to be (for a short sound,
    joined with the earlier prints of the same voice; nuanced by intonation; capped for a short
    sound), RAW when it is a plain comparison of the prints (of the same vector).

    ``weighs`` (reported): ``for``, the person's voice; ``against``, someone else we know
    (``best_other``), always nearer than the person: that person's voice reads "for", or the
    person's voice is learnt and far (under ``AGAINST_AT``) and ``best_other`` is named;
    ``unknown`` for everything else: nothing learnt for the person, a ``short`` sound, a tie with
    a known other who reads "for" (named in ``best_other``), a degree between the thresholds, a
    voice only a passing print matches (``via_passing``), or a voice far from the person that
    resembles nobody known (``resembles_nobody``).

    ``short``: under ``MIN_SECONDS`` of sound, even joined: never "against", never an unknown
    voice, only unsure.

    ``degree`` (reported, 0..1): the person's base and recent prints (this microphone's, else the
    others' at ``OTHER_MICROPHONE``), moved inside its band by intonation, held in the unsure band
    by a passing print, capped at ``SHORT_CAP`` for a short sound; 0 when nothing is learnt.

    ``learnt``: a base print of this print model exists for the person (on any microphone).

    ``best_other``: the nearest known other (another person's base and recent prints, any
    microphone, no discount), named when its raw degree is at least ``AGAINST_AT``.

    ``other_degree`` (reported): that other's degree, capped at ``SHORT_CAP`` for a short sound.

    ``intonation_fits``: pitch and rate within ``INTONATION_SPREADS`` spreads of the person's
    habits; ``None`` without habits or without a measure.

    ``core_degree`` (raw): the person's base and recent prints, as for ``degree`` but with no
    intonation, no passing print and no cap.

    ``other_raw`` (raw): the nearest known other's degree, with no cap.

    ``via_passing``: the degree is held up by a passing print (a cold): unsure, never "for".

    ``usable``: ``False`` when the print could not be read at all (no usable thresholds for its
    model, or a vector that is no print): nothing of the voice tells who spoke.

    ``to_relearn``: the person's voice is to be learnt again (``mark_to_relearn``: the assistant
    erased it on his own idea), whatever is read: a voice taken for them is not sure for what is
    sensitive until it is learnt again."""

    weighs: Weighs
    degree: float
    learnt: bool
    best_other: UUID | None
    other_degree: float
    intonation_fits: bool | None
    core_degree: float = 0.0
    other_raw: float = 0.0
    via_passing: bool = False
    short: bool = False
    usable: bool = True
    to_relearn: bool = False

    @property
    def resembles_nobody(self) -> bool:
        """The voice is learnt for the person, long enough to judge, far from it, and near no one
        else known."""
        return (
            self.learnt
            and not self.short
            and not self.via_passing
            and self.degree < AGAINST_AT
            and self.best_other is None
        )


NOT_LEARNED = Reading(
    weighs="unknown",
    degree=0.0,
    learnt=False,
    best_other=None,
    other_degree=0.0,
    intonation_fits=None,
)


def is_a_print(vector: Sequence[float]) -> bool:
    """Whether ``vector`` can be a print: finite, not all zero, not longer than the cap."""
    return (
        0 < len(vector) <= MAX_DIMENSIONS
        and all(math.isfinite(x) for x in vector)
        and any(x != 0.0 for x in vector)
    )


def usable_bounds(thresholds: PrintThresholds | None) -> PrintThresholds | None:
    """``thresholds`` when they are finite and ordered, else ``None`` (as if unknown)."""
    if thresholds is None:
        return None
    low, same, high = thresholds.print_low, thresholds.same_voice, thresholds.print_high
    if not all(math.isfinite(x) for x in (low, same, high)) or not low <= same <= high:
        return None
    if not low < high:
        return None
    return thresholds


def similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """The cosine of two vectors: 0 for a zero or non-finite vector or vectors of unequal
    length. Never not-a-number."""
    if len(a) != len(b) or not a:
        return 0.0
    if not all(math.isfinite(x) for x in a) or not all(math.isfinite(y) for y in b):
        return 0.0
    scale = max(max(abs(x) for x in a), max(abs(y) for y in b))
    if scale == 0.0:
        return 0.0
    # Scaled, so that very large components cannot overflow.
    a = [x / scale for x in a]
    b = [y / scale for y in b]
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    found = sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)
    return max(-1.0, min(1.0, found)) if math.isfinite(found) else 0.0


def degree_of(similarity_: float, thresholds: PrintThresholds) -> float:
    """The degree (0..1) of a similarity: linear between the bounds of its print model; 0 when
    the bounds are not ordered."""
    span = thresholds.print_high - thresholds.print_low
    if not math.isfinite(span) or span <= 0 or not math.isfinite(similarity_):
        return 0.0
    return min(1.0, max(0.0, (similarity_ - thresholds.print_low) / span))


def _mix(old: Sequence[float], new: Sequence[float], weight: float) -> list[float]:
    """``old`` moved ``weight`` of the way to ``new`` (``old`` itself if they do not fit)."""
    if len(old) != len(new):
        return list(old)
    return [(1.0 - weight) * a + weight * b for a, b in zip(old, new, strict=True)]


def _joined(
    print_: Print, recent: Sequence[Print], thresholds: PrintThresholds
) -> tuple[list[float], float]:
    """The vector to report and its seconds. A print of at least ``MIN_SECONDS`` is read alone;
    a shorter one is the mean of itself and the ``recent`` prints of its model that read "for"
    against it (a degree of ``FOR_AT`` or more), so that the utterance of another voice just
    before, even a near one, never lifts it."""
    vectors = [list(print_.vector)]
    seconds = print_.seconds
    if seconds >= MIN_SECONDS:
        return vectors[0], seconds
    for earlier in recent:
        if (
            earlier.model == print_.model
            and len(earlier.vector) == len(print_.vector)
            and is_a_print(earlier.vector)
            and degree_of(similarity(earlier.vector, print_.vector), thresholds) >= FOR_AT
        ):
            vectors.append(list(earlier.vector))
            seconds += earlier.seconds
    count = len(vectors)
    return [sum(column) / count for column in zip(*vectors, strict=True)], seconds


# --- Comparing a vector with the known prints -----------------------------------------------------


@dataclass(frozen=True)
class _Own:
    """A person's own prints against a vector (all raw degrees).

    ``core``: base and recent prints of this microphone, else of the others at
    ``OTHER_MICROPHONE``. ``base``: the base print of this microphone alone (0 with none).
    ``bases``: the base prints alone, read like ``core`` (what a new microphone enters on).
    ``passing``: the passing prints, read like ``core``. ``anywhere``: base and recent prints of
    any microphone, no discount (how the person compares with the nearest known other).
    ``learnt``: a base print on any microphone. ``base_here``: a base print on this one."""

    core: float = 0.0
    base: float = 0.0
    bases: float = 0.0
    passing: float = 0.0
    anywhere: float = 0.0
    learnt: bool = False
    base_here: bool = False


@dataclass(frozen=True)
class _Other:
    """The nearest known other: who, and their raw degree."""

    person_id: UUID | None = None
    degree: float = 0.0


@dataclass(frozen=True)
class _Measure:
    """A vector against the known prints: the person's own, and the nearest known other."""

    own: _Own
    other: _Other

    def other_is_nearer(self) -> bool:
        """The nearest known other is nearer than the person (a tie is not)."""
        return self.other.degree > self.own.anywhere

    def other_ties(self) -> bool:
        """The nearest known other reads "for" and is exactly as near as the person (common at
        the top of the degrees: two voices alike, twins)."""
        return self.other.degree >= FOR_AT and self.other.degree == self.own.anywhere

    def other_in_the_way(self) -> bool:
        """Whether the nearest known other keeps this vector from being learnt as the person's:
        the other reads "for", or the person has a learnt voice and the other is named
        (``AGAINST_AT``) and at least as near. A tie is in the way."""
        other = self.other.degree
        return other >= FOR_AT or (
            self.own.learnt and other >= AGAINST_AT and other >= self.own.anywhere
        )


def _best(
    rows: Sequence[VoicePrint],
    kinds: tuple[str, ...],
    vector: Sequence[float],
    thresholds: PrintThresholds,
    microphone: str | None,
) -> float:
    """The best degree of ``vector`` among ``rows`` of ``kinds``. With a ``microphone``: its own
    prints, else the others' at ``OTHER_MICROPHONE``; with ``None``: every microphone, no
    discount."""
    of_kind = [row for row in rows if row.kind in kinds]
    used, weight = of_kind, 1.0
    if microphone is not None:
        here = [row for row in of_kind if row.microphone == microphone]
        used, weight = (here, 1.0) if here else (of_kind, OTHER_MICROPHONE)
    found = max((similarity(vector, row.vector) for row in used), default=None)
    return 0.0 if found is None else degree_of(found, thresholds) * weight


def _own(
    rows: Sequence[VoicePrint],
    microphone: str,
    vector: Sequence[float],
    thresholds: PrintThresholds,
) -> _Own:
    here = [row for row in rows if row.microphone == microphone]
    return _Own(
        core=_best(rows, ("base", "recent"), vector, thresholds, microphone),
        base=_best(here, ("base",), vector, thresholds, microphone),
        bases=_best(rows, ("base",), vector, thresholds, microphone),
        passing=_best(rows, ("passing",), vector, thresholds, microphone),
        anywhere=_best(rows, ("base", "recent"), vector, thresholds, None),
        learnt=any(row.kind == "base" for row in rows),
        base_here=any(row.kind == "base" for row in here),
    )


def _nearest_other(
    known: dict[UUID, list[VoicePrint]],
    person_id: UUID | None,
    vector: Sequence[float],
    thresholds: PrintThresholds,
) -> _Other:
    """The nearest known other: the best degree, on any microphone and with no discount, of any
    known person but ``person_id``, read from base and recent prints only (a passing print may
    hold a voice that is not its owner's: it names nobody)."""
    nearest = _Other()
    for other_id, rows in known.items():
        if other_id == person_id:
            continue
        degree = _best(rows, ("base", "recent"), vector, thresholds, None)
        if degree > nearest.degree:
            nearest = _Other(other_id, degree)
    return nearest


async def _known_prints(
    session: AsyncSession, model: str, now: datetime
) -> dict[UUID, list[VoicePrint]]:
    """The live prints of this model of every known person (no candidates), by person."""
    rows = await session.execute(
        select(VoicePrint, VoiceRef.person_id)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(
            VoiceRef.person_id.is_not(None),
            VoicePrint.model == model,
            VoicePrint.kind != "candidate",
            (VoicePrint.expires_at.is_(None)) | (VoicePrint.expires_at > now),
        )
    )
    found: dict[UUID, list[VoicePrint]] = defaultdict(list)
    for row, person_id in rows.all():
        if person_id is not None:
            found[person_id].append(row)
    return found


async def _measure(
    session: AsyncSession,
    *,
    person_id: UUID | None,
    microphone: str,
    vector: Sequence[float],
    thresholds: PrintThresholds,
    model: str,
    now: datetime,
) -> _Measure:
    """``vector`` against the person's own prints and the nearest known other's."""
    known = await _known_prints(session, model, now)
    rows = known.get(person_id) if person_id is not None else None
    own = _Own() if rows is None else _own(rows, microphone, vector, thresholds)
    return _Measure(own=own, other=_nearest_other(known, person_id, vector, thresholds))


# --- Intonation -----------------------------------------------------------------------------------


def _fit(value: float | None, mean: float | None, spread: float | None) -> bool | None:
    if value is None or mean is None or spread is None:
        return None
    spread = max(spread, HABIT_SPREAD_FLOOR * abs(mean))
    return abs(value - mean) <= INTONATION_SPREADS * spread


async def _intonation_fits(
    session: AsyncSession, person_id: UUID, print_: Print, microphone: str
) -> bool | None:
    """Whether pitch and rate are within two spreads of the person's habits (``None`` when there
    is nothing to compare). The habits are those of the same print model, this microphone's
    first."""
    habits = await session.scalar(
        select(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(
            VoiceRef.person_id == person_id,
            VoicePrint.kind == "base",
            VoicePrint.model == print_.model,
        )
        .order_by((VoicePrint.microphone == microphone).desc(), VoicePrint.last_at.desc())
        .limit(1)
    )
    if habits is None:
        return None
    fits = [
        found
        for found in (
            _fit(print_.pitch_hz, habits.pitch_mean, habits.pitch_spread),
            _fit(print_.rate, habits.rate_mean, habits.rate_spread),
        )
        if found is not None
    ]
    return all(fits) if fits else None


def _with_intonation(degree: float, fits: bool | None) -> float:
    """``degree`` moved by at most ``INTONATION_ADJUST`` by whether intonation fits, and never out
    of its band: intonation does not lift a reading to ``for``, does not lower one under
    ``AGAINST_AT``, and does not lift a far voice into the band; it only nuances inside it."""
    if fits is None:
        return degree
    moved = degree + (INTONATION_ADJUST if fits else -INTONATION_ADJUST)
    if degree >= FOR_AT:
        low, high = FOR_AT, 1.0
    elif degree >= AGAINST_AT:
        low, high = AGAINST_AT, _JUST_UNDER_FOR
    else:
        low, high = 0.0, _JUST_UNDER_AGAINST
    return min(high, max(low, moved))


# --- Reading a voice ------------------------------------------------------------------------------


def _held_by_passing(own: _Own, degree: float, step: bool | None) -> tuple[float, bool]:
    """The passing floor: a voice its base and recent prints do not recognise but a passing print
    does (a cold) is unsure, in the band under ``FOR_AT``, never a stranger and never "for"."""
    if own.core >= FOR_AT or own.passing <= own.core or own.passing < AGAINST_AT:
        return degree, False
    held = _with_intonation(min(own.passing, _JUST_UNDER_FOR), step)
    return max(degree, held), True


def _verdict(measure: _Measure, *, short: bool, degree: float, via_passing: bool) -> Weighs:
    """What the reading weighs, one rule a line (the table at the top of the module)."""
    other, own = measure.other.degree, measure.own
    if not own.learnt or short:
        return "unknown"
    if other >= FOR_AT and measure.other_is_nearer():
        return "against"
    if measure.other_ties() or via_passing:
        return "unknown"
    if degree >= FOR_AT:
        return "for"
    if degree >= AGAINST_AT:
        return "unknown"
    if other >= AGAINST_AT and measure.other_is_nearer():
        return "against"
    return "unknown"


async def read_voice(
    session: AsyncSession,
    *,
    person_id: UUID | None,
    microphone: str,
    print_: Print,
    thresholds: PrintThresholds | None,
    recent: Sequence[Print] = (),
    now: datetime,
) -> Reading:
    """``print_`` read against the prints of ``person_id`` (``None``: nobody expected) and against
    the nearest known other. ``recent`` are the prints heard within ``JOIN_WITHIN_S`` seconds
    before: a print shorter than ``MIN_SECONDS`` adds up with those that read "for" in what is
    reported (``_joined``; ``learn`` never uses them). With no usable ``thresholds``, or a vector
    that is no print, the voice is not learned yet, and the print not usable
    (``Reading.usable``)."""
    bounds = usable_bounds(thresholds)
    if bounds is None or not is_a_print(print_.vector):
        return replace(NOT_LEARNED, usable=False)
    vector, seconds = _joined(print_, recent, bounds)
    short = seconds < MIN_SECONDS
    measure = await _measure(
        session,
        person_id=person_id,
        microphone=microphone,
        vector=vector,
        thresholds=bounds,
        model=print_.model,
        now=now,
    )
    own, other = measure.own, measure.other
    fits = (
        await _intonation_fits(session, person_id, print_, microphone)
        if person_id is not None and own.learnt
        else None
    )
    degree, via_passing = 0.0, False
    if own.learnt:
        step = None if short else fits
        degree, via_passing = _held_by_passing(own, _with_intonation(own.core, step), step)
    other_degree = other.degree
    if short:
        degree, other_degree = min(degree, SHORT_CAP), min(other_degree, SHORT_CAP)
    best_other = other.person_id if other.degree >= AGAINST_AT else None
    return Reading(
        weighs=_verdict(measure, short=short, degree=degree, via_passing=via_passing),
        degree=degree,
        learnt=own.learnt,
        best_other=best_other,
        other_degree=other_degree,
        intonation_fits=fits,
        core_degree=own.core,
        other_raw=other.degree,
        via_passing=via_passing,
        short=short,
        to_relearn=person_id is not None and await _to_relearn(session, person_id),
    )


async def _to_relearn(session: AsyncSession, person_id: UUID) -> bool:
    found = await session.scalar(
        select(VoiceRef.id)
        .where(VoiceRef.person_id == person_id, VoiceRef.relearn_at.is_not(None))
        .limit(1)
    )
    return found is not None


async def mark_to_relearn(session: AsyncSession, person_id: UUID, now: datetime) -> None:
    """What was learnt of ``person_id``'s voice was erased on the assistant's own idea: their
    voice is to be learnt again (``Reading.to_relearn``) until ``learn`` learns it. Flushed, the
    caller commits (in the transaction of the erasure)."""
    # Made at need: a person with no reference yet is marked too.
    await _person_ref(session, person_id)
    await session.execute(
        update(VoiceRef)
        .where(VoiceRef.person_id == person_id, VoiceRef.relearn_at.is_(None))
        .values(relearn_at=now)
    )


async def _relearnt(session: AsyncSession, person_id: UUID) -> None:
    """``person_id``'s voice is learnt again: no longer to be learnt again."""
    await session.execute(
        update(VoiceRef)
        .where(VoiceRef.person_id == person_id, VoiceRef.relearn_at.is_not(None))
        .values(relearn_at=None)
    )


# --- References -----------------------------------------------------------------------------------


def _new_reference() -> str:
    return REFERENCE_PREFIX + secrets.token_hex(8)


async def _person_ref(session: AsyncSession, person_id: UUID) -> VoiceRef:
    """The person's reference (the oldest, when they have several), made at need."""
    ref = await session.scalar(
        select(VoiceRef)
        .where(VoiceRef.person_id == person_id)
        .order_by(VoiceRef.created_at, VoiceRef.id)
        .limit(1)
    )
    if ref is None:
        ref = VoiceRef(reference=_new_reference(), person_id=person_id)
        session.add(ref)
        await session.flush()
    return ref


async def _find(
    session: AsyncSession, ref_id: UUID, microphone: str, kind: str, model: str
) -> VoicePrint | None:
    return await session.scalar(
        select(VoicePrint).where(
            VoicePrint.ref_id == ref_id,
            VoicePrint.microphone == microphone,
            VoicePrint.kind == kind,
            VoicePrint.model == model,
        )
    )


async def _insert(session: AsyncSession, row: VoicePrint) -> VoicePrint:
    """Add ``row``; when another utterance inserted the same (reference, microphone, kind, model)
    meanwhile, that row is returned instead and this one is dropped."""
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        found = await _find(session, row.ref_id, row.microphone, row.kind, row.model)
        if found is None:
            raise
        return found
    return row


def _move_habits(row: VoicePrint, print_: Print, weight: float) -> None:
    """Move the habits of pitch and rate of ``row`` toward this print's measures; a spread never
    goes under its floor."""
    for value, mean_name, spread_name in (
        (print_.pitch_hz, "pitch_mean", "pitch_spread"),
        (print_.rate, "rate_mean", "rate_spread"),
    ):
        if value is None or not math.isfinite(value) or value <= 0:
            continue
        mean = getattr(row, mean_name)
        spread = getattr(row, spread_name)
        if mean is None or spread is None:
            setattr(row, mean_name, value)
            setattr(row, spread_name, HABIT_FIRST_SPREAD * value)
        else:
            moved = (1.0 - weight) * spread + weight * abs(value - mean)
            new_mean = (1.0 - weight) * mean + weight * value
            setattr(row, spread_name, max(moved, HABIT_SPREAD_FLOOR * abs(new_mean)))
            setattr(row, mean_name, new_mean)


async def reference_for(
    session: AsyncSession,
    *,
    person_id: UUID | None,
    print_: Print,
    microphone: str,
    now: datetime,
    thresholds: PrintThresholds | None = None,
) -> str:
    """The reference of a voice: the person's own (made at need), or, for nobody known, that of
    the unknown voice whose first print is nearest (a similarity of the model's ``same_voice`` or
    more: the module's starting value without usable ``thresholds``), else a new one. An unknown
    voice keeps its first print as a candidate that lives ``UNKNOWN_LIVES`` after it was last
    heard, so that the next sentence finds it; the print never moves, so strangers near one
    another never chain into one reference. A voice that is no print, or a new voice shorter than
    ``MIN_SECONDS``, is given a reference and stores nothing."""
    if person_id is not None:
        return (await _person_ref(session, person_id)).reference
    if not is_a_print(print_.vector):
        return _new_reference()
    same = (usable_bounds(thresholds) or STARTING_BOUNDS).same_voice
    rows = await session.execute(
        select(VoicePrint, VoiceRef)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(
            VoiceRef.person_id.is_(None),
            VoicePrint.model == print_.model,
            (VoicePrint.expires_at.is_(None)) | (VoicePrint.expires_at > now),
        )
    )
    best: tuple[float, VoiceRef] | None = None
    for row, ref in rows.all():
        found = similarity(print_.vector, row.vector)
        if found >= same and (best is None or found > best[0]):
            best = (found, ref)
    if best is not None:
        if print_.seconds >= MIN_SECONDS:
            await _hear_unknown(session, best[1].id, microphone, print_, now)
        return best[1].reference
    if print_.seconds < MIN_SECONDS:
        return _new_reference()
    ref = VoiceRef(reference=_new_reference())
    session.add(ref)
    await session.flush()
    await _hear_unknown(session, ref.id, microphone, print_, now)
    return ref.reference


async def warned_at(session: AsyncSession, reference: str) -> datetime | None:
    """When the voice of ``reference`` was told that the assistant listens (``None``: never, or
    no such reference is kept)."""
    return await session.scalar(select(VoiceRef.warned_at).where(VoiceRef.reference == reference))


async def mark_warned(session: AsyncSession, reference: str, now: datetime) -> None:
    """The voice of ``reference`` was told it is listened to (a reply to it said whole): kept once,
    the first time. Nothing for a reference that is not kept. The caller commits."""
    ref = await session.scalar(select(VoiceRef).where(VoiceRef.reference == reference))
    if ref is not None and ref.warned_at is None:
        ref.warned_at = now
        await session.flush()


async def _first_candidate(session: AsyncSession, ref_id: UUID, model: str) -> VoicePrint | None:
    """The oldest candidate print of a reference, on whatever microphone."""
    return await session.scalar(
        select(VoicePrint)
        .where(
            VoicePrint.ref_id == ref_id,
            VoicePrint.kind == "candidate",
            VoicePrint.model == model,
        )
        .order_by(VoicePrint.first_visit_at, VoicePrint.id)
        .limit(1)
    )


async def _hear_unknown(
    session: AsyncSession, ref_id: UUID, microphone: str, print_: Print, now: datetime
) -> None:
    """An unknown voice heard again: its first print stays as it is (whatever the microphone: a
    new one does not store a second first print), it counts one more utterance and lives on."""
    row = await _first_candidate(session, ref_id, print_.model)
    if row is None:
        row = VoicePrint(
            ref_id=ref_id,
            microphone=microphone,
            kind="candidate",
            model=print_.model,
            vector=list(print_.vector),
            utterances=1,
            visits=1,
            first_visit_at=now,
            last_at=now,
            expires_at=now + UNKNOWN_LIVES,
        )
        _move_habits(row, print_, 1.0)
        fresh = row
        row = await _insert(session, fresh)
        if row is fresh:
            return
    row.utterances += 1
    row.last_at = now
    row.expires_at = now + UNKNOWN_LIVES
    await session.flush()


# --- Learning -------------------------------------------------------------------------------------


async def _person_rows(
    session: AsyncSession, person_id: UUID, microphone: str, model: str
) -> list[VoicePrint]:
    rows = await session.scalars(
        select(VoicePrint)
        .join(VoiceRef, VoiceRef.id == VoicePrint.ref_id)
        .where(
            VoiceRef.person_id == person_id,
            VoicePrint.microphone == microphone,
            VoicePrint.model == model,
        )
    )
    return list(rows)


async def learn(
    session: AsyncSession,
    *,
    person_id: UUID,
    microphone: str,
    print_: Print,
    thresholds: PrintThresholds | None,
    sure_otherwise: bool,
    visit_at: datetime,
    now: datetime,
) -> Learning:
    """Learn from ``print_`` alone, only when something else than the voice says it is
    ``person_id`` (``sure_otherwise``), the sound is long enough, it is a print and the model's
    ``thresholds`` are usable. Every degree is the single print's, raw: never joined with the
    utterances around it, never nuanced by intonation, never read through a passing print.

    Every utterance heard on a microphone where the person has a base (sure or not, long or
    short) counts for the visit (``_heard``): a visit ends after ``VISIT_GAP`` with nothing heard
    there, by the server's ``now`` (``visit_at`` only counts the visits of a candidate).

    The ladder, one rule a rung:
    - a base on this microphone, the print "for" against that BASE: the recent print moves fast
      and the base slowly, at most once a visit ("updated"), unless the nearest known other is at
      least as near;
    - a base on this microphone, the print between the thresholds against it and no other in
      the way: a passing print is made or renewed ("passing"), nothing else moves;
    - a base on this microphone, anything else: "nothing";
    - another known voice in the way: "nothing";
    - learnt on other microphones only: a candidate here when the person's base prints read
      "for" (at the lesser weight), else "nothing";
    - not learnt: the print joins the candidate of this microphone when it resembles it, else
      restarts it; the candidate becomes the base and recent prints after ``LEARN_UTTERANCES``
      like utterances on ``LEARN_VISITS`` visits (a visit is ``VISIT_GAP`` after the last
      utterance), unless its mean has another known voice in the way or, on a new microphone,
      no longer reads "for" against the person's bases."""
    bounds = usable_bounds(thresholds)
    if bounds is None or not is_a_print(print_.vector):
        return "nothing"
    here = await _person_rows(session, person_id, microphone, print_.model)
    _heard(here, now)
    await session.flush()
    if not sure_otherwise or print_.seconds < MIN_SECONDS:
        return "nothing"
    measure = await _measure(
        session,
        person_id=person_id,
        microphone=microphone,
        vector=print_.vector,
        thresholds=bounds,
        model=print_.model,
        now=now,
    )
    own = measure.own
    ref = await _person_ref(session, person_id)
    if own.base_here and own.base >= FOR_AT:
        if measure.other.degree >= own.anywhere:
            return "nothing"
        await _follow(session, ref, here, microphone, print_, now)
        await _relearnt(session, person_id)
        return "updated"
    if own.base_here and own.base >= AGAINST_AT and not measure.other_in_the_way():
        await _pass(session, ref, here, microphone, print_, now)
        return "passing"
    if own.base_here or measure.other_in_the_way():
        return "nothing"
    if own.learnt and own.bases < FOR_AT:
        return "nothing"
    found = await _candidate(session, ref, here, microphone, print_, bounds, visit_at, now)
    if found == "learnt":
        await _relearnt(session, person_id)
    return found


def _heard(here: Sequence[VoicePrint], now: datetime) -> None:
    """The person was heard on this microphone at ``now``: a new visit begins when nothing was
    heard there for ``VISIT_GAP`` (the base may move once in it)."""
    base = next((row for row in here if row.kind == "base"), None)
    if base is None:
        return
    if base.heard_at is None or now - base.heard_at >= VISIT_GAP:
        base.moved_in_visit = False
    if base.heard_at is None or now > base.heard_at:
        base.heard_at = now


async def _follow(
    session: AsyncSession,
    ref: VoiceRef,
    here: Sequence[VoicePrint],
    microphone: str,
    print_: Print,
    now: datetime,
) -> None:
    """The recent print follows every sure utterance; the base moves once a visit."""
    for kind, weight in (("recent", RECENT_WEIGHT), ("base", BASE_WEIGHT)):
        row = next((row for row in here if row.kind == kind), None)
        if row is not None and kind == "base" and row.moved_in_visit:
            continue
        if row is None:
            row = await _insert(
                session,
                VoicePrint(
                    ref_id=ref.id,
                    microphone=microphone,
                    kind=kind,
                    model=print_.model,
                    vector=list(print_.vector),
                    utterances=0,
                    visits=1,
                    first_visit_at=now,
                    last_at=now,
                    learnt_at=now,
                ),
            )
        else:
            row.vector = _mix(row.vector, print_.vector, weight)
        row.utterances += 1
        row.last_at = now
        _move_habits(row, print_, weight)
        if kind == "base":
            row.heard_at, row.moved_in_visit = now, True
    await session.flush()


async def _pass(
    session: AsyncSession,
    ref: VoiceRef,
    here: Sequence[VoicePrint],
    microphone: str,
    print_: Print,
    now: datetime,
) -> None:
    row = next((row for row in here if row.kind == "passing"), None)
    if row is not None and row.expires_at is not None and row.expires_at <= now:
        # Dead but not yet swept: replaced, never mixed into.
        row.vector = list(print_.vector)
        row.utterances = 0
        row.visits = 1
        row.first_visit_at = now
    elif row is None:
        row = await _insert(
            session,
            VoicePrint(
                ref_id=ref.id,
                microphone=microphone,
                kind="passing",
                model=print_.model,
                vector=list(print_.vector),
                utterances=0,
                visits=1,
                first_visit_at=now,
                last_at=now,
            ),
        )
    else:
        row.vector = _mix(row.vector, print_.vector, RECENT_WEIGHT)
    row.utterances += 1
    row.last_at = now
    row.expires_at = now + PASSING_LIVES
    await session.flush()


async def _candidate(
    session: AsyncSession,
    ref: VoiceRef,
    here: Sequence[VoicePrint],
    microphone: str,
    print_: Print,
    thresholds: PrintThresholds,
    visit_at: datetime,
    now: datetime,
) -> Learning:
    row = next((row for row in here if row.kind == "candidate"), None)
    if row is None:
        row = VoicePrint(
            ref_id=ref.id,
            microphone=microphone,
            kind="candidate",
            model=print_.model,
            vector=list(print_.vector),
            utterances=1,
            visits=1,
            first_visit_at=visit_at,
            last_at=visit_at,
        )
        _move_habits(row, print_, 1.0)
        fresh = row
        row = await _insert(session, fresh)
        if row is not fresh:
            # Another utterance made it meanwhile: this one counts on it.
            row.utterances += 1
            row.last_at = max(row.last_at, visit_at)
            await session.flush()
        return "candidate"
    if similarity(row.vector, print_.vector) < thresholds.same_voice:
        # Not the same voice as before: start again from this one.
        row.vector = list(print_.vector)
        row.utterances = 1
        row.visits = 1
        row.first_visit_at = visit_at
        row.last_at = visit_at
        row.pitch_mean = row.pitch_spread = row.rate_mean = row.rate_spread = None
        _move_habits(row, print_, 1.0)
        await session.flush()
        return "candidate"
    count = row.utterances
    row.vector = _mix(row.vector, print_.vector, 1.0 / (count + 1))
    row.utterances = count + 1
    if visit_at - row.last_at >= VISIT_GAP:
        row.visits += 1
    row.last_at = max(row.last_at, visit_at)
    _move_habits(row, print_, 1.0 / (count + 1))
    if row.utterances < LEARN_UTTERANCES or row.visits < LEARN_VISITS:
        await session.flush()
        return "candidate"
    # The mean of the candidate may by now be the voice of another known person.
    mean = await _measure(
        session,
        person_id=ref.person_id,
        microphone=microphone,
        vector=row.vector,
        thresholds=thresholds,
        model=print_.model,
        now=now,
    )
    if mean.other_in_the_way() or (mean.own.learnt and mean.own.bases < FOR_AT):
        await session.delete(row)
        await session.flush()
        return "nothing"
    for kind in ("base", "recent"):
        learnt = next((r for r in here if r.kind == kind), None)
        if learnt is None:
            learnt = await _insert(
                session,
                VoicePrint(
                    ref_id=ref.id,
                    microphone=microphone,
                    kind=kind,
                    model=print_.model,
                    vector=list(row.vector),
                ),
            )
        learnt.vector = list(row.vector)
        learnt.utterances = row.utterances
        learnt.visits = row.visits
        learnt.first_visit_at = row.first_visit_at
        learnt.last_at = row.last_at
        learnt.learnt_at = now
        if kind == "base":
            learnt.heard_at, learnt.moved_in_visit = now, True
        learnt.pitch_mean, learnt.pitch_spread = row.pitch_mean, row.pitch_spread
        learnt.rate_mean, learnt.rate_spread = row.rate_mean, row.rate_spread
    await session.delete(row)
    await session.flush()
    return "learnt"


# --- Forgetting -----------------------------------------------------------------------------------


async def unlearn(
    session: AsyncSession,
    *,
    person_id: UUID,
    since: datetime | None,
    microphone: str | None,
) -> int:
    """ "That was not me": remove the candidate, recent and passing prints of ``person_id`` (those
    last moved since ``since``, on ``microphone``, when given) and the base prints (all of them
    when ``since`` is ``None``, else those made since). Returns how many rows went. To learn a
    voice again from the start (a voice that changed fast), ``unlearn`` with ``since=None`` and
    let the usual learning make a new base."""
    mine = VoicePrint.ref_id.in_(select(VoiceRef.id).where(VoiceRef.person_id == person_id))
    scope: list[ColumnElement[bool]] = [mine]
    if microphone is not None:
        scope.append(VoicePrint.microphone == microphone)
    if since is None:
        wrong = VoicePrint.kind.in_(["candidate", "recent", "passing", "base"])
    else:
        wrong = (
            VoicePrint.kind.in_(["candidate", "recent", "passing"]) & (VoicePrint.last_at >= since)
        ) | ((VoicePrint.kind == "base") & (VoicePrint.learnt_at >= since))
    removed = await session.execute(
        delete(VoicePrint).where(*scope, wrong).returning(VoicePrint.id)
    )
    return len(removed.all())


async def erase_reference(session: AsyncSession, reference: str) -> None:
    """The reference and every print under it, gone."""
    await session.execute(delete(VoiceRef).where(VoiceRef.reference == reference))


async def whose_reference(session: AsyncSession, reference: str) -> tuple[bool, UUID | None]:
    """Whether ``reference`` is kept, and whose it is (``None``: a voice nobody knows)."""
    found = (
        await session.execute(select(VoiceRef.person_id).where(VoiceRef.reference == reference))
    ).first()
    return (False, None) if found is None else (True, found[0])


async def erase_unknown(session: AsyncSession, reference: str) -> int:
    """A voice nobody knows erased whole: its prints, then its reference; how many rows went
    (nothing for a reference that is someone's, or not kept)."""
    unknown = select(VoiceRef.id).where(
        VoiceRef.reference == reference, VoiceRef.person_id.is_(None)
    )
    prints = await session.execute(
        delete(VoicePrint).where(VoicePrint.ref_id.in_(unknown)).returning(VoicePrint.id)
    )
    refs = await session.execute(
        delete(VoiceRef)
        .where(VoiceRef.reference == reference, VoiceRef.person_id.is_(None))
        .returning(VoiceRef.id)
    )
    return len(prints.all()) + len(refs.all())


async def sweep(session: AsyncSession, now: datetime) -> int:
    """Remove the passing prints, the unknown voices not heard for ``UNKNOWN_LIVES`` (their
    prints, then their references with no print left) and the moments past their end; how many
    rows went."""
    prints = await session.execute(
        delete(VoicePrint)
        .where(VoicePrint.kind == "passing", VoicePrint.expires_at <= now)
        .returning(VoicePrint.id)
    )
    unknown = await session.execute(
        delete(VoicePrint)
        .where(
            VoicePrint.expires_at <= now,
            VoicePrint.ref_id.in_(select(VoiceRef.id).where(VoiceRef.person_id.is_(None))),
        )
        .returning(VoicePrint.id)
    )
    refs = await session.execute(
        delete(VoiceRef)
        .where(
            VoiceRef.person_id.is_(None),
            ~exists().where(VoicePrint.ref_id == VoiceRef.id),
        )
        .returning(VoiceRef.id)
    )
    moments = await session.execute(
        delete(VoiceMoment).where(VoiceMoment.expires_at <= now).returning(VoiceMoment.id)
    )
    return len(prints.all()) + len(unknown.all()) + len(refs.all()) + len(moments.all())
