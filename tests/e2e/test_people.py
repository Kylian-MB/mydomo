"""The assistant makes the people of the house (spec ``2026-11-23-domo-cree-les-personnes``), in
real browsers, against the real Docker Compose stack, with the fake model service in place of the
provider (``tests/fakes/fake_llm``).

The fake makes Domo act on demand: one call (``/tool <name> <json>``), or several in the same turn
(``/calls <json>``, a list of ``{"tool", "arguments"}``: ``act_on_my_own`` and the gesture it
marks, say), and it ends such a turn with the status of each call (``Fake calls: done, ...``). So
the records, the accesses and the refusals are the core's own, end to end: nothing of them is
faked but the model's words. A gesture on people nobody asked for is the assistant's idea: he
marks it first (``act_on_my_own``, with his reason) and gives the tool the same reason.

On an instance of this module's own (new when it starts, taken down at its end: the shared one
has lost its first owner by the end of ``test_household.py``), the owner of the instance is the
one who speaks (Alex, in the story of the plan), in the order of the file:

1. the owner says his parents come on Saturday: the assistant opens a record for each, on his own
   idea, with his reason, and nobody is asked anything; the household page shows two people
   known to the assistant, without access and without an account;
2. "give the house to my parents": both have an access, and still no account, as the household
   page shows;
3. the owner asks for a link for Dana, who lives here: the assistant opens her record (his idea)
   and invites that record (the owner's request); the page of the link proposes a username, Dana
   chooses a password, and the household page shows the same record, with an account now;
4. the owner withdraws Sam's access (his own order: a withdrawal); later the assistant, on his own
   idea, tries to bring Sam back: the code refuses it before any judgement, the reason is kept
   with the stored action, and Sam's access stays withdrawn.

Each scenario reads its own replies only: the page of the owner is shared by the module. Where the
page cannot show it (the reason of a refusal, the kind of an invitation), the database is read.
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest
from domo_common.i18n import t
from playwright.sync_api import Browser, Locator, Page, expect

from tests.fakes.fake_llm.app import CALLS_COMMAND, CALLS_PREFIX, TOOL_RESULT_PREFIX
from tests.stack.stack_support import StackInfo, running_stack_with_fake_llm

from .conftest import FRESH_ENV_FILE, FRESH_PROJECT
from .support import (
    EXPECT_TIMEOUT_MS,
    Household,
    assistant_replies,
    database_text,
    finish_the_first_launch,
    invitation_link,
    judged,
    reply_text,
    send_and_wait,
    tool_message,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
# The owner's parents, who come on Saturday, and Dana, who lives here.
PARENTS = ("Sam", "Tom")
DANA = "Dana"
DANA_USERNAME = "dana"
DANA_PASSWORD = "dana-test-passphrase"
# The assistant's reasons, one sentence each, given to act_on_my_own and to the tool alike.
PARENTS_COME = "The owner's parents come on Saturday: they will be in the house."
DANA_LIVES_HERE = "Dana lives here and is about to be given an access."
SAM_IS_BACK = "Sam is back in town and used to come and go."
# What the household page says of a record that has no account (the locale's words).
NO_ACCOUNT = t("household.no_account", "en")
OPENED_BY_DOMO = t("household.opened_by_domo", "en")


@pytest.fixture(scope="module")
def people_stack() -> Iterator[StackInfo]:
    """An instance for this module alone, new when it starts, taken down with its volumes at its
    end (the project, ports and network of the stacks of one test: see ``conftest.py``)."""
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def household(
    people_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, and the people who join during the module,
    each in a browser context of their own."""
    finish_the_first_launch(people_stack, browser, browser_context_args)
    people = Household(people_stack, browser, browser_context_args)
    try:
        yield people
    finally:
        people.close()


def calls(*steps: tuple[str, Mapping[str, Any]]) -> str:
    """The command that makes the fake model make the calls ``steps`` (a tool's name and its
    arguments) one after the other, in the same turn (``tests/fakes/fake_llm``)."""
    script = [{"tool": name, "arguments": dict(arguments)} for name, arguments in steps]
    return CALLS_COMMAND + json.dumps(script, ensure_ascii=False, separators=(",", ":"))


def said_of(statuses: Sequence[str]) -> str:
    """What the fake says at the end of a turn of several calls that ended with ``statuses``."""
    return CALLS_PREFIX + ", ".join(statuses)


def his_own(reason: str) -> tuple[str, Mapping[str, Any]]:
    """The call that marks the next one as the assistant's own idea, with ``reason``."""
    return ("act_on_my_own", {"reason": reason})


