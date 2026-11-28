"""Links sealed for one launch: what lets one of the assistant's tools send a secret link that
neither a model nor a program ever sees (design 6.5).

The tool that sends an invitation's link (``tools.invitation_send``) seals it here and gives the
program a marker in its place (``seal``: ``domo-sealed:<32 hex>``). The marker opens only in the
body of a request out (``madetools.means``), just before the gatekeeper is called, only in the
launch of the tool and the operation the seal is for, and in one request at most: never in a URL
nor a header (those reach logs), never in a dry run. That request goes to the address it names
alone (no redirect elsewhere), and the program reads only its status, never what the address said
back. What else comes back to a program of the chain is scrubbed of the link and its token
(``scrub``, ``scrub_bytes``), a second layer: an address that echoes what it received later gives
the program the marker again in the spellings below.

What is scrubbed: each value as written, percent-encoded (whole, or keeping ``/``), with ``/``
escaped as JSON may write it, and in base64 (standard and URL-safe, without its padding) when it
starts the encoded text; the token, made of URL-safe characters only, is found inside any of those
spellings of the link. Not scrubbed: a value encoded otherwise (hexadecimal, base64 starting at
another offset, a value cut in pieces, compressed or encrypted).

A ``SealedLinks`` lives in memory for one launch and its chain, and is never stored."""

import base64
import secrets
from collections.abc import Sequence
from typing import Final
from urllib.parse import quote

__all__ = ["MARKER_PREFIX", "SealedLinks"]

# What a marker begins with; 32 hexadecimal digits follow.
MARKER_PREFIX: Final = "domo-sealed:"
# The marker as a form or a query encodes it (``:`` percent-encoded, in either case).
_ENCODED_PREFIXES: Final = ("domo-sealed%3A", "domo-sealed%3a")
_HEX_DIGITS: Final = 32


def _spellings(value: str) -> list[str]:
    """The spellings of ``value`` scrubbed (see the module docstring), longest first."""
    raw = value.encode("utf-8")
    found = {
        value,
        quote(value, safe=""),
        quote(value),
        value.replace("/", "\\/"),
        base64.b64encode(raw).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
    }
    return sorted((spelling for spelling in found if spelling), key=len, reverse=True)


class SealedLinks:
    """The links sealed for one launch of the tool ``tool``, opened only in the requests out of its
    operation ``operation`` (see the module docstring)."""

    def __init__(self, tool: str, operation: str) -> None:
        self.tool = tool
        self.operation = operation
        self._raw: dict[str, str] = {}
        self._scrubbed: list[str] = []
        self._used = 0
        self._opened = False

    def seal(self, raw: str, *, scrub_too: Sequence[str] = ()) -> str:
        """The marker of ``raw``, which the program is given in its place. ``scrub_too``: other
        values that never come back to a program either (the link's token)."""
        marker = f"{MARKER_PREFIX}{secrets.token_hex(_HEX_DIGITS // 2)}"
        self._raw[marker] = raw
        self._scrubbed = [*self._scrubbed, raw, *(value for value in scrub_too if value)]
        return marker

    def opens_in(self, tool: str | None, operation: str | None) -> bool:
        """Whether the markers open in a request out of ``operation`` in a launch of ``tool``: in
        one request at most (``opened_once``)."""
        return not self._opened and tool == self.tool and operation == self.operation

    def opened_once(self) -> None:
        """Note that the markers opened in a request handed to the gatekeeper: they never open in
        another one, so a program cannot send the link again to have it echoed back."""
        self._opened = True

    def open_in(self, body: bytes) -> tuple[bytes, int]:
        """``body`` with each marker replaced by its value (percent-encoded where the marker was),
        and how many were opened. Counts nothing: ``sent`` does, once the request went out."""
        opened = 0
        for marker, raw in self._raw.items():
            for written, value in (
                (marker, raw),
                *(
                    (marker.replace(MARKER_PREFIX, prefix), quote(raw, safe=""))
                    for prefix in _ENCODED_PREFIXES
                ),
            ):
                needle = written.encode("ascii")
                count = body.count(needle)
                if count:
                    body = body.replace(needle, value.encode("utf-8"))
                    opened += count
        return body, opened

    def sent(self, opened: int) -> None:
        """Note that ``opened`` markers went out in a request the address took (a 2xx answer)."""
        self._used += opened

    @property
    def used(self) -> int:
        """How many markers went out, opened, in a request the address took."""
        return self._used

    def values(self) -> tuple[str, ...]:
        """What never comes back to a program: each link sealed and the values scrubbed with it."""
        return tuple(dict.fromkeys(self._scrubbed))

    def _replacements(self) -> list[tuple[str, str]]:
        marker_of = {raw: marker for marker, raw in self._raw.items()}
        fallback = next(iter(self._raw), MARKER_PREFIX)
        pairs: list[tuple[str, str]] = []
        for value in self.values():
            marker = marker_of.get(value, fallback)
            pairs.extend((spelling, marker) for spelling in _spellings(value))
        return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)

    def scrub(self, text: str) -> str:
        """``text`` with every spelling of the values replaced by a marker."""
        for spelling, marker in self._replacements():
            text = text.replace(spelling, marker)
        return text

    def scrub_bytes(self, data: bytes) -> bytes:
        """``data`` with every spelling of the values replaced by a marker."""
        for spelling, marker in self._replacements():
            data = data.replace(spelling.encode("utf-8"), marker.encode("ascii"))
        return data
