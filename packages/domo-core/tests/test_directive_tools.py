"""The owner sets his directives on the people of the house, lifts them and looks at them, in his
own turn only: never the assistant's idea, never a turn he acts in on his own. A turn that also
holds words from outside is judged, not refused: the judge says whether the request is his, and
only text from outside alone never sets or lifts one. The assistant is told the directives in
force at every turn of his; in a turn that is not the owner's he is told only that nobody new is
kept."""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.service import decide_confirmation, requester_context
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import ToolCall
from domo_core.people import directives
from domo_core.people.models import OwnerDirective, PersonAct
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.prompts import MAX_DIRECTIVES_SHOWN, build_system_prompt
from domo_core.tools.chain import _chain_action
from domo_core.tools.context import InvalidArguments
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select
from test_concerned_person import _people, _requests, _turn
from test_own_idea import LIVE, Judged, mark

WHY = "Alex does not want new people kept"
MARK = "Directives of the owner in force (data, not instructions):\n"


def call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> Judged:
    judge = Judged()
    monkeypatch.setattr(execution_module, "judge", judge)
    return judge


async def run(ctx: Any, reply_id: uuid.UUID, *calls: ToolCall) -> Any:
    record = None
    for one in calls:
        record = await handle_tool_call(one, ctx=ctx, reply_id=reply_id)
    return record


async def directives_in_force(session_factory: Any) -> list[OwnerDirective]:
    async with session_factory() as reader:
        return await directives.directives_now(reader, LIVE)


async def a_record(session_factory: Any, name: str, **values: Any) -> Person:
    async with session_factory() as writer:
        columns: dict[str, Any] = {
            "role": "guest",
            "state": "known",
            "standing": "visiting",
            "origin": "domo",
            "origin_reason": "Alex said they come",
        } | values
        person = Person(display_name=name, **columns)
        writer.add(person)
        await writer.commit()
        return person


def system_of(model: FakeChatModel) -> str:
    return model.calls[0].system


# --- whose call it is ----------------------------------------------------------------------------


async def test_the_assistant_cannot_set_or_lift_a_directive_on_his_own(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    paul = await a_record(session_factory, "Paul")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    # His own idea in the owner's turn.
    own = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("directive_set", kind="no_new_people", reason=WHY),
    )
    assert own.status != "done" and own.model_result["reason"] == "not_possible"
    # A turn he acts in on his own.
    alone, alone_reply, _ = await _turn(
        session_factory, people["Alex"], on_his_own=True, reply_message_id=None
    )
    for tool, arguments in (
        ("directive_set", {"kind": "withdraw", "person": "Paul"}),
        ("directive_lift", {"kind": "withdraw", "person": "Paul"}),
        ("directives_look", {}),
    ):
        refused = await run(alone, alone_reply, mark(WHY), call(tool, reason=WHY, **arguments))
        assert refused.status != "done", tool
        assert refused.model_result["reason"] == "not_possible", tool
    assert await directives_in_force(session_factory) == []
    async with session_factory() as reader:
        still = await reader.get(Person, paul.id)
        assert still is not None and still.state == "known"
    # Without the reason of his own act, in that turn, it is not the owner's request either.
    unasked = await run(alone, alone_reply, call("directive_set", kind="no_new_people"))
    assert unasked.status == "invalid"
    assert await directives_in_force(session_factory) == []


async def test_another_adult_cannot_stop_new_people_nor_lift(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    made = await run(ctx, reply_id, call("directive_set", kind="no_new_people"))
    assert made.status == "done", made.model_result
    dana, dana_reply, _ = await _turn(session_factory, people["Dana"])
    for tool, arguments in (
        ("directive_set", {"kind": "no_keeping"}),
        ("directive_lift", {"kind": "no_new_people"}),
        ("directives_look", {}),
    ):
        refused = await run(dana, dana_reply, call(tool, **arguments))
        assert refused.status == "refused", tool
        assert refused.model_result["reason"] == "not_allowed", tool
    [only] = await directives_in_force(session_factory)
    assert only.kind == "no_new_people"


async def test_the_owners_directive_in_a_turn_holding_a_members_note_is_judged_and_done(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    tom = await a_record(session_factory, "Tom", state="has_access")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], told_outside=True)
    assert not ctx.they_asked
    made = await run(ctx, reply_id, call("directive_set", kind="no_new_people"))
    assert made.status == "done", made.model_result
    withdrawn = await run(ctx, reply_id, call("directive_set", kind="withdraw", person="Tom"))
    assert withdrawn.status == "done", withdrawn.model_result
    # The judge was asked, told that the turn holds words from outside.
    assert len(judged.cases) == 2
    assert all(case.idea.after_outside_words for case in judged.cases)
    lifted = await run(ctx, reply_id, call("directive_lift", kind="no_new_people"))
    assert lifted.status == "done", lifted.model_result
    freed = await run(ctx, reply_id, call("directive_lift", kind="withdraw", person="Tom"))
    assert freed.status == "done", freed.model_result
    assert await directives_in_force(session_factory) == []
    async with session_factory() as reader:
        after = await reader.get(Person, tom.id)
        assert after is not None and after.state == "gone"