def nothing_asked(page: Page, since: int) -> None:
    """Nobody was asked anything: no request card under any of the assistant's replies from the
    ``since``-th on (those of the scenario), nor among the requests waiting for a yes. The replies
    before are earlier scenarios' of the module, whose page it shares."""
    replies = assistant_replies(page)
    for index in range(since, replies.count()):
        expect(replies.nth(index).locator(".confirmation-card")).to_have_count(0)
    expect(page.locator("#approvals .confirmation-card")).to_have_count(0)


def open_the_household_page(household: Household) -> Page:
    """The owner's household page (read only)."""
    owner = household.owner
    owner.goto(household.stack.base_url + "/household")
    expect(owner).to_have_url(re.compile(r"/household$"))
    return owner


def back_to_the_chat(household: Household) -> None:
    household.owner.goto(household.stack.base_url + "/chat")


def row_of(page: Page, name: str) -> Locator:
    """The row of the household page of the record named ``name``."""
    return page.locator(".household-person").filter(
        has=page.locator(".household-name", has_text=re.compile(rf"^{re.escape(name)}$"))
    )


def says(row: Locator, words: str) -> Locator:
    """The words of ``row`` that are exactly ``words``."""
    return row.locator(".household-state").filter(has_text=re.compile(rf"^{re.escape(words)}$"))


# --- the assistant opens the records of people who come ----------------------------------------


def test_the_parents_who_come_get_a_record_each_and_nobody_is_asked(household: Household):
    owner, stack = household.owner, household.stack
    start = assistant_replies(owner).count()
    opening = [
        step
        for parent in PARENTS
        for step in (
            his_own(PARENTS_COME),
            (
                "record_open",
                {
                    "name": parent,
                    "standing": "visiting",
                    "ground": "will_come",
                    "reason": PARENTS_COME,
                },
            ),
        )
    ]
    reply = send_and_wait(owner, "My parents come on Saturday.\n" + calls(*opening))
    expect(reply_text(reply)).to_have_text(said_of(["done"] * len(opening)), timeout=T)
    nothing_asked(owner, start)

    # Two records, known to the assistant, opened by him: no access, no account.
    page = open_the_household_page(household)
    for parent in PARENTS:
        row = row_of(page, parent)
        expect(row).to_have_count(1)
        expect(row).to_have_attribute("data-person-state", "known")
        expect(says(row, NO_ACCOUNT)).to_have_count(1)
        expect(says(row, OPENED_BY_DOMO)).to_have_count(1)
    back_to_the_chat(household)
    # His reason is kept with each record.
    for parent in PARENTS:
        origin = database_text(
            stack,
            f"SELECT origin || ' ' || origin_reason FROM persons WHERE display_name = '{parent}'",
        )
        assert origin == f"domo {PARENTS_COME}", origin


# --- an access is not an account -----------------------------------------------------------------


def test_the_house_given_to_the_parents_is_an_access_and_not_an_account(household: Household):
    owner, stack = household.owner, household.stack
    start = assistant_replies(owner).count()
    giving = [("household_change_role", {"person": parent, "role": "guest"}) for parent in PARENTS]
    reply = send_and_wait(owner, "Give the house to my parents.\n" + judged("do", calls(*giving)))
    expect(reply_text(reply)).to_have_text(said_of(["done"] * len(giving)), timeout=T)
    nothing_asked(owner, start)

    page = open_the_household_page(household)
    for parent in PARENTS:
        row = row_of(page, parent)
        expect(row).to_have_attribute("data-person-state", "has_access")
        expect(row).to_have_attribute("data-person-role", "guest")
        expect(says(row, NO_ACCOUNT)).to_have_count(1)
    back_to_the_chat(household)
    for parent in PARENTS:
        accounts = database_text(
            stack,
            "SELECT count(*) FROM accounts a JOIN persons p ON p.id = a.person_id "
            f"WHERE p.display_name = '{parent}'",
        )
        assert accounts == "0", (parent, accounts)


# --- a link gives Dana an account, on the record she has -----------------------------------------


