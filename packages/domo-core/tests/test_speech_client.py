"""The client of the speech service: what it sends through the relay, how it reads the answers,
and what its errors never carry."""

import io
import os
import tempfile
import wave
from pathlib import Path

import httpx
import pytest
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.catalog import UNKNOWN_PRICES
from domo_core.llm.models import Provider
from domo_core.llm.router import Route
from domo_core.llm.speech.client import (
    Heard,
    PrintThresholds,
    ServiceHealth,
    SpeechClient,
    SpeechError,
)

RELAY = "http://gatekeeper.test/llm/prov_home"
SECRET_TEXT = "the quick brown secret sentence"
SECRET_SOUND = b"\x01\x02" * 8000


def route_for(task: str = "hear") -> Route:
    provider = Provider(
        id="prov_home", kind="openai_compatible", base_url="http://192.168.1.10:8000/v1"
    )
    return Route(
        provider=provider,
        model_id="a-model",
        level="small",
        prices=UNKNOWN_PRICES,
        task=task,  # type: ignore[arg-type]
    )


@pytest.fixture
def client(gk_client: GatekeeperClient) -> SpeechClient:
    return SpeechClient(gk_client, httpx.AsyncClient())


async def test_transcribe_posts_a_wav_and_reads_words(client, respx_mock):
    seen: dict[str, bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["auth"] = request.headers["authorization"].encode()
        return httpx.Response(
            200,
            json={
                "text": "hello there",
                "language": "en",
                "duration": 0.5,
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.2},
                    {"word": "there", "start": 0.25, "end": 0.5},
                ],
            },
        )

    respx_mock.post(f"{RELAY}/audio/transcriptions").mock(side_effect=answer)
    heard = await client.transcribe(route_for(), SECRET_SOUND, language="en")
    assert heard == Heard("hello there", "en", 0.5, (("hello", 0.0, 0.2), ("there", 0.25, 0.5)))
    # The sound went as a WAV file: a header, then the very samples.
    body = seen["body"]
    start = body.index(b"RIFF")
    with wave.open(io.BytesIO(body[start : start + 44 + len(SECRET_SOUND)]), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == SECRET_SOUND
    assert seen["auth"].startswith(b"Bearer ")


async def test_say_streams_chunks_as_they_come(client, respx_mock):
    async def chunks():
        yield b"\x00\x01"
        yield b"\x02\x03"

    respx_mock.post(f"{RELAY}/audio/speech").mock(
        return_value=httpx.Response(200, content=chunks())
    )
    got = [c async for c in client.say(route_for("say"), "hi", voice="v", language="en")]
    assert b"".join(got) == b"\x00\x01\x02\x03"
    sent = respx_mock.calls.last.request.content
    assert b'"input":"hi"' in sent.replace(b" ", b"") and b'"voice":"v"' in sent.replace(b" ", b"")


async def test_a_503_preparing_is_told_as_preparing(client, respx_mock):
    respx_mock.post(f"{RELAY}/audio/transcriptions").mock(
        return_value=httpx.Response(503, json={"error": "preparing"})
    )
    with pytest.raises(SpeechError) as raised:
        await client.transcribe(route_for(), SECRET_SOUND)
    assert raised.value.reason == "preparing"


async def test_a_timeout_and_a_refusal_are_told_apart(client, respx_mock):
    route = respx_mock.post(f"{RELAY}/audio/transcriptions")
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SpeechError) as raised:
        await client.transcribe(route_for(), SECRET_SOUND)
    assert raised.value.reason == "timeout"
    route.mock(return_value=httpx.Response(400, json={"error": "bad_upload"}))
    with pytest.raises(SpeechError) as raised:
        await client.transcribe(route_for(), SECRET_SOUND)
    assert raised.value.reason == "refused"
    route.mock(side_effect=httpx.ConnectError("nobody"))
    with pytest.raises(SpeechError) as raised:
        await client.transcribe(route_for(), SECRET_SOUND)
    assert raised.value.reason == "unreachable"


async def test_the_voice_print_is_read(client, respx_mock):
    respx_mock.post(f"{RELAY}/voice/print").mock(
        return_value=httpx.Response(
            200,
            json={
                "vector": [0.5, 0.25],
                "model": "m",
                "seconds": 1.0,
                "pitch_hz": 120.0,
                "pitch_range_hz": None,
                "rate": None,
            },
        )
    )
    printed = await client.voice_print(route_for("voice_print"), SECRET_SOUND)
    assert printed.vector == (0.5, 0.25) and printed.pitch_hz == 120.0
    assert printed.pitch_range_hz is None


async def test_the_health_of_a_service(client, respx_mock):
    provider = route_for().provider
    route = respx_mock.post(f"{RELAY}/audio/transcriptions")
    route.mock(return_value=httpx.Response(503, json={"error": "preparing"}))
    assert await client.health(provider, at_home=True) == ServiceHealth("preparing")
    route.mock(return_value=httpx.Response(400, json={"error": "bad_upload"}))
    assert await client.health(provider, at_home=True) == ServiceHealth("ready")
    route.mock(
        return_value=httpx.Response(503, json={"error": "failed", "reason": "nothing_holds"})
    )
    assert await client.health(provider, at_home=True) == ServiceHealth("failed", "nothing_holds")
    route.mock(side_effect=httpx.ConnectError("nobody"))
    assert await client.health(provider, at_home=True) == ServiceHealth("down")
    with pytest.raises(ValueError):
        await client.health(provider, at_home=False)


