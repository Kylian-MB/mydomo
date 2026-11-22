"""Each member's own keys, in words (0.6.0, spec 4.2 and 4.3): ``key_add`` opens the vault form
(the models the key opens are listed once it is in, and the key is the one who gave it's),
``key_remove`` takes one of the person's keys away (never the last key of the household, and the
general key only when the one who gave it has another one to hand it to), ``key_share`` says who
else may use it. The page ``/chat/keys/new`` shows the same form as the first launch, guarded like
the tool, and Domo is told the keys a person may use, and which are theirs.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); ``gk_mock`` plays the
gatekeeper and ``model_lister`` the lists of models the keys open.
"""

import json
import re
import uuid
from html import unescape

import pytest
from chat_support import Card, dump
from domo_common.gatekeeper_api import FORM_TEXT_KEYS, ProviderRoute, SecretRequestCreate
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.describe import describe_type
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.llm.models import ModelLesson, Provider, ProviderModel, ProviderUser
from domo_core.llm.provider_keys import binding_for
from domo_core.llm.router import usable_keys
from domo_core.llm.spending_models import ModelCall
from domo_core.texts.models import WrittenText
from domo_core.texts.view import CatalogTexts
from fakes import LISTED_MODELS, FakeChatModel
from gatekeeper_support import form_texts
from sqlalchemy import delete, select, update
from test_spending import add_key
from web_support import in_language, reads_in

# --- adding a key --------------------------------------------------------------------------------


async def test_a_member_adds_a_key_in_words(adult_chat, gk_mock, last_audit_entry):
    reply = await adult_chat.say_with_tool("key_add", {"kind": "openai"}, judge="do")
    assert reply.actions == [{"tool": "key_add", "status": "done"}]
    [added] = await keys_of(adult_chat)
    assert (added.kind, added.base_url, added.is_general) == (
        "openai",
        "https://api.openai.com/v1",
        False,
    )
    # The vault form is shown once, under the card: the key goes from the browser to the vault.
    assert 'src="/vault/r/token-req1"' in reply.cards_html
    assert sent_request(gk_mock) == SecretRequestCreate(
        ref=f"provider:{added.id}:api_key",
        purpose=t("chat.key.secret_purpose", "en"),
        bindings=[binding_for("openai", "https://api.openai.com/v1")],
        language="en",
        texts=form_texts("en"),
    )
    assert reply.model_results[-1] == {"status": "done", "key_form_shown": True, "key": added.id}

    # Once it is filled in, the models the key opens are listed, and it is checked.
    adult_chat.use_model(FakeChatModel(["pong"]))
    checked = await adult_chat.get(
        Card(uuid.uuid4(), "", reply.cards_html).status_url_after_fulfilment(gk_mock)
    )
    assert checked.status_code == 200 and 'data-key-status="checked"' in checked.text
    assert await listed_models(adult_chat, added.id) == set(LISTED_MODELS)
    entry = await last_audit_entry("key.added")
    assert entry.actor == f"person:{adult_chat.person.id}"
    assert entry.details == {
        "provider_id": added.id,
        "kind": "openai",
        "host": "api.openai.com",
        "models": len(LISTED_MODELS),
    }
    # The key is Alex's: his next request goes on it.
    await adult_chat.say("bonjour")
    assert adult_chat.server.requested[-1][0] == added.id


async def test_the_new_key_page_opens_the_vault_form(adult_chat, gk_mock, last_audit_entry):
    page = await adult_chat.get(KEY_NEW_PATH)
    assert page.status_code == 200
    assert "data-key-new" in page.text and 'name="kind"' in page.text
    assert f'hx-post="{KEY_NEW_PATH}"' in page.text
    # What is wrong is said with the form again, and nothing is asked of the vault.
    wrong = await adult_chat.post(KEY_NEW_PATH, data={"kind": "nope"})
    assert wrong.status_code == 400 and t("setup.model.error.kind", "en") in unescape(wrong.text)
    assert not gk_mock.secret_requests.called and await keys_of(adult_chat) == []

    form = await adult_chat.post(KEY_NEW_PATH, data={"kind": "anthropic"})
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    [added] = await keys_of(adult_chat)
    assert sent_request(gk_mock).bindings == [binding_for("anthropic", "https://api.anthropic.com")]
    requested = await last_audit_entry("secret.requested")
    assert requested.details == {"ref": f"provider:{added.id}:api_key"}

    adult_chat.use_model(FakeChatModel(["pong"]))
    status_url = Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    checked = await adult_chat.get(status_url)
    assert 'data-key-status="checked"' in checked.text
    assert (await last_audit_entry("key.added")).details["provider_id"] == added.id
    # A later answer for the same form says what was found, without asking again.
    again = await adult_chat.get(status_url)
    assert 'data-key-status="checked"' in again.text
    assert await audit_count(adult_chat, "key.added") == 1


