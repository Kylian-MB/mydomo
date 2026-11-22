"""A scripted gatekeeper for the tests of the pages that ask it for secrets.

It lives here, and not in ``conftest.py``, for the reason given in ``web_support.py``.

``GatekeeperMock`` plays the gatekeeper's internal API on a ``respx`` router mounted on the test
gatekeeper's address (``llm_support.GATEKEEPER_URL``). It keeps what the real one keeps that
matters to the pages: the secret requests (and their status), which bindings a reference has
(they never change: another set is a 409), and which new request supersedes the pending one of
the same reference. What it answers can be replaced route by route, through ``respx``.
"""

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import respx
from domo_common.gatekeeper_api import (
    FORM_TEXT_KEYS,
    Binding,
    ProviderRoute,
    SecretRequestCreate,
)
from domo_common.meanings import meaning

RequestStatus = str  # "pending", "fulfilled", "expired" or "superseded"
# The calls ``GatekeeperMock.fail_next`` can make fail, by the name of the client's method.
_OPERATIONS = frozenset({"create_secret_request", "get_secret_request", "register_provider"})


def form_texts(language: str = "en") -> dict[str, str]:
    """The texts of a vault form the core sends once Domo is installed, as the tests write them,
    their holes left in for the gatekeeper: each its meaning (the house's version, in English), or
    marked with ``language`` (the version ``web_support.write_language_version`` writes)."""
    return {
        key: meaning(key) if language == "en" else f"({language}) {meaning(key)}"
        for key in FORM_TEXT_KEYS
    }


class GatekeeperMock:
    """The secret requests and the provider registry of a fake gatekeeper.

    * ``secret_requests``: the route of ``POST /internal/secret-requests``;
    * ``request_views``: the route of ``GET /internal/secret-requests/{id}``;
    * ``providers``: the route of ``PUT /internal/providers/{id}`` (it answers 204);
    * ``refs`` and ``statuses``: the reference and the status of each request, by request id;
    * ``set_status(request_id, status)``: what the owner (or time) did to a request;
    * ``fail_next(operation, status=..., code=...)``: the next call of ``operation``
      (``create_secret_request``, ``get_secret_request`` or ``register_provider``, the names of
      the client's methods) fails with that status and error code.

    Use ``secret_requests.calls.last.request.content`` for the body of the last request. Requests
    to ``127.0.0.1`` (the live servers of the chat pilots, see ``chat_support.py``) pass through.
    ``router`` is the ``respx`` router itself, for the routes of the relay a test scripts
    (``/llm/<route id>/...``), and ``routes()`` the routes declared so far.
    """

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.refs: dict[str, str] = {}
        self.statuses: dict[str, RequestStatus] = {}
        self._bindings: dict[str, list[Binding]] = {}
        self._ids = itertools.count(1)
        self._failures: dict[str, httpx.Response] = {}
        self.secret_requests = router.post("/internal/secret-requests").mock(
            side_effect=self._create
        )
        self.request_views = router.get(
            path__regex=r"^/internal/secret-requests/(?P<id>[^/]+)$"
        ).mock(side_effect=self._get)
        self.providers = router.put(path__regex=r"^/internal/providers/(?P<id>[^/]+)$").mock(
            side_effect=self._register
        )
        # The live servers of the chat tests listen on the loopback interface: what their
        # browsers send there is not for the gatekeeper, and goes through unmocked.
        router.route(host="127.0.0.1").pass_through()

    def routes(self) -> list[ProviderRoute]:
        """The routes declared to the gatekeeper so far, in order."""
        return [
            ProviderRoute.model_validate_json(call.request.content) for call in self.providers.calls
        ]

    def set_status(self, request_id: str, status: RequestStatus) -> None:
        self.statuses[request_id] = status

    def fail_next(self, operation: str, *, status: int, code: str | None = None) -> None:
        if operation not in _OPERATIONS:
            raise ValueError(f"no such operation: {operation}")
        body = {"error": code} if code is not None else {}
        self._failures[operation] = httpx.Response(status, json=body)

    def _failure(self, operation: str) -> httpx.Response | None:
        return self._failures.pop(operation, None)

    def _register(self, request: httpx.Request, id: str) -> httpx.Response:
        return self._failure("register_provider") or httpx.Response(204)

    def _view(self, request_id: str) -> dict[str, Any]:
        status = self.statuses[request_id]
        expires_at = datetime.now(UTC) + timedelta(minutes=15)
        return {
            "id": request_id,
            "ref": self.refs[request_id],
            "status": status,
            "form_path": f"/vault/r/token-{request_id}" if status == "pending" else None,
            "expires_at": expires_at.isoformat(),
        }

    def _create(self, request: httpx.Request) -> httpx.Response:
        failure = self._failure("create_secret_request")
        if failure is not None:
            return failure
        body = SecretRequestCreate.model_validate_json(request.content)
        known = self._bindings.get(body.ref)
        if known is not None and known != body.bindings:
            return httpx.Response(409, json={"error": "binding_conflict"})
        self._bindings[body.ref] = body.bindings
        for other, ref in self.refs.items():
            if ref == body.ref and self.statuses[other] == "pending":
                self.statuses[other] = "superseded"
        request_id = f"req{next(self._ids)}"
        self.refs[request_id] = body.ref
        self.statuses[request_id] = "pending"
        return httpx.Response(201, json=self._view(request_id))

    def _get(self, request: httpx.Request, id: str) -> httpx.Response:
        failure = self._failure("get_secret_request")
        if failure is not None:
            return failure
        if id not in self.refs:
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json=self._view(id))
