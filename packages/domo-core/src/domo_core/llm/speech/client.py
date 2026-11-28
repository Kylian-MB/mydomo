"""The client of a speech service, reached through the gatekeeper's relay like any model.

Plain ``httpx``, no SDK. The core's token goes in ``Authorization``; the gatekeeper puts the key of
the route in its place, when it has one. Sound is wrapped in a WAV header in memory: nothing is
written to disk. An error says why and nothing else (``SpeechError.reason``): never the text, the
sound, a header or the request, which holds the token.
"""

import io
import json
import logging
import wave
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx

from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.models import Provider
from domo_core.llm.router import Route
from domo_core.llm.speech.home_key import HOME_SPEECH_ID

logger = logging.getLogger(__name__)

# The speech service wants 16 kHz mono 16-bit sound.
SAMPLE_RATE: Final = 16_000
BYTES_PER_SECOND: Final = SAMPLE_RATE * 2
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

Reason = Literal[
    "preparing",
    "failed",
    "unavailable",
    "unreachable",
    "timeout",
    "refused",
    "limit_reached",
    "no_model",
]
HealthState = Literal["ready", "preparing", "failed", "down"]


@dataclass(frozen=True)
class ServiceHealth:
    """Whether a speech service is ``ready``, still ``preparing`` its models, ``failed`` (with
    the plain reason it gives: ``nothing_holds``, ``weights_unreachable``...) or ``down``."""

    state: HealthState
    reason: str | None = None


@dataclass(frozen=True)
class PrintThresholds:
    """The cosine similarities a print model's scale maps to: degree 0, the same voice, degree 1."""

    print_low: float
    same_voice: float
    print_high: float


class SpeechError(Exception):
    """A call of sound failed. ``reason`` says what the caller can do about it: ``preparing`` (the
    service is still getting its models ready: later), ``failed`` (nothing holds on its machine,
    or its weights cannot be fetched: it says so on its health), ``unavailable`` (the model
    cannot be loaded now), ``unreachable``, ``timeout``, ``refused`` (the service or the relay
    turned the request down), ``limit_reached`` (a firm spending limit: the call was not made) or
    ``no_model`` (no model, or one the service does not list). The message is the reason and
    nothing else."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True)
class Heard:
    """What was heard: the text, its language, the seconds of sound, and each word with its start
    and end in seconds."""

    text: str
    language: str
    seconds: float
    words: tuple[tuple[str, float, float], ...]


@dataclass(frozen=True)
class Print:
    """The print of a voice: a vector, the model that made it, the seconds it was made from, and
    the measures of the voice when the service gave them."""

    vector: tuple[float, ...]
    model: str
    seconds: float
    pitch_hz: float | None
    pitch_range_hz: float | None
    rate: float | None


def wav_of(pcm: bytes) -> bytes:
    """``pcm`` (16 kHz mono s16le) in a WAV file, in memory."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return out.getvalue()


def _error_of(body: bytes) -> tuple[str, str | None]:
    """The service's plain ``error`` and ``reason`` codes, empty when the body has none."""
    try:
        found = cast(object, json.loads(body))
    except ValueError:
        return "", None
    if not isinstance(found, dict):
        return "", None
    fields = cast(dict[str, object], found)
    error, reason = fields.get("error"), fields.get("reason")
    return (error if isinstance(error, str) else ""), (reason if isinstance(reason, str) else None)


def _reason_of(status: int, body: bytes) -> Reason:
    error, _ = _error_of(body)
    if status == 404 and error == "unknown_model":
        return "no_model"
    if status != 503:
        return "refused"
    if error == "failed":
        return "failed"
    if error == "engine_unavailable":
        return "unavailable"
    return "preparing"


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("not a number")
    return float(value)


def _maybe_number(value: object) -> float | None:
    return None if value is None else _number(value)


