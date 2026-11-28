"""Domo writes everything people read (0.7.0, spec ``2026-11-09-domo-ecrit-tout``, section 8:
scenarios 7 and 15), from default phrases he changes when he has a reason to (0.7.1, spec
``2026-11-11-essai-de-la-0-7-0``, section 7: scenarios 1, 2, 6 and 10), in real browsers, against
the real Docker Compose stack, with the fake model service in place of the provider
(``tests/fakes/fake_llm``).

The household's key gives the fake its marking address (``MARKED_BASE_URL``, under
``MARKED_PREFIX``): there, its writer of texts starts each text it writes with ``TEXT_MARK``
(``marked_text``), so that a page shows which of its texts the writer wrote; and an arrival turn
names who left the notes it is told (``Fake arrival: notes from Dana``), never their words. Every
other answer is the fake's usual one: its checker refuses nothing, Domo says ``Fake reply: ...``
or ``Fake tool result: <status>``, and an arrival with nothing to tell says nothing at all.

These scenarios have an instance of their own (``texts_stack``), new when the module starts, whose
first launch is made with that key. They build on each other and run in the order of this file,
each person in a browser context of their own (``people``). The arrival turns are on, as in a real
instance:

* scenario 1 (0.7.1): the installation, in a French browser, ends as soon as the key works: the
  chat opens at once, in French, and the writer was never called;
* scenario 7: Alex, an adult, asks to invite Tom for 2 days, which the owner must say yes to:
  each of them reads the head of the card written for them, checked, with Tom's name and the
  stay;
* scenario 15: Dana leaves a note for Sam, whose page has no zone of notes; Sam opens his page,
  the note is in the context of the arrival turn, as data (Domo says who left it), and asked for
  its exact words, Domo shows on a card what was kept of it;
* scenario 6 (0.7.1): Dana leaves Sam another note while his page is open: Domo tells it without
  Sam typing anything;
* scenario 10 (0.7.1): Dana leaves Alex a note, and Alex asks for its exact words: the card shows
  the passage of her message that is the note, as she typed it;
* scenario 2 (0.7.1, was 26): every page after the installation (the chat of each person, the
  account, the household, the audit log, an invitation, the sign-in, a page not found) shows only
  default phrases of the two catalogues (their holes filled), texts the writer wrote, names, the
  words people typed and what other parts of Domo write (his replies in the chat, the rights
  writer's summary of a right): never the sign "…" in place of a text.

A page hears from Domo by asking for what is new every 5 seconds, and the window "To approve"
asks for itself every 15 seconds: what arrives that way (the reply of an arrival, a head written
in the background, a news turn) is waited for much longer than that.
"""

import json
import re
from collections.abc import Collection, Iterator
from functools import cache
from importlib import resources
from string import Formatter
from typing import Any
from urllib.parse import urlsplit

import pytest
from domo_common.meanings import SIGN, default_text
from playwright.sync_api import Browser, Page, expect

from tests.fakes.fake_llm.app import ARRIVAL_NOTES_PREFIX, MARKED_PREFIX, TEXT_MARK
from tests.stack.stack_support import StackInfo, running_stack_with_fake_llm

