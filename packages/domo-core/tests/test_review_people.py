"""The quiet review has the same gestures on people as a turn (spec 6.6, Task 20).

A review (``memory.review``) is offered every gesture on people and every look at them the
assistant holds on his own, but forgetting someone and the owner's directives. Each such call is
his own idea, with its reason, born of the person reviewed: it goes the way of any call he marked
(``handle_tool_call``, with no reply), judged as him; nothing is shown on any channel, its audit
lines and its act are written as in a turn, a request it leaves waits as news for whoever answers
it, and a link it makes is never shown. The reviews run on the live server of the chat pilots."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, ChatServer, last_audit
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.chat.models import Message, MessageAction
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.store import conversation_made_in, pending_for
from domo_core.conversation import execution as execution_module
from domo_core.conversation.channel import Abilities
from domo_core.conversation.events import Event
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.models import Invitation
from domo_core.household.service import disable
from domo_core.llm.types import ToolCall, ToolResult
from domo_core.memory import review as review_module
from domo_core.memory.review import (
    MEMORY_TOOLS,
    PEOPLE_REVIEW_TOOLS,
    REVIEW_TOOLS,
    people_review_tools,
    review,
)
from domo_core.people import directives, records
from domo_core.people.acts import AUDIT_ACTION
from domo_core.people.models import PersonAct
from domo_core.policy import catalog
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import memory_review_prompt
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import func, select
from test_concerned_person import _turn, _yes
from test_memory_review import QUIET, deps_of, review_calls
from test_own_idea import Judged, mark

REASON = "they said so in the conversation"
DOUBT = Judgement("yes", reason="I am not sure of it", noted="it looks odd")

# The tools a review is given beside its memory (the brief's list, in no order).
GESTURES = {
    "record_open",
    "record_complete",
    "record_look",
    "record_erase",
    "link_note",
    "link_drop",
    "records_merge",
    "records_split",
    "access_mistake",
    "role_create",
    "role_change",
    "role_delete",
    "roles_look",
    "access_promise",
    "promise_keep",
    "promise_drop",
    "promises_look",
    "household_invite",
    "invitation_link_again",
    "household_cancel_invitation",
    "invitation_send",
    "household_change_role",
    "household_remove",
    "household_bring_back",
}


@pytest.fixture(autouse=True)
def judged(monkeypatch: pytest.MonkeyPatch) -> Judged:
    judge = Judged()
    monkeypatch.setattr(execution_module, "judge", judge)
    return judge


_calls = iter(range(1, 1_000_000))


def call(tool: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(f"people-{next(_calls)}", tool, json.dumps(arguments))


def his(tool: str, /, **arguments: Any) -> ToolCall:
    """A call of the review's, with its reason."""
    return call(tool, **arguments, reason=REASON)


async def reviewed(chat: ChatPilot, *calls: ToolCall, deps: Any = None) -> list[dict[str, Any]]:
    """A review of ``chat``'s person whose model makes ``calls``, one a round, then says done;
    what each call gave back, in order."""
    server = chat.server
    since = len(server.calls)
    chat.use_model(FakeChatModel(tool_script=[*calls, "done"]))
    chat.clock.advance(QUIET)
    assert await review(deps or deps_of(server), chat.person.id, chat.clock()) == "done"
    return results_of(server, since)


def results_of(server: ChatServer, since: int) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for made in review_calls(server, since):
        for item in made.messages:
            if isinstance(item, ToolResult):
                seen.setdefault(item.call_id, json.loads(item.content))
    return list(seen.values())


async def known(server: ChatServer, *names: str, **values: Any) -> dict[str, Person]:
    """Records the assistant knows by name, with no account (``known``, visiting)."""
    columns: dict[str, Any] = {
        "role": "guest",
        "state": "known",
        "standing": "visiting",
        "origin": "domo",
        "origin_reason": "they were spoken of",
    } | values
    async with server.session_factory() as writer:
        made = {name: Person(display_name=name, **columns) for name in names}
        writer.add_all(made.values())
        await writer.commit()
    return made