async def test_a_key_form_after_the_install_carries_the_givers_texts(
    adult_chat, gk_mock, session_factory
):  # Scenario 10
    # Alex reads French: the vault form says his texts, holes left in for the gatekeeper.
    async with session_factory() as writer:
        await reads_in(writer, adult_chat.person, "fr")
        await writer.commit()
    # By the tool, in his turn.
    await adult_chat.say_with_tool("key_add", {"kind": "openai"}, judge="do")
    by_the_tool = sent_request(gk_mock)
    assert (by_the_tool.language, by_the_tool.texts) == ("fr", form_texts("fr"))
    assert by_the_tool.purpose == in_language("fr", "chat.key.secret_purpose")
    # By the page of a new key: the same.
    await adult_chat.post(KEY_NEW_PATH, data={"kind": "anthropic"})
    by_the_page = sent_request(gk_mock)
    assert (by_the_page.language, by_the_page.texts) == ("fr", form_texts("fr"))
    assert by_the_page.purpose == in_language("fr", "chat.key.secret_purpose")


async def own_texts(chat) -> set[str]:
    """The keys of the texts written for the person of ``chat`` alone."""
    async with chat.server.session_factory() as reader:
        found = await reader.execute(
            select(WrittenText.key).where(WrittenText.person_id == chat.person.id)
        )
        return set(found.scalars())


async def with_a_writing_note(chat) -> None:
    """The person of ``chat`` has a writing note Domo keeps for them: a reason to write the
    texts they read (committed)."""
    async with chat.server.session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == chat.person.id).values(writing_note="Plain words.")
        )
        await writer.commit()


async def test_a_key_form_asks_nothing_for_a_giver_without_a_reason(adult_chat, gk_mock):
    # English has default phrases of its own, and Alex has no writing note: nothing to write.
    await adult_chat.post(KEY_NEW_PATH, data={"kind": "anthropic"})
    await adult_chat.server.texts_idle()
    assert not FORM_TEXT_KEYS & await own_texts(adult_chat)


async def test_opening_a_key_form_by_the_tool_has_the_givers_form_texts_written(
    adult_chat, gk_mock
):
    # Like a page asks its texts: the next form is in Alex's own words, after his writing note.
    await with_a_writing_note(adult_chat)
    assert not FORM_TEXT_KEYS & await own_texts(adult_chat)
    await adult_chat.say_with_tool("key_add", {"kind": "openai"}, judge="do")
    await adult_chat.server.texts_idle()
    assert await own_texts(adult_chat) >= FORM_TEXT_KEYS


async def test_opening_a_key_form_by_the_page_has_the_givers_form_texts_written(
    adult_chat, gk_mock
):
    await with_a_writing_note(adult_chat)
    assert not FORM_TEXT_KEYS & await own_texts(adult_chat)
    await adult_chat.post(KEY_NEW_PATH, data={"kind": "anthropic"})
    await adult_chat.server.texts_idle()
    assert await own_texts(adult_chat) >= FORM_TEXT_KEYS


async def test_a_server_without_a_list_is_asked_the_name_of_its_model(
    adult_chat, gk_mock, model_lister, last_audit_entry
):
    model_lister.models = ()
    form = await adult_chat.post(
        KEY_NEW_PATH,
        data={"kind": "openai_compatible", "base_url": "http://192.168.1.10:11434/v1"},
    )
    # No key: nothing to type in the vault, the server is checked at once, and asked the name.
    assert form.status_code == 200 and "data-model-name" in form.text
    assert not gk_mock.secret_requests.called
    [added] = await keys_of(adult_chat)
    name_url = f"{KEY_NEW_PATH}/{added.id}/check"
    assert f'hx-post="{name_url}"' in form.text
    adult_chat.use_model(FakeChatModel(["pong"]))
    checked = await adult_chat.post(name_url, data={"model": "llama3"})
    assert 'data-key-status="checked"' in checked.text
    assert await listed_models(adult_chat, added.id) == {"llama3"}
    assert (await last_audit_entry("key.added")).details["host"] == "192.168.1.10"


