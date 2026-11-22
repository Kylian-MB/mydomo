"""Building the model of a configured provider."""

from collections.abc import Callable

import httpx2

from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.adapters.anthropic import AnthropicChat
from domo_core.llm.adapters.google import GoogleChat
from domo_core.llm.adapters.openai_compat import OpenAICompatChat
from domo_core.llm.models import Provider
from domo_core.llm.types import ChatModel

# (key, model id): the model of a key is asked for by the caller, a call at a time.
ModelFactory = Callable[[Provider, str], ChatModel]


def build_chat_model(
    provider: Provider,
    model_id: str,
    gatekeeper: GatekeeperClient,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ChatModel:
    """The model ``model_id`` of the key ``provider``, which talks to the gatekeeper's relay and
    to nothing else.

    Each SDK is given the relay's address as its base URL (the provider's own address is
    registered with the gatekeeper, which is the one that calls it) and the core's token as its
    API key (the gatekeeper replaces it with the real key).

    ``transport`` is for tests: the HTTP transport of the OpenAI and Anthropic SDKs, which run on
    ``httpx2`` and so cannot be intercepted by ``respx``. The Google SDK runs on ``httpx``, which
    ``respx`` intercepts, and ignores it.
    """
    base_url = gatekeeper.relay_base_url(provider.id)
    token = gatekeeper.token
    match provider.kind:
        case "openai":
            return OpenAICompatChat(
                base_url=base_url,
                token=token,
                model=model_id,
                token_limit="max_completion_tokens",
                transport=transport,
            )
        case "openai_compatible":
            return OpenAICompatChat(
                base_url=base_url,
                token=token,
                model=model_id,
                token_limit="max_tokens",
                transport=transport,
            )
        case "anthropic":
            return AnthropicChat(
                base_url=base_url, token=token, model=model_id, transport=transport
            )
        case "google":
            return GoogleChat(base_url=base_url, token=token, model=model_id)
        case _:
            raise ValueError(f"Unknown provider kind: {provider.kind!r}")
