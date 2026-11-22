"""The models a key opens, read from the provider through the gatekeeper's relay.

The three SDKs list their models (``models.list``) the way the adapters call them: pointed at the
relay, given the core's token as their key, with no retry of their own. A failure becomes a
``ModelCallError`` (the same kinds as a call, see ``adapters._support``); one case is not a
failure: a server that imitates OpenAI and has no list (it answers 404) gives no model, and the
owner is asked for a name.

What a provider answers is not trusted: names over ``MAX_MODEL_ID_LENGTH`` characters or with a
control character are dropped, and the list is cut at ``MAX_LISTED_MODELS``.
"""

import unicodedata
from collections.abc import Awaitable, Callable
from typing import Final

import httpx
import httpx2
from anthropic import APIConnectionError as AnthropicConnectionError
from anthropic import APIStatusError as AnthropicStatusError
from anthropic import AsyncAnthropic
from google import genai
from google.genai import errors, types
from openai import APIConnectionError as OpenAIConnectionError
from openai import APIStatusError as OpenAIStatusError
from openai import AsyncOpenAI

from domo_core.gatekeeper_client import GatekeeperClient, new_http_client
from domo_core.llm.adapters._support import (
    kind_for_status,
    log_failure,
    new_sdk_http_client,
    sdk_timeout,
)
from domo_core.llm.adapters.google import apply_client_timeouts
from domo_core.llm.models import Provider
from domo_core.llm.types import ErrorKind, ModelCallError

MAX_LISTED_MODELS: Final = 500
MAX_MODEL_ID_LENGTH: Final = 200
# Anthropic's page size (its SDK follows the pages by itself).
_ANTHROPIC_PAGE_SIZE: Final = 100
_GOOGLE_GENERATE: Final = "generateContent"

# Lists the models a key opens (injected like the model factory).
ModelLister = Callable[[Provider], Awaitable[tuple[str, ...]]]


def has_control_characters(text: str) -> bool:
    """Whether ``text`` holds a control character or a line or paragraph separator."""
    return any(unicodedata.category(character) in ("Cc", "Zl", "Zp") for character in text)


def _usable(model_id: str) -> bool:
    return 0 < len(model_id) <= MAX_MODEL_ID_LENGTH and not has_control_characters(model_id)


def _classify(error: Exception) -> ErrorKind:
    if isinstance(error, OpenAIStatusError | AnthropicStatusError):
        return kind_for_status(error.status_code)
    if isinstance(error, errors.APIError):
        return kind_for_status(error.code)
    if isinstance(
        error,
        OpenAIConnectionError
        | AnthropicConnectionError
        | httpx2.TransportError
        | httpx.TransportError,
    ):
        return "unreachable"
    return "upstream"


class _Names:
    """The names kept so far: usable ones, once each, up to the limit."""

    def __init__(self) -> None:
        self._seen: dict[str, None] = {}

    def add(self, model_id: str) -> bool:
        """Keep ``model_id`` if it is usable. Returns whether the list is full."""
        if _usable(model_id):
            self._seen.setdefault(model_id)
        return len(self._seen) >= MAX_LISTED_MODELS

    def result(self) -> tuple[str, ...]:
        return tuple(self._seen)


async def _openai_names(
    base_url: str, token: str, transport: httpx2.AsyncBaseTransport | None
) -> tuple[str, ...]:
    names = _Names()
    async with AsyncOpenAI(
        api_key=token,
        base_url=base_url,
        max_retries=0,
        timeout=sdk_timeout(),
        http_client=new_sdk_http_client(transport),
    ) as client:
        async for model in client.models.list():
            if names.add(model.id):
                break
    return names.result()


async def _anthropic_names(
    base_url: str, token: str, transport: httpx2.AsyncBaseTransport | None
) -> tuple[str, ...]:
    names = _Names()
    async with AsyncAnthropic(
        api_key=token,
        base_url=base_url,
        max_retries=0,
        timeout=sdk_timeout(),
        http_client=new_sdk_http_client(transport),
    ) as client:
        async for model in client.models.list(limit=_ANTHROPIC_PAGE_SIZE):
            if names.add(model.id):
                break
    return names.result()


async def _google_names(base_url: str, token: str) -> tuple[str, ...]:
    names = _Names()
    http = new_http_client()
    http.event_hooks["request"].append(apply_client_timeouts(http.timeout))
    client: genai.Client | None = None
    try:
        client = genai.Client(
            api_key=token,
            vertexai=False,
            http_options=types.HttpOptions(base_url=base_url, httpx_async_client=http),
        )
        pager = await client.aio.models.list()  # pyright: ignore[reportUnknownMemberType]
        async for model in pager:
            if _GOOGLE_GENERATE not in (model.supported_actions or []):
                continue
            if names.add((model.name or "").removeprefix("models/")):
                break
    finally:
        await http.aclose()
        if client is not None:
            client.close()
    return names.result()


async def list_models(
    gatekeeper: GatekeeperClient,
    provider: Provider,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    """The names of the models the key ``provider`` opens, through the relay.

    Raises ``ModelCallError`` when the provider cannot be listed (a refused key is ``auth``). A
    server that imitates OpenAI and answers 404, which has no list, gives ``()``. Google's list
    keeps the models that can generate content, without their ``models/`` prefix. ``transport``
    is for tests (the SDKs on ``httpx2``).
    """
    base_url = gatekeeper.relay_base_url(provider.id)
    token = gatekeeper.token
    failure: ErrorKind | None = None
    names: tuple[str, ...] = ()
    unknown = False
    try:
        match provider.kind:
            case "openai" | "openai_compatible":
                names = await _openai_names(base_url, token, transport)
            case "anthropic":
                names = await _anthropic_names(base_url, token, transport)
            case "google":
                names = await _google_names(base_url, token)
            case _:
                unknown = True
    except Exception as error:
        if (
            provider.kind == "openai_compatible"
            and isinstance(error, OpenAIStatusError)
            and error.status_code == 404
        ):
            return ()
        failure = _classify(error)
        log_failure(failure, error)
    if unknown:
        raise ValueError(f"Unknown provider kind: {provider.kind!r}")
    if failure is not None:
        # Raised outside the handler: the SDK's exception holds the request, and so the token.
        raise ModelCallError(failure)
    return names
