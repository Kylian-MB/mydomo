"""The household's journey, in real browsers, against the real Docker Compose stack, with the fake
model service in place of the provider (``tests/fakes/fake_llm``).

The fake model makes Domo act on demand: a message ``/tool <name> <json>`` makes it call that
tool (``support.tool_message`` writes one), and it answers a tool's result with ``Fake tool
result: <status>``. Every call goes through the core as a real model's would: the policy barrier,
the yes-judge, the confirmations, the tools. The isolated classifier of the confirmations gets
``unclear`` from the fake (unless the reply is exactly ``yes``, ``no`` or ``change``), so the
replies these scenarios type are the short phrases the code recognises by itself (``oui``, ``je
confirme``). A yes with a change is typed as the call that applies it (``/tool confirmation_amend
...``): the classifier reads that as a change, which lets Domo change the request in that turn,
and the main model makes the call.

The yes-judge (spec ``2026-11-03-domo-juge-du-oui``) decides, after the barrier, what is asked
before an action that changes something: nothing ("do"), a yes, an explicit yes (the button, or "I
confirm"), a proof that it is the person, or that it is not done ("do_not"). The fake judge reads
the person's last words: a message that starts with ``/judge <verdict>`` (``support.judged``) says
what it answers; without it, it asks a plain yes. So an action of these scenarios that is done at
once (a note, a name) says "do", and a request asks a plain yes unless the owner's rules ask
more, or the scenario has the judge ask a firm yes (``FIRM``). Nothing in the code asks a yes by
default (the owner's decision of 2026-11-15: what is asked for and understood is done). Whatever
the judge says, the rights come first: a child's request still waits for an adult.

The scenarios build on each other and run in the order of this file, on the shared instance
(``stack_with_fake_llm``, after the first launch: ``completed_setup``), each person in a browser
context of their own (``household``). Five come first, about the chat page itself, and need
nothing before them (spec 5.7): Enter sends the message and Shift+Enter adds a line, and on a
touch screen Enter adds a line and the button sends; a reply the owner leaves the page during goes
on, so that it is there, whole, when they come back (the fake model answers slowly a message that
asks it to, ``/slow``), even when they write again first; and a reply whose stream stops short
shows Domo writing, then the reply. Then:

1. the owner invites Alex (an adult), with the button: the judge asks a yes, and the answer shows
   the invitation's link once; Alex opens it in another browser and joins;
2. Alex leaves a note for the owner, which the judge lets through (a note is just a message):
   there is no zone of notes on the owner's page; asked for its exact words, the owner's Domo
   shows them on a card, put in by the code, then says that he told it (spec 4.8);
3. the owner invites Sam (a child); Sam's note needs no adult's yes either, and reaches the
   owner's Domo at once;
4. Alex asks Domo to make Sam an adult: refused by the barrier, and nothing changes on the
   household page;
5. Alex sees nothing of the owner's conversation, not even by asking for one of its replies;
   he gives Domo a name of his own, "Brenda", which his page alone shows, with no card (spec
   6.7 of ``2026-11-15-la-memoire-et-le-temps``), then calls him Domo again;
6. Alex adds a passkey from the account page, signs out, and signs in with it (a browser given
   the keys of the stack's authority, so that its pages have no certificate error, and
   Chromium's virtual authenticator); then, in a browser of the same kind, the judge asks him a
   proof before he renames himself, and he gives it with a passkey, on the card;
7. the owner asks Domo for the model's key form, says yes with the button, and types the key in
   the vault's frame: the card says the new key works;
8. the judge lets the owner's invitation of Paul through: done at once, with no card, and the
   reply shows the link; it asks a yes for Dana's: a card, "oui", done. Then it asks a proof for
   Nico's: a wrong password does nothing, the right one runs it. And it does not let the owner's
   note to Alex through: Domo says why; the owner argues, Domo asks again, and the judge sees
   the new words and lets it through;
9. Alex leaves the owner a note, asks Domo what he left, and takes it back: the owner's Domo
   can no longer show it, and it cannot be taken back twice;
10. the owner says "Alex may invite guests, 3 days at most": Domo has the rights written by the
    rights writer (a call of its own to the model, which the fake answers with the draft the
    test wrote), the owner reads the examples on the card and says yes (applying rights takes his
    yes). Alex invites a guest for 2 days: it waits for the owner's yes (the right says so), and
    once the owner gave it, the button on Alex's card shows him the link, once. Alex invites an
    adult: refused by the limit;
11. the owner removes Alex and brings him back: the page of the link welcomes him back and shows
    his username; he chooses a new password and finds his conversation. The right the owner
    gave him is paused until they give it back;
12. the owner says "always ask me for the button before removing anyone": a rule of his, which the
    code holds whatever the judge says. Removing Alex, which the judge lets through, takes "I
    confirm"; Alex comes back once more;
13. a button on a request wakes Domo, in the conversation of the person who asked, page open or
    not: after a no he stays silent (nothing appears), after a yes he answers and his answer
    appears in the open page without reloading it. After a yes said in words, it is his answer
    that shows, not the fixed sentence;
14. the owner keeps a list of successors: the judge asks a firm yes of it (the card shows the list
    before and after), an adult is refused it, and a death is noted at once (the next successor
    comes first);
15. a chain of actions stopped after a button: the first step stays done, the card says the chain
    failed, and Domo, woken, is told;
16. the owner changes a chain that waits ("yes, but 5 days"): Domo gives the whole chain back,
    the judge asks a yes of the changed chain, so a new card replaces the old one, and "oui" runs
    it;
17. a chain is judged once, as a whole: one "oui" cancels Fay's invitation and invites her again
    as a guest (a step checked in its turn asks no yes of its own); a chain that hands the owner's
    role over, of which the judge asks a firm yes, takes "I confirm". It
    waits for Gil to join, and is
    dropped; his invitation stays;
18. a chain waits for someone to join: Hal's invitation is made, the chain pauses, its card shows
    the link behind a button; Hal joins in another browser and, at the upkeep's next pass, the
    chain goes on by itself (a note for Hal) and Domo says so in the owner's open page. Ida's
    invitation is cancelled before she joins: the chain stops at the wait, and Domo is told;
19. a chain's first step fails because Joy's name was taken meanwhile: Domo takes it up again
    with "Joy B" (``chain_continue`` with the steps changed), a new card, and both steps run;
20. one chain makes two links (Sam brought back as a child, Léa invited): each waits behind its
    own button on the card, none is stored anywhere; Sam comes back with his;
21. the owner lets Sam invite with an adult's yes; Sam invites Tom for 7 days, Alex has Domo
    show the request in his own chat and says "yes, but 3 days, not more": the invitation is made
    for 3 days at once, the note is kept for Sam, whose open page hears from Domo, and the
    link's button is on Sam's card only;
22. Sam drops his request that waits for an adult;
23. Ivy, a guest, stays longer; Justine's death, noted by mistake, is undone (a firm yes);
    Alex leaves himself a note, which his Domo shows him; the owner keeps one of two notes for
    Ivy a month, Ivy leaves by herself, and the upkeep erases the other note only; Alex may not
    remove Sam;
24. whatever the judge says, a child's request waits for an adult: Sam's invitation, for which the
    judge asks a proof, waits for an adult, who proves it is them in their window "To approve"
    (the window, which reloads itself, keeps the password the owner types); Sam asks to leave,
    the judge lets it through, it waits for an adult all the same, and the owner says yes;
25. the owner leaves in one sentence: hand the role to Alex, then remove oneself, one card, one
    "I confirm" (a plain "oui" is not enough); there is one owner at the end, Alex, and the
    former owner's next page is the sign-in;
26. Alex, the owner now, invites Kim and gives her his role once she has joined: the chain pauses,
    Kim joins, and the chain goes on by itself under the yes Alex gave: Kim is the only owner;
27. Kim, the owner now, tells Domo never to ask her (spec 6.3): for the hand-over of her role
    "not at all", then for applying rights when Domo judges he understood her, each a change
    of rights judged like any other; the judge may still ask her a proof before a hand-over. Then
    rights the judge lets through are applied at once, and so is her hand-over: Alex is the owner,
    with no card.

Domo answers every yes and every no (spec 5 of ``2026-11-01-actions-enchainees-depart``): after
a yes said in words, in the reply to those words (the fake model says ``Fake decided: <outcome>``);
after a button, a proof, or an approver's words, or once a chain that waited for someone's arrival
goes on or stops by itself (the core's upkeep looks every 5 seconds here, ``shared.env``), woken by
the server, in the conversation of the person who asked, and an open page finds his reply by
asking every few seconds. A scenario that clicks a button therefore waits for that reply
(``confirm_and_hear_back``, ``no_new_reply``) before it goes on: a message sent while Domo
answers is kept and answered after that reply (spec 11.3), and a bubble that arrives while a test
counts them would confuse it.
"""

import re
import uuid
from typing import Any

import pytest
from domo_common.i18n import t
from playwright.sync_api import Browser, Locator, Page, expect

from tests.fakes.fake_llm.app import JUDGE_DEFAULT, SLOW_PREFIX
from tests.stack.stack_support import StackInfo

from .support import (
    ALEX,
    ALEX_NEW_PASSWORD,
    ALEX_THIRD_PASSWORD,
    EXPECT_TIMEOUT_MS,
    HAL,
    IVY,
    JUSTINE,
    KIM,
    OWNER,
    OWNER_DISPLAY_NAME,
    OWNER_PASSWORD,
    SAM,
    SAM_NEW_PASSWORD,
    Household,
    VirtualAuthenticator,
    assistant_replies,
    become_the_member_again,
    confirm_and_hear_back,
    confirm_with_the_button,
    confirmation_card,
    database_text,
    draft_in_words,
    enter_vault_key,
    filled,
    invitation_link,
    judged,
    log_in,
    message_ids,
    no_new_reply,
    person_id_of,
    prove_with_the_passkey,
    prove_with_the_password,
    refuse_with_the_button,
    reply_text,
    send_and_wait,
    send_message,
    show_link,
    tool_message,
    wait_for_a_new_reply,
)

pytestmark = pytest.mark.stack

T = EXPECT_TIMEOUT_MS
# What the fake model says of a request that was just decided and ran (tests/fakes/fake_llm).
DECIDED = "Fake decided: executed"
# The reason the fake judge gives when the person's words do not say what it answers: it asks a
# plain yes ("yes: fake judge"), and the card says that Domo asks it, with this reason.
FAKE_JUDGE_REASON = JUDGE_DEFAULT.split(":", 1)[1].strip()
JUDGE_ASKS_A_YES = t("confirmation.asked.domo.yes", "en", reason=FAKE_JUDGE_REASON)
# A judge that asks a firm yes, of its own judgement (nothing asks one by default any more, since
# 2026-11-15: what is asked for and understood is done), and what the card then says.
FIRM = "explicit_yes: a big step"
JUDGE_ASKS_A_FIRM_YES = t("confirmation.asked.domo.explicit_yes", "en", reason="a big step")
# Who owns the household, as the database says.
OWNERS = "SELECT display_name FROM persons WHERE role = 'owner'"
# The note the judge does not let through at first (scenario 8), then does once the owner argued.
TEASING_NOTE = "Tu perds toujours aux échecs"
# The note of scenario 9: it has words of its own, so that the database tells it from the notes
# of the earlier scenarios.
NOTE_TO_TAKE_BACK = "Un mot à retirer"
# The right of scenarios 10, 11 and 27, as the owner says it, as the writer sums it up for the
# owner (the same sentence: the writer writes it in the language of the owner's words), and as a
# limit.
RIGHTS_WORDS = "Alex peut inviter des invités, 3 jours au plus."
THREE_DAYS = 'action.facts.role == "guest" && action.facts.days <= 3 ? "allow" : "refuse"'
# The owner's rule of scenario 12, and the "never ask me" of scenario 27, as the owner says them
# and as the writer sums them up.
REMOVE_RULE_WORDS = "Demande-moi toujours le bouton avant de retirer quelqu'un."
HAND_OVER_LIFT_WORDS = "Ne me demande plus jamais pour transmettre mon rôle."
RIGHTS_LIFT_WORDS = "N'attends plus mon oui pour les droits quand tu m'as bien compris."

