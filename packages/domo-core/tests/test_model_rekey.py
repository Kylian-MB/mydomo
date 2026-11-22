"""Entering the model's API key again from the chat (task 13).

A member asks Domo, and Domo calls ``model_rekey`` for one of the keys that person gave (only
the one who gave a key gives it again, 0.6.0). Once they said yes, the gatekeeper opens a one-time
form (``llm.provider_keys.request_key``) that is shown in a frame under the card, in the answer to
the yes and nowhere else (``card_once``): the key goes from their browser to the vault, never
through the core, the
model, the history, the logs or the audit log. The frame's poll
(``/chat/key/status/<provider>/<request>``) checks the key once it is in
(``llm.provider_keys.check_key``) and says what came of it.

When the key is saved but does not work (or could not be checked), Domo may no longer be able to
answer: the owner's "Enter the key again" button (``POST /chat/key/<provider>/form``, R92) opens a
new form, from the result of the check and from the chat's own error for a refused key.

The first launch takes the same steps (``test_setup_model.py``, unchanged).

The chat pilots talk with Domo on a live server (see ``chat_support.py``); ``gk_mock`` plays the
gatekeeper.
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
from typing import get_args
from zoneinfo import ZoneInfo

import httpx
import pytest
from chat_support import PILOT_MODELS, PILOT_PROVIDER_URL, Card, dump
from domo_common.gatekeeper_api import ProviderRoute, SecretRequestCreate, SecretRequestView
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.channels.page.abilities import PAGE
from domo_core.confirmations.chain import failed_step_may_have_acted
from domo_core.confirmations.describe import describe_for_people
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.steps import mark_step
from domo_core.gatekeeper_client import BindingConflictError, GatekeeperError
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.models import Provider, ProviderModel, ProviderUser, new_provider_id
from domo_core.llm.provider_keys import (
    KeyCheck,
    KeyForm,
    binding_for,
    check_key,
    gatekeeper_failure,
    install_key_form,
    key_form,
    request_key,
    vault_form_path,
)
from domo_core.llm.spending_models import ModelCall, SpendingLimit
from domo_core.llm.types import ChatMessage, ErrorKind, ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, needs_the_judge
from domo_core.policy.models import PolicyRight, PolicySetting
from domo_core.policy.roles import Actor
from domo_core.policy.settings import HOUSE_LANGUAGE, InvalidSetting, update_policy_setting
from domo_core.texts.view import REFERENCE, CatalogTexts, RecueilTexts
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS, InvalidArguments, tools_for
from fakes import LISTED_MODELS, FakeChatModel, FakeModelLister, fake_factory, is_judge_call
from gatekeeper_support import form_texts
from llm_support import CORE_TOKEN
from sqlalchemy import delete, update
from test_spending import add_key
from web_support import in_language, reads_in, shared_session_factory

# --- the plan's tests ----------------------------------------------------------------------------


async def test_rekey_needs_a_confirmation_every_time(owner_chat, gk_mock):
    owner_chat.use_model(
        FakeChatModel(tool_script=[ToolCall("c", "model_rekey", "{}"), "Je te le demande."])
    )
    reply = await owner_chat.say("ma clé ne marche plus")
    assert reply.actions[0]["status"] == "waiting" and not gk_mock.secret_requests.called


async def test_confirmed_rekey_shows_the_vault_form_and_checks_the_key(
    owner_chat, gk_mock, audit_actions
):
    # R47: the probe's model is put in the pilots' slot (the 1a ``use_model`` fixture is another
    # application's), and the answer of the status page is checked (D10).
    card = await owner_chat.confirm_last(await owner_chat.say_with_tool("model_rekey", {}))
    assert 'src="/vault/r/' in card.html
    owner_chat.use_model(FakeChatModel(["pong"]))
    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    assert r.status_code == 200 and 'data-key-status="checked"' in r.text
    assert "provider.rekeyed" in await audit_actions()


async def test_an_adult_who_gave_no_key_has_none_to_enter_again(adult_chat, gk_mock):
    reply = await adult_chat.say_with_tool("model_rekey", {})
    assert reply.actions == [{"tool": "model_rekey", "status": "invalid", "reason": "not_found"}]
    assert not gk_mock.secret_requests.called


async def test_a_member_who_may_change_the_key_gets_the_form_on_their_card(
    owner_chat, adult_chat, grant, gk_mock, last_audit_entry, alex_key
):
    """The owner says yes to Alex's request for his own key: the form is Alex's, not the owner's.
    Nothing is asked of the vault yet, and Alex's card has the "Enter the key" button."""
    await grant("model.rekey", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool("model_rekey", {})
    assert reply.actions == [{"tool": "model_rekey", "status": "waiting"}]
    answer = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert 'data-outcome="executed"' in answer.text
    assert not gk_mock.secret_requests.called
    assert "<iframe" not in answer.text and "/vault/r/" not in answer.text
    assert t("action.card.once.model.rekey", "en") not in plain(answer.text)

    url = f"/chat/key/{alex_key}/form"
    assert rekey_button_url(answer.text) is None  # the owner's card: it is not their form
    # The button names the request the owner said yes to.
    button = f"{url}?confirmation={reply.pending_confirmation_id}"
    assert rekey_button_url((await adult_chat.get("/chat")).text) == button
    assert t("action.card.done.model.rekey.on_request", "en") in plain(
        (await adult_chat.get("/chat")).text
    )

    form = await adult_chat.post(button)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    assert gk_mock.secret_requests.called
    entry = await last_audit_entry("secret.requested")
    assert entry.actor == f"person:{adult_chat.person.id}"
    adult_chat.use_model(FakeChatModel(["pong"]))
    fixed = await adult_chat.get(
        Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    )
    assert fixed.status_code == 200 and 'data-key-status="checked"' in fixed.text


async def test_a_member_who_says_yes_to_their_own_request_gets_the_form_at_once(
    adult_chat, grant, gk_mock, alex_key
):
    await grant("model.rekey", person=adult_chat.person, access="allow")
    card = await adult_chat.confirm_last(await adult_chat.say_with_tool("model_rekey", {}))
    assert card.status == "executed" and 'src="/vault/r/token-req1"' in card.html
    assert rekey_button_url(card.html) is None


async def test_the_model_is_told_the_form_is_on_the_card_of_the_one_who_asked(
    owner_chat, adult_chat, session_factory, alex_key
):
    ctx = context(adult_chat.person, session_factory)
    ctx.approved_by = owner_chat.person.id
    outcome = await TOOLS["model_rekey"].execute({}, ctx)
    assert outcome.status == "done" and outcome.card_once is None
    assert outcome.model_result == {"key_form": "waits_for_them"}
    assert outcome.card["rekey"] == alex_key


async def test_the_form_is_in_the_givers_words_whatever_the_turn_reads(
    owner_chat, session_factory, gk_client, gk_mock
):
    # A turn that reads the reference texts (a wake's) never puts them on the form: the one who
    # gives the key reads French, and so does the form.
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    ctx = context(owner_chat.person, session_factory)
    ctx.gatekeeper = gk_client
    ctx.approved_by = owner_chat.person.id
    # On a page, which opens the form (a channel without a safe screen leaves it on the card).
    ctx.abilities = PAGE
    assert ctx.texts is REFERENCE
    outcome = await TOOLS["model_rekey"].execute({}, ctx)
    assert outcome.status == "done" and outcome.card_once is not None
    sent = sent_request(gk_mock)
    assert (sent.language, sent.texts) == ("fr", form_texts("fr"))
    assert sent.purpose == in_language("fr", "chat.key.secret_purpose")


async def test_no_form_is_opened_that_nobody_would_see(owner_chat, session_factory, gk_mock):
    """A turn no page reads (a wake of Domo after a button): the owner's own yes, but nobody
    would see the form, so none is asked of the vault and the model is told it is on the card."""
    ctx = context(owner_chat.person, session_factory)
    ctx.approved_by = owner_chat.person.id
    ctx.no_reader = True
    outcome = await TOOLS["model_rekey"].execute({}, ctx)
    assert outcome.status == "done" and outcome.card_once is None
    assert outcome.model_result == {"key_form": "waits_for_them"}
    assert outcome.card["rekey"] == owner_chat.server.provider_id
    assert not gk_mock.secret_requests.called


_FORM_CARDS = (
    ("key_add", "action.card.done.key.add", "key_new"),
    ("model_rekey", "action.card.done.model.rekey", "rekey"),
    ("key_admin", "action.card.done.key.admin", "admin_key"),
)


async def _form_card(tool: str, ctx: ToolContext, alex_key: str, admin_key: str) -> dict:
    """The card ``tool`` leaves when the form waits on the card of the one who asked."""
    arguments = {
        "key_add": {"kind": "openai"},
        "model_rekey": {"key": alex_key},
        "key_admin": {"key": admin_key},
    }[tool]
    outcome = await TOOLS[tool].execute(arguments, ctx)
    assert outcome.status == "done" and outcome.card_once is None
    return dict(outcome.card)


@pytest.mark.parametrize(("tool", "base", "button"), _FORM_CARDS)
async def test_a_form_left_on_a_card_is_said_to_the_one_who_asked_when_they_alone_read_it(
    tool, base, button, owner_chat, adult_chat, session_factory, gk_mock, alex_key
):
    """Only when someone else said yes is the card read by another person (the approver), and
    speaks of "whoever asked". In every other case (a channel without a safe screen, a turn
    nobody reads, a chain) the one who asked reads it, in their own conversation: it says "your
    form", to them. The button under the card is the same in both."""
    admin = await add_key(
        session_factory,
        adult_chat.person.id,
        kind="anthropic",
        base_url="https://api.anthropic.com",
    )

    def alex() -> ToolContext:
        return context(adult_chat.person, session_factory)

    no_safe_screen, nobody_reads, a_chain, their_own_yes, approved = (alex() for _ in range(5))
    nobody_reads.abilities = a_chain.abilities = their_own_yes.abilities = PAGE
    nobody_reads.no_reader = True
    a_chain.in_a_chain = True
    their_own_yes.approved_by = adult_chat.person.id
    their_own_yes.no_reader = True
    approved.abilities = PAGE
    approved.approved_by = owner_chat.person.id

    for ctx in (no_safe_screen, nobody_reads, a_chain, their_own_yes):
        card = await _form_card(tool, ctx, alex_key, admin.id)
        assert card["key"] == f"{base}.yours" and card[button]
    card = await _form_card(tool, approved, alex_key, admin.id)
    assert card["key"] == f"{base}.on_request" and card[button]
    assert not gk_mock.secret_requests.called


@pytest.mark.parametrize(("tool", "base", "button"), _FORM_CARDS)
@pytest.mark.parametrize("language", ["en", "fr"])
def test_the_card_of_a_form_left_for_its_reader_speaks_to_them(tool, base, button, language):
    said = t(f"{base}.yours", language)
    in_words = t(f"{base}.yours.in_words", language)
    you, someone = {"en": ("your", "whoever"), "fr": ("ton", "la personne")}[language]
    for sentence in (said, in_words):
        assert you in sentence.lower() and someone not in sentence.lower()
    # On the page the button is under the card; where there is none, the sentence names none.
    assert "button" not in in_words.lower() and "bouton" not in in_words.lower()
    assert said != in_words


async def test_the_key_routes_are_for_the_one_who_gave_the_key(
    owner_chat, adult_chat, child_chat, grant, gk_mock
):
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    form_url = f"/chat/key/{owner_chat.server.provider_id}/form"
    for chat in (adult_chat, child_chat):
        assert (await chat.get(card.key_status_url)).status_code == 403
        assert (await chat.post(form_url)).status_code == 403
    # Even with a right of their own, the owner's key is not theirs to give again.
    await grant("model.rekey", person=adult_chat.person, access="allow")
    assert (await adult_chat.get(card.key_status_url)).status_code == 403
    assert (await adult_chat.post(form_url)).status_code == 403
    assert (await owner_chat.get(card.key_status_url)).status_code == 200


# --- opening a form is not skipped by posting to the route (ruling R20) ---------------------------


async def owner_says_yes_to_the_adults_request(
    owner_chat, adult_chat, grant, key, **right_fields
) -> tuple[str, str, PolicyRight]:
    """Alex may change his own key ``key``, with the owner as the one who says yes
    (``right_fields`` add to Alex's right: a limit, say). Alex asks, the owner says yes. The
    form's address, the button on Alex's card (it names the request), and Alex's right."""
    right = await grant(
        "model.rekey",
        person=adult_chat.person,
        access="allow",
        approver="owner",
        **right_fields,
    )
    reply = await adult_chat.say_with_tool("model_rekey", {})
    request_id = reply.pending_confirmation_id
    answer = await owner_chat.post(f"/confirmations/{request_id}/approve")
    assert 'data-outcome="executed"' in answer.text
    url = f"/chat/key/{key}/form"
    button = rekey_button_url((await adult_chat.get("/chat")).text)
    assert button == f"{url}?confirmation={request_id}"
    return url, button, right


async def test_a_member_whose_yes_is_the_owners_cannot_open_the_form_by_posting_to_the_route(
    owner_chat, adult_chat, grant, gk_mock
):
    await grant("model.rekey", person=adult_chat.person, access="allow", approver="owner")
    url = f"/chat/key/{adult_chat.server.provider_id}/form"
    assert (await adult_chat.post(url)).status_code == 403
    # A request that waits for the owner's yes opens nothing either.
    reply = await adult_chat.say_with_tool("model_rekey", {})
    waiting = f"{url}?confirmation={reply.pending_confirmation_id}"
    assert (await adult_chat.post(waiting)).status_code == 403
    assert (await adult_chat.post(url)).status_code == 403
    # Nor does it once refused.
    await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/refuse")
    assert (await adult_chat.post(waiting)).status_code == 403
    assert not gk_mock.secret_requests.called


async def test_after_the_owners_yes_the_card_button_opens_the_form_for_the_one_who_asked_only(
    owner_chat, adult_chat, child_chat, grant, gk_mock, alex_key
):
    url, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    assert (await adult_chat.post(url)).status_code == 403  # the address alone is not enough
    # Someone else, even with the right to change the key, cannot use Alex's request; nor can
    # the owner, whose yes it was.
    await grant("model.rekey", person=child_chat.person, access="allow", approver="owner")
    assert (await child_chat.post(button)).status_code == 403
    assert (await owner_chat.post(button)).status_code == 403
    assert not gk_mock.secret_requests.called

    form = await adult_chat.post(button)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    assert gk_mock.secret_requests.called


async def test_a_request_that_is_not_an_executed_yes_of_someone_else_opens_nothing(
    owner_chat, adult_chat, grant, gk_mock, alex_key
):
    url, _, _ = await owner_says_yes_to_the_adults_request(owner_chat, adult_chat, grant, alex_key)
    for odd in ("not-an-id", str(uuid.uuid4()), "00000000-0000-0000-0000-00000000000G"):
        assert (await adult_chat.post(f"{url}?confirmation={odd}")).status_code == 403, odd
    # The yes of someone else to another kind of request is no yes to this one.
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    invitation = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "adult"}
    )
    await owner_chat.post(f"/confirmations/{invitation.pending_confirmation_id}/approve")
    other = f"{url}?confirmation={invitation.pending_confirmation_id}"
    assert (await adult_chat.post(other)).status_code == 403
    assert not gk_mock.secret_requests.called


