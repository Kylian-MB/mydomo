"""Domo makes himself his tools (spec ``2026-11-17-domo-se-cree-un-outil``, and ``2026-11-18-les
-outils-sont-a-domo``: the tools are his), against the real Docker Compose stack, its sandbox
and its tools' base included, with the fake model service in place of the provider
(``tests/fakes/fake_llm``).

The fake makes Domo call a tool on demand (``/tool <name> <json>``), or do a request by hand then
build the tool for next time in the same turn (``/by_hand_then_build``: ``program_run``, then
``tool_build`` with ``from_run``); the model that writes a tool submits the object written after
``/submit`` in the build's purpose, and the reviewer finds nothing wrong; a plan due whose why is a
``/tool`` command makes that call; a launch of one of his tools is answered with what it gave
(``; result: ``), a list of his tools with their names (``; tools: ``). So the build is the core's
own, end to end: the upkeep takes it, the code is checked by the core, analysed and dry-run in the
real sandbox, reviewed, and the tool is made; nothing of it is faked but the model's words.

On an instance of this module's own (new when it starts, taken down at its end: the shared one
has lost its first owner by the end of ``test_household.py``), in the order of the file:

1. the owner asks for the month's expenses to be split; Domo opens the build (``tool_build``) and
   keeps the request in a plan that waits for it (``plan_set`` with ``after_build``, its time a
   day away). Within a few passes of the upkeep the build is ``ready``, the plan wakes Domo, who
   calls the new tool and says what it gave: the page shows that answer by itself, with no card
   and nothing that names the tool;
2. he asks again later: Domo calls the tool at once, and the reply holds its result;
3. he asks what tools Domo made: Domo finds them (``tool_find``) and looks at one
   (``tool_look``), with no card either;
4. Dana, an adult, asks Domo to note a loan and says she will again: Domo does it by hand at
   once (``program_run``: the program writes a row in her own corner of the tools' base, and he
   answers) and builds the tool from that program (``tool_build`` with ``from_run``); no card. Once
   the build passed, her next message uses the tool (``t_lend_note``), which writes in her corner
   again;
5. a tool whose operation costs (``t_order_pizzas``) stops before it spends: in the same turn
   Domo calls it again with ``yes_for`` (the call is the ask), the page shows the request
   waiting, no wake follows, and Dana's "oui" runs it once;
6. the bounds are his settings (spec 12, 2026-11-21): a tool that launches another one eleven
   times (``t_fan_out``) is stopped at its chain's ``chain_launches`` (10 at the start, the first
   launch included), and its program reads the refusal; Domo raises the setting for that tool
   alone (``tool_set``), and the same chain runs whole; ``tool_look`` with ``figures`` shows the
   launches of the tool it launched, all made without a turn;
7. a new version of a tool fails where the one before worked (``tool_change``); Domo lowers its
   ``failures_in_a_row`` to 1 (``tool_set``) and times it for the house (``tool_launch_when``):
   the launch fails, out of any turn, and sets it aside; he is woken on his own, in the owner's
   conversation, with the setting that did it, looks at the tool with its figures and goes back
   to the version before (``tool_back``), which is used again; the program's error stays out of
   the audit log.

Where the page cannot show it (the build, the plan, the audit log, the tools' base), the
databases are read.
"""

import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.fakes.fake_llm.app import (
    BY_HAND_COMMAND,
    BY_HAND_PREFIX,
    DECIDED_PREFIX,
    FIGURES_SEPARATOR,
    MADE_RESULT_SEPARATOR,
    SUBMIT_MARKER,
    TOOL_RESULT_PREFIX,
    TOOLS_SEPARATOR,
    TROUBLE_PREFIX,
)
from tests.stack.stack_support import StackInfo, running_stack_with_fake_llm

