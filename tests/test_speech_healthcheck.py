"""The speech container's health check: healthy when ready, or preparing with no failure; a
failure of any reason, or no answer, is not. Run against real bodies of ``/health``."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "packages" / "domo-speech" / "healthcheck.py"


def load():
    spec = importlib.util.spec_from_file_location("speech_healthcheck", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("code", "body", "healthy"),
    [
        (200, {"status": "ready"}, True),
        (200, {"status": "ready", "missing": ["say"]}, True),
        (503, {"status": "preparing"}, True),
        (503, {"status": "preparing", "measuring": True}, True),
        (503, {"status": "failed", "reason": "weights_unreachable", "retrying": True}, False),
        (503, {"status": "failed", "reason": "nothing_holds"}, False),
        (503, {"status": "failed", "reason": "measure_failed"}, False),
        (503, {"status": "failed"}, False),
        (503, {"status": "preparing", "reason": "weights_unreachable", "retrying": True}, False),
        (500, {"error": "internal"}, False),
        (503, None, False),
    ],
)
def test_the_health_check_reads_the_body_of_health(code, body, healthy):
    assert load().is_healthy(code, body) is healthy


def test_no_answer_is_not_healthy():
    # Nothing listens on this port: the check fails rather than raises.
    assert load().check("http://127.0.0.1:9/health") is False


def test_the_check_asks_the_port_the_service_listens_on(monkeypatch):
    monkeypatch.delenv("DOMO_SPEECH_PORT", raising=False)
    assert load().health_url() == "http://127.0.0.1:8083/health"
    monkeypatch.setenv("DOMO_SPEECH_PORT", "9123")
    assert load().health_url() == "http://127.0.0.1:9123/health"
