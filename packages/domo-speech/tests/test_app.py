import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
from domo_speech.app import create_app
from domo_speech.engines import Engines, Readiness
from domo_speech.wire import MAX_SOUND_BYTES, MAX_TEXT_CHARS
from starlette.types import Message, Scope


async def test_a_transcription_gives_text_language_and_word_times(
    client: httpx.AsyncClient, wav_of: Callable[..., bytes]
) -> None:
    sound = wav_of(b"text:turn the light on")

    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("utterance.wav", sound, "audio/wav")},
        data={
            "model": "fake-hear",
            "language": "fr",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "turn the light on"
    assert body["language"] == "fr"
    assert body["duration"] > 0
    assert [w["word"] for w in body["words"]] == ["turn", "the", "light", "on"]
    assert all(w["start"] < w["end"] for w in body["words"])


async def test_a_transcription_takes_raw_pcm_too(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("utterance.pcm", b"text:hello there", "application/octet-stream")},
        data={"model": "fake-hear"},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "hello there"


async def test_speech_streams_pcm_in_chunks(engines: Engines) -> None:
    text = "a sentence long enough to come out in several chunks"
    body = json.dumps({"model": "fake-say", "input": text, "response_format": "pcm"}).encode()
    sent: list[Message] = []

    requests = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive() -> Message:
        if (message := next(requests, None)) is not None:
            return message
        await asyncio.Event().wait()  # the client stays connected until the answer is complete
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

    await create_app(engines)(scope, receive, send)

    start = sent[0]
    assert start["status"] == 200
    assert (b"content-type", b"audio/pcm") in start["headers"]
    pieces = [m["body"] for m in sent[1:] if m["type"] == "http.response.body" and m["body"]]
    assert len(pieces) > 1
    assert b"".join(pieces) == text.encode()


async def test_a_print_gives_a_vector_and_the_intonation(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/voice/print",
        files={"file": ("utterance.pcm", b"\x80" + bytes(63_999), "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["vector"]) > 1
    assert body["model"] == "fake-print"
    assert body["seconds"] == 2.0
    assert body["pitch_hz"] is not None
    assert body["pitch_range_hz"] is not None
    assert body["rate"] is not None


async def test_models_lists_what_each_model_does(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")

    assert response.status_code == 200
    assert {(m["id"], m["does"]) for m in response.json()["data"]} == {
        ("fake-hear", "hear"),
        ("fake-say", "say"),
        ("fake-print", "voice_print"),
    }
    assert all(m["object"] == "model" for m in response.json()["data"])


async def test_health_says_preparing_then_ready(
    client: httpx.AsyncClient, set_readiness: Callable[[Readiness], None]
) -> None:
    set_readiness("preparing")

    health = await client.get("/health")
    heard = await client.post(
        "/v1/audio/transcriptions", files={"file": ("a.pcm", b"text:hi", "audio/pcm")}
    )
    said = await client.post("/v1/audio/speech", json={"model": "m", "input": "hi"})
    printed = await client.post("/v1/voice/print", files={"file": ("a.pcm", b"\x01", "audio/pcm")})

    assert (health.status_code, health.json()) == (503, {"status": "preparing"})
    for blocked in (heard, said, printed):
        assert (blocked.status_code, blocked.json()) == (503, {"error": "preparing"})

    set_readiness("ready")
    health = await client.get("/health")
    assert (health.status_code, health.json()) == (200, {"status": "ready"})

    set_readiness("failed")
    health = await client.get("/health")
    assert (health.status_code, health.json()) == (503, {"status": "failed"})


async def test_sound_over_the_bound_is_refused_413(client: httpx.AsyncClient) -> None:
    too_big = bytes(MAX_SOUND_BYTES + 1)

    for route in ("/v1/audio/transcriptions", "/v1/voice/print"):
        response = await client.post(route, files={"file": ("a.pcm", too_big, "audio/pcm")})
        assert response.status_code == 413


async def test_text_over_the_bound_is_refused_413(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/audio/speech", json={"model": "m", "input": "a" * (MAX_TEXT_CHARS + 1)}
    )

    assert response.status_code == 413


async def test_sound_that_is_not_16k_mono_is_refused_400(
    client: httpx.AsyncClient, wav_of: Callable[..., bytes]
) -> None:
    wrong = {
        "8 kHz": wav_of(bytes(1000), rate=8_000),
        "stereo": wav_of(bytes(1000), channels=2),
    }

    for sound in wrong.values():
        for route in ("/v1/audio/transcriptions", "/v1/voice/print"):
            response = await client.post(route, files={"file": ("a.wav", sound, "audio/wav")})
            assert response.status_code == 400


async def test_a_body_far_over_the_bound_is_cut_off_early(client: httpx.AsyncClient) -> None:
    read = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal read
        yield (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.pcm"\r\n'
            b"Content-Type: audio/pcm\r\n\r\n"
        )
        for _ in range(1000):  # 1000 MB if it were all read
            read += 1
            yield bytes(1024 * 1024)

    response = await client.post(
        "/v1/audio/transcriptions",
        content=endless(),
        headers={"content-type": "multipart/form-data; boundary=b"},
    )

    assert response.status_code == 413
    assert read < 40


async def test_a_declared_length_over_the_bound_is_refused_before_reading(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/voice/print",
        content=b"x",
        headers={
            "content-type": "multipart/form-data; boundary=b",
            "content-length": str(MAX_SOUND_BYTES * 2),
        },
    )

    assert response.status_code == 413


async def test_a_speech_body_far_over_the_bound_is_cut_off(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/audio/speech",
        content=b'{"model":"m","input":"' + b"a" * (5 * 1024 * 1024) + b'"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413


# A caller that hangs up leaves the queue (final fix wave B2)


class _Held:
    """Hearing and prints that wait until cancelled, and say whether they were."""

    def __init__(self) -> None:
        self.began = asyncio.Event()
        self.cancelled: list[str] = []

    async def _wait(self, name: str) -> None:
        self.began.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise

    async def transcribe(self, pcm: bytes, *, language: str | None, model: str | None = None):
        await self._wait("hear")

    async def voice_print(self, pcm: bytes, *, model: str | None = None):
        await self._wait("print")


async def _hang_up_during(app_engines: Engines, path: str, held: _Held) -> list[Message]:
    built = httpx.Request(
        "POST", f"http://speech{path}", files={"file": ("u.pcm", b"text:hello", "audio/pcm")}
    )
    body = built.read()
    messages = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive() -> Message:
        if (message := next(messages, None)) is not None:
            return message
        await held.began.wait()  # the caller hangs up once the work waits
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in built.headers.items()],
    }
    await asyncio.wait_for(create_app(app_engines)(scope, receive, send), 5)
    return sent


async def test_a_hearing_whose_caller_hung_up_is_given_up(engines: Engines) -> None:
    import dataclasses

    held = _Held()
    sent = await _hang_up_during(
        dataclasses.replace(engines, hears=held), "/v1/audio/transcriptions", held
    )
    assert held.cancelled == ["hear"]
    assert sent[0]["status"] == 499


async def test_a_print_whose_caller_hung_up_is_given_up(engines: Engines) -> None:
    import dataclasses

    held = _Held()
    sent = await _hang_up_during(dataclasses.replace(engines, prints=held), "/v1/voice/print", held)
    assert held.cancelled == ["print"]
    assert sent[0]["status"] == 499


async def test_a_caller_still_there_gets_the_answer(engines: Engines) -> None:
    # The watch for a hang-up never answers in place of the work.
    transport = httpx.ASGITransport(app=create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("u.pcm", b"text:hello there", "audio/pcm")},
        )
    assert (response.status_code, response.json()["text"]) == (200, "hello there")