async def person(server: ChatServer, person_id: uuid.UUID) -> Person:
    async with server.session_factory() as reader:
        found = await reader.get(Person, person_id)
    assert found is not None
    return found


async def acts(server: ChatServer, act: str) -> list[PersonAct]:
    async with server.session_factory() as reader:
        return list(await reader.scalars(select(PersonAct).where(PersonAct.act == act)))


async def count(server: ChatServer, model: Any) -> int:
    async with server.session_factory() as reader:
        return await reader.scalar(select(func.count()).select_from(model)) or 0


# --- what a review does on people ----------------------------------------------------------------


async def test_a_review_opens_a_record_from_what_was_said(owner_chat: ChatPilot):
    server = owner_chat.server
    owner = owner_chat.person
    await owner_chat.say("Dana moves in with us next week.")
    messages, actions = await count(server, Message), await count(server, MessageAction)
    heard: list[Event] = []

    async def hear(reply_id: uuid.UUID, event: Event) -> None:
        heard.append(event)

    everything = Abilities(True, True, True, True, frozenset())
    close = server.app.state.listeners.open_hearing(owner.id, hear, everything)
    try:
        [opened] = await reviewed(
            owner_chat,
            his("record_open", name="Dana", standing="lives_here", ground="will_come"),
        )
    finally:
        close()
    assert opened["status"] == "done", opened
    async with server.session_factory() as reader:
        [dana] = await reader.scalars(select(Person).where(Person.display_name == "Dana"))
    assert (dana.state, dana.origin, dana.origin_reason) == ("known", "domo", REASON)
    [act] = await acts(server, "opened")
    assert (act.by, act.turn_of, act.reason) == ("domo", owner.id, REASON)
    async with server.session_factory() as session:
        line = await last_audit(session, AUDIT_ACTION["opened"])
        decision = await last_audit(session, "policy.decision")
    assert line.actor == "domo" and line.details["act_id"] == str(act.id)  # type: ignore[index]
    assert decision.actor == "domo"
    # No message, no action row, nothing said to whoever listens.
    assert (await count(server, Message), await count(server, MessageAction)) == (
        messages,
        actions,
    )
    assert heard == []


async def test_a_guests_review_judges_as_the_assistant_and_tells_the_judge_whom_it_serves(
    guest_chat: ChatPilot, owner_chat: ChatPilot, judged: Judged, monkeypatch: pytest.MonkeyPatch
):
    server = guest_chat.server
    guest = guest_chat.person
    await known(server, "Tom")
    await guest_chat.say("My friend Tom comes tonight, and I stay a little longer.")
    deciding: list[Actor] = []
    decide = execution_module.decide_now

    async def decided_for(session: Any, actor: Actor, *args: Any, **kwargs: Any) -> Any:
        deciding.append(actor)
        return await decide(session, actor, *args, **kwargs)

    monkeypatch.setattr(execution_module, "decide_now", decided_for)
    friend = his("household_change_role", person="Tom", role="guest", guest_days=1)
    themselves = his(
        "household_change_role", person=f"person:{guest.id}", role="guest", guest_days=3
    )
    results = await reviewed(guest_chat, friend, themselves)
    assert [r["status"] for r in results] == ["done", "done"], results
    # Judged as the assistant, with his rights (the guest's own decision counts their right as
    # given, as for any idea of his in their turn).
    assert {actor.subject for actor in deciding[::2]} == {"domo"}
    first, second = judged.cases
    assert (first.idea.of, second.idea.of) == ("domo", "domo")
    assert first.action["facts"]["serves_the_speaker"] is False  # type: ignore[index]
    assert second.action["facts"]["serves_the_speaker"] is True  # type: ignore[index]

    # The owner's deny for that guest on that type refuses both before any judgement (S-C).
    async with server.session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=guest.id,
                action_type="household.change_role",
                access="deny",
                summary="Not for this guest.",
                owner_words="never for them",
                created_by=owner_chat.person.id,
            )
        )
        await writer.commit()
    await guest_chat.say("Tom stays another night.")
    again = await reviewed(guest_chat, friend, themselves)
    assert [(r["status"], r["reason"]) for r in again] == [("refused", "not_allowed")] * 2
    assert len(judged.cases) == 2


