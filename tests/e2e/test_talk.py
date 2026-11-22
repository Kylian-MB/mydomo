"""The second channel (0.8.0, spec ``2026-11-12-le-canal-et-l-identite``, section 7, points 5
and 6), against the real Docker Compose stack, with the fake model service in place of the provider
(``tests/fakes/fake_llm``): the owner entrusts a computer to Domo from his page, and talks to him
from it through the channel of the API (``/api/channel``), as the text program does
(``packages/domo-talk``). The program itself is not driven here: these are the requests it
makes, over HTTPS, trusting the authority ``/ca.crt`` serves, as the README tells a person to.

One scenario, on an instance of its own (``talk_stack``), new when the module starts, whose first
launch gives the household's key the fake's marking address (``MARKED_BASE_URL``): there, an
arrival or a news turn names who left the notes it is told (``Fake arrival: notes from Alex``),
so that what Domo says on his own can be told apart. In it:

* the owner asks, on his page, to entrust a computer: the code is shown once, in the answer, and
  never again;
* the computer pairs with that code (``POST /api/channel/pair``), which serves once;
* a message sent from the computer is answered word by word, and the owner's open page shows it
  and its reply, with nothing typed there;
* a note Alex leaves for the owner reaches the computer's ``listen`` stream, with nothing sent from
  the computer;
* a request that waits for a proof is proven from the computer with the owner's password: the
  request runs, Domo says so on the stream, and the password is nowhere in the conversation or
  the audit log.
"""

import json
import queue
import re
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, expect

from tests.fakes.fake_llm.app import ARRIVAL_NOTES_PREFIX, MARKED_PREFIX
from tests.stack.stack_support import StackInfo, running_stack_with_fake_llm

