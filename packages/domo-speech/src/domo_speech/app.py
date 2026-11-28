"""The speech service's five routes. Nothing is kept: sound and heard text stay in memory for the
time of one request, and no route logs them.

A caller that hangs up before its answer is not served for nobody: the work it waits for is given
up (``_while_there``), which leaves its turn in the queue of its model; work already begun on the
model ends and its result is dropped."""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from contextlib import suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from domo_speech.engines import Engines, EngineUnavailable, UnknownModel
from domo_speech.wire import MAX_SOUND_BYTES, MAX_TEXT_CHARS, pcm_of

logger = logging.getLogger(__name__)


class SpeechRequest(BaseModel):
    model: str = ""
    input: str
    voice: str | None = None
    language: str | None = None
    response_format: str = "pcm"


# What a multipart body may hold besides the sound (its small fields and the part headers).
_FORM_OVERHEAD = 64 * 1024
# The JSON body of the speech route: the longest text, every character escaped, plus the fields.
_MAX_JSON_BYTES = MAX_TEXT_CHARS * 6 + 4096


class _TooBig(Exception):
    pass


class _InMemoryParser(MultiPartParser):
    """Starlette's multipart parser, with a spool that never rolls over to a temporary file:
    sound lives in memory only (the body is bounded before it gets here)."""

    spool_max_size = MAX_SOUND_BYTES + _FORM_OVERHEAD


