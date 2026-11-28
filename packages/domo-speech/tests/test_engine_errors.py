"""When an engine fails: a plain error code, never the text or the sound; and a voice that fails
after its first chunk never looks like a complete answer."""

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from domo_speech.app import create_app
from domo_speech.engines import (
    Engines,
    EngineUnavailable,
    ModelEntry,
    Transcript,
    UnknownModel,
    VoicePrint,
)
from domo_speech.fakes import fake_engines
from starlette.types import Message, Scope

SECRET_TEXT = "orchid lantern forty"
SECRET_SOUND = b"\x09\x08UNIQUE-SOUND\x08\x09"


class Broken(Exception):
    """Carries what must never leave: the text and the sound."""

    def __init__(self) -> None:
        super().__init__(f"{SECRET_TEXT} {SECRET_SOUND!r}")


class FailingHears:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def transcribe(
        self, pcm: bytes, *, language: str | None, model: str | None, hint: str | None = None
    ) -> Transcript:
        raise self.error


class FailingPrints:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def voice_print(self, pcm: bytes, *, model: str | None) -> VoicePrint:
        raise self.error


class FailingSays:
    def __init__(self, error: Exception, *, after: int) -> None:
        self.error = error
        self.after = after

    async def say(
        self, text: str, *, voice: str | None, language: str | None, model: str | None
    ) -> AsyncIterator[bytes]:
        for _ in range(self.after):
            yield b"\x00\x01" * 8
        raise self.error


def _engines(error: Exception, *, after: int = 0) -> Engines:
    return dataclasses.replace(
        fake_engines(),
        hears=FailingHears(error),
        prints=FailingPrints(error),
        says=FailingSays(error, after=after),
    )


async def _calls(engines: Engines) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        sound = {"file": ("a.pcm", b"text:" + SECRET_SOUND, "audio/pcm")}
        return [
            await http.post("/v1/audio/transcriptions", files=sound, data={"model": "m"}),
            await http.post("/v1/voice/print", files=sound, data={"model": "m"}),
            await http.post("/v1/audio/speech", json={"model": "m", "input": SECRET_TEXT}),
        ]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (Broken(), 500, "engine_failed"),
        (EngineUnavailable(), 503, "engine_unavailable"),
        (UnknownModel(), 404, "unknown_model"),
    ],
)
async def test_a_failing_engine_answers_a_plain_code(
    error: Exception, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    for response in await _calls(_engines(error)):
        assert (response.status_code, response.json()) == (status, {"error": code})

    logged = caplog.text + "".join(str(r.exc_info) for r in caplog.records)
    assert SECRET_TEXT not in logged
    assert "UNIQUE-SOUND" not in logged


async def _speak(engines: Engines) -> tuple[list[Message], BaseException | None]:
    """Calls the speech route as a server would, and gives what it sent and how it ended."""
    body = json.dumps({"model": "m", "input": SECRET_TEXT}).encode()
    requests = iter([{"type": "http.request", "body": body, "more_body": False}])
    sent: list[Message] = []

    async def receive() -> Message:
        if (message := next(requests, None)) is not None:
            return message
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/audio/speech",
        "raw_path": b"/v1/audio/speech",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"host", b"speech")],
    }
    try:
        await create_app(engines)(scope, receive, send)
    except Exception as error:
        return sent, error
    return sent, None


async def test_a_voice_failing_after_its_first_chunk_never_completes_the_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    sent, ended = await _speak(_engines(Broken(), after=2))

    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert bodies and all(m.get("more_body") for m in bodies)
    # The server is told to drop the connection: the caller sees a cut answer, not a whole one.
    assert ended is not None
    assert SECRET_TEXT not in str(ended) and "UNIQUE-SOUND" not in str(ended)
    assert ended.__cause__ is None and ended.__suppress_context__
    assert SECRET_TEXT not in caplog.text


async def test_a_voice_failing_before_any_sound_answers_500() -> None:
    sent, ended = await _speak(_engines(Broken(), after=0))

    assert ended is None
    assert sent[0]["status"] == 500
    assert json.loads(sent[1]["body"]) == {"error": "engine_failed"}


async def test_the_model_asked_for_reaches_the_engine() -> None:
    asked: list[str | None] = []

    class Recording:
        async def transcribe(
            self, pcm: bytes, *, language: str | None, model: str | None, hint: str | None = None
        ) -> Transcript:
            asked.append(model)
            return Transcript(text="", language="en", seconds=0.0, words=())

    engines = dataclasses.replace(fake_engines(), hears=Recording())
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        await http.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.pcm", b"x", "audio/pcm")},
            data={"model": "a-chosen-model"},
        )

    assert asked == ["a-chosen-model"]


async def test_models_and_health_say_what_the_engines_measured() -> None:
    engines = dataclasses.replace(
        fake_engines(),
        models=lambda: (
            ModelEntry(id="h", does="hear", measures={"device": "cpu", "real_time_factor": 0.2}),
        ),
        ready=lambda: "failed",
        health=lambda: {"reason": "nothing_holds", "missing": ["hear", "say", "voice_print"]},
    )
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        listed = (await http.get("/v1/models")).json()["data"]
        health = await http.get("/health")

    assert listed == [
        {
            "id": "h",
            "object": "model",
            "does": "hear",
            "at_once": 1,
            "measures": {"device": "cpu", "real_time_factor": 0.2},
        }
    ]
    assert health.status_code == 503
    assert health.json() == {
        "status": "failed",
        "reason": "nothing_holds",
        "missing": ["hear", "say", "voice_print"],
    }


async def test_an_empty_request_is_a_bad_upload_not_a_crash(client: httpx.AsyncClient) -> None:
    # What the core's health probe sends: no body, no content type.
    for path in ("/v1/audio/transcriptions", "/v1/voice/print"):
        response = await client.post(path)
        assert (response.status_code, response.json()) == (400, {"error": "bad_upload"})
    response = await client.post(
        "/v1/audio/transcriptions", content=b"x", headers={"content-type": "text/plain"}
    )
    assert (response.status_code, response.json()) == (400, {"error": "bad_upload"})


async def test_a_failed_service_says_failed_and_why_on_every_working_route() -> None:
    engines = dataclasses.replace(
        fake_engines(), ready=lambda: "failed", health=lambda: {"reason": "weights_unreachable"}
    )
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        sound = {"file": ("a.pcm", b"text:hi", "audio/pcm")}
        answers = [
            await http.post("/v1/audio/transcriptions", files=sound),
            await http.post("/v1/voice/print", files=sound),
            await http.post("/v1/audio/speech", json={"model": "m", "input": "hi"}),
        ]

    for answer in answers:
        assert (answer.status_code, answer.json()) == (
            503,
            {"error": "failed", "reason": "weights_unreachable"},
        )


async def test_models_carry_licence_and_print_thresholds() -> None:
    engines = dataclasses.replace(
        fake_engines(),
        models=lambda: (
            ModelEntry(
                id="p",
                does="voice_print",
                licence="CC-BY-4.0",
                licence_url="https://spdx.org/licenses/CC-BY-4.0.html",
                restricted=False,
                thresholds=(0.2, 0.4, 0.7),
            ),
        ),
    )
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        (listed,) = (await http.get("/v1/models")).json()["data"]

    assert listed["licence"] == {
        "id": "CC-BY-4.0",
        "url": "https://spdx.org/licenses/CC-BY-4.0.html",
        "restricted": False,
    }
    assert listed["thresholds"] == {"print_low": 0.2, "same_voice": 0.4, "print_high": 0.7}