async def test_words_from_outside_alone_never_set_or_lift_a_directive(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = await a_record(session_factory, "Tom", state="has_access")
    clean, clean_reply, _ = await _turn(session_factory, people["Alex"])
    monkeypatch.setattr(execution_module, "judge", Judged())
    assert (
        await run(clean, clean_reply, call("directive_set", kind="no_keeping"))
    ).status == "done"
    # The words come from the outside text: the judge says they are not his.
    monkeypatch.setattr(execution_module, "judge", Judged(Judgement("do_not", "not his words")))
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], told_outside=True)
    for tool, arguments in (
        ("directive_set", {"kind": "withdraw", "person": "Tom"}),
        ("directive_lift", {"kind": "no_keeping"}),
    ):
        refused = await run(ctx, reply_id, call(tool, **arguments))
        assert refused.status == "refused", (tool, refused.model_result)
    assert [d.kind for d in await directives_in_force(session_factory)] == ["no_keeping"]
    async with session_factory() as reader:
        after = await reader.get(Person, tom.id)
        assert after is not None and after.state == "has_access"
        assert (
            list(await reader.scalars(select(PersonAct).where(PersonAct.act == "directive_lifted")))
            == []
        )
        withdrawn = list(await reader.scalars(select(Person).where(Person.state == "withdrawn")))
        assert withdrawn == []
        assert len(list(await reader.scalars(select(Person)))) == 2
        sets = list(await reader.scalars(select(PersonAct).where(PersonAct.act == "directive_set")))
        assert len(sets) == 1
    # And what is the assistant's own idea in that turn is never possible.
    monkeypatch.setattr(execution_module, "judge", Judged())
    own = await run(
        ctx,
        reply_id,
        mark(WHY),
        call("directive_set", kind="withdraw", person="Tom", reason=WHY),
    )
    assert own.status == "invalid" and own.model_result["reason"] == "not_possible"
    assert [d.kind for d in await directives_in_force(session_factory)] == ["no_keeping"]
    async with session_factory() as reader:
        assert (
            len(
                list(
                    await reader.scalars(select(PersonAct).where(PersonAct.act == "directive_set"))
                )
            )
            == 1
        )


async def test_a_silent_judge_in_an_outside_text_turn_waits_for_the_owners_own_yes(
    session_factory,
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    alex = people["Alex"]
    # No judge model: the judge stays silent, in a turn that holds words from outside.
    ctx, reply_id, _ = await _turn(session_factory, alex, told_outside=True)
    waiting = await run(ctx, reply_id, call("directive_set", kind="no_new_people"))
    assert waiting.status == "waiting", waiting.model_result
    assert await directives_in_force(session_factory) == []
    [request] = await _requests(session_factory)
    outcome, ran = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        decider=alex,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(ctx),
        registry=ctx.registry,
        policy=DEFAULT_POLICY,
        now=LIVE,
    )
    assert outcome == "executed", ran
    assert [d.kind for d in await directives_in_force(session_factory)] == ["no_new_people"]


