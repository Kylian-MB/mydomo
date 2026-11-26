"""Chatting with Domo: the page, posting a message, and the reply that streams.

The application runs under a real uvicorn server (see ``chat_support.py``), with sessions that
commit for real, as in production: a stream is a matter of timing, and of connections that come
and go. What the database holds is read with the rolled-back ``session`` fixture, which sees
whatever the server has committed.
"""

import asyncio
import html
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import domo_core
import pytest
from chat_support import (
    WAIT_SECONDS,
    SseEvent,
    assistant_id_in,
    consume_sse,
    get_message,
    last_audit,
    live_http_client,
    log_in_live,
    parse_sse,
    post_message,
    read_raw_stream,
    read_stream,
    tool_results,
    turn_tools,
    wait_for_status,
)
from domo_common.i18n import t
from domo_core.accounts.models import Account, Person
from domo_core.audit.log import verify_audit_chain
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.sse import RelaySink
from domo_core.chat.models import Message
from domo_core.chat.store import ConversationStore
from domo_core.conversation import turn as conversation_turn
from domo_core.conversation.service import STALE_AFTER
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.types import ChatChunk, ChatMessage, ChatUsage, ErrorKind, ToolCall
from domo_core.prompts import (
    HouseholdLine,
    KeyShown,
    KeysNote,
    MemoryBlock,
    ModelsNote,
    build_system_prompt,
    today_of,
)
from domo_core.proofs.service import agree
from domo_core.setup_flow.middleware import mark_setup_done
from domo_core.trust.service import Trust
from domo_core.web.deps import get_clock, get_gatekeeper, get_model_factory, get_settings
from domo_core.web.templating import static_url
from fakes import FakeChatModel, fake_factory, is_judge_call
from sqlalchemy import select, text, update
from web_support import OWNER_PASSWORD, NoWriting, house_reads, in_language, reads_in

STATIC_DIR = Path(domo_core.__file__).parent / "web" / "static"


@pytest.fixture
def clock(clock):
    """The test clock, set to the real time: the times of the messages come from the database,
    and the age of a reply is the difference between the two."""
    clock.advance(datetime.now(UTC) - clock())
    return clock


@pytest.fixture
def app(app_factory, clock, gk_client):
    """The application with sessions that commit for real (production's), the first launch over,
    the test clock, a gatekeeper client (the tools are given one; nothing here calls it) and,
    unless a test chooses (``use_model``), a model that says one word. Nobody's texts are written
    in the background (``NoWriting``): these tests count what one turn does (the streams of the
    model, the entries of the journal), and the pages show the house's version."""
    app = app_factory()
    mark_setup_done(app)
    app.state.text_writing = NoWriting()
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_gatekeeper] = lambda: gk_client
    app.dependency_overrides[get_model_factory] = lambda: fake_factory(FakeChatModel(["Hi"]))
    return app


@pytest.fixture
async def owner(add_owner_in_database) -> Account:
    return await add_owner_in_database()


# The model of conversation of the owner's key: the cheapest one of the level a conversation
# starts at (``medium``) among the ones it lists.
CHAT_MODEL = "example-sonnet-1"
# The key and the model the conversation talks on, as Domo is told them: a server of the
# household's own, whose price is known (nothing).
GENERAL_LABEL = "openai_compatible key of Owner, general"
CHAT_TALK = ModelsNote(key_label=GENERAL_LABEL, model_id=CHAT_MODEL, level="medium")


async def add_key(
    session_factory, giver: uuid.UUID, *, general: bool, models: tuple[str, ...]
) -> Provider:
    """A key of ``giver``, committed, that lists ``models``."""
    async with session_factory() as writer:
        row = Provider(
            id=new_provider_id(),
            kind="openai_compatible",
            base_url="http://llm.example.com/v1",
            given_by=giver,
            is_general=general,
        )
        writer.add(row)
        await writer.flush()
        writer.add_all(ProviderModel(provider_id=row.id, model_id=m) for m in models)
        await writer.commit()
        return row


@pytest.fixture
async def provider(session_factory, owner) -> Provider:
    """The household's general key, the owner's, which lists a small and a medium model."""
    return await add_key(
        session_factory, owner.person_id, general=True, models=("example-haiku-1", CHAT_MODEL)
    )


@pytest.fixture
async def chat_client_without_provider(live_client, owner):
    """The owner, logged in, on an instance whose provider row is missing."""
    await log_in_live(live_client)
    return live_client


@pytest.fixture
async def chat_client(chat_client_without_provider, provider):
    """The owner, logged in, on the live server."""
    return chat_client_without_provider


@pytest.fixture
async def anonymous_client(live_base_url):
    """A browser nobody is logged in on."""
    async with live_http_client(live_base_url) as client:
        yield client


@asynccontextmanager
async def logged_in_member(
    live_base_url, session_factory, password_hash, *, display_name: str, role: str, username: str
):
    """Another person (not the owner), logged in on the live server with a browser of their own."""
    async with session_factory() as writer:
        person = Person(display_name=display_name, role=role)
        writer.add(person)
        await writer.flush()
        writer.add(Account(person_id=person.id, username=username, password_hash=password_hash))
        await writer.flush()
        await agree(writer, person.id, "password", proposed_by="code", now=datetime.now(UTC))
        await writer.commit()
    async with live_http_client(live_base_url) as client:
        await log_in_live(client, username, OWNER_PASSWORD)
        yield client


@pytest.fixture
async def member_client(chat_client, live_base_url, session_factory, owner_password_hash):
    """An adult, logged in on the same server."""
    async with logged_in_member(
        live_base_url,
        session_factory,
        owner_password_hash,
        display_name="Member",
        role="adult",
        username="member",
    ) as client:
        yield client


@pytest.fixture
async def child_client(chat_client, live_base_url, session_factory, owner_password_hash):
    """A child, logged in on the same server."""
    async with logged_in_member(
        live_base_url,
        session_factory,
        owner_password_hash,
        display_name="Kid",
        role="child",
        username="kid",
    ) as client:
        yield client


def plain(page: str) -> str:
    """The text of a page as a person reads it: entities decoded."""
    return html.unescape(page)


def event_names(events: list[SseEvent]) -> list[str]:
    return [event.event for event in events]


async def message_rows(session) -> list[Any]:
    result = await session.execute(
        text("SELECT role, content, status, provenance, model FROM messages ORDER BY created_at")
    )
    return list(result.all())


# --- the page ---------------------------------------------------------------------------------