async def test_a_review_under_no_new_people_opens_nothing(owner_chat: ChatPilot):
    server = owner_chat.server
    async with server.session_factory() as writer:
        owner = await writer.get(Person, owner_chat.person.id)
        assert owner is not None
        await directives.set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=owner_chat.clock(),
        )
        await writer.commit()
    await owner_chat.say("Paul is coming on Sunday.")
    before = await count(server, Person)
    [refused] = await reviewed(
        owner_chat, his("record_open", name="Paul", standing="visiting", ground="will_come")
    )
    assert (refused["status"], refused["reason"]) == ("refused", "owner_directive")
    assert await count(server, Person) == before


async def test_the_review_has_every_gesture_the_assistant_holds_on_his_own(
    owner_chat: ChatPilot, monkeypatch: pytest.MonkeyPatch
):
    server = owner_chat.server
    await owner_chat.say("Hello there.")
    since = len(server.calls)
    await reviewed(owner_chat)
    offered = {tool.name for tool in review_calls(server, since)[0].tools}
    types = (catalog.PEOPLE_GESTURES | catalog.PEOPLE_READ_ACTIONS) - {"people.forget"}
    expected = {name for name, tool in TOOLS.items() if tool.action_type in types}
    assert offered - MEMORY_TOOLS == expected == PEOPLE_REVIEW_TOOLS == GESTURES
    assert REVIEW_TOOLS == MEMORY_TOOLS | GESTURES

    # A gesture the catalog adds joins a review by itself, nothing written in the review.
    class NewGesture:
        definition = TOOLS["record_open"].definition
        action_type = "people.new_gesture"

    registry: Mapping[str, Any] = {**TOOLS, "new_gesture": NewGesture()}
    monkeypatch.setattr(
        catalog, "PEOPLE_GESTURES", catalog.PEOPLE_GESTURES | {"people.new_gesture"}
    )
    assert people_review_tools(registry) == GESTURES | {"new_gesture"}


async def test_only_forgetting_and_the_directives_stay_out_of_a_review():
    near = (
        catalog.PEOPLE_GESTURES
        | catalog.PEOPLE_READ_ACTIONS
        | catalog.OWNER_DIRECTIVE_ACTIONS
        | {"people.forget_me"}
    )
    left_out = {
        name
        for name, tool in TOOLS.items()
        if tool.action_type in near and name not in REVIEW_TOOLS
    }
    assert left_out == {
        "forget_me",
        "record_forget",
        "directive_set",
        "directive_lift",
        "directives_look",
    }


async def test_every_act_of_a_review_carries_a_reason(owner_chat: ChatPilot):
    server = owner_chat.server
    await known(server, "Tom")
    await owner_chat.say("Paul is coming on Sunday, with Tom.")
    before = await count(server, Person)
    without, look = await reviewed(
        owner_chat,
        call("record_open", name="Paul", standing="visiting", ground="will_come"),
        # A look has no reason: it is no act.
        call("record_look", person="Tom"),
    )
    assert (without["status"], without["reason"]) == ("invalid", "invalid")
    assert look["status"] != "invalid", look
    assert await count(server, Person) == before


# --- invitations and links -----------------------------------------------------------------------


