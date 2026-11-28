"""Domo picks his model (0.6.0, spec ``2026-11-08-domo-choisit-son-modele``, section 8), in
real browsers, against the real Docker Compose stack, with the fake model service in place of the
provider (``tests/fakes/fake_llm``).

The fake lists two models, ``fake-haiku`` and ``fake-sonnet``: small and medium for the project's
table of levels. When the person's message is ``/model``, it says which one the call asked for
(``Fake model: <model>``): the chat shows which model Domo picked. It answers an upkeep turn
(someone who has no key Domo may use for them is asked, on the general key, to give one) with
``Fake upkeep``. Its key is local (a server of the network, ``openai_compatible``), so every call
is free: the firm limit and the real spending are left to the core's tests.

These scenarios have an instance of their own (``models_stack``), new when the module starts: the
shared one (``stack_with_fake_llm``) is the household journey's, whose first owner has left by its
end. They build on each other and run in the order of this file, each person in a browser context
of their own (``people``):

1. the first launch asks no model's name: the owner picks a kind of provider and types the key in
   the vault's form, the key's models are listed, and the conversation works;
2. a conversation takes the medium level: ``/model`` says ``fake-sonnet``;
3. Domo puts the conversation on the small level (``models_choose``, which asks no yes):
   ``/model`` says ``fake-haiku``;
4. Alex, an adult, adds a key of his own in words (``key_add``) and types it in the vault's form,
   under the card: the key works, and his next request is made on it, not on the general key;
5. the owner takes Paul off the general key (``keys_house``): Paul, who has no key of his own, is
   answered by an upkeep turn on the general key (``Fake upkeep``), not by a conversation;
6. ``spending_show`` tells the owner what Domo cost this month, and asks no yes: the month's
   calls are counted, and each cost nothing on the local key.

The judge lets the actions that change something through at once (``judged("do", ...)``): the
judge is the household journey's subject, not this one's.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from domo_common.i18n import t
from domo_core.llm.speech.home_key import HOME_SPEECH_ID
from playwright.sync_api import Browser, Page, expect

from tests.stack.stack_support import StackInfo, running_stack_with_fake_llm

from .conftest import FRESH_ENV_FILE, FRESH_PROJECT
from .support import (
    ALEX,
    EXPECT_TIMEOUT_MS,
    FAKE_LLM_BASE_URL,
    PAUL,
    WRITING_TIMEOUT_MS,
    Household,
    Member,
    complete_owner_step,
    confirmation_card,
    database_text,
    enter_vault_key,
    finish_the_first_launch,
    instance_is_new,
    invite_and_join,
    judged,
    reply_text,
    send_and_wait,
    submit_model_form,
    tool_message,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
# The message that makes the fake model say which model the call asked for.
MODEL_COMMAND = "/model"
# The household's general key (the first launch's), and the models the keys listed.
# The house's speech service is a key of its own (``HOME_SPEECH_ID``), recorded once there is an
# owner: the keys and the models a person gave are the others.
GENERAL_KEY = "SELECT id FROM providers WHERE is_general"
LISTED_MODELS = (
    "SELECT string_agg(model_id, ',' ORDER BY model_id) FROM provider_models "
    f"WHERE provider_id <> '{HOME_SPEECH_ID}'"
)
GIVEN_KEYS = f"SELECT count(*) FROM providers WHERE id <> '{HOME_SPEECH_ID}'"
# The calls of this calendar month (the household has no time zone: UTC, as the stack's clock).
CALLS_THIS_MONTH = (
    "SELECT count(*) FROM model_calls WHERE started_at >= date_trunc('month', now() AT TIME ZONE "
    "'UTC') AT TIME ZONE 'UTC'"
)


@pytest.fixture(scope="module")
def models_stack() -> Iterator[StackInfo]:
    """An instance for this module alone, new when it starts, taken down with its volumes at its
    end. It has the project, the ports and the network of the stacks that ``conftest.py`` starts
    for one test that needs a new instance: those are over before this module starts, and the
    shared stack may be up next to it."""
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def people(
    models_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, and the members who join during the module,
    each in a browser context of their own, closed at its end. If the first scenario did not go
    through the first launch (a scenario run alone), it is done here first."""
    finish_the_first_launch(models_stack, browser, browser_context_args)
    household = Household(models_stack, browser, browser_context_args)
    try:
        yield household
    finally:
        household.close()


def person_named(member: Member) -> str:
    """The query (a sub-query) of the id of ``member``."""
    return f"(SELECT id FROM persons WHERE display_name = '{member.display_name}')"


def key_of(member: Member) -> str:
    """The query of the id of the key ``member`` gave."""
    return f"SELECT id FROM providers WHERE given_by = {person_named(member)}"


def last_call_of(member: Member) -> str:
    """The query of the kind of task and the key of the last model call made for ``member``
    (``<task> <key id>``), but those that wrote their texts in the background (``writing``,
    ``checking``): a page they saw may have them written at any time."""
    return (
        "SELECT task || ' ' || provider_id FROM model_calls "
        f"WHERE person_id = {person_named(member)} AND task NOT IN ('writing', 'checking') "
        "ORDER BY started_at DESC LIMIT 1"
    )