# A step of a chain (``do_in_order``), as the model gives it.
type JsonStep = dict[str, Any]
# How long a page may wait for Domo once a chain that waited for someone's arrival goes on or
# stops by itself: the upkeep's next pass (every 5 seconds, shared.env), then his reply.
RESUME_MS = 30_000
# The words of the notes the chains leave, each its own so that a test tells them apart.
NOTE_OF_A_CHANGED_CHAIN = "Une suite changée"
NOTE_OF_A_RESUMED_CHAIN = "Une suite reprise"
WELCOME = "Bienvenue"
NOTE_TO_ONESELF = "appeler le plombier"
KEPT_NOTE = "Un mot gardé"
ERASED_NOTE = "Un mot effacé"
# Someone invited in the same chain as a person who comes back.
LEA = "Léa"
# The right of scenario 21, and what the adult says with his change, for the child.
SAM_RIGHTS_WORDS = "Sam peut inviter, avec l'accord d'un adulte."
FIRM_NOTE = "3 jours, pas plus"
# What the owner asks the fake model to answer slowly: eighteen pieces, half a second each.
SLOW_WORDS = (
    "un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze quinze seize"
)


def test_enter_sends_and_shift_enter_adds_a_line(household: Household):
    owner = household.owner
    owner.goto(household.stack.base_url + "/chat")
    box = owner.locator("#chat-text")
    sent = owner.locator(".message.user")
    replies = assistant_replies(owner)
    before, replies_before = sent.count(), replies.count()
    box.click()
    box.press_sequentially("line one")
    box.press("Shift+Enter")
    box.press_sequentially("line two")
    expect(box).to_have_value("line one\nline two")  # a new line, and nothing sent
    expect(sent).to_have_count(before)
    box.press("Enter")
    expect(sent).to_have_count(before + 1, timeout=T)
    assert sent.last.locator(".message-text").evaluate("e => e.textContent") == (
        "line one\nline two"
    )
    expect(replies).to_have_count(replies_before + 1, timeout=T)
    expect(replies.last).not_to_have_attribute("aria-busy", "true", timeout=T)
    expect(box).to_have_value("")
    # Enter on an empty box sends nothing, nor on a box of spaces, and adds no line either.
    box.press("Enter")
    box.press_sequentially("   ")
    box.press("Enter")
    owner.wait_for_timeout(1000)
    expect(box).to_have_value("   ")
    expect(sent).to_have_count(before + 1)
    expect(owner.locator("#chat-notice")).to_have_text("")  # not refused: never sent
    box.fill("")


def test_the_reply_goes_on_when_the_owner_opens_another_page(household: Household):
    owner, stack = household.owner, household.stack
    owner.goto(stack.base_url + "/chat")
    replies = assistant_replies(owner)
    before = replies.count()
    send_message(owner, SLOW_PREFIX + SLOW_WORDS)
    expect(replies).to_have_count(before + 1, timeout=T)
    message_id = replies.last.get_attribute("data-message-id")
    assert message_id
    writing = owner.locator(f'.message.assistant[data-message-id="{message_id}"]')
    expect(reply_text(writing)).to_contain_text("Fake", timeout=T)  # Domo has begun
    expect(writing).to_have_attribute("aria-busy", "true")  # and still writes
    owner.goto(stack.base_url + "/household")
    expect(owner).to_have_url(re.compile(r"/household$"))
    owner.goto(stack.base_url + "/chat")
    # Back on the chat while Domo still writes: the page shows him writing (spec 5.7.2), then
    # the whole reply in that place as soon as it is over (the page asks for what is new).
    back = owner.locator(f'.message.assistant[data-message-id="{message_id}"]')
    expect(back).to_have_attribute("data-writing", "true", timeout=T)
    expect(back.locator(".visually-hidden")).to_have_text(
        t("chat.status.writing", "en", domo="Domo")
    )
    expect(reply_text(back)).to_have_text(f"Fake reply: {SLOW_WORDS}", timeout=T)
    expect(back).not_to_have_attribute("data-writing", "true")
    expect(back).to_have_count(1)
    status = database_text(stack, f"SELECT status FROM messages WHERE id = '{message_id}'")
    assert status == "complete"


def test_enter_adds_a_line_on_a_touch_screen_and_the_button_sends(household: Household):
    """A phone's keyboard has no Shift+Enter: on a touch screen, Enter adds a line and the button
    sends, as in a phone's messaging app (review of Task 9). The owner, signed in again on a
    phone (Chromium's touch emulation: ``(hover: none) and (pointer: coarse)``)."""
    phone = household.new_page(has_touch=True, is_mobile=True)
    log_in(phone, household.stack)
    box = phone.locator("#chat-text")
    sent = phone.locator(".message.user")
    replies = assistant_replies(phone)
    before, replies_before = sent.count(), replies.count()
    box.tap()
    box.press_sequentially("line one")
    box.press("Enter")
    box.press_sequentially("line two")
    expect(box).to_have_value("line one\nline two")  # a new line, and nothing sent
    phone.wait_for_timeout(500)
    expect(sent).to_have_count(before)
    phone.locator('#chat-form [data-action="send"]').tap()
    expect(sent).to_have_count(before + 1, timeout=T)
    assert sent.last.locator(".message-text").evaluate("e => e.textContent") == (
        "line one\nline two"
    )
    expect(replies).to_have_count(replies_before + 1, timeout=T)
    expect(replies.last).not_to_have_attribute("aria-busy", "true", timeout=T)


def test_the_reply_stays_when_the_owner_writes_again_after_he_opens_another_page(
    household: Household,
):
    """The owner leaves the page while Domo writes and comes back: the page shows Domo
    writing. The reply is over, and before the page asks again (it does every five seconds) the
    owner sends a message, whose pair comes after the bubble that shows Domo writing: the reply
    still takes that bubble's place (review of Task 9, Important 2)."""
    owner, stack = household.owner, household.stack
    owner.goto(stack.base_url + "/chat")
    replies = assistant_replies(owner)
    before = replies.count()
    send_message(owner, SLOW_PREFIX + SLOW_WORDS)
    expect(replies).to_have_count(before + 1, timeout=T)
    message_id = replies.last.get_attribute("data-message-id")
    assert message_id
    first = owner.locator(f'.message.assistant[data-message-id="{message_id}"]')
    expect(reply_text(first)).to_contain_text("Fake", timeout=T)
    owner.goto(stack.base_url + "/household")
    owner.goto(stack.base_url + "/chat")
    expect(first).to_have_attribute("data-writing", "true", timeout=T)
    # As soon as the database holds the reply over, the owner writes again.
    query = f"SELECT status FROM messages WHERE id = '{message_id}'"
    for _ in range(200):
        if database_text(stack, query) == "complete":
            break
        owner.wait_for_timeout(100)
    send_message(owner, "encore")
    expect(replies).to_have_count(before + 2, timeout=T)
    expect(replies.last).not_to_have_attribute("aria-busy", "true", timeout=T)
    expect(reply_text(replies.last)).to_have_text("Fake reply: encore")
    expect(reply_text(first)).to_have_text(f"Fake reply: {SLOW_WORDS}", timeout=T)
    expect(first).not_to_have_attribute("data-writing", "true")


def test_a_reply_whose_stream_stops_short_shows_domo_writing_then_the_reply(
    household: Household,
):
    """A reply's stream that stops before its end, closed for good (the browser's EventSource is
    CLOSED: the server answered 204 when it came back, Domo writing the reply on the server): the
    page does not open it again, the bubble shows Domo writing, and the reply as stored takes
    its place once it is over (review of Task 9, Minor 3). The stop is the event the page gets
    then (``htmx:sseError`` with a closed source), sent to the bubble while the fake model
    answers slowly."""
    owner, stack = household.owner, household.stack
    owner.goto(stack.base_url + "/chat")
    replies = assistant_replies(owner)
    before = replies.count()
    send_message(owner, SLOW_PREFIX + SLOW_WORDS)
    expect(replies).to_have_count(before + 1, timeout=T)
    message_id = replies.last.get_attribute("data-message-id")
    assert message_id
    bubble = owner.locator(f'.message.assistant[data-message-id="{message_id}"]')
    expect(reply_text(bubble)).to_contain_text("Fake", timeout=T)  # Domo has begun
    bubble.evaluate(
        """bubble => bubble.dispatchEvent(new CustomEvent("htmx:sseError", {
            bubbles: true, detail: {source: {readyState: 2}}
        }))"""
    )
    expect(bubble).to_have_attribute("data-writing", "true")
    assert bubble.evaluate("bubble => bubble.hasAttribute('sse-connect')") is False
    # The page asks for what is new: once the reply is over, the stored one takes the place of
    # the bubble that showed Domo writing.
    expect(bubble).not_to_have_attribute("data-writing", "true", timeout=T)
    expect(reply_text(bubble)).to_have_text(f"Fake reply: {SLOW_WORDS}")
    expect(bubble).to_have_count(1)


def test_the_owner_invites_alex_who_joins_from_another_browser(household: Household):
    owner, stack = household.owner, household.stack
    invite = {"display_name": ALEX.display_name, "role": ALEX.role}
    reply = send_and_wait(owner, tool_message("household_invite", invite))
    # Domo was told that nothing has happened yet: the judge asks a yes, and the card says so.
    expect(reply_text(reply)).to_have_text("Fake tool result: waiting_for_confirmation")
    card = confirmation_card(reply)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    # Its head was written for the owner in his turn, the judge's reason put in by the code.
    head = card.locator(".confirmation-head")
    expect(head).to_contain_text(JUDGE_ASKS_A_YES)
    expect(head).to_contain_text(t("confirmation.hint.natural", "en"))
    expect(card.locator(".confirmation-asked")).to_have_count(0)
    expect(card.locator("a.action-link")).to_have_count(0)

    # The button (an explicit yes, more than is asked) wakes Domo, who answers in the open
    # page: his reply comes with no reload.
    confirm_and_hear_back(owner, card)
    link = invitation_link(card, stack)

    # The link is shown once: the card that the page shows again has none.
    owner.reload()
    expect(card).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    expect(card.locator("a.action-link")).to_have_count(0)

    alex = household.join(ALEX, link)
    expect(alex).to_have_url(re.compile(r"/chat$"))
    # The link works once.
    stranger = household.new_page()
    stranger.goto(link)
    expect(stranger.locator("h1")).to_have_text(t("invite.unavailable.title", "en"))


def test_alex_leaves_a_note_that_the_owner_finds(household: Household):
    alex, owner = household.page_of(ALEX), household.owner
    note = {"recipient": OWNER_DISPLAY_NAME, "text": "Bonjour"}
    reply = send_and_wait(alex, judged("do", tool_message("note_leave", note)))
    # The judge asks no yes of a note: it is left at once, and the card says for whom.
    expect(reply_text(reply)).to_have_text("Fake tool result: done")
    card = reply.locator(".action-card")
    expect(card).to_have_attribute("data-action-status", "done", timeout=T)
    expect(card.locator(".action-text")).to_have_text(
        t("action.card.done.note.leave", "en", name=OWNER_DISPLAY_NAME)
    )
    expect(reply.locator(".confirmation-card")).to_have_count(0)

    # No zone of notes on the owner's page: his Domo has the note, as data, to tell him.
    owner.goto(household.stack.base_url + "/chat")
    expect(owner.locator("#approvals")).to_have_count(1)
    expect(owner.locator("#notes")).to_have_count(0)
    note_id = note_id_of(household.stack, "Bonjour")
    # Asked for its exact words, he shows them on a card, put in by the code.
    shown = send_and_wait(owner, tool_message("note_show", {"note": note_id}))
    expect(reply_text(shown)).to_have_text("Fake tool result: done")
    expect(shown.locator(".action-text")).to_have_text(
        filled("action.card.done.note.show.noted", "en", name=ALEX.display_name, words="Bonjour"),
        timeout=T,
    )
    # Then he says that he told it: Alex may learn that it was passed on.
    told = send_and_wait(owner, tool_message("note_told", {"note": note_id}))
    expect(told.locator(".action-text")).to_have_text(
        t("action.card.done.note.told", "en", name=ALEX.display_name), timeout=T
    )
    told_at = f"SELECT told_at IS NOT NULL FROM notes WHERE id = '{note_id}'"
    assert database_text(household.stack, told_at) == "t"