from .conftest import FRESH_ENV_FILE, FRESH_PROJECT
from .support import (
    ALEX,
    EXPECT_TIMEOUT_MS,
    FAKE_LLM_BASE_URL,
    OWNER_DISPLAY_NAME,
    OWNER_USERNAME,
    PAUL,
    SAM,
    Household,
    Member,
    assistant_replies,
    complete_owner_step,
    confirmation_card,
    database_text,
    draft_in_words,
    enter_vault_key,
    filled,
    finish_the_first_launch,
    instance_is_new,
    invitation_link,
    invite_and_join,
    judged,
    person_id_of,
    reply_text,
    send_and_wait,
    submit_model_form,
    tool_message,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
# What a page gets by asking for it (the reply of an arrival every 5 seconds, the window "To
# approve" every 15 seconds), after the model wrote it: far longer than one ask.
ASKED_FOR_MS = 60_000
# A news turn comes to an open page at its next ask, at most one a minute (Sam's arrival of
# scenario 15 may be that recent), then its bubble at the ask after it.
NEWS_TURN_MS = 120_000
# The address of the fake under which its writer marks the texts it writes.
MARKED_BASE_URL = FAKE_LLM_BASE_URL.removesuffix("/v1") + MARKED_PREFIX + "/v1"
# Someone the owner invites in scenario 15, who leaves Sam a note.
DANA = Member("Dana", "adult", "dana", "dana-test-passphrase")
# The guest Alex asks to invite in scenario 7, and for how long.
TOM = "Tom"
TOM_FOR_TWO_DAYS = {"display_name": TOM, "role": "guest", "guest_days": 2}
TWO_DAYS = "2 days"
# The right of scenario 7, as the owner says it and as the rights writer sums it up.
RIGHTS_WORDS = "Alex may invite people, with my yes."
# The words of Dana's note for Sam, and of the one she leaves him while his page is open.
NOTE_FOR_SAM = "vide le lave-vaisselle"
ANOTHER_NOTE_FOR_SAM = "sors les poubelles"
# Dana's note for Alex (scenario 10): what Domo noted, and the passage of her message that is the
# note, as she typed it, her spelling mistake included (the rest of her message is not for Alex).
NOTED_FOR_ALEX = "Dana te demande de faire les courses."
DANA_S_WORDS = "Dit à Alex de faire les courses"
DANA_S_MESSAGE = f"{DANA_S_WORDS}, mais ne lui dis pas où je suis"
# Nothing Domo wrote: no text kept, and no call of the writer or of its checker.
TEXTS_WRITTEN = "SELECT count(*) FROM written_texts"
WRITER_CALLS = "SELECT count(*) FROM model_calls WHERE task IN ('writing', 'checking')"
# The draft of rights the rights writer wrote last, not applied yet (for Domo: the fake model
# takes it from nowhere else).
LATEST_DRAFT = (
    "SELECT id FROM policy_rights_drafts WHERE applied_at IS NULL ORDER BY created_at DESC LIMIT 1"
)
# The names a page may show as they are: Domo's, and the display names and usernames of the
# people of these scenarios and of those invited.
NAMES = frozenset(
    {
        "Domo",
        OWNER_DISPLAY_NAME,
        OWNER_USERNAME,
        *(member.display_name for member in (ALEX, SAM, DANA)),
        *(member.username for member in (ALEX, SAM, DANA)),
        TOM,
        PAUL.display_name,
    }
)

# What a page shows, as people take it in: each text of the page that is shown (those for screen
# readers included), the texts of the attributes that are read (a placeholder, a label, a title)
# and the title of its tab; each with where it is, and whether someone else than the writer wrote
# it: in a bubble of the conversation, the words a person typed or Domo's reply, and in a right
# of a card of rights, the rights writer's summary.
SHOWN_TEXTS = """() => {
  const visible = (element) =>
    element.checkVisibility({visibilityProperty: true, checkVisibilityCSS: true});
  const where = (element) => {
    const path = [];
    for (let at = element; at && at !== document.body && path.length < 4; at = at.parentElement) {
      path.unshift(at.tagName.toLowerCase() + (at.classList.length ? "." + at.classList[0] : ""));
    }
    return path.join(" > ");
  };
  const said = (element) =>
    element.closest(".message .message-text, .confirmation-rights li") !== null;
  // The tab's title is the page's title, then " - Domo" (base.html): its name.
  const title = document.title.trim().replace(/ - Domo$/, "");
  const shown = [{text: title, where: "title", said: false}];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = node.nodeValue.trim();
    const element = node.parentElement;
    if (text && element && visible(element)) {
      shown.push({text, where: where(element), said: said(element)});
    }
  }
  const named = document.body.querySelectorAll("[placeholder], [aria-label], [title], [alt]");
  for (const element of named) {
    if (!visible(element)) continue;
    for (const name of ["placeholder", "aria-label", "title", "alt"]) {
      const text = (element.getAttribute(name) || "").trim();
      if (text) shown.push({text, where: `${where(element)} [${name}]`, said: false});
    }
  }
  return shown;
}"""


def shown_texts(page: Page) -> list[dict[str, Any]]:
    """What ``page`` shows (``SHOWN_TEXTS``)."""
    return page.evaluate(SHOWN_TEXTS)


@cache
def default_phrases(language: str) -> tuple[str, ...]:
    """The default phrases of ``language``, as its catalogue holds them (holes left)."""
    path = resources.files("domo_common").joinpath("locales", f"{language}.json")
    return tuple(json.loads(path.read_text(encoding="utf-8")).values())


def one_spaced(text: str) -> str:
    """``text`` with each run of spaces as one space, and none around it."""
    return " ".join(text.split())


def _spaced(parts: list[str]) -> str:
    """The pattern of ``parts``, what a phrase has between its holes, each hole filled with
    something: their text, any run of spaces as one, and the space a part has next to a hole kept
    (what fills the hole is a word of its own: "at" in ``{date} at {time}`` is no piece of
    "chat.send"). The spaces at the two ends of the whole are dropped, as a page drops them."""
    patterns: list[str] = []
    for index, part in enumerate(parts):
        inner = re.escape(one_spaced(part)).replace(r"\ ", r"\s+")
        first, last = index == 0, index == len(parts) - 1
        if not inner:
            patterns.append(r"\s+" if part and not first and not last else "")
            continue
        before = r"\s+" if part[0].isspace() and not first else ""
        after = r"\s+" if part[-1].isspace() and not last else ""
        patterns.append(before + inner + after)
    return ".+?".join(patterns)


# The fewest letters of its own a default phrase, or a piece of one, has to be told by.
MIN_LETTERS = 2


@cache
def phrase_pattern(languages: tuple[str, ...]) -> re.Pattern[str]:
    """What a text of a page may be when it is a default phrase of ``languages``: a whole phrase,
    each hole filled with something, or the piece of one between two holes that a page shows in
    an element of its own (a name in bold, say), each hole inside it filled. A phrase or a piece
    with fewer than ``MIN_LETTERS`` letters of its own (``{date}``, ``{parts}.``,
    ``{hour}:{minute}``) would match nearly any text, a key or a phrase of another language
    included: it says nothing of the page, and gives no pattern."""
    patterns: set[str] = set()
    for language in languages:
        for phrase in default_phrases(language):
            segments = [""]
            for literal, field, _, _ in Formatter().parse(phrase):
                segments[-1] += literal
                if field is not None:
                    segments.append("")
            for first in range(len(segments)):
                for last in range(first, len(segments)):
                    parts = segments[first : last + 1]
                    if sum(char.isalpha() for char in "".join(parts)) >= MIN_LETTERS:
                        patterns.add(_spaced(parts))
    return re.compile("|".join(f"(?:{pattern})" for pattern in sorted(patterns)), re.DOTALL)


def not_a_default_phrase(
    page: Page, *, languages: tuple[str, ...] = ("en", "fr"), names: Collection[str] = NAMES
) -> list[str]:
    """What ``page`` shows that is neither a default phrase of ``languages`` (``phrase_pattern``),
    nor a text the writer wrote (it starts with the mark), nor one of ``names``, nor what someone
    else wrote (the words a person typed, Domo's replies, the rights writer's summaries), with
    where it is; and every sign shown in place of a text, wherever it is. The page must show two
    default phrases at least, a title and a heading say: a page read as showing nothing would pass
    for one that shows nothing else."""
    shown = shown_texts(page)
    pattern = phrase_pattern(languages)
    phrases = [item for item in shown if pattern.fullmatch(one_spaced(item["text"]))]
    assert len(phrases) >= 2, page.url
    return [
        f"{page.url} {item['where']}: {item['text']!r}"
        for item in shown
        if item["text"] == SIGN
        or (
            not item["said"]
            and not item["text"].startswith(TEXT_MARK)
            and item["text"] not in names
            and not pattern.fullmatch(one_spaced(item["text"]))
        )
    ]


def starts_with_the_mark() -> re.Pattern[str]:
    """What a text the writer wrote looks like: the mark first."""
    return re.compile(rf"^{re.escape(TEXT_MARK)} ")


def nothing_was_written(stack: StackInfo) -> bool:
    """Whether no text was kept for anyone, and neither the writer nor its checker was called."""
    return database_text(stack, TEXTS_WRITTEN) == "0" == database_text(stack, WRITER_CALLS)


def person_named(member: Member) -> str:
    """The query (a sub-query) of the id of ``member``."""
    return f"(SELECT id FROM persons WHERE display_name = '{member.display_name}')"


def alex_may_invite_with_the_owners_yes(alex_id: str) -> dict[str, Any]:
    """The draft the fake rights writer makes of ``RIGHTS_WORDS``: a right of Alex himself for
    ``household.invite``, with the owner's yes on each invitation, and one example, which the code
    checks: an invitation that asks the owner."""
    return {
        "changes": [
            {
                "person_id": alex_id,
                "action_type": "household.invite",
                "access": "allow",
                "approver": "owner",
                "summary": RIGHTS_WORDS,
            }
        ],
        "examples": [
            {
                "person_id": alex_id,
                "action_type": "household.invite",
                "facts": {
                    "role": "guest",
                    "days": 2,
                    "no_end": False,
                    "display_name": "Jo",
                    "owner_first": False,
                },
                "expected": "asks_the_owner",
            }
        ],
    }


def join_without_opening_the_chat(page: Page, link: str, member: Member, stack: StackInfo) -> None:
    """``member`` opens the invitation's ``link`` and joins, but does not go on to the chat: the
    form is sent as its page sends it (from its origin), and the answer, which leads to the chat,
    is not followed. The session it gives is the page's from now on, and the person's next chat
    page is their first visit."""
    page.goto(link)
    expect(page.locator("form[action^='/invite/'] [name=username]")).to_be_visible()
    answer = page.request.post(
        link,
        form={"username": member.username, "password": member.password},
        headers={"Origin": stack.base_url},
        max_redirects=0,
    )
    assert answer.status == 303, answer.status
    assert urlsplit(answer.headers["location"]).path == "/chat"


@pytest.fixture(scope="module")
def texts_stack() -> Iterator[StackInfo]:
    """An instance for this module alone, new when it starts, taken down with its volumes at its
    end. It has the project, the ports and the network of the stacks that ``conftest.py`` starts
    for one test that needs a new instance: those are over before this module starts, and the
    shared stack may be up next to it."""
    with running_stack_with_fake_llm(project=FRESH_PROJECT, env_file=FRESH_ENV_FILE) as info:
        yield info


@pytest.fixture(scope="module")
def people(
    texts_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
) -> Iterator[Household]:
    """The owner of the module's instance, signed in, and the members who join during the module,
    each in a browser context of their own, closed at its end. If the first scenario did not go
    through the first launch (a scenario run alone), it is done here first, with the key of the
    marking address."""
    finish_the_first_launch(texts_stack, browser, browser_context_args, base_url=MARKED_BASE_URL)
    household = Household(texts_stack, browser, browser_context_args)
    try:
        yield household
    finally:
        household.close()


def test_the_installation_ends_at_once_and_the_chat_opens_in_french(  # scenario 1
    texts_stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]
):
    stack = texts_stack
    assert instance_is_new(stack), "the first launch is the module's first scenario: run it first"
    # The owner's browser is in French: the installation is, and so is the house.
    french = browser.new_context(**{**browser_context_args, "locale": "fr-FR"})
    try:
        page = french.new_page()
        page.goto(stack.base_url + "/")
        page.wait_for_url("**/setup/owner")
        expect(page.locator("h1")).to_have_text(default_text("setup.owner.title", "fr"))
        complete_owner_step(page, stack)
        expect(page.locator("h1")).to_have_text(default_text("setup.model.title", "fr"))

        # The key works: the chat opens at once, with no page that waits for texts.
        submit_model_form(page, base_url=MARKED_BASE_URL, has_key=True)
        enter_vault_key(page, stack.test_key)
        page.wait_for_url("**/chat", timeout=T)
        expect(page.locator("#chat-form [data-action=send]")).to_have_text(
            default_text("chat.send", "fr")
        )
        # Everything on it is French, in the default phrases: nothing was written for it.
        assert not_a_default_phrase(page, languages=("fr",)) == []
        hello = send_and_wait(page, "bonjour")
        expect(reply_text(hello)).to_have_text("Fake reply: bonjour")
        assert nothing_was_written(stack)
    finally:
        french.close()