async def test_the_yes_of_someone_else_opens_the_form_for_as_long_as_requests_may_wait(
    owner_chat, adult_chat, grant, gk_mock, clock, alex_key
):
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    clock.advance(timedelta(hours=167))
    assert (await adult_chat.post(button)).status_code == 200
    clock.advance(timedelta(hours=2))  # the default lifetime is 168 hours, from the yes
    assert (await adult_chat.post(button)).status_code == 403


# --- a yes is spent once a key was saved under it, and the limits are read again (ruling R22) ----


async def test_a_key_saved_under_the_yes_spends_it(
    owner_chat, adult_chat, grant, gk_mock, last_audit_entry, alex_key
):
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    request_id = button.rsplit("=", 1)[1]
    form = await adult_chat.post(button)
    assert form.status_code == 200
    # The form's audit entry remembers the yes it was opened under, and the form's request.
    requested = await last_audit_entry("secret.requested")
    assert requested.details == {
        "ref": f"provider:{alex_key}:api_key",
        "confirmation": request_id,
        "request_id": "req1",
    }
    # Until a key is saved, the yes may be used again (a newer form replaces the older one).
    newer = await adult_chat.post(button)
    assert newer.status_code == 200
    adult_chat.use_model(FakeChatModel(["pong"]))
    status_url = Card(uuid.uuid4(), "", newer.text).status_url_after_fulfilment(gk_mock)
    fixed = await adult_chat.get(status_url)
    assert 'data-key-status="checked"' in fixed.text
    rekeyed = await last_audit_entry("provider.rekeyed")
    assert rekeyed.details == {
        "provider_id": alex_key,
        "request_id": "req2",
        "check": "ok",
        "confirmation": request_id,
    }
    # The vault changed: the yes is spent.
    assert (await adult_chat.post(button)).status_code == 403


