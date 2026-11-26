"""The steps of the first launch, as a person takes them in a browser, and what the end-to-end tests
share: the values they type, and the locators of the page's hooks. Then what the household journey
(``test_household.py``) adds: the people the owner invites, a browser context for each of them
(``Household``), the messages that make the fake model call a tool (and, for the rights, the
draft the fake rights writer makes), what the fake yes-judge answers of the action a message asks
for (``judged``), how a person proves on a card that it is them (``prove_with_the_password``,
``prove_with_the_passkey``), what a person who comes back chooses, how a page hears from Domo
after a button, a proof or an arrival (he is woken, and the page asks for what he says), the link
each step of a chain leaves behind its own button (``show_link``), and what a passkey needs (a
virtual authenticator, and the keys of the stack's certificate authority). And for the scenarios
of the models (``test_models.py``), on an instance of their own: its first launch, finished from a
fixture (``finish_the_first_launch``), and someone the owner invites and who joins at once
(``invite_and_join``). And for the story of going out (``test_going_out.py``): its stack's names,
and how a person gives an account in its one-time form (``account_form_link``,
``give_the_account``).

They are here, and not in ``conftest.py``, so that the test module and the fixtures can both
import them (the rule of ``tests/stack/stack_support.py``, which the stack tests follow).
"""

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from string import Formatter
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from domo_common.i18n import t
from domo_common.meanings import default_text
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from tests.fakes.fake_llm.app import JUDGE_PREFIX
from tests.stack.stack_support import StackInfo

OWNER_DISPLAY_NAME = "Owner"
OWNER_USERNAME = "owner"
OWNER_PASSWORD = "a-long-test-passphrase"

# Where the gatekeeper finds the fake model service: its name on the egress network, the port it
# listens on (compose.test.yaml) and the path the API lives under.
FAKE_LLM_BASE_URL = "http://fake-llm:9000/v1"

# How long a page may take to show what a test waits for. Playwright's own default (5 s) is for a
# page that is already loaded; the model step polls the vault every 2 s and the first reply goes
# through the core, the gatekeeper and the model.
EXPECT_TIMEOUT_MS = 30_000
# How long the end of the first launch may take once the key is given: the key is checked, then the
# chat opens (no text is written for it). Kept generous: a stack that has just started is slow.
WRITING_TIMEOUT_MS = 120_000


def setup_stage(stack: StackInfo) -> str:
    """How far the first launch of the instance has got: ``owner`` (nobody has set it up),
    ``model`` (the owner exists, no model works yet) or ``done``. A visitor who is not signed in
    is sent to the current setup step for any page, and to the sign-in page once the setup is
    over."""
    answer = httpx.get(stack.base_url + "/chat", verify=False, follow_redirects=False)
    if answer.status_code not in (302, 303, 307, 308):
        raise AssertionError(f"/chat answered {answer.status_code}, not a redirect")
    path = urlsplit(answer.headers.get("location", "")).path
    stages = {"/setup/owner": "owner", "/setup/model": "model", "/login": "done"}
    if path not in stages:
        raise AssertionError(f"/chat redirects to {path!r}: not a stage of the setup")
    return stages[path]


def instance_is_new(stack: StackInfo) -> bool:
    """Whether nobody has set the instance up yet."""
    return setup_stage(stack) == "owner"


def complete_owner_step(page: Page, stack: StackInfo) -> None:
    """The first page: the setup code from the core's logs, and the owner's name and password.
    Ends on the model step."""
    page.goto(stack.base_url + "/")
    page.wait_for_url("**/setup/owner")
    page.fill("[name=code]", stack.setup_code())
    page.fill("[name=display_name]", OWNER_DISPLAY_NAME)
    page.fill("[name=username]", OWNER_USERNAME)
    page.fill("[name=password]", OWNER_PASSWORD)
    page.click("form[action='/setup/owner'] button[type=submit]")
    page.wait_for_url("**/setup/model", timeout=EXPECT_TIMEOUT_MS)


def submit_model_form(page: Page, *, base_url: str, has_key: bool) -> None:
    """The model step's form: another server that speaks the OpenAI API, at ``base_url``. No model
    name is typed: the fake lists its models."""
    page.select_option("[name=kind]", "openai_compatible")
    page.fill("[name=base_url]", base_url)
    page.set_checked("[name=has_key]", has_key)
    page.click("form.model-form button[type=submit]")


