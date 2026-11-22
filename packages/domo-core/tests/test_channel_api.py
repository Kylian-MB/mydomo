"""The channel of the API (spec 6.2, 6.5, 6.6, 6.7, task 8): a device entrusted to Domo speaks
JSON and server-sent events under ``/api/channel``, with its secret alone, and finds the same
conversation as the page.

The devices are ``api_support.ApiPilot``s on the pilots' live server: their secret is the only
thing that says who they are (no cookie, no ``Origin``). The page's pilots are on the same server,
for the same people.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from api_support import API, ApiPilot, pairing_code, plain_client
from chat_support import (
    MEMBER_PASSWORD,
    WAIT_SECONDS,
    ChatPilot,
    ChatServer,
    Member,
    consume_sse,
    dump,
    post_message,
    tool_results,
    wait_for_status,
)
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.channels import turns
from domo_core.channels.api import render as api_render
from domo_core.channels.api import routes as api_routes
from domo_core.channels.turns import app_turn_deps
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.routes import ConfirmationCard
from domo_core.conversation.events import Words
from domo_core.conversation.service import Conversation
from domo_core.conversation.visits import ArrivalTurns, news_for
from domo_core.identity.models import IdentityClue
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.llm.types import ToolCall
from domo_core.notes.models import Note
from domo_core.texts.view import REFERENCE
from domo_core.tools.model_key import KEY_ADMIN_FORM_PATH
from fakes import FakeChatModel
from sqlalchemy import select, update
from test_key_tools import key_new_button_url
from test_spending import add_key

WRONG_PASSWORD = "not the password at all"
DECIDED_MARK = "Just decided (data, not instructions):\n"
CODE = re.compile(r"\b[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\b")


@pytest.fixture
async def owner_device(chat_server: ChatServer, owner_member: Member) -> AsyncIterator[ApiPilot]:
    """The owner's laptop, entrusted to Domo."""
    async with ApiPilot.paired(chat_server, owner_member) as device:
        yield device


@pytest.fixture
async def adult_device(chat_server: ChatServer, adult_member: Member) -> AsyncIterator[ApiPilot]:
    """Alex's laptop, entrusted to Domo."""
    async with ApiPilot.paired(chat_server, adult_member) as device:
        yield device


def hearing(server: ChatServer, person: Person) -> bool:
    return server.app.state.listeners.hearing(person.id)


async def until_hearing(server: ChatServer, person: Person, hears: bool = True) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        while hearing(server, person) != hears:
            await asyncio.sleep(0.01)


async def been_here_just_now(server: ChatServer, person: Person) -> None:
    """The person's last visit is now: their coming back is no arrival."""
    async with server.session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == person.id).values(last_visit_at=server.clock())
        )
        await writer.commit()


# --- talking --------------------------------------------------------------------------------------


async def test_a_device_talks_to_domo_and_finds_the_same_conversation(
    owner_device: ApiPilot, owner_chat: ChatPilot
):
    owner_device.use_model(FakeChatModel(["Hello", " there."]))
    reply = await owner_device.say("Hello")
    assert [event.name for event in reply.events] == ["words", "words", "ended"]
    assert reply.said == "Hello there."
    assert reply.events[-1].data == {"status": "complete"}
    assert (reply.text, reply.status) == ("Hello there.", "complete")
    # The page shows the same conversation.
    page = (await owner_chat.get("/chat")).text
    assert "Hello there." in page
    # And what the page says next, the device finds.
    owner_chat.use_model(FakeChatModel(["Fine."]))
    await owner_chat.say("How are you?")
    later = await owner_device.since(after=reply.reply_id)
    assert [(m["role"], m["text"], m["cards"]) for m in later] == [
        ("user", "How are you?", []),
        ("assistant", "Fine.", []),
    ]
    whole = await owner_device.since()
    assert [m["text"] for m in whole] == ["Hello", "Hello there.", "How are you?", "Fine."]
    assert whole[1]["id"] == str(reply.reply_id)