async def test_a_key_saved_under_the_yes_that_nobody_looked_at_spends_it_too(
    owner_chat, adult_chat, grant, gk_mock, alex_key
):
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    assert (await adult_chat.post(button)).status_code == 200
    # The form is filled in, and the tab is closed before the page asks where the form stands:
    # nothing is recorded, but the vault says it.
    gk_mock.set_status("req1", "fulfilled")
    assert (await adult_chat.post(button)).status_code == 403
    assert len(gk_mock.refs) == 1  # no new form was asked of the vault


async def test_a_vault_that_cannot_say_closes_the_button_rather_than_open_it(
    owner_chat, adult_chat, grant, gk_mock, alex_key
):
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    assert (await adult_chat.post(button)).status_code == 200
    gk_mock.fail_next("get_secret_request", status=500)
    assert (await adult_chat.post(button)).status_code == 403
    assert (await adult_chat.post(button)).status_code == 200  # it answers again


async def test_a_form_that_ran_out_unfilled_is_opened_again_from_its_answer(
    owner_chat, adult_chat, grant, gk_mock, last_audit_entry, alex_key
):
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    form = await adult_chat.post(button)
    gk_mock.set_status("req1", "expired")
    answer = await adult_chat.get(Card(uuid.uuid4(), "", form.text).key_status_url)
    assert 'data-key-status="expired"' in answer.text
    # The answer's button names the same yes, and opens a new form under it.
    assert rekey_button_url(answer.text) == button
    again = await adult_chat.post(rekey_button_url(answer.text))
    assert again.status_code == 200 and 'src="/vault/r/token-req2"' in again.text
    requested = await last_audit_entry("secret.requested")
    assert (requested.details["confirmation"], requested.details["request_id"]) == (
        button.rsplit("=", 1)[1],
        "req2",
    )
    # A form the vault no longer knows is reopened the same way.
    gk_mock.refs.pop("req2")
    lost = await adult_chat.get(Card(uuid.uuid4(), "", again.text).key_status_url)
    assert 'data-key-status="lost"' in lost.text and rekey_button_url(lost.text) == button


async def test_a_key_saved_but_refused_offers_no_button_to_a_spent_yes(
    owner_chat, adult_chat, grant, gk_mock, alex_key
):
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )
    form = await adult_chat.post(button)
    adult_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    status_url = Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    answer = await adult_chat.get(status_url)
    # The button would only answer 403, and a page shows nothing for that: there is none.
    assert 'data-model-error="auth"' in answer.text and rekey_button_url(answer.text) is None


async def test_a_limit_added_after_the_yes_closes_the_button_unless_it_only_asks_again(
    owner_chat, adult_chat, grant, gk_mock, session_factory, alex_key
):
    _, button, right = await owner_says_yes_to_the_adults_request(
        owner_chat, adult_chat, grant, alex_key
    )

    async def limit(expression: str) -> None:
        async with session_factory() as writer:
            await writer.execute(
                update(PolicyRight)
                .where(PolicyRight.id == right.id)
                .values(limit_expression=expression)
            )
            await writer.commit()

    # The yes was given: a limit that would ask the owner again does not take it back ...
    await limit('"ask_owner"')
    assert (await adult_chat.post(button)).status_code == 200
    # ... but one that refuses does.
    await limit('"refuse"')
    assert (await adult_chat.post(button)).status_code == 403


async def test_a_limit_that_counts_what_was_done_reads_the_request_as_at_its_yes(
    owner_chat, adult_chat, grant, gk_mock, alex_key
):  # Ruling R23
    once_a_day = 'history.day >= 1 ? "refuse" : "allow"'
    _, button, _ = await owner_says_yes_to_the_adults_request(
        owner_chat,
        adult_chat,
        grant,
        alex_key,
        limit=once_a_day,
        summary="One change of the key a day.",
    )
    # The limit let the request through when it was made (nothing done yet) and at the yes; at the
    # button the request itself is done, which the limit does not count against its own yes.
    form = await adult_chat.post(button)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    adult_chat.use_model(FakeChatModel(["pong"]))
    status_url = Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    assert 'data-key-status="checked"' in (await adult_chat.get(status_url)).text
    # Once a key is saved, the day's change is made: another request the same day is refused.
    again = await adult_chat.say_with_tool("model_rekey", {})
    assert again.model_results[-1] == {
        "status": "refused",
        "reason": "limit",
        "limit": "One change of the key a day.",
    }


async def test_a_limit_that_counts_what_was_done_reads_a_chain_as_at_its_yes(
    owner_chat, adult_chat, grant, gk_mock, alex_key
):  # Ruling R23, for the form a chain leaves on the card (spec 4.5)
    once_a_day = 'history.day >= 1 ? "refuse" : "allow"'
    await grant(
        "model.rekey",
        person=adult_chat.person,
        access="allow",
        approver="owner",
        limit=once_a_day,
        summary="One change of the key a day.",
    )
    reply = await adult_chat.say_with_tool(
        "do_in_order", {"steps": [{"tool": "model_rekey", "arguments": {}}]}
    )
    cid = reply.pending_confirmation_id
    answer = await owner_chat.post(f"/confirmations/{cid}/approve")
    assert 'data-outcome="executed"' in answer.text and rekey_button_url(answer.text) is None
    # The step done is the chain's own: the limit does not count it against the yes it ran under.
    button = rekey_button_url((await adult_chat.get("/chat")).text)
    assert button == f"/chat/key/{alex_key}/form?confirmation={cid}"
    form = await adult_chat.post(button)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text


async def test_the_form_a_chain_that_ran_at_once_left_opens_from_its_button(
    owner_chat, gk_mock, count_confirmations
):
    # The judge asks no yes of the owner: a chain that holds the form runs at once, with no
    # request; its button opens the form as the owner's own does (the route asks the barrier).
    reply = await owner_chat.say_with_tool(
        "do_in_order", {"steps": [{"tool": "model_rekey", "arguments": {}}]}, judge="do"
    )
    assert reply.actions == [{"tool": "do_in_order", "status": "done"}]
    assert await count_confirmations() == 0
    assert "<iframe" not in reply.cards_html and not gk_mock.secret_requests.called
    url = f"/chat/key/{owner_chat.server.provider_id}/form"
    assert rekey_button_url(reply.cards_html) == url
    assert rekey_button_url((await owner_chat.get("/chat")).text) == url
    form = await owner_chat.post(url)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text


