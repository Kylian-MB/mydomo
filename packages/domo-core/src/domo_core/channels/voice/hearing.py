"""Hearing a moment of speech (spec 6.1, 6.4, 6.5, 6.11): the sound becomes words and a voice print
at once, the voice is read against the page's person, and the sound is let go.

``hear`` asks the person's METERED speech clients (``deps.VoiceDeps.speech``): the words
(``hear``) and the print of the voice (``voice_print``) run together, both where the owner lets
what is heard go (at home by default), and always at home for a sentence heard as a proof
(``for_a_proof``). A print that fails never loses the words (no print, no reading). Words that are
empty, or only marks of noise (``[music]``, ``(laughs)``), are nothing heard. Once both calls are
over, nothing here holds the sound any more: it is never written, logged or kept.

The voice is read against the page's person (``prints.read_voice``, with the prints heard just
before for a short sound) under their reference, or under the reference of an unknown voice when
the reading says it resembles nobody known (``prints.reference_for``). Nothing is learnt here:
learning is the session's, which knows whether the words were a reserved moment's.

The house's speech service is given the known language of the page's person, else the house's,
as a hint (the owner, 2026-11-28, "donner la langue connue"): it hears in that language when its
own detection is not clear (a short sound, a low probability), and in the language it detects
when that is clear, so a sentence plainly said in another language is still heard in it and
answered in it. A provider outside the house gets no hint and detects as before.

When nothing can hear (no model where it may go, a service still preparing, down, or a firm
limit), ``CannotHear`` says what the page is told (``protocol.Hearing``): ``preparing``, or
``unavailable`` (with ``no_model`` when no model may hear at all).
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import anyio
from domo_common.text_rules import language_tag

from domo_core.accounts.models import Person
from domo_core.channels.voice.deps import VoiceDeps
from domo_core.channels.voice.prints import Reading, read_voice, reference_for
from domo_core.llm.speech.client import Heard, Print, PrintThresholds, SpeechError
from domo_core.llm.speech.metered import MeteredSpeech
from domo_core.policy.settings import load_policy_settings

logger = logging.getLogger(__name__)

# What a transcription writes for what is not words: a mark in brackets, a note between stars, a
# musical note.
_NOISE: Final = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪")
# Under this many seconds of sound, the language a hearing model detects is not trusted (the
# trials: a half-second French "Oui." read as English by every model): it is left unknown, and the
# answer falls back to the person's language, then the house's.
LANGUAGE_FROM_S: Final = 1.5


class CannotHear(Exception):
    """Nothing can hear the sound now. ``hearing`` is what the page is told (``preparing``, or
    ``unavailable``); ``no_model``: no model may hear at all (none at home, and the owner does not
    let what is heard go out, or none anywhere)."""

    def __init__(self, hearing: Literal["preparing", "unavailable"], *, no_model: bool = False):
        super().__init__(hearing)
        self.hearing: Literal["preparing", "unavailable"] = hearing
        self.no_model = no_model


@dataclass(frozen=True)
class HeardUtterance:
    """What a moment of speech was: its words (``text``) in ``language`` (empty when not known, as
    for a sound shorter than ``LANGUAGE_FROM_S``), its ``seconds``, the
    ``microphone`` it came from, the print of the voice (``print_``; ``None`` when it could not be
    made), the voice read against the page's person (``reading``) under ``reference``, the
    print model's ``thresholds`` (``None``: not known), and when it was heard (``at``)."""

    text: str
    language: str
    seconds: float
    microphone: str
    print_: Print | None
    reading: Reading | None
    reference: str | None
    at: datetime
    thresholds: PrintThresholds | None = None


def words_in(text: str) -> bool:
    """Whether a transcription holds words: something but marks of noise and punctuation."""
    return any(character.isalnum() for character in _NOISE.sub(" ", text))