async def test_the_probe_reads_a_real_ready_service_as_ready(gk_client):
    """The very request of the probe, answered by the speech service itself (its fake engines)."""
    speech_app = pytest.importorskip("domo_speech.app")
    fakes = pytest.importorskip("domo_speech.fakes")
    app = speech_app.create_app(fakes.fake_engines())
    prefix = "/llm/prov_home"

    async def under_v1(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(prefix):
            scope = {**scope, "path": "/v1" + scope["path"][len(prefix) :], "raw_path": b""}
        await app(scope, receive, send)

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=under_v1))
    speech = SpeechClient(gk_client, http)

    assert await speech.health(route_for().provider, at_home=True) == ServiceHealth("ready")


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (503, {"error": "failed", "reason": "weights_unreachable"}, "failed"),
        (503, {"error": "engine_unavailable"}, "unavailable"),
        (503, {"error": "preparing"}, "preparing"),
        (404, {"error": "unknown_model"}, "no_model"),
        (500, {"error": "engine_failed"}, "refused"),
    ],
)
async def test_what_the_service_says_is_told_apart(client, respx_mock, status, body, reason):
    respx_mock.post(f"{RELAY}/audio/transcriptions").mock(
        return_value=httpx.Response(status, json=body)
    )
    respx_mock.post(f"{RELAY}/audio/speech").mock(return_value=httpx.Response(status, json=body))

    with pytest.raises(SpeechError) as heard:
        await client.transcribe(route_for(), SECRET_SOUND)
    with pytest.raises(SpeechError) as said:
        async for _ in client.say(route_for("say"), "hi", voice=None, language=None):
            pass

    assert heard.value.reason == said.value.reason == reason


async def test_print_thresholds_are_read_per_print_model(client, respx_mock):
    respx_mock.get(f"{RELAY}/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "h", "does": "hear"},
                    {
                        "id": "p",
                        "does": "voice_print",
                        "thresholds": {"print_low": 0.2, "same_voice": 0.4, "print_high": 0.7},
                    },
                    {
                        "id": "disordered",
                        "does": "voice_print",
                        "thresholds": {"print_low": 0.8, "same_voice": 0.4, "print_high": 0.7},
                    },
                    {"id": "none", "does": "voice_print"},
                ]
            },
        )
    )

    found = await client.print_thresholds(route_for().provider, at_home=True)

    assert found == {"p": PrintThresholds(0.2, 0.4, 0.7)}
    with pytest.raises(ValueError):
        await client.print_thresholds(route_for().provider, at_home=False)


async def test_no_sound_is_written_anywhere(client, respx_mock, tmp_path, monkeypatch):
    empty = tmp_path / "temp"
    empty.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(tempfile, "tempdir", str(empty))
    monkeypatch.setenv("TMPDIR", str(empty))
    respx_mock.post(f"{RELAY}/audio/transcriptions").mock(
        return_value=httpx.Response(
            200, json={"text": "t", "language": "en", "duration": 1.0, "words": []}
        )
    )
    respx_mock.post(f"{RELAY}/audio/speech").mock(
        return_value=httpx.Response(200, content=b"\x00\x00")
    )
    await client.transcribe(route_for(), SECRET_SOUND)
    _ = [c async for c in client.say(route_for("say"), SECRET_TEXT, voice=None, language=None)]
    assert os.listdir(empty) == [] and os.listdir(work) == []
    assert Path(tempfile.gettempdir()) == empty


async def test_the_error_never_carries_the_text_or_the_sound(client, respx_mock, caplog):
    caplog.set_level("DEBUG")
    respx_mock.post(f"{RELAY}/audio/transcriptions").mock(side_effect=httpx.ReadTimeout("slow"))
    respx_mock.post(f"{RELAY}/audio/speech").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(SpeechError) as heard:
        await client.transcribe(route_for(), SECRET_SOUND)
    with pytest.raises(SpeechError) as said:
        _ = [c async for c in client.say(route_for("say"), SECRET_TEXT, voice=None, language=None)]
    for error in (heard.value, said.value):
        shown = f"{error!s} {error!r} {error.__cause__!r} {error.__context__!r}"
        assert SECRET_TEXT not in shown and "Bearer" not in shown
        assert SECRET_SOUND.hex()[:16] not in shown and repr(SECRET_SOUND[:8]) not in shown
        assert error.__cause__ is None
    assert SECRET_TEXT not in caplog.text and "Bearer" not in caplog.text
    assert SECRET_SOUND.hex()[:16] not in caplog.text and repr(SECRET_SOUND[:8]) not in caplog.text


async def test_a_language_hint_goes_to_the_houses_speech_service_only(client, respx_mock):
    """The person's known language is a hint the house's own service understands
    (``language_hint``: heard in when its detection is not clear); another provider gets none,
    and detects as before."""
    from domo_core.llm.speech.home_key import HOME_SPEECH_ID

    sent: list[bytes] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json={"text": "oui", "language": "fr", "duration": 0.5})

    home = route_for()
    home = Route(
        provider=Provider(id=HOME_SPEECH_ID, kind="openai_compatible", base_url="http://speech/v1"),
        model_id=home.model_id,
        level=home.level,
        prices=home.prices,
        task=home.task,
    )
    respx_mock.post(f"http://gatekeeper.test/llm/{HOME_SPEECH_ID}/audio/transcriptions").mock(
        side_effect=answer
    )
    respx_mock.post(f"{RELAY}/audio/transcriptions").mock(side_effect=answer)
    await client.transcribe(home, SECRET_SOUND, hint="fr")
    await client.transcribe(route_for(), SECRET_SOUND, hint="fr")
    assert b'name="language_hint"' in sent[0] and b"\r\nfr\r\n" in sent[0]
    assert b"language_hint" not in sent[1] and b'name="language"' not in sent[1]