async def test_a_child_adding_a_key_needs_an_adult(child_chat, adult_chat, gk_mock):
    reply = await child_chat.say_with_tool("key_add", {"kind": "openai"})
    assert reply.actions == [{"tool": "key_add", "status": "waiting"}]
    cid = reply.pending_confirmation_id
    answer = await adult_chat.post(f"/confirmations/{cid}/approve")
    assert 'data-outcome="executed"' in answer.text
    # The form is the child's: nothing is asked of the vault until they press the button.
    assert not gk_mock.secret_requests.called and "/vault/r/" not in answer.text
    assert key_new_button_url(answer.text) is None
    button = key_new_button_url((await child_chat.get("/chat")).text)
    assert button == f"{KEY_NEW_PATH}?confirmation={cid}"
    page = await child_chat.get(button)
    assert page.status_code == 200 and 'name="kind"' in page.text
    form = await child_chat.post(button, data={"kind": "openai"})
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    child_chat.use_model(FakeChatModel(["pong"]))
    status_url = Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock)
    assert 'data-key-status="checked"' in (await child_chat.get(status_url)).text
    [added] = await keys_of(child_chat)
    assert added.kind == "openai"
    # One yes, one key: once it is added, the button opens nothing more.
    assert (await child_chat.get(button)).status_code == 403


async def test_one_yes_adds_one_key_even_when_the_page_is_opened_twice(
    child_chat, adult_chat, gk_mock, last_audit_entry
):
    reply = await child_chat.say_with_tool("key_add", {"kind": "openai"})
    cid = reply.pending_confirmation_id
    await adult_chat.post(f"/confirmations/{cid}/approve")
    button = f"{KEY_NEW_PATH}?confirmation={cid}"
    form = await child_chat.post(button, data={"kind": "openai"})
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text
    [started] = await keys_of(child_chat)
    # The yes is bound to that key: the page opens no second one under it.
    assert (await child_chat.get(button)).status_code == 403
    assert (await child_chat.post(button, data={"kind": "anthropic"})).status_code == 403
    assert [key.id for key in await keys_of(child_chat)] == [started.id]
    entry = await last_audit_entry("key.started")
    assert entry.details == {
        "provider_id": started.id,
        "kind": "openai",
        "host": "api.openai.com",
        "confirmation": str(cid),
    }
    # That key's own form may be opened again under the yes, while nothing was saved.
    again = f"{KEY_NEW_PATH}/{started.id}/form?confirmation={cid}"
    reopened = await child_chat.post(again)
    assert reopened.status_code == 200 and 'src="/vault/r/token-req2"' in reopened.text
    # A key saved under the yes spends it, whatever its check says (ruling R22): a key the
    # provider refuses offers no new form under it.
    child_chat.use_model(FakeChatModel([], fail_after=0, error_kind="auth"))
    status_url = Card(uuid.uuid4(), "", reopened.text).status_url_after_fulfilment(gk_mock)
    refused = await child_chat.get(status_url)
    assert 'data-model-error="auth"' in refused.text
    assert 'data-action="rekey"' not in refused.text
    assert (await child_chat.post(again)).status_code == 403
    assert (await child_chat.get(button)).status_code == 403
    assert len(gk_mock.refs) == 2  # no third form was asked of the vault


async def test_a_key_added_in_words_under_a_yes_is_that_yes_s_one_key(
    adult_chat, gk_mock, last_audit_entry
):
    # Alex's own yes shows the form at once: the key it started is the key of that request.
    card = await adult_chat.confirm_last(
        await adult_chat.say_with_tool("key_add", {"kind": "openai"})
    )
    assert card.status == "executed" and 'src="/vault/r/token-req1"' in card.html
    [started] = await keys_of(adult_chat)
    entry = await last_audit_entry("key.started")
    assert entry.details == {
        "provider_id": started.id,
        "kind": "openai",
        "host": "api.openai.com",
        "confirmation": str(card.id),
    }
    assert (await adult_chat.get(f"{KEY_NEW_PATH}?confirmation={card.id}")).status_code == 403
    # A key that was not started from the chat has no page of a new key.
    general = adult_chat.server.provider_id
    assert (await adult_chat.post(f"{KEY_NEW_PATH}/{general}/check")).status_code == 404