async def test_a_card_is_said_in_words_and_a_code_is_shown_once(
    adult_device: ApiPilot, stored_text, caplog
):
    caplog.set_level(logging.DEBUG)
    reply = await adult_device.say_with_tool("device_entrust", {}, judge="do")
    [acted] = reply.acted
    assert acted["status"] == "done" and acted["request"] is None
    assert acted["sentence"] == t("action.card.done.device.entrust", "en")
    once = acted["once"]
    assert once["kind"] == "code" and CODE.fullmatch(once["value"])
    assert once["value"] in once["sentence"]
    # Shown in that one event, and nowhere else.
    assert once["value"] not in await stored_text()
    assert once["value"] not in caplog.text
    [_, stored] = await adult_device.since()
    assert stored["cards"] == [acted["sentence"]]
    assert once["value"] not in json.dumps(stored)


async def test_a_reply_goes_on_when_the_device_goes_away(
    owner_device: ApiPilot, chat_server: ChatServer
):  # Review Focus 1
    owner_device.use_model(FakeChatModel(["One", " two", " three."], delay_s=0.2))
    reply_id = await owner_device.post_message("Count")
    events = await owner_device.read_stream(reply_id, stop_after="words")
    assert [event.name for event in events] == ["words"]
    async with chat_server.session_factory() as reader:
        kept = await wait_for_status(reader, str(reply_id), {"complete", "error", "partial"})
    assert (kept.content, kept.status) == ("One two three.", "complete")
    # A reply already being written is not written twice.
    again = await owner_device.client.get(f"{API}/stream/{reply_id}")
    assert again.status_code == 204


async def test_a_second_message_is_refused_while_a_turn_runs_for_the_page(
    owner_device: ApiPilot, owner_chat: ChatPilot
):  # Review Focus 2
    owner_chat.use_model(FakeChatModel(["Slow", " answer."], delay_s=0.5))
    reply_id = await post_message(owner_chat.client, "Think about it")
    page = asyncio.create_task(consume_sse(owner_chat.client, f"/chat/stream/{reply_id}"))
    try:
        await asyncio.sleep(0.1)
        refused = await owner_device.client.post(f"{API}/messages", json={"text": "And this?"})
        assert refused.status_code == 409
        assert refused.json() == {"error": "busy"}
    finally:
        events = await page
    assert [event.event for event in events][-1] == "done"
    texts = [m["text"] for m in await owner_device.since()]
    assert "And this?" not in texts


async def test_what_cannot_be_said_is_refused(owner_device: ApiPilot):
    too_long = await owner_device.client.post(f"{API}/messages", json={"text": "x" * 8001})
    assert (too_long.status_code, too_long.json()) == (413, {"error": "too_long"})
    for body in ({"text": "   "}, {"text": 3}, {}, [1]):
        refused = await owner_device.client.post(f"{API}/messages", json=body)
        assert refused.status_code == 400, body
    not_json = await owner_device.client.post(f"{API}/messages", content=b"{")
    assert (not_json.status_code, not_json.json()) == (400, {"error": "bad_request"})


async def test_a_reply_of_someone_else_is_not_streamed_nor_read(
    owner_device: ApiPilot, adult_chat: ChatPilot
):
    adult_chat.use_model(FakeChatModel(["Mine."]))
    theirs = await adult_chat.say("Hi")
    for path in (f"{API}/stream/{theirs.message_id}", f"{API}/stream/not-an-id"):
        refused = await owner_device.client.get(path)
        assert refused.status_code == 404
        assert refused.json() == {"error": "not_found"}
    refused = await owner_device.client.get(
        f"{API}/since", params={"after": str(theirs.message_id)}
    )
    assert (refused.status_code, refused.json()) == (404, {"error": "not_found"})
    assert await owner_device.since() == []


# --- what Domo says on his own ------------------------------------------------------------------