from .conftest import FRESH_ENV_FILE, FRESH_PROJECT
from .support import (
    EXPECT_TIMEOUT_MS,
    Household,
    Member,
    assistant_replies,
    confirmation_card,
    database_text,
    finish_the_first_launch,
    invite_and_join,
    no_new_reply,
    reply_text,
    send_and_wait,
    tool_message,
    wait_for_a_new_reply,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
NAME = "split_expenses"
OFFERED = f"t_{NAME}"
CODE = (
    "def run(entry):\n"
    '    given = entry["arguments"]\n'
    '    each = (given["rent"] + given["food"]) // 2\n'
    '    return {"each": each, "dana_owes_alex": given["rent"] - each}\n'
)
SUBMISSION = {
    "name": NAME,
    "description": "Split the rent and the food of a month in two; the rent was paid by one.",
    "parameters": {
        "type": "object",
        "properties": {
            "rent": {"type": "integer", "minimum": 0},
            "food": {"type": "integer", "minimum": 0},
        },
        "required": ["rent", "food"],
        "additionalProperties": False,
    },
    "operations": {"split": {"says": "Splits the month's expenses in two.", "risks": []}},
    "code": CODE,
}
PURPOSE = (
    "Split the month's expenses between Alex and Dana: the rent and the food in two, Alex paid "
    "the rent and Dana the food; give each one's share and what Dana owes Alex."
    f"\n{SUBMIT_MARKER}" + json.dumps(SUBMISSION, separators=(",", ":"))
)
EXAMPLES = [
    {"entry": {"rent": 900, "food": 420}, "expected": {"each": 660, "dana_owes_alex": 240}},
    {"entry": {"rent": 1000, "food": 200}, "expected": {"each": 600, "dana_owes_alex": 400}},
]
ASKED = {"rent": 900, "food": 420}
# What the plan keeps for Domo: the call he makes when it wakes him (the fake reads it).
PLAN_WHY = f"/tool {OFFERED} " + json.dumps(ASKED, separators=(",", ":"))
# How long a build and the wake of a plan may take: a few passes of the upkeep (every 5 seconds,
# shared.env), a round of the model, the checks in the sandbox, then the page's asking.
BUILT_MS = 180_000
BUILT_S = BUILT_MS / 1000
SETTLED_WAIT_S = 30

# Dana, an adult of the household, who lends money and orders pizzas.
DANA = Member("Dana", "adult", "dana", "dana-test-passphrase")

# The tool Dana's loans are noted with: by hand first, then kept as a tool. It writes in the corner
# of whom it runs for; in a dry run (as for the house, with no corner) it writes nothing.
NOTE = "lend_note"
LOANS_TABLE = "CREATE TABLE IF NOT EXISTS loans (who text, cents int)"
LOAN_ROW = "INSERT INTO loans VALUES ($1, $2)"
NOTE_CODE = (
    "import domo\n"
    "\n"
    "\n"
    "def run(entry):\n"
    '    given = entry["arguments"]\n'
    '    domo.begin("note")\n'
    "    if not domo.dry_run:\n"
    f'        domo.write("{LOANS_TABLE}", [], "mine")\n'
    f'        domo.write("{LOAN_ROW}", [given["who"], given["cents"]], "mine")\n'
    '    return {"noted": given["who"], "cents": given["cents"]}\n'
)
NOTE_OPERATIONS = {"note": {"says": "Notes a loan in the lender's own notes.", "risks": []}}
NOTE_PARAMETERS = {
    "type": "object",
    "properties": {
        "who": {"type": "string", "minLength": 1, "maxLength": 40},
        "cents": {"type": "integer", "minimum": 1},
    },
    "required": ["who", "cents"],
    "additionalProperties": False,
}
NOTE_SUBMISSION = {
    "name": NOTE,
    "description": "Notes who was lent how much, in cents, in the lender's own notes.",
    "parameters": NOTE_PARAMETERS,
    "operations": NOTE_OPERATIONS,
    "code": NOTE_CODE,
}
NOTE_BY_HAND = {
    "run": {
        "code": NOTE_CODE,
        "arguments": {"who": "Sam", "cents": 2000},
        "operations": NOTE_OPERATIONS,
    },
    "build": {
        "name": NOTE,
        "purpose": "Note a loan: who was lent money and how much, in cents, in the lender's own "
        f"notes.\n{SUBMIT_MARKER}" + json.dumps(NOTE_SUBMISSION, separators=(",", ":")),
        "examples": [
            {"entry": {"who": "Sam", "cents": 2000}, "expected": {"noted": "Sam", "cents": 2000}}
        ],
    },
}

# A tool whose operation costs: it stops for Dana's yes before it orders anything.
ORDER = "order_pizzas"
ORDERS_TABLE = "CREATE TABLE IF NOT EXISTS orders (pizzas int)"
ORDER_ROW = "INSERT INTO orders VALUES ($1)"
ORDER_CODE = (
    "import domo\n"
    "\n"
    "\n"
    "def run(entry):\n"
    '    count = entry["arguments"]["count"]\n'
    '    domo.begin("order", 600 * count)\n'
    "    if not domo.dry_run:\n"
    f'        domo.write("{ORDERS_TABLE}", [], "mine")\n'
    f'        domo.write("{ORDER_ROW}", [count], "mine")\n'
    '    return {"ordered": count}\n'
)
ORDER_SUBMISSION = {
    "name": ORDER,
    "description": "Orders pizzas, 6 euros each.",
    "parameters": {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 10}},
        "required": ["count"],
        "additionalProperties": False,
    },
    "operations": {"order": {"says": "Orders the pizzas.", "risks": ["costs"]}},
    "code": ORDER_CODE,
}
ORDER_BUILD = {
    "name": ORDER,
    "purpose": "Order pizzas for the evening, 6 euros each.\n"
    + SUBMIT_MARKER
    + json.dumps(ORDER_SUBMISSION, separators=(",", ":")),
    "examples": [{"entry": {"count": 2}, "expected": {"ordered": 2}}],
}