class SpeechClient:
    """Hears, says and prints voices through the relay of a route's key."""

    def __init__(self, gatekeeper: GatekeeperClient, http: httpx.AsyncClient) -> None:
        self._gatekeeper = gatekeeper
        self._http = http

    def _url(self, provider_id: str, path: str) -> str:
        return f"{self._gatekeeper.relay_base_url(provider_id)}/{path}"

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._gatekeeper.token}"}

    async def _send(
        self,
        provider_id: str,
        path: str,
        *,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, str],
    ) -> dict[str, object]:
        """The JSON object a ``POST`` answers; ``SpeechError`` otherwise. The error is raised
        outside the handler: the HTTP exceptions hold the request, and so the token and the
        sound."""
        reason: Reason | None = None
        body: object = None
        try:
            response = await self._http.post(
                self._url(provider_id, path),
                files=files,
                data=data,
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            if response.status_code == 200:
                body = cast(object, response.json())
            else:
                reason = _reason_of(response.status_code, response.content)
        except httpx.TimeoutException:
            reason = "timeout"
        except httpx.HTTPError:
            reason = "unreachable"
        except ValueError:
            reason = "refused"
        if reason is None and not isinstance(body, dict):
            reason = "refused"
        if reason is not None:
            logger.warning("A call of sound failed (%s)", reason)
            raise SpeechError(reason)
        return cast(dict[str, object], body)

    async def transcribe(
        self, route: Route, pcm: bytes, *, language: str | None = None, hint: str | None = None
    ) -> Heard:
        """The words in ``pcm``, as the model of ``route`` hears them: in ``language`` when
        given; ``hint`` (the speaker's known language) goes to the house's own speech service
        only (``language_hint``, heard in when its detection is not clear), never to another
        provider, which detects as before."""
        data = {"model": route.model_id, **({"language": language} if language else {})}
        if hint and route.provider.id == HOME_SPEECH_ID:
            data["language_hint"] = hint
        fields = await self._send(
            route.provider.id,
            "audio/transcriptions",
            files={"file": ("sound.wav", wav_of(pcm), "audio/wav")},
            data=data,
        )
        try:
            words = tuple(
                (str(w["word"]), _number(w["start"]), _number(w["end"]))
                for w in cast(list[dict[str, object]], fields.get("words", []))
            )
            return Heard(
                text=str(fields["text"]),
                language=str(fields.get("language", "")),
                seconds=_number(fields["duration"]),
                words=words,
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("A call of sound failed (refused)")
            raise SpeechError("refused") from None

    async def voice_print(self, route: Route, pcm: bytes) -> Print:
        """The print of the voice in ``pcm``."""
        fields = await self._send(
            route.provider.id,
            "voice/print",
            files={"file": ("sound.wav", wav_of(pcm), "audio/wav")},
            data={"model": route.model_id},
        )
        try:
            return Print(
                vector=tuple(_number(x) for x in cast(list[object], fields["vector"])),
                model=str(fields["model"]),
                seconds=_number(fields["seconds"]),
                pitch_hz=_maybe_number(fields.get("pitch_hz")),
                pitch_range_hz=_maybe_number(fields.get("pitch_range_hz")),
                rate=_maybe_number(fields.get("rate")),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("A call of sound failed (refused)")
            raise SpeechError("refused") from None

    async def say(
        self, route: Route, text: str, *, voice: str | None, language: str | None
    ) -> AsyncGenerator[bytes]:
        """The sound of ``text`` (24 000 Hz mono s16le), in the chunks the service sends them."""
        request: dict[str, object] = {
            "model": route.model_id,
            "input": text,
            "response_format": "pcm",
        }
        if voice:
            request["voice"] = voice
        if language:
            request["language"] = language
        reason: Reason | None = None
        try:
            async with self._http.stream(
                "POST",
                self._url(route.provider.id, "audio/speech"),
                json=request,
                headers=self._headers(),
                timeout=_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    reason = _reason_of(response.status_code, await response.aread())
                else:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.TimeoutException:
            reason = "timeout"
        except httpx.HTTPError:
            reason = "unreachable"
        if reason is not None:
            logger.warning("A call of sound failed (%s)", reason)
            raise SpeechError(reason)

    async def health(self, provider: Provider, *, at_home: bool) -> ServiceHealth:
        """Whether the service of ``provider`` is ready, still preparing its models, failed (with
        its reason) or down. The relay only reaches what is below the service's ``/v1``, so this
        asks the transcription route for nothing: a service not ready answers 503 before it reads
        anything (``preparing``, or ``failed`` and why), a ready one turns the empty request down,
        and no sound is heard. The request is not counted, so it only goes to a server of the
        house (``at_home``, what ``speech.at_home`` says of it): ``ValueError`` for any other,
        which would be an uncounted request that may carry a paid key."""
        if not at_home:
            raise ValueError("the health of a speech service is only asked of a server at home")
        try:
            response = await self._http.post(
                self._url(provider.id, "audio/transcriptions"),
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError:
            return ServiceHealth("down")
        if response.status_code == 503:
            error, reason = _error_of(response.content)
            if error == "failed":
                return ServiceHealth("failed", reason)
            return ServiceHealth("preparing")
        if response.status_code in (200, 400, 413, 422):
            return ServiceHealth("ready")
        return ServiceHealth("down")

    async def print_thresholds(
        self, provider: Provider, *, at_home: bool
    ) -> dict[str, PrintThresholds]:
        """The thresholds of each print model the service of ``provider`` lists (its data is
        the one source), by model id; a model whose thresholds are missing or out of order is
        left out. Like ``health``, an uncounted request: only to a server of the house."""
        if not at_home:
            raise ValueError("the models of a speech service are only asked of a server at home")
        try:
            response = await self._http.get(
                self._url(provider.id, "models"), headers=self._headers(), timeout=_TIMEOUT
            )
            listed: object = cast(object, response.json()) if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return {}
        found: dict[str, PrintThresholds] = {}
        rows = cast(dict[str, object], listed).get("data") if isinstance(listed, dict) else None
        for row in cast(list[object], rows) if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, object], row)
            given = item.get("thresholds")
            if item.get("does") != "voice_print" or not isinstance(given, dict):
                continue
            values = cast(dict[str, object], given)
            try:
                low, same, high = (
                    _number(values[key]) for key in ("print_low", "same_voice", "print_high")
                )
            except (KeyError, ValueError):
                continue
            if 0 <= low < same < high <= 1:
                found[str(item.get("id"))] = PrintThresholds(low, same, high)
        return found
