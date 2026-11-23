"""Fixtures of the end-to-end tests: the Docker Compose stack with a fake model service, and a
browser (pytest-playwright's ``page``) that trusts the stack's local certificate authority.

Install the browser once with ``uv run playwright install chromium``.

Two stacks, started from ``compose.yaml`` plus ``compose.test.yaml`` (see
``tests/stack/stack_support.py``), each with the fake model service and a new random test key:

* ``stack_with_fake_llm``, for the whole session: a new instance (no owner, no model), shared by
  the tests that go through the first launch and then use the instance it leaves. It is the
  project ``domo-e2e``, with the ports and the internal network of ``shared.env``.
* ``fresh_stack_with_fake_llm``, one per test that needs an instance nobody has touched: started
  and removed for that test alone (a full teardown, volumes included). It runs while the shared
  stack is up, so it has a project of its own (``domo-e2e-fresh``) and its own ports and
  internal network (``fresh.env``). Neither collides with ``domo-test`` of the stack tests, whose
  session fixture stays up until the end of the run.
* ``going_out_stack_with_fake_llm``, for one module (``test_going_out.py``): a new instance with the
  fake model and the fakes of going out of the stack tests (``tests/stack/compose.going-out.yaml``:
  a device, the same device on a port of this machine, a site with a login), with what they accept
  new for the run (``GoingOutStack.going_out``). A project of its own (``domo-e2e-out``), with the
  ports and networks of ``going_out.env``, and the device's port on this machine
  (``support.GOING_OUT_HOST_DEVICE_PORT``) apart from the stack tests' one.

Then ``completed_setup`` (the shared instance has its owner and its model: the first launch is
done in a browser if no test has done it yet) and ``logged_in_chat_page`` (the owner, signed in,
on the chat page).

For the household journey (``test_household.py``): ``household``, the people of the household in
browser contexts of their own, for the whole module (its scenarios build on each other), and
``browser_trusting_the_stack``, a browser that trusts the shared stack's certificate authority,
for the passkey scenario (see its docstring).

The stack tests' own fixtures (``tests/stack/conftest.py``) are not visible here: pytest only
shares a conftest with its own directory, so the two fixtures of the fake-model variant are
defined in this file, on top of ``running_stack_with_fake_llm``.
"""

import secrets
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, Page

from tests.stack.stack_support import (
    FAKE_LLM_COMPOSE_FILES,
    FAKE_LLM_KEY_VARIABLE,
    GOING_OUT_FAKES,
    GoingOutStack,
    StackInfo,
    running_stack_going_out,
    running_stack_with_fake_llm,
)

from .support import (
    GOING_OUT_ENV_FILE,
    GOING_OUT_HOST_DEVICE_PORT,
    GOING_OUT_PROJECT,
    GOING_OUT_SITE,
    Household,
    authority_key_hashes,
    complete_setup,
    log_in,
    setup_stage,
)

SHARED_PROJECT = "domo-e2e"
SHARED_ENV_FILE = "tests/e2e/shared.env"
FRESH_PROJECT = "domo-e2e-fresh"
FRESH_ENV_FILE = "tests/e2e/fresh.env"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """The stack's certificate comes from its own authority, which no browser knows: the tests
    accept it (the stack tests check separately that it verifies once the authority is trusted).
    The passkey scenario is the exception: its browser trusts the authority instead
    (``browser_trusting_the_stack``). English, so that the texts the tests read are the English
    ones whatever the machine's language."""
    return {**browser_context_args, "ignore_https_errors": True, "locale": "en-US"}


@pytest.fixture(scope="session")
def stack_with_fake_llm() -> Iterator[StackInfo]:
    with running_stack_with_fake_llm(project=SHARED_PROJECT, env_file=SHARED_ENV_FILE) as info:
        yield info


@pytest.fixture
def fresh_stack_with_fake_llm() -> Iterator[StackInfo]:
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def going_out_stack_with_fake_llm(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[GoingOutStack]:
    """A new instance with the fake model and the fakes of going out of the stack tests (a device
    on another machine, the same device on a port of this machine, a site with a login:
    ``tests/stack/compose.going-out.yaml``, with what they accept new for the run), for one module,
    taken down with its volumes at its end. A project of its own (``going_out.env``)."""
    key = "sk-test-" + secrets.token_hex(16)
    with running_stack_going_out(
        tmp_path_factory.mktemp("going-out"),
        GOING_OUT_SITE,
        files=(*FAKE_LLM_COMPOSE_FILES, GOING_OUT_FAKES),
        project=GOING_OUT_PROJECT,
        env_file=GOING_OUT_ENV_FILE,
        host_device_port=GOING_OUT_HOST_DEVICE_PORT,
        extra_env={FAKE_LLM_KEY_VARIABLE: key},
    ) as info:
        yield info


@pytest.fixture(scope="session")
def completed_setup(
    stack_with_fake_llm: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> StackInfo:
    """The shared instance, with its first launch really over (the instance says so: its pages no
    longer lead to a setup step). What is left to do is done in a throwaway browser context: all
    of it if nobody has started (this test runs alone), the model step if an earlier test stopped
    half way. A finished instance is left as it is."""
    if setup_stage(stack_with_fake_llm) != "done":
        context = browser.new_context(**browser_context_args)
        try:
            complete_setup(context.new_page(), stack_with_fake_llm)
        finally:
            context.close()
    stage = setup_stage(stack_with_fake_llm)
    assert stage == "done", f"the first launch is not over: the instance is at its {stage} step"
    return stack_with_fake_llm


@pytest.fixture
def logged_in_chat_page(completed_setup: StackInfo, page: Page) -> Page:
    log_in(page, completed_setup)
    return page


@pytest.fixture(scope="module")
def household(
    completed_setup: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the shared instance, signed in in a browser context of their own, and the
    members who join during the module, each in theirs. Every context is closed at the end of the
    module."""
    people = Household(completed_setup, browser, browser_context_args)
    try:
        yield people
    finally:
        people.close()


@pytest.fixture
def browser_trusting_the_stack(
    completed_setup: StackInfo, launch_browser: Callable[..., Browser]
) -> Iterator[Browser]:
    """A browser that trusts the shared stack's certificate authority, as a device of the
    household does once its owner has installed the authority (README), so that its pages have
    no certificate error: Chrome refuses passkeys (WebAuthn) on a page that has one, and a test
    of passkeys must not depend on a browser that lets it through anyway.

    Chromium is given the keys of that authority with ``--ignore-certificate-errors-spki-list``,
    the option it documents for this (honoured because Playwright starts it with a profile of its
    own). It is not a trust anchor: Chromium ignores the certificate errors of any chain that
    holds one of those keys, whatever they are (a name that does not match, say). Every other
    certificate is checked as usual: certificate checks stay on, and its contexts must not
    ignore HTTPS errors either."""
    keys = ",".join(authority_key_hashes(completed_setup))
    trusting = launch_browser(args=[f"--ignore-certificate-errors-spki-list={keys}"])
    try:
        yield trusting
    finally:
        trusting.close()
