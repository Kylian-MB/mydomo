"""The household's rules on the keys (0.6.0, spec 4.3, 4.7, 5.6): who is on the general key and who
brings their own (``keys_house``, the owner's by default, and it can be given), what an invitee
uses (their inviter's keys, or their own), a person without a key asked for one through an upkeep
turn on the general key (or the fixed message, when the owner does not let the general key serve
for that), and the firm limits that the owner puts on the general key and each member on their own
keys (``keys_house``, ``key_limit``: one row, one audit name).

The chat pilots talk with Domo on a live server (see ``chat_support.py``); its general key is the
owner's and lists ``example-haiku-1`` (small), ``example-sonnet-1`` (medium) and
``example-opus-1`` (strong).
"""

import html
import json
import re
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from chat_support import ChatPilot, Member, dump
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.channels.page.abilities import PAGE
from domo_core.confirmations.describe import describe_type
from domo_core.conversation import execution as execution_module
from domo_core.conversation.prepare import UPKEEP_OUTPUT_TOKENS
from domo_core.household.models import Invitation
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.key_flow import KEY_NEW_PATH
from domo_core.llm.models import Provider, ProviderModel, ProviderUser, new_provider_id
from domo_core.llm.newcomers import newcomers_told
from domo_core.llm.provider_keys import general_when_none
from domo_core.llm.router import text_keys, usable_keys
from domo_core.llm.speech.home_key import HOME_SPEECH_ID
from domo_core.llm.spending_models import KeyNewcomer, ModelCall, SpendingLimit
from domo_core.llm.types import ToolCall
from domo_core.policy.roles import Actor
from domo_core.prompts import FORM_LINE, abilities_line
from domo_core.texts.install import house_key
from domo_core.texts.view import REFERENCE
from domo_core.tools.context import InvalidArguments
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel, is_judge_call
from sqlalchemy import select, text
from test_key_tools import all_keys, context_lines, error_of, facts_judged, key_new_button_url
from test_speech_home import HEARING, SAYING
from test_spending import add_key

# The first sentence of ``prompts/upkeep.md``: an upkeep turn's instructions start with it.
UPKEEP_FIRST_SENTENCE = (
    "You are the assistant. The person you are talking with has no model key that the assistant "
    "may use for them."
)


@pytest.fixture
async def paul_member(add_member) -> Member:
    """An adult of the household who gave no key: "Paul"."""
    return Member(await add_member("Paul", "adult", username="paul"), "paul")


@pytest.fixture
async def paul_chat(chat_server, paul_member) -> AsyncIterator[ChatPilot]:
    """Paul, talking with Domo."""
    async with chat_server.browser(paul_member) as client:
        yield ChatPilot(chat_server, paul_member, client)


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


async def lines_of(chat) -> list[ProviderUser]:
    async with chat.server.session_factory() as reader:
        found = await reader.execute(select(ProviderUser).order_by(ProviderUser.set_at))
        return list(found.scalars().all())


async def limits_of(chat) -> list[SpendingLimit]:
    async with chat.server.session_factory() as reader:
        found = await reader.execute(select(SpendingLimit).order_by(SpendingLimit.set_at))
        return list(found.scalars().all())


async def usable_ids(chat, person: Person) -> list[str]:
    async with chat.server.session_factory() as reader:
        return [key.id for key in await usable_keys(reader, person)]


async def invitation_named(chat, name: str) -> Invitation:
    async with chat.server.session_factory() as reader:
        found = await reader.execute(select(Invitation).where(Invitation.display_name == name))
        return found.scalar_one()


