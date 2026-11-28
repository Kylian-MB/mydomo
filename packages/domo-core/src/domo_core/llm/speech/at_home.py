"""Whether a key is a server of the house: the one rule of where an address leads.

The core resolves no name. The gatekeeper does, once, and judges the address it found with the
zones it also judges a program's request by (``domo_gatekeeper.reach``): "at home" is the zone
``house`` (a device of the private ranges) or ``host`` (the machine that hosts the assistant). A
public name, a public address, a name that resolves publicly, an address that leads nowhere, and
a gatekeeper that does not answer are all NOT at home: the check fails closed.

The one exception is the service the owner declared (``DOMO_SPEECH_URL``, ``HOME_SPEECH_ID``): it
is a service of the assistant's own installation, on a network those zones call "never", so it is
at home by that declaration, without asking the zone, for as long as its address is still the one
declared. Every other key keeps the zone rule.

Only a server that imitates OpenAI can be at home (the kind a speech service has). The answer is
kept briefly for a key and its address (``KEEP_FOR_S``), so that a turn of several sounds asks
once; a failure is never kept.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.models import Provider
from domo_core.llm.speech.home import HOME_SPEECH_ID

_AT_HOME_KIND: Final = "openai_compatible"
_HOME_ZONES: Final = frozenset({"house", "host"})
# How long an answer is kept, in seconds.
KEEP_FOR_S: Final = 60.0


def _normalised(url: str) -> str:
    """``url`` as one spelling: lower-case scheme and host, no trailing slash."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        parts._replace(netloc=parts.netloc.lower(), path=parts.path.rstrip("/"))
    ).rstrip("/")


# The check the routes and the prices take: whether a key is a server of the house.
HomeCheck = Callable[[Provider], Awaitable[bool]]


class AtHome:
    """The check ``HomeCheck`` made of the gatekeeper's answer, kept for ``KEEP_FOR_S``."""

    def __init__(
        self,
        gatekeeper: GatekeeperClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        keep_for: float = KEEP_FOR_S,
        declared_url: str | None = None,
    ) -> None:
        self._gatekeeper = gatekeeper
        self._declared = _normalised(declared_url) if declared_url else None
        self._clock = clock
        self._keep_for = keep_for
        self._kept: dict[tuple[str, str], tuple[float, bool]] = {}

    async def __call__(self, provider: Provider) -> bool:
        if provider.kind != _AT_HOME_KIND:
            return False
        if (
            provider.id == HOME_SPEECH_ID
            and self._declared
            and _normalised(provider.base_url) == self._declared
        ):
            return True
        key = (provider.id, provider.base_url)
        now = self._clock()
        kept = self._kept.get(key)
        if kept is not None and now < kept[0]:
            return kept[1]
        try:
            zone = await self._gatekeeper.provider_zone(provider.id)
        except GatekeeperError:
            return False  # logged where it is raised; not kept, so it is asked again
        home = zone in _HOME_ZONES
        self._kept[key] = (now + self._keep_for, home)
        return home