def test_a_childs_note_reaches_the_owner_with_no_adult_in_between(household: Household):
    owner, stack = household.owner, household.stack
    invite = {"display_name": SAM.display_name, "role": SAM.role}
    card = confirmation_card(send_and_wait(owner, tool_message("household_invite", invite)))
    confirm_and_hear_back(owner, card)
    sam = household.join(SAM, invitation_link(card, stack))

    note = {"recipient": OWNER_DISPLAY_NAME, "text": "Coucou"}
    reply = send_and_wait(sam, judged("do", tool_message("note_leave", note)))
    # Not even an adult's yes (a note is not among what a child asks an adult for): the note is
    # left at once.
    expect(reply.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    expect(reply.locator(".confirmation-card")).to_have_count(0)

    assert notes_to(stack, OWNER_DISPLAY_NAME, "Coucou") == "1"
    shown = send_and_wait(owner, tool_message("note_show", {"note": note_id_of(stack, "Coucou")}))
    expect(shown.locator(".action-text")).to_have_text(
        filled("action.card.done.note.show.noted", "en", name=SAM.display_name, words="Coucou"),
        timeout=T,
    )


def test_alex_cannot_make_sam_an_adult(household: Household):
    alex, owner, stack = household.page_of(ALEX), household.owner, household.stack
    change = {"person": SAM.display_name, "role": "adult"}
    reply = send_and_wait(alex, tool_message("household_change_role", change))
    # Domo does not even offer this tool to an adult; the fake model calls it all the same, as
    # a confused or manipulated model could, and the barrier refuses it.
    expect(reply.locator(".action-card")).to_have_attribute("data-action-status", "refused")
    expect(reply_text(reply)).to_have_text("Fake tool result: refused")
    expect(reply.locator("[data-action=confirm]")).to_have_count(0)

    owner.goto(stack.base_url + "/household")
    sam = owner.locator(".household-person").filter(
        has=owner.locator(".household-name", has_text=re.compile(rf"^{SAM.display_name}$"))
    )
    expect(sam).to_have_attribute("data-person-role", "child")
    owner.goto(stack.base_url + "/chat")


def test_alex_sees_nothing_of_the_owners_conversation(household: Household):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    private = "A word for the owner only"
    reply = send_and_wait(owner, private)
    expect(reply_text(reply)).to_have_text(f"Fake reply: {private}")
    reply_id = reply.get_attribute("data-message-id")
    assert reply_id

    alex.reload()
    owners, alexs = message_ids(owner), message_ids(alex)
    assert owners and alexs and owners.isdisjoint(alexs)
    seen_by_alex = alex.locator("#messages").inner_text()
    assert private not in seen_by_alex and "household_invite" not in seen_by_alex

    # One of the owner's replies, asked for with Alex's session: as if it did not exist...
    stream = f"{stack.base_url}/chat/stream/{reply_id}"
    assert alex.request.get(stream).status == 404
    # ...while it does exist: for the owner it is a reply that is over.
    assert owner.request.get(stream).status == 204


def test_alex_gives_domo_a_name_that_only_his_own_page_shows(household: Household):
    alex, owner, stack = household.page_of(ALEX), household.owner, household.stack
    reply = send_and_wait(alex, tool_message("domo_name", {"name": "Brenda"}))
    # Never judged, never shown: done at once, with no card.
    expect(reply_text(reply)).to_have_text("Fake tool result: done")
    expect(reply.locator(".action-card")).to_have_count(0)
    # Alex's page writes it where it wrote "Domo"; the owner's does not.
    alex.reload()
    expect(alex.locator("a.brand")).to_have_text("Brenda")
    expect(alex).to_have_title(re.compile(r" - Brenda$"))
    owner.goto(stack.base_url + "/chat")
    expect(owner.locator("a.brand")).to_have_text("Domo")
    expect(owner).to_have_title(re.compile(r" - Domo$"))
    # Back to Domo, for the scenarios that follow.
    send_and_wait(alex, tool_message("domo_name", {"name": None}))
    alex.reload()
    expect(alex.locator("a.brand")).to_have_text("Domo")


def test_alex_signs_in_with_a_passkey(
    household: Household, browser_trusting_the_stack: Browser, browser_context_args
):
    stack = household.stack
    # No certificate error on the page, as on a device of the household that trusts the stack's
    # authority (Chrome refuses passkeys otherwise): the browser ignores the errors of the chains
    # that hold the authority's keys, and checks every other certificate as usual.
    context = browser_trusting_the_stack.new_context(
        **{**browser_context_args, "ignore_https_errors": False}
    )
    try:
        page = context.new_page()
        log_in(page, stack, username=ALEX.username, password=ALEX.password)
        authenticator = VirtualAuthenticator(page)

        page.goto(stack.base_url + "/account")
        page.fill("#passkey-label", "laptop")
        page.fill("#passkey-current", ALEX.password)
        page.click("[data-action=passkey-register]")
        page.wait_for_url(re.compile(r"/account\?saved=passkey$"), timeout=T)
        expect(page.locator(".passkey .passkey-label")).to_have_text("laptop")
        assert len(authenticator.credentials()) == 1

        page.click("form.logout button[type=submit]")
        page.wait_for_url("**/login", timeout=T)
        page.click("[data-action=passkey-login]")
        page.wait_for_url("**/chat", timeout=T)

        # It is Alex who is signed in, with the passkey just added.
        page.goto(stack.base_url + "/account")
        expect(page.locator("[data-account-username]")).to_have_attribute(
            "data-account-username", ALEX.username
        )
        expect(page.locator(".passkey .passkey-meta")).not_to_contain_text(
            t("account.passkeys.never_used", "en")
        )
    finally:
        context.close()


def test_alex_proves_with_a_passkey_in_the_chat(
    household: Household, browser_trusting_the_stack: Browser, browser_context_args
):
    """The judge asks Alex to prove it is him before he renames himself (spec 4.5, scenario 5):
    the card offers his passkey, and the page's script (``static/passkeys.js``) takes the card's
    form before htmx does, lets the authenticator answer and has htmx post the answer. A browser
    that trusts the stack's authority, as for the sign-in with a passkey; its authenticator is a
    new one, so Alex adds a passkey to it first."""
    stack = household.stack
    context = browser_trusting_the_stack.new_context(
        **{**browser_context_args, "ignore_https_errors": False}
    )
    try:
        page = context.new_page()
        log_in(page, stack, username=ALEX.username, password=ALEX.password)
        authenticator = VirtualAuthenticator(page)
        page.goto(stack.base_url + "/account")
        page.fill("#passkey-label", "phone")
        page.fill("#passkey-current", ALEX.password)
        page.click("[data-action=passkey-register]")
        page.wait_for_url(re.compile(r"/account\?saved=passkey$"), timeout=T)
        assert len(authenticator.credentials()) == 1
        page.goto(stack.base_url + "/chat")

        renamed = {"name": "Alex B"}
        asked = send_and_wait(page, judged("proof: x", tool_message("profile_rename", renamed)))
        expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
        card = confirmation_card(asked)
        expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
        expect(card).to_contain_text(t("confirmation.hint.proof", "en"))
        expect(card.locator("[data-action=confirm]")).to_have_count(0)
        expect(card.locator("[data-action=passkey-prove]")).to_be_visible()

        # The passkey proves it: the name changes, and Domo, woken, says so in the open page.
        before = assistant_replies(page).count()
        prove_with_the_passkey(card)
        expect(card).to_have_attribute("data-confirmation-status", "executed", timeout=T)
        heard = wait_for_a_new_reply(page, before)
        expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)
        name = (
            "SELECT p.display_name FROM persons p JOIN accounts a ON a.person_id = p.id "
            f"WHERE a.username = '{ALEX.username}'"
        )
        assert database_text(stack, name) == "Alex B"

        # His name back, which the judge lets through at once, for the scenarios that follow.
        back = {"name": ALEX.display_name}
        again = send_and_wait(page, judged("do", tool_message("profile_rename", back)))
        expect(reply_text(again)).to_have_text("Fake tool result: done")
        assert database_text(stack, name) == ALEX.display_name
    finally:
        context.close()


def test_the_owner_enters_the_model_key_again_from_the_chat(household: Household):
    owner, stack = household.owner, household.stack
    reply = send_and_wait(owner, tool_message("model_rekey", {}))
    card = confirmation_card(reply)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    # The judge asks a yes; the button gives it.
    expect(card).to_contain_text(JUDGE_ASKS_A_YES)

    confirm_and_hear_back(owner, card)
    expect(card.locator("iframe.vault-frame")).to_be_visible()
    # The key goes into the vault's own form, in its frame: never through the core.
    enter_vault_key(owner, stack.test_key)
    result = card.locator(".key-result")
    expect(result).to_have_attribute("data-key-status", "checked", timeout=T)
    expect(result).to_have_text(t("chat.key.checked", "en"))
    assert stack.test_key not in stack.logs("core")

    after = send_and_wait(owner, "hello again")
    expect(reply_text(after)).to_have_text("Fake reply: hello again")


def test_the_judge_lets_an_invitation_through_or_asks_a_yes(household: Household):
    """Scenarios 1 and 2 of the spec: the judge says "do" of the owner's invitation of a guest,
    and it is done at once, with no card; it says "yes" of another, and a yes in words does."""
    owner, stack = household.owner, household.stack

    # "Do": no request, the invitation is made, and the reply's card shows the link once.
    paul = {"display_name": "Paul", "role": "guest", "guest_days": 2}
    done = send_and_wait(owner, judged("do", tool_message("household_invite", paul)))
    expect(reply_text(done)).to_have_text("Fake tool result: done")
    expect(done.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    expect(confirmation_card(done)).to_have_count(0)
    invitation_link(done, stack)

    # "Yes" (what the fake judge says unless told otherwise): a card, "oui", done, and the
    # answer shows the link.
    dana = {"display_name": "Dana", "role": "adult"}
    asked = send_and_wait(owner, tool_message("household_invite", dana))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    card = confirmation_card(asked)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(card).to_contain_text(JUDGE_ASKS_A_YES)
    expect(card).to_contain_text(t("confirmation.hint.natural", "en"))
    expect(card.locator("a.action-link")).to_have_count(0)
    yes = send_and_wait(owner, "oui")
    expect(reply_text(yes)).to_have_text(DECIDED)
    answered = confirmation_card(yes)
    expect(answered).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    invitation_link(answered, stack)


def test_the_judge_asks_for_a_proof_and_a_wrong_password_does_nothing(household: Household):
    """Scenario 5 of the spec: the judge asks a proof that it is the owner. The card offers his
    password (he has no passkey), in a form of its own, never in the conversation: a wrong one
    does nothing, the right one runs the invitation, and Domo, woken by the proof, says so."""
    owner, stack = household.owner, household.stack
    nico = {"display_name": "Nico", "role": "guest", "guest_days": 2}
    proof = judged("proof: never at this hour", tool_message("household_invite", nico))
    asked = send_and_wait(owner, proof)
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    card = confirmation_card(asked)
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(card).to_contain_text(
        t("confirmation.asked.domo.proof", "en", reason="never at this hour")
    )
    expect(card).to_contain_text(t("confirmation.hint.proof", "en"))
    expect(card.locator(".confirmation-proof")).to_be_visible()
    expect(card.locator("[data-action=confirm]")).to_have_count(0)
    expect(card.locator("[data-action=passkey-prove]")).to_have_count(0)

    # A wrong password: nothing is done, and the card says so, the proof still offered.
    prove_with_the_password(card, "wrong-password-0000")
    expect(card).to_have_attribute("data-outcome", "proof_failed", timeout=T)
    expect(card).to_have_attribute("data-confirmation-status", "pending")
    expect(card.locator(".confirmation-note")).to_have_text(t("confirmation.proof.failed", "en"))
    expect(card.locator("a.action-link")).to_have_count(0)
    assert open_invitations(stack, "Nico", "guest") == "0"

    # The right password: the invitation is made, the card that answers shows its link once.
    before = assistant_replies(owner).count()
    prove_with_the_password(card, OWNER_PASSWORD)
    expect(card).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    invitation_link(card, stack)
    heard = wait_for_a_new_reply(owner, before)
    expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)
    assert open_invitations(stack, "Nico", "guest") == "1"


def test_domo_says_why_it_will_not_and_asks_again_once_convinced(household: Household):
    """Scenario 6 of the spec: the judge does not let the owner's note to Alex through, and Domo
    is told why (the card says it too). The owner argues; Domo, convinced, asks again, and the
    judge, who sees the new words, lets it through."""
    owner, stack = household.owner, household.stack
    note = {"recipient": ALEX.display_name, "text": TEASING_NOTE}
    refused = send_and_wait(
        owner, judged("do_not: that sounds unkind", tool_message("note_leave", note))
    )
    expect(reply_text(refused)).to_have_text("Fake tool result: refused")
    card = refused.locator(".action-card")
    expect(card).to_have_attribute("data-action-status", "refused", timeout=T)
    expect(card.locator(".action-text")).to_have_text(
        t("action.card.refused.judge", "en", reason="that sounds unkind")
    )
    expect(confirmation_card(refused)).to_have_count(0)
    assert notes_to(stack, ALEX.display_name, TEASING_NOTE) == "0"

    done = send_and_wait(owner, judged("do: a joke between them", tool_message("note_leave", note)))
    expect(reply_text(done)).to_have_text("Fake tool result: done")
    expect(done.locator(".action-card")).to_have_attribute("data-action-status", "done", timeout=T)
    assert notes_to(stack, ALEX.display_name, TEASING_NOTE) == "1"