async def test_a_failed_model_rekey_step_in_a_chain_is_known_not_done(
    owner_chat, gk_mock, session_factory, steps_rows, confirmation_status
):  # spec 5.5.2: in a chain the step only leaves a button, it never touches the vault
    reply = await owner_chat.say_with_tool(
        "do_in_order", {"steps": [{"tool": "model_rekey", "arguments": {}}]}
    )
    cid = reply.pending_confirmation_id
    assert cid is not None
    # Say its yes was given, and the step failed for the vault: as a stop leaves it.
    async with session_factory() as writer:
        await mark_step(
            writer,
            cid,
            0,
            status="failed",
            result={
                "key": "action.card.failed.model.rekey.gatekeeper",
                "params": {},
                "reason": "gatekeeper",
            },
            now=owner_chat.clock(),
        )
        await writer.execute(
            update(Confirmation)
            .where(Confirmation.id == cid)
            .values(
                status="failed",
                decided_by=owner_chat.person.id,
                decided_at=owner_chat.clock(),
                decided_via="button",
            )
        )
        await writer.commit()
    [row] = await steps_rows(cid)
    assert not failed_step_may_have_acted(row)
    # Taken up again, the judge is told it did nothing, and it runs under the same yes.
    continued = await owner_chat.say_with_tool(
        "chain_continue", {"request_id": str(cid)}, judge="do"
    )
    assert continued.actions == [{"tool": "chain_continue", "status": "done"}]
    assert continued.pending_confirmation_id == cid
    assert await confirmation_status(cid) == "executed"
    assert [row.status for row in await steps_rows(cid)] == ["done"]
    assert not gk_mock.secret_requests.called
    told = [
        json.loads(message.content)
        for call in owner_chat.server.calls
        if is_judge_call(call.system)
        for message in call.messages
        if isinstance(message, ChatMessage)
    ][-1]
    assert (told["request"]["failed_reason"], told["request"]["may_have_acted"]) == (
        "gatekeeper",
        False,
    )


async def test_a_member_who_says_yes_to_their_own_request_opens_the_form_directly(
    adult_chat, gk_mock, alex_key
):
    # Giving one's own key again is every member's.
    url = f"/chat/key/{alex_key}/form"
    form = await adult_chat.post(url)
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text


@pytest.mark.parametrize("limit", ['"refuse"', '"ask_owner"'])
async def test_a_limit_that_refuses_or_asks_the_owner_closes_the_route_too(
    adult_chat, grant, gk_mock, limit, alex_key
):
    await grant("model.rekey", person=adult_chat.person, access="allow", limit=limit)
    url = f"/chat/key/{alex_key}/form"
    assert (await adult_chat.post(url)).status_code == 403
    assert not gk_mock.secret_requests.called


async def test_a_yes_that_is_an_adults_closes_the_route_for_a_child(child_chat, gk_mock):
    # A child's request waits for an adult by default: no yes of their own to count, even for
    # their own key.
    sams = await add_key(child_chat.server.session_factory, child_chat.person.id)
    url = f"/chat/key/{sams.id}/form"
    assert (await child_chat.post(url)).status_code == 403
    assert not gk_mock.secret_requests.called


# --- only the one who gave a key gives it again (0.6.0, spec 4.2) ---------------------------------


async def test_only_the_giver_rekeys_a_key(owner_chat, adult_chat, gk_mock, alex_key):
    general = owner_chat.server.provider_id
    # The owner may not give Alex's key again, by Domo or by the page.
    reply = await owner_chat.say_with_tool("model_rekey", {"key": alex_key}, judge="do")
    assert reply.actions == [{"tool": "model_rekey", "status": "invalid", "reason": "not_found"}]
    assert (await owner_chat.post(f"/chat/key/{alex_key}/form")).status_code == 403
    # Nor Alex the general key, which the owner gave.
    reply = await adult_chat.say_with_tool("model_rekey", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "model_rekey", "status": "invalid", "reason": "not_found"}]
    assert not gk_mock.secret_requests.called
    # Alex gave one key: with no key named, it is that one.
    card = await adult_chat.confirm_last(await adult_chat.say_with_tool("model_rekey", {}))
    assert card.status == "executed" and 'src="/vault/r/token-req1"' in card.html
    assert sent_request(gk_mock).ref == f"provider:{alex_key}:api_key"
    # Who gave several names the one.
    second = await add_key(owner_chat.server.session_factory, owner_chat.person.id)
    reply = await owner_chat.say_with_tool("model_rekey", {}, judge="do")
    assert reply.actions == [{"tool": "model_rekey", "status": "invalid", "reason": "ambiguous"}]
    reply = await owner_chat.say_with_tool("model_rekey", {"key": second.id}, judge="do")
    assert reply.actions == [{"tool": "model_rekey", "status": "done"}]
    assert sent_request(gk_mock).ref == f"provider:{second.id}:api_key"


async def test_a_turn_on_a_key_someone_else_shared_offers_no_new_form(
    owner_chat, adult_chat, alex_key, session_factory
):
    """The owner's request goes on Alex's key, which Alex shared with him (his own key lists no
    model): when the provider refuses it, only Alex could give it again, so no button."""
    async with session_factory() as writer:
        writer.add(
            ProviderUser(
                provider_id=alex_key,
                scope="person",
                person_id=owner_chat.person.id,
                allowed=True,
                set_by=adult_chat.person.id,
            )
        )
        await writer.execute(
            delete(ProviderModel).where(ProviderModel.provider_id == owner_chat.server.provider_id)
        )
        await writer.commit()
    owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    reply = await owner_chat.say("bonjour")
    assert owner_chat.server.requested[-1][0] == alex_key
    error = error_of(reply)
    assert unescape(error) == t("chat.error.auth", "en") and rekey_button_url(error) is None
    # On his own key, Alex is offered it.
    adult_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    error = error_of(await adult_chat.say("bonjour"))
    assert rekey_button_url(error) == f"/chat/key/{alex_key}/form"


# --- the extracted steps (llm.provider_keys) -----------------------------------------------------


async def test_request_key_asks_for_the_providers_reference_and_binding(gk_client, gk_mock):
    view = await request_key(gk_client, keyed_provider(), KeyForm(CatalogTexts("en")))
    # Once installed, the form is in the words of the one who gives the key, its holes left in
    # for the gatekeeper to fill.
    assert sent_request(gk_mock) == SecretRequestCreate(
        ref="provider:p-0123abcd:api_key",
        purpose=t("chat.key.secret_purpose", "en"),
        bindings=[binding_for("anthropic", "https://api.anthropic.com")],
        language="en",
        texts=form_texts("en"),
    )
    assert view.status == "pending" and vault_form_path(view) == "/vault/r/token-req1"
    # The first launch has its fixed purpose, and the gatekeeper's own texts on the form.
    await request_key(gk_client, keyed_provider(), install_key_form(CatalogTexts("fr")))
    installing = sent_request(gk_mock)
    assert installing.purpose == t("setup.model.secret_purpose", "fr")
    assert (installing.language, installing.texts) == (None, None)


async def test_a_form_the_gatekeeper_would_refuse_is_not_asked_for(gk_client, gk_mock):
    # Review Focus 3: a text with markup (never written: the checks refuse it) fails cleanly.
    marked = RecueilTexts(
        language="en",
        zone=ZoneInfo("UTC"),
        found={"vault.form.privacy": '<a href="https://evil.example">here</a>'},
        personal=frozenset(),
    )
    with pytest.raises(GatekeeperError) as refused:
        await request_key(gk_client, keyed_provider(), KeyForm(marked))
    assert refused.value.code == "invalid_request" and "evil" not in str(refused.value)
    assert not gk_mock.secret_requests.called


async def test_a_house_language_written_as_a_name_still_opens_the_form(
    gk_client, gk_mock, session, add_person
):
    # The house's language takes only a tag (Ruling R35); one written by hand as a name is no
    # choice: the house's default is read, and the form opens in it.
    alex = await add_person("Alex", "adult")
    with pytest.raises(InvalidSetting):
        await update_policy_setting(
            session, key=HOUSE_LANGUAGE, value="French", by=alex.id, now=datetime.now(UTC)
        )
    session.add(
        PolicySetting(
            key=HOUSE_LANGUAGE, value="French", updated_by=alex.id, updated_at=datetime.now(UTC)
        )
    )
    await session.flush()
    await request_key(gk_client, keyed_provider(), await key_form(session, alex))
    sent = sent_request(gk_mock)
    assert sent.language == "en" and sent.texts is not None
    # Texts of a language that is no tag would go as undetermined.
    odd = RecueilTexts(language="French", zone=ZoneInfo("UTC"), found={}, personal=frozenset())
    await request_key(gk_client, keyed_provider(), KeyForm(odd))
    assert sent_request(gk_mock).language == "und"
    # A tag is sent as it is.
    named = RecueilTexts(language="pt-BR", zone=ZoneInfo("UTC"), found={}, personal=frozenset())
    await request_key(gk_client, keyed_provider(), KeyForm(named))
    assert sent_request(gk_mock).language == "pt-BR"


