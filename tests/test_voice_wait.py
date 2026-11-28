"""The voice trials wait for the speech service, and give up at once when it says it will not
hear in this start (no stack: the service's answers are made up)."""

from collections.abc import Iterator
from typing import Any, cast

import pytest

from tests.e2e import voice_support
from tests.e2e.voice_support import SPEECH_READY_S, wait_for_speech
from tests.stack.stack_support import StackInfo


class _Stack:
    """Stands for the stack: its speech service's output."""

    def logs(self, service: str) -> str:
        assert service == "speech"
        return "\n".join(f"line {n}" for n in range(100))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _answers(monkeypatch: pytest.MonkeyPatch, *healths: dict[str, Any]) -> _Clock:
    said: Iterator[dict[str, Any]] = iter(healths)
    last: list[dict[str, Any]] = [healths[-1]]

    def health(stack: object) -> dict[str, Any]:
        last[0] = next(said, last[0])
        return last[0]

    monkeypatch.setattr(voice_support, "speech_health", health)
    return _Clock()


def _wait(clock: _Clock) -> None:
    wait_for_speech(cast(StackInfo, _Stack()), sleep=clock.sleep, clock=clock)


def test_ready_with_a_use_missing_and_nothing_to_come_fails_at_once_with_its_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _answers(monkeypatch, {"status": "ready", "missing": ["hear"]})

    with pytest.raises(AssertionError) as raised:
        _wait(clock)

    assert clock.slept == []
    assert "will not hear and say" in str(raised.value)
    assert "'missing': ['hear']" in str(raised.value)
    assert "line 99" in str(raised.value) and "line 39" not in str(raised.value)


def test_a_use_missing_while_measured_again_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _answers(
        monkeypatch,
        {"status": "ready", "missing": ["hear"], "measuring": True},
        {"status": "ready", "missing": ["hear"], "retrying": True},
        {"status": "ready"},
    )

    _wait(clock)

    assert clock.slept == [5, 5]


def test_a_slow_hearing_still_measured_again_later_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _answers(monkeypatch, {"status": "ready", "slow": ["hear"], "retrying": True})

    _wait(clock)

    assert clock.slept == []


def test_a_service_that_never_gets_there_is_given_up_at_the_limit_with_its_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _answers(monkeypatch, {"status": "preparing"})

    with pytest.raises(AssertionError) as raised:
        _wait(clock)

    assert clock.now >= SPEECH_READY_S
    assert "line 99" in str(raised.value)
