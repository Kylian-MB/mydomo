"""The public web app of the gatekeeper: the one-time form where the owner enters a secret.

It is served through the edge proxy and embedded as an iframe in the core's page, but it is the
gatekeeper that renders it and receives the submission, so the value goes straight into the
vault and never transits through the core. The form is plain HTML without any JavaScript. The
value is never logged, echoed back or kept anywhere but the vault, and every response, errors
included, carries headers that keep it out of caches, referrers and foreign frames.

What its pages say (spec 4.5): the texts the core sent with the request, in the words of the one
who gives the secret, and nothing else (``_words``); their holes (``{max}``) are filled here,
with the gatekeeper's own values, as they are. A request sent without texts (the installation)
shows the gatekeeper's fixed texts, in the language the browser prefers, English or French. A page
for a request the gatekeeper does not know (a link expired, used, or that never was) shows a sign
without words: Domo's page, around the form, says what it is about. Each address the secret
will be sent to is shown by the gatekeeper itself, never by a text: a symbol, the host in bold, the
header in code, without words.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from domo_common.i18n import negotiate_language, t
from domo_common.meanings import SIGN
from domo_common.text_rules import HOLE
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, StrictUndefined

from domo_gatekeeper.errors import (
    BindingMismatchError,
    InvalidSecretValueError,
    RequestNotPendingError,
)
from domo_gatekeeper.secret_requests import FORM_PATH_PREFIX, PendingRequest, SecretRequestStore
from domo_gatekeeper.vault import Vault

MAX_VALUE_LENGTH = 8192
# 8192 characters of 4-byte UTF-8, each percent-encoded as 3 characters per byte, fits in 100 KiB.
MAX_BODY_BYTES = 128 * 1024

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'self'; form-action 'self'",
    "X-Domo-Component": "gatekeeper",
}

# Autoescaping is on: the purpose, the texts and the destinations come from the core, not from
# the owner.
_templates = Environment(
    loader=PackageLoader("domo_gatekeeper", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


class _Say(Protocol):
    """What a page says for ``key``, its holes filled with ``values``."""

    def __call__(self, key: str, /, **values: str) -> str: ...


def _filled(text: str, values: Mapping[str, str]) -> str:
    """``text`` with each hole the gatekeeper has a value for filled with it, in one pass: a
    value is put as it is, and a hole it has no value for is left as it is written."""
    return HOLE.sub(lambda hole: values.get(hole.group(1), hole.group(0)), text)


def _words(request: Request, pending: PendingRequest | None) -> tuple[str, _Say]:
    """The language of a page and what it says (see the module docstring): the texts of
    ``pending`` (a text it lacks is the sign); the fixed texts for a request sent without; the
    sign alone for a request the gatekeeper does not know (``None``)."""
    browser = negotiate_language(request.headers.get("accept-language"))

    def sign(key: str, /, **values: str) -> str:
        return SIGN

    def fixed(key: str, /, **values: str) -> str:
        return t(key, browser, **values)

    if pending is None:
        return browser, sign
    texts, language = pending.texts, pending.language
    if texts is None or language is None:
        return browser, fixed

    def sent(key: str, /, **values: str) -> str:
        return _filled(texts.get(key, SIGN), values)

    return language, sent


def _render(
    request: Request,
    template: str,
    pending: PendingRequest | None,
    status_code: int = 200,
    **context: Any,
) -> Response:
    lang, say = _words(request, pending)
    html = _templates.get_template(template).render(lang=lang, t=say, **context)
    return HTMLResponse(html, status_code=status_code)


def _notice(
    request: Request, status_code: int, message_key: str, pending: PendingRequest | None
) -> Response:
    return _render(request, "secret_notice.html", pending, status_code, message_key=message_key)


def _form(request: Request, pending: PendingRequest, error_key: str | None = None) -> Response:
    destinations = [
        {
            "host": f"{b.host}:{b.port}" if b.port is not None else b.host,
            "header": b.location.name,
        }
        for b in pending.bindings
    ]
    return _render(
        request,
        "secret_form.html",
        pending,
        400 if error_key else 200,
        purpose=pending.purpose,
        destinations=destinations,
        max_length=MAX_VALUE_LENGTH,
        error_key=error_key,
    )


async def _pending_of(store: SecretRequestStore, request: Request) -> PendingRequest | None:
    """The request whose form the address of ``request`` is, while it can still be fulfilled:
    for the pages said before its route runs, or once it failed. ``None`` when the address is no
    form's, or when the store does not answer."""
    token = request.url.path.removeprefix(FORM_PATH_PREFIX)
    if token == request.url.path or not token or "/" in token:
        return None
    try:
        return await run_in_threadpool(store.pending_by_token, token)
    except Exception:
        return None


def _harden(response: Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value


def create_public_app(store: SecretRequestStore, vault: Vault) -> FastAPI:
    # No API docs or schema: the only surface is the form.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def harden(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        declared = request.headers.get("content-length", "")
        if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
            response = _notice(request, 413, "vault.too_large", await _pending_of(store, request))
        else:
            response = await call_next(request)
        _harden(response)
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Response:
        # Outside the middleware above, so the headers are set here too. Starlette re-raises
        # the exception afterwards, for the server to log; its message carries no secret.
        response = _notice(request, 500, "vault.error", await _pending_of(store, request))
        _harden(response)
        return response

    @app.get(FORM_PATH_PREFIX + "{token}")
    async def show_form(token: str, request: Request) -> Response:
        pending = await run_in_threadpool(store.pending_by_token, token)
        if pending is None:
            return _notice(request, 404, "vault.unavailable", None)
        return _form(request, pending)

    @app.post(FORM_PATH_PREFIX + "{token}")
    async def submit(token: str, request: Request) -> Response:
        pending = await run_in_threadpool(store.pending_by_token, token)
        if pending is None:
            return _notice(request, 404, "vault.unavailable", None)
        async with request.form() as form:
            field = form.get("value")
        # Anything but text (a missing field, a file upload) counts as an empty value.
        value = field if isinstance(field, str) else ""
        if not 1 <= len(value) <= MAX_VALUE_LENGTH:
            return _form(request, pending, "vault.form.error.length")
        try:
            await run_in_threadpool(store.fulfil, token, value, vault)
        except RequestNotPendingError:
            # Used or expired since it was read: its own words say so.
            return _notice(request, 404, "vault.unavailable", pending)
        except InvalidSecretValueError:
            return _form(request, pending, "vault.form.error.invalid")
        except BindingMismatchError:
            return _notice(request, 409, "vault.conflict", pending)
        return _render(request, "secret_saved.html", pending)

    return app