async def test_a_key_asked_for_again_keeps_its_reference_and_its_binding(gk_client, gk_mock):
    first = await request_key(gk_client, keyed_provider(), KeyForm(CatalogTexts("en")))
    first_body = sent_request(gk_mock)
    second = await request_key(gk_client, keyed_provider(), KeyForm(CatalogTexts("en")))
    assert sent_request(gk_mock) == first_body
    assert gk_mock.statuses[first.id] == "superseded" and second.status == "pending"


async def test_a_provider_without_a_key_is_never_asked_for_one(gk_client, gk_mock):
    with pytest.raises(ValueError):
        await request_key(gk_client, keyed_provider(secret_ref=None), KeyForm(CatalogTexts("en")))
    assert not gk_mock.secret_requests.called


@pytest.mark.parametrize(
    ("status", "form_path", "shown"),
    [
        ("pending", "/vault/r/abc_DEF-1", True),
        ("pending", None, False),
        ("pending", "/vault/r/", False),
        ("pending", "https://example.com/vault/r/abc", False),
        ("pending", "/vault/r/abc/../../account", False),
        ("pending", '/vault/r/abc"onload="x', False),
        ("fulfilled", "/vault/r/abc", False),
    ],
)
def test_only_a_pending_form_of_the_vault_is_ever_framed(status, form_path, shown):
    view = SecretRequestView(
        id="req1",
        ref="provider:p-0123abcd:api_key",
        status=status,
        form_path=form_path,
        expires_at="2030-01-01T00:00:00+00:00",  # type: ignore[arg-type]
    )
    assert vault_form_path(view) == (form_path if shown else None)


async def checked(session, gk_client, model, lister=None, *, provider=None):
    """``check_key`` of a saved key, with the models of ``lister`` (the default list of the
    tests) and the model ``model``."""
    key = provider or keyed_provider()
    if key not in session:
        if key.given_by is None:
            giver = Person(display_name="Owner", role="owner")
            session.add(giver)
            await session.flush()
            key.given_by = giver.id
        session.add(key)
        await session.flush()
    factory = fake_factory(model)
    result = await check_key(
        session,
        gk_client,
        key,
        factory,
        lister or FakeModelLister(),
        catalog=shipped_catalog(),
        now=datetime.now(UTC),
        # The question's account is kept on the same rolled-back session, where the key is.
        session_factory=shared_session_factory(session),
    )
    return result, factory


async def test_check_key_registers_the_route_then_asks_the_cheapest_model(
    session, gk_client, gk_mock
):
    model = FakeChatModel(["pong"])
    result, factory = await checked(session, gk_client, model)
    assert result == KeyCheck(None, LISTED_MODELS, "example-haiku-1")
    assert factory.requested == [("p-0123abcd", "example-haiku-1")]
    assert registered_route(gk_mock) == ProviderRoute(
        id="p-0123abcd",
        base_url="https://api.anthropic.com",
        secret_ref="provider:p-0123abcd:api_key",
    )
    assert len(model.calls) == 1


async def test_check_key_tells_what_the_model_said_of_the_key(session, gk_client, gk_mock):
    refused = FakeChatModel([], fail_after=0, error_kind="auth")
    result, _ = await checked(session, gk_client, refused)
    assert result.error == "auth" and result.checked_with == "example-haiku-1"


async def test_check_key_tells_what_the_listing_said_of_the_key(session, gk_client, gk_mock):
    lister = FakeModelLister()
    lister.error = "auth"
    model = FakeChatModel(["pong"])
    result, _ = await checked(session, gk_client, model, lister)
    assert result == KeyCheck("auth", (), None) and model.calls == []


async def test_a_route_the_gatekeeper_refuses_stops_before_the_model(session, gk_client, gk_mock):
    gk_mock.providers.mock(return_value=httpx.Response(409, json={"error": "binding_conflict"}))
    model = FakeChatModel(["pong"])
    with pytest.raises(GatekeeperError) as raised:
        await checked(session, gk_client, model)
    assert gatekeeper_failure(raised.value) == "binding_conflict" and model.calls == []


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (BindingConflictError("op", status=409, code="binding_conflict"), "binding_conflict"),
        (GatekeeperError("op", status=409, code="insecure_transport"), "insecure_transport"),
        (GatekeeperError("op", status=404, code="unknown_secret"), "gatekeeper"),
        (GatekeeperError("op", code="unreachable"), "gatekeeper"),
    ],
)
def test_a_gatekeeper_failure_is_one_of_three_things_to_tell(error, failure):
    assert gatekeeper_failure(error) == failure


# --- the tool ------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "adult", "child", "guest"])
def test_everyone_is_offered_model_rekey_for_their_own_keys(role):  # 0.6.0
    names = [definition.name for definition in tools_for(role, TOOLS, DEFAULT_POLICY)]
    assert "model_rekey" in names


def test_model_rekey_is_judged_every_time():  # D1
    assert TOOLS["model_rekey"].action_type == "model.rekey"
    assert needs_the_judge("model.rekey")


async def test_model_rekey_names_the_provider_whose_key_it_asks_for(session_factory):
    provider_id, owner = await committed_provider(session_factory)
    for arguments in ({}, {"key": provider_id}):
        action = await TOOLS["model_rekey"].describe(
            arguments, actor(owner), context(owner, session_factory)
        )
        assert action == DescribedAction(
            "core", "model.rekey", f"provider:{provider_id}", "secrets", "household"
        )


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"key": 3}, "invalid"),
        ({"provider": "p-0123abcd"}, "invalid"),
        ({"": None}, "invalid"),
        ({"key": "x"}, "not_found"),
        ({"key": "p-99999999"}, "not_found"),
    ],
)
async def test_model_rekey_takes_only_the_key_of_the_person(arguments, code, session_factory):
    _, owner = await committed_provider(session_factory)
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["model_rekey"].describe(
            arguments, actor(owner), context(owner, session_factory)
        )
    assert raised.value.code == code


async def test_a_model_without_a_key_has_no_key_to_enter_again(owner_chat, gk_mock):
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(update(Provider).values(secret_ref=None))
        await writer.commit()
    reply = await owner_chat.say_with_tool("model_rekey", {})
    assert reply.actions == [{"tool": "model_rekey", "status": "invalid", "reason": "not_found"}]
    assert "no key" in reply.model_result_text
    assert not gk_mock.secret_requests.called


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_request_reads_as_a_plain_sentence(lang):
    action = DescribedAction("core", "model.rekey", "provider:p-0123abcd", "secrets", "household")
    assert describe_for_people(action, {}, CatalogTexts(lang)) == t(
        "action.describe.model.rekey", lang
    )


async def test_domo_is_told_the_form_was_shown_and_nothing_more(owner_chat, gk_mock):
    # When the judge asks no yes, the model reads the result of the call itself.
    reply = await owner_chat.say_with_tool("model_rekey", {}, judge="do")
    assert reply.actions == [{"tool": "model_rekey", "status": "done"}]
    assert reply.model_result_text == '{"status": "done", "key_form_shown": true}'
    # The person who asked sees the form, under the card the stream carries.
    assert 'src="/vault/r/token-req1"' in reply.cards_html


# --- the confirmed request: the form, in its frame -----------------------------------------------