async def test_the_new_key_page_needs_the_same_agreement_as_the_tool(
    child_chat, adult_chat, gk_mock
):
    # A child's key waits for an adult: the page does not skip that.
    assert (await child_chat.get(KEY_NEW_PATH)).status_code == 403
    assert (await child_chat.post(KEY_NEW_PATH, data={"kind": "openai"})).status_code == 403
    reply = await child_chat.say_with_tool("key_add", {"kind": "openai"})
    waiting = f"{KEY_NEW_PATH}?confirmation={reply.pending_confirmation_id}"
    assert (await child_chat.get(waiting)).status_code == 403
    assert (await child_chat.post(waiting, data={"kind": "openai"})).status_code == 403
    for odd in ("not-an-id", str(uuid.uuid4())):
        assert (await child_chat.get(f"{KEY_NEW_PATH}?confirmation={odd}")).status_code == 403
    await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    # The yes is for the one who asked, and no one else.
    assert (await adult_chat.get(waiting)).status_code == 403
    assert (await child_chat.get(waiting)).status_code == 200
    assert not gk_mock.secret_requests.called
    # An adult needs nobody's agreement.
    assert (await adult_chat.get(KEY_NEW_PATH)).status_code == 200


async def test_a_chain_leaves_the_form_of_a_new_key_behind_its_button(adult_chat, gk_mock):
    reply = await adult_chat.say_with_tool(
        "do_in_order", {"steps": [{"tool": "key_add", "arguments": {"kind": "openai"}}]}
    )
    cid = reply.pending_confirmation_id
    card = await adult_chat.confirm_last(reply)
    # A step of a chain never opens the vault at once: its button, which names the chain, does.
    assert card.status == "executed" and not gk_mock.secret_requests.called
    button = f"{KEY_NEW_PATH}?confirmation={cid}"
    assert key_new_button_url((await adult_chat.get("/chat")).text) == button
    form = await adult_chat.post(button, data={"kind": "openai"})
    assert form.status_code == 200 and 'src="/vault/r/token-req1"' in form.text


# --- removing a key ------------------------------------------------------------------------------


async def test_removing_the_last_key_is_refused_until_another_is_given(owner_chat, gk_mock):
    general = owner_chat.server.provider_id
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "invalid", "reason": "not_possible"}]
    assert "give another key first" in reply.model_result_text
    assert [key.id for key in await all_keys(owner_chat)] == [general]
    assert not gk_mock.providers.called

    other = await add_key(owner_chat.server.session_factory, owner_chat.person.id)
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    assert [(key.id, key.is_general) for key in await all_keys(owner_chat)] == [(other.id, True)]


async def test_a_refused_removal_is_no_mistake_of_the_model(owner_chat, session_factory):
    """Removing the last key is a call the household's rules refuse, not one the model got
    wrong: the rest of the request stays on the same model, and no lesson is written."""
    server = owner_chat.server
    first = len(server.requested)
    reply = await owner_chat.say_with_tool("key_remove", {"key": server.provider_id}, judge="do")
    asked = [model_id for _, model_id in server.requested[first:]]
    assert len(asked) >= 2 and set(asked) == {asked[0]}
    async with session_factory() as reader:
        assert (await reader.execute(select(ModelLesson))).first() is None
    assert reply.actions[0]["reason"] == "not_possible"


async def test_removing_the_general_key_hands_it_to_the_owners_other_key(
    owner_chat, adult_chat, gk_mock, last_audit_entry
):
    factory = owner_chat.server.session_factory
    general = owner_chat.server.provider_id
    await add_key(factory, adult_chat.person.id)
    # Alex's key is not the owner's: the general key cannot go to it.
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "invalid", "reason": "not_possible"}]
    assert "general key" in reply.model_result_text

    older = await add_key(factory, owner_chat.person.id)
    newer = await add_key(factory, owner_chat.person.id)
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    generals = {key.id: key.is_general for key in await all_keys(owner_chat)}
    assert general not in generals
    assert (generals[older.id], generals[newer.id]) == (True, False)
    entry = await last_audit_entry("key.removed")
    assert entry.details == {"provider_id": general}
    assert entry.actor == f"person:{owner_chat.person.id}"
    # Only one's own key is removed.
    reply = await adult_chat.say_with_tool("key_remove", {"key": older.id}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "invalid", "reason": "not_found"}]


