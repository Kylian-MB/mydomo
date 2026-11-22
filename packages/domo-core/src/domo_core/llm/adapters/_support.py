"""What the adapters have in common: the HTTP client of the SDKs, the meaning of a status and the
small rules of tool calling."""

import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import cast

import httpx2

from domo_core.gatekeeper_client import (
    CONNECT_TIMEOUT_S,
    POOL_TIMEOUT_S,
    READ_TIMEOUT_S,
    WRITE_TIMEOUT_S,
)
from domo_core.llm.types import ErrorKind, JsonObject, ToolCall

logger = logging.getLogger("domo_core.llm")

# The numbers of the identifiers that the adapters make up. Process-wide, so that two calls of a
# conversation never share one, whichever model call they came from.
_made_up_call_numbers = itertools.count(1)


def sdk_timeout() -> httpx2.Timeout:
    """The timeouts of a model call (see ``gatekeeper_client``)."""
    return httpx2.Timeout(
        connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S
    )


def new_sdk_http_client(transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """The HTTP client of an SDK that runs on ``httpx2`` (OpenAI, Anthropic).

    It does not follow redirects and ignores the proxy variables of the environment: the core's
    token, which the SDK sends as its API key, must go to the gatekeeper and nowhere else.
    ``transport`` is for tests.
    """
    return httpx2.AsyncClient(
        timeout=sdk_timeout(), follow_redirects=False, trust_env=False, transport=transport
    )


def kind_for_status(status: int) -> ErrorKind:
    """What an HTTP status of the relay means for the caller.

    The gatekeeper answers 409 when the secret of the provider is missing from its vault: the key
    has to be entered again, like for a refused key.
    """
    if status in (401, 403, 409):
        return "auth"
    if status in (502, 503, 504):
        return "unreachable"
    if status == 429:
        return "rate_limited"
    if status in (400, 404, 422):
        return "bad_request"
    return "upstream"


def log_failure(kind: ErrorKind, error: Exception) -> None:
    """Record that a model call failed: the kind and the type of the exception, never its text
    (an SDK's message can quote the provider's answer)."""
    logger.warning("Model call failed (%s): %s", kind, type(error).__name__)


def made_up_call_id() -> str:
    """The identifier of a tool call whose provider sent none (Google always, some servers that
    imitate OpenAI): ``call-<n>``, different each time."""
    return f"call-{next(_made_up_call_numbers)}"


@dataclass
class PendingCall:
    """A tool call being assembled from the pieces of a stream."""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list[str])
    provider_state: str | None = None

    def complete(self) -> ToolCall | None:
        """The call, or ``None`` when the provider never said which tool it is.

        A call with no argument at all has ``{}`` for them, which is what a tool with no
        parameter is called with; anything else is kept as received, for the caller to check.
        """
        if not self.name:
            return None
        arguments = "".join(self.arguments)
        return ToolCall(
            self.id or made_up_call_id(),
            self.name,
            arguments if arguments.strip() else "{}",
            self.provider_state,
        )


def object_arguments_text(arguments: str) -> str:
    """The arguments of a call as the text to replay in the history of the servers that imitate
    OpenAI: as received when they are a JSON object, else ``{}``.

    Those servers parse the arguments of the history for their chat templates, and answer 400 to
    text that is not an object: one bad call must not make the rest of the turn fail.
    """
    try:
        parsed: object = json.loads(arguments)
    except ValueError:
        return "{}"
    return arguments if isinstance(parsed, dict) else "{}"


def arguments_as_object(arguments: str) -> JsonObject:
    """The arguments of a call as the object that Anthropic and Google want in the history.

    The model can send anything and the history keeps it as received: what is not a JSON object is
    replayed as no arguments, so that one bad call does not make the whole conversation fail.
    """
    try:
        parsed: object = json.loads(arguments)
    except ValueError:
        return {}
    return cast(JsonObject, parsed) if isinstance(parsed, dict) else {}
