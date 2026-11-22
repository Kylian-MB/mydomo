"""A page knows whose it is.

A page left open keeps what it showed: one person's conversation, their menu. When the browser's
session becomes someone else's meanwhile (they joined through an invitation in another tab, or
logged in), what that page asks would be done as the newcomer, under the first person's
conversation: in the owner's trial of 0.8.0, his messages were answered as the one who had just
joined.

So a page carries the id of the person it was rendered for (``PAGE_PERSON_HEADER``, sent with
every request of the page: htmx's, and those of ``chat.js`` and ``passkeys.js``), and a
request that carries another id than the session's person is not served: nothing is done, and
the answer makes the tab reload in full (``HX-Refresh``), so that it shows the session's person.
A request without it (a direct call, a stream: a browser cannot send a header with one) is served
as before.

The pilots talk with Domo on a live server (``chat_support.py``): a request of the adult's
browser that carries the owner's id is the owner's page, left open, in a browser whose session
became the adult's.
"""

import re
import uuid
from pathlib import Path

import domo_core
import pytest
from chat_support import ChatPilot, assistant_id_in, consume_sse, live_http_client
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Conversation
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.web.deps import PAGE_PERSON_HEADER
from sqlalchemy import func, select

STATIC_DIR = Path(domo_core.__file__).parent / "web" / "static"


def of(pilot: ChatPilot) -> dict[str, str]:
    """What a page rendered for ``pilot``'s person sends with each of its requests."""
    return {PAGE_PERSON_HEADER: str(pilot.person.id)}


def reloads(response) -> bool:
    """Whether ``response`` did nothing and makes the tab reload in full."""
    return (
        response.status_code == 409
        and response.headers.get("HX-Refresh") == "true"
        and response.text == ""
    )


async def conversations_of(pilot: ChatPilot) -> int:
    async with pilot.server.session_factory() as session:
        found = await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.person_id == pilot.person.id)
        )
        return found.scalar_one()


@pytest.mark.parametrize("path", ["/chat", "/account"])
async def test_a_page_carries_the_id_of_the_person_it_was_rendered_for(owner_chat, path):
    page = (await owner_chat.get(path)).text
    body = re.search(r"<body[^>]*>", page)
    assert body is not None
    mine = str(owner_chat.person.id)
    # For htmx, which sends it with every request of the page; and for the page's own code.
    assert f'hx-headers=\'{{"{PAGE_PERSON_HEADER}": "{mine}"}}\'' in body.group(0)
    assert f'data-page-person="{mine}"' in body.group(0)


async def test_a_page_nobody_is_logged_in_on_carries_nobodys_id(chat_server):
    async with live_http_client(chat_server.base_url) as anonymous:
        page = (await anonymous.get("/login")).text
    assert PAGE_PERSON_HEADER not in page and "data-page-person" not in page


async def test_a_message_sent_from_another_persons_page_is_not_sent(
    owner_chat, adult_chat, chat_server
):
    calls = len(chat_server.calls)
    sent = await adult_chat.post("/chat/messages", data={"text": "hello"}, headers=of(owner_chat))
    assert reloads(sent)
    # Nothing was stored, and no turn began: neither for the one nor for the other.
    assert await conversations_of(adult_chat) == 0 and await conversations_of(owner_chat) == 0
    assert len(chat_server.calls) == calls
    # Their own page sends it, and its reply comes.
    mine = await adult_chat.post("/chat/messages", data={"text": "hello"}, headers=of(adult_chat))
    assert mine.status_code == 200
    events = await consume_sse(adult_chat.client, f"/chat/stream/{assistant_id_in(mine.text)}")
    assert events[-1].event == "done"
    assert await conversations_of(adult_chat) == 1 and len(chat_server.calls) > calls


