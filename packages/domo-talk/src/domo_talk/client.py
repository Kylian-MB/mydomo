"""The client of the channel of Domo's API (``/api/channel``): JSON in, JSON and server-sent
events out, with the secret of this computer's device as a Bearer token. It holds no logic of
Domo: it sends what it is given and gives back what the server says.

A secret, a password, a code is never in an exception message, a repr or a log: the exceptions
here say a plain sentence, the server's error code at most.
"""

import codecs
import json
import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Self, cast
from urllib.parse import urlsplit

import httpx

from domo_talk.config import Saved

API_PATH = "/api/channel"
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_LINE_END = re.compile(r"\r\n|\r|\n")
# What the stream of what Domo says on his own may stay silent: the server writes a comment
# every 20 seconds, so a longer silence is a connection that died. In seconds.
_LISTEN_QUIET_LIMIT = 90.0

_SENTENCES = {
    "no_key": "The assistant cannot answer yet: he has no key to think with.",
    "too_long": "That is too long to send.",
    "not_sayable": "The assistant cannot take that message.",
    "held_back": "Too many tries. Wait a little, then try again.",
    "unknown_code": "That code is not valid: it may be wrong, already used, or too old.",
    "bad_name": "That name cannot be used.",
}


_PROOF_BUSY = "The assistant is checking too many passwords at once. Try again in a moment."


class TalkError(Exception):
    """Something the person is told in one plain sentence (the message), and the code the program
    then exits with."""

    exit_status = 1


class Unpaired(TalkError):
    """The server does not know this device: it is unknown, or it was taken back."""

    exit_status = 2

    def __init__(self) -> None:
        super().__init__(
            "The assistant does not know this computer (any more). Pair it again: domo-talk pair "
            "<url>"
        )


class Unreachable(TalkError):
    def __init__(self, url: str, error: Exception) -> None:
        message = f"Cannot reach the assistant at {url}."
        if "CERTIFICATE" in str(error).upper():
            message += " His certificate is not trusted here: pair with --ca <path to his ca.crt>."
        super().__init__(message)


class Refused(TalkError):
    """The server refused: ``error`` is its code."""

    def __init__(self, error: str, sentence: str | None = None) -> None:
        self.error = error
        super().__init__(
            sentence or _SENTENCES.get(error, f"The assistant refused that ({error}).")
        )


class Event(NamedTuple):
    """An event of a stream: its name and its data, a JSON object."""

    name: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """A message of the conversation as Domo keeps it, its cards already in words."""

    id: str
    role: str
    text: str
    cards: tuple[str, ...]


@dataclass(frozen=True)
class Past:
    """The conversation as Domo keeps it, and the name the person calls him by (``None``: they
    call him Domo)."""

    messages: list[Message]
    domo_name: str | None = None


@dataclass(frozen=True)
class Proved:
    """What a proof came to: its outcome, and the server's sentence for it."""

    outcome: str
    sentence: str
    # How the request is answered now (``{"id", "answer_with", "proofs"}``), None when it no longer
    # waits for this person.
    request: dict[str, Any] | None = None
    # What the request that ran shows only once (``{"kind", "value", "sentence"}``: a link, a
    # code), None for nothing; never in a representation.
    once: dict[str, Any] | None = field(default=None, repr=False)

    def waits_for(self, confirmation_id: str, means: str) -> bool:
        """Whether the server says that the request ``confirmation_id`` still waits for a proof
        by ``means``."""
        request = self.request
        if request is None or request.get("id") != confirmation_id:
            return False
        proofs: object = request.get("proofs")
        return isinstance(proofs, list) and means in cast(list[object], proofs)


class SseParser:
    """Reads a stream of server-sent events, given in pieces cut anywhere: between two bytes of a
    character, between the two characters of a line break.

    ``feed`` gives the events that the piece completed, as ``(name, data)``: the name is the last
    ``event`` field (``message`` for none), the data the ``data`` fields joined by a line feed.
    Lines end with LF, CRLF or CR; a line starting with ``:`` is a comment; one space after the
    colon of a field is dropped. An event that the stream ends in the middle of is not given."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text = ""
        self._name = ""
        self._data: list[str] | None = None

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        self._text += self._decoder.decode(chunk)
        events: list[tuple[str, str]] = []
        while (line_end := _LINE_END.search(self._text)) is not None:
            if line_end.group() == "\r" and line_end.end() == len(self._text):
                break  # may be the first half of CRLF
            line = self._text[: line_end.start()]
            self._text = self._text[line_end.end() :]
            found = self._line(line)
            if found is not None:
                events.append(found)
        return events

    def _line(self, line: str) -> tuple[str, str] | None:
        if line == "":
            if self._data is None:
                self._name = ""
                return None
            event = (self._name or "message", "\n".join(self._data))
            self._name, self._data = "", None
            return event
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._name = value
        elif name == "data":
            self._data = [*(self._data or []), value]
        return None


def _checked_url(url: str) -> str:
    """The address of Domo without a closing slash. Plain http is for this computer only: a
    secret does not travel in the clear to another machine."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in _LOOPBACK:
        return url
    raise TalkError("Give the https address of the assistant, for example https://domo.example")