def test_alex_takes_back_a_note_the_owner_was_not_told(household: Household):
    alex, owner = household.page_of(ALEX), household.owner
    note = {"recipient": OWNER_DISPLAY_NAME, "text": NOTE_TO_TAKE_BACK}
    left = send_and_wait(alex, judged("do", tool_message("note_leave", note)))
    expect(reply_text(left)).to_have_text("Fake tool result: done")

    # The note waits for the owner's Domo to tell it.
    note_id = note_id_of(household.stack, NOTE_TO_TAKE_BACK)
    told_at = f"SELECT told_at IS NULL FROM notes WHERE id = '{note_id}'"
    assert database_text(household.stack, told_at) == "t"

    # Alex asks what he left (Domo finds the note's id with this; a reading, which the judge is
    # not asked about), then takes it back, which the judge lets through.
    listed = send_and_wait(alex, tool_message("note_list_sent", {}))
    expect(reply_text(listed)).to_have_text("Fake tool result: done")
    expect(listed.locator(".action-text")).to_have_text(t("action.card.done.note.list_sent", "en"))
    withdraw = tool_message("note_withdraw", {"note": note_id})
    withdrawn = send_and_wait(alex, judged("do", withdraw))
    expect(reply_text(withdrawn)).to_have_text("Fake tool result: done")
    card = withdrawn.locator(".action-card")
    expect(card).to_have_attribute("data-action-status", "done", timeout=T)
    expect(card.locator(".action-text")).to_have_text(
        t("action.card.done.note.withdraw", "en", name=OWNER_DISPLAY_NAME)
    )
    expect(withdrawn.locator(".confirmation-card")).to_have_count(0)

    # The owner's Domo can no longer show it (the note of scenario 2 he still can).
    gone = send_and_wait(owner, tool_message("note_show", {"note": note_id}))
    expect(reply_text(gone)).to_have_text("Fake tool result: invalid")
    expect(gone.locator(".action-card")).not_to_contain_text(NOTE_TO_TAKE_BACK)
    bonjour = note_id_of(household.stack, "Bonjour")
    still = send_and_wait(owner, tool_message("note_show", {"note": bonjour}))
    expect(reply_text(still)).to_have_text("Fake tool result: done")

    # It cannot be taken back twice.
    again = send_and_wait(alex, judged("do", withdraw))
    expect(reply_text(again)).to_have_text("Fake tool result: invalid")
    expect(again.locator(".action-card")).to_have_attribute("data-action-status", "invalid")


def alex_may_invite_guests_for_three_days(alex_id: str) -> dict[str, Any]:
    """The draft the fake rights writer makes of ``RIGHTS_WORDS``: a right of Alex himself for
    ``household.invite``, with a limit (a guest, 3 days at most) and the owner's yes on every
    invitation of his. The code checks it as it checks a real writer's: three examples, one that
    passes and two that are stopped, the last of them a guest for 40 days, which the household's
    own rule (30 days at most) already refuses, because the limit replaces that rule and the owner
    must see what becomes of it. The outcome of each is the one the code works out."""

    def invite(role: str, days: int | None, name: str, expected: str) -> dict[str, Any]:
        return {
            "person_id": alex_id,
            "action_type": "household.invite",
            "facts": {"role": role, "days": days, "display_name": name, "owner_first": False},
            "expected": expected,
        }

    return {
        "changes": [
            {
                "person_id": alex_id,
                "action_type": "household.invite",
                "access": "allow",
                "approver": "owner",
                "limit_expression": THREE_DAYS,
                "summary": RIGHTS_WORDS,
            }
        ],
        "examples": [
            invite("guest", 2, "Jo", "asks_the_owner"),
            invite("adult", None, "Kim", "refused"),
            invite("guest", 40, "Lou", "refused"),
        ],
    }


def latest_draft_id(stack: StackInfo) -> str:
    """The id of the draft of rights the writer wrote last, not applied yet. It is for Domo (the
    fake model takes it from nowhere else): the test reads it in the database."""
    draft_id = database_text(
        stack,
        "SELECT id FROM policy_rights_drafts WHERE applied_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
    )
    assert str(uuid.UUID(draft_id)) == draft_id
    return draft_id


def propose_rights(page: Page, stack: StackInfo, words: str, draft: dict[str, Any]) -> str:
    """The owner, in ``page``, says ``words``: Domo has the rights written (the fake writer
    makes ``draft`` of them), which changes nothing yet and asks no judge. The draft's id."""
    proposed = send_and_wait(
        page, tool_message("rights_propose", {"request": draft_in_words(words, draft)})
    )
    expect(reply_text(proposed)).to_have_text("Fake tool result: done")
    return latest_draft_id(stack)


def test_the_owner_lets_alex_invite_guests_for_three_days_at_most(household: Household):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    alex_id = person_id_of(owner, stack, ALEX)
    done = DECIDED

    # The owner says it. Domo has the rights written (a call of its own to the model, which the
    # fake answers with this draft) and the code checks them on the examples. Nothing changes yet
    # (until then Alex invites as any adult does, since 0.9.1).
    words = draft_in_words(RIGHTS_WORDS, alex_may_invite_guests_for_three_days(alex_id))
    proposed = send_and_wait(owner, tool_message("rights_propose", {"request": words}))
    expect(reply_text(proposed)).to_have_text("Fake tool result: done")
    expect(proposed.locator(".action-card")).to_have_attribute(
        "data-action-status", "done", timeout=T
    )
    expect(proposed.locator(".action-text")).to_have_text(
        t("action.card.done.rights.propose", "en")
    )
    invitation = {"display_name": "Jo", "role": "guest", "guest_days": 2}

    draft_id = latest_draft_id(stack)

    # The card of the change asks for the owner's yes, which the judge asks here (applying rights
    # asks nothing by default), and shows the right and the examples, each with what happens to
    # it.
    apply = confirmation_card(
        send_and_wait(owner, tool_message("rights_apply", {"draft": draft_id}))
    )
    expect(apply).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(apply).to_contain_text(JUDGE_ASKS_A_YES)
    expect(apply).to_contain_text(t("confirmation.hint.natural", "en"))
    expect(apply.locator(".confirmation-rights li")).to_have_text([RIGHTS_WORDS])
    examples = apply.locator(".confirmation-examples li")
    expect(examples).to_have_count(3)
    expect(examples).to_contain_text(["Jo", "Kim", "Lou"])
    expect(examples.locator("strong")).to_have_text(
        [
            t("rights.example.outcome.asks_the_owner", "en"),
            t("rights.example.outcome.refused", "en"),
            t("rights.example.outcome.refused", "en"),
        ]
    )
    confirmed = send_and_wait(owner, "oui")
    expect(reply_text(confirmed)).to_have_text(done)
    expect(confirmation_card(confirmed)).to_have_attribute(
        "data-confirmation-status", "executed", timeout=T
    )

    # Alex invites a guest for 2 days: it waits for the owner's yes, not his own.
    asked = send_and_wait(alex, tool_message("household_invite", invitation))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    request = confirmation_card(asked)
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(request.locator(".action-text")).to_have_text(t("action.card.waiting.owner", "en"))
    expect(request.locator("[data-action=confirm]")).to_have_count(0)

    # It is in the owner's window "To approve", with the name of who asks. The owner says yes.
    owner.reload()
    waiting = owner.locator("#approvals .confirmation-card")
    expect(waiting).to_have_count(1, timeout=T)
    expect(owner.locator("#approvals .approval-from")).to_have_text(
        t("approvals.from", "en", name=ALEX.display_name)
    )
    before = assistant_replies(alex).count()
    confirm_with_the_button(waiting)

    # Domo is woken in Alex's conversation, though it is the owner who clicked, and Alex's page
    # (open all this time) gets his reply by itself.
    heard = wait_for_a_new_reply(alex, before)
    expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)

    # Then it is Alex who has the link: a button on his card, since someone else said yes. It
    # shows the link once; the link works.
    alex.reload()
    expect(request).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    expect(request.locator("a.action-link")).to_have_count(0)
    show = request.locator("[data-action=show-link]")
    expect(show).to_have_text(t("invitation.link.show", "en"))
    show.click()
    link = invitation_link(request, stack)
    guest = household.new_page()
    guest.goto(link)
    expect(guest.locator("h1")).to_have_text(t("invite.title", "en"))

    # A second time, the link is not shown: it was.
    alex.reload()
    request.locator("[data-action=show-link]").click()
    expect(request.locator("[data-invitation-link=gone]")).to_have_text(
        t("invitation.link.gone", "en")
    )
    expect(request.locator("a.action-link")).to_have_count(0)

    # An adult, or a guest for longer than 3 days: the limit refuses, and says so on the card.
    for refused_invitation in (
        {"display_name": "Kim", "role": "adult"},
        {"display_name": "Lou", "role": "guest", "guest_days": 5},
    ):
        refused = send_and_wait(alex, tool_message("household_invite", refused_invitation))
        expect(reply_text(refused)).to_have_text("Fake tool result: refused")
        card = refused.locator(".action-card")
        expect(card).to_have_attribute("data-action-status", "refused", timeout=T)
        expect(card.locator(".action-text")).to_have_text(t("action.card.refused.limit", "en"))
        expect(refused.locator("[data-action=confirm]")).to_have_count(0)


def test_the_owner_removes_alex_and_brings_him_back(household: Household):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    alex.reload()
    kept = message_ids(alex)
    assert kept

    # Removed: his session ends at once, and the household page says the account is closed.
    removal = confirmation_card(
        send_and_wait(owner, tool_message("household_remove", {"person": ALEX.display_name}))
    )
    confirm_and_hear_back(owner, removal)
    alex.goto(stack.base_url + "/chat")
    expect(alex).to_have_url(re.compile(r"/login$"))
    owner.goto(stack.base_url + "/household")
    person = owner.locator(".household-person").filter(
        has=owner.locator(".household-name", has_text=re.compile(rf"^{ALEX.display_name}$"))
    )
    expect(person).to_have_attribute("data-person-state", "removed")
    owner.goto(stack.base_url + "/chat")

    # The owner brings him back: a link, which the owner is shown once. The card also tells the
    # owner the right they gave him, which is paused when he comes back.
    back = {"person": ALEX.display_name, "role": ALEX.role}
    card = confirmation_card(send_and_wait(owner, tool_message("household_bring_back", back)))
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    confirm_and_hear_back(owner, card)
    link = invitation_link(card, stack)
    expect(card.locator(".action-rights li")).to_have_text([RIGHTS_WORDS])

    # The page of the link welcomes him back, with the username he has always had; only a new
    # password is asked.
    page = household.new_page()
    page.goto(link)
    expect(page.locator("h1")).to_have_text(t("invite.return.title", "en", name=ALEX.display_name))
    expect(page.locator(".invite-username")).to_have_text(
        t("invite.return.username", "en", username=ALEX.username)
    )
    expect(page.locator("[name=username]")).to_have_count(0)
    become_the_member_again(page, ALEX_NEW_PASSWORD)
    household.come_back(ALEX, page)

    # His conversation is there, and the household page has him again.
    assert kept <= message_ids(page)
    owner.goto(stack.base_url + "/household")
    expect(person).to_have_attribute("data-person-state", "active")
    owner.goto(stack.base_url + "/chat")

    # The right the owner gave him is paused: it counts again only when they give it back. Until
    # then he invites as any adult does (0.9.1): the yes asked is his own, not the owner's.
    invitation = {"display_name": "Mia", "role": "guest", "guest_days": 2}
    paused = send_and_wait(page, tool_message("household_invite", invitation))
    expect(reply_text(paused)).to_have_text("Fake tool result: waiting_for_confirmation")
    request = confirmation_card(paused)
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(request.locator("[data-action=confirm]")).to_have_count(1)
    refuse_with_the_button(request)