async def test_the_form_is_shown_in_a_frame_that_sends_no_referrer(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    assert card.status == "executed"
    (frame,) = re.findall(r"<iframe[^>]*>", card.html)
    assert 'src="/vault/r/token-req1"' in frame and 'referrerpolicy="no-referrer"' in frame
    assert f'title="{t("chat.key.frame_title", "en")}"' in unescape(frame)
    assert "sandbox" not in frame  # the form must be able to post
    assert t("action.card.done.model.rekey", "en") in plain(card.html)
    assert t("action.card.once.model.rekey", "en") in plain(card.html)


async def test_the_form_polls_its_status_every_two_seconds(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    provider_id = owner_chat.server.provider_id
    assert card.key_status_url == f"/chat/key/status/{provider_id}/req1"
    (poll,) = re.findall(r"<[^>]*hx-get=[^>]*>", card.html)
    # No trigger filter: with htmx's allowEval off, a filter is silently always true.
    assert 'hx-trigger="every 2s"' in poll and 'hx-swap="outerHTML"' in poll


async def test_the_form_is_for_the_same_key_and_destination_in_the_owners_language(
    owner_chat, gk_mock, session_factory
):
    reply = await owner_chat.say_with_tool("model_rekey", {})
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    response = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    provider_id = owner_chat.server.provider_id
    assert sent_request(gk_mock) == SecretRequestCreate(
        ref=f"provider:{provider_id}:api_key",
        purpose=in_language("fr", "chat.key.secret_purpose"),
        bindings=[binding_for("openai_compatible", PILOT_PROVIDER_URL)],
        language="fr",
        texts=form_texts("fr"),
    )
    assert in_language("fr", "action.card.once.model.rekey") in plain(response.text)
    assert in_language("fr", "chat.key.frame_title") in unescape(response.text)


async def test_the_form_is_kept_nowhere_and_never_told_to_the_model(
    owner_chat, gk_mock, stored_text, caplog
):
    caplog.set_level(logging.DEBUG)
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    # A later turn: the model is shown the history, with what came of the request.
    await owner_chat.say("merci")
    calls = owner_chat.server.calls
    later = json.dumps([dump(m) for m in calls[-1].messages], ensure_ascii=False)
    assert "[action model.rekey: done]" in later
    everything_sent = json.dumps(
        [[call.system, [dump(m) for m in call.messages]] for call in calls], ensure_ascii=False
    )
    kept = await stored_text()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged  # the logs were read (the server's and the database's, at every level)
    for part in ("/vault/r/", "token-req1", card.key_status_url):
        assert part not in everything_sent and part not in kept and part not in logged


async def test_a_rekey_confirmed_in_words_shows_the_form_in_the_answer_only(
    owner_chat, gk_mock, stored_text
):
    await owner_chat.say_with_tool("model_rekey", {})
    answer = await owner_chat.say("oui")
    assert 'src="/vault/r/token-req1"' in answer.cards_html
    assert "/vault/r/" not in answer.text and "/vault/r/" not in await stored_text()


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        (httpx.Response(500, json={"error": "internal_error"}), "gatekeeper"),
        (httpx.Response(409, json={"error": "binding_conflict"}), "binding_conflict"),
        (httpx.Response(409, json={"error": "insecure_transport"}), "insecure_transport"),
    ],
)
async def test_a_vault_that_opens_no_form_is_explained(owner_chat, gk_mock, answer, failure):
    gk_mock.secret_requests.mock(return_value=answer)
    card = await open_key_form(owner_chat)
    assert card.status == "failed"
    assert t(f"action.card.failed.model.rekey.{failure}", "en") in plain(card.html)
    assert "<iframe" not in card.html and card.key_status_url is None


async def test_a_provider_changed_since_the_yes_gets_no_form(owner_chat, gk_mock):
    reply = await owner_chat.say_with_tool("model_rekey", {})
    other = new_provider_id()
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(delete(ProviderModel))  # a key with models keeps its id
        await writer.execute(delete(ModelCall))  # and so does a key with counted calls
        await writer.execute(
            update(Provider).values(id=other, secret_ref=f"provider:{other}:api_key")
        )
        await writer.commit()
    card = await owner_chat.confirm_last(reply)
    # Described again at the yes, the call is about another provider: it is not carried out.
    assert card.status == "refused" and not gk_mock.secret_requests.called


# --- following the form: GET /chat/key/status/{provider_id}/{request_id} -------------------------


async def test_while_the_form_waits_the_poll_goes_on_without_the_frame(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    r = await owner_chat.get(card.key_status_url)
    assert r.status_code == 200
    assert f'hx-get="{card.key_status_url}"' in r.text and 'hx-trigger="every 2s"' in r.text
    assert "<iframe" not in r.text and "hx-retarget" not in r.headers
    assert not gk_mock.providers.called


async def test_a_new_key_is_checked_through_the_gatekeeper_and_the_poll_ends(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    model = owner_chat.use_model(FakeChatModel(["pong"]))
    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    provider_id = owner_chat.server.provider_id
    assert registered_route(gk_mock) == ProviderRoute(
        id=provider_id, base_url=PILOT_PROVIDER_URL, secret_ref=f"provider:{provider_id}:api_key"
    )
    assert len(model.calls) == 1  # the probe
    assert t("chat.key.checked", "en") in plain(r.text)
    # The answer takes the place of the frame and of the poll: nothing polls any more.
    assert r.headers["hx-retarget"] == "closest .key-form"
    assert r.headers["hx-reswap"] == "outerHTML"
    assert "hx-trigger" not in r.text and "<iframe" not in r.text


async def test_the_request_and_the_new_key_are_audited_without_the_form(
    owner_chat, gk_mock, last_audit_entry, audit_dump
):
    card = await open_key_form(owner_chat)
    owner_chat.use_model(FakeChatModel(["pong"]))
    await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    provider_id = owner_chat.server.provider_id
    requested = await last_audit_entry("secret.requested")
    assert requested.actor == f"person:{owner_chat.person.id}"
    assert requested.details == {"ref": f"provider:{provider_id}:api_key"}
    rekeyed = await last_audit_entry("provider.rekeyed")
    assert rekeyed.actor == f"person:{owner_chat.person.id}"
    assert rekeyed.details == {"provider_id": provider_id, "request_id": "req1", "check": "ok"}
    assert "/vault/r/" not in await audit_dump()


async def test_a_key_of_a_server_without_a_list_is_checked_with_the_model_it_has(
    owner_chat, gk_mock, model_lister, last_audit_entry
):
    # A server that gives no list: its key lists the one model it was named with.
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(delete(ProviderModel).where(ProviderModel.model_id != PILOT_MODELS[1]))
        await writer.commit()
    model_lister.models = ()
    card = await open_key_form(owner_chat)
    model = owner_chat.use_model(FakeChatModel(["pong"]))
    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    assert len(model.calls) == 1  # the probe really ran, with the model the key already has
    assert owner_chat.server.requested[-1] == (owner_chat.server.provider_id, PILOT_MODELS[1])
    assert t("chat.key.checked", "en") in plain(r.text)
    assert (await last_audit_entry("provider.rekeyed")).details["check"] == "ok"


async def test_a_wrong_key_of_a_server_without_a_list_is_not_reported_as_working(
    owner_chat, gk_mock, model_lister, last_audit_entry
):
    model_lister.models = ()
    card = await open_key_form(owner_chat)
    owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    assert 'data-model-error="auth"' in r.text
    assert t("chat.key.checked", "en") not in plain(r.text)
    assert (await last_audit_entry("provider.rekeyed")).details["check"] == "auth"


@pytest.mark.parametrize("kind", get_args(ErrorKind))
async def test_each_failure_of_the_check_is_explained_and_audited(
    owner_chat, gk_mock, last_audit_entry, kind
):
    card = await open_key_form(owner_chat)
    owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind=kind))
    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    assert r.status_code == 200 and f'data-model-error="{kind}"' in r.text
    assert t(f"chat.key.error.{kind}", "en") in plain(r.text)
    assert "ask the assistant" not in plain(r.text)  # who may no longer be able to answer
    assert r.headers["hx-retarget"] == "closest .key-form"
    # The key may not work: the owner can enter it again (R92).
    provider_id = owner_chat.server.provider_id
    assert rekey_button_url(r.text) == f"/chat/key/{provider_id}/form"
    # The vault holds the new key whatever the check said: it was entered again.
    entry = await last_audit_entry("provider.rekeyed")
    assert entry.details == {"provider_id": provider_id, "request_id": "req1", "check": kind}


