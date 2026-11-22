"""From the first launch to the first reply: a stranger's path, in a real browser, against the
real Docker Compose stack, with a fake model service (``tests/fakes/fake_llm``) in place of the
provider.

Two stacks are involved (see ``conftest.py``):

* ``stack_with_fake_llm`` is shared by the tests of this module and starts as a new instance.
  ``test_first_launch_to_first_reply`` goes through the setup on it, in a browser; the tests after
  it start from the instance it leaves (``completed_setup`` does the same journey when they run
  alone). The tests therefore run in the order of this file.
* ``fresh_stack_with_fake_llm`` is a stack of its own for one test, which needs an instance that
  has no owner yet.

What the tests prove together: a stranger gets from ``docker compose up`` to a first reply in a
browser, the chat opening as soon as the key works, in the language of the browser, with no text
written for it (0.7.1 spec 5.1, scenario 1); the model's key goes from the browser to the vault
and nowhere else, so that the core never holds it; and a provider that echoes the key back does
not get it to the page.
"""

import re

import pytest
from domo_common.i18n import t
from domo_common.meanings import default_text
from playwright.sync_api import Browser, expect

from tests.stack.stack_support import StackInfo

from .support import (
    EXPECT_TIMEOUT_MS,
    FAKE_LLM_BASE_URL,
    assistant_replies,
    complete_owner_step,
    complete_setup,
    database_text,
    enter_vault_key,
    instance_is_new,
    send_message,
    setup_stage,
    submit_model_form,
)

pytestmark = pytest.mark.stack

# Nothing Domo wrote: no text kept, and no call of the writer or of its checker.
TEXTS_WRITTEN = "SELECT count(*) FROM written_texts"
WRITER_CALLS = "SELECT count(*) FROM model_calls WHERE task IN ('writing', 'checking')"
HOUSE_LANGUAGE = "SELECT value #>> '{}' FROM policy_settings WHERE key = 'house.language'"


def nothing_was_written(stack: StackInfo) -> bool:
    return database_text(stack, TEXTS_WRITTEN) == "0" == database_text(stack, WRITER_CALLS)


def test_first_launch_to_first_reply(stack_with_fake_llm, page):
    s = stack_with_fake_llm
    assert instance_is_new(s), "this test goes through the first launch: run it before the others"
    complete_owner_step(page, s)

    # The step sends an address it cannot use back with the reason, in place: the same page, the
    # same form with what was typed (static/model-step.js shows the 400 answer).
    page.evaluate("window.sameDocument = true")
    submit_model_form(page, base_url="file:///etc/hostname", has_key=False)
    expect(page.locator("#model-step .alert")).to_have_text(
        t("setup.model.error.base_url", "en"), timeout=EXPECT_TIMEOUT_MS
    )
    expect(page).to_have_url(re.compile(r"/setup/model$"))
    expect(page.locator("[name=base_url]")).to_have_value("file:///etc/hostname")
    assert page.evaluate("window.sameDocument === true"), "the page was reloaded"

    submit_model_form(page, base_url=FAKE_LLM_BASE_URL, has_key=True)
    enter_vault_key(page, s.test_key)
    # The key works: the chat opens at once, with no page that waits for texts.
    page.wait_for_url("**/chat", timeout=EXPECT_TIMEOUT_MS)
    assert setup_stage(s) == "done"
    # The house's language is the browser's, its whole tag (en-US): its default phrases are read,
    # and nothing is written for it.
    assert database_text(s, HOUSE_LANGUAGE) == "en-US"
    expect(page.locator("[data-action=send]")).to_have_text(default_text("chat.send", "en"))
    assert nothing_was_written(s)

    send_message(page, "hello")
    expect(assistant_replies(page).last).to_have_text(
        "Fake reply: hello", timeout=EXPECT_TIMEOUT_MS
    )


def test_secret_never_reaches_core(stack_with_fake_llm, completed_setup):
    s = stack_with_fake_llm
    # The key was used. The fake model answers 401 to any other key, so a 200 means that the call
    # reached it with the right one: from the vault, through the relay. These checks are not about
    # a key that was never typed. And they can find it where it is: the fake model, which has to
    # know it, holds it in its environment.
    assert "POST /v1/chat/completions -> 200" in s.logs("fake-llm")
    assert s.test_key in s.exec("fake-llm", ["env"]).stdout

    # Each log has what it is known to hold, so that "the key is not in it" is about a log that
    # was read. (The edge logs no request, by design: its check says little more than that.)
    known_lines = {
        "core": '"POST /setup/owner ',  # the access log is on, and the owner step happened
        "gatekeeper": "Uvicorn running on",
        "edge": '"msg":"server running"',
    }
    for service, line in known_lines.items():
        log = s.logs(service)
        assert line in log, f"the log of {service} has no {line!r}"
        assert s.test_key not in log, service
    core_log = s.logs("core")
    assert "The assistant setup code" in core_log
    # The vault's pages are served by the gatekeeper: the core never gets a request for one.
    assert "/vault/" not in core_log

    core_env = s.exec("core", ["env"])
    assert core_env.returncode == 0 and "DOMO_TRUSTED_PROXIES" in core_env.stdout
    assert s.test_key not in core_env.stdout
    dump = s.exec("db", ["pg_dump", "-U", "domo", "domo"])
    assert dump.returncode == 0 and "CREATE TABLE" in dump.stdout, dump.stderr
    assert s.test_key not in dump.stdout


