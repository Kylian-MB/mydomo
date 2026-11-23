"""The core's side of the gatekeeper: its internal API, and the address the model SDKs call.

The core never holds a provider's key. It asks the gatekeeper for secrets
(``create_secret_request``), tells it which providers exist (``register_provider``) and sends its
model calls to the gatekeeper's relay (``relay_base_url``), authenticating with a token it shares
with the gatekeeper. The SDKs are given that token as their "API key"; the gatekeeper swaps it for
the real one.

The token is read once, at start-up, from a file (``load_core_token``). It is never in a repr, a log
line or an error message, and an HTTP library's exception, which carries the request and so the
token, is never chained to the errors raised here.

Every failed call is logged (WARNING) where its ``GatekeeperError`` is raised, so that no caller
can forget to: the setup's model step, the steps that put a model key to use
(``llm.provider_keys``), Domo's tool (``tools.model_key``) and the chat's key form
(``channels.page.key_routes``) alike. The line says the operation (a route of the API, never a path
with an id in it), the status and the code, and nothing else: never the token, a request's body, a
form's path or what the gatekeeper's answer said beyond its code.

A program's request goes through the gatekeeper's relay for programs (``out``): what comes back
is its answer, scrubbed by the gatekeeper, or ``OutRefused`` with the relay's code alone.
"""

import logging
import re
from pathlib import Path
from typing import Final, cast, get_args
from urllib.parse import quote

import httpx
from domo_common.core_token import CoreTokenError
from domo_common.core_token import load_core_token as common_load_core_token
from domo_common.gatekeeper_api import (
    OutRefusalCode,
    OutRequest,
    OutResponse,
    ProviderRoute,
    SecretMetadata,
    SecretRequestCreate,
    SecretRequestView,
)
from pydantic import BaseModel, TypeAdapter

from domo_core.config import ConfigurationError

# How long the core waits for the gatekeeper, for its API and for the model calls it relays. The
# read timeout is a little above the 300 s that the gatekeeper waits for a provider, so that a
# provider that is too slow reaches the core as the gatekeeper's own 504 and not as a timeout.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 320.0
WRITE_TIMEOUT_S = 60.0
POOL_TIMEOUT_S = 10.0

# The error codes of the gatekeeper's API are plain snake_case words.
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

logger = logging.getLogger(__name__)

_SECRET_LIST = TypeAdapter(list[SecretMetadata])


class GatekeeperError(Exception):
    """A call to the gatekeeper's internal API failed.

    ``status`` is the HTTP status of its answer (``None`` when it could not be reached) and
    ``code`` the error code of that answer, or ``"unreachable"`` / ``"bad_response"`` when there
    was no usable answer. The message holds the operation, the status and the code, nothing else.
    """

    def __init__(self, operation: str, *, status: int | None = None, code: str | None = None):
        detail = f"status {status}" if status is not None else "no answer"
        if code is not None:
            detail += f", {code}"
        super().__init__(f"The gatekeeper call {operation} failed ({detail}).")
        self.operation = operation
        self.status = status
        self.code = code


class BindingConflictError(GatekeeperError):
    """The gatekeeper refused because a secret is already bound to other destinations: the
    bindings of a secret never change. To use other destinations, use another reference."""


# The route of a program's request, and the codes its refusals carry (``{"code": ...}``).
OUT_OPERATION: Final = "POST /internal/out"
_OUT_CODES: Final[frozenset[str]] = frozenset(get_args(OutRefusalCode))
# What the core waits beyond a program's own seconds, for the gatekeeper to answer once they ran
# out (it bounds the request itself by them).
OUT_MARGIN_S: Final = 10.0


class OutRefused(GatekeeperError):
    """A program's request (``GatekeeperClient.out``) was not relayed: ``code`` is the relay's
    own code (``OutRefusalCode``), ``unreachable`` when the gatekeeper gave no answer, or
    ``failed`` for any other answer. Nothing of an answer's text is kept."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(OUT_OPERATION, status=status, code=code)
        self.refusal = code


def _logged[E: GatekeeperError](error: E) -> E:
    """``error``, once it is logged: its operation, status and code (see the module docstring)."""
    logger.warning(
        "The gatekeeper call %s failed: status %s, code %s",
        error.operation,
        error.status,
        error.code,
    )
    return error


def load_core_token(path: Path) -> str:
    """The token the core authenticates with, read from ``path`` with the rule the gatekeeper
    reads it with (``domo_common.core_token``).

    There is no fallback. Raises ``ConfigurationError`` (whose message never includes the token)
    when the file is missing, unreadable, empty, or holds something that cannot be a header value.
    """
    try:
        return common_load_core_token(path)
    except CoreTokenError as exc:
        raise ConfigurationError(str(exc)) from None


def new_http_client(*, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The HTTP client of the core's calls to the gatekeeper: the timeouts above, no redirect
    followed and no proxy taken from the environment, since the token must go to the gatekeeper
    and nowhere else. ``transport`` is for tests."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=READ_TIMEOUT_S,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        ),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def _error_code(response: httpx.Response) -> str | None:
    """The ``{"error": "<code>"}`` of an answer, if it has one in the expected form."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    code = cast(dict[str, object], body).get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        return code
    return None