def enter_vault_key(page: Page, key: str) -> None:
    """The key goes into the vault's own form, in its frame: the core's page never sees it."""
    vault = page.frame_locator("iframe[src^='/vault/r/']")
    vault.locator("[name=value]").fill(key)
    vault.locator("button[type=submit]").click()


def complete_setup(page: Page, stack: StackInfo, *, base_url: str = FAKE_LLM_BASE_URL) -> None:
    """The rest of the first launch, with the right key, from wherever it has got to: the whole
    of it for a new instance, or the model step for an owner who exists but whose model does not
    work yet (a browser that has no session signs in first). The model is the fake at
    ``base_url``. Ends on the chat page."""
    stage = setup_stage(stack)
    if stage == "owner":
        complete_owner_step(page, stack)
    elif stage == "model":
        log_in(page, stack, setup_unfinished=True)
        page.goto(stack.base_url + "/setup/model")
    else:
        raise AssertionError("the setup is already done")
    submit_model_form(page, base_url=base_url, has_key=True)
    enter_vault_key(page, stack.test_key)
    page.wait_for_url("**/chat", timeout=WRITING_TIMEOUT_MS)


def finish_the_first_launch(
    stack: StackInfo,
    browser: Browser,
    context_args: Mapping[str, Any],
    *,
    base_url: str = FAKE_LLM_BASE_URL,
) -> None:
    """Make sure the first launch of ``stack`` is really over (the instance says so: its pages no
    longer lead to a setup step). What is left of it is done in a throwaway browser context, with
    the fake at ``base_url``: all of it for a new instance, the model step for one an earlier test
    left half way. An instance whose first launch is over is left as it is."""
    if setup_stage(stack) != "done":
        context = browser.new_context(**context_args)
        try:
            complete_setup(context.new_page(), stack, base_url=base_url)
        finally:
            context.close()
    stage = setup_stage(stack)
    assert stage == "done", f"the first launch is not over: the instance is at its {stage} step"


def log_in(
    page: Page,
    stack: StackInfo,
    *,
    setup_unfinished: bool = False,
    username: str = OWNER_USERNAME,
    password: str = OWNER_PASSWORD,
) -> None:
    """Sign in, as the owner unless another ``username`` and ``password`` are given. Ends on the
    chat page (on the model step, which stands in front of it, when ``setup_unfinished``)."""
    page.goto(stack.base_url + "/login")
    page.fill("[name=username]", username)
    page.fill("[name=password]", password)
    page.click("form[action='/login'] button[type=submit]")
    landing = r"/(chat|setup/model)$" if setup_unfinished else r"/chat$"
    page.wait_for_url(re.compile(landing), timeout=EXPECT_TIMEOUT_MS)


def send_message(page: Page, text: str) -> None:
    """Type ``text`` in the chat and send it. (The send button is looked up inside the chat form:
    the top bar has a submit button of its own, to sign out.)"""
    page.fill("#chat-form [name=text]", text)
    page.click('#chat-form [data-action="send"]')


def assistant_replies(page: Page) -> Locator:
    """The assistant's bubbles of the conversation."""
    return page.locator(".message.assistant")


# --- the household journey (test_household.py) ------------------------------------------------


@dataclass(frozen=True)
class Member:
    """Someone the owner invites, with the role they get, and the username and password they
    choose when they join (generic names, test values)."""

    display_name: str
    role: str
    username: str
    password: str


ALEX = Member("Alex", "adult", "alex", "alex-test-passphrase")
SAM = Member("Sam", "child", "sam", "sam-test-passphrase")
JUSTINE = Member("Justine", "adult", "justine", "justine-test-passphrase")
# People whom a chain waits for, and a guest whose stay changes before she leaves by herself.
HAL = Member("Hal", "adult", "hal", "hal-test-passphrase")
KIM = Member("Kim", "adult", "kim", "kim-test-passphrase")
IVY = Member("Ivy", "guest", "ivy", "ivy-test-passphrase")
# Someone the owner takes off the general key (test_models.py): he has no key of his own.
PAUL = Member("Paul", "adult", "paul", "paul-test-passphrase")
# The first owner, as the household page knows them (they never choose another name).
OWNER = Member(OWNER_DISPLAY_NAME, "owner", OWNER_USERNAME, OWNER_PASSWORD)
# What a person who comes back chooses: a new password (the username does not change). Alex comes
# back twice.
ALEX_NEW_PASSWORD = "alex-new-test-passphrase"
ALEX_THIRD_PASSWORD = "alex-third-test-passphrase"
SAM_NEW_PASSWORD = "sam-new-test-passphrase"