async def test_the_check_of_a_key_at_its_limit_is_explained(
    owner_chat, gk_mock, last_audit_entry, session_factory
):
    card = await open_key_form(owner_chat)
    provider_id = owner_chat.server.provider_id
    # A firm limit that the check's question would pass, set once the form is open.
    async with session_factory() as writer:
        writer.add(
            SpendingLimit(
                provider_id=provider_id,
                amount=Decimal("0.0001"),
                currency="USD",
                period="month",
                set_by=owner_chat.person.id,
            )
        )
        await writer.commit()
    model = owner_chat.use_model(FakeChatModel(["pong"]))

    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))

    # The question is not asked, and the page says why, with the key it is about.
    assert r.status_code == 200 and 'data-model-error="limit_reached"' in r.text
    assert t("chat.key.error.limit_reached", "en") in plain(r.text)
    assert rekey_button_url(r.text) == f"/chat/key/{provider_id}/form"
    assert model.calls == []
    entry = await last_audit_entry("provider.rekeyed")
    assert entry.details == {
        "provider_id": provider_id,
        "request_id": "req1",
        "check": "limit_reached",
    }


async def test_a_route_the_gatekeeper_refuses_is_explained(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    gk_mock.providers.mock(return_value=httpx.Response(409, json={"error": "binding_conflict"}))
    model = owner_chat.use_model(FakeChatModel(["pong"]))
    r = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    assert 'data-model-error="binding_conflict"' in r.text
    assert t("chat.key.error.binding_conflict", "en") in plain(r.text)
    assert model.calls == []
    assert rekey_button_url(r.text) is None  # a new key would not help


@pytest.mark.parametrize("status", ["expired", "superseded"])
async def test_a_form_that_ran_out_offers_to_ask_domo_again(
    owner_chat, gk_mock, audit_actions, status
):
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    gk_mock.set_status("req1", status)
    r = await owner_chat.get(card.key_status_url)
    assert r.status_code == 200 and f'data-key-status="{status}"' in r.text
    assert t(f"chat.key.{status}", "en") in plain(r.text)
    assert r.headers["hx-retarget"] == "closest .key-form" and "hx-trigger" not in r.text
    assert not gk_mock.providers.called and "provider.rekeyed" not in await audit_actions()
    # The form may have been opened by the button, when Domo could not answer (R92).
    assert rekey_button_url(r.text) == f"/chat/key/{owner_chat.server.provider_id}/form"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"error": "internal_error"}),
        httpx.Response(503),
        httpx.ConnectError("refused"),
    ],
)
async def test_a_vault_that_does_not_answer_for_a_moment_keeps_the_form(
    owner_chat, gk_mock, answer
):  # Minor 1
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    if isinstance(answer, Exception):
        gk_mock.request_views.mock(side_effect=answer)
    else:
        gk_mock.request_views.mock(return_value=answer)
    r = await owner_chat.get(card.key_status_url)
    # The poll again, with a note: the frame (and the key being typed) stays.
    assert r.status_code == 200 and "hx-retarget" not in r.headers
    assert f'hx-get="{card.key_status_url}"' in r.text and 'hx-trigger="every 2s"' in r.text
    assert t("chat.key.waiting_vault", "en") in plain(r.text)


