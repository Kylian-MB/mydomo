"""The texts the reader of a request reads (``request_texts``): those ``web.texts_middleware`` read
for it before its route ran: the fixed catalog of its browser's language during the installation,
what Domo wrote for its reader afterwards."""

from typing import Final

from starlette.requests import Request

from domo_core.texts.view import Texts

# Where the texts of a request are kept (``request.state.texts``).
STATE_KEY: Final = "texts"
# Where the middleware keeps the name the reader sees for Domo (their own, else the house's, else
# "Domo": ``naming.name_shown``), for the texts of their pages that name him (``web.templating``).
DOMO_STATE_KEY: Final = "calls_domo"


def request_texts(request: Request) -> Texts:
    """The texts the reader of ``request`` reads: every text a route shows comes from them.
    ``RuntimeError`` for a request the texts middleware did not see (a static file)."""
    texts: Texts | None = request.scope.get("state", {}).get(STATE_KEY)
    if texts is None:
        raise RuntimeError("the texts of this request were not read (web.texts_middleware)")
    return texts


def use_texts(request: Request, texts: Texts) -> None:
    """Have what is left of ``request`` read ``texts``: for a route that learnt in which language
    its page is read (the page of a link, in the language written for it then)."""
    request.scope.setdefault("state", {})[STATE_KEY] = texts