def submitted(name: str, purpose: str, submission: dict[str, Any]) -> str:
    """A build's purpose that makes the fake toolmaker submit ``submission`` for ``name``."""
    assert submission["name"] == name
    return f"{purpose}\n{SUBMIT_MARKER}" + json.dumps(submission, separators=(",", ":"))


NO_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": False}

# A tool that only says it ran, and one that launches it as many times as it is asked: a chain of
# launches, which reads its refusal when the chain is stopped.
LEAF = "leaf"
LEAF_BUILD = {
    "name": LEAF,
    "purpose": submitted(
        LEAF,
        "Say that it ran.",
        {
            "name": LEAF,
            "description": "Says that it ran.",
            "parameters": NO_PARAMETERS,
            "operations": {"answer": {"says": "Says that it ran.", "risks": []}},
            "code": 'def run(entry):\n    return {"leaf": True}\n',
        },
    ),
    "examples": [{"entry": {}, "expected": {"leaf": True}}],
}
FAN = "fan_out"
FAN_CODE = (
    "import domo\n"
    "\n"
    "\n"
    "def run(entry):\n"
    '    count = entry["arguments"]["count"]\n'
    '    domo.begin("fan")\n'
    "    launched = 0\n"
    "    for _ in range(count):\n"
    "        try:\n"
    f'            domo.tool("{LEAF}", {{}})\n'
    "        except domo.Refused as refused:\n"
    '            return {"launched": launched, "refused": refused.reason}\n'
    "        launched += 1\n"
    '    return {"launched": launched}\n'
)
FAN_BUILD = {
    "name": FAN,
    "purpose": submitted(
        FAN,
        f"Launch the tool {LEAF} as many times as asked, one after the other; say how many ran, "
        "and why it stopped if it was refused.",
        {
            "name": FAN,
            "description": f"Launches {LEAF} a number of times, one after the other.",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 50}},
                "required": ["count"],
                "additionalProperties": False,
            },
            "operations": {"fan": {"says": f"Launches {LEAF} again and again.", "risks": []}},
            "code": FAN_CODE,
        },
    ),
    "examples": [{"entry": {"count": 2}, "expected": {"launched": 2}}],
}
# Eleven launches of the leaf, and the first one: twelve in one chain.
FANNED = 11
# Where ``chain_launches`` starts (the first launch of the chain included): the leaves that run
# before the chain is stopped.
CHAIN_START = 10
RAISED = 20