from .conftest import FRESH_ENV_FILE, FRESH_PROJECT
from .support import (
    ALEX,
    EXPECT_TIMEOUT_MS,
    FAKE_LLM_BASE_URL,
    OWNER_DISPLAY_NAME,
    OWNER_PASSWORD,
    Household,
    assistant_replies,
    database_text,
    finish_the_first_launch,
    invite_and_join,
    judged,
    reply_text,
    send_and_wait,
    tool_message,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
API = "/api/channel"
# The address of the fake under which an arrival or a news turn names who left its notes.
MARKED_BASE_URL = FAKE_LLM_BASE_URL.removesuffix("/v1") + MARKED_PREFIX + "/v1"
# What a code to entrust a device looks like (``devices.service``).
CODE = re.compile(r"\b[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\b")
# What the owner's open page gets by asking for it every 5 seconds, after the model wrote it.
ASKED_FOR_MS = 60_000
# How long a stream may wait for what Domo says on his own: a news turn is offered at most once
# a minute (the owner's arrival when he signed in may be that recent), then written.
HEARD_WITHIN_S = 150.0
SAID_FROM_THE_COMPUTER = "Hello from the terminal"
NOTE_FOR_THE_OWNER = "buy bread on the way home"
GUEST = {"display_name": "Rae", "role": "guest", "guest_days": 2}


@dataclass(frozen=True)
class Event:
    """An event of a stream of the channel: its name and its data (one JSON object)."""

    name: str
    data: dict[str, Any]


def events_of(lines: Iterator[str]) -> Iterator[Event]:
    """The events of a server-sent events stream, read line by line (a comment is skipped)."""
    name, data = "message", ""
    for line in lines:
        if line == "":
            if data:
                yield Event(name, json.loads(data))
            name, data = "message", ""
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data += line.removeprefix("data:").strip()


def trusting_the_stack(stack: StackInfo) -> ssl.SSLContext:
    """What a computer does once it trusts the authority the stack serves at ``/ca.crt``: the
    channel's certificate then verifies (no check of it is turned off)."""
    authority = httpx.get(stack.base_url + "/ca.crt", verify=False).text
    return ssl.create_default_context(cadata=authority)


class Computer:
    """The owner's computer, as the text program speaks for it: HTTPS to the channel of the API,
    with the device's secret once paired, and a ``listen`` stream read in a thread of its own."""

    def __init__(self, stack: StackInfo) -> None:
        self._client = httpx.Client(
            base_url=stack.base_url, verify=trusting_the_stack(stack), timeout=30
        )
        self._secret: str | None = None
        self.heard: queue.Queue[Event] = queue.Queue()
        self._listening: threading.Thread | None = None

    def pair(self, code: str, name: str) -> httpx.Response:
        answer = self._client.post(f"{API}/pair", json={"code": code, "name": name})
        if answer.status_code == 200:
            self._secret = answer.json()["secret"]
        return answer

    @property
    def _auth(self) -> dict[str, str]:
        assert self._secret is not None, "the computer is not paired"
        return {"Authorization": f"Bearer {self._secret}"}

    def say(self, text: str) -> list[Event]:
        """Send ``text``, and read the reply's stream to its end."""
        sent = self._client.post(f"{API}/messages", json={"text": text}, headers=self._auth)
        assert sent.status_code == 200, sent.text
        reply_id = sent.json()["reply_id"]
        path = f"{API}/stream/{reply_id}"
        with self._client.stream("GET", path, headers=self._auth, timeout=120) as stream:
            assert stream.status_code == 200
            return list(events_of(stream.iter_lines()))

    def prove(self, request_id: str, password: str) -> httpx.Response:
        return self._client.post(
            f"{API}/confirmations/{request_id}/prove",
            json={"means": "password", "answer": password},
            headers=self._auth,
        )

    def since(self) -> list[dict[str, Any]]:
        answer = self._client.get(f"{API}/since", headers=self._auth)
        assert answer.status_code == 200
        return answer.json()["messages"]

    def listen(self) -> None:
        """Open the ``listen`` stream, read in a thread until the computer is closed."""
        opened = threading.Event()

        def read() -> None:
            timeout = httpx.Timeout(30, read=None)
            try:
                path = f"{API}/listen"
                with self._client.stream("GET", path, headers=self._auth, timeout=timeout) as s:
                    assert s.status_code == 200
                    opened.set()
                    for event in events_of(s.iter_lines()):
                        self.heard.put(event)
            except httpx.HTTPError:
                pass  # the computer was closed
            finally:
                opened.set()

        self._listening = threading.Thread(target=read, daemon=True)
        self._listening.start()
        assert opened.wait(30), "the listen stream did not open"

    def said_on_its_own(self, wanted: re.Pattern[str]) -> str:
        """What Domo said on his own, on the ``listen`` stream: the words of the first reply
        that matches ``wanted`` (each reply's words, put together, up to its end)."""
        words: dict[str, str] = {}
        while True:
            try:
                event = self.heard.get(timeout=HEARD_WITHIN_S)
            except queue.Empty:
                raise AssertionError(
                    f"The assistant said nothing like {wanted.pattern!r}"
                ) from None
            message_id = event.data.get("message_id", "")
            if event.name == "words":
                words[message_id] = words.get(message_id, "") + event.data["text"]
            elif event.name == "ended" and wanted.fullmatch(words.get(message_id, "")):
                return words[message_id]

    def close(self) -> None:
        self._client.close()


def words_of(events: list[Event]) -> str:
    return "".join(event.data["text"] for event in events if event.name == "words")


@pytest.fixture(scope="module")
def talk_stack() -> Iterator[StackInfo]:
    """An instance for this module alone, new when it starts, taken down with its volumes at its
    end (the project, ports and network of the stacks of one test: see ``conftest.py``)."""
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def people(
    talk_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, after a first launch made with the key of
    the marking address, and the members who join, each in a browser context of their own."""
    finish_the_first_launch(talk_stack, browser, browser_context_args, base_url=MARKED_BASE_URL)
    household = Household(talk_stack, browser, browser_context_args)
    try:
        yield household
    finally:
        household.close()


@pytest.fixture
def computer(talk_stack: StackInfo) -> Iterator[Computer]:
    device = Computer(talk_stack)
    try:
        yield device
    finally:
        device.close()


def test_a_person_entrusts_a_computer_and_talks_from_it(people: Household, computer: Computer):
    stack, owner = people.stack, people.owner
    # Someone who will leave the owner a note.
    alex = invite_and_join(people, ALEX)

    # On his page, the owner asks to entrust his computer: the code is shown once, in the answer.
    entrusted = send_and_wait(owner, judged("do", tool_message("device_entrust", {})))
    once = entrusted.locator(".action-once")
    expect(once).to_have_text(CODE, timeout=T)
    found = CODE.search(once.inner_text())
    assert found is not None
    code = found.group(0)
    owner.reload()
    expect(assistant_replies(owner).last).to_be_visible(timeout=T)
    assert code not in owner.content(), "the code is shown again"

    # The computer pairs with it, once.
    paired = computer.pair(code, "Laptop")
    assert paired.status_code == 200, paired.text
    assert paired.json()["person"] == OWNER_DISPLAY_NAME
    again = Computer(stack)
    try:
        refused = again.pair(code, "Another")
        assert (refused.status_code, refused.json()) == (404, {"error": "unknown_code"})
    finally:
        again.close()

    # A message from the computer: answered word by word, and the owner's open page shows it.
    events = computer.say(SAID_FROM_THE_COMPUTER)
    assert words_of(events) == f"Fake reply: {SAID_FROM_THE_COMPUTER}"
    assert events[-1] == Event("ended", {"status": "complete"})
    said = owner.locator(".message.user .message-text").last
    expect(said).to_have_text(SAID_FROM_THE_COMPUTER, timeout=ASKED_FOR_MS)
    expect(reply_text(assistant_replies(owner).last)).to_have_text(
        f"Fake reply: {SAID_FROM_THE_COMPUTER}", timeout=ASKED_FOR_MS
    )

    # A note Alex leaves for the owner reaches the computer, with nothing sent from it.
    computer.listen()
    note = {"recipient": OWNER_DISPLAY_NAME, "text": NOTE_FOR_THE_OWNER}
    left = send_and_wait(alex, judged("do", tool_message("note_leave", note)))
    expect(left.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    told = re.compile(rf"^{re.escape(ARRIVAL_NOTES_PREFIX)}{re.escape(ALEX.display_name)}$")
    computer.said_on_its_own(told)

    # A request that waits for a proof, proven from the computer with the owner's password.
    asked = computer.say(judged("proof: x", tool_message("household_invite", GUEST)))
    [acted] = [event for event in asked if event.name == "acted"]
    request = acted.data["request"]
    assert request["answer_with"] == "proof" and "password" in request["proofs"]
    wrong = computer.prove(request["id"], "not-the-password-0000")
    assert (wrong.status_code, wrong.json()["outcome"]) == (200, "proof_failed")
    assert wrong.json()["request"] is not None, "the request no longer waits"
    proven = computer.prove(request["id"], OWNER_PASSWORD)
    assert proven.status_code == 200, proven.text
    assert (proven.json()["outcome"], proven.json()["request"]) == ("executed", None)
    # The invitation's link reaches the computer that proved it, in that answer only.
    link = proven.json()["once"]
    assert link["kind"] == "link" and link["value"].startswith("https://"), link
    assert all(link["value"] not in json.dumps(message) for message in computer.since())
    open_invitations = database_text(
        stack,
        "SELECT count(*) FROM invitations WHERE accepted_at IS NULL AND cancelled_at IS NULL "
        f"AND display_name = '{GUEST['display_name']}' AND role = 'guest'",
    )
    assert open_invitations == "1"
    # Domo, woken by the proof, says what came of it, on the stream.
    computer.said_on_its_own(re.compile(r"^Fake decided: executed$"))
    # The password went to the code only: not to the conversation, not to the audit log.
    assert all(OWNER_PASSWORD not in message["text"] for message in computer.since())
    for query in (
        f"SELECT count(*) FROM messages WHERE content LIKE '%{OWNER_PASSWORD}%'",
        f"SELECT count(*) FROM audit_log WHERE details::text LIKE '%{OWNER_PASSWORD}%'",
    ):
        assert database_text(stack, query) == "0", query
