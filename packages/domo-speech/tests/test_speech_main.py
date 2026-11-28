"""The command says plainly when the real engines' runtimes are not installed."""

import importlib.util
from pathlib import Path

import pytest
from domo_speech import __main__ as command
from domo_speech.config import SpeechSettings


def test_without_the_runtimes_the_command_exits_with_one_plain_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_find_spec = importlib.util.find_spec

    def missing(name: str, *args: object) -> object:
        return None if name == "ctranslate2" else real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", missing)

    with pytest.raises(SystemExit) as stopped:
        command.build_engines(SpeechSettings(engines="real", model_dir=tmp_path))

    assert "models" in str(stopped.value.code)