def _out_code(response: httpx.Response) -> str:
    """The relay's code of a refused program's request (``{"code": "<code>"}``, one of
    ``OutRefusalCode``), else ``failed``: never anything else an answer says."""
    try:
        body: object = response.json()
    except ValueError:
        return "failed"
    code = cast(dict[str, object], body).get("code") if isinstance(body, dict) else None
    if response.status_code != 200 and isinstance(code, str) and code in _OUT_CODES:
        return code
    return "failed"


class GatekeeperClient:
    """The gatekeeper's internal API, and the address of its relay.

    ``http`` is shared and owned by the caller (the application's lifespan closes it).
    """

    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._http = http

    def __repr__(self) -> str:
        return f"GatekeeperClient(base_url={self._base_url!r})"

    @property
    def token(self) -> str:
        """The core's token. Only for building the model SDKs' clients, which send it as their
        API key: it must not go anywhere else (not in a log, an error or a page)."""
        return self._token

    def relay_base_url(self, provider_id: str) -> str:
        """Where a model SDK sends its calls for ``provider_id``: the gatekeeper's relay."""
        return f"{self._base_url}/llm/{provider_id}"

    async def create_secret_request(self, request: SecretRequestCreate) -> SecretRequestView:
        """Ask the gatekeeper for a form where the owner will enter a secret.

        Raises ``BindingConflictError`` when the reference exists with other destinations.
        """
        operation = "POST /internal/secret-requests"
        response = await self._send(operation, "POST", "/internal/secret-requests", body=request)
        self._check(response, operation, expected=201)
        return self._parse(response, operation, SecretRequestView)

    async def get_secret_request(self, request_id: str) -> SecretRequestView:
        """The state of a secret request (pending until the owner fulfils it)."""
        operation = "GET /internal/secret-requests/{id}"
        path = f"/internal/secret-requests/{quote(request_id, safe='')}"
        response = await self._send(operation, "GET", path)
        self._check(response, operation, expected=200)
        return self._parse(response, operation, SecretRequestView)

    async def list_secrets(self) -> list[SecretMetadata]:
        """What the vault holds, without the values."""
        operation = "GET /internal/secrets"
        response = await self._send(operation, "GET", "/internal/secrets")
        self._check(response, operation, expected=200)
        try:
            return _SECRET_LIST.validate_json(response.content)
        except ValueError:
            error = GatekeeperError(operation, status=response.status_code, code="bad_response")
        raise _logged(error)

    async def register_provider(self, route: ProviderRoute) -> None:
        """Tell the gatekeeper where a provider is and which secret unlocks it.

        Raises ``BindingConflictError`` when the secret is bound to other destinations than the
        provider's host, and ``GatekeeperError`` when the secret does not exist (404), or the
        address would send a key in clear over the network (409, ``insecure_transport``).
        """
        operation = "PUT /internal/providers/{id}"
        path = f"/internal/providers/{quote(route.id, safe='')}"
        response = await self._send(operation, "PUT", path, body=route)
        self._check(response, operation, expected=204)

    async def out(self, request: OutRequest) -> OutResponse:
        """Have the gatekeeper send a program's request (``POST /internal/out``) and give its
        answer. Raises ``OutRefused`` with the relay's code when it refuses or fails,
        ``unreachable`` when the gatekeeper does not answer, ``failed`` for anything else: never
        with a word of what an answer said beyond a code it knows. The wait follows the
        request's own ``seconds``, which the gatekeeper bounds it by."""
        seconds = request.seconds + OUT_MARGIN_S
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S, read=seconds, write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S
        )
        try:
            response = await self._send(
                OUT_OPERATION, "POST", "/internal/out", body=request, timeout=timeout
            )
        except GatekeeperError:
            # Logged where it was raised: the gatekeeper gave no answer.
            refused = OutRefused("unreachable")
        else:
            if response.status_code == 200:
                try:
                    return OutResponse.model_validate_json(response.content)
                except ValueError:
                    pass
            refused = OutRefused(_out_code(response), status=response.status_code)
            _logged(refused)
        raise refused

    async def _send(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        headers = {"authorization": f"Bearer {self._token}"}
        content = None
        if body is not None:
            content = body.model_dump_json().encode()
            headers["content-type"] = "application/json"
        response: httpx.Response | None
        try:
            if timeout is None:
                response = await self._http.request(
                    method, f"{self._base_url}{path}", content=content, headers=headers
                )
            else:
                response = await self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    content=content,
                    headers=headers,
                    timeout=timeout,
                )
        except (httpx.HTTPError, httpx.InvalidURL):
            response = None
        if response is None:
            # Raised outside the handler: the HTTP exception holds the request, and so the token.
            # Named by its operation: the path may hold an id.
            raise _logged(GatekeeperError(operation, code="unreachable"))
        return response

    @staticmethod
    def _check(response: httpx.Response, operation: str, *, expected: int) -> None:
        if response.status_code == expected:
            return
        code = _error_code(response)
        if response.status_code == 409 and code in (None, "binding_conflict"):
            raise _logged(
                BindingConflictError(operation, status=409, code=code or "binding_conflict")
            )
        raise _logged(GatekeeperError(operation, status=response.status_code, code=code))

    @staticmethod
    def _parse[M: BaseModel](response: httpx.Response, operation: str, model: type[M]) -> M:
        try:
            return model.model_validate_json(response.content)
        except ValueError:
            error = GatekeeperError(operation, status=response.status_code, code="bad_response")
        raise _logged(error)