async def test_a_review_invites_and_the_link_is_never_shown_there(
    owner_chat: ChatPilot, monkeypatch: pytest.MonkeyPatch
):
    # Imported here: the launches run on the fakes of these test modules.
    from test_invitation_send import MAILER, OPERATIONS, posting
    from test_madetools_handover import add_tool
    from test_madetools_out import FakeOut, going_out, opened

    server = owner_chat.server
    owner = owner_chat.person
    await known(server, "Dana", role="adult", standing="lives_here")
    await owner_chat.say("Dana lives with us now and has no way in yet.")
    [invited] = await reviewed(owner_chat, his("household_invite", person="Dana", role="adult"))
    assert invited["status"] == "done", invited
    assert invited["link"] == "waits_for_them"
    async with server.session_factory() as reader:
        [invitation] = await reader.scalars(select(Invitation))
    assert (invitation.by_domo, invitation.created_by) == (True, None)
    assert (invitation.link_for_id, invitation.token_hash) == (owner.id, None)

    # Then sent with one of his tools: the link goes out, never shown to anyone here.
    program, _ = posting()
    gatekeeper = FakeOut()
    await add_tool(server.session_factory, MAILER, OPERATIONS)
    await opened(server.session_factory, MAILER)
    launch = going_out(server.session_factory, owner_chat.clock, {MAILER: program}, gatekeeper)
    monkeypatch.setattr(
        review_module,
        "made_tool_deps",
        lambda deps: replace(launch, house_origin="https://home.example"),
    )
    send = his(
        "invitation_send",
        person="Dana",
        tool=MAILER,
        operation="send",
        arguments={"to": "dana@example.org", "text": "Your way in: {link}"},
    )
    await owner_chat.say("Please send it to Dana.")
    [sent] = await reviewed(owner_chat, send)
    assert sent == {"status": "done", "sent": True}
    [act] = await acts(server, "link_sent")
    assert (act.by, act.reason) == ("domo", REASON)
    body = gatekeeper.sent[0].body_b64
    assert body is not None


# --- what he gave, he takes back ------------------------------------------------------------------


async def test_a_review_removes_an_access_the_assistant_gave(owner_chat: ChatPilot, judged: Judged):
    server = owner_chat.server
    tom = (await known(server, "Tom"))["Tom"]
    async with server.session_factory() as writer:
        record = await writer.get(Person, tom.id)
        assert record is not None
        await records.give_access(
            writer,
            record,
            role="guest",
            made_role=None,
            standing=None,
            guest_days=2,
            by="domo",
            by_owner=False,
            reason="Tom came for the weekend",
            turn_of=None,
            now=owner_chat.clock(),
        )
        await writer.commit()
    await owner_chat.say("Tom went home this morning.")
    [removed] = await reviewed(owner_chat, his("household_remove", person="Tom"))
    assert removed["status"] == "done", removed
    assert (await person(server, tom.id)).state == "gone"
    [case] = judged.cases
    assert case.action["facts"]["given_by_domo"] is True  # type: ignore[index]
    [act] = await acts(server, "access_removed")
    assert (act.by, act.reason) == ("domo", REASON)
    async with server.session_factory() as session:
        assert (await last_audit(session, AUDIT_ACTION["access_removed"])).actor == "domo"


async def test_what_the_owner_gave_is_not_given_by_the_assistant(
    owner_chat: ChatPilot, judged: Judged
):
    server = owner_chat.server
    tom = (await known(server, "Tom"))["Tom"]
    async with server.session_factory() as writer:
        record = await writer.get(Person, tom.id)
        assert record is not None
        await records.give_access(
            writer,
            record,
            role="guest",
            made_role=None,
            standing=None,
            guest_days=2,
            by=f"person:{owner_chat.person.id}",
            by_owner=True,
            reason=None,
            turn_of=owner_chat.person.id,
            now=owner_chat.clock(),
        )
        await writer.commit()
    await owner_chat.say("Tom went home this morning.")
    await reviewed(owner_chat, his("household_remove", person="Tom"))
    [case] = judged.cases
    # No code refusal: the judge weighs that the access was the owner's.
    assert case.action["facts"]["given_by_domo"] is False  # type: ignore[index]


