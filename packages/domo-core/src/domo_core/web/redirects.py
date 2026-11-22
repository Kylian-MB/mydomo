"""Sending a visitor somewhere else, for pages and for htmx requests alike."""

from starlette.datastructures import Headers
from starlette.responses import RedirectResponse, Response


def redirect_to(request_headers: Headers, location: str) -> Response:
    """The answer that sends the visitor to ``location``.

    A browser gets a 303. htmx would follow a redirect inside its request and swap the page it
    lands on into the middle of the current one, so an htmx request is answered with a 401 and
    an ``HX-Redirect`` header, which makes it navigate instead.
    """
    if request_headers.get("hx-request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=303)
