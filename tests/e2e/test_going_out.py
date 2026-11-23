"""Told an address and given a token, Domo builds and uses his own tools for a device (step 2 of
spec ``2026-11-18-les-outils-sont-a-domo``, section 13.1), in the browser, against the real
Docker Compose stack with the fake model service (``tests/fakes/fake_llm``) and the fakes of going
out of the stack tests (``tests/stack/compose.going-out.yaml``): a device of the house on another
machine (``device``), the same device on a port of the machine that hosts Domo
(``host.docker.internal``), and a site with a login over HTTPS (``site``).

The fake makes Domo call a tool on demand (``/tool <name> <json>``); the model that writes a tool
submits the object after ``/submit`` in the build's purpose, and the reviewer finds nothing wrong.
The yes-judge reads the person's words for an address or an account they ask for, and the fake is
steered each way: ``/judge do`` lets it through at once, with no card; ``/judge yes`` asks a plain
yes, with a card. Everything else is the core's own:
the addresses he opens, the accounts asked and typed into the gatekeeper's one-time form in the
browser, the programs run in the real sandbox, every request out judged by the barrier and sent
by the gatekeeper with the account placed in it.

On an instance of this module's own, in the order of the file:

1. the owner says where his device is: Domo opens the address for his programs run by hand and
   asks for a token (a house account); the owner types it in the one-time form, and Domo runs a
   program by hand that lists what the device has;
2. Domo builds himself a tool that sets a thing's state (it declares its account and its
   address), opens the address for it, and the owner has the lamp switched on with it; timed for
   the house, it runs by itself, with no turn, on the house account, and the lamp is off again;
3. the same device on a port of the machine that hosts Domo, which Domo has not the right to
   open: in the owner's turn the judge weighs his words. Asking a yes, it stays closed until the
   owner gives it with the card's button, and the program then reaches it; letting it through, it
   opens for the tool at once, with no card;
4. Dana gives an account of her own for the site: it serves her launches, never one for Paul (a
   child) nor one for the house; once she removed it, her next launch is refused;
5. the owner asks to close the device's address for the tool: it is closed for the programs run by
   hand too (ruling R20); the next launch is refused ``closed_address`` (the tool is not set aside
   for one failure). Opening it again is judged (ruling R24): Domo's own reopening, the judge
   asking the owner first, waits on a card and stays closed while he gives no yes; the owner's own
   request, let through, opens it at once, and the tool works again. (A deny rule of the owner on
   ``net.open``, which makes a closing absolute, is left to the unit tests.)

No reply, card or page ever shows the token, the site's user or its password: each test reads
every word of its own replies, and the pages of the forms. Where the page cannot show it (the open
addresses, the accounts, the launches, the audit log), the database is read.
"""

import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.fakes.fake_llm.app import (
    DECIDED_PREFIX,
    FIGURES_SEPARATOR,
    MADE_RESULT_SEPARATOR,
    SUBMIT_MARKER,
    TOOL_RESULT_PREFIX,
)
from tests.stack.stack_support import GoingOutSecrets, GoingOutStack

