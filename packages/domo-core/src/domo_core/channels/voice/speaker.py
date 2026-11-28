"""Who probably speaks at a page, from what the voice of one sentence says (spec 6.5).

``voice_speaker`` turns the reading of a sentence's voice against the page's person
(``prints.Reading``) into a ``Speaker``: the probable person and the clues the code made, each
with its real age. The standing (``conversation.speaker.standing``) follows from them alone:

=================================================  ===========  ==================  =========
What the voice says                                Person       Voice clue          Standing
=================================================  ===========  ==================  =========
no print; not learnt yet; a short sound; fits       the page's   ``unknown`` or      sure (*)
the page's person (``for``)                                     ``for``, degree
another known person, clearly (``other_degree``     that person  ``for`` them        unsure
from ``FOR_AT`` and above the page person's)                    (session ``against``)
learnt for the page's person on this microphone,    nobody       ``against``         unknown
long enough, far from it (under ``AGAINST_AT``),
nobody else clearly
anything else (a cold, a voice between the          the page's   ``against``         unsure
thresholds, a tie; far from the page's person on
a microphone they have no base print on)
=================================================  ===========  ==================  =========

(*) for what is ordinary only, when the sound gave no print or a short one (see below). A voice
that reads clearly as another known person is that person even when the page person's voice is
not learnt yet.

Besides the voice: the page's session (dated when the person signed in; for another person, a
weak ``session`` clue ``against`` them: the page is signed in as someone else, whose person is
kept on the speaker, ``Speaker.page_person_id``; none for nobody), an ``intonation`` clue when
the reading measured it against the page's person's habits (not for another person: it was
measured against the page's person), and the proofs kept for the page's person, only while the
voice does not contradict them. Every clue of the voice is weak: a voice alone never establishes
anybody. A short sound is never against anybody: it cannot be judged, so it says nothing of who
speaks (``unknown``).

A sentence whose voice is too short to tell who said it, even joined with the same voice heard
just before (``Reading.short``), or that gave no print at all or one that cannot be read
(``Reading.usable``), is marked
(``Speaker.voice_too_short``, with a ``voice`` clue ``unknown``): still the page's person, and
sure for what is ordinary, but unsure for what is sensitive, a kept proof or not (ruling T22): a
stranger's short "yes" at a signed-in page is never taken as that person's without the judge.
"""

from datetime import datetime
from typing import Literal

from domo_core.accounts.models import Person
from domo_core.channels.voice.abilities import VOICE_CHANNEL
from domo_core.channels.voice.prints import Reading
from domo_core.channels.voice.thresholds import AGAINST_AT, FOR_AT
from domo_core.conversation.speaker import Clue, Speaker, heeded_proofs
from domo_core.llm.speech.client import Print

_Row = Literal["page", "unsure", "other", "nobody"]


def voice_speaker(
    *,
    page_person: Person,
    reading: Reading,
    print_: Print | None,
    microphone: str,
    signed_in_at: datetime,
    proofs: tuple[Clue, ...],
    now: datetime,
) -> Speaker:
    """Who probably speaks a sentence heard at ``page_person``'s page on ``microphone`` at
    ``now``, from the ``reading`` of its voice (``print_``: ``None`` when it had none), the page's
    session opened at ``signed_in_at``, and the ``proofs`` kept for the page's person on this
    channel and microphone (see the module docstring)."""
    session = Clue("session", "for", "strong", "code", signed_in_at)
    row = _row_of(reading, print_)
    if row == "other":
        assert reading.best_other is not None
        voice = Clue("voice", "for", "weak", "code", now, degree=reading.other_degree)
        # Heard at a page signed in as someone else: the code says so (review of Task 24).
        elsewhere = Clue("session", "against", "weak", "code", signed_in_at)
        return Speaker(
            reading.best_other,
            VOICE_CHANNEL,
            clues=(voice, elsewhere),
            microphone=microphone,
            page_person_id=page_person.id,
        )
    # The page's session is the page person's: no clue of it for nobody known (ruling T23).
    clues: list[Clue] = [] if row == "nobody" else [session]
    if print_ is not None:
        weighs: Literal["for", "against", "unknown"] = (
            "for"
            if row == "page" and reading.weighs == "for"
            else "unknown"
            if row == "page"
            else "against"
        )
        clues.append(Clue("voice", weighs, "weak", "code", now, degree=reading.degree))
        if reading.intonation_fits is not None:
            fits = "for" if reading.intonation_fits else "against"
            clues.append(Clue("intonation", fits, "weak", "code", now))
    else:
        # Heard, but nothing of the voice to tell who said it.
        clues.append(Clue("voice", "unknown", "weak", "code", now))
    if row != "nobody":
        # What is kept is the page person's, never a stranger's (review of Task 23).
        clues.extend(heeded_proofs(tuple(clues), proofs))
    return Speaker(
        None if row == "nobody" else page_person.id,
        VOICE_CHANNEL,
        clues=tuple(clues),
        microphone=microphone,
        voice_too_short=print_ is None or reading.short or not reading.usable or reading.to_relearn,
    )


def _row_of(reading: Reading, print_: Print | None) -> _Row:
    """Which row of the module docstring's table the voice is in."""
    if print_ is None or reading.short:
        return "page"
    if (
        reading.best_other is not None
        and reading.other_degree >= FOR_AT
        and reading.other_degree > reading.degree
    ):
        # Another known person, clearly: that person, whether or not the page person's voice is
        # learnt yet (final fix wave, voice I3).
        return "other"
    if not reading.learnt or reading.weighs == "for":
        return "page"
    if not reading.via_passing and reading.degree < AGAINST_AT and not reading.new_microphone:
        return "nobody"
    # A microphone the page's person has no base print on: far is unsure, never nobody known.
    return "unsure"


def carrying_on(
    speaker: Speaker,
    *,
    page_person: Person,
    signed_in_at: datetime,
    proofs: tuple[Clue, ...],
    now: datetime,
) -> Speaker:
    """``speaker``, read like nobody known at ``page_person``'s page, taken as that person after
    all because the quick model judged the sentence theirs (``channels.voice.whose``): the page's
    session for them, every clue of the voice kept (its ``against`` makes them unsure, and the
    judge sees it), and the assistant's own reading (a ``context`` clue of his, which never changes
    the standing)."""
    clues = (
        Clue("session", "for", "strong", "code", signed_in_at),
        *speaker.clues,
        Clue("context", "for", "weak", "domo", now),
    )
    return Speaker(
        page_person.id,
        VOICE_CHANNEL,
        clues=clues + heeded_proofs(clues, proofs),
        microphone=speaker.microphone,
        voice_too_short=speaker.voice_too_short,
    )
