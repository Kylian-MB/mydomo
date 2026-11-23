"""The internal API of the gatekeeper: what the core talks to, and nobody else.

It serves the secret requests, the secret metadata, the registration of provider routes and the
relay (``/llm/{provider_id}/...``) that the core's model SDKs call as if it were the provider.
The core authenticates with a shared token. The SDKs send it as their "API key", so the relay
routes also take it from ``x-api-key`` and ``x-goog-api-key``; every other route takes a bearer
token only. Everything but ``/internal/health`` is closed to a caller without the token: the
check is a middleware, so it covers unknown paths too and runs before any body is read.

Errors are small JSON bodies, ``{"error": "<code>"}``: they carry no secret, no header value and
no echo of the request, so the core can map them and nothing leaks through them. The requests of
programs (``/internal/out``, see ``out``) are refused the same way, as ``{"code": "<code>"}``.
"""

import hmac
import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, cast
from urllib.parse import unquote

import httpx
from domo_common.gatekeeper_api import (
    OutRefusalCode,
    OutRequest,
    ProviderRoute,
    SecretMetadata,
    SecretRequestCreate,
    SecretRequestView,
)
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from domo_gatekeeper.errors import (
    BindingMismatchError,
    InsecureTransportError,
    InvalidRelayPath,
    InvalidSecretValueError,
    OutRefused,
    UnknownProviderError,
    UpstreamError,
    UpstreamTimeoutError,
    VaultDecryptError,
)
from domo_gatekeeper.out import ProgramRelay
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.relay import relay
from domo_gatekeeper.secret_requests import SecretRequestStore
from domo_gatekeeper.vault import Vault

logger = logging.getLogger(__name__)

HEALTH_PATH = "/internal/health"
LLM_PREFIX = "/llm/"
# The largest request body the relay accepts (Anthropic's own limit is 32 MiB).
MAX_RELAY_BODY_BYTES = 64 * 1024 * 1024

_RELAY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
# The headers the SDKs may carry the core's token in, besides ``authorization``.
_SDK_KEY_HEADERS = (b"x-api-key", b"x-goog-api-key")
# What the relay can raise, most specific first: the API answer for each.
_RELAY_ERRORS: tuple[tuple[type[Exception], int, str], ...] = (
    (InvalidRelayPath, 400, "invalid_path"),
    (InsecureTransportError, 403, "insecure_transport"),
    (BindingMismatchError, 403, "binding_mismatch"),
    (UpstreamTimeoutError, 504, "upstream_timeout"),
    (UpstreamError, 502, "upstream_error"),
    (VaultDecryptError, 500, "vault_error"),
    (InvalidSecretValueError, 500, "secret_unusable"),
)

OUT_PATH = "/internal/out"
# The HTTP status of each refusal of a program's request (its body is ``{"code": ...}``).
_OUT_STATUSES: dict[OutRefusalCode, int] = {
    "closed_address": 403,
    "never_reachable": 403,
    "host_not_opened": 403,
    "needs_https": 403,
    "account_not_for_address": 403,
    "account_not_found": 404,
    "bad_redirect": 502,
    "too_big": 502,
    "too_slow": 504,
    "unreachable": 502,
    "bad_request": 400,
    "zone_changed": 403,
}

# RFC 9110 "token": what a header name is made of.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# What the HTTP stack can carry as a header value: printable ASCII, blanks included.
_HEADER_VALUE = re.compile(r"[\x20-\x7e\t]*")
# The server adds its own: a second one would be a protocol violation.
_NOT_RELAYED_BACK = frozenset({"date"})

# The ASGI types, spelled out: starlette is a dependency of fastapi, not one of ours.
_Message = MutableMapping[str, Any]
_Scope = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