def test_the_owner_asks_for_the_button_before_removing_anyone(household: Household):
    """Scenarios 4 and 16 of the spec: "always ask me for the button before removing anyone" is a
    rule of the owner, for everyone (a right that says what to ask, and nothing else), applied
    with his yes. The code holds it whatever the judge says: a removal the judge lets through
    still takes the button or "I confirm". (It holds for the rest of the journey: the removals
    after this one are confirmed with the button, or are steps of a chain that takes "I confirm"
    anyway.)"""
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    owner_id = person_id_of(owner, stack, OWNER)
    alex_id = person_id_of(owner, stack, ALEX)
    removing_alex = {"person": f"person:{alex_id}", "current_role": "adult", "is_self": False}
    rule = {
        "changes": [
            {"action_type": "household.remove", "ask": "explicit_yes", "summary": REMOVE_RULE_WORDS}
        ],
        "examples": [
            {
                "person_id": owner_id,
                "action_type": "household.remove",
                "facts": removing_alex,
                "expected": "asks_them",
            }
        ],
    }
    draft_id = propose_rights(owner, stack, REMOVE_RULE_WORDS, rule)
    apply = confirmation_card(
        send_and_wait(owner, tool_message("rights_apply", {"draft": draft_id}))
    )
    expect(apply).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(apply.locator(".confirmation-rights li")).to_have_text([REMOVE_RULE_WORDS])
    expect(apply.locator(".confirmation-examples li strong")).to_have_text(
        [t("rights.example.outcome.asks_them", "en")]
    )
    applied = send_and_wait(owner, "je confirme")
    expect(reply_text(applied)).to_have_text(DECIDED)
    expect(confirmation_card(applied)).to_have_attribute(
        "data-confirmation-status", "executed", timeout=T
    )

    # The judge lets the removal of Alex through; the owner's rule asks for the button or "I
    # confirm", and the card says it is his rule. A plain "oui" leaves it waiting.
    removing = judged("do", tool_message("household_remove", {"person": ALEX.display_name}))
    asked = send_and_wait(owner, removing)
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    removal = confirmation_card(asked)
    expect(removal).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(removal).to_contain_text(t("confirmation.asked.rule.explicit_yes.yours", "en"))
    expect(removal).to_contain_text(t("confirmation.hint.explicit", "en"))
    plain = send_and_wait(owner, "oui")
    expect(reply_text(plain)).to_have_text("Fake reply: oui")  # Domo answers; nothing decided
    owner.reload()
    expect(removal).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    alex.goto(stack.base_url + "/chat")
    expect(alex).to_have_url(re.compile(r"/chat$"))  # still in the household

    # "Je confirme": Alex is removed; his session ends.
    confirmed = send_and_wait(owner, "je confirme")
    expect(reply_text(confirmed)).to_have_text(DECIDED)
    alex.goto(stack.base_url + "/chat")
    expect(alex).to_have_url(re.compile(r"/login$"))

    # The owner brings him back for the scenarios that follow (no rule on that: a yes, the
    # button), and Alex comes back with a new password.
    back = {"person": ALEX.display_name, "role": ALEX.role}
    card = confirmation_card(send_and_wait(owner, tool_message("household_bring_back", back)))
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    confirm_and_hear_back(owner, card)
    page = household.new_page()
    page.goto(invitation_link(card, stack))
    become_the_member_again(page, ALEX_THIRD_PASSWORD)
    household.come_back(ALEX, page)


def model_calls(stack: StackInfo) -> int:
    """How many calls the fake model service has answered so far (its log has a line for each)."""
    return stack.logs("fake-llm").count("POST /v1/chat/completions -> 200")


def test_a_button_wakes_domo_who_stays_silent_after_a_no_and_answers_after_a_yes(
    household: Household,
):
    owner = household.owner
    eve = {"display_name": "Eve", "role": "adult"}

    # A no, by the button: Domo is woken and has nothing to add; nothing appears, then or later.
    card = confirmation_card(send_and_wait(owner, tool_message("household_invite", eve)))
    expect(card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    before = assistant_replies(owner).count()
    asked_the_model = model_calls(household.stack)
    refuse_with_the_button(card)
    no_new_reply(owner, before)
    # He was woken all the same (that costs a call to the model), and said nothing.
    assert model_calls(household.stack) > asked_the_model

    # A yes, by the button: a bubble appears in the open page, which is not reloaded.
    card = confirmation_card(send_and_wait(owner, tool_message("household_invite", eve)))
    reply = confirm_and_hear_back(owner, card)
    expect(reply_text(reply)).not_to_have_text(t("confirmation.reply.executed", "en"))

    # A yes said in words: it is Domo's answer that shows, not the sentence made in advance.
    fay = {"display_name": "Fay", "role": "adult"}
    asked = send_and_wait(owner, tool_message("household_invite", fay))
    expect(confirmation_card(asked)).to_have_attribute(
        "data-confirmation-status", "pending", timeout=T
    )
    confirmed = send_and_wait(owner, "je confirme")
    expect(reply_text(confirmed)).to_have_text(DECIDED)
    expect(reply_text(confirmed)).not_to_have_text(t("confirmation.reply.executed", "en"))
    expect(confirmation_card(confirmed)).to_have_attribute(
        "data-confirmation-status", "executed", timeout=T
    )


def test_the_owner_keeps_a_list_of_successors_that_an_adult_cannot_see(household: Household):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    adult = t("household.role.adult", "en")
    paper = t("successors.access.paper_code", "en")
    brother = "mon frère"

    # Justine joins the household, as an adult.
    invitation = {"display_name": JUSTINE.display_name, "role": JUSTINE.role}
    invite = confirmation_card(send_and_wait(owner, tool_message("household_invite", invitation)))
    confirm_and_hear_back(owner, invite)
    household.join(JUSTINE, invitation_link(invite, stack))

    # The list: Justine first, then Paul, who is outside the household and whose door is opened
    # with a code kept on paper (another Paul than the guest invited in scenario 8). The judge
    # asks a firm yes of it here (nothing asks one by default): a plain yes leaves it
    # waiting; the card shows the list before and after; the explicit yes changes it.
    places = [
        {"person": JUSTINE.display_name},
        {"name": "Paul", "relation": brother, "access": "paper_code"},
    ]
    setting = judged(FIRM, tool_message("successors_set", {"successors": places}))
    asked = send_and_wait(owner, setting)
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    request = confirmation_card(asked)
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(request).to_contain_text(JUDGE_ASKS_A_FIRM_YES)
    expect(request.locator(".confirmation-successors-before")).to_have_text(
        t("confirmation.successors.before", "en", list=t("successors.none", "en"))
    )
    after = f"1. {JUSTINE.display_name} ({adult}) ; 2. Paul ({brother}, {paper})"
    expect(request.locator(".confirmation-successors-after")).to_have_text(
        t("confirmation.successors.after", "en", list=after)
    )
    plain = send_and_wait(owner, "oui")
    expect(reply_text(plain)).to_have_text("Fake reply: oui")
    owner.reload()
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    assert database_text(stack, "SELECT count(*) FROM successors") == "0"
    confirmed = send_and_wait(owner, "je confirme")
    expect(reply_text(confirmed)).to_have_text(DECIDED)
    expect(confirmation_card(confirmed)).to_have_attribute(
        "data-confirmation-status", "executed", timeout=T
    )
    in_order = (
        "SELECT string_agg(coalesce(p.display_name, s.name), ',' ORDER BY s.rank) "
        "FROM successors s LEFT JOIN persons p ON p.id = s.person_id"
    )
    assert database_text(stack, in_order) == f"{JUSTINE.display_name},Paul"

    # An adult asks for the list: refused. (Domo does not even offer him the tool; the fake model
    # calls it all the same and the barrier refuses.)
    seen_by_alex = send_and_wait(alex, tool_message("successors_show", {}))
    expect(reply_text(seen_by_alex)).to_have_text("Fake tool result: refused")
    expect(seen_by_alex.locator(".action-card")).to_have_attribute("data-action-status", "refused")

    # The owner says that Justine died: the judge lets it through, it is noted at once, with no
    # yes, and Paul comes first.
    died = send_and_wait(
        owner, judged("do", tool_message("successors_note_death", {"name": JUSTINE.display_name}))
    )
    expect(reply_text(died)).to_have_text("Fake tool result: done")
    note = died.locator(".action-card")
    expect(note).to_have_attribute("data-action-status", "done", timeout=T)
    expect(note.locator(".action-text")).to_have_text(
        t("action.card.done.successors.note_death", "en", name=JUSTINE.display_name)
    )
    expect(died.locator(".confirmation-card")).to_have_count(0)
    deceased = (
        "SELECT s.deceased_at IS NOT NULL FROM successors s "
        f"JOIN persons p ON p.id = s.person_id WHERE p.display_name = '{JUSTINE.display_name}'"
    )
    assert database_text(stack, deceased) == "t"
    noted = "SELECT count(*) FROM audit_log WHERE action = 'successors.death_noted'"
    assert database_text(stack, noted) == "1"

    # Paul comes first. The database cannot say so: the places keep the rank they were given (1 and
    # 2), and the rank among the living is worked out when the list is read. It is read with the
    # same function by the owner's tool, by his context and by the card of a change of the list,
    # which shows the list as it is now ("Before"): there the living are numbered, and Justine is
    # not. (The fake model tells Domo's tool result by its status only, so the card is what a
    # test can read.) The owner asks for the same list again, reads the card, and says no.
    stored_ranks = "SELECT string_agg(rank::text, ',' ORDER BY rank) FROM successors"
    assert database_text(stack, stored_ranks) == "1,2"
    reading = send_and_wait(owner, tool_message("successors_set", {"successors": places}))
    request = confirmation_card(reading)
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    # A day as the page says it, in English (``Nov 10, 2026``).
    day = r"[A-Z][a-z]{2} \d{1,2}, \d{4}"
    justine = re.escape(
        f"{JUSTINE.display_name} ({adult}, {t('successors.deceased', 'en', date='@')})"
    ).replace("@", day)
    paul = re.escape(f"1. Paul ({brother}, {paper})")
    before_line = re.escape(t("confirmation.successors.before", "en", list="@")).replace(
        "@", f"{justine} ; {paul}"
    )
    expect(request.locator(".confirmation-successors-before")).to_have_text(
        re.compile(f"^{before_line}$")
    )
    declined = send_and_wait(owner, "non")
    expect(reply_text(declined)).to_have_text("Fake decided: refused")
    expect(confirmation_card(declined)).to_have_attribute(
        "data-confirmation-status", "refused", timeout=T
    )
    assert database_text(stack, in_order) == f"{JUSTINE.display_name},Paul"


def test_a_chain_stopped_after_a_button_keeps_its_first_step_and_wakes_domo(
    household: Household,
):
    owner, stack = household.owner, household.stack
    steps = [
        {
            "tool": "household_change_role",
            "arguments": {"person": SAM.display_name, "role": "adult"},
        },
        {"tool": "household_cancel_invitation", "arguments": {"display_name": "Dana"}},
    ]
    asked = send_and_wait(owner, tool_message("do_in_order", {"steps": steps}))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    chain = confirmation_card(asked)
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(chain.locator(".confirmation-steps li")).to_have_count(2)

    # Before the button, the second step becomes impossible: the owner cancels Dana's invitation
    # in words (the chain's card is not the one this reply shows, so "oui" answers this one).
    cancel = {"display_name": "Dana"}
    cancelling = send_and_wait(owner, tool_message("household_cancel_invitation", cancel))
    expect(confirmation_card(cancelling)).to_have_attribute(
        "data-confirmation-status", "pending", timeout=T
    )
    cancelled = send_and_wait(owner, "oui")
    expect(reply_text(cancelled)).to_have_text(DECIDED)

    # Then the button of the chain: the first step is done, the second fails, the card says so,
    # and Domo, woken, is told (the fake says what became of it).
    confirm_and_hear_back(owner, chain, said="Fake decided: failed", status="failed")
    states = chain.locator(".confirmation-steps li")
    expect(states.nth(0)).to_have_attribute("data-step-state", "done")
    expect(states.nth(1)).to_have_attribute("data-step-state", "failed")

    owner.goto(stack.base_url + "/household")
    sam = owner.locator(".household-person").filter(
        has=owner.locator(".household-name", has_text=re.compile(rf"^{SAM.display_name}$"))
    )
    expect(sam).to_have_attribute("data-person-role", "adult")
    owner.goto(stack.base_url + "/chat")


def step(tool: str, arguments: dict[str, Any], after_arrival_of: str | None = None) -> JsonStep:
    """A step of ``do_in_order``: a tool and its arguments, and, for a step that waits for someone
    to join, the name of their invitation."""
    made: JsonStep = {"tool": tool, "arguments": arguments}
    if after_arrival_of is not None:
        made["after_arrival_of"] = after_arrival_of
    return made


def request_id(card: Locator) -> str:
    """The id of the request a card shows (Domo has it in his context; the fake model takes it
    from nowhere else, so the test reads it on the card)."""
    found = card.get_attribute("data-confirmation-id")
    assert found, "the card names no request"
    return found


def stay_ends_in(stack: StackInfo, name: str, days: int) -> bool:
    """Whether the stay of the guest ``name`` ends ``days`` days from now (give or take a few
    minutes: the stay was set a moment ago)."""
    query = (
        "SELECT abs(extract(epoch FROM expires_at - (now() + interval '1 day' * "
        f"{days}))) < 300 FROM persons WHERE display_name = '{name}' AND role = 'guest'"
    )
    return database_text(stack, query) == "t"


def open_invitations(stack: StackInfo, name: str, role: str) -> str:
    """How many invitations made for ``name`` as ``role`` still wait for someone to join."""
    return database_text(
        stack,
        "SELECT count(*) FROM invitations WHERE accepted_at IS NULL AND cancelled_at IS NULL "
        f"AND expires_at > now() AND display_name = '{name}' AND role = '{role}'",
    )


def note_id_of(stack: StackInfo, text: str) -> str:
    """The id of the note with the words ``text`` (what the context of its recipient's turns gives
    Domo, read in the database)."""
    return database_text(stack, f"SELECT id FROM notes WHERE text = '{text}'")


def notes_to(stack: StackInfo, name: str, text: str) -> str:
    """How many notes with the words ``text`` the person ``name`` has received (what no page shows
    to anyone, read in the database)."""
    return database_text(
        stack,
        "SELECT count(*) FROM notes n JOIN persons p ON p.id = n.recipient_id "
        f"WHERE p.display_name = '{name}' AND n.text = '{text}'",
    )


def test_the_owner_changes_a_chain_that_waits_for_a_yes(household: Household):
    owner, stack = household.owner, household.stack
    two_days = [
        step(
            "household_change_role", {"person": SAM.display_name, "role": "guest", "guest_days": 2}
        ),
        step("note_leave", {"recipient": ALEX.display_name, "text": NOTE_OF_A_CHANGED_CHAIN}),
    ]
    asked = send_and_wait(owner, tool_message("do_in_order", {"steps": two_days}))
    chain = confirmation_card(asked)
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    old_id = request_id(chain)

    # "Yes, but 5 days": the classifier reads a change, which lets Domo change this request in
    # this turn, and he gives the whole chain back as he understood it.
    five_days = [
        step(
            "household_change_role", {"person": SAM.display_name, "role": "guest", "guest_days": 5}
        ),
        two_days[1],
    ]
    change = {"request_id": old_id, "arguments": {"steps": five_days}, "ask_again": False}
    changed = send_and_wait(owner, tool_message("confirmation_amend", change))
    expect(reply_text(changed)).to_have_text("Fake tool result: done")

    # The judge judges the changed chain, with what changed, and asks a yes of it (the words of
    # the change do not give a yes the judge asks again): the changed chain is a new card, which
    # replaces the old one, and a plain "oui" runs it.
    new_id = database_text(stack, f"SELECT replaced_by FROM confirmations WHERE id = '{old_id}'")
    assert str(uuid.UUID(new_id)) == new_id
    owner.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "replaced", timeout=T)
    again = changed.locator(f'.confirmation-card[data-confirmation-id="{new_id}"]')
    expect(again).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(again.locator(".confirmation-steps li")).to_have_count(2)
    expect(again).to_contain_text(JUDGE_ASKS_A_YES)
    expect(again).to_contain_text(t("confirmation.hint.natural", "en"))

    confirmed = send_and_wait(owner, "oui")
    expect(reply_text(confirmed)).to_have_text(DECIDED)
    assert stay_ends_in(stack, SAM.display_name, 5)
    assert notes_to(stack, ALEX.display_name, NOTE_OF_A_CHANGED_CHAIN) == "1"