@pytest.mark.parametrize("path", ["/chat/since", f"/chat/since/{uuid.uuid4()}", "/approvals"])
async def test_what_an_open_page_asks_by_itself_notices_it_too(owner_chat, adult_chat, path):
    """The page asks for what is new every few seconds: a tab left open corrects itself even if
    nothing is typed in it."""
    asked = await adult_chat.get(path, headers={"HX-Request": "true", **of(owner_chat)})
    assert reloads(asked)


async def test_a_page_of_their_own_is_served_as_before(owner_chat, adult_chat):
    for pilot in (owner_chat, adult_chat):
        assert (await pilot.get("/chat/since", headers=of(pilot))).status_code == 200
        assert (await pilot.get("/chat/since")).status_code == 200
        assert (await pilot.get("/approvals", headers=of(pilot))).status_code == 200


async def test_the_buttons_of_another_persons_page_do_nothing(
    owner_chat, adult_chat, child_chat, confirmation_status, gk_mock
):
    # A child's request that waits for an adult: its button, on the owner's page left open, would
    # be pressed as the adult the session became.
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    assert rid is not None
    for decision in ("approve", "refuse"):
        pressed = await adult_chat.post(f"/confirmations/{rid}/{decision}", headers=of(owner_chat))
        assert reloads(pressed)
    assert await confirmation_status(rid) == "pending"
    # The form of a key, and a reply's stream.
    assert reloads(await adult_chat.get(KEY_NEW_PATH, headers=of(owner_chat)))
    assert reloads(
        await adult_chat.post(KEY_NEW_PATH, data={"kind": "openai"}, headers=of(owner_chat))
    )
    assert reloads(await adult_chat.get(f"/chat/stream/{uuid.uuid4()}", headers=of(owner_chat)))
    assert not gk_mock.secret_requests.called


async def test_a_session_that_ended_is_sent_to_the_login_page_as_before(chat_server, owner_chat):
    async with live_http_client(chat_server.base_url) as anonymous:
        asked = await anonymous.get("/chat/since", headers={"HX-Request": "true", **of(owner_chat)})
    assert asked.status_code == 401 and asked.headers["HX-Redirect"] == "/login"


async def test_a_passkey_is_never_added_from_another_persons_page(
    owner_chat, adult_chat, session_factory
):
    """Review M4: "add a passkey" on the owner's account page left open, in a browser whose
    session became the adult's, would register the authenticator at hand for the adult's account.
    The page says whose it is when it asks for the options: none are given, so no ceremony
    begins, and the tab reloads."""
    asked = await adult_chat.post(
        "/account/passkeys/options", data={"current": "whatever"}, headers=of(owner_chat)
    )
    assert reloads(asked)
    async with session_factory() as reader:
        # Nothing was checked nor written: not even a wrong password noted.
        failed = await reader.execute(
            select(func.count())
            .select_from(AuditEntry)
            .where(AuditEntry.action == "auth.passkey_add_failed")
        )
        assert failed.scalar_one() == 0
    # Their own page is answered by the route itself, as before: never told to reload.
    mine = await adult_chat.post(
        "/account/passkeys/options", data={"current": "whatever"}, headers=of(adult_chat)
    )
    assert mine.status_code != 409 and "HX-Refresh" not in mine.headers


def test_the_passkey_script_says_whose_page_it_is_and_reloads_when_told():
    script = (STATIC_DIR / "passkeys.js").read_text(encoding="utf-8")
    assert 'document.body.getAttribute("data-page-person")' in script
    assert f'headers["{PAGE_PERSON_HEADER}"] = pagePerson' in script
    assert 'response.headers.get("HX-Refresh") === "true"' in script
    assert "window.location.reload()" in script


def test_the_pages_own_requests_say_whose_page_it_is_and_reload_when_told():
    """``chat.js`` asks for what is new by itself (not through htmx): it sends the page's person
    as htmx does, and reloads the tab when the answer says so."""
    script = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert 'document.body.getAttribute("data-page-person")' in script
    assert f'headers["{PAGE_PERSON_HEADER}"] = pagePerson' in script
    assert 'response.headers.get("HX-Refresh") === "true"' in script
    assert "window.location.reload()" in script