def test_the_first_launch_asks_no_model_name(models_stack: StackInfo, page: Page):
    stack = models_stack
    assert instance_is_new(stack), "the first launch is the module's first scenario: run it first"
    complete_owner_step(page, stack)

    # The kind of provider and, for a server of one's own, its address and whether it takes a
    # key: no field for the name of a model.
    expect(page.locator("form.model-form [name=kind]")).to_be_visible()
    expect(page.locator("[name=model]")).to_have_count(0)
    submit_model_form(page, base_url=FAKE_LLM_BASE_URL, has_key=True)
    enter_vault_key(page, stack.test_key)
    # The fake lists its models: no name is asked after the key either; Domo writes the texts
    # of his pages, then the chat opens.
    page.wait_for_url("**/chat", timeout=WRITING_TIMEOUT_MS)

    # The key of the first launch is the general key, and Domo knows the models it opens.
    assert database_text(stack, "SELECT count(*) FROM providers WHERE is_general") == "1"
    assert database_text(stack, LISTED_MODELS) == "fake-haiku,fake-sonnet"
    reply = send_and_wait(page, "hello")
    expect(reply_text(reply)).to_have_text("Fake reply: hello")


def test_a_conversation_takes_the_medium_level(people: Household):
    reply = send_and_wait(people.owner, MODEL_COMMAND)
    expect(reply_text(reply)).to_have_text("Fake model: fake-sonnet")


def test_domo_puts_the_conversation_on_the_small_level(people: Household):
    owner, stack = people.owner, people.stack
    choice = {"why": "test", "task": "conversation", "level": "small"}
    chosen = send_and_wait(owner, tool_message("models_choose", choice))
    # It changes nothing in the household: no yes is asked, and the change is written down with
    # why.
    expect(reply_text(chosen)).to_have_text("Fake tool result: done")
    expect(confirmation_card(chosen)).to_have_count(0)
    changed = "SELECT details->>'why' FROM audit_log WHERE action = 'models.choices_changed'"
    assert database_text(stack, changed) == "test"

    reply = send_and_wait(owner, MODEL_COMMAND)
    expect(reply_text(reply)).to_have_text("Fake model: fake-haiku")


def test_alex_adds_a_key_of_his_own_in_words(people: Household):
    stack = people.stack
    alex = invite_and_join(people, ALEX)

    new_key = {"kind": "openai_compatible", "base_url": FAKE_LLM_BASE_URL, "has_key": True}
    added = send_and_wait(alex, judged("do", tool_message("key_add", new_key)))
    expect(reply_text(added)).to_have_text("Fake tool result: done")
    card = added.locator(".action-card")
    expect(card).to_have_attribute("data-action-status", "done", timeout=T)
    # The vault's form, in its frame under the card: the key goes from his browser to the vault,
    # never through the core. Once it is in, the key is checked.
    expect(card.locator("iframe.vault-frame")).to_be_visible()
    enter_vault_key(alex, stack.test_key)
    result = card.locator(".key-result")
    expect(result).to_have_attribute("data-key-status", "checked", timeout=T)
    expect(result).to_have_text(t("chat.key.checked", "en"))
    assert stack.test_key not in stack.logs("core")

    # Two keys given now: the general one, and his. His next request is made on his own key.
    assert database_text(stack, GIVEN_KEYS) == "2"
    his = database_text(stack, key_of(ALEX))
    assert his and his != database_text(stack, GENERAL_KEY)
    hello = send_and_wait(alex, "hello")
    expect(reply_text(hello)).to_have_text("Fake reply: hello")
    assert database_text(stack, last_call_of(ALEX)) == f"conversation {his}"


def test_paul_taken_off_the_general_key_is_asked_for_a_key_of_his_own(people: Household):
    owner, stack = people.owner, people.stack
    paul = invite_and_join(people, PAUL)
    # By default, the whole household is on the general key: a conversation.
    expect(reply_text(send_and_wait(paul, "hello"))).to_have_text("Fake reply: hello")

    rule = {"person": PAUL.display_name, "on_general_key": "deny"}
    ruled = send_and_wait(owner, judged("do", tool_message("keys_house", rule)))
    expect(reply_text(ruled)).to_have_text("Fake tool result: done")

    # Paul has no key Domo may use for him now. The general key still serves to ask him for one
    # (the upkeep, which the house allows by default): an upkeep turn answers him, never a
    # conversation.
    asked = send_and_wait(paul, "hello again")
    expect(reply_text(asked)).to_have_text("Fake upkeep")
    general = database_text(stack, GENERAL_KEY)
    assert database_text(stack, last_call_of(PAUL)) == f"upkeep {general}"


def test_domo_tells_what_he_cost_this_month(people: Household):
    owner, stack = people.owner, people.stack
    shown = send_and_wait(owner, tool_message("spending_show", {"period": "month"}))
    # It changes nothing: no yes is asked.
    expect(reply_text(shown)).to_have_text("Fake tool result: done")
    card = shown.locator(".action-card")
    expect(card).to_have_attribute("data-action-status", "done", timeout=T)
    expect(card.locator(".action-text")).to_have_text(t("action.card.done.spending.show", "en"))
    expect(confirmation_card(shown)).to_have_count(0)
    # What it reads: this month's calls, every one of them free on the local key.
    assert int(database_text(stack, CALLS_THIS_MONTH)) > 0
    assert database_text(stack, "SELECT count(*) FROM model_calls WHERE cost_usd <> 0") == "0"
