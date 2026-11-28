"""The language of what is heard (the owner, 2026-11-28, "donner la langue connue"): a language
asked is heard in; a HINT (the person's known language) is used only when the model's own
detection is not clear (a short sound, a low probability), so that a sentence clearly said in
another language is still heard in it."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from domo_speech.real import hears

SECOND = b"\x00\x00" * 16_000


class _Model:
    supported_languages = ("en", "fr", "nl")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.asked: list[str | None] = []
        self.decoded: list[str | None] = []
        self.detected: tuple[str, float] = ("en", 0.95)

    def transcribe(self, audio: Any, *, language: str | None, **kwargs: Any) -> Any:
        self.asked.append(language)
        found, probability = (language, 1.0) if language else self.detected

        def parts() -> Any:
            # Decoding is lazy, as in the library: only what is read is decoded.
            self.decoded.append(found)
            yield SimpleNamespace(text=f" heard in {found}", words=[])

        return parts(), SimpleNamespace(language=found, language_probability=probability)


@pytest.fixture
def hearing(monkeypatch: pytest.MonkeyPatch) -> hears.Hearing:
    monkeypatch.setattr(hears, "_faster_whisper", lambda: SimpleNamespace(WhisperModel=_Model))
    return hears.Hearing(Path("weights"), gpu=False)


def _model(hearing: hears.Hearing) -> _Model:
    return hearing._model  # pyright: ignore[reportPrivateUsage]


def test_a_language_asked_is_heard_in(hearing: hears.Hearing) -> None:
    transcript = hearing.transcribe(SECOND * 3, "fr")
    assert _model(hearing).asked == ["fr"]
    assert transcript.language == "fr"


def test_a_language_the_model_does_not_know_lets_it_guess(hearing: hears.Hearing) -> None:
    hearing.transcribe(SECOND * 3, "xx")
    hearing.transcribe(SECOND * 3, None, hint="xx")
    assert _model(hearing).asked == [None, None]


def test_a_clear_detection_wins_over_the_hint(hearing: hears.Hearing) -> None:
    _model(hearing).detected = ("en", 0.95)
    transcript = hearing.transcribe(SECOND * 3, None, hint="fr")
    assert transcript.language == "en" and transcript.text == "heard in en"
    assert _model(hearing).asked == [None]


def test_an_unsure_detection_takes_the_hint_without_decoding_twice(
    hearing: hears.Hearing,
) -> None:
    _model(hearing).detected = ("en", 0.55)
    transcript = hearing.transcribe(SECOND * 3, None, hint="fr")
    assert transcript.language == "fr" and transcript.text == "heard in fr"
    assert _model(hearing).asked == [None, "fr"]
    assert _model(hearing).decoded == ["fr"]


def test_a_short_sound_takes_the_hint_even_when_detection_seems_clear(
    hearing: hears.Hearing,
) -> None:
    _model(hearing).detected = ("en", 0.95)
    transcript = hearing.transcribe(SECOND, None, hint="fr")
    assert transcript.language == "fr"


def test_a_detection_that_agrees_with_the_hint_is_heard_once(hearing: hears.Hearing) -> None:
    _model(hearing).detected = ("fr", 0.5)
    hearing.transcribe(SECOND, None, hint="fr")
    assert _model(hearing).asked == [None]