async def test_a_vault_that_refuses_the_poll_ends_the_form(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    gk_mock.request_views.mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    r = await owner_chat.get(card.key_status_url)
    assert r.status_code == 200 and 'data-model-error="gatekeeper"' in r.text
    assert t("chat.key.error.gatekeeper", "en") in plain(r.text) and "hx-trigger" not in r.text
    assert r.headers["hx-retarget"] == "closest .key-form"
    assert rekey_button_url(r.text) == f"/chat/key/{owner_chat.server.provider_id}/form"


async def test_a_form_the_vault_lost_ends_the_poll(owner_chat, gk_mock):  # Minor 2
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    del gk_mock.refs["req1"]  # the gatekeeper answers 404
    r = await owner_chat.get(card.key_status_url)
    # A final answer (a 404 would not be swapped, and the poll would go on for good).
    assert r.status_code == 200 and 'data-key-status="lost"' in r.text
    assert t("chat.key.lost", "en") in plain(r.text) and "hx-trigger" not in r.text
    assert r.headers["hx-retarget"] == "closest .key-form"
    assert rekey_button_url(r.text) == f"/chat/key/{owner_chat.server.provider_id}/form"


async def test_a_filled_in_form_is_checked_and_audited_once(
    owner_chat, gk_mock, audit_actions
):  # Minor 3
    card = await open_key_form(owner_chat)
    model = owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    url = card.status_url_after_fulfilment(gk_mock)
    first = await owner_chat.get(url)
    again = await owner_chat.get(url)
    assert 'data-model-error="auth"' in first.text and 'data-model-error="auth"' in again.text
    assert again.headers["hx-retarget"] == "closest .key-form"
    # The second answer is the recorded one: no new route, no new question, no new entry.
    assert len(model.calls) == 1 and gk_mock.providers.call_count == 1
    assert (await audit_actions()).count("provider.rekeyed") == 1


async def test_the_result_is_in_the_language_of_the_owner(owner_chat, gk_mock, session_factory):
    card = await open_key_form(owner_chat)
    owner_chat.use_model(FakeChatModel(["pong"]))
    url = card.status_url_after_fulfilment(gk_mock)
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    r = await owner_chat.get(url)
    assert in_language("fr", "chat.key.checked") in plain(r.text)


async def test_only_a_known_request_of_the_provider_with_a_key_can_be_followed(owner_chat, gk_mock):
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    provider_id = owner_chat.server.provider_id
    gk_mock.refs["other"] = "provider:p-99999999:api_key"
    gk_mock.statuses["other"] = "pending"
    for url in (
        f"/chat/key/status/{provider_id}/a.b",  # not a request id of the gatekeeper
        f"/chat/key/status/{provider_id}/other",  # another key's request
        "/chat/key/status/p-99999999/req1",  # no such provider
        "/chat/key/status/provider/req1",  # not a provider id
    ):
        assert (await owner_chat.get(url)).status_code == 404, url
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(update(Provider).values(secret_ref=None))
        await writer.commit()
    assert (await owner_chat.get(card.key_status_url)).status_code == 404


async def test_only_the_one_who_gave_the_key_may_follow_the_form(
    owner_chat, live_adult_client, gk_mock
):
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    r = await live_adult_client.get(card.key_status_url)
    assert r.status_code == 403


# --- entering the key again: POST /chat/key/{provider_id}/form (R92) -----------------------------


async def test_a_refused_key_can_be_entered_again_until_one_works(
    owner_chat, gk_mock, audit_actions, last_audit_entry
):
    card = await open_key_form(owner_chat)
    owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    refused = await owner_chat.get(card.status_url_after_fulfilment(gk_mock))
    url = rekey_button_url(refused.text)
    provider_id = owner_chat.server.provider_id
    assert url == f"/chat/key/{provider_id}/form"
    # The button takes the place of the answer it is in.
    assert 'hx-target="closest .key-result"' in refused.text
    first_request = sent_request(gk_mock)
    form = await owner_chat.post(url)
    assert form.status_code == 200 and "hx-retarget" not in form.headers
    # A new form for the same key and the same destination, in its frame, with its poll.
    assert sent_request(gk_mock) == first_request
    (frame,) = re.findall(r"<iframe[^>]*>", form.text)
    assert 'src="/vault/r/token-req2"' in frame and 'referrerpolicy="no-referrer"' in frame
    shown = Card(uuid.uuid4(), "", form.text)
    assert shown.key_status_url == f"/chat/key/status/{provider_id}/req2"
    assert (await audit_actions()).count("secret.requested") == 2
    requested = await last_audit_entry("secret.requested")
    assert requested.actor == f"person:{owner_chat.person.id}"
    assert requested.details == {"ref": f"provider:{provider_id}:api_key"}
    # This time the key works.
    owner_chat.use_model(FakeChatModel(["pong"]))
    fixed = await owner_chat.get(shown.status_url_after_fulfilment(gk_mock))
    assert 'data-key-status="checked"' in fixed.text and rekey_button_url(fixed.text) is None


async def test_a_new_form_is_asked_for_in_the_owners_language(
    owner_chat, gk_mock, session_factory
):  # R46
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    url = f"/chat/key/{owner_chat.server.provider_id}/form"
    form = await owner_chat.post(url)
    assert sent_request(gk_mock).purpose == in_language("fr", "chat.key.secret_purpose")
    assert in_language("fr", "chat.key.frame_title") in unescape(form.text)
    assert in_language("fr", "chat.key.waiting") in plain(form.text)


async def test_a_vault_that_opens_no_new_form_says_so_in_place_of_the_button(
    owner_chat, gk_mock, audit_actions
):
    gk_mock.secret_requests.mock(return_value=httpx.Response(500, json={"error": "internal_error"}))
    url = f"/chat/key/{owner_chat.server.provider_id}/form"
    r = await owner_chat.post(url)
    assert r.status_code == 200 and 'data-model-error="gatekeeper"' in r.text
    assert "hx-retarget" not in r.headers and "<iframe" not in r.text
    assert rekey_button_url(r.text) == url  # it can be tried again
    assert "secret.requested" not in await audit_actions()


async def test_only_the_one_who_gave_the_key_may_open_a_new_form(owner_chat, adult_chat, gk_mock):
    url = f"/chat/key/{owner_chat.server.provider_id}/form"
    assert (await adult_chat.post(url)).status_code == 403
    # From another site: the Origin check of every request that changes something.
    other_site = await owner_chat.client.post(url, headers={"origin": "https://example.com"})
    assert other_site.status_code == 403
    for unknown in ("/chat/key/p-99999999/form", "/chat/key/provider/form"):
        assert (await owner_chat.post(unknown)).status_code == 404, unknown
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(update(Provider).values(secret_ref=None))
        await writer.commit()
    assert (await owner_chat.post(url)).status_code == 404  # no key to enter again
    assert not gk_mock.secret_requests.called


async def test_the_chat_offers_the_owner_a_new_form_when_every_turn_fails(owner_chat, gk_mock):
    owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    reply = await owner_chat.say("bonjour")
    assert reply.status == "error"
    error = error_of(reply)
    assert t("chat.error.auth", "en") in plain(error)
    url = rekey_button_url(error)
    assert url == f"/chat/key/{owner_chat.server.provider_id}/form"
    assert 'hx-target="closest .key-rekey"' in error
    form = await owner_chat.post(url)
    assert 'src="/vault/r/token-req1"' in form.text
    owner_chat.use_model(FakeChatModel(["pong"]))
    fixed = await owner_chat.get(
        Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    )
    assert 'data-key-status="checked"' in fixed.text
    # And Domo answers again.
    owner_chat.use_model(FakeChatModel(["Je t'entends."]))
    assert (await owner_chat.say("tu m'entends ?")).status == "complete"


async def test_nobody_else_is_offered_a_new_form_on_the_chat_error(adult_chat):
    adult_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    error = error_of(await adult_chat.say("bonjour"))
    assert unescape(error) == t("chat.error.auth", "en") and rekey_button_url(error) is None


@pytest.mark.parametrize("keyed", [True, False])
async def test_only_a_refused_key_of_a_provider_with_a_key_offers_a_new_form(owner_chat, keyed):
    if not keyed:
        async with owner_chat.server.session_factory() as writer:
            await writer.execute(update(Provider).values(secret_ref=None))
            await writer.commit()
    kind = "upstream" if keyed else "auth"
    owner_chat.use_model(FakeChatModel([], fail_after=0, error_kind=kind))
    error = error_of(await owner_chat.say("bonjour"))
    assert unescape(error) == t(f"chat.error.{kind}", "en") and rekey_button_url(error) is None


# --- helpers of this file ------------------------------------------------------------------------


def keyed_provider(*, secret_ref: str | None = "provider:p-0123abcd:api_key") -> Provider:
    """A provider with a key (not stored): Anthropic's API."""
    return Provider(
        id="p-0123abcd",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        secret_ref=secret_ref,
    )


async def committed_provider(session_factory) -> tuple[str, Person]:
    """The general key, with a key in the vault, given by an owner, committed; its id, and the
    owner."""
    provider_id = new_provider_id()
    async with session_factory() as writer:
        giver = Person(display_name="Owner", role="owner")
        writer.add(giver)
        await writer.flush()
        writer.add(
            Provider(
                id=provider_id,
                kind="anthropic",
                base_url="https://api.anthropic.com",
                secret_ref=f"provider:{provider_id}:api_key",
                given_by=giver.id,
                is_general=True,
            )
        )
        await writer.commit()
    return provider_id, giver


@pytest.fixture
async def alex_key(chat_server, adult_member) -> str:
    """A key Alex gave (committed), with a key in the vault and the pilots' models: his requests
    go on it. Its id."""
    return (await add_key(chat_server.session_factory, adult_member.person.id)).id


async def open_key_form(owner_chat) -> Card:
    """The owner asks Domo for a form for the model's key and says yes with the button."""
    return await owner_chat.confirm_last(await owner_chat.say_with_tool("model_rekey", {}))


def sent_request(gk_mock) -> SecretRequestCreate:
    """The body of the last secret request the core sent."""
    return SecretRequestCreate.model_validate_json(
        gk_mock.secret_requests.calls.last.request.content
    )


def registered_route(gk_mock) -> ProviderRoute:
    """The body of the last provider route the core registered."""
    return ProviderRoute.model_validate_json(gk_mock.providers.calls.last.request.content)


_REKEY_BUTTON = re.compile(r'<button[^>]*data-action="rekey"[^>]*\bhx-post="([^"]+)"')


def rekey_button_url(fragment: str) -> str | None:
    """Where the "Enter the key again" button of a fragment posts, if it has one."""
    found = _REKEY_BUTTON.search(fragment)
    return found.group(1) if found else None


def error_of(reply) -> str:
    """The ``error`` event of a reply (exactly one)."""
    (error,) = [event.data for event in reply.events if event.event == "error"]
    return error


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def actor(person: Person) -> Actor:
    return Actor(person.id, person.role)  # type: ignore[arg-type]


def context(person: Person, session_factory) -> ToolContext:
    """The context of a call made by ``person`` (what ``describe`` does not use is left empty)."""
    return ToolContext(
        session_factory=session_factory,
        actor=actor(person),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: None,  # type: ignore[arg-type, return-value]
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


# --- the gatekeeper's errors are logged, here too (task 14, R51) -------------------------------


def gatekeeper_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the core logged about the gatekeeper's failures."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "domo_core.gatekeeper_client" and record.levelname == "WARNING"
    ]


async def test_a_vault_that_opens_no_form_for_domo_is_logged(owner_chat, gk_mock, caplog):
    """``tools.model_key``, through ``llm.provider_keys.request_key``."""
    caplog.set_level(logging.INFO)
    gk_mock.secret_requests.mock(return_value=httpx.Response(500, json={"error": "internal_error"}))
    card = await open_key_form(owner_chat)
    assert card.status == "failed"
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call POST /internal/secret-requests failed: status 500, code internal_error"
    ]


async def test_a_vault_that_refuses_the_poll_is_logged(owner_chat, gk_mock, caplog):
    """``channels.page.key_routes``: the status of the form."""
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    caplog.set_level(logging.INFO)
    gk_mock.request_views.mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    await owner_chat.get(card.key_status_url)
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call GET /internal/secret-requests/{id} failed: status 401, "
        "code unauthorized"
    ]


async def test_a_refused_route_when_checking_the_key_is_logged(owner_chat, gk_mock, caplog):
    """``channels.page.key_routes``, through ``llm.provider_keys.check_key``."""
    card = await open_key_form(owner_chat)
    assert card.key_status_url
    caplog.set_level(logging.INFO)
    gk_mock.set_status("req1", "fulfilled")
    gk_mock.fail_next("register_provider", status=409, code="binding_conflict")
    r = await owner_chat.get(card.key_status_url)
    assert 'data-model-error="binding_conflict"' in r.text
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call PUT /internal/providers/{id} failed: status 409, code binding_conflict"
    ]
    assert CORE_TOKEN not in caplog.text and "/vault/r/" not in "".join(gatekeeper_warnings(caplog))


async def test_a_vault_that_opens_no_new_form_for_the_owner_is_logged(owner_chat, gk_mock, caplog):
    """``channels.page.key_routes``: the owner's "Enter the key again" button."""
    caplog.set_level(logging.INFO)
    gk_mock.fail_next("create_secret_request", status=500, code="internal_error")
    await owner_chat.post(f"/chat/key/{owner_chat.server.provider_id}/form")
    assert gatekeeper_warnings(caplog) == [
        "The gatekeeper call POST /internal/secret-requests failed: status 500, code internal_error"
    ]
