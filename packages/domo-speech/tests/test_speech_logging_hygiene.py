"""What is heard is not logged, and nothing is written to disk.

Covered: the three working routes with the root logger at DEBUG (what the fake engines hear and
say, and the bytes of the upload, must appear in no record), the access log of the real server's
configuration, and the working directory during a request.
"""

import logging
import tempfile
from pathlib import Path

import httpx
import pytest
from domo_speech.__main__ import server_config
from domo_speech.config import SpeechSettings
from domo_speech.engines import Engines

HEARD = "ripple banana seven"
TO_SAY = "violet tunnel nine"
BYTE_RUN = b"\x07\xc3UNIQUE-RUN\xc3\x07"


async def _use_every_route(client: httpx.AsyncClient) -> None:
    heard = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("a.pcm", b"text:" + HEARD.encode() + BYTE_RUN, "audio/pcm")},
    )
    said = await client.post("/v1/audio/speech", json={"model": "m", "input": TO_SAY})
    printed = await client.post("/v1/voice/print", files={"file": ("a.pcm", BYTE_RUN, "audio/pcm")})
    assert (heard.status_code, said.status_code, printed.status_code) == (200, 200, 200)
    assert HEARD in heard.text
    assert said.content == TO_SAY.encode()


async def test_no_heard_text_and_no_sound_reaches_the_logs(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture, engines: Engines
) -> None:
    caplog.set_level(logging.DEBUG)

    await _use_every_route(client)

    logged = "\n".join(record.getMessage() for record in caplog.records) + caplog.text
    for secret in (HEARD, TO_SAY, "UNIQUE-RUN", repr(BYTE_RUN), str(BYTE_RUN)):
        assert secret not in logged
    # The server's own configuration writes no access log (a line per request).
    assert server_config(SpeechSettings(), engines).access_log is False


async def test_nothing_is_written_under_the_working_directory(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    await _use_every_route(client)

    assert list(tmp_path.iterdir()) == []


async def test_a_long_utterance_never_touches_the_temporary_directory(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    long_sound = b"text:" + bytes(3 * 1024 * 1024)  # about 98 s at 16 kHz, over the 1 MB spool

    for route in ("/v1/audio/transcriptions", "/v1/voice/print"):
        response = await client.post(route, files={"file": ("a.pcm", long_sound, "audio/pcm")})
        assert response.status_code == 200

    assert list(tmp_path.iterdir()) == []
