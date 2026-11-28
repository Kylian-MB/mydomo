"""The hearing model is told the language to hear in when the caller knows it (the owner,
2026-11-28, "donner la langue connue"), and guesses when the language is unknown to it."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from domo_speech.real import hears


class _Model:
    supported_languages = ("en", "fr", "nl")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.asked: list[str | None] = []

    def transcribe(self, audio: Any, *, language: str | None, **kwargs: Any) -> Any:
        self.asked.append(language)
        part = SimpleNamespace(text=" Bonjour.", words=[])
        return iter([part]), SimpleNamespace(language=language or "en")


@pytest.fixture
def hearing(monkeypatch: pytest.MonkeyPatch) -> hears.Hearing:
    monkeypatch.setattr(hears, "_faster_whisper", lambda: SimpleNamespace(WhisperModel=_Model))
    return hears.Hearing(Path("weights"), gpu=False)


def _asked(hearing: hears.Hearing) -> list[str | None]:
    return hearing._model.asked  # pyright: ignore[reportPrivateUsage]


def test_a_known_language_is_given_to_the_model(hearing: hears.Hearing) -> None:
    transcript = hearing.transcribe(b"\x00\x00" * 16_000, "fr")
    assert _asked(hearing) == ["fr"]
    assert transcript.language == "fr"


def test_a_language_the_model_does_not_know_lets_it_guess(hearing: hears.Hearing) -> None:
    hearing.transcribe(b"\x00\x00" * 16_000, "xx")
    hearing.transcribe(b"\x00\x00" * 16_000, None)
    assert _asked(hearing) == [None, None]