def _refusal(status: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


class SpeechCutOff(Exception):
    """A voice failed after its first chunk was sent: raised to the server so that it drops the
    connection, and the caller sees a cut answer instead of a complete one. Its message is fixed
    and it carries no cause (the engine's error may hold the text)."""

    def __init__(self) -> None:
        super().__init__("the voice failed after its first chunk")


def _engine_refusal(error: Exception) -> JSONResponse:
    """The plain code for an engine's failure; only the kind of error is logged, never its
    message (it may hold the text or the sound)."""
    if isinstance(error, UnknownModel):
        return _refusal(404, "unknown_model")
    if isinstance(error, EngineUnavailable):
        return _refusal(503, "engine_unavailable")
    logger.warning("An engine failed (%s)", type(error).__name__)
    return _refusal(500, "engine_failed")


async def _answer[T](call: Awaitable[T]) -> T | JSONResponse:
    try:
        return await call
    except Exception as error:
        return _engine_refusal(error)


async def _hung_up(request: Request) -> None:
    """Returns when the caller hangs up (its body already read: only that can come)."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _while_there[T](request: Request, call: Awaitable[T]) -> T | JSONResponse:
    """``_answer(call)``, given up as soon as the caller hangs up (see the module docstring): a
    request still waiting for its turn leaves the queue then, before any work."""
    work = asyncio.ensure_future(_answer(call))
    gone = asyncio.ensure_future(_hung_up(request))
    try:
        await asyncio.wait((work, gone), return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        work.cancel()
        raise
    finally:
        gone.cancel()
    if work.done():
        return work.result()
    work.cancel()
    with suppress(asyncio.CancelledError):
        await work
    logger.info("A caller hung up before its answer")
    return _refusal(499, "caller_gone")


async def _rest(first: bytes, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    try:
        async for chunk in chunks:
            yield chunk
    except Exception as error:
        logger.warning("A voice failed after its first chunk (%s)", type(error).__name__)
        raise SpeechCutOff from None


async def _bounded(request: Request, limit: int) -> AsyncGenerator[bytes, None]:
    """The body of the request, cut off (``_TooBig``) as soon as it passes ``limit`` bytes: a
    declared length over the bound is refused before reading anything."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _TooBig
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _TooBig
        yield chunk


def _field(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return (value or None) if isinstance(value, str) else None


async def _read_sound(
    request: Request,
) -> tuple[bytes, str | None, str | None] | JSONResponse:
    """The PCM, the language and the model asked for in a multipart request, or the refusal to
    answer with. A request that is not multipart (an empty one, for one: the core's health probe)
    is a bad upload.

    The body is read as a stream and parsed in memory: nothing is written to disk and nothing
    past the bound is read."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return _refusal(400, "bad_upload")
    parser = _InMemoryParser(request.headers, _bounded(request, MAX_SOUND_BYTES + _FORM_OVERHEAD))
    try:
        form = await parser.parse()
    except _TooBig:
        return _refusal(413, "sound_too_big")
    except (MultiPartException, ValueError, AssertionError, KeyError):
        return _refusal(400, "bad_upload")
    file = form.get("file")
    if not isinstance(file, UploadFile):
        return _refusal(400, "bad_upload")
    upload = await file.read(MAX_SOUND_BYTES + 1)
    if len(upload) > MAX_SOUND_BYTES:
        return _refusal(413, "sound_too_big")
    try:
        pcm = pcm_of(upload)
    except ValueError:
        return _refusal(400, "sound_not_16k_mono")
    return pcm, _field(form, "language"), _field(form, "model")


def create_app(engines: Engines) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def not_ready() -> JSONResponse | None:
        state = engines.ready()
        if state == "ready":
            return None
        if state == "preparing":
            return _refusal(503, "preparing")
        body: dict[str, object] = {"error": "failed"}
        reason = engines.health().get("reason")
        if isinstance(reason, str):
            body["reason"] = reason
        return JSONResponse(body, status_code=503)

    @app.get("/health")
    async def health() -> JSONResponse:
        state = engines.ready()
        return JSONResponse(
            {"status": state, **engines.health()}, status_code=200 if state == "ready" else 503
        )

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        listed: list[dict[str, object]] = []
        for entry in engines.models():
            item: dict[str, object] = {
                "id": entry.id,
                "object": "model",
                "does": entry.does,
                "at_once": entry.at_once,
            }
            if entry.slow:
                item["slow"] = True
            if entry.measures:
                item["measures"] = dict(entry.measures)
            if entry.licence:
                item["licence"] = {
                    "id": entry.licence,
                    "url": entry.licence_url,
                    "restricted": entry.restricted,
                }
            if entry.thresholds is not None:
                low, same, high = entry.thresholds
                item["thresholds"] = {"print_low": low, "same_voice": same, "print_high": high}
            listed.append(item)
        return {"data": listed}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request) -> Response:
        if (waiting := not_ready()) is not None:
            return waiting
        heard = await _read_sound(request)
        if isinstance(heard, JSONResponse):
            return heard
        sound, language, model = heard
        transcript = await _while_there(
            request, engines.hears.transcribe(sound, language=language, model=model)
        )
        if isinstance(transcript, JSONResponse):
            return transcript
        return JSONResponse(
            {
                "text": transcript.text,
                "language": transcript.language,
                "duration": transcript.seconds,
                "words": [
                    {"word": w.text, "start": w.start, "end": w.end} for w in transcript.words
                ],
            }
        )

    @app.post("/v1/audio/speech")
    async def speech(http_request: Request) -> Response:
        if (waiting := not_ready()) is not None:
            return waiting
        try:
            body = b"".join([chunk async for chunk in _bounded(http_request, _MAX_JSON_BYTES)])
        except _TooBig:
            return _refusal(413, "text_too_long")
        try:
            request = SpeechRequest.model_validate_json(body)
        except ValidationError:
            return _refusal(422, "bad_request")
        if len(request.input) > MAX_TEXT_CHARS:
            return _refusal(413, "text_too_long")
        chunks = engines.says.say(
            request.input, voice=request.voice, language=request.language, model=request.model
        )
        # The first chunk is waited for here: a voice that fails before any sound answers a
        # plain error; one that fails later cuts the answer (``SpeechCutOff``).
        first = await _while_there(http_request, anext(chunks, b""))
        if isinstance(first, JSONResponse):
            return first
        return StreamingResponse(_rest(first, chunks), media_type="audio/pcm")

    @app.post("/v1/voice/print")
    async def voice_print(request: Request) -> Response:
        if (waiting := not_ready()) is not None:
            return waiting
        heard = await _read_sound(request)
        if isinstance(heard, JSONResponse):
            return heard
        sound, _, model = heard
        result = await _while_there(request, engines.prints.voice_print(sound, model=model))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse(
            {
                "vector": list(result.vector),
                "model": result.model,
                "seconds": result.seconds,
                "pitch_hz": result.pitch_hz,
                "pitch_range_hz": result.pitch_range_hz,
                "rate": result.rate,
            }
        )

    return app