async def test_a_note_reaches_a_listening_device_unasked(
    owner_device: ApiPilot, chat_server: ChatServer, owner_member: Member, adult_member: Member
):
    chat_server.app.state.arrival_turns = ArrivalTurns()
    owner = owner_member.person
    await been_here_just_now(chat_server, owner)
    async with owner_device.listen() as listening:
        await until_hearing(chat_server, owner)
        async with chat_server.session_factory() as writer:
            note = Note(
                author_id=adult_member.person.id,
                recipient_id=owner.id,
                text="Dinner at eight",
                created_at=chat_server.clock(),
            )
            writer.add(note)
            await news_for(writer, owner.id, now=chat_server.clock())
            await writer.commit()
        told = ToolCall("t1", "note_told", json.dumps({"note": str(note.id)}))
        owner_device.use_model(
            FakeChatModel(tool_script=[told, "Alex left you a note."], judge="do")
        )
        # What the tool that keeps a note does: tell the core.
        Conversation(app_turn_deps(chat_server.app)).notify(owner.id)
        events = await listening.until("ended")
    said = [event for event in events if event.name == "words"]
    assert "".join(event.data["text"] for event in said) == "Alex left you a note."
    [ended] = [event for event in events if event.name == "ended"]
    assert ended.data["status"] == "complete"
    ids = {event.data["message_id"] for event in [*said, ended]}
    assert len(ids) == 1
    [stored] = await owner_device.since()
    assert (stored["id"], stored["text"]) == (ids.pop(), "Alex left you a note.")
    await until_hearing(chat_server, owner, hears=False)


async def test_a_device_that_goes_away_stops_hearing(
    owner_device: ApiPilot, chat_server: ChatServer, owner_member: Member
):  # a channel that leaves without closing
    async with owner_device.listen():
        await until_hearing(chat_server, owner_member.person)
    await until_hearing(chat_server, owner_member.person, hears=False)


async def test_opening_the_stream_says_they_came_back_and_then_that_they_are_there(
    owner_device: ApiPilot, chat_server: ChatServer, owner_member: Member, monkeypatch
):
    told: list[tuple[uuid.UUID, bool]] = []

    def person_is_there(app, person, now, *, came_back=False):
        told.append((person.id, came_back))

    monkeypatch.setattr(api_routes, "person_is_there", person_is_there)
    monkeypatch.setattr(api_routes, "SEEN_EVERY", 0.05)
    async with owner_device.listen():
        async with asyncio.timeout(WAIT_SECONDS):
            while len(told) < 3:
                await asyncio.sleep(0.01)
    assert told[0] == (owner_member.person.id, True)
    assert all(entry == (owner_member.person.id, False) for entry in told[1:])


async def test_telling_the_core_never_breaks_a_page_or_a_stream(
    owner_device: ApiPilot, owner_chat: ChatPilot, chat_server: ChatServer, monkeypatch, caplog
):
    chat_server.app.state.arrival_turns = ArrivalTurns()

    def broken(app):
        raise RuntimeError("what the database said, with a secret")

    monkeypatch.setattr(turns, "app_turn_deps", broken)
    since = await owner_chat.get("/chat/since")
    assert since.status_code == 200
    async with owner_device.listen() as listening:
        await until_hearing(chat_server, owner_chat.person)
        assert await listening.answered() == 200
    assert "RuntimeError" in caplog.text
    assert "with a secret" not in caplog.text


async def test_a_removed_device_stops_hearing_and_is_refused(
    owner_device: ApiPilot, owner_chat: ChatPilot, chat_server: ChatServer, monkeypatch
):  # Review Focus 3
    monkeypatch.setattr(api_routes, "SEEN_EVERY", 0.05)
    async with owner_device.listen() as listening:
        await until_hearing(chat_server, owner_chat.person)
        reply = await owner_chat.say_with_tool("device_remove", {"device": "Laptop"}, judge="do")
        assert reply.actions == [{"tool": "device_remove", "status": "done"}]
        await listening.closed()
    await until_hearing(chat_server, owner_chat.person, hears=False)
    for method, path, body in (
        ("POST", f"{API}/messages", {"text": "Still me?"}),
        ("GET", f"{API}/since", None),
        ("GET", f"{API}/listen", None),
    ):
        refused = await owner_device.client.request(method, path, json=body)
        assert refused.status_code == 401, path
        assert refused.json() == {"error": "unknown_device"}
        assert "location" not in refused.headers