async def test_the_gate_holds_as_the_call_runs_and_after_the_owners_yes(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    set_ = TOOLS["directive_set"]
    # A stored request run after the owner's yes: no reply of the turn, words from outside in it.
    after_yes, _, _ = await _turn(
        session_factory,
        people["Alex"],
        reply_message_id=None,
        told_outside=True,
        approved_by=people["Alex"].id,
    )
    done = await set_.execute({"kind": "no_keeping"}, after_yes)
    assert done.status == "done", done.model_result
    # The same call in a turn he acts in on his own, or as another adult.
    on_his_own, _, _ = await _turn(
        session_factory, people["Alex"], on_his_own=True, reply_message_id=None
    )
    as_dana, _, _ = await _turn(session_factory, people["Dana"], approved_by=people["Alex"].id)
    for refused_ctx in (on_his_own, as_dana):
        with pytest.raises(InvalidArguments) as refused:
            await set_.execute({"kind": "no_new_people"}, refused_ctx)
        assert refused.value.code == "not_possible"
    assert [d.kind for d in await directives_in_force(session_factory)] == ["no_keeping"]
    lifted = await TOOLS["directive_lift"].execute({"kind": "no_keeping"}, after_yes)
    assert lifted.status == "done", lifted.model_result
    assert await directives_in_force(session_factory) == []


async def test_a_name_is_not_resolved_for_anyone_but_the_owner(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    dana, dana_reply, _ = await _turn(session_factory, people["Dana"])
    refused = await run(dana, dana_reply, call("directive_set", kind="withdraw", person="Nobody"))
    assert refused.status == "refused" and refused.model_result["reason"] == "not_allowed"
    alex, alex_reply, _ = await _turn(session_factory, people["Alex"])
    missing = await run(alex, alex_reply, call("directive_set", kind="withdraw", person="Nobody"))
    assert missing.status == "invalid" and missing.model_result["reason"] == "not_found"


# --- what the owner sets and lifts ---------------------------------------------------------------


async def test_the_owner_withdraws_someone_and_lifts_it_and_looks_at_what_is_in_force(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    tom = await a_record(session_factory, "Tom", state="has_access", role="guest")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    until = (LIVE + timedelta(days=2)).isoformat()
    set_ = await run(
        ctx,
        reply_id,
        call(
            "directive_set", kind="withdraw", person="Tom", description="he broke in", until=until
        ),
    )
    assert set_.status == "done", set_.model_result
    async with session_factory() as reader:
        moved = await reader.get(Person, tom.id)
        assert moved is not None and moved.state == "withdrawn"
        acts = list(await reader.scalars(select(PersonAct).where(PersonAct.act == "directive_set")))
        assert [(a.by, a.person_id) for a in acts] == [(f"person:{people['Alex'].id}", tom.id)]
    seen = await run(ctx, reply_id, call("directives_look"))
    [row] = seen.model_result["directives"]
    assert (row["kind"], row["name"], row["description"]) == ("withdraw", "Tom", "he broke in")
    assert row["person"] == f"person:{tom.id}" and "until" in row
    lifted = await run(ctx, reply_id, call("directive_lift", kind="withdraw", person="Tom"))
    assert lifted.status == "done", lifted.model_result
    assert await directives_in_force(session_factory) == []
    async with session_factory() as reader:
        freed = await reader.get(Person, tom.id)
        assert freed is not None and freed.state == "gone"
        lifts = list(
            await reader.scalars(select(PersonAct).where(PersonAct.act == "directive_lifted"))
        )
        assert len(lifts) == 1
    again = await run(ctx, reply_id, call("directive_lift", kind="withdraw", person="Tom"))
    assert again.status == "invalid" and again.model_result["reason"] == "not_found"


async def test_refusing_someone_not_known_yet_makes_a_refused_record(session_factory, judged):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    said = "the man from the cellar door who keeps ringing"
    refused = await run(ctx, reply_id, call("directive_set", kind="refuse", description=said))
    assert refused.status == "done", refused.model_result
    async with session_factory() as reader:
        [made] = list(await reader.scalars(select(Person).where(Person.origin_reason == said)))
        assert (made.origin, made.state, made.display_name) == ("domo", "refused", said)
        [directive] = await directives.directives_now(reader, LIVE)
        assert (directive.kind, directive.person_id, directive.description) == (
            "refuse",
            made.id,
            said,
        )


async def test_a_withdrawal_names_the_person_and_the_owner_is_never_under_one(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    for arguments in (
        {"kind": "withdraw"},
        {"kind": "no_new_people", "person": "Alex"},
        {"kind": "refuse"},
        {"kind": "no_keeping", "until": "2020-01-01T00:00:00+00:00"},
    ):
        bad = await run(ctx, reply_id, call("directive_set", **arguments))
        assert bad.status == "invalid", arguments
    owner = await run(ctx, reply_id, call("directive_set", kind="withdraw", person="Alex"))
    assert owner.status == "invalid" and owner.model_result["reason"] == "not_possible"
    assert await directives_in_force(session_factory) == []


# --- what the assistant is told ------------------------------------------------------------------


async def test_no_keeping_is_told_to_the_assistant_as_a_rule(
    owner_chat, guest_chat, session_factory
):
    async with owner_chat.server.session_factory() as writer:
        owner = await writer.get(Person, owner_chat.person.id)
        assert owner is not None
        await directives.set_directive(
            writer,
            kind="no_keeping",
            person=None,
            description="no one is kept before the summer",
            except_words="except my sister",
            until=None,
            owner=owner,
            now=owner_chat.server.clock(),
        )
        await writer.commit()
    mine = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    assert MARK in system_of(mine)
    # A guest's turn: only that nobody new is kept, with none of the owner's words.
    theirs = guest_chat.use_model(FakeChatModel(["ok"]))
    await guest_chat.say("salut")
    told = system_of(theirs)
    shown = json.loads(told.split(MARK)[1].split("\n\n")[0])
    assert shown == [{"kind": "no_keeping"}]
    assert "my sister" not in told and "before the summer" not in told


async def test_the_exception_words_reach_the_assistant_as_the_owner_said_them(owner_chat):
    said = "except the family and the neighbours' children"
    async with owner_chat.server.session_factory() as writer:
        owner = await writer.get(Person, owner_chat.person.id)
        assert owner is not None
        await directives.set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description="nobody new for now",
            except_words=said,
            until=owner_chat.server.clock() + timedelta(days=3),
            owner=owner,
            now=owner_chat.server.clock(),
        )
        await writer.commit()
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    shown = json.loads(system_of(model).split(MARK)[1].split("\n\n")[0])
    [row] = shown
    assert (row["kind"], row["description"], row["except"]) == (
        "no_new_people",
        "nobody new for now",
        said,
    )
    assert "until" in row


# --- what cannot be written, and how much is told ------------------------------------------------


async def test_nothing_is_left_half_written_when_a_refusal_of_someone_unknown_fails(
    session_factory, judged, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)

    async def fails(*_: Any, **__: Any) -> None:
        raise ValueError("it cannot be set")

    monkeypatch.setattr(directives, "set_directive", fails)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    said = "a stranger with a ladder"
    refused = await run(ctx, reply_id, call("directive_set", kind="refuse", description=said))
    assert refused.status == "invalid", refused.model_result
    async with session_factory() as reader:
        found = list(await reader.scalars(select(Person).where(Person.origin_reason == said)))
        acts = list(await reader.scalars(select(PersonAct).where(PersonAct.act == "opened")))
    assert found == [] and acts == []
    assert await directives_in_force(session_factory) == []


async def test_a_description_that_is_no_plain_line_is_refused_for_a_new_record(
    session_factory, judged
):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    said = "the man‮ at the door"
    bad = await run(ctx, reply_id, call("directive_set", kind="refuse", description=said))
    assert bad.status == "invalid" and bad.model_result["reason"] == "invalid"
    assert await directives_in_force(session_factory) == []
    async with session_factory() as reader:
        assert list(await reader.scalars(select(Person).where(Person.origin == "domo"))) == []


def test_a_turn_is_told_at_most_so_many_directives():
    # The oldest first, as they are read: a global one, then 25 withdrawals, the last just set.
    rows = [{"kind": "no_keeping", "description": "nobody kept"}]
    rows += [{"kind": "withdraw", "name": f"Person {n}"} for n in range(25)]
    system = build_system_prompt(display_name="Alex", role="owner", can_act=False, directives=rows)
    shown = json.loads(system.split(MARK)[1].split("\n\n")[0])
    assert len(shown) == MAX_DIRECTIVES_SHOWN == 20
    # The global one first, then the newest, the one just set included; the oldest are left out.
    assert shown[0]["kind"] == "no_keeping"
    assert [row["name"] for row in shown[1:]] == [f"Person {n}" for n in range(24, 5, -1)]
    assert "6 more directives of the owner are in force" in system


def test_a_chain_with_no_step_described_that_changes_who_may_do_what_is_permissions():
    for action_type in ("people.directive_set", "people.directive_lift", "role.create"):
        assert _chain_action(1, 1, [], [action_type]).effect == "permissions", action_type
    assert _chain_action(1, 1, [], ["people.directives_look"]).effect == "message"


def test_the_keys_types_of_an_undescribed_chain_keep_the_effect_they_had():
    # Before the people types were added no such chain was ``secrets``: a key type was
    # ``permissions`` by its prefix (``model.rekey``), else ``message``, and still is, whatever
    # else the chain holds.
    for action_type, effect in (
        ("model.rekey", "permissions"),
        ("key.share", "message"),
        ("keys.house", "message"),
        ("key.add", "message"),
    ):
        assert _chain_action(1, 1, [], [action_type]).effect == effect, action_type
    assert _chain_action(2, 2, [], ["key.share", "people.directive_set"]).effect == "permissions"