async def test_a_review_cannot_bring_back_what_the_owner_withdrew(owner_chat: ChatPilot):
    server = owner_chat.server
    dana = (await known(server, "Dana", role="adult", standing="lives_here"))["Dana"]
    async with server.session_factory() as writer:
        owner = await writer.get(Person, owner_chat.person.id)
        record = await writer.get(Person, dana.id)
        assert owner is not None and record is not None
        await directives.set_directive(
            writer,
            kind="withdraw",
            person=record,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=owner_chat.clock(),
        )
        await writer.commit()
    await owner_chat.say("Dana would like to come back.")
    [refused] = await reviewed(owner_chat, his("household_bring_back", person="Dana", role="adult"))
    assert (refused["status"], refused["reason"]) == ("refused", "owner_directive")
    assert (await person(server, dana.id)).state == "withdrawn"


# --- a request it leaves -------------------------------------------------------------------------


async def _doubted_change(chat: ChatPilot, monkeypatch: pytest.MonkeyPatch) -> Confirmation:
    """A review of ``chat``'s person whose judge doubts making Tom a guest: the request it left."""
    monkeypatch.setattr(execution_module, "judge", Judged(DOUBT))
    await known(chat.server, "Tom")
    await chat.say("Tom comes tonight.")
    [waiting] = await reviewed(chat, his("household_change_role", person="Tom", role="guest"))
    assert waiting["status"] == "waiting_for_confirmation", waiting
    async with chat.server.session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    return request


async def test_a_doubt_in_the_owner_s_review_waits_for_him_as_news(
    owner_chat: ChatPilot, monkeypatch: pytest.MonkeyPatch
):
    server = owner_chat.server
    owner = owner_chat.person
    request = await _doubted_change(owner_chat, monkeypatch)
    ctx, _, _ = await _turn(server.session_factory, owner)
    assert (request.concerned_id, request.requester_id) == (owner.id, owner.id)
    assert (request.message_id, request.conversation_id) == (None, ctx.conversation_id)
    assert (await person(server, owner.id)).news_at is not None
    async with server.session_factory() as reader:
        settings = await load_policy_settings(reader)
        # What his next turn reads as waiting for his answer.
        shown = await pending_for(
            reader, owner.id, ctx.conversation_id, settings, owner_chat.clock()
        )
        assert [r.id for r in shown] == [request.id]
        assert await conversation_made_in(reader, request) == ctx.conversation_id
    outcome, _ = await _yes(ctx, request, owner)
    assert outcome == "executed"
    async with server.session_factory() as reader:
        [tom] = await reader.scalars(select(Person).where(Person.display_name == "Tom"))
    assert tom.state == "has_access"


async def test_a_doubt_in_an_adult_s_review_waits_for_the_owner_and_runs_after_his_yes(
    adult_chat: ChatPilot, owner_chat: ChatPilot, monkeypatch: pytest.MonkeyPatch
):
    server = adult_chat.server
    owner, adult = owner_chat.person, adult_chat.person
    request = await _doubted_change(adult_chat, monkeypatch)
    owners, _, _ = await _turn(server.session_factory, owner)
    adults, _, _ = await _turn(server.session_factory, adult)
    # It waits in the owner's conversation, as news for him; made in the adult's review, it is
    # the owner's to run (final review C I-2: the adult may have left when the yes comes).
    assert (request.concerned_id, request.requester_id) == (owner.id, owner.id)
    assert request.conversation_id == owners.conversation_id
    assert (await person(server, owner.id)).news_at is not None
    async with server.session_factory() as reader:
        settings = await load_policy_settings(reader)
        shown = await pending_for(
            reader, owner.id, owners.conversation_id, settings, owner_chat.clock()
        )
        assert [r.id for r in shown] == [request.id]
        assert await conversation_made_in(reader, request) == owners.conversation_id
    assert adults.conversation_id != owners.conversation_id
    outcome, _ = await _yes(owners, request, owner)
    assert outcome == "executed"
    async with server.session_factory() as reader:
        [tom] = await reader.scalars(select(Person).where(Person.display_name == "Tom"))
    assert tom.state == "has_access"