# Where the edge keeps the certificates of its local authority (the README's fingerprint check
# reads the root from there too).
AUTHORITY_CERTIFICATES = (
    "/data/caddy/pki/authorities/local/root.crt",
    "/data/caddy/pki/authorities/local/intermediate.crt",
)


def tool_message(name: str, arguments: Mapping[str, Any]) -> str:
    """The message that makes the fake model call the tool ``name`` with ``arguments`` (see
    ``tests/fakes/fake_llm``): ``/tool <name> <json>``, the JSON written compactly."""
    return f"/tool {name} {json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))}"


def judged(verdict: str, message: str) -> str:
    """``message`` with what the fake yes-judge answers of the action it makes Domo call
    (``tests/fakes/fake_llm``): ``/judge <verdict> <message>``, where ``verdict`` may carry a
    reason (``"do_not: that sounds unkind"``). The judge reads it in the person's last words; every
    other rule of the fake reads the message without it. Without it, the judge asks a plain yes
    (``JUDGE_DEFAULT``)."""
    return f"{JUDGE_PREFIX}{verdict} {message}"


def draft_in_words(words: str, draft: Mapping[str, Any]) -> str:
    """The owner's ``words`` for ``rights_propose``, then ``/draft`` and the draft that the fake
    rights writer will make of them (``tests/fakes/fake_llm``: the writer is a call of its own to
    the model, which sees these words and the household, and answers with the JSON that follows
    ``/draft``). The core checks that draft as it checks a real writer's."""
    return f"{words} /draft {json.dumps(draft, ensure_ascii=False, separators=(',', ':'))}"


def send_and_wait(page: Page, text: str) -> Locator:
    """Send ``text`` and wait until the reply is over (the page stops marking it busy once its
    stream has ended): the next message would otherwise be refused while it streams. Returns the
    reply's bubble, found by its message id, so that the locator still holds after a reload."""
    replies = assistant_replies(page)
    before = replies.count()
    send_message(page, text)
    expect(replies).to_have_count(before + 1, timeout=EXPECT_TIMEOUT_MS)
    reply = _bubble_number(page, before)
    expect(reply).not_to_have_attribute("aria-busy", "true", timeout=EXPECT_TIMEOUT_MS)
    return reply


def _bubble_number(page: Page, index: int) -> Locator:
    """The assistant's bubble at ``index`` (from 0), found again by its message id, so that the
    locator holds after a reload and when bubbles come after it."""
    message_id = assistant_replies(page).nth(index).get_attribute("data-message-id")
    assert message_id, "the new reply has no message id"
    return page.locator(f'.message.assistant[data-message-id="{message_id}"]')


def wait_for_a_new_reply(page: Page, before: int, timeout_ms: int = EXPECT_TIMEOUT_MS) -> Locator:
    """Wait, without reloading the page, until it shows one assistant bubble more than ``before``
    (what ``assistant_replies(page).count()`` said earlier): the reply Domo gives when a button
    wakes him, or when a chain that waited for someone's arrival goes on or stops by itself. The
    server writes it with no page asking for it; an open page finds it by asking every few seconds
    (``chat.js``), so this takes up to a few seconds after a button, and up to the upkeep's pass
    more after an arrival (``DOMO_UPKEEP_SECONDS`` of ``shared.env``): ``timeout_ms`` says how
    long to wait in all. Returns that bubble, which may still show Domo writing: its words come
    at a later asking, so read them with a timeout longer than one (``EXPECT_TIMEOUT_MS``)."""
    expect(assistant_replies(page)).to_have_count(before + 1, timeout=timeout_ms)
    return _bubble_number(page, before)


