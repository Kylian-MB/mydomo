"""Memory and time (0.9.0, spec ``2026-11-15-la-memoire-et-le-temps``), against the real Docker
Compose stack, with the fake model service in place of the provider (``tests/fakes/fake_llm``).

The fake makes Domo call a tool on demand (``/tool <name> <json>``), says which plans woke him in
a turn a plan started (``Fake plan: <why>``), and says what the context of a turn gives of his
memory when it is asked ``/memory`` (``Fake memory: <contents>``, and ``(words only)`` when the
search works by the words alone): what the model was given is read in the chat, as a person would
read Domo's answer.

One instance for the module (``memory_stack``), new when it starts, taken down with its volumes at
its end: the scenarios restart its core, which no other module's instance should see. Its upkeep
looks every 5 seconds (``fresh.env``), so a plan due in a few seconds wakes Domo without the
test waiting the minute of a real instance; the plan is set a little ahead of the clock rather
than a minute (the code refuses only a time already past). In it, the owner, Alex (an adult) and
"Sam Martin" (a child), each in a browser of their own, and the owner's computer, paired as the
text program pairs (``test_talk.Computer``):

a. a plan due in a few seconds: Domo speaks first, in the owner's open page with nothing sent
   from it, and on the computer's ``listen`` stream (what ``domo-talk`` shows); the plan is done;
b. what Domo keeps in his memory is in the context of the next turn, after the core was
   restarted (a new container: nothing of it lived in the core's process);
c. Alex calls Domo "Brenda": his page says Brenda where it said Domo, Sam's still says Domo;
d. Alex says "tell Sam that..." with the first name alone: the note is for Sam Martin, and the
   reply shows one card, the note's, and no other;
e. the core started with the folder of its local model empty (``compose.no-model.yaml``) answers,
   and finds a memory by its words alone (the context says so).
"""

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_common.i18n import t
from playwright.sync_api import Browser, Page, expect

from tests.fakes.fake_llm.app import MEMORY_PREFIX, PLAN_PREFIX, WORDS_ONLY_SUFFIX
from tests.stack.stack_support import (
    UP_TIMEOUT_S,
    StackInfo,
    run,
    running_stack_with_fake_llm,
    wait_until_ready,
)

from .conftest import FRESH_ENV_FILE, FRESH_PROJECT
from .support import (
    ALEX,
    EXPECT_TIMEOUT_MS,
    OWNER_DISPLAY_NAME,
    Household,
    Member,
    assistant_replies,
    database_text,
    finish_the_first_launch,
    invite_and_join,
    judged,
    reply_text,
    send_and_wait,
    tool_message,
    wait_for_a_new_reply,
)
from .test_talk import CODE, Computer

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
# A child of the household whose first name alone is what Alex says.
SAM_MARTIN = Member("Sam Martin", "child", "sam", "sam-test-passphrase")
# How far ahead of the clock the plan is set, and how long the owner's page may wait for Domo
# once it is due: the upkeep's next pass (every 5 seconds), the turn, then the page's asking.
PLAN_AHEAD = timedelta(seconds=45)
PLAN_MS = 120_000
# How long the plan may take to be settled once its turn was shown.
SETTLED_WAIT_S = 30
PLAN_WHY = "Remind the owner to take the bins out"
# What the owner tells Domo to keep in mind, and what Alex tells him, kept out of the core.
KEPT_IN_MIND = "The dog of the house is called Rex"
BOILER = "The boiler clicks at night"
NOTE_FOR_SAM = "Dinner is at eight"
# The compose file that empties the folder of the core's local model, and that folder.
NO_MODEL_FILE = "tests/e2e/compose.no-model.yaml"
NO_MODEL_DIR = "/opt/domo/no-model"
# What the core logs when its local model cannot be loaded (memory/embedder.py).
MODEL_UNAVAILABLE = "The local model is unavailable (FileNotFoundError)"
LOG_WAIT_S = 30