async def deny_on_the_general_key(owner_chat, name: str) -> None:
    reply = await owner_chat.say_with_tool(
        "keys_house", {"person": name, "on_general_key": "deny"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]


# --- who is on the general key -------------------------------------------------------------------


async def test_the_owner_says_paul_brings_his_own_key(owner_chat, paul_chat, last_audit_entry):
    server = owner_chat.server
    await deny_on_the_general_key(owner_chat, "Paul")
    [line] = await lines_of(owner_chat)
    assert (line.provider_id, line.scope, line.person_id, line.allowed, line.set_by) == (
        server.provider_id,
        "person",
        paul_chat.person.id,
        False,
        owner_chat.person.id,
    )
    entry = await last_audit_entry("keys.house_changed")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {
        "changes": {"person": str(paul_chat.person.id), "on_general_key": "deny"}
    }
    assert await usable_ids(paul_chat, paul_chat.person) == []

    # Paul has no key Domo may use for him: his next turn only asks him for one.
    paul_chat.use_model(FakeChatModel(["To talk with me, give a key of your own."]))
    first = len(server.calls)
    answer = await paul_chat.say("hello")
    [call] = server.calls[first:]
    assert call.system.startswith(UPKEEP_FIRST_SENTENCE)
    assert [tool.name for tool in call.tools] == ["key_add"]
    assert (answer.text, answer.status) == ("To talk with me, give a key of your own.", "complete")


async def test_without_upkeep_the_page_shows_the_fixed_message(
    owner_chat, paul_chat, current_settings, last_audit_entry
):
    server = owner_chat.server
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Paul", "on_general_key": "deny", "upkeep_on_general_key": False},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert (await current_settings()).upkeep_on_general is False
    assert (await last_audit_entry("keys.house_changed")).details == {
        "changes": {
            "person": str(paul_chat.person.id),
            "on_general_key": "deny",
            "upkeep_on_general_key": False,
        }
    }
    calls = len(server.calls)
    answer = await paul_chat.post("/chat/messages", data={"text": "hello"})
    assert answer.status_code == 409
    assert t("chat.error.no_key", "en") in plain(answer.text)
    # The fixed message opens the form of a key of his own, and no model is asked.
    assert key_new_button_url(answer.text) == KEY_NEW_PATH
    assert len(server.calls) == calls
    page = await paul_chat.get(KEY_NEW_PATH)
    assert page.status_code == 200 and 'name="kind"' in page.text


async def record_the_home_speech_key(chat) -> None:
    """The house's speech service as its upkeep records it: a key without a secret, given by the
    owner, let to the whole household, listing a model that hears and one that says."""
    async with chat.server.session_factory() as session:
        session.add(
            Provider(
                id=HOME_SPEECH_ID,
                kind="openai_compatible",
                base_url="http://speech:8083/v1",
                secret_ref=None,
                given_by=chat.person.id,
                is_general=False,
            )
        )
        await session.flush()
        session.add(
            ProviderUser(
                provider_id=HOME_SPEECH_ID,
                scope="household",
                person_id=None,
                allowed=True,
                set_by=chat.person.id,
            )
        )
        for model_id in (HEARING, SAYING):
            session.add(ProviderModel(provider_id=HOME_SPEECH_ID, model_id=model_id))
        await session.commit()


async def test_a_person_whose_only_key_is_the_home_speech_service_is_asked_for_one(
    owner_chat, paul_chat
):
    server = owner_chat.server
    await record_the_home_speech_key(owner_chat)
    await deny_on_the_general_key(owner_chat, "Paul")
    # The speech service hears and says for him, but it has no model to talk with.
    assert await usable_ids(paul_chat, paul_chat.person) == [HOME_SPEECH_ID]
    async with server.session_factory() as reader:
        assert await text_keys(reader, paul_chat.person) == []

    paul_chat.use_model(FakeChatModel(["To talk with me, give a key of your own."]))
    first = len(server.calls)
    answer = await paul_chat.say("hello")
    [call] = server.calls[first:]
    assert call.system.startswith(UPKEEP_FIRST_SENTENCE)
    assert [tool.name for tool in call.tools] == ["key_add"]
    assert (answer.text, answer.status) == ("To talk with me, give a key of your own.", "complete")


async def test_removing_the_general_key_beside_the_home_speech_key_warns_it_is_the_last(
    owner_chat, gk_mock
):
    # The speech service hears and says, it never thinks: it is no key that works, and no heir.
    await record_the_home_speech_key(owner_chat)
    general = owner_chat.server.provider_id
    await owner_chat.say_with_tool("key_remove", {"key": general}, judge="warn: hmm")
    assert facts_judged(owner_chat) == {"last_key": True, "general_key_without_heir": True}


async def test_removing_the_general_key_hands_it_to_a_thinking_key_not_the_home_speech_key(
    owner_chat, gk_mock
):
    await record_the_home_speech_key(owner_chat)
    general = owner_chat.server.provider_id
    later = await add_key(owner_chat.server.session_factory, owner_chat.person.id)
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert reply.actions == [{"tool": "key_remove", "status": "done"}]
    assert facts_judged(owner_chat) == {"last_key": False, "general_key_without_heir": False}
    generals = {key.id: key.is_general for key in await all_keys(owner_chat)}
    assert generals == {HOME_SPEECH_ID: False, later.id: True}


async def test_the_house_never_writes_its_texts_on_the_home_speech_key(session, add_person):
    owner = await add_person("Owner", "owner")
    session.add(
        Provider(
            id=HOME_SPEECH_ID,
            kind="openai_compatible",
            base_url="http://speech:8083/v1",
            secret_ref=None,
            given_by=owner.id,
        )
    )
    await session.flush()
    assert await house_key(session) is None
    thinking = Provider(
        id=new_provider_id(),
        kind="openai_compatible",
        base_url="https://llm.example.com/v1",
        secret_ref=None,
        given_by=owner.id,
    )
    session.add(thinking)
    await session.flush()
    found = await house_key(session)
    assert found is not None and found.id == thinking.id


async def test_the_home_speech_key_never_becomes_the_general_key(session, add_person):
    # Whatever its service lists, even a model that is not known to hear or say.
    owner = await add_person("Owner", "owner")
    speech = Provider(
        id=HOME_SPEECH_ID,
        kind="openai_compatible",
        base_url="http://speech:8083/v1",
        secret_ref=None,
        given_by=owner.id,
    )
    session.add(speech)
    await session.flush()
    session.add_all(
        ProviderModel(provider_id=HOME_SPEECH_ID, model_id=m)
        for m in (HEARING, SAYING, "example-unknown-1")
    )
    await session.flush()
    await general_when_none(session, speech)
    assert not speech.is_general


async def test_with_only_the_home_speech_service_and_no_upkeep_the_page_offers_a_key(
    owner_chat, paul_chat
):
    server = owner_chat.server
    await record_the_home_speech_key(owner_chat)
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Paul", "on_general_key": "deny", "upkeep_on_general_key": False},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    page = await paul_chat.get("/chat")
    assert key_new_button_url(page.text) == KEY_NEW_PATH
    calls = len(server.calls)
    answer = await paul_chat.post("/chat/messages", data={"text": "hello"})
    assert answer.status_code == 409
    assert t("chat.error.no_key", "en") in plain(answer.text)
    assert len(server.calls) == calls


async def test_names_read_too_slowly_do_nothing_and_say_it_may_be_tried_again(
    owner_chat, paul_member, monkeypatch, caplog
):
    # The read that gives the people a call names their whole name, before anything is decided,
    # is cut by its deadline: a machine that stalls longer than that (a shared build machine
    # does) must leave nothing done, the model told why, and the next call working.
    real_whole_names = execution_module.whole_names

    async def frozen(session, *args, **kwargs):
        await session.execute(text("SELECT pg_sleep(30)"))
        return await real_whole_names(session, *args, **kwargs)

    monkeypatch.setattr(execution_module, "_CHECK_SECONDS", 1.0)
    monkeypatch.setattr(execution_module, "whole_names", frozen)
    reply = await owner_chat.say_with_tool(
        "keys_house", {"person": "Paul", "on_general_key": "deny"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "failed"}]
    [result] = reply.model_results
    assert (result["status"], result["reason"]) == ("failed", "took_too_long")
    assert result["detail"] == (
        "nothing was done: finding who the call speaks of took too long; you may try again"
    )
    assert "Reading the names of a call of keys_house took too long" in caplog.text
    assert await lines_of(owner_chat) == []

    # The query cut in the middle left no session or connection of the turn's behind.
    monkeypatch.setattr(execution_module, "whole_names", real_whole_names)
    await deny_on_the_general_key(owner_chat, "Paul")
    [line] = await lines_of(owner_chat)
    assert (line.person_id, line.allowed) == (paul_member.person.id, False)


async def test_names_that_cannot_be_read_do_nothing_and_say_it_may_be_tried_again(
    owner_chat, paul_member, monkeypatch
):
    async def broken(*_: object, **__: object) -> None:
        raise OSError("the database is gone")

    monkeypatch.setattr(execution_module, "whole_names", broken)
    reply = await owner_chat.say_with_tool(
        "keys_house", {"person": "Paul", "on_general_key": "deny"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "failed"}]
    [result] = reply.model_results
    assert (result["status"], result["reason"]) == ("failed", "not_read")
    assert result["detail"] == (
        "nothing was done: who the call speaks of could not be read; you may try again"
    )
    assert await lines_of(owner_chat) == []


async def test_house_rules_are_the_owners_and_can_be_given(
    owner_chat, adult_chat, paul_chat, guest_chat, grant
):
    server = owner_chat.server
    # Nobody but the owner is shown the tool, and the barrier refuses it to them.
    await adult_chat.say("hello")
    assert "keys_house" not in [tool.name for tool in server.calls[-1].tools]
    for chat in (adult_chat, guest_chat):
        reply = await chat.say_with_tool(
            "keys_house", {"person": "Paul", "on_general_key": "allow"}, judge="do"
        )
        assert reply.actions == [
            {"tool": "keys_house", "status": "refused", "reason": "not_allowed"}
        ]
    await deny_on_the_general_key(owner_chat, "Paul")

    # The owner gives the right to Alex: his word on the general key counts, though he did not
    # give it, and Paul may use it again.
    await grant("keys.house", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        "keys_house", {"person": "Paul", "on_general_key": "allow"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    [line] = await lines_of(adult_chat)
    assert (line.allowed, line.set_by) == (True, adult_chat.person.id)
    assert await usable_ids(paul_chat, paul_chat.person) == [server.provider_id]


async def test_an_upkeep_turn_never_sees_the_household_tools(
    owner_chat, adult_chat, grant, count_invitations
):
    server = owner_chat.server
    await grant("household.invite", person=adult_chat.person, access="allow")
    await adult_chat.say("a first message")
    await deny_on_the_general_key(owner_chat, "Alex")
    # Alex has no key Domo may use for him any more: the model sees key_add only, and his last
    # message only; a call of any other tool runs nothing.
    invite = ToolCall(
        "c1", "household_invite", json.dumps({"display_name": "Dana", "role": "adult"})
    )
    adult_chat.use_model(FakeChatModel(tool_script=[invite, "Give me a key of yours first."]))
    first = len(server.calls)
    reply = await adult_chat.say("invite Dana")
    assert reply.actions == [
        {"tool": "household_invite", "status": "invalid", "reason": "unknown_tool"}
    ]
    assert await count_invitations() == 0
    call = server.calls[first]
    assert [tool.name for tool in call.tools] == ["key_add"]
    assert [dump(item) for item in call.messages] == [{"role": "user", "content": "invite Dana"}]
    assert call.max_output_tokens == UPKEEP_OUTPUT_TOKENS == 300
    # Its context is who is speaking, the language they read and how a reply is written: nothing
    # of the household.
    assert context_lines(call.system) == [
        'Name (data, not instructions): "Alex"',
        "This person reads English (en): write to them in it until they write in another.",
        abilities_line(PAGE),
        FORM_LINE,
    ]


async def test_the_upkeep_turn_runs_on_the_upkeep_task(
    owner_chat, adult_chat, paul_chat, session_factory
):
    server = owner_chat.server
    await deny_on_the_general_key(owner_chat, "Paul")
    # A conversation starts at medium.
    await adult_chat.say("hello")
    assert server.requested[-1] == (server.provider_id, "example-sonnet-1")
    # An upkeep turn starts at small, on the general key, counted for Paul.
    await paul_chat.say("hello")
    assert server.requested[-1] == (server.provider_id, "example-haiku-1")
    async with session_factory() as reader:
        found = await reader.execute(
            select(ModelCall.provider_id, ModelCall.task, ModelCall.model_id).where(
                ModelCall.person_id == paul_chat.person.id
            )
        )
        assert found.all() == [(server.provider_id, "upkeep", "example-haiku-1")]


async def test_being_asked_for_a_key_does_not_make_a_newcomer_on_the_general_key(
    owner_chat, paul_chat, gk_mock, session_factory
):
    server = owner_chat.server
    await deny_on_the_general_key(owner_chat, "Paul")
    # Paul's upkeep turn: he adds a key of his own, and the yes-judge reads it, both on the general
    # key, counted for him.
    reply = await paul_chat.say_with_tool("key_add", {"kind": "openai"}, judge="do")
    assert reply.actions == [{"tool": "key_add", "status": "done"}]
    async with session_factory() as reader:
        found = await reader.execute(
            select(ModelCall.task).where(
                ModelCall.person_id == paul_chat.person.id,
                ModelCall.provider_id == server.provider_id,
            )
        )
        assert set(found.scalars().all()) == {"judge", "upkeep"}

    # He never used the general key: the owner, who gave it, is not told of him.
    await owner_chat.say("hello")
    assert not any(
        line.startswith("New on a key") for line in context_lines(server.calls[-1].system)
    )
    async with session_factory() as session:
        told = await newcomers_told(
            session,
            giver_id=owner_chat.person.id,
            now=owner_chat.clock(),
            zone=None,
            catalog=shipped_catalog(),
        )
        marked = await session.execute(
            select(KeyNewcomer).where(KeyNewcomer.person_id == paul_chat.person.id)
        )
        assert (told, marked.all()) == ([], [])


async def test_a_limit_on_the_general_key_for_paul_stops_his_upkeep_turns_too(
    owner_chat, paul_chat
):
    server = owner_chat.server
    await deny_on_the_general_key(owner_chat, "Paul")
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Paul", "limit_amount": 0.01, "limit_currency": "USD"},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    answer = await paul_chat.say("hello")
    assert answer.status == "error"
    assert t("chat.error.limit_reached", "en") in html.unescape(error_of(answer))
    assert server.requested[-1] == (server.provider_id, "example-haiku-1")


# --- invitees ------------------------------------------------------------------------------------


async def test_an_invitee_uses_the_inviters_key(adult_chat, grant, join_with_invitation):
    server = adult_chat.server
    alexs = await add_key(server.session_factory, adult_chat.person.id)
    await grant("household.invite", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        "household_invite",
        {"display_name": "Robin", "role": "guest", "uses_my_key": True},
        judge="do",
    )
    assert reply.actions == [{"tool": "household_invite", "status": "done"}]
    invitation = await invitation_named(adult_chat, "Robin")
    assert invitation.uses_inviter_key is True
    robin = await join_with_invitation(invitation.id, "robin")
    # Robin may use Alex's key, before the general key.
    assert await usable_ids(adult_chat, robin) == [alexs.id, server.provider_id]
    [line] = await lines_of(adult_chat)
    assert (line.provider_id, line.scope, line.person_id, line.allowed, line.set_by) == (
        alexs.id,
        "person",
        robin.id,
        True,
        adult_chat.person.id,
    )


async def test_an_invitee_brings_their_own_when_told(owner_chat, join_with_invitation):
    server = owner_chat.server
    reply = await owner_chat.say_with_tool(
        "household_invite",
        {"display_name": "Robin", "role": "guest", "uses_my_key": False},
        judge="do",
    )
    assert reply.actions == [{"tool": "household_invite", "status": "done"}]
    invitation = await invitation_named(owner_chat, "Robin")
    assert invitation.uses_inviter_key is False
    robin = await join_with_invitation(invitation.id, "robin")
    assert await usable_ids(owner_chat, robin) == []
    [line] = await lines_of(owner_chat)
    assert (line.provider_id, line.scope, line.person_id, line.allowed) == (
        server.provider_id,
        "person",
        robin.id,
        False,
    )


async def test_the_house_default_for_invitees(
    owner_chat, join_with_invitation, current_settings, last_audit_entry
):
    server = owner_chat.server
    reply = await owner_chat.say_with_tool("keys_house", {"invitee_default": "own"}, judge="do")
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert (await current_settings()).invitee_default == "own"
    assert (await last_audit_entry("keys.house_changed")).details == {
        "changes": {"invitee_default": "own"}
    }
    # Nothing said at the invitation: the house's default, a key of their own.
    await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Robin", "role": "guest"}, judge="do"
    )
    robin_invitation = await invitation_named(owner_chat, "Robin")
    assert robin_invitation.uses_inviter_key is None
    robin = await join_with_invitation(robin_invitation.id, "robin")
    assert await usable_ids(owner_chat, robin) == []
    # Said at the invitation: it wins over the default (the owner's own key is the general one).
    await owner_chat.say_with_tool(
        "household_invite",
        {"display_name": "Dana", "role": "adult", "uses_my_key": True},
        judge="do",
    )
    dana = await join_with_invitation((await invitation_named(owner_chat, "Dana")).id, "dana")
    assert await usable_ids(owner_chat, dana) == [server.provider_id]


async def invite_and_join(chat, join_with_invitation, name: str, **arguments) -> Person:
    """``chat`` invites ``name`` (a guest, unless ``arguments`` say otherwise), who then joins."""
    reply = await chat.say_with_tool(
        "household_invite", {"display_name": name, "role": "guest", **arguments}, judge="do"
    )
    assert reply.actions == [{"tool": "household_invite", "status": "done"}]
    return await join_with_invitation((await invitation_named(chat, name)).id, name.lower())


async def test_an_invitee_of_the_hosts_keys_follows_them_even_under_own(
    owner_chat, adult_chat, grant, join_with_invitation
):
    """The owner said invitees bring their own, and Alex's invitation says Robin uses Alex's keys:
    Robin uses Alex's key and follows Alex on the general key, with no line of their own there.
    The rule decides only when the invitation says nothing."""
    server = owner_chat.server
    reply = await owner_chat.say_with_tool("keys_house", {"invitee_default": "own"}, judge="do")
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    alexs = await add_key(server.session_factory, adult_chat.person.id)
    await grant("household.invite", person=adult_chat.person, access="allow")
    robin = await invite_and_join(adult_chat, join_with_invitation, "Robin", uses_my_key=True)
    assert await usable_ids(adult_chat, robin) == [alexs.id, server.provider_id]
    assert [line.person_id for line in await lines_of(adult_chat)] == [robin.id]
    # Nothing said at the invitation: the house's default, a key of their own.
    dana = await invite_and_join(adult_chat, join_with_invitation, "Dana")
    assert await usable_ids(adult_chat, dana) == []


async def test_under_never_whoever_joins_is_off_the_general_key_whatever_their_invitation_says(
    owner_chat, adult_chat, grant, join_with_invitation
):
    server = owner_chat.server
    alexs = await add_key(server.session_factory, adult_chat.person.id)
    await grant("household.invite", person=adult_chat.person, access="allow")
    before = await invite_and_join(adult_chat, join_with_invitation, "Dana", uses_my_key=True)
    assert await usable_ids(adult_chat, before) == [alexs.id, server.provider_id]

    reply = await owner_chat.say_with_tool(
        "keys_house", {"invitee_general_key": "never"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    robin = await invite_and_join(adult_chat, join_with_invitation, "Robin", uses_my_key=True)
    assert await usable_ids(adult_chat, robin) == [alexs.id]
    [line] = [
        line
        for line in await lines_of(adult_chat)
        if line.person_id == robin.id and line.provider_id == server.provider_id
    ]
    assert (line.provider_id, line.scope, line.allowed) == (server.provider_id, "person", False)
    # Someone who joined before the rule keeps what they have.
    assert await usable_ids(adult_chat, before) == [alexs.id, server.provider_id]


async def test_under_never_the_general_keys_giver_still_shares_it_with_their_invitee(
    owner_chat, join_with_invitation
):
    server = owner_chat.server
    reply = await owner_chat.say_with_tool(
        "keys_house", {"invitee_general_key": "never"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    robin = await invite_and_join(owner_chat, join_with_invitation, "Robin", uses_my_key=True)
    assert await usable_ids(owner_chat, robin) == [server.provider_id]
    assert await lines_of(owner_chat) == []


async def test_keys_house_sets_the_rule_for_the_next_invitees(
    owner_chat, current_settings, last_audit_entry
):
    reply = await owner_chat.say_with_tool(
        "keys_house", {"invitee_general_key": "never"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert (await current_settings()).invitee_general == "never"
    assert (await last_audit_entry("keys.house_changed")).details == {
        "changes": {"invitee_general_key": "never"}
    }
    assert reply.model_results[-1]["changed"] == {"invitee_general_key": "never"}


@pytest.mark.parametrize(
    ("told", "sentence"),
    [
        (True, "action.describe.household.invite.inviter_key"),
        (False, "action.describe.household.invite.own_key"),
    ],
)
def test_whoever_says_yes_to_an_invitation_reads_whose_keys_the_invitee_uses(told, sentence):
    described = describe_type(
        "household.invite",
        {"display_name": "Robin", "role": "adult", "uses_my_key": told},
        REFERENCE,
    )
    assert described.endswith(t(sentence, "en"))


def test_whoever_says_yes_reads_when_the_invitee_has_the_general_key_too():
    """An invitee of the inviter's keys has the general key too, unless the house's rule is that
    invitees bring their own: whoever says yes reads it."""
    described = describe_type(
        "household.invite",
        {"display_name": "Robin", "role": "adult", "uses_my_key": True},
        REFERENCE,
    )
    assert "the general key" in described


# --- the limits ----------------------------------------------------------------------------------


async def test_a_limit_on_the_general_key_for_one_person(
    owner_chat, adult_chat, paul_chat, last_audit_entry
):
    server = owner_chat.server
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"person": "Paul", "limit_amount": 0.01, "limit_currency": "USD"},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    [limit] = await limits_of(owner_chat)
    assert (
        limit.provider_id,
        limit.person_id,
        limit.amount,
        limit.currency,
        limit.period,
        limit.warn_at_percent,
        limit.set_by,
    ) == (
        server.provider_id,
        paul_chat.person.id,
        Decimal("0.01"),
        "USD",
        "month",
        None,
        owner_chat.person.id,
    )
    assert (await last_audit_entry("spending.limit_set")).details == {
        "provider_id": server.provider_id,
        "person": str(paul_chat.person.id),
        "amount": "0.01",
        "currency": "USD",
        "period": "month",
    }
    # Paul's request would pass it: the fixed message, and no model is asked.
    calls = len(server.calls)
    answer = await paul_chat.say("hello")
    assert answer.status == "error" and len(server.calls) == calls
    assert t("chat.error.limit_reached", "en") in html.unescape(error_of(answer))
    # Alex is not limited.
    adult_chat.use_model(FakeChatModel(["Hello Alex"]))
    assert (await adult_chat.say("hello")).text == "Hello Alex"


async def test_keys_house_sets_a_limit_for_each_guest(owner_chat, last_audit_entry):
    general = owner_chat.server.provider_id
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"limit_amount": 2, "limit_currency": "EUR", "limit_role": "guest", "limit_split": "each"},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    [limit] = await limits_of(owner_chat)
    assert (
        limit.provider_id,
        limit.person_id,
        limit.role,
        limit.split,
        limit.amount,
        limit.currency,
        limit.period,
    ) == (general, None, "guest", "each", Decimal("2"), "EUR", "month")
    assert (await last_audit_entry("spending.limit_set")).details == {
        "provider_id": general,
        "person": None,
        "role": "guest",
        "split": "each",
        "amount": "2",
        "currency": "EUR",
        "period": "month",
    }
    assert (await last_audit_entry("keys.house_changed")).details == {"changes": {"limit": "set"}}
    # Taking it away names the same people.
    reply = await owner_chat.say_with_tool(
        "keys_house",
        {"remove_limit": True, "limit_role": "guest", "limit_split": "each"},
        judge="do",
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert await limits_of(owner_chat) == []
    assert (await last_audit_entry("spending.limit_removed")).details == {
        "provider_id": general,
        "person": None,
        "role": "guest",
        "split": "each",
    }


async def test_a_member_limits_all_the_children_together_on_their_own_key(adult_chat):
    alexs = await add_key(adult_chat.server.session_factory, adult_chat.person.id)
    reply = await adult_chat.say_with_tool(
        "key_limit",
        {"key": alexs.id, "amount": 3, "currency": "USD", "role": "child", "split": "together"},
        judge="do",
    )
    assert reply.actions == [{"tool": "key_limit", "status": "done"}]
    [limit] = await limits_of(adult_chat)
    assert (limit.provider_id, limit.person_id, limit.role, limit.split) == (
        alexs.id,
        None,
        "child",
        "together",
    )
    assert "for" not in reply.model_results[-1]
    assert (reply.model_results[-1]["role"], reply.model_results[-1]["split"]) == (
        "child",
        "together",
    )


async def test_taking_away_the_whole_keys_limit_leaves_the_limit_for_each_guest(owner_chat):
    general = owner_chat.server.provider_id
    for arguments in (
        {"limit_amount": 20, "limit_currency": "EUR"},
        {"limit_amount": 2, "limit_currency": "EUR", "limit_role": "guest", "limit_split": "each"},
        {"remove_limit": True},
    ):
        reply = await owner_chat.say_with_tool("keys_house", arguments, judge="do")
        assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    [left] = await limits_of(owner_chat)
    assert (left.provider_id, left.person_id, left.role, left.split, left.amount) == (
        general,
        None,
        "guest",
        "each",
        Decimal("2"),
    )


async def test_a_house_rule_that_changes_nothing_says_so(owner_chat, audit_actions):
    """Taking away a limit the general key does not have changes nothing: the card says so,
    rather than that the rules changed (followups 11)."""
    reply = await owner_chat.say_with_tool("keys_house", {"remove_limit": True}, judge="do")
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert "keys.house_changed" not in await audit_actions()
    shown = plain(reply.cards_html)
    assert t("action.card.done.keys.house.unchanged", "en") in shown
    assert t("action.card.done.keys.house", "en") not in shown
    # A rule that changes something says so.
    reply = await owner_chat.say_with_tool(
        "keys_house", {"limit_amount": 20, "limit_currency": "EUR"}, judge="do"
    )
    shown = plain(reply.cards_html)
    assert t("action.card.done.keys.house", "en") in shown
    assert t("action.card.done.keys.house.unchanged", "en") not in shown


async def test_both_limit_tools_write_the_same_row_and_audit_the_same_name(
    owner_chat, last_audit_entry, audit_actions
):
    general = owner_chat.server.provider_id
    reply = await owner_chat.say_with_tool(
        "keys_house", {"limit_amount": 20, "limit_currency": "EUR"}, judge="do"
    )
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert (await last_audit_entry("spending.limit_set")).details == {
        "provider_id": general,
        "person": None,
        "amount": "20",
        "currency": "EUR",
        "period": "month",
    }
    # The owner gave the general key: the limit of his own key is the same row.
    reply = await owner_chat.say_with_tool(
        "key_limit",
        {"key": general, "amount": 30, "currency": "EUR", "period": "week", "warn_at_percent": 80},
        judge="do",
    )
    assert reply.actions == [{"tool": "key_limit", "status": "done"}]
    [limit] = await limits_of(owner_chat)
    assert (limit.provider_id, limit.person_id, limit.amount, limit.period) == (
        general,
        None,
        Decimal("30"),
        "week",
    )
    assert limit.warn_at_percent == 80
    assert (await last_audit_entry("spending.limit_set")).details == {
        "provider_id": general,
        "person": None,
        "amount": "30",
        "currency": "EUR",
        "period": "week",
    }
    # Either takes it away.
    reply = await owner_chat.say_with_tool("keys_house", {"remove_limit": True}, judge="do")
    assert reply.actions == [{"tool": "keys_house", "status": "done"}]
    assert await limits_of(owner_chat) == []
    assert (await last_audit_entry("spending.limit_removed")).details == {
        "provider_id": general,
        "person": None,
    }
    assert (await audit_actions()).count("spending.limit_set") == 2


async def test_a_member_limits_their_own_key(adult_chat, guest_chat, last_audit_entry):
    server = adult_chat.server
    alexs = await add_key(server.session_factory, adult_chat.person.id)
    reply = await adult_chat.say_with_tool(
        "key_limit",
        {"key": alexs.id, "amount": 5, "currency": "USD", "person": "Robin"},
        judge="do",
    )
    assert reply.actions == [{"tool": "key_limit", "status": "done"}]
    [limit] = await limits_of(adult_chat)
    assert (limit.provider_id, limit.person_id, limit.amount, limit.currency, limit.set_by) == (
        alexs.id,
        guest_chat.person.id,
        Decimal("5"),
        "USD",
        adult_chat.person.id,
    )
    assert (await last_audit_entry("spending.limit_set")).actor == f"person:{adult_chat.person.id}"
    # Not on a key someone else gave: that limit is not his to set.
    reply = await adult_chat.say_with_tool(
        "key_limit", {"key": server.provider_id, "amount": 1, "currency": "USD"}, judge="do"
    )
    assert reply.actions == [{"tool": "key_limit", "status": "invalid", "reason": "not_possible"}]
    # A guest limits a key of their own too.
    robins = await add_key(server.session_factory, guest_chat.person.id)
    reply = await guest_chat.say_with_tool(
        "key_limit",
        {"key": robins.id, "amount": 2, "currency": "EUR", "period": "day"},
        judge="do",
    )
    assert reply.actions == [{"tool": "key_limit", "status": "done"}]
    assert {(row.provider_id, row.period) for row in await limits_of(guest_chat)} == {
        (alexs.id, "month"),
        (robins.id, "day"),
    }


# --- what the tools refuse -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        # No change at all, or one that makes no sense: a mistake of the call.
        ({}, "invalid"),
        ({"person": "Alex"}, "invalid"),
        ({"on_general_key": "deny"}, "invalid"),
        ({"person": "Alex", "on_general_key": "maybe"}, "invalid"),
        ({"invitee_default": "anyone"}, "invalid"),
        ({"invitee_general_key": "sometimes"}, "invalid"),
        ({"upkeep_on_general_key": "yes"}, "invalid"),
        ({"limit_amount": 0, "limit_currency": "USD"}, "invalid"),
        # Under the smallest step a limit keeps (the schema's minimum).
        ({"limit_amount": 0.00005, "limit_currency": "USD"}, "invalid"),
        ({"limit_amount": True, "limit_currency": "USD"}, "invalid"),
        ({"limit_amount": 5}, "invalid"),
        ({"limit_amount": 5, "limit_currency": "GBP"}, "invalid"),
        ({"limit_amount": 5, "limit_currency": "USD", "limit_period": "year"}, "invalid"),
        ({"limit_amount": 5, "limit_currency": "USD", "warn_at_percent": 101}, "invalid"),
        ({"limit_currency": "USD"}, "invalid"),
        ({"limit_amount": 5, "limit_currency": "USD", "remove_limit": True}, "invalid"),
        ({"remove_limit": False}, "invalid"),
        # A role: not with a person, not without its split (nor the split without it), and
        # with an amount or a removal.
        (
            {
                "person": "Alex",
                "limit_role": "guest",
                "limit_split": "each",
                "limit_amount": 5,
                "limit_currency": "USD",
            },
            "invalid",
        ),
        ({"limit_split": "each", "limit_amount": 5, "limit_currency": "USD"}, "invalid"),
        ({"limit_role": "guest", "limit_amount": 5, "limit_currency": "USD"}, "invalid"),
        ({"limit_role": "guest", "limit_split": "each"}, "invalid"),
        ({"limit_role": "owner", "limit_split": "each", "remove_limit": True}, "invalid"),
        ({"limit_role": "guest", "limit_split": "some", "remove_limit": True}, "invalid"),
        ({"person": "Alex", "on_general_key": "deny", "key": "p-00000000"}, "invalid"),
        # Right calls that the household refuses.
        ({"person": "Nobody", "on_general_key": "deny"}, "not_found"),
        ({"person": "Owner", "on_general_key": "deny"}, "not_possible"),
    ],
)
async def test_keys_house_refuses_what_it_cannot_do(tool_context, adult_member, arguments, code):
    actor = Actor(tool_context.person.id, "owner")
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["keys_house"].describe(arguments, actor, tool_context)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"amount": 5, "currency": "USD"}, "invalid"),
        ({"key": "general"}, "invalid"),
        ({"key": "general", "amount": 5}, "invalid"),
        ({"key": "general", "amount": -1, "currency": "USD"}, "invalid"),
        ({"key": "general", "amount": 0.00005, "currency": "USD"}, "invalid"),
        ({"key": "general", "amount": 5, "currency": "USD", "remove": True}, "invalid"),
        ({"key": "general", "remove": True, "period": "day"}, "invalid"),
        ({"key": "general", "amount": 5, "currency": "USD", "warn_at_percent": 0}, "invalid"),
        (
            {
                "key": "general",
                "amount": 5,
                "currency": "USD",
                "role": "guest",
                "split": "each",
                "person": "Alex",
            },
            "invalid",
        ),
        ({"key": "general", "amount": 5, "currency": "USD", "split": "each"}, "invalid"),
        ({"key": "general", "amount": 5, "currency": "USD", "role": "guest"}, "invalid"),
        ({"key": "general", "role": "guest", "split": "each"}, "invalid"),
        ({"key": "p-00000000", "amount": 5, "currency": "USD"}, "not_found"),
        ({"key": "general", "amount": 5, "currency": "USD", "person": "Nobody"}, "not_found"),
    ],
)
async def test_key_limit_refuses_what_it_cannot_do(tool_context, chat_server, arguments, code):
    if arguments.get("key") == "general":
        arguments = {**arguments, "key": chat_server.provider_id}
    actor = Actor(tool_context.person.id, "owner")
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["key_limit"].describe(arguments, actor, tool_context)
    assert raised.value.code == code


# --- what whoever says yes reads -----------------------------------------------------------------

KEY = "p-1234abcd"
SAMS_KEY = {"key_kind": "openai", "key_of": "Sam"}


@pytest.mark.parametrize(
    ("arguments", "facts", "sentence"),
    [
        (
            {
                "key": KEY,
                "amount": 5,
                "currency": "EUR",
                "period": "week",
                "person": "Robin",
                "warn_at_percent": 80,
            },
            SAMS_KEY,
            "Limit what the OpenAI key of “Sam” (p-1234abcd) spends for “Robin” to €5.00 per "
            "week. Warn at 80% of it.",
        ),
        (
            {"key": KEY, "amount": 0.5, "currency": "USD"},
            SAMS_KEY,
            "Limit what the OpenAI key of “Sam” (p-1234abcd) spends to $0.50 per month.",
        ),
        # The amount as the limit keeps it: four decimals.
        (
            {"key": KEY, "amount": 5.123456, "currency": "USD"},
            SAMS_KEY,
            "Limit what the OpenAI key of “Sam” (p-1234abcd) spends to $5.1235 per month.",
        ),
        (
            {"key": KEY, "remove": True},
            SAMS_KEY,
            "Take away the spending limit of the OpenAI key of “Sam” (p-1234abcd).",
        ),
        (
            {"key": KEY, "remove": True, "person": "Robin"},
            SAMS_KEY,
            "Take away the spending limit of the OpenAI key of “Sam” (p-1234abcd) for “Robin”.",
        ),
        (
            {"key": KEY, "amount": 2, "currency": "EUR", "role": "guest", "split": "each"},
            SAMS_KEY,
            "Limit what the OpenAI key of “Sam” (p-1234abcd) spends for each guest to €2.00 per "
            "month.",
        ),
        (
            {
                "key": KEY,
                "amount": 5,
                "currency": "USD",
                "period": "day",
                "role": "child",
                "split": "together",
            },
            SAMS_KEY,
            "Limit what the OpenAI key of “Sam” (p-1234abcd) spends for all the children together "
            "to $5.00 per day.",
        ),
        (
            {"key": KEY, "amount": 5, "currency": "USD", "role": "adult", "split": "together"},
            SAMS_KEY,
            "Limit what the OpenAI key of “Sam” (p-1234abcd) spends for all the adults together "
            "to $5.00 per month.",
        ),
        (
            {"key": KEY, "remove": True, "role": "guest", "split": "each"},
            SAMS_KEY,
            "Take away the spending limit of the OpenAI key of “Sam” (p-1234abcd) for each guest.",
        ),
        (
            {"key": KEY, "remove": True, "role": "guest", "split": "together"},
            SAMS_KEY,
            "Take away the spending limit of the OpenAI key of “Sam” (p-1234abcd) for all the "
            "guests together.",
        ),
        (
            {"key": KEY, "remove": True, "role": "child", "split": "each"},
            SAMS_KEY,
            "Take away the spending limit of the OpenAI key of “Sam” (p-1234abcd) for each child.",
        ),
        (
            {"key": KEY, "remove": True, "role": "adult", "split": "together"},
            SAMS_KEY,
            "Take away the spending limit of the OpenAI key of “Sam” (p-1234abcd) for all the "
            "adults together.",
        ),
        # A step checked in its turn has no described action: the key by its id.
        (
            {"key": KEY, "amount": 5, "currency": "EUR"},
            None,
            "Limit what the key p-1234abcd spends to €5.00 per month.",
        ),
    ],
)
def test_whoever_says_yes_to_a_limit_reads_all_of_it(arguments, facts, sentence):
    # Its amount as the reader reads an amount (here, in English): its currency with it.
    assert describe_type("spending.limit", arguments, REFERENCE, facts) == sentence


@pytest.mark.parametrize(
    ("arguments", "sentence"),
    [
        (
            {"person": "Paul", "on_general_key": "deny"},
            "Change the rules of the household's keys. “Paul” brings a key of their own, and no "
            "longer uses the general key.",
        ),
        (
            {"person": "Paul", "on_general_key": "allow"},
            "Change the rules of the household's keys. “Paul” may use the general key.",
        ),
        (
            {"invitee_default": "own", "upkeep_on_general_key": False},
            "Change the rules of the household's keys. An invitee brings a key of their own, "
            "unless their invitation says otherwise. The general key no longer serves to ask "
            "someone without a key for one: the page shows them a message instead.",
        ),
        (
            {"invitee_general_key": "follow"},
            "Change the rules of the household's keys. Invitees who join from now on use the "
            "general key when whoever invites them does.",
        ),
        (
            {"invitee_general_key": "never"},
            "Change the rules of the household's keys. Invitees who join from now on do not use "
            "the general key, even when whoever invites them does.",
        ),
        (
            {"invitee_default": "inviter", "upkeep_on_general_key": True},
            "Change the rules of the household's keys. An invitee uses the keys of whoever "
            "invites them, unless their invitation says otherwise. The general key serves to ask "
            "someone without a key for one.",
        ),
        (
            {
                "person": "Paul",
                "limit_amount": 20,
                "limit_currency": "EUR",
                "warn_at_percent": 50,
            },
            "Change the rules of the household's keys. Limit what the general key spends for "
            "“Paul” to €20.00 per month. Warn at 50% of it.",
        ),
        (
            {
                "limit_amount": 2,
                "limit_currency": "EUR",
                "limit_role": "guest",
                "limit_split": "together",
            },
            "Change the rules of the household's keys. Limit what the general key spends for all "
            "the guests together to €2.00 per month.",
        ),
        (
            {"remove_limit": True, "limit_role": "guest", "limit_split": "each"},
            "Change the rules of the household's keys. Take away the spending limit of the "
            "general key for each guest.",
        ),
        (
            {"remove_limit": True},
            "Change the rules of the household's keys. Take away the spending limit of the "
            "general key.",
        ),
    ],
)
def test_whoever_says_yes_to_the_house_rules_reads_each_change(arguments, sentence):
    assert describe_type("keys.house", arguments, REFERENCE) == sentence


@pytest.mark.parametrize(
    ("action_type", "arguments"),
    [
        ("keys.house", {"person": "Paul", "on_general_key": "maybe"}),
        ("keys.house", {}),
        ("spending.limit", {"key": KEY, "amount": "lots", "currency": "EUR"}),
        ("spending.limit", {"key": "not a key", "amount": 5, "currency": "EUR"}),
        ("spending.limit", {"key": KEY}),
        # A role goes with its split, and a split with its role.
        ("spending.limit", {"key": KEY, "amount": 5, "currency": "EUR", "role": "guest"}),
        ("spending.limit", {"key": KEY, "amount": 5, "currency": "EUR", "split": "each"}),
        ("spending.limit", {"key": KEY, "remove": True, "split": "together"}),
        ("keys.house", {"limit_amount": 2, "limit_currency": "EUR", "limit_split": "together"}),
    ],
)
def test_a_call_that_does_not_read_gets_the_general_sentence(action_type, arguments):
    assert describe_type(action_type, arguments, REFERENCE) == t("action.describe", "en")


async def test_the_limit_a_child_asks_for_is_read_whole_by_them_the_adult_and_the_judge(
    child_chat, adult_chat
):
    server = child_chat.server
    sams = await add_key(server.session_factory, child_chat.person.id)
    first = len(server.calls)
    reply = await child_chat.say_with_tool(
        "key_limit", {"key": sams.id, "amount": 5, "currency": "USD", "warn_at_percent": 80}
    )
    assert reply.actions == [{"tool": "key_limit", "status": "waiting"}]
    sentence = (
        f"Limit what the Other server (OpenAI-compatible) key of “Sam” ({sams.id}) spends to "
        "$5.00 per month. Warn at 80% of it."
    )
    # The one who asked reads the amount they confirm, the adult who agrees reads all of it.
    assert sentence in plain(reply.cards_html)
    assert sentence in plain((await adult_chat.get("/approvals")).text)
    # And so does the yes judge.
    [judged] = [call for call in server.calls[first:] if is_judge_call(call.system)]
    assert sentence in json.dumps([dump(item) for item in judged.messages], ensure_ascii=False)


async def test_a_limit_is_described_with_the_key_it_is_on(tool_context, chat_server):
    action = await TOOLS["key_limit"].describe(
        {"key": chat_server.provider_id, "amount": 5, "currency": "USD"},
        Actor(tool_context.person.id, "owner"),
        tool_context,
    )
    assert dict(action.facts) == {"key_kind": "openai_compatible", "key_of": "Owner"}