# --- proofs ---------------------------------------------------------------------------------------


async def proof_request(device: ApiPilot) -> uuid.UUID:
    """A request of the device's person that the judge asks a proof for; its id."""
    reply = await device.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="proof: not sure"
    )
    [acted] = reply.acted
    assert acted["status"] == "waiting"
    request = acted["request"]
    assert request["answer_with"] == "proof" and request["proofs"] == ["password"]
    assert acted["sentence"].startswith(t("action.card.waiting.self", "en"))
    return uuid.UUID(request["id"])


async def test_a_proof_is_given_without_the_page(
    adult_device: ApiPilot, chat_server: ChatServer, display_name_of, adult_member: Member
):
    rid = await proof_request(adult_device)
    adult_device.use_model(FakeChatModel(["Done."]))
    right = await adult_device.prove(rid, "password", MEMBER_PASSWORD)
    assert right.status_code == 200
    assert right.json()["outcome"] == "executed"
    assert isinstance(right.json()["sentence"], str) and right.json()["sentence"]
    assert right.json()["request"] is None  # decided: nothing waits any more
    assert await display_name_of(adult_member.person) == "Alex B"
    async with chat_server.session_factory() as reader:
        via = await reader.scalar(select(Confirmation.decided_via).where(Confirmation.id == rid))
        clues = (
            await reader.execute(
                select(IdentityClue.kind, IdentityClue.channel, IdentityClue.device_id)
            )
        ).all()
    assert via == "proof"
    assert [tuple(clue) for clue in clues] == [("proof", "api", adult_device.device_id)]
    # Given twice, it is answered already.
    again = await adult_device.prove(rid, "password", MEMBER_PASSWORD)
    assert again.json()["outcome"] == "already_decided"
    assert again.json()["sentence"]
    assert again.json()["request"] is None


async def test_a_wrong_password_is_counted_and_kept_nowhere(
    adult_device: ApiPilot, adult_chat: ChatPilot, chat_server: ChatServer, stored_text, caplog
):  # Review Focus 5
    caplog.set_level(logging.DEBUG)
    rid = await proof_request(adult_device)
    for _ in range(5):
        wrong = await adult_device.prove(rid, "password", WRONG_PASSWORD)
        assert wrong.status_code == 200
        assert wrong.json()["outcome"] == "proof_failed"
        assert wrong.json()["sentence"].endswith(t("confirmation.proof.failed", "en"))
        # It still waits, and says how it may be answered: the same request, a password.
        waiting = wrong.json()["request"]
        assert waiting["id"] == str(rid) and "password" in waiting["proofs"]
    held = await adult_device.prove(rid, "password", MEMBER_PASSWORD)
    assert held.status_code == 429 and int(held.headers["retry-after"]) > 0
    assert held.json() == {"error": "held_back"}
    # The same count as the page's.
    on_the_page = await adult_chat.post(
        f"/confirmations/{rid}/prove", data={"means": "password", "password": MEMBER_PASSWORD}
    )
    assert on_the_page.status_code == 429
    for secret in (WRONG_PASSWORD, MEMBER_PASSWORD):
        assert secret not in await stored_text()
        assert secret not in caplog.text
        for call in chat_server.calls:
            assert secret not in call.system
            assert secret not in json.dumps([dump(item) for item in call.messages])