async def test_the_general_key_goes_to_another_key_of_the_one_who_gave_it(
    owner_chat, adult_chat, gk_mock, set_role
):
    """The owner's role was handed over to Alex: the general key is still the key of the one who
    gave it, and when they remove it, it goes to another key of theirs, never to Alex's."""
    factory = owner_chat.server.session_factory
    general = owner_chat.server.provider_id
    await set_role(owner_chat.person, "adult")
    await set_role(adult_chat.person, "owner")
    alexs = await add_key(factory, adult_chat.person.id)
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "invalid", "reason": "not_possible"}]
    assert "the one who gave it gave no other key" in reply.model_result_text
    assert "(the owner)" not in reply.model_result_text

    theirs = await add_key(factory, owner_chat.person.id)
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    generals = {key.id: key.is_general for key in await all_keys(owner_chat)}
    assert general not in generals
    assert (generals[theirs.id], generals[alexs.id]) == (True, False)


async def test_removing_a_key_closes_its_route(adult_chat, gk_mock, session_factory):
    factory = adult_chat.server.session_factory
    alex = await add_key(factory, adult_chat.person.id)
    await adult_chat.say("bonjour")  # a call counted on Alex's key
    # It lists no model any more: Alex's request that removes it goes on the general key, and
    # Domo reads what came of it.
    async with session_factory() as writer:
        await writer.execute(delete(ProviderModel).where(ProviderModel.provider_id == alex.id))
        await writer.commit()
    reply = await adult_chat.say_with_tool("key_remove", {"key": alex.id}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    routes = [
        ProviderRoute.model_validate_json(call.request.content) for call in gk_mock.providers.calls
    ]
    assert ProviderRoute(id=alex.id, base_url=alex.base_url, secret_ref=None) in routes
    assert ProviderRoute(id=f"{alex.id}-admin", base_url=alex.base_url, secret_ref=None) in routes
    # Domo is told the key stays in the vault, and to have it revoked at the provider.
    assert "revoke" in reply.model_result_text
    assert alex.id not in {key.id for key in await all_keys(adult_chat)}
    async with session_factory() as reader:
        models = await reader.execute(
            select(ProviderModel).where(ProviderModel.provider_id == alex.id)
        )
        assert models.first() is None
        calls = (await reader.execute(select(ModelCall.provider_id))).scalars().all()
    assert calls and alex.id not in calls and None in calls


async def test_a_pass_that_gave_a_removed_key_its_secret_back_meanwhile_is_undone(
    adult_chat, gk_mock, session_factory
):
    """A pass that declares a key's route with its secret (a listing, a reading of the real
    spending) may do it just after ``key_remove`` declared it without one, and before the row is
    locked: once the row is gone, both routes are declared without a secret again."""
    alex = await add_key(adult_chat.server.session_factory, adult_chat.person.id)
    # The request goes on the general key (Alex's lists no model), as in the test above.
    async with session_factory() as writer:
        await writer.execute(delete(ProviderModel).where(ProviderModel.provider_id == alex.id))
        await writer.commit()
    # What the gatekeeper holds for each route: the secret it was last declared with.
    secrets: dict[str, str | None] = {}
    raced: list[str] = []
    register = gk_mock.providers.side_effect

    def gatekeeper(request, id):
        route = ProviderRoute.model_validate_json(request.content)
        secrets[route.id] = route.secret_ref
        if route.id == f"{alex.id}-admin" and route.secret_ref is None and not raced:
            # The pass declares the key's route with its secret, right after the removal.
            raced.append(alex.id)
            secrets[alex.id] = alex.secret_ref
        return register(request, id)

    gk_mock.providers.side_effect = gatekeeper
    reply = await adult_chat.say_with_tool("key_remove", {"key": alex.id}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    assert raced
    assert (secrets[alex.id], secrets[f"{alex.id}-admin"]) == (None, None)
    assert alex.id not in {key.id for key in await all_keys(adult_chat)}


async def test_a_key_removed_during_a_turn_stops_the_next_call(adult_chat, gk_mock):
    alex = await add_key(adult_chat.server.session_factory, adult_chat.person.id)
    # The request goes on Alex's key, and Domo removes it: the next call of the turn has none.
    reply = await adult_chat.say_with_tool("key_remove", {"key": alex.id}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    assert t("chat.error.no_model", "en") in unescape(error_of(reply))
    assert reply.status == "error"


# --- sharing a key -------------------------------------------------------------------------------


async def test_sharing_a_key_with_a_person(adult_chat, child_chat, last_audit_entry):
    server = adult_chat.server
    alex = await add_key(server.session_factory, adult_chat.person.id)
    assert alex.id not in await usable_ids(child_chat)
    reply = await adult_chat.say_with_tool(
        "key_share",
        {"key": alex.id, "with": "person", "person": "Sam", "allowed": True},
        judge="do",
    )
    assert reply.actions == [{"tool": "key_share", "status": "done"}]
    async with server.session_factory() as reader:
        [line] = (await reader.execute(select(ProviderUser))).scalars().all()
    assert (line.provider_id, line.scope, line.person_id, line.allowed, line.set_by) == (
        alex.id,
        "person",
        child_chat.person.id,
        True,
        adult_chat.person.id,
    )
    entry = await last_audit_entry("key.shared")
    assert entry.details == {
        "provider_id": alex.id,
        "scope": "person",
        "person": str(child_chat.person.id),
        "allowed": True,
    }
    # Sam may use it now: it comes before the general key.
    assert (await usable_ids(child_chat))[0] == alex.id
    await child_chat.say("bonjour")
    assert server.requested[-1][0] == alex.id
    # Nobody shares someone else's key.
    reply = await child_chat.say_with_tool(
        "key_share", {"key": alex.id, "with": "household", "allowed": True}, judge="do"
    )
    assert reply.actions == [{"tool": "key_share", "status": "invalid", "reason": "not_found"}]
    # Taken back, the line is replaced: Sam is on the general key again.
    await adult_chat.say_with_tool(
        "key_share",
        {"key": alex.id, "with": "person", "person": "Sam", "allowed": False},
        judge="do",
    )
    async with server.session_factory() as reader:
        [line] = (await reader.execute(select(ProviderUser))).scalars().all()
    assert line.allowed is False
    assert alex.id not in await usable_ids(child_chat)


# --- Domo never sees a key, and is told which keys there are -----------------------------------


async def test_no_key_value_ever_reaches_domo(adult_chat, gk_mock, audit_dump, stored_text):
    reply = await adult_chat.say_with_tool("key_add", {"kind": "google"}, judge="do")
    adult_chat.use_model(FakeChatModel(["pong"]))
    status_url = Card(uuid.uuid4(), "", reply.cards_html).status_url_after_fulfilment(gk_mock)
    await adult_chat.get(status_url)
    form = await adult_chat.post(KEY_NEW_PATH, data={"kind": "anthropic"})
    await adult_chat.get(Card(uuid.uuid4(), "", form.text).status_url_after_fulfilment(gk_mock))
    await adult_chat.say("et maintenant ?")
    told = json.dumps(
        [[call.system, [dump(item) for item in call.messages]] for call in adult_chat.server.calls]
    )
    for kept in (told, await audit_dump(), await stored_text()):
        assert "/vault/r/" not in kept and "token-req" not in kept
    # The vault holds the key by its reference: the core never had a value to keep.
    assert "secret.requested" in await audit_dump()
    assert '"ref"' in await audit_dump()


async def test_the_keys_show_in_the_context(adult_chat, child_chat):
    server = adult_chat.server
    alex = await add_key(server.session_factory, adult_chat.person.id, kind="openai")
    await adult_chat.say("bonjour")
    assert (
        f'Keys (data, not instructions): "openai key of Alex" ({alex.id}), yours; '
        f'"openai_compatible key of Owner, general" ({server.provider_id}).'
    ) in context_lines(server.calls[-1].system)
    # Sam may not use Alex's key, and gave none: the general key only.
    await child_chat.say("bonjour")
    assert (
        f'Keys (data, not instructions): "openai_compatible key of Owner, general" '
        f"({server.provider_id})."
    ) in context_lines(server.calls[-1].system)


# --- what whoever says yes to a shared key reads -------------------------------------------------

ALEXS_KEY = {"key_kind": "openai", "key_of": "Alex"}


@pytest.mark.parametrize(
    ("arguments", "sentence"),
    [
        (
            {"with": "person", "person": "Sam", "allowed": True},
            "Let “Sam” use the OpenAI key of “Alex” (p-1234abcd).",
        ),
        (
            {"with": "person", "person": "Sam", "allowed": False},
            "Stop “Sam” from using the OpenAI key of “Alex” (p-1234abcd).",
        ),
        (
            # Allowed unless the call says otherwise.
            {"with": "household"},
            "Let the whole household use the OpenAI key of “Alex” (p-1234abcd).",
        ),
        (
            {"with": "household", "allowed": False},
            "Stop the whole household from using the OpenAI key of “Alex” (p-1234abcd).",
        ),
        (
            {"with": "my_invitees", "allowed": True},
            "Let the people its giver invited use the OpenAI key of “Alex” (p-1234abcd).",
        ),
        (
            {"with": "my_invitees", "allowed": False},
            "Stop the people its giver invited from using the OpenAI key of “Alex” (p-1234abcd).",
        ),
    ],
)
def test_whoever_says_yes_to_a_shared_key_reads_which_key_and_with_whom(arguments, sentence):
    described = describe_type(
        "key.share", {"key": "p-1234abcd", **arguments}, CatalogTexts("en"), ALEXS_KEY
    )
    assert described == sentence


def test_a_key_of_a_kind_without_a_word_is_told_by_its_kind():
    facts = {"key_kind": "mistral", "key_of": "Alex"}
    described = describe_type(
        "key.share", {"key": "p-1234abcd", "with": "household"}, CatalogTexts("en"), facts
    )
    assert described == "Let the whole household use the mistral key of “Alex” (p-1234abcd)."


def test_a_share_that_does_not_read_gets_the_general_sentence():
    for arguments in (
        {"key": "p-1234abcd", "with": "everyone"},
        {"key": "p-1234abcd", "with": "person"},
        {"key": "p-1234abcd", "with": "household", "allowed": "yes"},
    ):
        assert describe_type("key.share", arguments, CatalogTexts("en")) == t(
            "action.describe", "en"
        )


async def test_a_child_s_shared_key_is_read_whole_by_the_adult(child_chat, adult_chat):
    sams = await add_key(child_chat.server.session_factory, child_chat.person.id)
    reply = await child_chat.say_with_tool(
        "key_share", {"key": sams.id, "with": "person", "person": "Alex", "allowed": True}
    )
    assert reply.actions == [{"tool": "key_share", "status": "waiting"}]
    sentence = f"Let “Alex” use the Other server (OpenAI-compatible) key of “Sam” ({sams.id})."
    assert sentence in unescape(reply.cards_html)
    assert sentence in unescape((await adult_chat.get("/approvals")).text)


# --- helpers of this file ------------------------------------------------------------------------


async def all_keys(chat) -> list[Provider]:
    """Every key of the household (committed), oldest first."""
    async with chat.server.session_factory() as reader:
        found = await reader.execute(select(Provider).order_by(Provider.created_at, Provider.id))
        return list(found.scalars().all())


async def keys_of(chat) -> list[Provider]:
    """The keys the pilot's person gave, oldest first."""
    return [key for key in await all_keys(chat) if key.given_by == chat.person.id]


async def usable_ids(chat) -> list[str]:
    async with chat.server.session_factory() as reader:
        return [key.id for key in await usable_keys(reader, chat.person)]


async def listed_models(chat, provider_id: str) -> set[str]:
    async with chat.server.session_factory() as reader:
        found = await reader.execute(
            select(ProviderModel.model_id).where(ProviderModel.provider_id == provider_id)
        )
        return set(found.scalars().all())


async def audit_count(chat, action: str) -> int:
    async with chat.server.session_factory() as reader:
        found = await reader.execute(select(AuditEntry.id).where(AuditEntry.action == action))
        return len(found.all())


def sent_request(gk_mock) -> SecretRequestCreate:
    """The body of the last secret request the core sent."""
    return SecretRequestCreate.model_validate_json(
        gk_mock.secret_requests.calls.last.request.content
    )


_KEY_NEW_BUTTON = re.compile(r'<button[^>]*data-action="key-new"[^>]*\bhx-get="([^"]+)"')


def key_new_button_url(fragment: str) -> str | None:
    """Where the "Add the key" button of a fragment goes, if it has one."""
    found = _KEY_NEW_BUTTON.search(fragment)
    return unescape(found.group(1)) if found else None


def error_of(reply) -> str:
    """The ``error`` event of a reply (exactly one)."""
    (error,) = [event.data for event in reply.events if event.event == "error"]
    return error


def context_lines(system: str) -> list[str]:
    return system.split("\n\nContext\n", 1)[1].splitlines()