from .support import (
    EXPECT_TIMEOUT_MS,
    GOING_OUT_HOST_DEVICE_PORT,
    OWNER,
    Household,
    Member,
    account_form_link,
    assistant_replies,
    confirm_and_hear_back,
    confirmation_card,
    database_text,
    finish_the_first_launch,
    give_the_account,
    invite_and_join,
    judged,
    no_new_reply,
    reply_text,
    send_and_wait,
    tool_message,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
# How long a build may take (a few passes of the upkeep, the checks in the sandbox), and a launch
# timed a few seconds ahead (its time, a pass of the upkeep, the launch).
BUILT_S = 180
LAUNCHED_S = 90
TIMED_IN_S = 15

# Dana, an adult, who has an account at the site; Paul, a child.
DANA = Member("Dana", "adult", "dana", "dana-test-passphrase")
PAUL = Member("Paul", "child", "paul", "paul-test-passphrase")

# The device, as the owner says where it is, and the same device on a port of this machine.
DEVICE = {"host": "device", "port": 8000}
DEVICE_URL = "http://device:8000"
ON_HOST = {"host": "host.docker.internal", "port": GOING_OUT_HOST_DEVICE_PORT}
ON_HOST_URL = f"http://host.docker.internal:{GOING_OUT_HOST_DEVICE_PORT}"
# The house's account of the device: a token, in a header, for both of its addresses.
DEVICE_ACCOUNT = {
    "name": "device",
    "for": "house",
    "hosts": [DEVICE, ON_HOST],
    "place": {"kind": "header", "name": "authorization", "template": "Bearer {value}"},
}
# The site, and Dana's account there: a user and a password, in the body of its login request.
SITE = {"host": "site", "port": 8443}
SITE_URL = "https://site:8443"
SITE_ACCOUNT = {
    "name": "site",
    "for": "me",
    "hosts": [SITE],
    "place": {
        "kind": "login",
        "path": "/login",
        "encoding": "form",
        "user_field": "user",
        "password_field": "password",
    },
}

# The program Domo runs by hand to read what the device says at an address: the status and the
# body, or why the request was refused.
LOOK_CODE = (
    "import json\n"
    "import domo\n"
    "\n"
    "\n"
    "def run(entry):\n"
    '    domo.begin("look")\n'
    "    try:\n"
    '        answer = domo.out("GET", entry["arguments"]["url"], None, None, "device")\n'
    "    except domo.Refused as refused:\n"
    '        return {"refused": refused.reason}\n'
    '    return {"status": answer["status"], "body": json.loads(answer["body"])}\n'
)
LOOK_OPERATIONS = {"look": {"says": "Reads what the device says.", "risks": []}}

# The tool that sets a thing of the device on or off. A request refused makes its launch fail (it
# does not catch it); in a dry run (the checks of its build) nothing is sent and it says what it
# was asked.
SET = "device_set"
SET_CODE = (
    "import json\n"
    "import domo\n"
    "\n"
    "\n"
    "def run(entry):\n"
    '    given = entry["arguments"]\n'
    '    domo.begin("set")\n'
    "    answer = domo.out(\n"
    '        "POST",\n'
    f'        "{DEVICE_URL}/state/" + given["thing"],\n'
    '        {"content-type": "application/json"},\n'
    '        json.dumps({"state": given["state"]}),\n'
    '        "device",\n'
    "    )\n"
    "    if answer is None:\n"
    '        return {"thing": given["thing"], "state": given["state"]}\n'
    '    return json.loads(answer["body"])\n'
)
SET_SUBMISSION = {
    "name": SET,
    "description": "Sets a thing of the device on or off.",
    "parameters": {
        "type": "object",
        "properties": {
            "thing": {"type": "string", "minLength": 1, "maxLength": 40},
            "state": {"type": "string", "enum": ["on", "off"]},
        },
        "required": ["thing", "state"],
        "additionalProperties": False,
    },
    "operations": {
        "set": {
            "says": "Sets a thing of the device on or off.",
            "risks": ["physical"],
            "accounts": ["device"],
            "addresses": [DEVICE],
        }
    },
    "code": SET_CODE,
}
SET_BUILD = {
    "name": SET,
    "purpose": "Switch a thing of the device on or off, with the house's account of it."
    f"\n{SUBMIT_MARKER}" + json.dumps(SET_SUBMISSION, separators=(",", ":")),
    "examples": [
        {"entry": {"thing": "lamp", "state": "on"}, "expected": {"thing": "lamp", "state": "on"}}
    ],
}

# Dana's tool: it logs in at the site with the account and reads the account's page; it says the
# status of each, or why it was refused. In a dry run nothing is sent: no status.
LOOK_SITE = "site_account"
LOOK_SITE_CODE = (
    "import domo\n"
    "\n"
    "\n"
    "def status(answer):\n"
    '    return None if answer is None else answer["status"]\n'
    "\n"
    "\n"
    "def run(entry):\n"
    '    domo.begin("look")\n'
    "    try:\n"
    f'        login = domo.out("POST", "{SITE_URL}/login", None, "remember=yes", "site")\n'
    f'        page = domo.out("GET", "{SITE_URL}/account", None, None, "site")\n'
    "    except domo.Refused as refused:\n"
    '        return {"refused": refused.reason}\n'
    '    return {"logged_in": status(login), "account": status(page)}\n'
)
LOOK_SITE_SUBMISSION = {
    "name": LOOK_SITE,
    "description": "Logs in at the site with the account and reads the account's page.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    "operations": {
        "look": {
            "says": "Logs in at the site and reads the account's page.",
            "risks": [],
            "accounts": ["site"],
            "addresses": [SITE],
        }
    },
    "code": LOOK_SITE_CODE,
}
LOOK_SITE_BUILD = {
    "name": LOOK_SITE,
    "purpose": "Log in at the site with the account and read the account's page."
    f"\n{SUBMIT_MARKER}" + json.dumps(LOOK_SITE_SUBMISSION, separators=(",", ":")),
    "examples": [{"entry": {}, "expected": {"logged_in": None, "account": None}}],
}