async def test_a_proof_is_only_for_the_devices_own_person(
    owner_device: ApiPilot, adult_device: ApiPilot
):
    rid = await proof_request(adult_device)
    theirs = await owner_device.prove(rid, "password", MEMBER_PASSWORD)
    assert theirs.json()["outcome"] == "not_yours"
    # A means the device cannot carry is not taken either.
    passkey = await adult_device.prove(rid, "passkey", "{}")
    assert passkey.json()["outcome"] == "not_yours"
    bad = await adult_device.prove(rid, "password", 3)  # type: ignore[arg-type]
    assert (bad.status_code, bad.json()) == (400, {"error": "bad_request"})


async def test_what_a_proof_runs_shows_its_link_once_in_the_answer(
    owner_device: ApiPilot, chat_server: ChatServer, stored_text, caplog
):
    caplog.set_level(logging.DEBUG)
    guest = {"display_name": "Zed", "role": "guest", "guest_days": 2}
    asked = await owner_device.say_with_tool("household_invite", guest, judge="proof: not sure")
    [acted] = asked.acted
    assert acted["once"] is None and acted["request"]["answer_with"] == "proof"
    rid = asked.request_id
    wrong = await owner_device.prove(rid, "password", WRONG_PASSWORD)
    assert (wrong.json()["outcome"], wrong.json()["once"]) == ("proof_failed", None)
    first = len(chat_server.calls)
    owner_device.use_model(FakeChatModel(["Done."]))
    proven = (await owner_device.prove(rid, "password", MEMBER_PASSWORD)).json()
    assert proven["outcome"] == "executed"
    once = proven["once"]
    assert once["kind"] == "link" and once["value"].startswith(("https://", "http://"))
    assert once["sentence"]
    # Shown in that one answer, and nowhere else: not kept, not logged, not to the model.
    link = once["value"]
    assert link not in await stored_text()
    assert link not in caplog.text
    assert link not in json.dumps(await owner_device.since())
    for call in chat_server.calls[first:]:
        assert link not in call.system
        assert link not in json.dumps([dump(item) for item in call.messages])
    again = (await owner_device.prove(rid, "password", MEMBER_PASSWORD)).json()
    assert (again["outcome"], again["once"]) == ("already_decided", None)


async def test_the_conversation_kept_says_a_request_as_it_is_now(adult_device: ApiPilot):
    asked = await adult_device.say_with_tool(
        "profile_rename", {"name": "Zed Q"}, judge="proof: not sure"
    )
    rid = asked.request_id
    waits = t("action.card.waiting.self", "en")

    async def card_of_the_request() -> str:
        [kept] = [m for m in await adult_device.since() if m["id"] == str(asked.reply_id)]
        [card] = kept["cards"]
        return card

    assert (await card_of_the_request()).startswith(waits)
    adult_device.use_model(FakeChatModel(["Done."]))
    proven = await adult_device.prove(rid, "password", MEMBER_PASSWORD)
    assert proven.json()["outcome"] == "executed"
    now = await card_of_the_request()
    assert now and waits not in now
    assert now.startswith(proven.json()["sentence"].split(".")[0])


# --- the key of a model: a form this channel cannot open ----------------------------------------


async def model_results_since(server: ChatServer, first: int) -> list[dict]:
    """What the model read back of the tools, in the calls since ``first``."""
    return tool_results(server.calls[first:])


async def no_form_was_opened(gk_mock, audit_actions) -> None:
    assert not gk_mock.secret_requests.called
    assert "secret.requested" not in await audit_actions()


async def test_a_new_key_is_left_on_the_page_not_opened_here(
    adult_device: ApiPilot, adult_chat: ChatPilot, chat_server: ChatServer, gk_mock, audit_actions
):
    first = len(chat_server.calls)
    reply = await adult_device.say_with_tool("key_add", {"kind": "openai"}, judge="do")
    [acted] = reply.acted
    assert acted["sentence"] == t("action.card.done.key.add.yours.in_words", "en")
    assert "button" not in acted["sentence"].lower()
    assert acted["once"] is None
    assert (await model_results_since(chat_server, first))[-1]["key_form"] == "waits_for_them"
    await no_form_was_opened(gk_mock, audit_actions)
    page = (await adult_chat.get("/chat")).text
    assert key_new_button_url(page) == KEY_NEW_PATH