def test_a_chain_is_judged_once_and_asks_what_the_judge_asks(household: Household):
    """Scenario 11 of the spec: a chain is one request, one card and one verdict of the judge for
    all its steps, and a step checked in its turn (its person changes with an earlier step) no
    longer asks a yes of its own. A chain that hands the owner's role over asks nothing by default
    (2026-11-15); when the judge asks a firm yes of it, it takes the button or "I confirm"."""
    owner, stack = household.owner, household.stack
    natural, explicit = t("confirmation.hint.natural", "en"), t("confirmation.hint.explicit", "en")

    # Fay's name is taken until her invitation (scenario 13) is cancelled, so inviting her again
    # as a guest is checked in its turn: one card, the judge's yes, and a plain "oui" does it all.
    fay = [
        step("household_cancel_invitation", {"display_name": "Fay"}),
        step("household_invite", {"display_name": "Fay", "role": "guest", "guest_days": 2}),
    ]
    asked = send_and_wait(owner, tool_message("do_in_order", {"steps": fay}))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    chain = confirmation_card(asked)
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(chain.locator(".confirmation-steps li[data-checked-in-turn]")).to_have_count(1)
    expect(chain).to_contain_text(JUDGE_ASKS_A_YES)
    expect(chain).to_contain_text(natural)
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text(DECIDED)
    assert open_invitations(stack, "Fay", "adult") == "0"
    assert open_invitations(stack, "Fay", "guest") == "1"

    # The hand-over of the owner's role: the judge asks a firm yes of the chain, so it takes "I
    # confirm" (a plain "oui" leaves it waiting). It waits for Gil to join; Domo
    # drops it, and Gil's invitation stays.
    gil = [
        step("household_invite", {"display_name": "Gil", "role": "adult"}),
        step("household_transfer_ownership", {"person": "Gil"}, after_arrival_of="Gil"),
    ]
    held = send_and_wait(owner, judged(FIRM, tool_message("do_in_order", {"steps": gil})))
    waiting = confirmation_card(held)
    expect(waiting).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(waiting).to_contain_text(JUDGE_ASKS_A_FIRM_YES)
    expect(waiting).to_contain_text(explicit)
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text("Fake reply: oui")
    assert open_invitations(stack, "Gil", "adult") == "0"
    expect(reply_text(send_and_wait(owner, "je confirme"))).to_have_text("Fake decided: paused")
    assert open_invitations(stack, "Gil", "adult") == "1"
    owner.reload()
    expect(waiting).to_have_attribute("data-confirmation-status", "waiting_arrival", timeout=T)
    expect(waiting.locator(".action-text")).to_have_text(
        t("action.card.waiting_arrival", "en", name="Gil")
    )
    drop = {"request_id": request_id(waiting)}
    expect(
        reply_text(send_and_wait(owner, tool_message("confirmation_expire", drop)))
    ).to_have_text("Fake tool result: done")
    owner.reload()
    expect(waiting).to_have_attribute("data-confirmation-status", "expired", timeout=T)
    assert open_invitations(stack, "Gil", "adult") == "1"
    assert database_text(stack, OWNERS) == OWNER_DISPLAY_NAME


def test_a_chain_waits_for_someone_to_join_and_goes_on_by_itself(household: Household):
    owner, stack = household.owner, household.stack

    # "Invite Hal as an adult, and leave him a welcome once he is here": the judge asks a yes of
    # the chain, "oui", the invitation is made and the chain pauses.
    hal = [
        step("household_invite", {"display_name": HAL.display_name, "role": HAL.role}),
        step(
            "note_leave",
            {"recipient": HAL.display_name, "text": WELCOME},
            after_arrival_of=HAL.display_name,
        ),
    ]
    chain = confirmation_card(send_and_wait(owner, tool_message("do_in_order", {"steps": hal})))
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text("Fake decided: paused")

    # The card of the paused chain keeps the link of the invitation behind its button.
    owner.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "waiting_arrival", timeout=T)
    link = show_link(chain, 1, stack)

    # Hal joins from another browser: at the upkeep's next pass the chain goes on by itself, and
    # Domo, woken, says so in the owner's open page, which is not reloaded.
    before = assistant_replies(owner).count()
    hal_page = household.join(HAL, link)
    heard = wait_for_a_new_reply(owner, before, timeout_ms=RESUME_MS)
    expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)
    # The note the chain left waits for Hal's Domo, who shows him its exact words.
    assert notes_to(stack, HAL.display_name, WELCOME) == "1"
    welcome = send_and_wait(
        hal_page, tool_message("note_show", {"note": note_id_of(stack, WELCOME)})
    )
    expect(welcome.locator(".action-text")).to_have_text(
        filled("action.card.done.note.show.noted", "en", name=OWNER_DISPLAY_NAME, words=WELCOME),
        timeout=T,
    )
    owner.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "executed", timeout=T)

    # Ida's link ends before she joins (the owner cancels her invitation): the chain stops at the
    # wait, and Domo is woken with the step that failed.
    ida = [
        step("household_invite", {"display_name": "Ida", "role": "guest", "guest_days": 2}),
        step("note_leave", {"recipient": "Ida", "text": "x"}, after_arrival_of="Ida"),
    ]
    stopped = confirmation_card(send_and_wait(owner, tool_message("do_in_order", {"steps": ida})))
    expect(stopped).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text("Fake decided: paused")
    cancel = {"display_name": "Ida"}
    cancelling = send_and_wait(owner, tool_message("household_cancel_invitation", cancel))
    expect(confirmation_card(cancelling)).to_have_attribute(
        "data-confirmation-status", "pending", timeout=T
    )
    # The bubble that answers the "oui" comes first, the wake's after it.
    replies = assistant_replies(owner).count()
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text(DECIDED)
    heard = wait_for_a_new_reply(owner, replies + 1, timeout_ms=RESUME_MS)
    expect(reply_text(heard)).to_have_text("Fake decided: failed", timeout=T)
    owner.reload()
    expect(stopped).to_have_attribute("data-confirmation-status", "failed", timeout=T)
    states = stopped.locator(".confirmation-steps li")
    expect(states.nth(0)).to_have_attribute("data-step-state", "done")
    expect(states.nth(1)).to_have_attribute("data-step-state", "failed")


def test_domo_takes_up_the_failed_step_of_a_chain_again_with_a_change(household: Household):
    owner, stack = household.owner, household.stack
    joy = [
        step("household_invite", {"display_name": "Joy", "role": "adult"}),
        step("note_leave", {"recipient": ALEX.display_name, "text": NOTE_OF_A_RESUMED_CHAIN}),
    ]
    chain = confirmation_card(send_and_wait(owner, tool_message("do_in_order", {"steps": joy})))
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    chain_id = request_id(chain)

    # Before the button, Joy is invited in words: her name is taken.
    joy_alone = {"display_name": "Joy", "role": "adult"}
    invited = confirmation_card(send_and_wait(owner, tool_message("household_invite", joy_alone)))
    expect(invited).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(reply_text(send_and_wait(owner, "je confirme"))).to_have_text(DECIDED)

    # The button: the first step fails (the name is taken), and the rest is not done.
    confirm_and_hear_back(owner, chain, said="Fake decided: failed", status="failed")
    expect(chain.locator(".confirmation-steps li").nth(0)).to_have_attribute(
        "data-step-state", "failed"
    )
    assert notes_to(stack, ALEX.display_name, NOTE_OF_A_RESUMED_CHAIN) == "0"

    # "Take it up again with Joy B": the failed step changed, then the rest. The judge, told the
    # chain's history and what changed, asks a yes again: a new card.
    joy_b = [step("household_invite", {"display_name": "Joy B", "role": "adult"}), joy[1]]
    again = {"request_id": chain_id, "steps": joy_b}
    taken_up = send_and_wait(owner, tool_message("chain_continue", again))
    expect(reply_text(taken_up)).to_have_text("Fake tool result: done")
    new_card = confirmation_card(taken_up)
    expect(new_card).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    confirm_and_hear_back(owner, new_card)

    assert open_invitations(stack, "Joy B", "adult") == "1"
    assert notes_to(stack, ALEX.display_name, NOTE_OF_A_RESUMED_CHAIN) == "1"
    owner.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "replaced", timeout=T)