@pytest.fixture(scope="module")
def memory_stack() -> Iterator[StackInfo]:
    """An instance for this module alone, new when it starts, taken down with its volumes at its
    end (the project, ports and network of the stacks of one test: see ``conftest.py``)."""
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def people(
    memory_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, and Alex and Sam Martin, whom the owner
    invites (the judge lets it through) and who join, each in a browser context of their own."""
    finish_the_first_launch(memory_stack, browser, browser_context_args)
    household = Household(memory_stack, browser, browser_context_args)
    try:
        invite_and_join(household, ALEX)
        invite_and_join(household, SAM_MARTIN)
        yield household
    finally:
        household.close()


@pytest.fixture(scope="module")
def computer(people: Household) -> Iterator[Computer]:
    """The owner's computer, entrusted from his page and paired with the code shown once."""
    entrusted = send_and_wait(people.owner, judged("do", tool_message("device_entrust", {})))
    once = entrusted.locator(".action-once")
    expect(once).to_have_text(CODE, timeout=T)
    found = CODE.search(once.inner_text())
    assert found is not None
    device = Computer(people.stack)
    try:
        paired = device.pair(found.group(0), "Laptop")
        assert paired.status_code == 200, paired.text
        assert paired.json()["person"] == OWNER_DISPLAY_NAME
        yield device
    finally:
        device.close()


def recreate_the_core(stack: StackInfo, *more_files: str) -> None:
    """Stop the core and start a new container of it (its image as built, the other services left
    as they are), with ``more_files`` added to the stack's compose files; wait until it is healthy
    and the page answers again."""
    added = [option for name in more_files for option in ("-f", name)]
    command = [*stack.compose_command, *added]
    command += ["up", "--detach", "--no-deps", "--force-recreate", "--wait", "core"]
    started = run(command, env=stack.env, timeout=UP_TIMEOUT_S)
    assert started.returncode == 0, started.stderr[-4000:]
    wait_until_ready(stack.base_url)


def back_on_the_chat(page: Page, stack: StackInfo) -> None:
    """Open the chat again in ``page`` (its session lives in the database: a new core keeps it)."""
    page.goto(stack.base_url + "/chat")
    expect(page).to_have_url(re.compile(r"/chat$"), timeout=T)


def wait_for_the_log(stack: StackInfo, service: str, line: str) -> None:
    deadline = time.monotonic() + LOG_WAIT_S
    while line not in stack.logs(service):
        if time.monotonic() > deadline:
            raise AssertionError(f"the log of {service} has no {line!r}")
        time.sleep(1)


def test_a_plan_due_makes_domo_speak_first_on_the_page_and_the_computer(
    people: Household, computer: Computer
):
    owner, stack = people.owner, people.stack
    computer.listen()
    # Counted before the message is sent: the reply to it is the next bubble, Domo's own words
    # the one after, however soon the plan is due.
    before = assistant_replies(owner).count()
    # The time is read at the last moment, with room for the turn that sets it.
    when = (datetime.now(UTC) + PLAN_AHEAD).isoformat(timespec="seconds")
    reply = send_and_wait(owner, tool_message("plan_set", {"why": PLAN_WHY, "when": when}))
    # Never judged, never shown: set at once, with no card.
    expect(reply_text(reply)).to_have_text("Fake tool result: done")
    expect(reply.locator(".action-card")).to_have_count(0)

    # Its time comes: Domo speaks first, in the open page, with nothing sent from it...
    heard = wait_for_a_new_reply(owner, before + 1, timeout_ms=PLAN_MS)
    expect(reply_text(heard)).to_have_text(PLAN_PREFIX + PLAN_WHY, timeout=T)
    # ...and on the computer's stream, where domo-talk shows what he says on his own.
    computer.said_on_its_own(re.compile(rf"^{re.escape(PLAN_PREFIX + PLAN_WHY)}$"))
    # The plan was given once: it is done (settled once the turn is over: asked again until a
    # deadline), and nothing more comes of it.
    state = f"SELECT state FROM plans WHERE why = '{PLAN_WHY}'"
    deadline = time.monotonic() + SETTLED_WAIT_S
    while (found := database_text(stack, state)) != "done" and time.monotonic() < deadline:
        time.sleep(1)
    assert found == "done"


def test_what_domo_keeps_is_in_the_next_turn_after_the_core_restarts(people: Household):
    owner, stack = people.owner, people.stack
    memory = {
        "content": KEPT_IN_MIND,
        "subject": "the house",
        "kind": "fact",
        "private": False,
        "core": True,
    }
    kept = send_and_wait(owner, tool_message("memory_keep", memory))
    expect(reply_text(kept)).to_have_text("Fake tool result: done")
    expect(kept.locator(".action-card")).to_have_count(0)
    in_the_core = f"SELECT core FROM memories WHERE content = '{KEPT_IN_MIND}'"
    assert database_text(stack, in_the_core) == "t"

    recreate_the_core(stack)
    back_on_the_chat(owner, stack)
    told = send_and_wait(owner, "/memory")
    expect(reply_text(told)).to_have_text(
        re.compile(rf"^{re.escape(MEMORY_PREFIX + KEPT_IN_MIND)}")
    )


def test_alex_calls_domo_brenda_and_sams_page_still_says_domo(people: Household):
    alex, sam, stack = people.page_of(ALEX), people.page_of(SAM_MARTIN), people.stack
    back_on_the_chat(alex, stack)
    named = send_and_wait(alex, tool_message("domo_name", {"name": "Brenda"}))
    expect(reply_text(named)).to_have_text("Fake tool result: done")
    expect(named.locator(".action-card")).to_have_count(0)
    alex.reload()
    expect(alex.locator("a.brand")).to_have_text("Brenda")
    expect(alex).to_have_title(re.compile(r" - Brenda$"))
    back_on_the_chat(sam, stack)
    expect(sam.locator("a.brand")).to_have_text("Domo")
    expect(sam).to_have_title(re.compile(r" - Domo$"))


def test_a_first_name_alone_finds_the_person_and_shows_one_card(people: Household):
    alex, stack = people.page_of(ALEX), people.stack
    first_name = SAM_MARTIN.display_name.split()[0]
    note = {"recipient": first_name, "text": NOTE_FOR_SAM}
    reply = send_and_wait(alex, judged("do", tool_message("note_leave", note)))
    expect(reply_text(reply)).to_have_text("Fake tool result: done")
    # One card, the note's, which names Sam as the household knows them; no other.
    cards = reply.locator(".action-card")
    expect(cards).to_have_count(1)
    expect(cards).to_have_attribute("data-action-status", "done", timeout=T)
    expect(cards.locator(".action-text")).to_have_text(
        t("action.card.done.note.leave", "en", name=SAM_MARTIN.display_name)
    )
    expect(reply.locator(".confirmation-card")).to_have_count(0)
    received = (
        "SELECT count(*) FROM notes n JOIN persons p ON p.id = n.recipient_id "
        f"WHERE p.display_name = '{SAM_MARTIN.display_name}' AND n.text = '{NOTE_FOR_SAM}'"
    )
    assert database_text(stack, received) == "1"


def test_without_the_local_model_domo_answers_and_finds_by_the_words(people: Household):
    owner, stack = people.owner, people.stack
    memory = {
        "content": BOILER,
        "subject": "the boiler",
        "kind": "fact",
        "private": False,
        "core": False,
    }
    kept = send_and_wait(owner, tool_message("memory_keep", memory))
    expect(reply_text(kept)).to_have_text("Fake tool result: done")

    recreate_the_core(stack, NO_MODEL_FILE)
    # The folder is there and empty: the core cannot load its model, and says so in its log.
    listed = stack.exec("core", ["ls", "-A", NO_MODEL_DIR])
    assert (listed.returncode, listed.stdout.strip()) == (0, ""), listed.stderr
    wait_for_the_log(stack, "core", MODEL_UNAVAILABLE)

    back_on_the_chat(owner, stack)
    found = send_and_wait(owner, "/memory boiler")
    expected = rf"^{re.escape(MEMORY_PREFIX)}.*{re.escape(BOILER)}{re.escape(WORDS_ONLY_SUFFIX)}$"
    expect(reply_text(found)).to_have_text(re.compile(expected))
