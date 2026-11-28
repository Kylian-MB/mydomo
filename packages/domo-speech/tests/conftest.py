import dataclasses
import io
import wave
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from domo_speech.app import create_app
from domo_speech.engines import Engines, Readiness
from domo_speech.fakes import fake_engines


def _wav_of(pcm: bytes, *, rate: int = 16_000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


@pytest.fixture
def wav_of() -> Callable[..., bytes]:
    """Builds a WAV file around PCM samples: ``wav_of(pcm, rate=16000, channels=1)``."""
    return _wav_of


@pytest.fixture
def set_readiness() -> Callable[[Readiness], None]:
    """Changes what the engines say about their readiness, while the app runs."""
    return _state.set


class _State:
    def __init__(self) -> None:
        self.value: Readiness = "ready"

    def set(self, value: Readiness) -> None:
        self.value = value

    def __call__(self) -> Readiness:
        return self.value


_state = _State()


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    _state.set("ready")


@pytest.fixture
def engines() -> Engines:
    return dataclasses.replace(fake_engines(), ready=_state)


@pytest.fixture
async def client(engines: Engines) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        yield http