def test_upstream_echo_is_scrubbed(stack_with_fake_llm, logged_in_chat_page):
    page = logged_in_chat_page
    send_message(page, "echo-key")
    reply = assistant_replies(page).last
    # Known limit, logged for a later plan: the scrubber reads the bytes of the response, so a key
    # a provider echoes whole is masked, but one it splits across streamed events is not. The fake
    # sends the key in one event.
    expect(reply).to_contain_text("[REDACTED]", timeout=EXPECT_TIMEOUT_MS)
    assert stack_with_fake_llm.test_key not in reply.inner_text()
    assert stack_with_fake_llm.test_key not in page.content()
    # Nor was it kept: the stored reply is the scrubbed one.
    dump = stack_with_fake_llm.exec("db", ["pg_dump", "-U", "domo", "domo"]).stdout
    assert "Fake reply: [REDACTED]" in dump
    assert stack_with_fake_llm.test_key not in dump


def test_wrong_key_then_rekey(fresh_stack_with_fake_llm, page):
    s = fresh_stack_with_fake_llm
    complete_owner_step(page, s)
    submit_model_form(page, base_url=FAKE_LLM_BASE_URL, has_key=True)
    enter_vault_key(page, "sk-test-wrong")
    expect(page.locator("[data-model-error]")).to_have_attribute(
        "data-model-error", "auth", timeout=EXPECT_TIMEOUT_MS
    )
    page.click("[data-action=rekey]")
    enter_vault_key(page, s.test_key)
    page.wait_for_url("**/chat", timeout=EXPECT_TIMEOUT_MS)

    # The new value is the one the vault uses from now on.
    send_message(page, "hello")
    expect(assistant_replies(page).last).to_have_text(
        "Fake reply: hello", timeout=EXPECT_TIMEOUT_MS
    )


def test_a_french_browser_opens_the_chat_in_french(
    fresh_stack_with_fake_llm, browser: Browser, browser_context_args
):  # scenario 1
    s = fresh_stack_with_fake_llm
    context = browser.new_context(**{**browser_context_args, "locale": "fr-FR"})
    try:
        page = context.new_page()
        # The whole first launch, in French: the chat opens as soon as the key works.
        complete_setup(page, s)
        assert setup_stage(s) == "done"
        assert page.locator("html").get_attribute("lang") == "fr-FR"
        expect(page.locator("[data-action=send]")).to_have_text(default_text("chat.send", "fr"))
        assert database_text(s, HOUSE_LANGUAGE) == "fr-FR"
        # Its pages again, in French still: Domo has no reason to write them.
        page.reload()
        expect(page.locator("[data-action=send]")).to_have_text(default_text("chat.send", "fr"))
        assert nothing_was_written(s)
    finally:
        context.close()


def test_a_setup_left_at_the_model_step_is_finished_from_another_browser(
    fresh_stack_with_fake_llm, browser, browser_context_args, page
):
    """The owner exists but the model does not work yet, and the browser that created the owner is
    gone: signing in again (the setup gate allows it) leads back to the model step, which can be
    finished. This is also how ``completed_setup`` finishes an instance that an earlier test left
    half way."""
    s = fresh_stack_with_fake_llm
    assert setup_stage(s) == "owner"
    complete_owner_step(page, s)
    assert setup_stage(s) == "model"

    context = browser.new_context(**browser_context_args)
    try:
        other = context.new_page()
        complete_setup(other, s)
        assert setup_stage(s) == "done"
        send_message(other, "hello")
        expect(assistant_replies(other).last).to_have_text(
            "Fake reply: hello", timeout=EXPECT_TIMEOUT_MS
        )
    finally:
        context.close()


def test_audit_chain_valid_after_setup(stack_with_fake_llm, logged_in_chat_page):
    page = logged_in_chat_page
    page.goto(stack_with_fake_llm.base_url + "/audit")
    expect(page.locator("[data-chain-status]")).to_have_attribute("data-chain-status", "ok")