async def test_an_admin_key_is_left_on_the_page_not_opened_here(
    adult_device: ApiPilot, adult_chat: ChatPilot, chat_server: ChatServer, gk_mock, audit_actions
):
    key = await add_key(
        chat_server.session_factory,
        adult_chat.person.id,
        kind="anthropic",
        base_url="https://api.anthropic.com",
    )
    first = len(chat_server.calls)
    reply = await adult_device.say_with_tool("key_admin", {"key": key.id}, judge="do")
    [acted] = reply.acted
    assert acted["sentence"] == t("action.card.done.key.admin.yours.in_words", "en")
    assert "button" not in acted["sentence"].lower()
    assert acted["once"] is None
    assert (await model_results_since(chat_server, first))[-1]["admin_form"] == "waits_for_them"
    await no_form_was_opened(gk_mock, audit_actions)
    page = (await adult_chat.get("/chat")).text
    assert f'hx-get="{KEY_ADMIN_FORM_PATH.format(provider_id=key.id)}"' in page or (
        KEY_ADMIN_FORM_PATH.format(provider_id=key.id) in page
    )


async def test_the_models_key_given_again_is_left_on_the_page_not_opened_here(
    owner_device: ApiPilot, owner_chat: ChatPilot, chat_server: ChatServer, gk_mock, audit_actions
):
    asked = await owner_device.say_with_tool("model_rekey", {})
    rid = asked.request_id
    assert rid is not None and asked.acted[0]["request"]["answer_with"] == "yes"
    owner_device.use_model(FakeChatModel(["It waits on your page."]))
    answer = await owner_device.say("yes")
    [decided] = answer.acted
    assert decided["status"] == "executed"
    assert t("action.card.done.model.rekey.yours.in_words", "en") in decided["sentence"]
    assert "button" not in decided["sentence"].lower()
    assert decided["once"] is None
    told = json.loads(chat_server.calls[-1].system.split(DECIDED_MARK)[1].split("\n\n")[0])
    assert told["result"]["key_form"] == "waits_for_them"
    await no_form_was_opened(gk_mock, audit_actions)
    page = (await owner_chat.get("/chat")).text
    assert f"/chat/key/{chat_server.provider_id}/form?confirmation={rid}" in page


async def test_the_page_still_opens_the_form_at_once(adult_chat: ChatPilot, gk_mock):
    reply = await adult_chat.say_with_tool("key_add", {"kind": "openai"}, judge="do")
    assert 'src="/vault/r/token-req1"' in reply.cards_html
    assert reply.model_results[-1]["key_form_shown"] is True


# --- what cannot be read never ends a stream ------------------------------------------------------


async def test_a_transient_error_keeps_listening(
    owner_device: ApiPilot, chat_server: ChatServer, owner_member: Member, monkeypatch, caplog
):
    monkeypatch.setattr(api_routes, "SEEN_EVERY", 0.05)
    failed: list[bool] = []
    known = api_routes.device_of

    async def flaky(session, secret, now):
        if not failed:
            failed.append(True)
            raise ConnectionError("what the database said")
        return await known(session, secret, now)

    async with owner_device.listen() as listening:
        await until_hearing(chat_server, owner_member.person)
        monkeypatch.setattr(api_routes, "device_of", flaky)
        async with asyncio.timeout(WAIT_SECONDS):
            while not failed:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)  # a few more ticks
        assert not listening.ended.is_set() and hearing(chat_server, owner_member.person)
        await chat_server.app.state.listeners.say(
            owner_member.person.id, uuid.uuid4(), Words("Still here.")
        )
        [*_, heard] = await listening.until("words")
    assert heard.data["text"] == "Still here."
    assert "ConnectionError" in caplog.text and "what the database said" not in caplog.text


