"""Masking of secret values in what a provider sends back.

A provider may echo the key it was given (an error message, a debug header), and the gatekeeper
must never hand a secret value to the core. ``Scrubber`` masks every secret in a stream of
bytes, whatever the way the stream is cut into chunks; ``scrub_text`` does the same for a string.
It is given the parts of the secrets (``vault.secret_parts``): the user and the password of an
account are masked each on its own, so that one of them echoed alone is masked too.

A secret is masked in its raw form and, when it is at least ``_MIN_ENCODED_LENGTH`` characters
long, in the forms it commonly takes when echoed: base64 (standard and URL-safe alphabets, with
and without padding), percent-encoding, and the escapes of a JSON string (a quote, a backslash, a
``\\/``, a letter outside ASCII as ``\\uXXXX``). Shorter secrets are masked in their raw form only:
their encoded forms are too short to be told apart from ordinary text.
"""

import base64
import json
import re
from collections.abc import Iterable, Sequence
from typing import Final
from urllib.parse import quote

MASK = "[REDACTED]"

_MASK_BYTES = MASK.encode()
_MIN_ENCODED_LENGTH = 8
# The shortest secret, in bytes, looked for in sound (``SecretDetector``). Eight random bytes
# turn up by chance once in about 10**19 bytes; two, every few seconds of sound.
SHORTEST_DETECTED: Final = 8


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
    variants |= _json_forms(secret)
    return variants


def _json_forms(secret: str) -> set[bytes]:
    """The forms ``secret`` takes inside a JSON string (an error body echoing it): its quote,
    backslash and control characters escaped, the letters outside ASCII as they are or as
    ``\\uXXXX`` (hex digits in either case), and ``/`` raw or written ``\\/``."""
    forms: set[bytes] = set()
    for ensure_ascii in (True, False):
        escaped = json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1]
        for written in (escaped, escaped.replace("/", "\\/")):
            forms.add(_encode(written))
            upper = re.sub(
                r"\\u[0-9a-f]{4}", lambda match: "\\u" + match.group()[2:].upper(), written
            )
            forms.add(_encode(upper))
    return forms


def _matcher(secrets: Iterable[str]) -> tuple[re.Pattern[bytes] | None, int]:
    """The pattern of every form of ``secrets`` (``None`` for none) and how many bytes a stream
    must keep back so that a secret cut between two chunks is still seen whole."""
    variants: set[bytes] = set()
    for secret in secrets:
        if secret:
            variants |= _variants(secret)
    # Longest first: where several variants start at the same place, the longest wins (a padded
    # base64 form is masked whole, not just up to its padding).
    ordered = sorted(variants, key=lambda variant: (-len(variant), variant))
    pattern = re.compile(b"|".join(re.escape(v) for v in ordered)) if ordered else None
    return pattern, max((len(v) for v in ordered), default=1) - 1


class Scrubber:
    """Masks secrets in a stream of bytes fed chunk by chunk.

    Each chunk may end in the middle of a secret, so ``feed`` keeps back the last
    ``longest variant - 1`` bytes until the next chunk shows whether they start one; ``flush``
    releases them at the end of the stream. The output is the same whatever the chunking.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        self._pattern, self._holdback = _matcher(secrets)
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


class SecretFound(Exception):
    """A secret was found in a stream that is never masked (``SecretDetector``). The message
    says nothing of which secret, or where."""


class SecretDetector:
    """Finds secrets in a stream of bytes that must reach its reader unchanged (sound).

    It matches what ``Scrubber`` masks, in the same forms, but never alters a byte: ``feed``
    returns the bytes now known to start no secret, and raises ``SecretFound`` when a secret is
    whole in what it was given. Like the scrubber it keeps back ``longest variant - 1`` bytes, so
    no part of a secret is released before the secret is seen whole; ``flush`` releases them at
    the end of the stream. The chunks it returns may be cut differently from the ones it took.

    Only secrets of ``SHORTEST_DETECTED`` bytes or more are looked for: a shorter one (a code of
    a few digits) turns up by chance in any long sound and would cut them all. Text keeps being
    scrubbed of every secret (``Scrubber``).
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        self._pattern, self._holdback = _matcher(
            secret for secret in secrets if len(_encode(secret)) >= SHORTEST_DETECTED
        )
        self._pending = b""

    def feed(self, chunk: bytes) -> bytes:
        """Take the next chunk; return what is now safe to emit, or raise ``SecretFound``."""
        if self._pattern is None:
            return chunk
        data = self._pending + chunk
        if self._pattern.search(data) is not None:
            self._pending = b""
            raise SecretFound("A secret was found in a stream that is relayed unchanged.")
        limit = max(len(data) - self._holdback, 0)
        self._pending = data[limit:]
        return data[:limit]

    def flush(self) -> bytes:
        """End of the stream: release what was kept back (it holds no whole secret)."""
        pending, self._pending = self._pending, b""
        return pending


def scrub_text(text: str, secrets: Sequence[str]) -> str:
    """``text`` with every secret masked."""
    scrubber = Scrubber(secrets)
    data = _encode(text)
    return (scrubber.feed(data) + scrubber.flush()).decode("utf-8", errors="surrogatepass")