def test_each_reader_of_a_request_reads_a_head_written_for_them(people: Household):  # scenario 7
    owner, stack = people.owner, people.stack
    alex = invite_and_join(people, ALEX)

    # The owner lets Alex invite people, with his yes on each invitation: the rights writer writes
    # the right of his words (the fake makes this draft of them), and applying it takes his yes.
    alex_id = person_id_of(owner, stack, ALEX)
    words = draft_in_words(RIGHTS_WORDS, alex_may_invite_with_the_owners_yes(alex_id))
    proposed = send_and_wait(owner, tool_message("rights_propose", {"request": words}))
    expect(reply_text(proposed)).to_have_text("Fake tool result: done")
    draft_id = database_text(stack, LATEST_DRAFT)
    applying = send_and_wait(owner, tool_message("rights_apply", {"draft": draft_id}))
    expect(confirmation_card(applying)).to_have_attribute(
        "data-confirmation-status", "pending", timeout=T
    )
    applied = send_and_wait(owner, "oui")
    expect(confirmation_card(applied)).to_have_attribute(
        "data-confirmation-status", "executed", timeout=T
    )

    # Alex asks to invite Tom for 2 days: it waits for the owner's yes. The head of his card was
    # written for him in his turn, checked against the exact action: Tom and the stay are in it.
    asked = send_and_wait(alex, tool_message("household_invite", TOM_FOR_TWO_DAYS))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    card = confirmation_card(asked)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    request_id = card.get_attribute("data-confirmation-id")
    assert request_id
    his = card.locator(".confirmation-head")
    expect(his).to_have_text(starts_with_the_mark(), timeout=T)
    expect(his).to_contain_text(TOM)
    expect(his).to_contain_text(TWO_DAYS)

    # The owner reads it in his window "To approve", with the head written for him, in the
    # background on his keys: Tom and the stay are in it too.
    owner.goto(stack.base_url + "/chat")
    theirs = owner.locator(
        f'#approvals .confirmation-card[data-confirmation-id="{request_id}"] .confirmation-head'
    )
    expect(theirs).to_have_text(starts_with_the_mark(), timeout=ASKED_FOR_MS)
    expect(theirs).to_contain_text(TOM)
    expect(theirs).to_contain_text(TWO_DAYS)

    # One head for each reader, kept with the card once it was checked.
    readers = database_text(
        stack,
        "SELECT string_agg(p.display_name, ',' ORDER BY p.display_name) FROM card_texts c "
        f"JOIN persons p ON p.id = c.reader_id WHERE c.confirmation_id = '{request_id}'",
    )
    assert readers == f"{ALEX.display_name},{OWNER_DISPLAY_NAME}"