# A tool whose second version fails on every real launch (its dry runs pass: the checks let it
# through), so that it is set aside and Domo goes back to the first.
GREET = "greet"
GREET_PARAMETERS = {
    "type": "object",
    "properties": {"who": {"type": "string", "minLength": 1, "maxLength": 40}},
    "required": ["who"],
    "additionalProperties": False,
}
GREET_OPERATIONS = {"greet": {"says": "Says hello to someone.", "risks": []}}
GREET_EXAMPLES = [{"entry": {"who": "Sam"}, "expected": {"hello": "Sam"}}]
GREET_V1 = 'def run(entry):\n    return {"hello": entry["arguments"]["who"]}\n'
GREET_V2 = (
    "import domo\n"
    "\n"
    "\n"
    "def run(entry):\n"
    "    if not domo.dry_run:\n"
    '        raise ValueError("the new version breaks")\n'
    '    return {"hello": entry["arguments"]["who"]}\n'
)
BROKEN = "the new version breaks"


def greet_build(code: str, purpose: str) -> dict[str, Any]:
    """The arguments of a ``tool_build`` or a ``tool_change`` of ``greet`` with ``code``."""
    submission = {
        "name": GREET,
        "description": "Says hello to someone.",
        "parameters": GREET_PARAMETERS,
        "operations": GREET_OPERATIONS,
        "code": code,
    }
    return {
        "name": GREET,
        "purpose": submitted(GREET, purpose, submission),
        "examples": GREET_EXAMPLES,
    }


# How long a tool in trouble takes to wake Domo: the trigger's time, a pass of the upkeep (every
# 5 seconds, fresh.env), the launch, then the turn of the wake and the page's asking.
TROUBLE_MS = 120_000