class _ApiError(Exception):
    """An error with its HTTP status and its code, to be answered as ``{"error": code}``."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _error_response(status: int, code: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


class _CoreAuthentication:
    """Lets a request through only with the core's token; the health check is the exception.

    Pure ASGI, not ``BaseHTTPMiddleware``: relayed answers are streamed, and that wrapper would
    get between them and the client.
    """

    def __init__(self, app: _ASGIApp, *, token: str) -> None:
        self._app = app
        self._token = token.encode()

    def _authorised(self, scope: _Scope) -> bool:
        relay_route = scope["path"].startswith(LLM_PREFIX)
        candidates: list[bytes] = []
        for name, value in scope["headers"]:
            if name == b"authorization":
                scheme, _, credentials = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    candidates.append(credentials.lstrip(b" "))
            elif relay_route and name in _SDK_KEY_HEADERS:
                candidates.append(value)
        # Every candidate is compared, in constant time: no early exit tells which one matched.
        return any([hmac.compare_digest(candidate, self._token) for candidate in candidates])

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        # Only HTTP requests are guarded: the app has no other kind of route.
        if scope["type"] == "http" and scope["path"] != HEALTH_PATH and not self._authorised(scope):
            response = _error_response(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _caller_headers(request: Request) -> dict[str, str]:
    """The caller's headers, one value per (lower-cased) name, or a 400 for one that cannot go.

    A name that is not a token, or a value that is not printable ASCII, would fail deep inside
    the HTTP client (or worse, be accepted by it): the request is refused instead. Repeated
    headers are combined in the way RFC 9110 defines.
    """
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        if _HEADER_NAME.fullmatch(name) is None or _HEADER_VALUE.fullmatch(value) is None:
            raise _ApiError(400, "invalid_header")
        key = name.lower()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return headers


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > MAX_RELAY_BODY_BYTES:
        raise _ApiError(413, "body_too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_RELAY_BODY_BYTES:
            raise _ApiError(413, "body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _relay_path(request: Request, provider_id: str) -> str:
    """The path below the provider id, as the caller sent it: still percent-encoded.

    The relay refuses an encoded dot or separator, which a decoded path would turn into a real
    one, and the provider gets the characters the SDK meant to send.
    """
    raw = request.scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, bytes) else str(request.scope["path"])
    segment, _, rest = path.removeprefix(LLM_PREFIX).partition("/")
    if unquote(segment) != provider_id:
        raise UnknownProviderError(provider_id)
    return rest


def _relay_error(exc: Exception, route: ProviderRoute) -> _ApiError | None:
    """The API error for something ``relay`` raised, or None when the API does not know it."""
    for error_type, status, code in _RELAY_ERRORS:
        if isinstance(exc, error_type):
            return _ApiError(status, code)
    # The route names a secret that is not in the vault (the vault raises KeyError(ref)).
    if (
        isinstance(exc, KeyError)
        and route.secret_ref is not None
        and exc.args == (route.secret_ref,)
    ):
        return _ApiError(409, "secret_missing")
    return None


def _forwardable(name: str, value: str) -> bool:
    return (
        name.lower() not in _NOT_RELAYED_BACK
        and _HEADER_NAME.fullmatch(name) is not None
        and _HEADER_VALUE.fullmatch(value) is not None
    )


def _out_refusal(code: OutRefusalCode) -> Response:
    return JSONResponse({"code": code}, status_code=_OUT_STATUSES[code])


def create_internal_app(
    *,
    vault: Vault,
    requests: SecretRequestStore,
    providers: ProviderRegistry,
    http_client: httpx.AsyncClient,
    core_token: str,
    out_relay: ProgramRelay | None = None,
) -> FastAPI:
    """The internal app; ``out_relay`` serves the requests of programs (``OUT_PATH``), which
    the app does not have without it."""
    if not core_token:
        raise ValueError("The core token must not be empty.")
    # No API docs or schema: nothing here is for browsing.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(_CoreAuthentication, token=core_token)

    @app.exception_handler(_ApiError)
    async def api_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, _ApiError)
        return _error_response(exc.status, exc.code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: Exception) -> Response:
        # FastAPI's own answer quotes the offending input: say nothing about it.
        return _error_response(422, "invalid_request")

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Response:
        return _error_response(404, "not_found")

    @app.exception_handler(405)
    async def method_not_allowed(request: Request, exc: Exception) -> Response:
        # Starlette's error carries the ``Allow`` header that a 405 must have.
        headers = cast(dict[str, str] | None, getattr(exc, "headers", None))
        return _error_response(405, "method_not_allowed", headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Response:
        # Starlette re-raises the exception afterwards, for the server to log.
        return _error_response(500, "internal_error")

    @app.get(HEALTH_PATH)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/secret-requests", status_code=201, response_model=SecretRequestView)
    async def create_secret_request(body: SecretRequestCreate) -> SecretRequestView:
        try:
            return await run_in_threadpool(requests.create, body, vault)
        except BindingMismatchError:
            raise _ApiError(409, "binding_conflict") from None

    @app.get("/internal/secret-requests/{request_id}")
    async def get_secret_request(request_id: str) -> SecretRequestView:
        view = await run_in_threadpool(requests.get, request_id)
        if view is None:
            raise _ApiError(404, "not_found")
        return view

    @app.get("/internal/secrets")
    async def list_secrets() -> list[SecretMetadata]:
        return await run_in_threadpool(vault.list_metadata)

    @app.put("/internal/providers/{provider_id}", status_code=204)
    async def register_provider(provider_id: str, route: ProviderRoute) -> Response:
        if route.id != provider_id:
            raise _ApiError(400, "id_mismatch")
        try:
            await run_in_threadpool(providers.register, route, vault)
        except KeyError:
            raise _ApiError(404, "unknown_secret") from None
        except InsecureTransportError:
            raise _ApiError(409, "insecure_transport") from None
        except BindingMismatchError:
            raise _ApiError(409, "binding_conflict") from None
        return Response(status_code=204)

    @app.api_route(LLM_PREFIX + "{provider_id}/{path:path}", methods=_RELAY_METHODS)
    async def relay_to_provider(provider_id: str, request: Request) -> Response:
        route = await run_in_threadpool(providers.get, provider_id)
        if route is None:
            raise _ApiError(404, "unknown_provider")
        try:
            path = _relay_path(request, provider_id)
        except UnknownProviderError:
            raise _ApiError(404, "unknown_provider") from None
        headers = _caller_headers(request)
        body = await _read_body(request)
        try:
            result = await relay(
                route,
                method=request.method,
                path=path,
                query=request.scope["query_string"].decode("latin-1"),
                headers=headers,
                body=body,
                vault=vault,
                client=http_client,
            )
        except Exception as exc:
            error = _relay_error(exc, route)
            if error is None:
                raise
            # The provider and the code only: nothing of the request, and never a value.
            logger.warning("Relay to provider %s refused: %s", route.id, error.code)
            raise error from None
        response = StreamingResponse(result.body, status_code=result.status)
        for name, value in result.headers:
            if _forwardable(name, value):
                response.headers.append(name, value)
        # Releases the provider's connection if the body is not read to its end.
        released = BackgroundTasks()
        released.add_task(result.aclose)
        response.background = released
        return response

    if out_relay is not None:
        program_relay = out_relay

        @app.post(OUT_PATH)
        async def send_for_a_program(request: Request) -> Response:
            # Read and checked here, not by FastAPI: whatever is wrong with it is "bad_request".
            try:
                ask = OutRequest.model_validate_json(await _read_body(request))
            except (_ApiError, ValidationError):
                return _out_refusal("bad_request")
            try:
                answer = await program_relay.send(ask)
            except OutRefused as refused:
                return _out_refusal(refused.code)
            return JSONResponse(answer.model_dump(mode="json"))

    return app