async def hear(
    deps: VoiceDeps,
    *,
    person: Person,
    microphone: str,
    pcm: bytes,
    for_a_proof: bool,
    now: datetime,
    recent: Sequence[Print] = (),
) -> HeardUtterance | None:
    """What ``person``'s page heard on ``microphone`` (see the module docstring): ``None`` when
    it holds no words; ``CannotHear`` when nothing can hear it. ``recent`` are the prints heard
    just before (a short sound is read with those of the same voice)."""
    hearing = await deps.speech.open(person, "hear", for_a_proof=for_a_proof)
    if hearing is None:
        # A phrase is only ever heard at home: none there is not "no model may hear at all".
        raise CannotHear("unavailable", no_model=not for_a_proof)
    printing = await deps.speech.open(person, "voice_print", for_a_proof=for_a_proof)
    language = await _hint(deps, person)
    words, print_ = await _both(hearing, printing, pcm, language)
    del pcm  # the sound is let go: nothing below needs it
    if not words_in(words.text):
        return None
    reading: Reading | None = None
    reference: str | None = None
    thresholds: PrintThresholds | None = None
    if print_ is not None and printing is not None:
        thresholds = await _thresholds_of(deps, printing, print_.model)
        try:
            reading, reference = await _read(
                deps, person, microphone, print_, thresholds, recent, now
            )
        except Exception as error:
            # The words go on, with no reading: the voice says nothing of who speaks this time.
            logger.error("A voice could not be read (%s)", type(error).__name__)
        else:
            # The verdict alone: what the voice is said to be, never more of it.
            logger.debug("A voice was read: %s", reading.weighs)
    return HeardUtterance(
        text=words.text.strip(),
        language=words.language if words.seconds >= LANGUAGE_FROM_S else "",
        seconds=words.seconds,
        microphone=microphone,
        print_=print_,
        reading=reading,
        reference=reference,
        at=now,
        thresholds=thresholds,
    )


@dataclass
class _Got:
    """What the two calls of ``_both`` gave: the words, or why not; the print."""

    words: Heard | None = None
    failed: str | None = None
    print_: Print | None = None


async def _hint(deps: VoiceDeps, person: Person) -> str | None:
    """The language the hearing is hinted to hear in: the page person's, else the house's,
    its first part only (``fr`` for ``fr-BE``); ``None`` (the model guesses) when neither is a
    language tag."""
    known = person.language
    if known is None or language_tag(known) is None:
        async with deps.turn.session_factory() as session:
            known = (await load_policy_settings(session)).house_language
    tag = language_tag(known)
    return None if tag is None else tag.split("-", 1)[0]


async def _both(
    hearing: MeteredSpeech, printing: MeteredSpeech | None, pcm: bytes, language: str | None
) -> tuple[Heard, Print | None]:
    """The words of ``pcm``, with ``language`` as the hint of the language they are in
    (``None``: none), and its print, asked at once. ``CannotHear`` when the words cannot be had;
    a print that fails is no print."""
    got = _Got()

    async def listen() -> None:
        try:
            got.words = await hearing.transcribe(pcm, hint=language)
        except SpeechError as error:
            got.failed = error.reason
        except Exception as error:
            logger.warning("A sound could not be heard (%s)", type(error).__name__)
            got.failed = "failed"

    async def take_the_print() -> None:
        assert printing is not None
        try:
            got.print_ = await printing.voice_print(pcm)
        except Exception as error:
            # The words go on without it.
            logger.info("A voice could not be printed (%s)", type(error).__name__)

    async with anyio.create_task_group() as group:
        group.start_soon(listen)
        if printing is not None:
            group.start_soon(take_the_print)
    if got.words is None:
        raise CannotHear("preparing" if got.failed == "preparing" else "unavailable")
    return got.words, got.print_


async def _read(
    deps: VoiceDeps,
    person: Person,
    microphone: str,
    print_: Print,
    thresholds: PrintThresholds | None,
    recent: Sequence[Print],
    now: datetime,
) -> tuple[Reading, str]:
    """The voice of ``print_`` read against ``person``, and its reference (theirs, or an unknown
    voice's when it resembles nobody known)."""
    async with deps.turn.session_factory() as session:
        reading = await read_voice(
            session,
            person_id=person.id,
            microphone=microphone,
            print_=print_,
            thresholds=thresholds,
            recent=recent,
            now=now,
        )
        reference = await reference_for(
            session,
            person_id=None if reading.resembles_nobody else person.id,
            print_=print_,
            microphone=microphone,
            now=now,
            thresholds=thresholds,
        )
        await session.commit()
    return reading, reference


async def _thresholds_of(
    deps: VoiceDeps, printing: MeteredSpeech, model: str
) -> PrintThresholds | None:
    """The thresholds of the print model; ``None`` (the voice is then not learnt yet) when they
    cannot be read."""
    try:
        return await deps.speech.thresholds(printing, model)
    except Exception as error:
        logger.info("The thresholds of a print model could not be read (%s)", type(error).__name__)
        return None