def test_a_chain_with_two_links_leaves_each_behind_its_own_button(household: Household):
    owner, stack = household.owner, household.stack
    # Sam is removed first, with the button that the owner's rule asks for (scenario 12).
    removal = confirmation_card(
        send_and_wait(owner, tool_message("household_remove", {"person": SAM.display_name}))
    )
    confirm_and_hear_back(owner, removal)

    # "Bring Sam back as a child and invite Léa": one card, one yes, two links.
    both = [
        step("household_bring_back", {"person": SAM.display_name, "role": "child"}),
        step("household_invite", {"display_name": LEA, "role": "adult"}),
    ]
    chain = confirmation_card(send_and_wait(owner, tool_message("do_in_order", {"steps": both})))
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    confirm_and_hear_back(owner, chain)

    # Each link waits behind its own button, on the card of the one who asked.
    owner.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    expect(chain.locator('[data-step] [data-action="show-link"]')).to_have_count(2)
    expect(chain.locator('[data-step="1"] [data-action="show-link"]')).to_have_count(1)
    expect(chain.locator('[data-step="2"] [data-action="show-link"]')).to_have_count(1)
    sam_link = show_link(chain, 1, stack)
    lea_link = show_link(chain, 2, stack)
    assert sam_link != lea_link

    # Neither link is stored anywhere: no model ever had one.
    for table in (
        "messages",
        "message_actions",
        "confirmations",
        "confirmation_steps",
        "audit_log",
    ):
        stored = f"SELECT count(*) FROM {table} kept WHERE kept::text LIKE '%/invite/%'"
        assert database_text(stack, stored) == "0", table

    # Sam comes back with his link, as a child.
    page = household.new_page()
    page.goto(sam_link)
    expect(page.locator("h1")).to_have_text(t("invite.return.title", "en", name=SAM.display_name))
    become_the_member_again(page, SAM_NEW_PASSWORD)
    household.come_back(SAM, page)
    role = f"SELECT role FROM persons WHERE display_name = '{SAM.display_name}'"
    assert database_text(stack, role) == "child"


def sam_may_invite_with_an_adults_yes(sam_id: str) -> dict[str, Any]:
    """The draft the fake rights writer makes of ``SAM_RIGHTS_WORDS``: a right of Sam himself for
    ``household.invite``, each invitation of his waiting for an adult's yes, and an example that
    shows it (Sam invites Tom, a guest, for 7 days: it waits for an adult)."""
    return {
        "changes": [
            {
                "person_id": sam_id,
                "action_type": "household.invite",
                "access": "allow",
                "approver": "adult",
                "summary": SAM_RIGHTS_WORDS,
            }
        ],
        "examples": [
            {
                "person_id": sam_id,
                "action_type": "household.invite",
                "facts": {"role": "guest", "days": 7, "display_name": "Tom", "owner_first": False},
                "expected": "asks_an_adult",
            }
        ],
    }


def test_an_adult_says_yes_with_a_change_to_a_childs_request_in_their_own_chat(
    household: Household,
):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    sam = household.page_of(SAM)

    # The owner lets Sam invite, with an adult's yes (applying rights takes his yes).
    sam_id = person_id_of(owner, stack, SAM)
    draft_id = propose_rights(
        owner, stack, SAM_RIGHTS_WORDS, sam_may_invite_with_an_adults_yes(sam_id)
    )
    apply = confirmation_card(
        send_and_wait(owner, tool_message("rights_apply", {"draft": draft_id}))
    )
    expect(apply.locator(".confirmation-rights li")).to_have_text([SAM_RIGHTS_WORDS], timeout=T)
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text(DECIDED)

    # Sam invites Tom for 7 days: the judge lets it through, and it waits for an adult all the
    # same (the right says so).
    tom = {"display_name": "Tom", "role": "guest", "guest_days": 7}
    asked = send_and_wait(sam, judged("do", tool_message("household_invite", tom)))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    request = confirmation_card(asked)
    expect(request.locator(".action-text")).to_have_text(t("action.card.waiting.adult", "en"))
    rid = request_id(request)

    # Alex has Domo show it in his own chat, then says "yes, but 3 days, not more". The judge
    # judges the changed invitation for Sam, with Sam's words (never Alex's), and lets it through
    # again: Alex's change is the adult's yes, so the changed invitation is made at once, and what
    # he said is noted for Sam.
    shown = send_and_wait(alex, tool_message("confirmation_remind", {"request_id": rid}))
    expect(reply_text(shown)).to_have_text("Fake tool result: done")
    before = assistant_replies(sam).count()
    change = {
        "request_id": rid,
        "arguments": {"guest_days": 3},
        "ask_again": False,
        "note": FIRM_NOTE,
    }
    changed = send_and_wait(alex, tool_message("confirmation_amend", change))
    expect(reply_text(changed)).to_have_text("Fake tool result: done")
    status = f"SELECT status FROM confirmations WHERE id = '{rid}'"
    assert database_text(stack, status) == "executed"
    days = "SELECT guest_days FROM invitations WHERE display_name = 'Tom'"
    assert database_text(stack, days) == "3"
    noted = f"SELECT approver_note FROM confirmations WHERE id = '{rid}'"
    assert database_text(stack, noted) == FIRM_NOTE

    # Sam's open page hears from Domo, woken in his conversation. The link is Sam's to show, on
    # his card, and nowhere on Alex's page.
    heard = wait_for_a_new_reply(sam, before)
    expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)
    sam.reload()
    expect(request).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    expect(request.locator("[data-action=show-link]")).to_have_count(1)
    alex.reload()
    expect(changed).to_be_visible(timeout=T)
    on_alex_page = alex.locator(f'[data-confirmation-id="{rid}"] [data-action=show-link]')
    expect(on_alex_page).to_have_count(0)
    expect(changed.locator("[data-action=show-link]")).to_have_count(0)


def test_a_child_drops_their_request_that_waits_for_an_adult(household: Household):
    sam, stack = household.page_of(SAM), household.stack
    uma = {"display_name": "Uma", "role": "guest", "guest_days": 7}
    request = confirmation_card(send_and_wait(sam, tool_message("household_invite", uma)))
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(request.locator(".action-text")).to_have_text(t("action.card.waiting.adult", "en"))

    # "Never mind": Domo sees Sam's request that waits for an adult, and drops it.
    drop = {"request_id": request_id(request)}
    dropped = send_and_wait(sam, tool_message("confirmation_expire", drop))
    expect(reply_text(dropped)).to_have_text("Fake tool result: done")
    sam.reload()
    expect(request).to_have_attribute("data-confirmation-status", "expired", timeout=T)
    uma_invited = "SELECT count(*) FROM invitations WHERE display_name = 'Uma'"
    assert database_text(stack, uma_invited) == "0"


def test_a_stay_a_death_undone_a_note_to_oneself_leaving_and_kept_notes(household: Household):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack

    # Ivy, a guest for 2 days, joins; then she stays 5 days from now after all.
    ivy_invitation = {"display_name": IVY.display_name, "role": IVY.role, "guest_days": 2}
    invite = confirmation_card(
        send_and_wait(owner, tool_message("household_invite", ivy_invitation))
    )
    confirm_and_hear_back(owner, invite)
    ivy = household.join(IVY, invitation_link(invite, stack))
    assert stay_ends_in(stack, IVY.display_name, 2)
    longer = {"person": IVY.display_name, "role": "guest", "guest_days": 5}
    stay = confirmation_card(send_and_wait(owner, tool_message("household_change_role", longer)))
    expect(stay).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    confirm_and_hear_back(owner, stay)
    assert stay_ends_in(stack, IVY.display_name, 5)

    # "Justine is not dead": the judge asks a firm yes before putting her back (nothing asks one
    # by default), so "oui" is not enough.
    deceased = (
        "SELECT s.deceased_at IS NOT NULL FROM successors s "
        f"JOIN persons p ON p.id = s.person_id WHERE p.display_name = '{JUSTINE.display_name}'"
    )
    assert database_text(stack, deceased) == "t"
    undo = {"name": JUSTINE.display_name}
    undoing = confirmation_card(
        send_and_wait(owner, judged(FIRM, tool_message("successors_undo_death", undo)))
    )
    expect(undoing).to_contain_text(t("confirmation.hint.explicit", "en"), timeout=T)
    expect(reply_text(send_and_wait(owner, "oui"))).to_have_text("Fake reply: oui")
    assert database_text(stack, deceased) == "t"
    expect(reply_text(send_and_wait(owner, "je confirme"))).to_have_text(DECIDED)
    assert database_text(stack, deceased) == "f"

    # Alex leaves himself a note, which the judge lets through: his own Domo shows it to him.
    to_himself = {"recipient": ALEX.display_name, "text": NOTE_TO_ONESELF}
    left = send_and_wait(alex, judged("do", tool_message("note_leave", to_himself)))
    expect(reply_text(left)).to_have_text("Fake tool result: done")
    mine = send_and_wait(
        alex, tool_message("note_show", {"note": note_id_of(stack, NOTE_TO_ONESELF)})
    )
    expect(mine.locator(".action-text")).to_have_text(
        filled(
            "action.card.done.note.show.noted", "en", name=ALEX.display_name, words=NOTE_TO_ONESELF
        ),
        timeout=T,
    )

    # The owner leaves Ivy two notes, and keeps one of them a month (all three let through by the
    # judge).
    for text in (KEPT_NOTE, ERASED_NOTE):
        note = {"recipient": IVY.display_name, "text": text}
        left = send_and_wait(owner, judged("do", tool_message("note_leave", note)))
        expect(reply_text(left)).to_have_text("Fake tool result: done")
    kept_id = database_text(stack, f"SELECT id FROM notes WHERE text = '{KEPT_NOTE}'")
    in_a_month = database_text(stack, "SELECT (current_date + 30)::text")
    keep = {"note": kept_id, "until": in_a_month}
    kept = send_and_wait(owner, judged("do", tool_message("note_keep", keep)))
    expect(reply_text(kept)).to_have_text("Fake tool result: done")

    # Ivy leaves the household herself (the judge asks her yes): her page shows Domo's last
    # words, and her next page is the sign-in. Her session ends as she leaves: the next ask of her
    # window "To approve" (every 15 seconds, an htmx request, answered 401 with the way to the
    # sign-in) would take her page there at once, perhaps before those words are read. The window
    # is taken out of her page first, so that nothing asks for her until she opens a page.
    leaving = confirmation_card(send_and_wait(ivy, tool_message("household_leave", {})))
    expect(leaving).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    ivy.locator("#approvals").evaluate("zone => zone.remove()")
    expect(reply_text(send_and_wait(ivy, "oui"))).to_have_text(DECIDED)
    ivy.goto(stack.base_url + "/chat")
    expect(ivy).to_have_url(re.compile(r"/login$"))
    # Two passes of the upkeep later (every 5 seconds here), the note that was not kept is
    # erased, the kept one is still there.
    ivy.wait_for_timeout(10_000)
    assert notes_to(stack, IVY.display_name, ERASED_NOTE) == "0"
    assert notes_to(stack, IVY.display_name, KEPT_NOTE) == "1"

    # Removing someone else stays the owner's: Alex is refused.
    refused = send_and_wait(alex, tool_message("household_remove", {"person": SAM.display_name}))
    expect(reply_text(refused)).to_have_text("Fake tool result: refused")
    expect(refused.locator(".action-card")).to_have_attribute("data-action-status", "refused")


def approvals_zone_asks(page: Page) -> int:
    """How many times the approvals zone of ``page`` came to ask for itself again since
    ``count_the_approvals_zone_asking`` began counting (to ask, not sent: a proof in use holds
    it back)."""
    return page.evaluate("() => window.e2eApprovalsAsked")


def count_the_approvals_zone_asking(page: Page) -> None:
    """Count, in ``page``, each time its approvals zone (``#approvals``, which asks for itself
    every 15 seconds) is about to ask (htmx's ``htmx:beforeRequest``, which the page's
    ``passkeys.js`` cancels while a proof is given in the zone)."""
    page.evaluate(
        """() => {
            window.e2eApprovalsAsked = 0;
            document.addEventListener("htmx:beforeRequest", (event) => {
                if (event.detail.elt && event.detail.elt.id === "approvals") {
                    window.e2eApprovalsAsked += 1;
                }
            });
        }"""
    )