async def test_the_chat_page_is_for_people_who_are_logged_in(anonymous_client):
    r = await anonymous_client.get("/chat")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_an_empty_conversation_shows_no_fixed_welcome(chat_client):
    """The welcome block of 0.4.0 is gone (spec 4.9): the page of an empty conversation holds no
    message, and when it opens Domo may write first himself (``test_arrival.py``)."""
    r = await chat_client.get("/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"
    assert 'class="message welcome"' not in r.text
    assert "message assistant" not in r.text and "message user" not in r.text
    assert "sse-connect" not in r.text


async def test_the_page_of_an_empty_conversation_is_in_the_persons_language(
    chat_client, owner, session_factory
):
    async with session_factory() as writer:
        await reads_in(writer, owner.person_id, "fr")
        await writer.commit()
    r = await chat_client.get("/chat")
    assert in_language("fr", "chat.send") in plain(r.text)


async def test_opening_the_page_sends_nothing_to_the_model(chat_client, use_model, session):
    model = FakeChatModel(["ok"])
    use_model(model)
    assert (await chat_client.get("/chat")).status_code == 200
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert [call.messages for call in model.calls] == [[ChatMessage("user", "hi")]]


async def test_the_page_has_the_form_the_end_to_end_tests_use(chat_client):
    page = (await chat_client.get("/chat")).text
    form = re.search(r'<form\b[^>]*\bid="chat-form"[^>]*>(.*?)</form>', page, re.DOTALL)
    assert form is not None
    assert re.search(r'<textarea\b[^>]*\bname="text"', form.group(1))
    send = re.search(r'<button\b[^>]*\btype="submit"[^>]*>', form.group(1))
    assert send is not None and 'data-action="send"' in send.group(0)
    assert 'hx-post="/chat/messages"' in form.group(0)
    assert 'maxlength="8000"' in form.group(1)


async def test_the_page_has_the_navigation_and_the_chat_script(chat_client):
    page = (await chat_client.get("/chat")).text
    assert 'action="/logout"' in page  # the top bar: a person is logged in
    assert f'<script src="{static_url("chat.js")}" defer></script>' in page
    assert 'id="messages"' in page and 'id="chat-notice"' in page


async def test_the_history_shows_both_sides_in_order(chat_client, use_model):
    use_model(FakeChatModel(["It ", "works"]))
    msg_id = await post_message(chat_client, "Does it work?")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    page = (await chat_client.get("/chat")).text
    bubbles = re.findall(
        r'<div class="message (user|assistant)"[^>]*>\s*<div class="message-text">(.*?)</div>',
        page,
        re.DOTALL,
    )
    assert bubbles == [("user", "Does it work?"), ("assistant", "It works")]
    assert "sse-connect" not in page  # a finished reply does not open a stream again


async def test_replies_that_did_not_end_well_say_so_in_the_history(
    chat_client, session_factory, owner
):
    async with session_factory() as writer:
        store = ConversationStore(writer)
        conversation = await store.active_conversation(owner.person_id)
        await store.add_message(
            conversation.id, role="user", content="q1", status="complete", provenance="person"
        )
        await store.add_message(
            conversation.id,
            role="assistant",
            content="half an ans",
            status="partial",
            provenance="model",
            model="example-model-1",
        )
        await store.add_message(
            conversation.id, role="user", content="q2", status="complete", provenance="person"
        )
        await store.add_message(
            conversation.id,
            role="assistant",
            content="",
            status="error",
            provenance="model",
            model="example-model-1",
        )
        await writer.commit()
    page = plain((await chat_client.get("/chat")).text)
    assert "half an ans" in page
    assert t("chat.status.partial", "en") in page
    assert t("chat.status.error", "en") in page


async def test_the_history_is_the_one_of_the_person(chat_client, member_client, use_model):
    use_model(FakeChatModel(["reply"]))
    msg_id = await post_message(chat_client, "owner secret question")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert "owner secret question" in (await chat_client.get("/chat")).text
    assert "owner secret question" not in (await member_client.get("/chat")).text


_BUBBLE = re.compile(
    r'<div class="message (user|assistant)"[^>]*data-message-id="([0-9a-f-]{36})"[^>]*>\s*'
    r'<div class="message-text">(.*?)</div>',
    re.DOTALL,
)


async def test_a_reply_still_being_written_is_not_on_the_page_until_it_is_over(
    chat_client, use_model
):
    use_model(FakeChatModel(["Here I am"]))
    reply_id = await post_message(chat_client, "Are you there?")
    page = (await chat_client.get("/chat")).text
    # An empty bubble that nothing fills would stay empty: the page shows it once it is over.
    assert [(side, text) for side, _, text in _BUBBLE.findall(page)] == [("user", "Are you there?")]
    await consume_sse(chat_client, f"/chat/stream/{reply_id}")
    page = (await chat_client.get("/chat")).text
    assert [(side, text) for side, _, text in _BUBBLE.findall(page)] == [
        ("user", "Are you there?"),
        ("assistant", "Here I am"),
    ]


async def test_since_gives_the_bubbles_that_came_after_once_they_are_over(chat_client, use_model):
    use_model(FakeChatModel(["first answer"]))
    first = await post_message(chat_client, "one")
    await consume_sse(chat_client, f"/chat/stream/{first}")
    nothing = await chat_client.get(f"/chat/since/{first}")
    assert (nothing.status_code, nothing.text.strip()) == (200, "")
    assert nothing.headers["cache-control"] == "no-store"
    use_model(FakeChatModel(["second answer"]))
    second = await post_message(chat_client, "two")
    # The person's message is over, the reply is not: only the message.
    during = (await chat_client.get(f"/chat/since/{first}")).text
    assert [(side, text) for side, _, text in _BUBBLE.findall(during)] == [("user", "two")]
    await consume_sse(chat_client, f"/chat/stream/{second}")
    after = (await chat_client.get(f"/chat/since/{first}")).text
    assert [(side, text) for side, _, text in _BUBBLE.findall(after)] == [
        ("user", "two"),
        ("assistant", "second answer"),
    ]
    assert _BUBBLE.findall(after)[1][1] == second
    assert "<html" not in after.lower() and "sse-connect" not in after
    assert (await chat_client.get(f"/chat/since/{second}")).text.strip() == ""


async def test_since_is_only_for_the_persons_own_messages(chat_client, member_client, use_model):
    use_model(FakeChatModel(["private"]))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert (await member_client.get(f"/chat/since/{msg_id}")).status_code == 404
    for unknown in ("00000000-0000-4000-8000-000000000000", "not-a-uuid", "1"):
        assert (await chat_client.get(f"/chat/since/{unknown}")).status_code == 404


async def test_the_page_asks_for_what_came_after_every_five_seconds():
    script = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert "SINCE_EVERY_MS = 5000" in script
    assert '"/chat/since/"' in script


def test_enter_sends_the_message_and_shift_enter_adds_a_line_in_the_script():  # scenario 19
    """The browser behaviour itself is the end-to-end test's (``tests/e2e/test_household.py``):
    here, that the script listens to the keys of the text box and sends the form as a click of the
    button would, never during a composition (accents, the keyboard of an Asian language), and
    not on a touch screen, whose keyboard has no Shift+Enter: Enter adds a line there, and the
    button sends, as in a phone's messaging app."""
    script = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert 'text.addEventListener("keydown"' in script
    assert "form.requestSubmit()" in script
    assert "event.shiftKey" in script and "event.isComposing" in script
    assert 'matchMedia("(hover: none) and (pointer: coarse)")' in script


def test_the_page_asks_for_what_is_new_from_before_the_first_bubble_being_written():
    """A message sent once a reply shown being written is over (before the next round asks) adds
    its pair after that bubble: asked from the last bubble, the answer would not hold the reply
    any more, and the page would take its bubble away (review Important 2). The page asks from
    the bubble just before the first one being written: the answer holds that reply, over."""
    script = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert "function askFrom()" in script
    assert 'messages.querySelectorAll("[data-message-id]:not([data-writing])")' not in script


# --- posting a message -------------------------------------------------------------------------


async def test_posting_opens_a_pair_of_bubbles_and_a_stream(chat_client, provider, session):
    r = await chat_client.post("/chat/messages", data={"text": "hello there"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"
    assert len(re.findall(r'class="message user"', r.text)) == 1
    assert len(re.findall(r'class="message assistant"', r.text)) == 1
    assert "hello there" in r.text
    msg_id = assistant_id_in(r.text)
    assistant = re.search(r'<div class="message assistant"[^>]*>', r.text)
    assert assistant is not None
    for attribute in ('hx-ext="sse"', f'sse-connect="/chat/stream/{msg_id}"', 'sse-close="done"'):
        assert attribute in assistant.group(0)
    delta = re.search(r'<[^>]*\bsse-swap="delta"[^>]*>', r.text)
    assert delta is not None and 'hx-swap="beforeend"' in delta.group(0)
    assert re.search(r'<[^>]*\bsse-swap="error"[^>]*>', r.text)
    rows = await message_rows(session)
    assert [tuple(row) for row in rows] == [
        ("user", "hello there", "complete", "person", None),
        # The model is written once the turn is over (the one of its last call).
        ("assistant", "", "streaming", "model", None),
    ]


async def test_the_pair_is_a_fragment_that_clears_the_notice(chat_client):
    r = await chat_client.post(
        "/chat/messages", data={"text": "hi"}, headers={"hx-request": "true"}
    )
    assert "<html" not in r.text.lower()
    assert re.search(r'<div id="chat-notice"[^>]*hx-swap-oob="true"', r.text)


async def test_posted_text_is_escaped_in_the_bubble(chat_client):
    r = await chat_client.post(
        "/chat/messages",
        data={"text": '<img src=x onerror=alert(1)> <script>alert(2)</script> & "q"'},
    )
    assert "<img" not in r.text and "<script>" not in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text
    assert "&lt;script&gt;" in r.text


async def test_the_text_keeps_its_line_breaks_with_unix_endings(chat_client, session):
    # A browser sends the line breaks of a text box as CRLF.
    await post_message(chat_client, "first line\r\nsecond line\rthird")
    (user, _) = await message_rows(session)
    assert user.content == "first line\nsecond line\nthird"


async def test_a_text_of_the_maximum_length_is_accepted(chat_client):
    await post_message(chat_client, "x" * 8000)


@pytest.mark.parametrize("value", ["", "   ", " \n\t ", "x" * 8001, "a\x00b"])
async def test_a_text_that_cannot_be_sent_is_refused_with_an_explanation(
    chat_client, session, value
):
    r = await chat_client.post("/chat/messages", data={"text": value})
    assert r.status_code == 400
    assert t("chat.error.text", "en", max="8000") in plain(r.text)
    assert r.headers["hx-retarget"] == "#chat-notice"
    assert r.headers["hx-reswap"] == "innerHTML"
    assert await message_rows(session) == []


async def test_a_missing_text_is_refused_too(chat_client, session):
    r = await chat_client.post("/chat/messages", data={})
    assert r.status_code == 400
    assert await message_rows(session) == []


async def test_the_refusal_is_translated(chat_client, owner, session_factory):
    async with session_factory() as writer:
        await reads_in(writer, owner.person_id, "fr")
        await writer.commit()
    r = await chat_client.post("/chat/messages", data={"text": ""})
    assert in_language("fr", "chat.error.text", max="8000") in plain(r.text)


async def test_a_person_without_a_key_is_told_so(
    chat_client_without_provider, session, session_factory
):
    # The only key is someone else's, and its giver shares it with nobody.
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="adult")
        writer.add(alex)
        await writer.commit()
    await add_key(session_factory, alex.id, general=False, models=(CHAT_MODEL,))
    r = await chat_client_without_provider.post("/chat/messages", data={"text": "hi"})
    assert r.status_code == 409
    assert t("chat.error.no_key", "en") in plain(r.text)
    # With no general key to ask them for one, the message opens the form of a key of their own.
    assert f'data-action="key-new" hx-get="{KEY_NEW_PATH}"' in r.text
    assert await message_rows(session) == []


async def test_the_reply_says_the_model_that_answered(chat_client, use_model, session, provider):
    use_model(FakeChatModel(["Hello"], usage=ChatUsage(7, 2)))
    msg_id = await post_message(chat_client, "hi")
    # Before the turn, no model has answered yet.
    assert (await get_message(session, msg_id)).model is None
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    message = await get_message(session, msg_id)
    assert (message.content, message.model) == ("Hello", CHAT_MODEL)
    entry = await last_audit(session, "chat.turn")
    assert entry.details == {
        "provider_id": provider.id,
        "model": CHAT_MODEL,
        "level": "medium",
        "status": "complete",
        "input_tokens": 7,
        "output_tokens": 2,
        # A server of the household's own (no key, a local address) costs nothing.
        "cost_usd": "0.000000",
        "estimated": False,
    }


async def test_a_visitor_cannot_post(anonymous_client, owner, provider):
    r = await anonymous_client.post("/chat/messages", data={"text": "hi"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    r = await anonymous_client.post(
        "/chat/messages", data={"text": "hi"}, headers={"hx-request": "true"}
    )
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"


async def test_a_request_from_another_site_is_refused(chat_client, session):
    r = await chat_client.post(
        "/chat/messages", data={"text": "hi"}, headers={"origin": "https://evil.example.net"}
    )
    assert r.status_code == 403
    assert await message_rows(session) == []


async def test_second_message_while_a_reply_is_written_is_kept(chat_client, use_model, session):
    # Spec 11.3: never refused. Its bubble alone, at once; no reply of its own is opened.
    use_model(FakeChatModel(["x"]))
    await post_message(chat_client, "first")
    r = await chat_client.post("/chat/messages", data={"text": "second"})
    assert r.status_code == 202
    assert 'class="message user"' in r.text and "second" in plain(r.text)
    assert "sse-connect" not in r.text and "hx-retarget" not in r.headers
    rows = await message_rows(session)
    assert [(row.role, row.content, row.status) for row in rows] == [
        ("user", "first", "complete"),
        ("assistant", "", "streaming"),
        ("user", "second", "complete"),
    ]


async def test_stale_streaming_message_does_not_block(chat_client, session, clock):
    first_id = await post_message(chat_client, "first")
    clock.advance(timedelta(minutes=3))
    r = await chat_client.post("/chat/messages", data={"text": "second"})
    assert r.status_code == 200
    assert (await get_message(session, first_id)).status == "error"


async def test_a_reply_that_is_not_two_minutes_old_still_holds_the_message(chat_client, clock):
    await post_message(chat_client, "first")
    clock.advance(timedelta(minutes=1, seconds=50))
    # Kept for that reply, which may still be written.
    assert (await chat_client.post("/chat/messages", data={"text": "second"})).status_code == 202
    clock.advance(timedelta(seconds=30))
    # Nobody writes it any more: a new reply answers what was said since.
    assert (await chat_client.post("/chat/messages", data={"text": "third"})).status_code == 200


async def test_two_messages_posted_at_once_open_only_one_reply(chat_client, use_model, session):
    use_model(FakeChatModel(["ok"]))
    opening = await post_message(chat_client, "opening")  # the conversation exists from here on
    await consume_sse(chat_client, f"/chat/stream/{opening}")
    first, second = await asyncio.gather(
        chat_client.post("/chat/messages", data={"text": "one"}),
        chat_client.post("/chat/messages", data={"text": "two"}),
    )
    # One opens the reply, the other is kept for it.
    assert sorted([first.status_code, second.status_code]) == [200, 202]
    assert len(await message_rows(session)) == 5


async def test_a_message_waits_for_whoever_else_is_working_on_the_conversation(
    chat_client, use_model, session_factory
):
    """Checking that no reply is streaming and opening one are one step: a request that finds
    the conversation busy (its row locked) waits, and then sees what the other one did."""
    use_model(FakeChatModel(["ok"]))
    opening = await post_message(chat_client, "opening")
    await consume_sse(chat_client, f"/chat/stream/{opening}")
    async with session_factory() as other:
        # A lock that the foreign key checks of other inserts do not conflict with: only the
        # conversation's own lock does.
        await other.execute(text("SELECT id FROM conversations FOR NO KEY UPDATE"))
        waiting = asyncio.create_task(chat_client.post("/chat/messages", data={"text": "hi"}))
        await asyncio.sleep(0.5)
        assert not waiting.done()
        await other.rollback()
    assert (await waiting).status_code == 200


async def test_a_new_message_can_be_posted_once_the_reply_is_over(chat_client, use_model):
    use_model(FakeChatModel(["ok"]))
    first = await post_message(chat_client, "first")
    await consume_sse(chat_client, f"/chat/stream/{first}")
    second = await post_message(chat_client, "second")  # no waiting: the reply is settled
    assert first != second


async def test_each_person_has_a_conversation_of_their_own(
    chat_client, member_client, session, owner
):
    await post_message(chat_client, "mine")
    await post_message(member_client, "theirs")  # the owner's open reply does not block them
    result = await session.execute(
        text(
            "SELECT c.person_id, m.content FROM messages m JOIN conversations c "
            "ON c.id = m.conversation_id WHERE m.role = 'user'"
        )
    )
    rows = [tuple(row) for row in result.all()]
    assert len(rows) == 2 and len({person for person, _ in rows}) == 2
    assert (owner.person_id, "mine") in rows


# --- the reply --------------------------------------------------------------------------------


async def test_post_then_stream_persists_assistant_reply(chat_client, use_model, session):
    use_model(FakeChatModel(["Hel", "lo"]))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    message = await get_message(session, msg_id)
    assert message.content == "Hello"
    assert message.status == "complete"


async def test_the_reply_arrives_as_deltas_then_done(chat_client, use_model):
    use_model(FakeChatModel(["Hel", "lo"]))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert events == [SseEvent("delta", "Hel"), SseEvent("delta", "lo"), SseEvent("done", "")]


async def test_the_stream_is_not_cached_or_buffered(chat_client, use_model):
    use_model(FakeChatModel(["x"]))
    msg_id = await post_message(chat_client, "hi")
    async with chat_client.stream("GET", f"/chat/stream/{msg_id}") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["content-security-policy"].startswith("default-src 'self'")
        await response.aread()


async def test_the_reply_is_streamed_as_it_comes(chat_client, use_model, session):
    use_model(FakeChatModel(["a", "b", "c"], delay_s=1.0))
    msg_id = await post_message(chat_client, "hi")
    loop = asyncio.get_running_loop()
    started = loop.time()
    first = await consume_sse(chat_client, f"/chat/stream/{msg_id}", stop_after_events=1)
    assert first == [SseEvent("delta", "a")]
    assert loop.time() - started < 0.8  # the first piece did not wait for the others
    await wait_for_status(session, msg_id, {"complete"})  # and the reply went on to its end


async def test_the_model_gets_the_prompt_the_history_and_the_limit(
    chat_client, use_model, settings, clock, provider
):
    model = FakeChatModel(["ok"])
    use_model(model)
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    # The owner of the conftest's instance, who has tools to act with, has chosen no successor
    # yet.
    expected = build_system_prompt(
        abilities=PAGE,
        display_name="Owner",
        role="owner",
        can_act=True,
        **turn_tools("owner"),
        successors=[],
        today=today_of(clock(), None),
        household=(HouseholdLine("Owner", "owner", is_you=True),),
        # No model of meanings in the pilots: the search works by words alone.
        memory=MemoryBlock(search="words_only"),
        models=CHAT_TALK,
        keys=KeysNote(listed=(KeyShown(GENERAL_LABEL, provider.id, yours=True),)),
        reads="en",
        # Nobody's word has been weighed yet: the household's start (trust per person).
        trust=Trust(percent=70, facts=0),
    )
    assert [call.system for call in model.calls] == [expected]
    assert [call.messages for call in model.calls] == [[ChatMessage("user", "hi")]]
    assert [call.max_output_tokens for call in model.calls] == [settings.max_output_tokens]


async def test_the_prompt_is_built_for_the_person_who_speaks(
    child_client, use_model, clock, provider
):
    model = FakeChatModel(["ok"])
    use_model(model)
    msg_id = await post_message(child_client, "hi")
    await consume_sse(child_client, f"/chat/stream/{msg_id}")
    expected = build_system_prompt(
        abilities=PAGE,
        display_name="Kid",
        role="child",
        can_act=True,
        **turn_tools("child"),
        today=today_of(clock(), None),
        household=(
            HouseholdLine("Owner", "owner"),
            HouseholdLine("Kid", "child", is_you=True),
        ),
        # No model of meanings in the pilots: the search works by words alone.
        memory=MemoryBlock(search="words_only"),
        models=CHAT_TALK,
        keys=KeysNote(listed=(KeyShown(GENERAL_LABEL, provider.id),)),
        reads="en",
        # Nobody's word has been weighed yet: the household's start (trust per person).
        trust=Trust(percent=70, facts=0),
    )
    assert [call.system for call in model.calls] == [expected]


async def test_someone_without_a_language_gets_the_houses(
    chat_client, use_model, session_factory, owner
):  # spec 5.3
    """The owner has no language of their own yet: Domo is told the house's."""
    async with session_factory() as writer:
        await house_reads(writer, "fr")
        await writer.execute(
            update(Person).where(Person.id == owner.person_id).values(language=None)
        )
        await writer.commit()
    model = FakeChatModel(["ok"])
    use_model(model)
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    [call] = model.calls
    line = "This person reads French (fr): write to them in it until they write in another."
    assert line in call.system.split("\n\nContext\n")[1].splitlines()


async def test_the_history_is_the_conversation_without_the_empty_reply(chat_client, use_model):
    model = FakeChatModel(["one"])
    use_model(model)
    first = await post_message(chat_client, "first question")
    await consume_sse(chat_client, f"/chat/stream/{first}")
    second = await post_message(chat_client, "second question")
    await consume_sse(chat_client, f"/chat/stream/{second}")
    assert model.calls[1].messages == [
        ChatMessage("user", "first question"),
        ChatMessage("assistant", "one"),
        ChatMessage("user", "second question"),
    ]


async def test_replies_without_text_are_not_sent_back_to_the_model(chat_client, use_model):
    use_model(FakeChatModel(["x"], fail_after=0))
    first = await post_message(chat_client, "first")
    await consume_sse(chat_client, f"/chat/stream/{first}")  # fails: an empty reply
    model = FakeChatModel(["ok"])
    use_model(model)
    second = await post_message(chat_client, "second")
    await consume_sse(chat_client, f"/chat/stream/{second}")
    # Two questions in a row would be refused by strict chat templates: they are one message.
    assert model.calls[0].messages == [ChatMessage("user", "first\n\nsecond")]


async def test_a_failed_reply_is_not_sent_back_even_when_it_said_something(chat_client, use_model):
    use_model(FakeChatModel(["Hel", "lo"], fail_after=1))
    first = await post_message(chat_client, "first")
    events = await consume_sse(chat_client, f"/chat/stream/{first}")
    assert event_names(events) == ["delta", "error", "done"]
    model = FakeChatModel(["ok"])
    use_model(model)
    second = await post_message(chat_client, "second")
    await consume_sse(chat_client, f"/chat/stream/{second}")
    assert model.calls[0].messages == [ChatMessage("user", "first\n\nsecond")]


async def test_the_history_never_has_two_messages_of_one_side_in_a_row(chat_client, use_model):
    use_model(FakeChatModel(["x"], fail_after=0))
    for question in ("q1", "q2"):  # two failures: three questions with nothing between them
        reply = await post_message(chat_client, question)
        await consume_sse(chat_client, f"/chat/stream/{reply}")
    model = FakeChatModel(["fine"])
    use_model(model)
    reply = await post_message(chat_client, "q3")
    await consume_sse(chat_client, f"/chat/stream/{reply}")
    assert model.calls[0].messages == [ChatMessage("user", "q1\n\nq2\n\nq3")]
    # And the turn after that one has the usual shape.
    reply = await post_message(chat_client, "q4")
    await consume_sse(chat_client, f"/chat/stream/{reply}")
    roles = [m.role for m in model.calls[1].messages]
    assert roles == ["user", "assistant", "user"]


async def test_a_reply_cut_short_is_part_of_the_history(chat_client, use_model, session, app):
    use_model(FakeChatModel(["Hel", "lo"], delay_s=0.3))
    first = await post_message(chat_client, "first")
    await consume_sse(chat_client, f"/chat/stream/{first}", stop_after_events=1)
    await stop_the_turns(app)  # the core stops while Domo writes
    await wait_for_status(session, first, {"partial"})
    model = FakeChatModel(["ok"])
    use_model(model)
    second = await post_message(chat_client, "second")
    await consume_sse(chat_client, f"/chat/stream/{second}")
    assert model.calls[0].messages == [
        ChatMessage("user", "first"),
        ChatMessage("assistant", "Hel"),
        ChatMessage("user", "second"),
    ]


async def test_the_history_is_limited_to_the_budget(chat_client, use_model, app, settings):
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"max_history_chars": 20}
    )
    model = FakeChatModel(["abcdefgh"])
    use_model(model)
    first = await post_message(chat_client, "q" * 9)
    await consume_sse(chat_client, f"/chat/stream/{first}")
    second = await post_message(chat_client, "z" * 9)
    await consume_sse(chat_client, f"/chat/stream/{second}")
    # 9 + 8 + 9 = 26 characters do not fit in 20: the oldest message goes, and the history does
    # not begin with the reply that is left.
    assert model.calls[1].messages == [ChatMessage("user", "z" * 9)]


# --- what the model says is not trusted --------------------------------------------------------

HOSTILE = [
    "<img src=x onerror=alert(1)>",
    "<script>alert(2)</script>",
    "a & b < c > d \"quoted\" 'single'",
]


async def test_model_output_is_escaped_in_the_stream_and_in_the_history(
    chat_client, use_model, session
):
    use_model(FakeChatModel(HOSTILE))
    msg_id = await post_message(chat_client, "hi")
    body = (await read_raw_stream(chat_client, f"/chat/stream/{msg_id}")).decode()
    assert "<" not in body and ">" not in body  # nothing a browser could take for markup
    events = parse_sse(body)
    deltas = [event.data for event in events if event.event == "delta"]
    assert deltas[0] == "&lt;img src=x onerror=alert(1)&gt;"
    assert deltas[1] == "&lt;script&gt;alert(2)&lt;/script&gt;"
    assert [html.unescape(delta) for delta in deltas] == HOSTILE
    # What is stored is the text, as the model said it.
    assert (await get_message(session, msg_id)).content == "".join(HOSTILE)
    page = (await chat_client.get("/chat")).text
    assert "<img src=x" not in page and "<script>alert" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page


async def test_a_line_break_in_the_reply_cannot_forge_an_event(chat_client, use_model):
    attack = "one\r\nevent: error\r\ndata: forged\r\n\r\nevent: done\rtwo"
    use_model(FakeChatModel([attack]))
    msg_id = await post_message(chat_client, "hi")
    raw = await read_raw_stream(chat_client, f"/chat/stream/{msg_id}")
    assert b"\r" not in raw
    events = parse_sse(raw.decode())
    assert event_names(events) == ["delta", "done"]
    assert events[0].data == "one\nevent: error\ndata: forged\n\nevent: done\ntwo"


async def test_line_breaks_and_spaces_of_the_reply_survive_the_stream(chat_client, use_model):
    chunks = ["  indented\n", "\nafter a blank line", " "]
    use_model(FakeChatModel(chunks))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert [e.data for e in events if e.event == "delta"] == chunks


async def test_characters_the_database_cannot_hold_are_cleaned_before_they_are_stored(
    chat_client, use_model, session
):
    use_model(FakeChatModel(["a\x00b", "c\ud800d"]))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert [e.data for e in events if e.event == "delta"] == ["ab", "c\ufffdd"]
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("abc\ufffdd", "complete")


# --- a reply that fails ------------------------------------------------------------------------


async def test_a_failure_is_announced_with_a_translated_message(chat_client, use_model, session):
    use_model(FakeChatModel(["Hel", "lo"], fail_after=1, error_kind="rate_limited"))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert event_names(events) == ["delta", "error", "done"]
    assert html.unescape(events[1].data) == t("chat.error.rate_limited", "en")
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("Hel", "error")  # the text so far is kept


async def test_the_failure_is_announced_in_the_language_of_the_reader(
    chat_client, use_model, owner, session_factory
):
    use_model(FakeChatModel(["x"], fail_after=0, error_kind="unreachable"))
    msg_id = await post_message(chat_client, "hi")
    async with session_factory() as writer:
        await reads_in(writer, owner.person_id, "fr")
        await writer.commit()
    async with chat_client.stream("GET", f"/chat/stream/{msg_id}") as response:
        events = parse_sse((await response.aread()).decode())
    assert html.unescape(events[0].data) == in_language("fr", "chat.error.unreachable")


@pytest.mark.parametrize("kind", get_args(ErrorKind))
def test_every_kind_of_failure_has_a_message(kind):
    assert t(f"chat.error.{kind}", "en")


async def test_a_failure_before_any_text_leaves_an_empty_reply_marked_as_an_error(
    chat_client, use_model, session
):
    use_model(FakeChatModel(["x"], fail_after=0, error_kind="auth"))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert event_names(events) == ["error", "done"]
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("", "error")


class BrokenModel:
    """A model that fails in a way no adapter should let through."""

    def stream(self, **_: object):
        raise RuntimeError("secret detail that must not be shown")


async def test_an_unexpected_failure_is_an_error_not_a_stuck_reply(
    chat_client, use_model, session, caplog
):
    use_model(BrokenModel())  # type: ignore[arg-type]
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert event_names(events) == ["error", "done"]
    assert html.unescape(events[0].data) == t("chat.error.upstream", "en")
    assert "secret detail" not in json.dumps([tuple(e) for e in events])
    assert (await get_message(session, msg_id)).status == "error"
    assert "secret detail" not in caplog.text
    assert (await chat_client.post("/chat/messages", data={"text": "again"})).status_code == 200


# --- who may stream what, and when -------------------------------------------------------------


async def test_a_reply_that_is_over_is_not_streamed_again(chat_client, use_model, session):
    model = FakeChatModel(["Hello"])
    use_model(model)
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    r = await chat_client.get(f"/chat/stream/{msg_id}")
    assert r.status_code == 204
    assert r.content == b""
    assert len(model.calls) == 1  # no second call to the model
    assert (await get_message(session, msg_id)).content == "Hello"


@pytest.mark.parametrize("final", ["error", "partial"])
async def test_replies_that_ended_otherwise_are_not_streamed_again_either(
    chat_client, session_factory, final
):
    msg_id = await post_message(chat_client, "hi")
    async with session_factory() as writer:
        await writer.execute(
            text("UPDATE messages SET status = :s WHERE id = CAST(:id AS uuid)"),
            {"s": final, "id": msg_id},
        )
        await writer.commit()
    assert (await chat_client.get(f"/chat/stream/{msg_id}")).status_code == 204


@pytest.mark.parametrize(
    "message_id", ["00000000-0000-4000-8000-000000000000", "not-a-uuid", "1", "%20", "../chat"]
)
async def test_an_unknown_message_is_a_404(chat_client, message_id):
    assert (await chat_client.get(f"/chat/stream/{message_id}")).status_code == 404


async def test_the_message_of_the_person_is_not_streamed_to_another(
    chat_client, member_client, use_model
):
    use_model(FakeChatModel(["private"]))
    msg_id = await post_message(chat_client, "hi")
    assert (await member_client.get(f"/chat/stream/{msg_id}")).status_code == 404
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")  # still theirs to read
    assert events[0] == SseEvent("delta", "private")


async def test_a_message_written_by_a_person_cannot_be_streamed(chat_client, session):
    await post_message(chat_client, "hi")
    result = await session.execute(text("SELECT id FROM messages WHERE role = 'user'"))
    user_id = str(result.scalar_one())
    assert (await chat_client.get(f"/chat/stream/{user_id}")).status_code == 404


async def test_a_visitor_cannot_stream(anonymous_client, chat_client, use_model):
    use_model(FakeChatModel(["x"]))
    msg_id = await post_message(chat_client, "hi")
    r = await anonymous_client.get(f"/chat/stream/{msg_id}")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_only_get_is_allowed_on_the_stream(chat_client):
    msg_id = "00000000-0000-4000-8000-000000000000"
    assert (await chat_client.post(f"/chat/stream/{msg_id}")).status_code == 405


# --- a reader who leaves: the reply goes on without its page (spec 5.7, scenario 20) ------------


async def stop_the_turns(app) -> None:
    """Stop the turns that still run, as the core does when it stops (``Wakes.stop``): a reply's
    turn is a task of ``app.state.wakes``, which outlives its connection."""
    await app.state.wakes.stop(WAIT_SECONDS)


_USER_BUBBLE = re.compile(r'class="message user"[^>]*data-message-id="([0-9a-f-]{36})"')


def user_id_in(fragment: str) -> str:
    """The id of the person's message in the pair of bubbles a posted message is answered with."""
    found = _USER_BUBBLE.findall(fragment)
    assert len(found) == 1, fragment
    return found[0]


async def test_a_reply_ends_even_when_the_page_leaves(owner_chat, chat_server):
    model = owner_chat.use_model(FakeChatModel(["Hel", "lo", " there"], delay_s=0.3))
    reply_id = await post_message(owner_chat.client, "hi")
    first = await consume_sse(owner_chat.client, f"/chat/stream/{reply_id}", stop_after_events=1)
    assert first == [SseEvent("delta", "Hel")]
    # The page is gone (another tab, the page closed, the connection lost): the turn goes on, on
    # the server, as a wake does.
    await chat_server.wait_for_wakes()
    async with chat_server.session_factory() as session:
        message = await get_message(session, reply_id)
    assert (message.content, message.status) == ("Hello there", "complete")
    assert (model.started, model.closed) == (1, 1)
    assert await claims_are_released(chat_server.app)


async def test_a_page_opened_while_the_reply_is_written_gets_it_when_it_is_over(
    owner_chat, chat_server
):
    owner_chat.use_model(FakeChatModel(["a", "b", "c"], delay_s=0.5))
    # Another page of the owner, opened while Domo still writes (a browser that does not wait
    # for the turns: it looks while one runs; signed in first, which takes a moment).
    async with chat_server.browser(owner_chat.member, waits_for_wakes=False) as other_page:
        pair = await owner_chat.client.post("/chat/messages", data={"text": "hi"})
        person_message, reply_id = user_id_in(pair.text), assistant_id_in(pair.text)
        url = f"/chat/stream/{reply_id}"
        await consume_sse(owner_chat.client, url, stop_after_events=1)  # the first page leaves
        again = await other_page.get(url)
        assert again.status_code == 204 and again.content == b""  # the reply has its writer
        # Not over yet: the page is shown Domo writing it (spec 5.7.2), not the reply.
        during = (await other_page.get(f"/chat/since/{person_message}")).text
        assert writing_ids(during) == [reply_id]
        await chat_server.wait_for_wakes()
        after = (await other_page.get(f"/chat/since/{person_message}")).text
    assert [(side, text) for side, _, text in _BUBBLE.findall(after)] == [("assistant", "abc")]
    assert _BUBBLE.findall(after)[0][1] == reply_id and writing_ids(after) == []


async def test_a_reply_whose_turn_still_runs_is_not_stale(owner_chat, chat_server, clock):
    owner_chat.use_model(FakeChatModel(["a", "b"], delay_s=1.5))
    # A browser that does not wait for the turns, to look while one runs (signed in first).
    async with chat_server.browser(owner_chat.member, waits_for_wakes=False) as other_page:
        reply_id = await post_message(owner_chat.client, "hi")
        await consume_sse(owner_chat.client, f"/chat/stream/{reply_id}", stop_after_events=1)
        clock.advance(STALE_AFTER + timedelta(minutes=1))  # older than that, but its turn runs
        async with chat_server.session_factory() as session:
            assert (await get_message(session, reply_id)).status == "streaming"
        kept = await other_page.post("/chat/messages", data={"text": "second"})
        assert kept.status_code == 202  # kept for it: one reply at a time, as when the page stays
        async with chat_server.session_factory() as session:
            assert (await get_message(session, reply_id)).status == "streaming"  # not marked dead
    await chat_server.wait_for_wakes()
    async with chat_server.session_factory() as session:
        message = await get_message(session, reply_id)
    assert (message.content, message.status) == ("ab", "complete")
    # The message kept is answered once that reply is over; then the conversation is free.
    async with chat_server.session_factory() as session:
        last = await session.scalar(
            select(Message.content).order_by(Message.created_at.desc()).limit(1)
        )
    assert last == "ab"
    assert (await owner_chat.client.post("/chat/messages", data={"text": "x"})).status_code == 200


class GatedModel:
    """A model that says nothing until the test lets it (``go``): the reader leaves before any
    text."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.go = asyncio.Event()
        self.closed = 0

    async def stream(self, **_: object):
        try:
            self.started.set()
            await self.go.wait()
            yield ChatChunk("late")
        finally:
            self.closed += 1


async def test_a_reader_who_leaves_before_the_first_word_still_gets_the_whole_reply(
    chat_client, use_model, session
):
    model = GatedModel()
    use_model(model)  # type: ignore[arg-type]
    msg_id = await post_message(chat_client, "hi")
    async with chat_client.stream("GET", f"/chat/stream/{msg_id}") as response:
        assert response.status_code == 200
        await asyncio.wait_for(model.started.wait(), 5)
        # leaving the block closes the connection
    await asyncio.sleep(0.1)
    assert (await get_message(session, msg_id)).status == "streaming"  # the model still thinks
    model.go.set()
    message = await wait_for_status(session, msg_id, {"complete"})
    assert message.content == "late"
    assert model.closed == 1


class InvitesOnceTheReaderLeft:
    """The model of a turn whose reader leaves before Domo acts: the first call that may use
    tools says a word, waits until the test says the page is gone (``gone``), then invites Dana as
    a guest; every later one answers "ok". The yes-judge lets the invitation through."""

    def __init__(self) -> None:
        self.gone = asyncio.Event()
        self._made = False

    async def stream(self, *, system: str, tools=(), tool_choice="auto", **_: object):
        if is_judge_call(system):
            yield ChatChunk("do")
        elif tools and tool_choice == "auto" and not self._made:
            self._made = True
            yield ChatChunk("Let me see.")
            await self.gone.wait()
            yield ToolCall("call-1", "household_invite", '{"display_name":"Dana","role":"guest"}')
        else:
            yield ChatChunk("ok")
        yield ChatUsage(3, 2)


async def test_a_link_made_once_the_page_left_waits_on_the_card(
    owner_chat, chat_server, monkeypatch
):
    """The page that would show the invitation's link once is gone: the link waits behind the
    button of the person's card, as in a wake, and Domo is told so (never that it was shown)."""
    model = owner_chat.use_model(InvitesOnceTheReaderLeft())
    relayed = RelaySink.relay
    left = asyncio.Event()

    async def relay_then_say_it_left(sink: RelaySink, channel: Any) -> None:
        try:
            await relayed(sink, channel)
        finally:
            left.set()

    monkeypatch.setattr(RelaySink, "relay", relay_then_say_it_left)
    reply_id = await post_message(owner_chat.client, "Invite Dana for two days.")
    await consume_sse(owner_chat.client, f"/chat/stream/{reply_id}", stop_after_events=1)
    # The server has seen the page go: its relay is over, and the turn has no reader any more.
    await asyncio.wait_for(left.wait(), WAIT_SECONDS)
    model.gone.set()
    await chat_server.wait_for_wakes()
    async with chat_server.session_factory() as session:
        assert (await get_message(session, reply_id)).status == "complete"
        stored = await session.execute(
            text(
                "SELECT tool_name, status, summary FROM message_actions "
                "WHERE message_id = CAST(:id AS uuid)"
            ),
            {"id": reply_id},
        )
        [(tool, status, card)] = stored.all()
    assert (tool, status) == ("household_invite", "done")
    assert card["key"] == "action.card.done.household.invite.on_request"
    assert "invitation" in card
    [result] = tool_results(chat_server.calls)
    assert result["link"] == "waits_for_them" and "link_shown_to_owner" not in result


async def test_a_reply_cut_short_by_the_core_stopping_does_not_block_the_conversation(
    chat_client, use_model, session, app
):
    use_model(FakeChatModel(["Hel", "lo"], delay_s=0.3))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}", stop_after_events=1)
    await stop_the_turns(app)
    assert (await get_message(session, msg_id)).status == "partial"
    assert (await chat_client.post("/chat/messages", data={"text": "again"})).status_code == 200


# --- the page shows Domo writing a reply that runs on the server (spec 5.7.2) -----------------

_ASSISTANT_TAG = re.compile(r'<div class="message assistant"[^>]*>')
_ID = re.compile(r'data-message-id="([0-9a-f-]{36})"')


def writing_ids(fragment: str) -> list[str]:
    """The ids of the bubbles that show Domo writing a reply that runs on the server."""
    return [
        found.group(1)
        for tag in _ASSISTANT_TAG.findall(fragment)
        if 'data-writing="true"' in tag and (found := _ID.search(tag)) is not None
    ]


async def test_a_page_opened_while_domo_writes_shows_him_writing_then_the_reply(
    owner_chat, chat_server
):
    owner_chat.use_model(FakeChatModel(["a", "b", "c"], delay_s=0.6))
    # A page of the owner opened while Domo still writes (signed in first: it takes a moment).
    async with chat_server.browser(owner_chat.member, waits_for_wakes=False) as other_page:
        pair = await owner_chat.client.post("/chat/messages", data={"text": "hi"})
        person_message, reply_id = user_id_in(pair.text), assistant_id_in(pair.text)
        await consume_sse(owner_chat.client, f"/chat/stream/{reply_id}", stop_after_events=1)
        page = (await other_page.get("/chat")).text
        assert writing_ids(page) == [reply_id]
        [bubble] = [tag for tag in _ASSISTANT_TAG.findall(page) if reply_id in tag]
        assert 'aria-busy="true"' in bubble and "sse-connect" not in bubble  # no second writer
        assert t("chat.status.writing", "en", domo="Domo") in page
        assert [(side, text) for side, _, text in _BUBBLE.findall(page)] == [
            ("user", "hi"),
            ("assistant", ""),  # Domo writing: no words yet, three dots
        ]
        during = (await other_page.get(f"/chat/since/{person_message}")).text
        assert writing_ids(during) == [reply_id]
        await chat_server.wait_for_wakes()
        after = (await other_page.get(f"/chat/since/{person_message}")).text
        page = (await other_page.get("/chat")).text
    assert writing_ids(after) == [] and writing_ids(page) == []
    assert [(side, text) for side, _, text in _BUBBLE.findall(after)] == [("assistant", "abc")]
    assert _BUBBLE.findall(after)[0][1] == reply_id
    assert ("assistant", "abc") in [(side, text) for side, _, text in _BUBBLE.findall(page)]


async def test_a_reply_nobody_writes_is_not_shown_as_written(chat_client, use_model):
    """Posted, but its stream never opened (the page left at once): no turn runs it, so the page
    does not say that Domo writes it (it is taken for dead after ``STALE_AFTER``)."""
    use_model(FakeChatModel(["late"]))
    pair = await chat_client.post("/chat/messages", data={"text": "hi"})
    person_message = user_id_in(pair.text)
    assert writing_ids((await chat_client.get("/chat")).text) == []
    assert (await chat_client.get(f"/chat/since/{person_message}")).text.strip() == ""


def test_a_reply_whose_stream_stops_shows_domo_writing_until_it_is_over_in_the_script():
    """The browser behaviour itself is the end-to-end test's for a page opened again; a stream
    that a phone or the network cuts, the page staying open (the browser reconnects, and gets a
    204: the turn still runs), is checked here: the bubble shows Domo writing, the page goes on
    asking for what is new from the bubble before it, and the finished reply takes its place."""
    script = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert 'bubble.setAttribute("data-writing", "true")' in script  # the stream stopped short
    assert "var from = askFrom();" in script  # asked from the bubble before it
    assert '[aria-busy="true"]:not([data-writing])' in script  # writing elsewhere is not busy
    assert "replaceWith(bubble)" in script  # the finished reply in its place


async def test_no_transaction_is_held_open_while_a_reply_streams(chat_client, use_model, engine):
    use_model(FakeChatModel(["a", "b", "c"], delay_s=0.4))
    msg_id = await post_message(chat_client, "hi")
    # The probe is outside any transaction (``pg_stat_activity`` is a snapshot that a transaction
    # keeps for its whole length).
    async with engine.connect() as connection:
        probe = await connection.execution_options(isolation_level="AUTOCOMMIT")
        async with chat_client.stream("GET", f"/chat/stream/{msg_id}") as response:
            pieces = response.aiter_text()  # kept: dropping it would close the stream
            received: list[SseEvent] = []
            while not received:
                received.extend(parse_sse(await anext(pieces)))
            # The model is thinking about the next word: the application must be holding nothing.
            result = await probe.execute(
                text(
                    "SELECT pid, state, left(query, 80) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND state LIKE 'idle in transaction%'"
                )
            )
            assert result.all() == []
            async for _ in pieces:
                pass


# --- one generation per reply ------------------------------------------------------------------


async def claims_are_released(app, timeout: float = 5.0) -> bool:
    """Whether no reply is claimed any more (the claim goes a moment after the end is stored)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while app.state.streaming_replies:
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def audit_count(session) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM audit_log WHERE action = 'chat.turn'")
    )
    return result.scalar_one()


async def test_two_connections_to_one_reply_make_one_generation(
    chat_client, use_model, session, app
):
    model = FakeChatModel(["a", "b", "c"], delay_s=0.3)
    use_model(model)
    msg_id = await post_message(chat_client, "hi")
    url = f"/chat/stream/{msg_id}"
    outcomes = await asyncio.gather(read_stream(chat_client, url), read_stream(chat_client, url))
    assert sorted(status for status, _ in outcomes) == [200, 204]
    (streamed,) = [events for status, events in outcomes if status == 200]
    (refused,) = [events for status, events in outcomes if status == 204]
    assert [e.data for e in streamed if e.event == "delta"] == ["a", "b", "c"]
    assert refused == []
    assert model.started == 1
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("abc", "complete")
    assert await audit_count(session) == 1
    assert await claims_are_released(app)


async def test_a_browser_that_reconnects_to_a_reply_still_being_written_is_told_to_stop(
    chat_client, use_model, session
):
    """The first connection is still there (half open, say) and the browser opens another."""
    model = FakeChatModel(["a", "b", "c"], delay_s=0.4)
    use_model(model)
    msg_id = await post_message(chat_client, "hi")
    url = f"/chat/stream/{msg_id}"
    async with chat_client.stream("GET", url) as response:
        pieces = response.aiter_text()
        first: list[SseEvent] = []
        while not first:
            first.extend(parse_sse(await anext(pieces)))
        reconnect = await chat_client.get(url)
        assert reconnect.status_code == 204 and reconnect.content == b""
        async for _ in pieces:
            pass
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("abc", "complete")  # the whole generation
    assert model.started == 1
    assert await audit_count(session) == 1


async def test_the_claim_does_not_outlive_the_turn(chat_client, use_model, session, app):
    use_model(FakeChatModel(["a"]))
    done = await post_message(chat_client, "one")
    await consume_sse(chat_client, f"/chat/stream/{done}")
    assert await claims_are_released(app)

    use_model(FakeChatModel(["a"], fail_after=0))
    failed = await post_message(chat_client, "two")
    await consume_sse(chat_client, f"/chat/stream/{failed}")
    assert await claims_are_released(app)

    use_model(FakeChatModel(["a", "b"], delay_s=0.3))
    left = await post_message(chat_client, "three")
    await consume_sse(chat_client, f"/chat/stream/{left}", stop_after_events=1)
    await wait_for_status(session, left, {"complete"})  # its reader left, and the turn ended
    assert await claims_are_released(app)

    use_model(FakeChatModel(["a", "b"], delay_s=0.3))
    stopped = await post_message(chat_client, "four")
    await consume_sse(chat_client, f"/chat/stream/{stopped}", stop_after_events=1)
    await stop_the_turns(app)
    await wait_for_status(session, stopped, {"partial"})
    assert await claims_are_released(app)
    # Nor does a refused request leave one behind.
    assert (await chat_client.get(f"/chat/stream/{done}")).status_code == 204
    assert await claims_are_released(app)


async def wait_for_the_turns(app) -> None:
    """Wait until no turn of Domo runs any more (``app.state.wakes``)."""
    async with asyncio.timeout(WAIT_SECONDS):
        while app.state.wakes.pending:
            await asyncio.sleep(0.01)


async def test_a_reply_that_is_being_written_is_never_taken_for_dead(
    chat_client, use_model, session, clock, app
):
    use_model(FakeChatModel(["a", "b"], delay_s=1.0))
    msg_id = await post_message(chat_client, "hi")
    async with chat_client.stream("GET", f"/chat/stream/{msg_id}") as response:
        pieces = response.aiter_text()
        first: list[SseEvent] = []
        while not first:
            first.extend(parse_sse(await anext(pieces)))
        clock.advance(timedelta(minutes=3))  # older than two minutes, but somebody is writing it
        kept = await chat_client.post("/chat/messages", data={"text": "second"})
        assert kept.status_code == 202  # kept for it, not taken for a dead reply's
        assert (await get_message(session, msg_id)).status == "streaming"
        async for _ in pieces:
            pass
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("ab", "complete")
    # The message kept is answered by a reply of its own, once that one is over.
    await wait_for_the_turns(app)
    rows = await message_rows(session)
    assert [(row.role, row.content) for row in rows][-2:] == [
        ("user", "second"),
        ("assistant", "ab"),
    ]
    assert (await chat_client.post("/chat/messages", data={"text": "third"})).status_code == 200


async def test_a_reply_settled_by_someone_else_is_not_reported_as_a_turn(
    chat_client, use_model, session, session_factory
):
    use_model(FakeChatModel(["a", "b"], delay_s=0.5))
    msg_id = await post_message(chat_client, "hi")
    async with chat_client.stream("GET", f"/chat/stream/{msg_id}") as response:
        pieces = response.aiter_text()
        first: list[SseEvent] = []
        while not first:
            first.extend(parse_sse(await anext(pieces)))
        async with session_factory() as writer:
            await writer.execute(
                text("UPDATE messages SET status = 'error' WHERE id = CAST(:id AS uuid)"),
                {"id": msg_id},
            )
            await writer.commit()
        async for _ in pieces:
            pass
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("", "error")  # not rewritten
    assert await audit_count(session) == 0  # and no turn is reported for it


async def test_a_hanging_audit_does_not_keep_the_reply_streaming(
    chat_client, use_model, session, monkeypatch
):
    async def hang(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("domo_core.conversation.turn.append_audit", hang)
    monkeypatch.setattr(conversation_turn, "_SETTLE_SECONDS", 0.5)  # per attempt
    use_model(FakeChatModel(["Hello"]))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert event_names(events) == ["delta", "done"]
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("Hello", "complete")  # stored without the audit
    assert await audit_count(session) == 0


# --- the audit ---------------------------------------------------------------------------------


async def test_turn_audit_has_no_content(chat_client, use_model, session):
    use_model(FakeChatModel(["secret words"]))
    msg_id = await post_message(chat_client, "private question")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    entry = await last_audit(session, "chat.turn")
    assert set(entry.details) == {
        "provider_id",
        "model",
        "level",
        "status",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "estimated",
    }
    assert "private question" not in json.dumps(entry.details)
    assert "secret words" not in json.dumps(entry.details)


async def test_the_audit_says_what_the_turn_used(chat_client, use_model, session, owner, provider):
    use_model(FakeChatModel(["ok"], usage=ChatUsage(120, 34)))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    entry = await last_audit(session, "chat.turn")
    assert entry.actor == f"person:{owner.person_id}"
    assert entry.details == {
        "provider_id": provider.id,
        "model": CHAT_MODEL,
        "level": "medium",
        "status": "complete",
        "input_tokens": 120,
        "output_tokens": 34,
        "cost_usd": "0.000000",
        "estimated": False,
    }


async def test_the_audit_counts_zero_tokens_when_the_provider_reports_none(
    chat_client, use_model, session
):
    use_model(FakeChatModel(["ok"], usage=None))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    details = (await last_audit(session, "chat.turn")).details
    assert (details["input_tokens"], details["output_tokens"]) == (0, 0)


async def test_the_audit_follows_the_end_of_the_turn(chat_client, use_model, session, app):
    use_model(FakeChatModel(["Hel", "lo"], fail_after=1))
    failed = await post_message(chat_client, "one")
    await consume_sse(chat_client, f"/chat/stream/{failed}")
    assert (await last_audit(session, "chat.turn")).details["status"] == "error"

    use_model(FakeChatModel(["Hel", "lo"], delay_s=0.3))
    left = await post_message(chat_client, "two")
    await consume_sse(chat_client, f"/chat/stream/{left}", stop_after_events=1)
    await wait_for_status(
        session, left, {"complete"}
    )  # its reader left: the turn ended all the same
    assert (await last_audit(session, "chat.turn")).details["status"] == "complete"

    use_model(FakeChatModel(["Hel", "lo"], delay_s=0.3))
    stopped = await post_message(chat_client, "three")
    await consume_sse(chat_client, f"/chat/stream/{stopped}", stop_after_events=1)
    await stop_the_turns(app)
    await wait_for_status(session, stopped, {"partial"})  # settled with its audit entry, together
    assert (await last_audit(session, "chat.turn")).details["status"] == "partial"


async def test_one_turn_is_one_audit_entry_and_the_chain_holds(chat_client, use_model, session):
    use_model(FakeChatModel(["ok"]))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert (await chat_client.get(f"/chat/stream/{msg_id}")).status_code == 204
    count = (
        await session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'chat.turn'"))
    ).scalar_one()
    assert count == 1
    assert (await verify_audit_chain(session)).ok


async def test_the_status_is_kept_even_when_the_audit_cannot_be_written(
    chat_client, use_model, session, monkeypatch
):
    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the audit log is unavailable")

    monkeypatch.setattr("domo_core.conversation.turn.append_audit", refuse)
    use_model(FakeChatModel(["Hello"]))
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert event_names(events) == ["delta", "done"]
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == ("Hello", "complete")


# --- the clock and the static files ------------------------------------------------------------


def test_the_default_clock_is_utc_and_current():
    now = get_clock()()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)


def test_the_stylesheet_keeps_the_line_breaks_of_the_messages():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.message-text\s*\{([^}]*)\}", css)
    assert rule is not None and "white-space: pre-wrap" in rule.group(1)


async def test_the_chat_script_is_a_static_file(chat_client):
    r = await chat_client.get("/static/chat.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


# --- an answer cut by the token limit --------------------------------------------------------


async def test_an_answer_cut_short_with_nothing_said_is_never_an_empty_reply(
    chat_client, use_model, session
):
    """The owner's trial of 0.7.5: an empty bubble, stored as a complete answer. More room is
    tried first; if the answer is still cut, the code says so in Domo's reply."""
    model = FakeChatModel([], usage=ChatUsage(10, 2048, cut_short=True))
    use_model(model)
    msg_id = await post_message(chat_client, "hi")
    events = await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    assert event_names(events) == ["delta", "done"]
    sentence = t("chat.cut_short", "en")
    assert html.unescape(events[0].data) == sentence
    message = await get_message(session, msg_id)
    assert (message.content, message.status) == (sentence, "complete")
    limits = [call.max_output_tokens for call in model.calls]
    assert len(limits) == 3 and limits[0] < limits[1] < limits[2]


async def test_an_answer_cut_short_after_some_words_says_so_after_them(
    chat_client, use_model, session
):
    use_model(FakeChatModel(["Je vais "], usage=ChatUsage(10, 2048, cut_short=True)))
    msg_id = await post_message(chat_client, "hi")
    await consume_sse(chat_client, f"/chat/stream/{msg_id}")
    message = await get_message(session, msg_id)
    assert message.content == "Je vais \n\n" + t("chat.cut_short", "en")
    assert message.status == "complete"
