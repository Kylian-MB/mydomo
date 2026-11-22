"""The channel of the API (spec 6.7): a device entrusted to Domo (spec 6.5) speaks to the core in
JSON and server-sent events under ``API_PATH``, with the device's secret alone: no browser, no
cookie, no page. It adds nothing of Domo: it gives the core what the device says, and puts
what the core gives back into JSON (``channels.api.routes``, ``channels.api.render``).

Only the path is here, for the middlewares that treat the channel apart (``web.security``,
``setup_flow.middleware``, ``web.texts_middleware``): importing it imports nothing else.
"""

from typing import Final

# Where the channel answers, and each of its routes under it.
API_PATH: Final = "/api/channel"


def is_api_path(path: str) -> bool:
    """Whether ``path`` is the channel's, or under it."""
    return path == API_PATH or path.startswith(f"{API_PATH}/")