def test_a_child_waits_for_an_adult_whatever_the_judge_says(household: Household):
    """Scenario 3 of the spec: what a child asks an adult for waits for an adult, whatever the
    judge says; its verdict only says how that adult says yes. Sam may invite with an adult's
    yes (scenario 21): the judge asks a proof, so the adult who answers proves it is them, in
    their window "To approve", which reloads itself every 15 seconds and must not take away the
    password being typed (``passkeys.js`` holds it back). Then Sam asks to leave, the judge lets
    it through, and it waits for an adult all the same: the owner says yes."""
    owner, sam, stack = household.owner, household.page_of(SAM), household.stack

    vic = {"display_name": "Vic", "role": "guest", "guest_days": 2}
    asked = send_and_wait(sam, judged("proof: x", tool_message("household_invite", vic)))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    request = confirmation_card(asked)
    expect(request.locator(".action-text")).to_have_text(
        t("action.card.waiting.adult", "en"), timeout=T
    )
    expect(request.locator(".confirmation-proof")).to_have_count(0)  # not Sam's to give

    owner.reload()
    waiting = owner.locator("#approvals .confirmation-card")
    expect(waiting).to_have_count(1, timeout=T)
    expect(owner.locator("#approvals .approval-from")).to_have_text(
        t("approvals.from", "en", name=SAM.display_name)
    )
    expect(waiting).to_contain_text(t("confirmation.hint.proof", "en"))
    expect(waiting.locator("[data-action=confirm]")).to_have_count(0)

    # The owner types his password; the zone comes to ask for itself again, and waits: the form
    # and what is typed in it stay (the zone's content is the one marked once it was typed). The
    # password comes first: a tick before it reloads the zone, as it should, and would take the
    # mark and the counter's start with it.
    password = waiting.locator("input[name=password]")
    password.fill(OWNER_PASSWORD)
    owner.locator("#approvals .approvals").evaluate("zone => { zone.dataset.e2eMarked = 'yes' }")
    count_the_approvals_zone_asking(owner)
    for _ in range(100):
        if approvals_zone_asks(owner) >= 1:
            break
        owner.wait_for_timeout(250)
    assert approvals_zone_asks(owner) >= 1, "the approvals zone did not come to ask again"
    expect(owner.locator("#approvals .approvals[data-e2e-marked]")).to_have_count(1)
    expect(password).to_have_value(OWNER_PASSWORD)

    # The proof holds: the invitation is made, and Sam's open page hears from Domo.
    before = assistant_replies(sam).count()
    waiting.locator("[data-action=password-prove]").click()
    expect(waiting).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    heard = wait_for_a_new_reply(sam, before)
    expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)
    assert open_invitations(stack, "Vic", "guest") == "1"

    # Sam asks to leave: the judge lets it through, and it waits for an adult all the same.
    leaving = send_and_wait(sam, judged("do", tool_message("household_leave", {})))
    expect(reply_text(leaving)).to_have_text("Fake tool result: waiting_for_confirmation")
    request = confirmation_card(leaving)
    expect(request).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(request.locator(".action-text")).to_have_text(t("action.card.waiting.adult", "en"))
    expect(request.locator("[data-action=confirm]")).to_have_count(0)

    # The owner approves it from "To approve": Sam has left, his next page is the sign-in.
    owner.reload()
    waiting = owner.locator("#approvals .confirmation-card")
    expect(waiting).to_have_count(1, timeout=T)
    expect(waiting).to_contain_text(t("confirmation.hint.approver", "en"))
    confirm_with_the_button(waiting)
    sam.goto(stack.base_url + "/chat")
    expect(sam).to_have_url(re.compile(r"/login$"))


def test_the_owner_leaves_in_one_sentence(household: Household):
    owner, alex, stack = household.owner, household.page_of(ALEX), household.stack
    old_owner_id = person_id_of(owner, stack, OWNER)
    alex_id = person_id_of(owner, stack, ALEX)

    # "Give my role to Alex and remove me": one card with two steps, and one yes. The judge asks
    # a firm one here (nothing asks it by default; the owner's own rule on removing someone,
    # scenario 12, asks as much): a plain "oui" leaves it waiting.
    steps = [
        {"tool": "household_transfer_ownership", "arguments": {"person": ALEX.display_name}},
        {"tool": "household_remove", "arguments": {"person": OWNER_DISPLAY_NAME}},
    ]
    asked = send_and_wait(owner, judged(FIRM, tool_message("do_in_order", {"steps": steps})))
    expect(reply_text(asked)).to_have_text("Fake tool result: waiting_for_confirmation")
    chain = confirmation_card(asked)
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(chain.locator(".confirmation-steps li")).to_have_count(2)
    expect(chain).to_contain_text(t("confirmation.hint.explicit", "en"))
    plain = send_and_wait(owner, "oui")
    expect(reply_text(plain)).to_have_text("Fake reply: oui")
    still_owner = f"SELECT role FROM persons WHERE id = '{old_owner_id}'"
    assert database_text(stack, still_owner) == "owner"

    confirmed = send_and_wait(owner, "je confirme")
    expect(reply_text(confirmed)).to_have_text(DECIDED)

    # The former owner has left: his next page is the sign-in.
    owner.goto(stack.base_url + "/chat")
    expect(owner).to_have_url(re.compile(r"/login$"))

    # Alex is the owner (the household page is the owner's), the former owner is removed, there is
    # one owner, and both steps are in the audit.
    alex.goto(stack.base_url + "/household")
    people = alex.locator(".household-person")
    as_owner = people.filter(
        has=alex.locator(".household-name", has_text=re.compile(rf"^{ALEX.display_name}$"))
    )
    expect(as_owner).to_have_attribute("data-person-role", "owner")
    former = people.filter(
        has=alex.locator(".household-name", has_text=re.compile(rf"^{OWNER.display_name}$"))
    )
    expect(former).to_have_attribute("data-person-state", "removed")
    expect(former).to_have_attribute("data-person-role", "adult")
    assert database_text(stack, "SELECT count(*) FROM persons WHERE role = 'owner'") == "1"
    assert database_text(stack, "SELECT id FROM persons WHERE role = 'owner'") == alex_id
    transferred = (
        "SELECT count(*) FROM audit_log WHERE action = 'household.ownership_transferred' "
        f"AND details->>'to' = 'person:{alex_id}'"
    )
    assert database_text(stack, transferred) == "1"
    removed = (
        "SELECT count(*) FROM audit_log WHERE action = 'household.removed' "
        f"AND details->>'person_id' = '{old_owner_id}'"
    )
    assert database_text(stack, removed) == "1"


# --- after the hand-over (scenario 25): Alex is the owner -----------------------------------------


def test_a_chain_hands_the_role_over_once_the_invited_person_has_joined(household: Household):
    alex, stack = household.page_of(ALEX), household.stack
    alex.goto(stack.base_url + "/chat")

    # "Invite Kim as an adult and give her my role once she is here": the judge asks a yes of
    # the chain, Alex gives it, the invitation is made, and the chain pauses.
    kim = [
        step("household_invite", {"display_name": KIM.display_name, "role": KIM.role}),
        step(
            "household_transfer_ownership",
            {"person": KIM.display_name},
            after_arrival_of=KIM.display_name,
        ),
    ]
    chain = confirmation_card(send_and_wait(alex, tool_message("do_in_order", {"steps": kim})))
    expect(chain).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(reply_text(send_and_wait(alex, "je confirme"))).to_have_text("Fake decided: paused")
    alex.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "waiting_arrival", timeout=T)
    link = show_link(chain, 1, stack)

    # Kim joins: the chain goes on by itself, under the yes Alex gave it (he asked for it and
    # was understood: nothing asks him again, since 2026-11-15, unless a rule of his own asks
    # more than that yes gave). Domo, woken, says so in his open page: Kim is the owner, the
    # only one.
    before = assistant_replies(alex).count()
    household.join(KIM, link)
    heard = wait_for_a_new_reply(alex, before, timeout_ms=RESUME_MS)
    expect(reply_text(heard)).to_have_text(DECIDED, timeout=T)
    alex.reload()
    expect(chain).to_have_attribute("data-confirmation-status", "executed", timeout=T)
    assert database_text(stack, OWNERS) == KIM.display_name


# --- after the second hand-over (scenario 26): Kim is the owner -----------------------------------


def test_the_owner_tells_domo_never_to_ask_her(household: Household):
    """Spec 6.3 (scenarios 17 and 18), as it stands since 2026-11-15 (nothing is asked by default
    any more: there is no yes to lift): the owner may tell Domo not to ask her before one of her
    own actions, for herself only (never for a role, nor for everyone: it would pass to a later
    owner). Kim, the owner now, says it for handing her role over, "not at all", then for applying
    rights, "when Domo judges he understood her". Each is a change of rights, judged like any
    other: the first waits for the yes the judge asks, the second is applied at once. With the
    hand-over "not at all", the judge may still ask her a proof that it is her; rights the judge
    lets through are applied at once; and the last hand-over, which the judge lets through, makes
    Alex the owner with no card."""
    kim, stack = household.page_of(KIM), household.stack
    kim.goto(stack.base_url + "/chat")
    kim_id = person_id_of(kim, stack, KIM)
    alex_id = person_id_of(kim, stack, ALEX)
    hand_over = tool_message("household_transfer_ownership", {"person": ALEX.display_name})

    # "Never ask me again before handing my role over": the judge asks a yes of this change of
    # rights (the fake judge's answer when the words do not say), and the card shows its example.
    to_alex = {
        "from": f"person:{kim_id}",
        "to": f"person:{alex_id}",
        "requester_role": "owner",
        "to_role": "adult",
    }
    never = {
        "changes": [
            {
                "person_id": kim_id,
                "action_type": "household.transfer_ownership",
                "ask": "never",
                "summary": HAND_OVER_LIFT_WORDS,
            }
        ],
        "examples": [
            {
                "person_id": kim_id,
                "action_type": "household.transfer_ownership",
                "facts": to_alex,
                "expected": "domo_judges",
            }
        ],
    }
    draft_id = propose_rights(kim, stack, HAND_OVER_LIFT_WORDS, never)
    applying = send_and_wait(kim, tool_message("rights_apply", {"draft": draft_id}))
    expect(reply_text(applying)).to_have_text("Fake tool result: waiting_for_confirmation")
    apply = confirmation_card(applying)
    expect(apply).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(apply).to_contain_text(JUDGE_ASKS_A_YES)
    expect(apply.locator(".confirmation-examples li strong")).to_have_text(
        [t("rights.example.outcome.domo_judges", "en")]
    )
    expect(reply_text(send_and_wait(kim, "oui"))).to_have_text(DECIDED)

    # The judge doubts it is Kim who asks to hand her role over: a proof, the one thing still
    # asked of a hand-over she said "not at all" about. Nothing is handed over; she refuses it, and
    # Domo, woken, says nothing.
    doubted = send_and_wait(kim, judged("proof: not sure it is you", hand_over))
    expect(reply_text(doubted)).to_have_text("Fake tool result: waiting_for_confirmation")
    proof = confirmation_card(doubted)
    expect(proof).to_have_attribute("data-confirmation-status", "pending", timeout=T)
    expect(proof).to_contain_text(
        t("confirmation.asked.domo.proof", "en", reason="not sure it is you")
    )
    expect(proof).to_contain_text(t("confirmation.hint.proof", "en"))
    assert database_text(stack, OWNERS) == KIM.display_name
    before = assistant_replies(kim).count()
    refuse_with_the_button(proof)
    no_new_reply(kim, before)
    assert database_text(stack, OWNERS) == KIM.display_name

    # "Do not wait for my yes on rights when you understood me": a change of rights too, which
    # the judge lets through: it is applied at once, with no card (applying rights asks nothing
    # by default).
    when_understood = {
        "changes": [
            {
                "person_id": kim_id,
                "action_type": "rights.apply",
                "ask": "judge",
                "summary": RIGHTS_LIFT_WORDS,
            }
        ],
        "examples": [
            {
                "person_id": kim_id,
                "action_type": "rights.apply",
                "facts": {},
                "expected": "domo_judges",
            }
        ],
    }
    draft_id = propose_rights(kim, stack, RIGHTS_LIFT_WORDS, when_understood)
    lifting = send_and_wait(kim, judged("do", tool_message("rights_apply", {"draft": draft_id})))
    expect(reply_text(lifting)).to_have_text("Fake tool result: done")
    expect(confirmation_card(lifting)).to_have_count(0)

    # Rights the judge lets through are applied at once: Alex may invite guests again, 3 days
    # at most (the right he had was paused when he came back).
    draft_id = propose_rights(
        kim, stack, RIGHTS_WORDS, alex_may_invite_guests_for_three_days(alex_id)
    )
    applied = send_and_wait(kim, judged("do", tool_message("rights_apply", {"draft": draft_id})))
    expect(reply_text(applied)).to_have_text("Fake tool result: done")
    expect(applied.locator(".action-card")).to_have_attribute(
        "data-action-status", "done", timeout=T
    )
    expect(confirmation_card(applied)).to_have_count(0)
    in_force = (
        "SELECT count(*) FROM policy_rights WHERE action_type = 'household.invite' "
        f"AND person_id = '{alex_id}' AND revoked_at IS NULL AND paused_at IS NULL"
    )
    assert database_text(stack, in_force) == "1"

    # And the hand-over the judge lets through is done at once, with no card: Alex is the owner.
    handed = send_and_wait(kim, judged("do", hand_over))
    expect(reply_text(handed)).to_have_text("Fake tool result: done")
    expect(handed.locator(".action-card")).to_have_attribute(
        "data-action-status", "done", timeout=T
    )
    expect(confirmation_card(handed)).to_have_count(0)
    assert database_text(stack, OWNERS) == ALEX.display_name
