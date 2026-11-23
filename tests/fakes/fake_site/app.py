"""A fake site with a login, for the stack tests of going out (tests/stack/test_going_out.py).

It stands where a site would: HTTPS with a certificate of the tests' own authority (the files
``FAKE_SITE_CERT`` and ``FAKE_SITE_KEY``, made by the tests), on ``FAKE_SITE_PORT`` (8443). A
program of Domo reaches it through the gatekeeper, with an account (a user and a password) the
gatekeeper puts in the body of the login request; the session cookie the site gives goes to the
gatekeeper, never to the program. It answers, in JSON:

* ``GET /health``: 200, for the container's health check.
* ``POST /login`` (a form, ``user`` and ``password``): with ``FAKE_SITE_USER`` and
  ``FAKE_SITE_PASSWORD``, 200 and a new session cookie (``session``, 32 characters); else 401.
  Either way it echoes the password it received: what a careless site would give back.
* ``GET /account``: with the cookie of a session, 200, the account's user and the session it saw
  (an echo of the cookie); else 401.

It never writes what it receives anywhere: no access log. Never part of an installation.
"""

import json
import os
import secrets
import ssl
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USER = os.environ.get("FAKE_SITE_USER", "")
PASSWORD = os.environ.get("FAKE_SITE_PASSWORD", "")
PORT = int(os.environ.get("FAKE_SITE_PORT", "8443"))
CERT = os.environ.get("FAKE_SITE_CERT", "/certs/site.crt")
KEY = os.environ.get("FAKE_SITE_KEY", "/certs/site.key")

_sessions: set[str] = set()
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base's name
        """No access log: nothing received is written anywhere."""

    def _send(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _session(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("session")
        if morsel is None:
            return None
        with _lock:
            return morsel.value if morsel.value in _sessions else None

    def do_GET(self) -> None:  # noqa: N802 - the base's name
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
        elif path == "/account":
            session = self._session()
            if session is None:
                self._send(401, {"error": "log in first"})
            else:
                self._send(200, {"account": USER, "session_seen": session})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - the base's name
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path != "/login":
            self._send(404, {"error": "not found"})
            return
        form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        user = form.get("user", [""])[0]
        password = form.get("password", [""])[0]
        if not (USER and PASSWORD and user == USER and password == PASSWORD):
            self._send(401, {"error": "wrong account", "password_seen": password})
            return
        session = secrets.token_hex(16)
        with _lock:
            _sessions.add(session)
        cookie = f"session={session}; Path=/; Secure; HttpOnly"
        self._send(200, {"welcome": user, "password_seen": password}, {"Set-Cookie": cookie})


def main() -> None:
    if not (USER and PASSWORD):
        raise SystemExit("FAKE_SITE_USER and FAKE_SITE_PASSWORD are not set")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(CERT, KEY)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
