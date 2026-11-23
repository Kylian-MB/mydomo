"""The kinds of model provider Domo knows, and how the gatekeeper must send each one its key.

The core never holds a key. For a provider with a key, the gatekeeper adds it to the request it
relays, in the place that the provider's API expects (``key_location``): the core's setup code
registers that place as the binding of the secret.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from domo_common.gatekeeper_api import HeaderLocation

ProviderKind = Literal["anthropic", "openai", "google", "openai_compatible"]


@dataclass(frozen=True)
class ProviderKindSpec:
    """What is fixed about a kind of provider.

    ``default_base_url`` is where the provider's API lives, or ``None`` when the user says (a
    server of their own). ``key_location`` is where the gatekeeper puts the key, and
    ``key_required`` whether the provider works without one (a local server may).
    """

    default_base_url: str | None
    key_location: HeaderLocation
    key_required: bool


PROVIDER_KINDS: Mapping[ProviderKind, ProviderKindSpec] = MappingProxyType(
    {
        "anthropic": ProviderKindSpec(
            default_base_url="https://api.anthropic.com",
            key_location=HeaderLocation(kind="header", name="x-api-key", template="{value}"),
            key_required=True,
        ),
        "openai": ProviderKindSpec(
            default_base_url="https://api.openai.com/v1",
            key_location=HeaderLocation(
                kind="header", name="authorization", template="Bearer {value}"
            ),
            key_required=True,
        ),
        "google": ProviderKindSpec(
            default_base_url="https://generativelanguage.googleapis.com",
            key_location=HeaderLocation(kind="header", name="x-goog-api-key", template="{value}"),
            key_required=True,
        ),
        "openai_compatible": ProviderKindSpec(
            default_base_url=None,
            key_location=HeaderLocation(
                kind="header", name="authorization", template="Bearer {value}"
            ),
            key_required=False,
        ),
    }
)
