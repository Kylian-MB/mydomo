"""What the edge's ``Caddyfile`` says about the page's voice socket: read from the file, nothing
started. (``tests/stack`` drives the edge itself; these only read it, so they run everywhere.)

Over HTTP/2 (extended CONNECT, RFC 8441, which the edge's Caddy speaks), the bytes a WebSocket
sends up are the request's body: the core's general ``handle`` caps a body at 1 MB, which would end
a voice socket after about 31 s of speech in all. So the voice socket has handles of its own, with
no such cap (the core bounds each frame and each moment itself). Only a WebSocket request to
``/chat/voice`` reaches them: an HTTP/1.1 upgrade (``GET`` with ``Connection: Upgrade`` and
``Upgrade: websocket``) or an HTTP/2 extended CONNECT (``CONNECT`` with ``:protocol: websocket``,
which is how Caddy's own proxy tells one: ``r.Method == CONNECT`` and
``r.Header.Get(":protocol") == "websocket"``). Any other request to that path, a ``POST`` with a big
body say, falls through to the general handle and its cap.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CADDYFILE = ROOT / "deploy" / "edge" / "Caddyfile"


def site_route() -> str:
    """The ``route`` block of the site (the HTTPS site), comments taken out."""
    lines = [line.split("#", 1)[0].rstrip() for line in CADDYFILE.read_text("utf-8").splitlines()]
    text = "\n".join(line for line in lines if line.strip())
    site = text[text.index("{$DOMO_SITE_ADDRESSES") :]
    return site[site.index("route {") :]


def site() -> str:
    """The HTTPS site (the last one of the file), comments taken out."""
    lines = [line.split("#", 1)[0].rstrip() for line in CADDYFILE.read_text("utf-8").splitlines()]
    text = "\n".join(line for line in lines if line.strip())
    return text[text.index("{$DOMO_SITE_ADDRESSES") :]


def block(text: str, opening: str) -> str:
    """The block that opens with ``opening`` (its braces balanced)."""
    start = text.index(opening)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced block: {opening}")


def inside(text: str) -> list[str]:
    """The lines of a block, its opening and closing lines left out, stripped."""
    return [line.strip() for line in text.splitlines()[1:-1]]


def test_only_websocket_requests_match_the_voice_socket() -> None:
    upgrade = block(site(), "@voice_socket_h1 {")
    assert sorted(inside(upgrade)) == sorted(
        [
            "path /chat/voice",
            "method GET",
            "header Connection *Upgrade*",
            "header Connection *upgrade*",
            "header Upgrade websocket",
            "header Upgrade WebSocket",
        ]
    )
    connect = block(site(), "@voice_socket_h2 {")
    assert sorted(inside(connect)) == sorted(
        ["path /chat/voice", "method CONNECT", "header :protocol websocket"]
    )


def test_the_voice_socket_has_handles_of_its_own_without_a_body_cap() -> None:
    route = site_route()
    for name in ("@voice_socket_h1", "@voice_socket_h2"):
        handle = block(route, f"handle {name} {{")
        # Nothing else in it: no body cap, no header rule that could break the upgrade or drop
        # the cookie.
        assert inside(handle) == ["reverse_proxy core:8000"]
    # No handle that takes the path whatever the request.
    assert not re.search(r"handle\s+/chat/voice", route)


def test_the_voice_handles_come_before_the_general_one_and_after_the_refusal_of_my_tools() -> None:
    route = site_route()
    tools = route.index("import closed_to_my_tools")
    upgrade = route.index("handle @voice_socket_h1 {")
    connect = route.index("handle @voice_socket_h2 {")
    general = route.index("handle {")
    # In a "route", the directives run in the order written; the first "handle" that matches wins.
    assert tools < upgrade < general
    assert tools < connect < general


def test_the_rest_of_the_core_keeps_its_body_cap() -> None:
    general = block(site_route(), "handle {")
    assert re.search(r"max_size 1MB", general)
    assert "reverse_proxy core:8000" in general