async def test_a_request_that_cannot_be_read_is_said_without_how_to_answer_it(
    adult_device: ApiPilot, monkeypatch, caplog
):
    def unreadable(c):
        raise ValueError("a row edited by hand")

    monkeypatch.setattr(api_render, "ask_of", unreadable)
    reply = await adult_device.say_with_tool("profile_rename", {"name": "Alex B"})
    [acted] = reply.acted
    assert acted["status"] == "waiting"
    assert acted["request"]["answer_with"] is None and acted["request"]["proofs"] == []
    assert reply.events[-1].name == "ended"
    assert "ValueError" in caplog.text and "edited by hand" not in caplog.text


# --- pairing and the gates ------------------------------------------------------------------------


async def test_pairing_gives_a_secret_once_and_a_refused_code_says_nothing_more(
    chat_server: ChatServer, adult_member: Member, caplog
):
    caplog.set_level(logging.DEBUG)
    code = await pairing_code(chat_server, adult_member)
    async with plain_client(chat_server) as client:
        bad_name = await client.post(f"{API}/pair", json={"code": code, "name": "a\nb"})
        assert (bad_name.status_code, bad_name.json()) == (400, {"error": "bad_name"})
        paired = await client.post(f"{API}/pair", json={"code": code, "name": "Laptop"})
        assert paired.status_code == 200
        body = paired.json()
        # And the name Alex calls Domo by, none yet (0.9.0, spec 6.7).
        assert set(body) == {"device_id", "secret", "person", "domo_name"}
        assert body["person"] == "Alex" and body["domo_name"] is None
        assert "set-cookie" not in paired.headers
        again = await client.post(f"{API}/pair", json={"code": code, "name": "Laptop"})
        assert (again.status_code, again.json()) == (404, {"error": "unknown_code"})
    assert body["secret"] not in caplog.text and code not in caplog.text


async def test_pairing_is_held_back_per_address(chat_server: ChatServer):
    async with plain_client(chat_server) as client:
        for _ in range(5):
            refused = await client.post(
                f"{API}/pair", json={"code": "AAAA-AAAA-AAAA", "name": "Laptop"}
            )
            assert refused.status_code == 404
        held = await client.post(f"{API}/pair", json={"code": "AAAA-AAAA-AAAA", "name": "Laptop"})
        assert held.status_code == 429 and int(held.headers["retry-after"]) > 0
        assert held.json() == {"error": "held_back"}


async def test_an_unknown_secret_is_refused_without_a_redirect(chat_server: ChatServer):
    async with plain_client(chat_server) as client:
        for headers in ({}, {"authorization": "Bearer nope"}, {"authorization": "Basic x"}):
            refused = await client.get(f"{API}/since", headers=headers)
            assert (refused.status_code, refused.json()) == (401, {"error": "unknown_device"})
        missing = await client.get(f"{API}/nothing-here")
        assert missing.status_code == 404 and missing.json() == {"error": "not_found"}


async def test_the_api_refuses_a_browser_session(
    owner_device: ApiPilot, live_owner_client: httpx.AsyncClient
):
    token = live_owner_client.cookies[SESSION_COOKIE]
    for method, path in (("POST", f"{API}/messages"), ("GET", f"{API}/since")):
        refused = await owner_device.client.request(
            method, path, json={"text": "Hi"}, headers={"cookie": f"{SESSION_COOKIE}={token}"}
        )
        assert (refused.status_code, refused.json()) == (403, {"error": "browser_session"})
    # The browser itself, with its Origin, is refused too.
    refused = await live_owner_client.post(f"{API}/messages", json={"text": "Hi"})
    assert refused.status_code == 403
    # And no answer of the API sets a cookie.
    owner_device.use_model(FakeChatModel(["ok"]))
    answer = await owner_device.client.post(f"{API}/messages", json={"text": "Hi"})
    assert "set-cookie" not in answer.headers


