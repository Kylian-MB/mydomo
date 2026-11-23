"""A fake device of the house, for the stack tests of going out (tests/stack/test_going_out.py).

It stands where a device of the house would: a program of Domo reaches it through the
gatekeeper, with the house's account (a token, ``Authorization: Bearer <token>``) the gatekeeper
places in the request. It answers, on ``FAKE_DEVICE_PORT`` (8000), in JSON:

* ``GET /health``: 200, for the container's health check; no token needed.
* ``GET /things``: the list of its things (``lamp``); ``GET /state/<thing>`` a thing's state;
  ``POST /state/<thing>`` with ``{"state": ...}`` sets it. Without exactly
  ``Bearer <FAKE_DEVICE_TOKEN>``: 401.
* ``GET /echo``: what it received in ``Authorization`` and ``Via`` (the header as it came and in
  base64), in the body and in an ``X-Echo-Authorization`` header: what a device that echoes its
  token would give back. No token needed.
* ``GET /redirect?to=<url>``: 302 to that URL.
* ``GET /slow?seconds=<n>``: answers after that many seconds.
* ``GET /endless``: a body that never ends (chunks until the other side goes away).

It never writes what it receives anywhere: no access log. Never part of an installation.
"""

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = os.environ.get("FAKE_DEVICE_TOKEN", "")
PORT = int(os.environ.get("FAKE_DEVICE_PORT", "8000"))
CHUNK = b"x" * 65536

_states = {"lamp": "off"}
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

    def _authorized(self) -> bool:
        return bool(TOKEN) and self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _thing(self, path: str) -> str | None:
        name = path.removeprefix("/state/")
        return name if name != path and name else None

    def do_GET(self) -> None:  # noqa: N802 - the base's name
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/health":
            self._send(200, {"ok": True})
        elif url.path == "/echo":
            authorization = self.headers.get("Authorization", "")
            echoed = {
                "authorization": authorization,
                "authorization_b64": base64.b64encode(authorization.encode()).decode("ascii"),
                "via": self.headers.get("Via", ""),
            }
            # Only printable ASCII goes in a header.
            self._send(200, echoed, {"X-Echo-Authorization": authorization})
        elif url.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", query.get("to", ["/health"])[0])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == "/slow":
            time.sleep(float(query.get("seconds", ["5"])[0]))
            self._send(200, {"slow": True})
        elif url.path == "/endless":
            self._endless()
        elif not self._authorized():
            self._send(401, {"error": "unauthorized"})
        elif url.path == "/things":
            with _lock:
                self._send(200, {"things": sorted(_states)})
        elif (thing := self._thing(url.path)) is not None:
            with _lock:
                state = _states.get(thing)
            if state is None:
                self._send(404, {"error": "no such thing"})
            else:
                self._send(200, {"thing": thing, "state": state})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - the base's name
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        thing = self._thing(url.path)
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if thing is None:
            self._send(404, {"error": "not found"})
            return
        try:
            state = json.loads(raw or b"{}")["state"]
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": 'a state is {"state": ...}'})
            return
        with _lock:
            _states[thing] = state
        self._send(200, {"thing": thing, "state": state})

    def _endless(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(CHUNK), CHUNK))
                self.wfile.flush()
        except OSError:
            return


def main() -> None:
    if not TOKEN:
        raise SystemExit("FAKE_DEVICE_TOKEN is not set")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
