"""Masking of secret values in what a provider sends back.

A provider may echo the key it was given (an error message, a debug header), and the gatekeeper
must never hand a secret value to the core. ``Scrubber`` masks every secret in a stream of
bytes, whatever the way the stream is cut into chunks; ``scrub_text`` does the same for a string.

A secret is masked in its raw form and, when it is at least ``_MIN_ENCODED_LENGTH`` characters
long, in the forms it commonly takes when echoed: base64 (standard and URL-safe alphabets, with
and without padding) and percent-encoding. Shorter secrets are masked in their raw form only:
their encoded forms are too short to be told apart from ordinary text.
"""

import base64
import re
from collections.abc import Iterable, Sequence
from urllib.parse import quote

MASK = "[REDACTED]"

_MASK_BYTES = MASK.encode()
_MIN_ENCODED_LENGTH = 8


def _encode(text: str) -> bytes:
    # surrogatepass: text that is not valid Unicode must neither raise (the error would carry
    # the text) nor be skipped.
    return text.encode("utf-8", errors="surrogatepass")


def _variants(secret: str) -> set[bytes]:
    """Every byte string under which a secret is masked."""
    raw = _encode(secret)
    variants = {raw}
    if len(secret) < _MIN_ENCODED_LENGTH:
        return variants
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(raw)
        variants.add(encoded)
        variants.add(encoded.rstrip(b"="))
    percent = quote(raw, safe="").encode()
    variants.add(percent)
    # The hex digits of a percent-encoding are case-insensitive.
    variants.add(re.sub(rb"%[0-9A-F]{2}", lambda match: match.group().lower(), percent))
    return variants


class Scrubber:
    """Masks secrets in a stream of bytes fed chunk by chunk.

    Each chunk may end in the middle of a secret, so ``feed`` keeps back the last
    ``longest variant - 1`` bytes until the next chunk shows whether they start one; ``flush``
    releases them at the end of the stream. The output is the same whatever the chunking.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        variants: set[bytes] = set()
        for secret in secrets:
            if secret:
                variants |= _variants(secret)
        # Longest first: where several variants start at the same place, the longest wins (a
        # padded base64 form is masked whole, not just up to its padding).
        ordered = sorted(variants, key=lambda variant: (-len(variant), variant))
        self._pattern = re.compile(b"|".join(re.escape(v) for v in ordered)) if ordered else None
        self._holdback = max((len(v) for v in ordered), default=1) - 1
        self._pending = b""

    def feed(self, chunk: bytes) -> bytes:
        """Take the next chunk; return what is now safe to emit."""
        if self._pattern is None:
            return chunk
        data = self._pending + chunk
        # A match starting before ``limit`` lies wholly inside ``data``; one starting after it
        # might continue in the next chunk.
        limit = len(data) - self._holdback
        out = bytearray()
        position = 0
        for match in self._pattern.finditer(data):
            if match.start() >= limit:
                break
            out += data[position : match.start()]
            out += _MASK_BYTES
            position = match.end()
        emit_end = max(position, limit)
        out += data[position:emit_end]
        self._pending = data[emit_end:]
        return bytes(out)

    def flush(self) -> bytes:
        """End of the stream: release what was kept back, masked."""
        pending, self._pending = self._pending, b""
        if self._pattern is None:
            return pending
        return self._pattern.sub(_MASK_BYTES, pending)


def scrub_text(text: str, secrets: Sequence[str]) -> str:
    """``text`` with every secret masked."""
    scrubber = Scrubber(secrets)
    data = _encode(text)
    return (scrubber.feed(data) + scrubber.flush()).decode("utf-8", errors="surrogatepass")