async def test_no_origin_is_needed_with_a_device_secret(owner_device: ApiPilot):
    owner_device.use_model(FakeChatModel(["ok"]))
    assert "origin" not in owner_device.client.headers
    reply = await owner_device.say("Hi")
    assert reply.said == "ok"
    # Nor does one from elsewhere matter: the secret is what counts.
    other = await owner_device.client.post(
        f"{API}/messages", json={"text": "Again"}, headers={"origin": "https://elsewhere.example"}
    )
    assert other.status_code == 200
    await owner_device.read_stream(uuid.UUID(other.json()["reply_id"]))


async def test_the_api_says_503_before_setup(app_factory, client_factory):
    app = app_factory()
    async with client_factory(app, origin=None) as client:
        for method, path in (("POST", f"{API}/pair"), ("POST", f"{API}/messages")):
            refused = await client.request(method, path, json={"code": "x", "name": "y"})
            assert (refused.status_code, refused.json()) == (503, {"error": "not_set_up"})
            assert "location" not in refused.headers


@pytest.mark.parametrize("outcome", ["needs_explicit", "needs_proof"])
def test_what_a_yes_in_words_still_needs_is_said_without_a_card_or_a_button(outcome):
    """The page says these notes with its card and its button (``confirmation.outcome.<outcome>``);
    this channel has neither: what the request still needs is said so that it is true here."""
    page_note = REFERENCE(f"confirmation.outcome.{outcome}")
    card = ConfirmationCard(id=str(uuid.uuid4()), status="pending", outcome=outcome, note=page_note)
    said = api_render.decision_sentence(card, REFERENCE)
    assert said == REFERENCE(f"confirmation.outcome.{outcome}.in_words")
    for word in ("button", "card"):
        assert word not in said.lower()
    for language in ("en", "fr"):
        for word in ("button", "card", "bouton", "carte"):
            assert word not in t(f"confirmation.outcome.{outcome}.in_words", language).lower()
    # Any other note is the page's own.
    refused = ConfirmationCard(
        id=str(uuid.uuid4()), status="pending", outcome="expired", note="It expired."
    )
    assert api_render.decision_sentence(refused, REFERENCE) == "It expired."


def test_the_cards_of_a_form_left_for_its_reader_are_said_in_words_here():
    """A form left on the card of the one who asked sends them to the button under it, whoever
    the card speaks to (``.on_request``: to the approver too; ``.yours``: to them alone)."""
    for action in ("model.rekey", "key.add", "key.admin"):
        for reader in ("on_request", "yours"):
            assert f"action.card.done.{action}.{reader}" in api_render.CARDS_IN_WORDS


@pytest.mark.parametrize("key", sorted(api_render.CARDS_IN_WORDS))
def test_a_card_that_points_at_a_button_is_said_in_words_here(key):
    """The page says these cards with the button under them; this channel has none: their
    sentence says where the person finds it, in their texts, wherever it is said."""
    page_sentence = REFERENCE(key)
    assert api_render.in_words(f"Done. {page_sentence}", REFERENCE) == (
        f"Done. {REFERENCE(f'{key}.in_words')}"
    )
    assert api_render.sentence_of_card({"key": key, "params": {}}, REFERENCE) == REFERENCE(
        f"{key}.in_words"
    )
    for language in ("en", "fr"):
        for word in ("button", "bouton"):
            assert word not in t(f"{key}.in_words", language).lower()


async def test_the_exact_words_of_a_note_are_said_in_full_on_the_api(
    adult_device: ApiPilot, owner_member: Member, leave_note
):
    # F2 (the owner's trial of 0.9.0): where a channel has no cards, the card of note_show is said
    # in words, the exact words in full; and Domo is given them to quote himself.
    words = "Je regarde ça demain, pour que tu puisses piloter la maison."
    note = await leave_note(
        owner_member.person, adult_device.member.person, "He will look into it.", exact=words
    )
    reply = await adult_device.say_with_tool("note_show", {"note": str(note.id)}, judge="do")
    [acted] = reply.acted
    assert acted["status"] == "done" and words in acted["sentence"]
    [_, stored] = await adult_device.since()
    assert any(words in card for card in stored["cards"])