@pytest.fixture(scope="module")
def tools_stack() -> Iterator[StackInfo]:
    """An instance for this module alone, new when it starts, taken down with its volumes at its
    end (the project, ports and network of the stacks of one test: see ``conftest.py``)."""
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def household(
    tools_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, and the members who join during the
    module, each in a browser context of their own."""
    finish_the_first_launch(tools_stack, browser, browser_context_args)
    people = Household(tools_stack, browser, browser_context_args)
    try:
        yield people
    finally:
        people.close()


def said_with(result: dict[str, Any]) -> str:
    """What the fake says of a launch of a tool that gave ``result``."""
    given = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return f"{TOOL_RESULT_PREFIX}done{MADE_RESULT_SEPARATOR}{given}"


def no_card_and_no_tool(page: Page, *names: str) -> None:
    """Nothing on the page shows a tool of Domo: no card of an action or of a confirmation under
    any of his replies nor among the requests waiting for a yes (``#approvals``), and none of his
    words names a tool of ``names`` (as the household knows it, or as it is offered)."""
    replies = assistant_replies(page)
    expect(replies.locator(".action-card")).to_have_count(0)
    expect(replies.locator(".confirmation-card")).to_have_count(0)
    expect(page.locator("#approvals .confirmation-card")).to_have_count(0)
    for text in replies.locator(".message-text").all_inner_texts():
        for name in names:
            assert name not in text and f"t_{name}" not in text, text


def wait_for(stack: StackInfo, query: str, expected: str, *, seconds: float = SETTLED_WAIT_S):
    deadline = time.monotonic() + seconds
    while (found := database_text(stack, query)) != expected and time.monotonic() < deadline:
        time.sleep(1)
    assert found == expected, (query, found)


def build_of(name: str) -> str:
    """The query of the state of the latest build of ``name``."""
    return f"SELECT state FROM tool_builds WHERE name = '{name}' ORDER BY created_at DESC LIMIT 1"


def tools_base_text(stack: StackInfo, query: str) -> str:
    """What a query of the tools' own database selects (one value): read only, as the core's
    database is (``support.database_text``)."""
    result = stack.exec(
        "db",
        [
            "psql",
            "--username=domo",
            "--dbname=domo_tools",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--command",
            query,
        ],
    )
    if result.returncode != 0:
        raise AssertionError(f"the query failed: {result.stderr}")
    return result.stdout.strip()


def corner_of(stack: StackInfo, member: Member) -> str:
    """The schema of ``member``'s own corner of the tools' base."""
    corner = database_text(
        stack,
        "SELECT s.schema_name FROM tools_spaces s JOIN persons p ON p.id = s.person_id "
        f"WHERE p.display_name = '{member.display_name}' AND p.disabled_at IS NULL",
    )
    assert re.fullmatch(r"c_[0-9a-f]{12}", corner), corner
    return corner


def launches_of(stack: StackInfo, name: str) -> list[str]:
    """How each launch of the tool ``name`` ended, in order (the audit log)."""
    launched = (
        "SELECT details->>'status' FROM audit_log "
        f"WHERE action = 'madetool.launched' AND details->>'tool' = '{name}' ORDER BY id"
    )
    text = database_text(stack, launched)
    return text.splitlines() if text else []


# --- the owner's tool, waited for in a plan -----------------------------------------------------


def test_a_request_waits_for_the_tool_domo_builds_and_is_done_with_it(household: Household):
    owner, stack = household.owner, household.stack
    before = assistant_replies(owner).count()
    building = {"name": NAME, "purpose": PURPOSE, "examples": EXAMPLES}
    opened = send_and_wait(owner, tool_message("tool_build", building))
    expect(reply_text(opened)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    build_id = database_text(stack, f"SELECT id FROM tool_builds WHERE name = '{NAME}'")
    assert re.fullmatch(r"[0-9a-f-]{36}", build_id), build_id

    # The request is kept in a plan that waits for the build; its own time is a day away.
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    plan = {"why": PLAN_WHY, "when": when, "after_build": build_id}
    kept = send_and_wait(owner, tool_message("plan_set", plan))
    expect(reply_text(kept)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)

    # The build ends ready, the plan wakes Domo, who uses the tool and says what it gave: in the
    # open page, with nothing sent from it.
    heard = wait_for_a_new_reply(owner, before + 2, timeout_ms=BUILT_MS)
    expect(reply_text(heard)).to_have_text(
        said_with({"each": 660, "dana_owes_alex": 240}), timeout=T
    )
    no_card_and_no_tool(owner, NAME)

    # What the page cannot show: the tool is ready (it is Domo's, nobody's in particular), its
    # build ready, the plan done, and the launch was the woken turn's.
    assert database_text(stack, f"SELECT state FROM tool_builds WHERE id = '{build_id}'") == (
        "ready"
    )
    assert database_text(stack, f"SELECT state FROM made_tools WHERE name = '{NAME}'") == "ready"
    # Done whatever woke it: a request without a rule is served once (its time will not wake it).
    wait_for(stack, f"SELECT state FROM plans WHERE why = '{PLAN_WHY}'", "done")
    launched = (
        "SELECT (details->>'status') || ' ' || (details->>'cause') FROM audit_log "
        f"WHERE action = 'madetool.launched' AND details->>'tool' = '{NAME}' ORDER BY id"
    )
    assert database_text(stack, launched).splitlines() == ["done turn"]
    # The audit log keeps that it was built and checked, never its code, purpose or examples.
    built = (
        "SELECT count(*) FROM audit_log "
        f"WHERE action = 'madetool.built' AND details->>'tool' = '{NAME}'"
    )
    assert database_text(stack, built) == "1"
    leaked = (
        "SELECT count(*) FROM audit_log WHERE details::text LIKE '%dana_owes_alex%' "
        "OR details::text LIKE '%def run%'"
    )
    assert database_text(stack, leaked) == "0"


def test_asked_again_domo_uses_his_tool_at_once(household: Household):
    owner = household.owner
    again = {"rent": 1000, "food": 200}
    reply = send_and_wait(owner, "And for next month?\n" + tool_message(OFFERED, again))
    expect(reply_text(reply)).to_have_text(said_with({"each": 600, "dana_owes_alex": 400}))
    no_card_and_no_tool(owner, NAME)


def test_asked_what_tools_he_made_domo_finds_them_and_looks(household: Household):
    owner = household.owner
    found = send_and_wait(
        owner,
        "What tools have you made?\n" + tool_message("tool_find", {"about": "split expenses"}),
    )
    expect(reply_text(found)).to_have_text(f"{TOOL_RESULT_PREFIX}done{TOOLS_SEPARATOR}{NAME}")
    looked = send_and_wait(owner, tool_message("tool_look", {"name": NAME}))
    expect(reply_text(looked)).to_have_text(TOOL_RESULT_PREFIX + "done")
    # His words name the tool here, because the owner asked; still no card of anything.
    replies = assistant_replies(owner)
    expect(replies.locator(".action-card")).to_have_count(0)
    expect(replies.locator(".confirmation-card")).to_have_count(0)


# --- Dana's loans: by hand now, a tool for next time --------------------------------------------


def test_done_by_hand_at_once_then_the_tool_is_built_from_it_and_used_next_time(
    household: Household,
):
    stack = household.stack
    dana = invite_and_join(household, DANA)
    words = "I lent Sam 20 euros, note it for me. I'll tell you about my loans again."
    command = BY_HAND_COMMAND + json.dumps(NOTE_BY_HAND, separators=(",", ":"))
    answered = send_and_wait(dana, f"{words}\n{command}")
    # The program ran in her turn and wrote in her corner; Domo answers with what it gave, and
    # says nothing of a program or a tool.
    expect(reply_text(answered)).to_have_text(
        BY_HAND_PREFIX + '{"cents":2000,"noted":"Sam"}', timeout=T
    )
    no_card_and_no_tool(dana, NOTE)
    corner = corner_of(stack, DANA)
    assert tools_base_text(stack, f"SELECT who || ' ' || cents FROM {corner}.loans") == "Sam 2000"
    # The run is kept, for Dana, and the build starts from it; the audit keeps that it ran, never
    # its code nor its arguments.
    run = database_text(
        stack, f"SELECT from_run_id FROM tool_builds WHERE name = '{NOTE}' ORDER BY created_at"
    )
    assert re.fullmatch(r"[0-9a-f-]{36}", run), run
    ran = database_text(stack, "SELECT count(*) FROM audit_log WHERE action = 'program.ran'")
    assert int(ran) >= 1
    leaked = "SELECT count(*) FROM audit_log WHERE details::text LIKE '%loans%'"
    assert database_text(stack, leaked) == "0"

    # Once the build passed (the upkeep, out of her turn), her next message uses the tool.
    wait_for(stack, build_of(NOTE), "ready", seconds=BUILT_S)
    used = send_and_wait(
        dana, "And Paul 5 euros.\n" + tool_message(f"t_{NOTE}", {"who": "Paul", "cents": 500})
    )
    expect(reply_text(used)).to_have_text(said_with({"noted": "Paul", "cents": 500}))
    no_card_and_no_tool(dana, NOTE)
    rows = tools_base_text(stack, f"SELECT string_agg(who, ',' ORDER BY who) FROM {corner}.loans")
    assert rows == "Paul,Sam"
    assert launches_of(stack, NOTE) == ["done"]


# --- a tool that costs waits for Dana's yes ------------------------------------------------------


def test_a_tool_that_costs_stops_for_a_yes_shown_as_waiting_and_her_oui_runs_it_once(
    household: Household,
):
    stack = household.stack
    dana = household.page_of(DANA)
    opened = send_and_wait(dana, tool_message("tool_build", ORDER_BUILD))
    expect(reply_text(opened)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    wait_for(stack, build_of(ORDER), "ready", seconds=BUILT_S)

    # The launch stops at its begin, before it spends anything; in the same turn Domo calls the
    # tool again with yes_for (the fake does, as the core tells him: that call is the ask, R20),
    # and the request waits for her yes, shown with its card.
    before = assistant_replies(dana).count()
    asked = send_and_wait(
        dana, "Order two pizzas for tonight.\n" + tool_message(f"t_{ORDER}", {"count": 2})
    )
    expect(reply_text(asked)).to_have_text(TOOL_RESULT_PREFIX + "waiting_for_confirmation")
    card = confirmation_card(asked)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    stop = database_text(
        stack,
        "SELECT s.amount_cents FROM launch_stops s JOIN made_tools t ON t.id = s.tool_id "
        f"WHERE t.name = '{ORDER}'",
    )
    assert stop == "1200", stop
    corner = corner_of(stack, DANA)
    assert tools_base_text(stack, f"SELECT to_regclass('{corner}.orders')::text") == ""
    # The stop was told in the turn that waited for it: no wake follows it (R14), no reply more,
    # and nothing kept to wake her with.
    no_new_reply(dana, before + 1)
    kept = (
        "SELECT count(*) FROM made_tool_runs r JOIN made_tools t ON t.id = r.tool_id "
        f"WHERE t.name = '{ORDER}'"
    )
    assert database_text(stack, kept) == "0"

    # Her "oui" runs it, once.
    yes = send_and_wait(dana, "oui")
    expect(reply_text(yes)).to_have_text(DECIDED_PREFIX + "executed")
    expect(confirmation_card(yes)).to_have_attribute(
        "data-confirmation-status", "executed", timeout=T
    )
    assert tools_base_text(stack, f"SELECT string_agg(pizzas::text, ',') FROM {corner}.orders") == (
        "2"
    )
    assert launches_of(stack, ORDER).count("done") == 1


# --- the bounds are his settings -----------------------------------------------------------------


def built(
    page: Page, stack: StackInfo, building: dict[str, Any], *, tool: str = "tool_build"
) -> None:
    """Have Domo open the build ``building`` (``tool_build``, or ``tool_change``) and wait until
    it is ready."""
    opened = send_and_wait(page, tool_message(tool, building))
    expect(reply_text(opened)).to_have_text(TOOL_RESULT_PREFIX + "done", timeout=T)
    wait_for(stack, build_of(str(building["name"])), "ready", seconds=BUILT_S)


def silent(page: Page, since: int) -> None:
    """No card under any of Domo's replies from the ``since``-th on (those of the scenario), nor
    among the requests waiting for a yes: his acts on his own tools are never shown. The replies
    before are earlier scenarios' of the module, whose page it shares: the owner's invitation of
    Dana shows its link on its card, as it must."""
    replies = assistant_replies(page)
    for index in range(since, replies.count()):
        expect(replies.nth(index).locator(".action-card")).to_have_count(0)
        expect(replies.nth(index).locator(".confirmation-card")).to_have_count(0)
    expect(page.locator("#approvals .confirmation-card")).to_have_count(0)


def test_a_chain_stopped_by_its_setting_runs_once_domo_raises_it_for_the_tool(
    household: Household,
):
    owner, stack = household.owner, household.stack
    start = assistant_replies(owner).count()
    built(owner, stack, LEAF_BUILD)
    built(owner, stack, FAN_BUILD)

    # Twelve launches in one chain: the leaf runs until the chain has made chain_launches, and the
    # program reads why the next one is refused.
    stopped = send_and_wait(owner, tool_message(f"t_{FAN}", {"count": FANNED}))
    expect(reply_text(stopped)).to_have_text(
        said_with({"launched": CHAIN_START - 1, "refused": "too_many_launches"})
    )
    refused = (
        "SELECT details->>'reason' FROM audit_log "
        f"WHERE action = 'madetool.tool_refused' AND details->>'tool' = '{FAN}'"
    )
    assert database_text(stack, refused) == "too_many_launches"

    # Domo raises the setting for this tool alone: written in the audit log with its figures.
    raised = send_and_wait(
        owner, tool_message("tool_set", {"name": FAN, "set": {"chain_launches": RAISED}})
    )
    expect(reply_text(raised)).to_have_text(TOOL_RESULT_PREFIX + "done")
    changed = (
        "SELECT (details->'before'->>'chain_launches') || ' ' || "
        "(details->'after'->>'chain_launches') FROM audit_log "
        f"WHERE action = 'madetool.settings_changed' AND details->>'tool' = '{FAN}'"
    )
    assert database_text(stack, changed) == f"{CHAIN_START} {RAISED}"
    own = f"SELECT settings->>'chain_launches' FROM made_tools WHERE name = '{FAN}'"
    assert database_text(stack, own) == str(RAISED)
    # The house's value did not move: only the tool carries it.
    house = "SELECT count(*) FROM tool_settings WHERE key = 'chain_launches'"
    assert database_text(stack, house) == "0"

    # The same chain now runs whole.
    ran = send_and_wait(owner, tool_message(f"t_{FAN}", {"count": FANNED}))
    expect(reply_text(ran)).to_have_text(said_with({"launched": FANNED}))

    # What the leaf did, as Domo reads it: every launch made by a tool, none by a turn of his.
    leaves = CHAIN_START - 1 + FANNED
    looked = send_and_wait(owner, tool_message("tool_look", {"name": LEAF, "figures": True}))
    expect(reply_text(looked)).to_have_text(
        f"{TOOL_RESULT_PREFIX}done{FIGURES_SEPARATOR}"
        f"v1: {leaves} launched, 0 failed, {leaves} without a turn"
    )
    traced = (
        "SELECT count(*) FILTER (WHERE l.cause = 'tool' AND NOT l.by_turn) "
        "FROM made_tool_launches l JOIN made_tools t ON t.id = l.tool_id "
        f"WHERE t.name = '{LEAF}'"
    )
    assert database_text(stack, traced) == str(leaves)
    assert launches_of(stack, FAN) == ["done", "done"]
    silent(owner, start)


def test_a_tool_set_aside_out_of_a_turn_wakes_domo_who_goes_back_a_version(
    household: Household,
):
    owner, stack = household.owner, household.stack
    start = assistant_replies(owner).count()
    built(owner, stack, greet_build(GREET_V1, "Say hello to someone, by name."))
    built(
        owner,
        stack,
        greet_build(GREET_V2, "Say hello to someone, by name, the new way."),
        tool="tool_change",
    )
    versions = (
        "SELECT count(*) FROM made_tool_versions v JOIN made_tools t ON t.id = v.tool_id "
        f"WHERE t.name = '{GREET}'"
    )
    assert database_text(stack, versions) == "2"

    # One failure in a row now sets it aside: a setting of this tool alone.
    lowered = send_and_wait(
        owner, tool_message("tool_set", {"name": GREET, "set": {"failures_in_a_row": 1}})
    )
    expect(reply_text(lowered)).to_have_text(TOOL_RESULT_PREFIX + "done")

    # Launched for the house in a few seconds, by itself: no turn reads how it ends.
    when = (datetime.now(UTC) + timedelta(seconds=15)).isoformat(timespec="seconds")
    timed = {"name": GREET, "when": when, "for": "house", "arguments": {"who": "Sam"}}
    set_at = send_and_wait(owner, tool_message("tool_launch_when", timed))
    expect(reply_text(set_at)).to_have_text(TOOL_RESULT_PREFIX + "done")
    before = assistant_replies(owner).count()

    # It fails and is set aside; Domo is woken on his own, in the owner's conversation, with the
    # setting that did it: he looks at it and goes back to the version before.
    woken = wait_for_a_new_reply(owner, before, timeout_ms=TROUBLE_MS)
    expect(reply_text(woken)).to_have_text(
        f"{TROUBLE_PREFIX}{GREET} set_aside (failures_in_a_row 1); back to version 1: done",
        timeout=T,
    )
    silent(owner, start)
    assert launches_of(stack, GREET)[-1] == "set_aside"
    set_aside = (
        "SELECT count(*) FROM audit_log "
        f"WHERE action = 'madetool.set_aside' AND details->>'tool' = '{GREET}'"
    )
    assert database_text(stack, set_aside) == "1"
    went_back = (
        "SELECT details->>'version' FROM audit_log "
        f"WHERE action = 'madetool.went_back' AND details->>'tool' = '{GREET}'"
    )
    assert database_text(stack, went_back) == "1"
    woken_once = "SELECT coalesce(sum(wakes), 0) FROM tool_trouble_days"
    assert database_text(stack, woken_once) == "1"
    first = (
        "SELECT t.state || ' ' || (t.current_version_id = ("
        "SELECT v.id FROM made_tool_versions v WHERE v.tool_id = t.id "
        "ORDER BY v.created_at, v.id LIMIT 1))::text "
        f"FROM made_tools t WHERE t.name = '{GREET}'"
    )
    assert database_text(stack, first) == "ready true"
    # The program's words are kept for Domo in the trace, never in the audit log.
    traced = (
        "SELECT count(*) FROM made_tool_launches l JOIN made_tools t ON t.id = l.tool_id "
        f"WHERE t.name = '{GREET}' AND l.error LIKE '%{BROKEN}%'"
    )
    assert database_text(stack, traced) == "1"
    leaked = f"SELECT count(*) FROM audit_log WHERE details::text LIKE '%{BROKEN}%'"
    assert database_text(stack, leaked) == "0"

    # The version in force works again.
    again = send_and_wait(owner, tool_message(f"t_{GREET}", {"who": "Dana"}))
    expect(reply_text(again)).to_have_text(said_with({"hello": "Dana"}))