@pytest.fixture(scope="module")
def household(
    going_out_stack_with_fake_llm: GoingOutStack,
    browser: Browser,
    browser_context_args: dict[str, Any],
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, and the members who join during the
    module, each in a browser context of their own."""
    finish_the_first_launch(going_out_stack_with_fake_llm, browser, browser_context_args)
    people = Household(going_out_stack_with_fake_llm, browser, browser_context_args)
    try:
        yield people
    finally:
        people.close()


def secrets_of(household: Household) -> GoingOutSecrets:
    stack = household.stack
    assert isinstance(stack, GoingOutStack) and stack.going_out is not None
    return stack.going_out


def never_shown(household: Household, page: Page, since: int, *shown: str) -> None:
    """None of the secrets of the fakes in any word of the replies of ``page`` from the
    ``since``-th on (this test's: the page is shared by the module), their cards included, nor in
    the texts ``shown`` (the pages of the forms)."""
    given = secrets_of(household)
    replies = assistant_replies(page)
    texts = [replies.nth(index).inner_text() for index in range(since, replies.count())]
    for text in [*texts, *shown]:
        for secret in (given.device_token, given.site_user, given.site_password):
            assert secret not in text, text


def made(result: Any, status: str = "done") -> str:
    """What the fake says of a launch (a tool's, or a program's run by hand) that ended ``status``
    and gave ``result``."""
    given = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return f"{TOOL_RESULT_PREFIX}{status}{MADE_RESULT_SEPARATOR}{given}"


def run_by_hand(url: str) -> str:
    """The message that makes Domo run, by hand, the program that reads ``url``."""
    program = {"code": LOOK_CODE, "arguments": {"url": url}, "operations": LOOK_OPERATIONS}
    return tool_message("program_run", program)


def wait_for(stack: GoingOutStack, query: str, expected: str, *, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while (found := database_text(stack, query)) != expected and time.monotonic() < deadline:
        time.sleep(1)
    assert found == expected, (query, found)


def built(page: Page, stack: GoingOutStack, building: dict[str, Any]) -> None:
    """Have Domo open the build ``building`` and wait until it is ready."""
    opened = send_and_wait(page, tool_message("tool_build", building))
    expect(reply_text(opened)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    name = building["name"]
    state = f"SELECT state FROM tool_builds WHERE name = '{name}' ORDER BY created_at DESC LIMIT 1"
    wait_for(stack, state, "ready", seconds=BUILT_S)


def launches_of(stack: GoingOutStack, name: str) -> list[str]:
    """Each launch of the tool ``name``, in order: its cause, for whom, whether a turn made it,
    how it ended."""
    query = (
        "SELECT l.cause || ' ' || l.for_kind || ' ' || l.by_turn::text || ' ' || l.status "
        "FROM made_tool_launches l JOIN made_tools t ON t.id = l.tool_id "
        f"WHERE t.name = '{name}' ORDER BY l.at"
    )
    text = database_text(stack, query)
    return text.splitlines() if text else []


def wait_for_launches(stack: GoingOutStack, name: str, count: int) -> list[str]:
    deadline = time.monotonic() + LAUNCHED_S
    while len(found := launches_of(stack, name)) < count and time.monotonic() < deadline:
        time.sleep(1)
    assert len(found) == count, found
    return found


def outs_of(stack: GoingOutStack, tool: str | None) -> list[str]:
    """What came of each request out of the tool ``tool`` (``None``: of the programs run by hand),
    in order (the audit log): for whom (``house``, or a person's id), the method, then the status
    or the refusal."""
    which = "IS NULL" if tool is None else f"= '{tool}'"
    query = (
        "SELECT coalesce(details->>'for_person', 'house') || ' ' || (details->>'method') || ' ' "
        "|| coalesce(details->>'status', details->>'refused') FROM audit_log "
        f"WHERE action = 'madetool.out_done' AND details->>'tool' {which} ORDER BY id"
    )
    text = database_text(stack, query)
    return text.splitlines() if text else []


def live_addresses(stack: GoingOutStack, tool: str | None, address: dict[str, Any]) -> str:
    """How many open addresses ``tool`` (``None``: the programs run by hand) has at
    ``address``."""
    which = "tool_name IS NULL" if tool is None else f"tool_name = '{tool}'"
    return database_text(
        stack,
        f"SELECT count(*) FROM open_addresses WHERE {which} AND host = '{address['host']}' "
        f"AND port = {address['port']} AND closed_at IS NULL",
    )


def person_id(stack: GoingOutStack, member: Member) -> str:
    return database_text(
        stack,
        f"SELECT id FROM persons WHERE display_name = '{member.display_name}' "
        "AND disabled_at IS NULL",
    )


def opened(page: Page, words: str, address: dict[str, Any]) -> None:
    """Domo opens ``address`` in the conversation of ``page``, as the person asked in ``words``:
    his own act, whose judge reads the person's words and lets it through (``judged("do")``), so
    it is done at once, with no card waiting for a yes, and said in one reply (counted from
    before the message is sent: nothing comes after it)."""
    before = assistant_replies(page).count()
    done = send_and_wait(page, f"{words}\n" + judged("do", tool_message("address_open", address)))
    expect(reply_text(done)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    expect(confirmation_card(done)).to_have_count(0)
    no_new_reply(page, before + 1)


def asked_the_owner(owner: Page, words: str, address: dict[str, Any]) -> Locator:
    """Domo opens ``address`` in the owner's conversation with the judge asking a plain yes on
    his words (``judged("yes")``): the opening waits for it, shown with its card, which the owner
    may answer (its Confirm button is there). Returns the reply that shows it."""
    asked = send_and_wait(
        owner, f"{words}\n" + judged("yes", tool_message("address_open", address))
    )
    expect(reply_text(asked)).to_have_text(
        TOOL_RESULT_PREFIX + "waiting_for_confirmation", timeout=T
    )
    card = confirmation_card(asked)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(card.locator("[data-action=confirm]")).to_be_visible(timeout=T)
    return asked


def openings(stack: GoingOutStack, address: dict[str, Any]) -> list[str]:
    """How each opening of ``address`` went, in order (the audit log): what it was opened under,
    and whether it opened again what the owner had closed."""
    query = (
        "SELECT coalesce(details->>'under', '-') || ' ' || (details->>'reopened') FROM audit_log "
        f"WHERE action = 'outward.address_opened' AND details->>'host' = '{address['host']}' "
        f"AND (details->>'port')::int = {address['port']} ORDER BY id"
    )
    text = database_text(stack, query)
    return text.splitlines() if text else []


def in_a_while() -> str:
    return (datetime.now(UTC) + timedelta(seconds=TIMED_IN_S)).isoformat(timespec="seconds")


# --- 1. told an address and given a token ---------------------------------------------------------


def test_told_where_the_device_is_domo_opens_it_asks_a_token_and_lists_it_by_hand(
    household: Household,
):
    owner, stack = household.owner, household.stack
    assert isinstance(stack, GoingOutStack)
    start = assistant_replies(owner).count()

    # The owner says where his device is: Domo opens the address for his programs run by hand.
    opened(owner, "My device is at http://device:8000, can you work with it?", DEVICE)
    assert live_addresses(stack, None, DEVICE) == "1"

    # And asks for its token: a house account, whose form is shown once, to the owner.
    asked = send_and_wait(
        owner,
        "It wants a token.\n" + judged("do", tool_message("account_ask", DEVICE_ACCOUNT)),
    )
    expect(reply_text(asked)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    saved = give_the_account(
        owner, account_form_link(asked), {"value": secrets_of(household).device_token}
    )
    account = database_text(
        stack,
        "SELECT (person_id IS NULL)::text || ' ' || for_domo::text FROM service_accounts "
        "WHERE name = 'device' AND removed_at IS NULL",
    )
    assert account == "true true"

    # Domo tries by hand: a program run once lists what the device has, with the account.
    listed = send_and_wait(owner, "What does it have?\n" + run_by_hand(f"{DEVICE_URL}/things"))
    expect(reply_text(listed)).to_have_text(
        made({"status": 200, "body": {"things": ["lamp"]}}), timeout=T
    )
    assert outs_of(stack, None) == [f"{person_id(stack, OWNER)} GET 200"]
    never_shown(household, owner, start, saved)


# --- 2. his own tool, for the owner and for the house ---------------------------------------------


def test_domo_builds_his_tool_and_it_serves_the_owner_and_the_house_without_a_turn(
    household: Household,
):
    owner, stack = household.owner, household.stack
    assert isinstance(stack, GoingOutStack)
    start = assistant_replies(owner).count()
    built(owner, stack, SET_BUILD)
    declared = database_text(
        stack, f"SELECT operations->'set'->>'accounts' FROM made_tools WHERE name = '{SET}'"
    )
    assert json.loads(declared) == ["device"]

    # The address open to the programs run by hand is not the tool's: Domo opens it for it.
    assert live_addresses(stack, SET, DEVICE) == "0"
    opened(owner, "Use it for that.", {**DEVICE, "tool": SET})
    assert live_addresses(stack, SET, DEVICE) == "1"

    # For the owner, in his turn.
    on = send_and_wait(
        owner,
        "Switch the lamp on.\n" + tool_message(f"t_{SET}", {"thing": "lamp", "state": "on"}),
    )
    expect(reply_text(on)).to_have_text(made({"thing": "lamp", "state": "on"}), timeout=T)

    # Timed for the house: it runs by itself, with no turn, on the house account.
    timed = {
        "name": SET,
        "when": in_a_while(),
        "for": "house",
        "arguments": {"thing": "lamp", "state": "off"},
    }
    set_at = send_and_wait(owner, tool_message("tool_launch_when", timed))
    expect(reply_text(set_at)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    assert wait_for_launches(stack, SET, 2) == [
        "turn person true done",
        "trigger house false done",
    ]
    owner_id = person_id(stack, OWNER)
    assert outs_of(stack, SET) == [f"{owner_id} POST 200", "house POST 200"]

    # The lamp is off again, as the device says to a program run by hand.
    state = send_and_wait(owner, "Is the lamp off?\n" + run_by_hand(f"{DEVICE_URL}/state/lamp"))
    expect(reply_text(state)).to_have_text(
        made({"status": 200, "body": {"thing": "lamp", "state": "off"}}), timeout=T
    )
    looked = send_and_wait(owner, tool_message("tool_look", {"name": SET, "figures": True}))
    expect(reply_text(looked)).to_have_text(
        f"{TOOL_RESULT_PREFIX}done{FIGURES_SEPARATOR}v1: 2 launched, 0 failed, 1 without a turn",
        timeout=T,
    )
    never_shown(household, owner, start)


# --- 3. a port of the machine that hosts Domo ---------------------------------------------------


def test_a_port_of_the_host_opens_once_the_owner_said_yes_and_then_the_program_reaches_it(
    household: Household,
):
    owner, stack = household.owner, household.stack
    assert isinstance(stack, GoingOutStack)
    start = assistant_replies(owner).count()
    things = f"{ON_HOST_URL}/things"

    # Nothing is open there yet.
    before = send_and_wait(owner, run_by_hand(things))
    expect(reply_text(before)).to_have_text(made({"refused": "closed_address"}), timeout=T)

    # The owner says the device is also on this machine. Domo has not the right to open a port
    # of it: in the owner's own turn, the judge weighs his words. Here it asks a plain yes: the
    # opening waits for it, and the port stays closed until he gives it.
    asked = asked_the_owner(
        owner, "The same device also runs on this machine.", {**ON_HOST, "on_host": True}
    )
    assert live_addresses(stack, None, ON_HOST) == "0"

    # His yes, with the button, opens it as a port of the host.
    confirm_and_hear_back(owner, confirmation_card(asked), said=DECIDED_PREFIX + "executed")
    row = database_text(
        stack,
        "SELECT on_host::text FROM open_addresses WHERE tool_name IS NULL "
        f"AND host = '{ON_HOST['host']}' AND port = {ON_HOST['port']} AND closed_at IS NULL",
    )
    assert row == "true"
    assert openings(stack, ON_HOST) == ["owner_yes false"]

    # The program now reaches the device on the host's port, with the house account.
    after = send_and_wait(owner, run_by_hand(things))
    expect(reply_text(after)).to_have_text(
        made({"status": 200, "body": {"things": ["lamp"]}}), timeout=T
    )

    # The same port for his tool too, and this time the judge lets it through on the owner's
    # words: opened at once, with no card, as decided on his words.
    opened(owner, "Let your tool reach it there too.", {**ON_HOST, "on_host": True, "tool": SET})
    assert live_addresses(stack, SET, ON_HOST) == "1"
    assert openings(stack, ON_HOST) == ["owner_yes false", "owner_asked false"]
    never_shown(household, owner, start)


# --- 4. Dana's own account ------------------------------------------------------------------------


def test_dana_s_own_account_serves_her_never_paul_nor_the_house_and_once_removed_none(
    household: Household,
):
    owner, stack = household.owner, household.stack
    assert isinstance(stack, GoingOutStack)
    given = secrets_of(household)
    owner_start = assistant_replies(owner).count()
    dana = invite_and_join(household, DANA)
    paul = invite_and_join(household, PAUL)

    # Dana gives an account of her own for the site, in its form: a user and a password.
    asked = send_and_wait(
        dana,
        "I have an account at the site, use it for me.\n"
        + judged("do", tool_message("account_ask", SITE_ACCOUNT)),
    )
    expect(reply_text(asked)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    saved = give_the_account(
        dana,
        account_form_link(asked),
        {"user": given.site_user, "password": given.site_password},
    )
    dana_id = person_id(stack, DANA)
    whose = database_text(
        stack, "SELECT person_id FROM service_accounts WHERE name = 'site' AND removed_at IS NULL"
    )
    assert whose == dana_id

    # Domo builds the tool that uses it and opens the site for it, in her conversation.
    built(dana, stack, LOOK_SITE_BUILD)
    opened(dana, "Go ahead.", {**SITE, "tool": LOOK_SITE})

    # For Dana, it logs in and reads her account's page.
    hers = send_and_wait(dana, "How is my account?\n" + tool_message(f"t_{LOOK_SITE}", {}))
    expect(reply_text(hers)).to_have_text(made({"logged_in": 200, "account": 200}), timeout=T)

    # For Paul, her account is not his.
    his = send_and_wait(paul, tool_message(f"t_{LOOK_SITE}", {}))
    expect(reply_text(his)).to_have_text(made({"refused": "account_not_yours"}), timeout=T)

    # For the house, neither: on his own Domo never uses a person's account.
    timed = {"name": LOOK_SITE, "when": in_a_while(), "for": "house", "arguments": {}}
    set_at = send_and_wait(owner, tool_message("tool_launch_when", timed))
    expect(reply_text(set_at)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    assert wait_for_launches(stack, LOOK_SITE, 3)[-1] == "trigger house false done"
    paul_id = person_id(stack, PAUL)
    assert outs_of(stack, LOOK_SITE) == [
        f"{dana_id} POST 200",
        f"{dana_id} GET 200",
        f"{paul_id} POST account_not_yours",
        "house POST account_not_yours",
    ]

    # Dana removes it: her next launch is refused.
    removed = send_and_wait(
        dana,
        "Stop using my account at the site.\n"
        + judged("do", tool_message("account_remove", {"name": "site", "for": "me"})),
    )
    expect(reply_text(removed)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    again = send_and_wait(dana, tool_message(f"t_{LOOK_SITE}", {}))
    expect(reply_text(again)).to_have_text(made({"refused": "account_removed"}), timeout=T)

    for page, since in ((dana, 0), (paul, 0), (owner, owner_start)):
        never_shown(household, page, since, saved)


# --- 5. the owner closes the address --------------------------------------------------------------


def test_the_address_the_owner_closed_stays_closed_until_reopening_is_judged_and_done(
    household: Household,
):
    owner, stack = household.owner, household.stack
    assert isinstance(stack, GoingOutStack)
    start = assistant_replies(owner).count()

    closed = send_and_wait(
        owner,
        "Close the device for that tool, I don't want it reached.\n"
        + tool_message("address_close", {**DEVICE, "tool": SET}),
    )
    expect(reply_text(closed)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    by_owner = database_text(
        stack,
        "SELECT closed_by_owner::text FROM open_addresses "
        f"WHERE tool_name = '{SET}' AND closed_at IS NOT NULL",
    )
    assert by_owner == "true"
    # His closing closes the same host and port for every tool, the programs run by hand too
    # (opened for them in the first test; ruling R20).
    assert live_addresses(stack, None, DEVICE) == "0"

    # The next launch is refused at its request: a failure of the launch, and one is not enough
    # to set the tool aside (its failures_in_a_row is 3).
    refused = send_and_wait(owner, tool_message(f"t_{SET}", {"thing": "lamp", "state": "on"}))
    expect(reply_text(refused)).to_have_text(
        re.compile("^" + re.escape(f"{TOOL_RESULT_PREFIX}failed{MADE_RESULT_SEPARATOR}") + ".*"),
        timeout=T,
    )
    expect(reply_text(refused)).to_contain_text("closed_address", timeout=T)
    tool = f"SELECT state || ' ' || failures_in_a_row FROM made_tools WHERE name = '{SET}'"
    assert database_text(stack, tool) == "ready 1"

    # Domo opens it again of himself in the owner's conversation; the judge, told the owner
    # closed it and when, asks the owner first: a card, and it stays closed while he gives no yes.
    asked_the_owner(owner, "What about the lamp?", {**DEVICE, "tool": SET})
    assert live_addresses(stack, SET, DEVICE) == "0"

    # He does not say yes: the next launch is refused the same way.
    still = send_and_wait(owner, tool_message(f"t_{SET}", {"thing": "lamp", "state": "on"}))
    expect(reply_text(still)).to_contain_text("closed_address", timeout=T)
    assert database_text(stack, tool) == "ready 2"
    owner_id = person_id(stack, OWNER)
    assert outs_of(stack, SET)[-2:] == [
        f"{owner_id} POST closed_address",
        f"{owner_id} POST closed_address",
    ]
    assert live_addresses(stack, SET, DEVICE) == "0"

    # The owner himself asks to open it again: the judge lets it through on his words, and it
    # opens at once, with no card, in one reply; the audit says it opened again what he closed,
    # as he asked.
    opened(owner, "Open the device again for that tool, it's fine now.", {**DEVICE, "tool": SET})
    assert live_addresses(stack, SET, DEVICE) == "1"
    assert openings(stack, DEVICE)[-1] == "owner_asked true"
    again = send_and_wait(owner, tool_message(f"t_{SET}", {"thing": "lamp", "state": "on"}))
    expect(reply_text(again)).to_have_text(made({"thing": "lamp", "state": "on"}), timeout=T)
    assert outs_of(stack, SET)[-1] == f"{owner_id} POST 200"
    assert database_text(stack, tool) == "ready 0"
    never_shown(household, owner, start)
