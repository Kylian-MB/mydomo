"""Jinja2 templates: every template can call ``t(key, **params)`` (the text the visitor reads,
from their ``Texts``: ``request.state.texts`` for a page, the reader's own for a fragment rendered
outside a request), read ``lang`` (the language of those texts, for ``<html lang>``) and
``domo`` (the name the reader calls Domo by, spec 6.7: "Domo" unless they gave him another;
a fragment rendered outside a request is given it, or reads "Domo"), and name a static file with
``static_url(path)``.

Autoescaping is on and a missing variable is an error, not an empty string. Templates carry no
inline script or style (the content security policy forbids them): code lives in ``static/``.
"""

from collections.abc import Mapping
from importlib.metadata import version
from typing import Any, Final
from urllib.parse import quote

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.requests import Request
from starlette.responses import Response

from domo_core.texts.view import Texts
from domo_core.web.language import DOMO_STATE_KEY, request_texts

# The version of the core, in the address of every static file (``static_url``).
STATIC_VERSION: Final = version("domo-core")


def static_url(path: str) -> str:
    """The address of ``path``, a file of ``web/static``, with the version of the core in its
    query (``/static/app.css?v=0.1.0``). An update changes the address, so a browser that kept the
    earlier file in its cache asks for the new one, instead of running an old script against a new
    page. The file is served whatever the query says. Every template names its files this way."""
    return f"/static/{path}?v={quote(STATIC_VERSION, safe='')}"


def _language_context(texts: Texts) -> dict[str, Any]:
    """What every template reads of ``texts``: ``t`` (a text, its holes filled from the template's
    values) and ``lang`` (their language)."""

    def translate(key: str, /, **params: Any) -> str:
        return texts(key, **params)

    return {"lang": texts.language, "t": translate, "static_url": static_url}


def domo_of(calls_domo: str | None) -> str:
    """The name a reader's pages call Domo by: theirs, else Domo."""
    return calls_domo or "Domo"


def _i18n_context(request: Request) -> dict[str, Any]:
    # The texts ``web.texts_middleware`` read for the request (``request.state.texts``), and the
    # name the reader calls Domo by, read with them.
    calls_domo = request.scope.get("state", {}).get(DOMO_STATE_KEY)
    return {
        **_language_context(request_texts(request)),
        "domo": domo_of(calls_domo if isinstance(calls_domo, str) else None),
    }


def create_templates() -> Jinja2Templates:
    """The templates of ``web/templates``, with ``t``, ``lang`` and ``static_url`` available in
    all of them."""
    environment = Environment(
        loader=PackageLoader("domo_core", "web/templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return Jinja2Templates(env=environment, context_processors=[_i18n_context])


def render(
    request: Request,
    template: str,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    **context: Any,
) -> Response:
    """Render ``template`` (a page, or a fragment for htmx) with ``context``."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request, template, context, status_code=status_code, headers=headers
    )


def render_fragment(templates: Jinja2Templates, template: str, texts: Texts, **context: Any) -> str:
    """``template`` rendered to text with ``texts``, outside a request: for the pieces of a page
    that a stream sends once its response has started (the chat's action cards)."""
    return templates.get_template(template).render({**_language_context(texts), **context})