def _verify(ca: str | None) -> ssl.SSLContext | bool:
    """What the certificate of Domo is checked against: the system's store, or, when the person
    gave it, the authority of Domo alone. Never nothing."""
    if ca is None:
        return True
    try:
        return ssl.create_default_context(cafile=ca)
    except (OSError, ssl.SSLError):
        raise TalkError(f"Cannot read the certificate file {ca}.") from None


def _http(url: str, ca: str | None, headers: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=url + API_PATH,
        headers=headers,
        verify=_verify(ca),
        timeout=httpx.Timeout(30.0),
    )


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        found: object = response.json()
    except ValueError:
        return None
    return found if isinstance(found, dict) else None  # pyright: ignore[reportUnknownVariableType]


def _check(response: httpx.Response) -> None:
    """Nothing for a good answer; ``Unpaired`` for a 401, ``Refused`` for any other failure."""
    if response.status_code == 401:
        raise Unpaired
    if response.status_code >= 400:
        response.read()
        error = (_json_object(response) or {}).get("error")
        raise Refused(error if isinstance(error, str) else f"http_{response.status_code}")


def _object(response: httpx.Response) -> dict[str, Any]:
    found = _json_object(response)
    if found is None:
        raise Refused("bad_answer")
    return found


def _event_of(name: str, data: str) -> Event | None:
    try:
        found: object = json.loads(data)
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    return Event(name, cast(dict[str, Any], found))


class Client:
    """What this computer says to Domo, with the device's secret."""

    def __init__(self, saved: Saved) -> None:
        self._url = _checked_url(saved.url)
        self._http = _http(self._url, saved.ca, {"Authorization": f"Bearer {saved.secret}"})
        # The reply ``say`` read last, if any.
        self.last_reply: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def pair(url: str, code: str, name: str, ca: str | None) -> Saved:
        """Trade the code Domo showed for a secret of this computer's own."""
        url = _checked_url(url)
        with _http(url, ca, {}) as http:
            try:
                response = http.post("/pair", json={"code": code, "name": name})
            except httpx.HTTPError as error:
                raise Unreachable(url, error) from None
        _check(response)
        secret = _object(response).get("secret")
        if not isinstance(secret, str) or not secret:
            raise Refused("bad_answer")
        return Saved(url=url, secret=secret, ca=ca)

    def say(self, text: str) -> Iterator[Event]:
        """Say ``text`` to Domo, then give the events of his reply as they come. While he writes a
        reply, the message is kept for it: no event here, what answers it comes as what he says on
        his own (``listen``)."""
        response = self._call("POST", "/messages", json={"text": text})
        answer = _object(response)
        if answer.get("kept") is True:
            return
        reply_id = answer.get("reply_id")
        if not isinstance(reply_id, str):
            raise Refused("bad_answer")
        self.last_reply = reply_id
        yield from self._events(f"/stream/{reply_id}", read_timeout=None)

    def listen(self) -> Iterator[Event]:
        """What Domo says on his own, for as long as the connection holds."""
        yield from self._events("/listen", read_timeout=_LISTEN_QUIET_LIMIT)

    def since(self, after: str | None) -> list[Message]:
        """The conversation as Domo keeps it, after the message ``after`` (the latest ones
        without it)."""
        return self.past(after).messages

    def past(self, after: str | None) -> Past:
        """``since``, with the name the person calls Domo by, as the server gives it."""
        params = {} if after is None else {"after": after}
        answer = _object(self._call("GET", "/since", params=params))
        name: object = answer.get("domo_name")
        found = answer.get("messages")
        items: list[Any] = found if isinstance(found, list) else []  # pyright: ignore[reportUnknownVariableType]
        messages: list[Message] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            fields: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
            cards: object = fields.get("cards")
            messages.append(
                Message(
                    id=str(fields.get("id", "")),
                    role=str(fields.get("role", "")),
                    text=str(fields.get("text", "")),
                    cards=tuple(str(c) for c in cast(list[object], cards))
                    if isinstance(cards, list)
                    else (),
                )
            )
        return Past(messages, name if isinstance(name, str) and name else None)

    def prove(self, confirmation_id: str, means: str, answer: str) -> Proved:
        """Give a proof for the request ``confirmation_id``. ``answer`` goes into the body of the
        request and nowhere else."""
        try:
            response = self._call(
                "POST",
                f"/confirmations/{confirmation_id}/prove",
                json={"means": means, "answer": answer},
            )
        except Refused as refused:
            if refused.error == "busy":  # too many passwords wait to be checked
                raise Refused("busy", _PROOF_BUSY) from None
            raise
        found = _object(response)
        request = found.get("request")
        once = found.get("once")
        return Proved(
            outcome=str(found.get("outcome", "")),
            sentence=str(found.get("sentence", "")),
            request=cast(dict[str, Any], request) if isinstance(request, dict) else None,
            once=cast(dict[str, Any], once) if isinstance(once, dict) else None,
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise Unreachable(self._url, error) from None
        _check(response)
        return response

    def _events(self, path: str, *, read_timeout: float | None) -> Iterator[Event]:
        timeout = httpx.Timeout(30.0, read=read_timeout)
        parser = SseParser()
        try:
            with self._http.stream("GET", path, timeout=timeout) as response:
                if response.status_code == 204:
                    return
                _check(response)
                for chunk in response.iter_bytes():
                    for name, data in parser.feed(chunk):
                        event = _event_of(name, data)
                        if event is not None:
                            yield event
        except httpx.HTTPError as error:
            raise Unreachable(self._url, error) from None