def no_new_reply(page: Page, before: int, seconds: float = 8.0) -> None:
    """Check that Domo, woken by a button, said nothing: that for ``seconds`` (longer than the
    few seconds the page takes to ask for what is new) the page does not show more assistant
    bubbles than ``before``, and that the server has none to give after the page's last bubble.
    Absence cannot be waited for: this is the one place where a test waits for a fixed time."""
    replies = assistant_replies(page)
    last = page.locator(".message[data-message-id]").last.get_attribute("data-message-id")
    assert last, "the page shows no message"
    page.wait_for_timeout(int(seconds * 1000))
    expect(replies).to_have_count(before)
    # The page's own asking could be late: ask the server as the page does.
    since = page.request.get(
        urljoin(page.url, f"/chat/since/{last}"), headers={"HX-Request": "true"}
    )
    assert since.ok and since.text().strip() == "", "The assistant said something after all"


def reply_text(reply: Locator) -> Locator:
    """The words of a reply's bubble."""
    return reply.locator(".message-text")


def filled(key: str, language: str, **holes: str) -> re.Pattern[str]:
    """The default phrase of ``key`` in ``language`` with ``holes`` filled, and any other hole (a
    date the code writes, say) filled with something."""
    pieces: list[str] = []
    for literal, field, _, _ in Formatter().parse(default_text(key, language)):
        pieces.append(re.escape(literal))
        if field is not None:
            pieces.append(re.escape(holes[field]) if field in holes else ".+?")
    return re.compile("^" + "".join(pieces) + "$", re.DOTALL)


def confirmation_card(reply: Locator) -> Locator:
    """The card of the request a reply shows (one per reply in these scenarios)."""
    return reply.locator(".confirmation-card")


def confirm_with_the_button(card: Locator, status: str = "executed") -> None:
    """Click the card's Confirm button, and wait until the card that comes back says what became of
    the request: that it ran (``status`` ``executed``), or ``failed`` (htmx puts the card in the
    place of the one that was clicked)."""
    card.locator("[data-action=confirm]").click()
    expect(card).to_have_attribute("data-confirmation-status", status, timeout=EXPECT_TIMEOUT_MS)


def refuse_with_the_button(card: Locator) -> None:
    """Click the card's Refuse button, and wait until the card that comes back says the request
    was refused."""
    card.locator("[data-action=refuse]").click()
    expect(card).to_have_attribute("data-confirmation-status", "refused", timeout=EXPECT_TIMEOUT_MS)


def prove_with_the_password(card: Locator, password: str) -> None:
    """On the ``card`` of a request that asks a proof that it is the person, type ``password`` in
    the proof's own field and send it (htmx puts the card that comes back in the place of this
    one). The password goes to the card's form only: never to the conversation."""
    card.locator("input[name=password]").fill(password)
    card.locator("[data-action=password-prove]").click()


def prove_with_the_passkey(card: Locator) -> None:
    """On the ``card`` of a request that asks a proof, use the passkey: the page's script
    (``static/passkeys.js``) asks the server for the options, lets the browser's authenticator
    answer (the virtual one of these tests answers by itself) and posts the answer from the card's
    form. The button is shown only where the browser can use passkeys and the person has one."""
    card.locator("[data-action=passkey-prove]").click()


def confirm_and_hear_back(
    page: Page, card: Locator, said: str = "Fake decided: executed", status: str = "executed"
) -> Locator:
    """Click the Confirm button of a request of the person whose ``page`` this is, and wait for
    what follows in that page, without reloading it: the card says what became of the request
    (``status``), and Domo, woken by the button, adds a bubble that says ``said`` (the fake
    model's words for a decided request). Waiting for that bubble also leaves the conversation
    free: a message sent while Domo answers is kept for that reply, and answered after it (spec
    11.3), which would add bubbles a test does not expect. Returns the bubble."""
    before = assistant_replies(page).count()
    confirm_with_the_button(card, status)
    heard = wait_for_a_new_reply(page, before)
    # The bubble may first show Domo writing: his words come at the page's next asking.
    expect(reply_text(heard)).to_have_text(said, timeout=EXPECT_TIMEOUT_MS)
    return heard


def invitation_link_pattern(stack: StackInfo) -> re.Pattern[str]:
    """What an invitation's link looks like: the page's own address, and a one-time token."""
    return re.compile(rf"^{re.escape(stack.base_url)}/invite/[A-Za-z0-9_-]+$")


def invitation_link(card: Locator, stack: StackInfo) -> str:
    """The link a card shows once, in the answer to the yes that made the invitation."""
    link = card.locator("a.action-link")
    expect(link).to_have_attribute("href", invitation_link_pattern(stack))
    href = link.get_attribute("href")
    assert href is not None
    return href


