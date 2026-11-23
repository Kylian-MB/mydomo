"""The sessions of the sites an account logs in to: their cookies, kept by the gatekeeper.

A site that a program logs in to with an account answers with cookies: they are the session, as
good as the password for a while. They are kept here, in memory only, one jar per account, and
sent again with the next requests made with that account (``cookies_for``), never to the program.
A jar unused for the idle time is forgotten (``forget_idle``), and all are on a restart.
``values(ref)`` gives the values kept for one account, for the scrubber of that account's answers:
a cookie of a session never comes back to a program, even echoed by the site. The answers of
other requests are not scrubbed of them (the session is not theirs), and a value shorter than
8 characters is never scrubbed (a flag such as ``1`` is no credential).

The cookie rules (domain, path, ``Secure``, expiry) are the standard library's.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.message import Message
from http.cookiejar import CookieJar
from urllib.request import Request

# The shortest cookie value an answer is scrubbed of.
SHORTEST_SCRUBBED = 8


class _Answer:
    """What ``CookieJar.extract_cookies`` reads of an answer: its ``Set-Cookie`` headers."""

    def __init__(self, set_cookie_headers: Iterable[str]) -> None:
        self._headers = Message()
        for header in set_cookie_headers:
            self._headers["Set-Cookie"] = header

    def info(self) -> Message:
        return self._headers


def _url(host: str, port: int, path: str, secure: bool) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{'https' if secure else 'http'}://{shown}:{port}{path or '/'}"


def _sets_a_cookie(header: str) -> bool:
    """Whether a ``Set-Cookie`` header names a cookie and its value (``name=value``)."""
    name, equals, _ = header.partition(";")[0].partition("=")
    return bool(equals) and bool(name.strip())


@dataclass
class _Jar:
    cookies: CookieJar = field(default_factory=CookieJar)
    used_at: float = 0.0


class Sessions:
    """The cookie jars of the accounts, by account ref."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._jars: dict[str, _Jar] = {}

    def forget_idle(self, idle_seconds: float) -> None:
        """Forget every jar unused for more than ``idle_seconds``."""
        now = self._clock()
        for ref in [ref for ref, jar in self._jars.items() if now - jar.used_at > idle_seconds]:
            del self._jars[ref]

    def cookies_for(self, ref: str, host: str, port: int, path: str, secure: bool) -> str | None:
        """The ``Cookie`` header the account ``ref`` sends to ``host`` for ``path``, if any."""
        jar = self._jars.get(ref)
        if jar is None:
            return None
        jar.used_at = self._clock()
        request = Request(_url(host, port, path, secure))
        jar.cookies.add_cookie_header(request)
        return request.get_header("Cookie")

    def keep(self, ref: str, host: str, set_cookie_headers: Iterable[str]) -> None:
        """Keep the cookies ``host`` set in an answer to a request made with the account ``ref``."""
        headers = [header for header in set_cookie_headers if _sets_a_cookie(header)]
        if not headers:
            return
        jar = self._jars.setdefault(ref, _Jar())
        jar.used_at = self._clock()
        # The path of the request is not known here: a cookie without one is for the whole host.
        request = Request(_url(host, 443, "/", True))
        jar.cookies.extract_cookies(_Answer(headers), request)  # pyright: ignore[reportArgumentType]

    def values(self, ref: str) -> list[str]:
        """The cookie values kept for the account ``ref``: what the answers of its requests are
        scrubbed of. A value shorter than ``SHORTEST_SCRUBBED`` is left out: it is no credential,
        and masking it would blank ordinary text."""
        jar = self._jars.get(ref)
        if jar is None:
            return []
        return [
            cookie.value
            for cookie in jar.cookies
            if cookie.value and len(cookie.value) >= SHORTEST_SCRUBBED
        ]