def test_a_note_is_told_by_domo_then_shown_word_for_word(people: Household):  # scenario 15
    owner, stack = people.owner, people.stack
    dana = invite_and_join(people, DANA)
    # Sam, a child, joins with his link, and opens his chat only later.
    invitation = {"display_name": SAM.display_name, "role": SAM.role}
    invited = send_and_wait(owner, judged("do", tool_message("household_invite", invitation)))
    sam = people.new_page()
    join_without_opening_the_chat(sam, invitation_link(invited, stack), SAM, stack)
    people.come_back(SAM, sam)

    note = {"recipient": SAM.display_name, "text": NOTE_FOR_SAM}
    left = send_and_wait(dana, judged("do", tool_message("note_leave", note)))
    expect(left.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    note_id = database_text(stack, f"SELECT id FROM notes WHERE text = '{NOTE_FOR_SAM}'")

    # Sam opens his page, for the first time: there is no zone of notes. The arrival turn has the
    # note in its context, as data, and Domo says who left it (the fake names who left the notes
    # it is told, never their words).
    sam.goto(stack.base_url + "/chat")
    expect(sam.locator("#notes")).to_have_count(0)
    expect(reply_text(assistant_replies(sam).last)).to_have_text(
        f"{ARRIVAL_NOTES_PREFIX}{DANA.display_name}", timeout=ASKED_FOR_MS
    )
    arrivals = "SELECT count(*) FROM model_calls WHERE task = 'arrival' AND person_id = "
    assert database_text(stack, arrivals + person_named(SAM)) == "1"
    # A turn with tools marks a note told only when Domo says so (note_told): not yet.
    assert database_text(stack, f"SELECT told_at IS NULL FROM notes WHERE id = '{note_id}'") == "t"

    # "What did she write exactly?": Domo shows what was kept of her note on a card. It kept no
    # passage of her message (her message was only the command that made the call, which gave no
    # passage), so the card says that what it shows is what Domo noted.
    shown = send_and_wait(sam, tool_message("note_show", {"note": note_id}))
    expect(reply_text(shown)).to_have_text("Fake tool result: done")
    noted = filled(
        "action.card.done.note.show.noted", "en", name=DANA.display_name, words=NOTE_FOR_SAM
    )
    expect(shown.locator(".action-text")).to_have_text(noted, timeout=T)


def test_a_note_for_someone_whose_page_is_open_is_told_without_their_typing(
    people: Household,
):  # scenario 6 of the trial of 0.7.0
    stack = people.stack
    dana, sam = people.page_of(DANA), people.page_of(SAM)
    # Sam's page stays open on the chat, after the last scenario; he types nothing.
    replies = assistant_replies(sam)
    shown = replies.count()
    note = {"recipient": SAM.display_name, "text": ANOTHER_NOTE_FOR_SAM}
    left = send_and_wait(dana, judged("do", tool_message("note_leave", note)))
    expect(left.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    # His page asks what is new every few seconds: Domo has an arrival turn for him at once (the
    # fake names who left the notes it is told), and its bubble comes on its own.
    expect(replies).to_have_count(shown + 1, timeout=NEWS_TURN_MS)
    # Dana's notes it was told: this one, and the one of the last scenario if it is not told yet.
    dana_s = re.escape(DANA.display_name)
    told = re.compile(rf"^{re.escape(ARRIVAL_NOTES_PREFIX)}{dana_s}(, {dana_s})?$")
    expect(reply_text(replies.last)).to_have_text(told)
    arrivals = "SELECT count(*) FROM model_calls WHERE task = 'arrival' AND person_id = "
    assert database_text(stack, arrivals + person_named(SAM)) == "2"


def test_the_exact_words_of_a_note_are_the_ones_its_author_typed(
    people: Household,
):  # scenario 10 of the trial of 0.7.0
    stack = people.stack
    dana, alex = people.page_of(DANA), people.page_of(ALEX)
    alex.goto(stack.base_url + "/chat")
    alex.wait_for_load_state("networkidle")
    replies = assistant_replies(alex)
    shown_before = replies.count()
    # Dana's message holds the note and something that is not for Alex; Domo notes it in his
    # words, and says which passage of her message is the note.
    note = {"recipient": ALEX.display_name, "text": NOTED_FOR_ALEX, "exact": DANA_S_WORDS}
    message = f"{DANA_S_MESSAGE}\n{judged('do', tool_message('note_leave', note))}"
    left = send_and_wait(dana, message)
    expect(left.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    note_id = database_text(
        stack, f"SELECT id FROM notes WHERE recipient_id = {person_named(ALEX)}"
    )
    # The code kept her own words, the passage alone.
    kept = database_text(stack, f"SELECT exact_text FROM notes WHERE id = '{note_id}'")
    assert kept == DANA_S_WORDS

    # Alex's page is open: Domo tells him at once that Dana left him a note.
    expect(replies).to_have_count(shown_before + 1, timeout=NEWS_TURN_MS)
    told = f"{ARRIVAL_NOTES_PREFIX}{DANA.display_name}"
    expect(reply_text(replies.last)).to_have_text(told, timeout=ASKED_FOR_MS)
    # "What did she write exactly?": the card shows her words, as she typed them, with who and
    # when, and nothing of the rest of her message.
    shown = send_and_wait(alex, tool_message("note_show", {"note": note_id}))
    expect(reply_text(shown)).to_have_text("Fake tool result: done")
    exact = filled("action.card.done.note.show", "en", name=DANA.display_name, words=DANA_S_WORDS)
    card = shown.locator(".action-text")
    expect(card).to_have_text(exact, timeout=T)
    expect(card).not_to_contain_text("où je suis")


def test_no_page_shows_a_sign_for_a_text(people: Household):  # scenario 2 (was 26)
    owner, stack = people.owner, people.stack
    # An invitation that waits, for a visitor to open.
    invitation = {"display_name": PAUL.display_name, "role": "guest", "guest_days": 3}
    invited = send_and_wait(owner, judged("do", tool_message("household_invite", invitation)))
    link = invitation_link(invited, stack)

    found: list[str] = []
    # The owner's pages: Alex's request still waits for his yes in his window "To approve".
    owner.goto(stack.base_url + "/chat")
    expect(owner.locator("#approvals .confirmation-card")).to_have_count(1, timeout=ASKED_FOR_MS)
    found += not_a_default_phrase(owner)
    for path in ("/account", "/household", "/audit"):
        owner.goto(stack.base_url + path)
        found += not_a_default_phrase(owner)
    owner.goto(stack.base_url + "/no-such-page")
    # The owner reads French, the house's language.
    expect(owner.locator("h1")).to_have_text(default_text("error.not_found.title", "fr"))
    found += not_a_default_phrase(owner)
    # The members' pages.
    for member, paths in (
        (ALEX, ("/chat", "/account")),
        (SAM, ("/chat", "/account")),
        (DANA, ("/chat",)),
    ):
        page = people.page_of(member)
        for path in paths:
            page.goto(stack.base_url + path)
            page.wait_for_load_state("networkidle")
            found += not_a_default_phrase(page)
    # A visitor's: the invitation, the sign-in, a page not found.
    visitor = people.new_page()
    for address in (link, stack.base_url + "/login", stack.base_url + "/no-such-page"):
        visitor.goto(address)
        found += not_a_default_phrase(visitor)
    not_found = {default_text("error.not_found.title", language) for language in ("en", "fr")}
    assert visitor.locator("h1").inner_text() in not_found

    assert found == [], "\n".join(found)