def show_link(card: Locator, step: int, stack: StackInfo) -> str:
    """The link that step ``step`` (from 1) of a chain left on the ``card`` of the one who asked:
    its own "Show the link" button (``[data-step="<step>"]``, under the step's number), clicked,
    shows it once, in the place of the button. The step's group is found by its number, which the
    answer keeps (the button's ``data-step`` goes with the button)."""
    number = t("action.card.step", "en", step=str(step))
    group = card.locator(".action-step-buttons").filter(
        has=card.page.locator(".action-step", has_text=re.compile(rf"^{re.escape(number)}$"))
    )
    group.locator(f'[data-step="{step}"] [data-action="show-link"]').click()
    return invitation_link(group, stack)


def join_with_invitation(page: Page, link: str, member: Member) -> None:
    """Open an invitation's link and join: the username and password ``member`` chooses. Ends on
    the chat page, signed in."""
    page.goto(link)
    page.fill("[name=username]", member.username)
    page.fill("[name=password]", member.password)
    page.click("form[action^='/invite/'] button[type=submit]")
    page.wait_for_url("**/chat", timeout=EXPECT_TIMEOUT_MS)


def become_the_member_again(page: Page, password: str) -> None:
    """On the page of a return link, choose a new ``password`` (the username is not asked: it does
    not change) and come back. Ends on the chat page, signed in."""
    page.fill("[name=password]", password)
    page.click("form[action^='/invite/'] button[type=submit]")
    page.wait_for_url("**/chat", timeout=EXPECT_TIMEOUT_MS)


def person_id_of(page: Page, stack: StackInfo, member: Member) -> str:
    """The id of ``member``, read on the household page (which the owner may open) with ``page``,
    the owner's. Ends back on the chat page."""
    page.goto(stack.base_url + "/household")
    person = page.locator(".household-person").filter(
        has=page.locator(".household-name", has_text=re.compile(rf"^{member.display_name}$"))
    )
    person_id = person.get_attribute("data-person-id")
    assert person_id, f"{member.display_name} is not on the household page"
    page.goto(stack.base_url + "/chat")
    return person_id