def test_a_link_gives_dana_an_account_on_the_record_she_has(household: Household):
    owner, stack = household.owner, household.stack
    start = assistant_replies(owner).count()
    steps = [
        his_own(DANA_LIVES_HERE),
        (
            "record_open",
            {
                "name": DANA,
                "standing": "lives_here",
                "ground": "lives_here",
                "reason": DANA_LIVES_HERE,
            },
        ),
        (
            "household_invite",
            {"person": DANA, "role": "adult", "username": DANA_USERNAME},
        ),
    ]
    reply = send_and_wait(
        owner, "Dana lives here with me: give me a link for her.\n" + judged("do", calls(*steps))
    )
    expect(reply_text(reply)).to_have_text(said_of(["done"] * len(steps)), timeout=T)
    nothing_asked(owner, start)
    link = invitation_link(reply, stack)

    # Her record, before the link is used: no account yet.
    page = open_the_household_page(household)
    row = row_of(page, DANA)
    expect(row).to_have_count(1)
    expect(says(row, NO_ACCOUNT)).to_have_count(1)
    dana_id = row.get_attribute("data-person-id")
    assert dana_id, "Dana is not on the household page"
    back_to_the_chat(household)
    # An invitation of her record, not of someone new.
    invited = database_text(
        stack,
        f"SELECT kind || ' ' || person_id::text FROM invitations WHERE display_name = '{DANA}'",
    )
    assert invited == f"record {dana_id}", invited

    # The page of the link proposes a username; Dana keeps it and chooses her password.
    dana = household.new_page()
    dana.goto(link)
    expect(dana.locator("[name=username]")).to_have_value(DANA_USERNAME)
    expect(dana.locator("#username-hint")).to_have_text(t("invite.username_proposed", "en"))
    dana.fill("[name=password]", DANA_PASSWORD)
    dana.click("form[action^='/invite/'] button[type=submit]")
    dana.wait_for_url("**/chat", timeout=T)

    # The same record, with an account now, and access.
    page = open_the_household_page(household)
    same = page.locator(f'.household-person[data-person-id="{dana_id}"]')
    expect(same).to_have_count(1)
    expect(same.locator(".household-name")).to_have_text(DANA)
    expect(same).to_have_attribute("data-person-state", "has_access")
    expect(same).to_have_attribute("data-person-role", "adult")
    expect(says(same, NO_ACCOUNT)).to_have_count(0)
    expect(row_of(page, DANA)).to_have_count(1)
    back_to_the_chat(household)
    account = database_text(stack, f"SELECT username FROM accounts WHERE person_id = '{dana_id}'")
    assert account == DANA_USERNAME, account


# --- what the owner withdrew, the assistant does not give back ----------------------------------


def test_what_the_owner_withdrew_the_assistant_cannot_give_back(household: Household):
    owner, stack = household.owner, household.stack
    sam = PARENTS[0]

    # The owner's own order: a withdrawal, which only the owner lifts.
    removed = send_and_wait(owner, judged("do", tool_message("household_remove", {"person": sam})))
    expect(reply_text(removed)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    expect(removed.locator(".confirmation-card")).to_have_count(0)
    page = open_the_household_page(household)
    expect(row_of(page, sam)).to_have_attribute("data-person-state", "withdrawn")
    sam_id = row_of(page, sam).get_attribute("data-person-id")
    assert sam_id, "Sam is not on the household page"
    back_to_the_chat(household)
    directive = database_text(
        stack,
        f"SELECT kind FROM owner_directives WHERE person_id = '{sam_id}' AND lifted_at IS NULL",
    )
    assert directive == "withdraw", directive

    # Later the owner only says that Sam is back; the idea of bringing him back is the
    # assistant's, and the code refuses it before any judgement. He says so in his own words.
    back = {"person": sam, "role": "guest", "reason": SAM_IS_BACK}
    start = assistant_replies(owner).count()
    attempt = send_and_wait(
        owner,
        "Sam is back in town.\n" + calls(his_own(SAM_IS_BACK), ("household_bring_back", back)),
    )
    expect(reply_text(attempt)).to_have_text(said_of(["done", "refused"]), timeout=T)
    nothing_asked(owner, start)
    # Why, as the stored action keeps it: the owner's directive, on an act of the assistant's own,
    # with his reason.
    stored = database_text(
        stack,
        "SELECT status || ' ' || (summary->>'reason') || ' ' || by_domo::text || ' ' || "
        "coalesce(idea_reason, '') FROM message_actions WHERE tool_name = 'household_bring_back'",
    )
    assert stored == f"refused owner_directive true {SAM_IS_BACK}", stored

    # Nothing changed: Sam's access stays withdrawn.
    page = open_the_household_page(household)
    expect(row_of(page, sam)).to_have_attribute("data-person-state", "withdrawn")
    back_to_the_chat(household)
    assert database_text(stack, f"SELECT state FROM persons WHERE id = '{sam_id}'") == "withdrawn"