# --- the last review of someone who left ---------------------------------------------------------


async def test_the_last_review_of_someone_who_left_can_still_open_a_record(
    owner_chat: ChatPilot, child_chat: ChatPilot
):
    server = owner_chat.server
    child = child_chat.person
    await child_chat.say("My friend Tom comes to every birthday.")
    async with server.session_factory() as writer:
        record = await writer.get(Person, child.id)
        assert record is not None
        await disable(writer, record, owner_chat.clock(), by=owner_chat.person.id)
        await writer.commit()
    [opened] = await reviewed(
        child_chat, his("record_open", name="Tom", standing="elsewhere", ground="matters")
    )
    assert opened["status"] == "done", opened
    [act] = await acts(server, "opened")
    assert (act.by, act.turn_of) == ("domo", child.id)


async def test_only_a_review_s_calls_outlive_the_person_who_left(owner_chat: ChatPilot):
    server = owner_chat.server
    tom = (await known(server, "Tom", standing="lives_here"))["Tom"]
    # Paul, a guest whose stay is over: no longer in the household.
    async with server.session_factory() as writer:
        guest = Person(
            display_name="Paul", role="guest", expires_at=owner_chat.clock() - timedelta(days=1)
        )
        writer.add(guest)
        await writer.commit()
    turn, reply_id, _ = await _turn_of(server, guest)
    complete = call("record_complete", person="Tom", standing="visiting", reason=REASON)
    # A turn of someone no longer in the household is refused, as always.
    await handle_tool_call(mark(REASON), ctx=turn, reply_id=reply_id)
    refused = await handle_tool_call(complete, ctx=turn, reply_id=reply_id)
    assert (refused.status, refused.model_result["reason"]) == ("refused", "not_allowed")
    # A review's call is his own: it runs.
    reviewing = replace(turn, in_review=True, reply_message_id=None)
    await handle_tool_call(mark(REASON), ctx=reviewing, reply_id=None)
    done = await handle_tool_call(complete, ctx=reviewing, reply_id=None)
    assert done.status == "done", done.model_result
    assert (await person(server, tom.id)).standing == "visiting"


async def _turn_of(server: ChatServer, someone: Person) -> Any:
    """A turn of ``someone`` (given a conversation of their own) on the pilots' server."""
    from domo_core.chat.models import Conversation

    async with server.session_factory() as writer:
        writer.add(Conversation(person_id=someone.id))
        await writer.commit()
    ctx, reply_id, notified = await _turn(server.session_factory, someone)
    return replace(ctx, actor=actor_of(someone)), reply_id, notified


# --- the prompt ----------------------------------------------------------------------------------


def test_the_review_prompt_names_no_person_and_fits():
    prompt = memory_review_prompt()
    for name in ("Alex", "Dana", "Paul", "Sam", "Tom", "Owner"):
        assert name not in prompt
    assert "record" in prompt and "reason" in prompt
    # Its bound (the plan's Global Constraints): under 4 000 characters.
    assert len(prompt) < 4000


async def test_a_review_s_request_for_the_owner_waits_with_him_as_his_own(
    guest_chat: ChatPilot, owner_chat: ChatPilot
):
    """In Sam's review the assistant invites Tom and judges that the owner must agree first: the
    request waits in Alex's conversation, as news for him, and is his to run (its requester the
    owner), so that Alex's yes still runs once Sam has left."""
    server = guest_chat.server
    owner = owner_chat.person
    await guest_chat.say("My friend Tom comes tonight.")
    [waits] = await reviewed(
        guest_chat,
        his("household_invite", display_name="Tom", role="guest", guest_days=1, owner_first=True),
    )
    assert waits["status"] == "waiting_for_confirmation", waits
    async with server.session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
        made_in = await conversation_made_in(reader, request)
        alex = await reader.get(Person, owner.id)
    assert request.requester_id == owner.id
    assert made_in == request.conversation_id
    assert alex is not None and alex.news_at is not None