def database_text(stack: StackInfo, query: str) -> str:
    """What a query of the core's database selects (one value), for what no page shows. Read only:
    these tests never write to the database, the core does."""
    result = stack.exec(
        "db",
        [
            "psql",
            "--username=domo",
            "--dbname=domo",
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


def message_ids(page: Page) -> set[str]:
    """The ids of the messages the page shows."""
    ids: list[str] = page.locator(".message[data-message-id]").evaluate_all(
        "elements => elements.map(element => element.dataset.messageId)"
    )
    return set(ids)


class Household:
    """The people of the household journey, each in a browser context of their own (so each with
    their own session), for as long as the module runs: the owner, signed in, and the members
    who joined with the link of their invitation (``join``)."""

    def __init__(self, stack: StackInfo, browser: Browser, context_args: Mapping[str, Any]):
        self.stack = stack
        self._browser = browser
        self._context_args = dict(context_args)
        self._contexts: list[BrowserContext] = []
        self._members: dict[str, Page] = {}
        self.owner = self.new_page()
        log_in(self.owner, stack)

    def new_page(self, **options: Any) -> Page:
        """A page in a new browser context, nobody's yet (closed with the others). ``options``
        are more options of the context (``has_touch=True``: a touch screen)."""
        context = self._browser.new_context(**{**self._context_args, **options})
        self._contexts.append(context)
        return context.new_page()

    def join(self, member: Member, link: str) -> Page:
        """``member`` opens ``link`` in a browser of their own and joins."""
        page = self.new_page()
        join_with_invitation(page, link, member)
        self._members[member.display_name] = page
        return page

    def come_back(self, member: Member, page: Page) -> None:
        """``page`` is now ``member``'s: they came back to the household in it, and the page they
        had before no longer counts."""
        self._members[member.display_name] = page

    def page_of(self, member: Member) -> Page:
        """The page of a member who joined in an earlier scenario."""
        try:
            return self._members[member.display_name]
        except KeyError:
            raise AssertionError(
                f"{member.display_name} has not joined the household: the scenarios of "
                "test_household.py build on each other and run in the order of the file"
            ) from None

    def close(self) -> None:
        for context in self._contexts:
            context.close()


def invite_and_join(household: Household, member: Member) -> Page:
    """The owner invites ``member`` (an adult or a child), which the judge lets through: it is done
    at once, with no card, and the reply's card shows the link once. ``member`` joins with it, in
    a browser of their own. Returns their page, on the chat."""
    invitation = {"display_name": member.display_name, "role": member.role}
    reply = send_and_wait(
        household.owner, judged("do", tool_message("household_invite", invitation))
    )
    return household.join(member, invitation_link(reply, household.stack))


def authority_key_hashes(stack: StackInfo) -> list[str]:
    """The public keys of the stack's local certificate authority (its root and its
    intermediate), as Chromium's ``--ignore-certificate-errors-spki-list`` takes them: the
    base64 of the SHA-256 of each key's SubjectPublicKeyInfo. The edge presents the intermediate
    with its certificate, so a browser given these keys accepts the stack's certificate. The
    flag is not a trust anchor: Chromium ignores the certificate errors of any chain that holds
    one of these keys, whatever they are (a name that does not match, say), and checks every
    other certificate as usual."""
    hashes: list[str] = []
    for path in AUTHORITY_CERTIFICATES:
        result = stack.exec("edge", ["cat", path])
        if result.returncode != 0:
            raise AssertionError(f"could not read {path} in the edge: {result.stderr}")
        certificate = x509.load_pem_x509_certificate(result.stdout.encode())
        key = certificate.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        hashes.append(base64.b64encode(hashlib.sha256(key).digest()).decode())
    return hashes


# --- going out (test_going_out.py) ----------------------------------------------------------------

# The stack of the story of going out (conftest.py's ``going_out_stack_with_fake_llm``): its
# project, its env file, the port of this machine its device on the host is published on (apart
# from the stack tests' ``GOING_OUT_HOST_DEVICE_PORT``), and the name its site is reached by (and
# its certificate made for).
GOING_OUT_PROJECT = "domo-e2e-out"
GOING_OUT_ENV_FILE = "tests/e2e/going_out.env"
GOING_OUT_HOST_DEVICE_PORT = 18997
GOING_OUT_SITE = "site"


def account_form_link(reply: Locator) -> str:
    """The link to the one-time form of an account a reply shows once, on its card (the
    gatekeeper's own page, under the stack's address)."""
    link = reply.locator(".action-card a.action-link")
    expect(link).to_have_attribute("href", re.compile(r"^https://localhost:\d+/.+"))
    href = link.get_attribute("href")
    assert href is not None
    return href


def give_the_account(page: Page, link: str, fields: Mapping[str, str]) -> str:
    """The person whose ``page`` this is opens the form of ``link`` in a tab of their own browser,
    types the account (``fields``: ``value``, or ``user`` and ``password``) and sends it, as they
    would: from their browser to the vault, never through the conversation. Returns the words of
    the page that came back (no field any more), and closes the tab."""
    tab = page.context.new_page()
    try:
        tab.goto(link)
        for name, value in fields.items():
            tab.fill(f"form [name={name}]", value)
        tab.click("form button[type=submit]")
        expect(tab.locator("form")).to_have_count(0, timeout=EXPECT_TIMEOUT_MS)
        return tab.locator("body").inner_text()
    finally:
        tab.close()


class VirtualAuthenticator:
    """Chromium's virtual WebAuthn authenticator (the DevTools protocol's ``WebAuthn`` domain),
    attached to one page and kept across its navigations: an authenticator of the device that
    keeps passkeys (resident keys), checks its user (as a fingerprint would) and answers by
    itself, so that no prompt waits for a person."""

    def __init__(self, page: Page) -> None:
        self._session = page.context.new_cdp_session(page)
        self._session.send("WebAuthn.enable", {"enableUI": False})
        options = {
            "protocol": "ctap2",
            "transport": "internal",
            "hasResidentKey": True,
            "hasUserVerification": True,
            "isUserVerified": True,
            "automaticPresenceSimulation": True,
        }
        added = self._session.send("WebAuthn.addVirtualAuthenticator", {"options": options})
        self._id: str = added["authenticatorId"]

    def credentials(self) -> list[dict[str, Any]]:
        """The passkeys it holds."""
        answer = self._session.send("WebAuthn.getCredentials", {"authenticatorId": self._id})
        return list(answer["credentials"])
