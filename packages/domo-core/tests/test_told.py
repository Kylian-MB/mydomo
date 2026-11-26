"""What Domo told a person before an action (spec 6.8): when the judge warns them (``warn``)
and they answer, yes or no, the code keeps it as a memory of their own, ``told_by_domo``; and
what the judge reads of the memories as the person's own (``confirmations.case.gather``, its
``remembered``): only what the person said themselves or what Domo told them, never what someone
else said, nor the words of someone else, a deduction or an outside content (those have their own
places in the case)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import status_of
from domo_common.i18n import t
from domo_core.confirmations.case import CASE_LINES, Gathered, gather
from domo_core.confirmations.chain import just_decided
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.routes import _asked as asked_on_the_card  # pyright: ignore[reportPrivateUsage]
from domo_core.confirmations.store import CANNOT_BE_UNDONE_REASON
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import ToolCall
from domo_core.memory.embedder import NO_EMBEDDER
from domo_core.memory.models import Memory
from domo_core.memory.service import keep
from domo_core.memory.told import agreed_asking, told
from domo_core.policy.actions import DescribedAction
from domo_core.policy.subjects import HOUSE, Subject
from domo_core.texts.view import REFERENCE
from fakes import FakeChatModel
from sqlalchemy import select
from test_concerned_person import (  # pyright: ignore[reportPrivateUsage]
    _judge,
    _people,
    _requests,
    _turn,
    _yes,
    take_back,
)
from test_own_idea import LIVE

DO = Judgement("do")

RENAME = ("profile_rename", {"name": "Alex B"})
WARNING = "warn: you cannot take it back"
WARNED = "you cannot take it back"


async def told_memories(session_factory, person_id: uuid.UUID) -> list[Memory]:
    """The memories of what Domo told this person (committed)."""
    async with session_factory() as reader:
        found = await reader.scalars(
            select(Memory).where(Memory.said_by_id == person_id, Memory.origin == "told_by_domo")
        )
        return list(found.all())


async def warned_of(session_factory, cid: uuid.UUID | str | None) -> bool:
    assert cid is not None
    async with session_factory() as reader:
        found = await reader.execute(
            select(Confirmation.warned).where(Confirmation.id == uuid.UUID(str(cid)))
        )
        return found.scalar_one()


# --- what the code keeps when the person answers a warning ---------------------------------------


@pytest.mark.parametrize("answer", ["oui", "non merci", "button_refuse"])
async def test_answering_a_warned_request_is_remembered(
    adult_chat, chat_server, confirmation_ask, confirmation_status, answer
):
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    cid = reply.pending_confirmation_id
    # To the code a warning is a yes asked, with the judge's reason.
    assert await confirmation_ask(cid) == ("yes", "judge", WARNED)
    assert await warned_of(chat_server.session_factory, cid)
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []

    if answer == "button_refuse":
        assert status_of(await adult_chat.post(f"/confirmations/{cid}/refuse")) == "refused"
    else:
        await adult_chat.say(answer)
    assert await confirmation_status(cid) == ("executed" if answer == "oui" else "refused")

    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    assert (memory.content, memory.action_type, memory.subject) == (
        WARNED,
        "profile.rename",
        "profile.rename",
    )
    assert (memory.kind, memory.kept_private, memory.core) == ("fact", True, False)
    assert memory.told_answer == ("yes" if answer == "oui" else "no")


async def test_an_expired_warned_request_is_not_remembered(adult_chat, chat_server, clock):
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    clock.advance(timedelta(hours=168, seconds=1))
    response = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(response) == "expired"
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []


async def test_a_plain_yes_of_the_judge_is_not_remembered(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    assert not await warned_of(chat_server.session_factory, reply.pending_confirmation_id)
    await adult_chat.say("oui")
    assert await told_memories(chat_server.session_factory, adult_chat.person.id) == []


async def test_an_approver_answering_a_childs_warned_request_writes_nothing(
    child_chat,
    chat_server,
    grant,
    live_adult_client,
    owner_member,
    adult_member,
    confirmation_status,
):
    # Task 7's missing test: only the one who asked was told; an approver's answer keeps nothing.
    await grant("note.leave", role="child", approver="adult")
    reply = await child_chat.say_with_tool(
        "note_leave", {"recipient": "Owner", "text": "x"}, judge=WARNING
    )
    cid = reply.pending_confirmation_id
    assert await warned_of(chat_server.session_factory, cid)
    await live_adult_client.post(f"/confirmations/{cid}/approve")
    assert await confirmation_status(cid) == "executed"
    for person in (child_chat.person, adult_member.person):
        assert await told_memories(chat_server.session_factory, person.id) == []


async def test_a_warned_request_answered_by_a_change_in_words_is_remembered(
    adult_chat, chat_server, confirmation_status
):
    # Task 7's missing test: the one who asked answers with a change; they were told.
    reply = await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    rid = str(reply.pending_confirmation_id)
    change = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": {"name": "Alex C"}, "ask_again": False}),
    )
    adult_chat.use_model(FakeChatModel(tool_script=["change", change, "Done."], judge="do"))
    await adult_chat.say("ok, but call me Alex C instead")
    assert await confirmation_status(rid) == "executed"
    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    # A change is their yes, with a change: never a no to the warning.
    assert (memory.content, memory.action_type, memory.told_answer) == (
        WARNED,
        "profile.rename",
        "yes",
    )


async def test_a_warning_given_for_a_chain_is_remembered_under_its_step_s_type(
    adult_chat, chat_server
):
    # Final review M9 closed: kept under the type of the chain's step, never the chain's own.
    chain = {"steps": [{"tool": RENAME[0], "arguments": RENAME[1]}]}
    reply = await adult_chat.say_with_tool("do_in_order", chain, judge=WARNING)
    cid = reply.pending_confirmation_id
    assert await warned_of(chat_server.session_factory, cid)
    await adult_chat.say("oui")
    [memory] = await told_memories(chat_server.session_factory, adult_chat.person.id)
    assert (memory.content, memory.action_type, memory.told_answer) == (
        WARNED,
        "profile.rename",
        "yes",
    )


async def test_a_second_warning_refreshes_the_memory(session, add_person):
    alex = await add_person("Alex", "adult")
    first = datetime(2030, 1, 1, tzinfo=UTC)
    await told(
        session,
        person_id=alex.id,
        action_type="household.remove",
        reason="no way back",
        answer="yes",
        now=first,
    )
    later = first + timedelta(days=30)
    await told(
        session,
        person_id=alex.id,
        action_type="household.remove",
        reason="gone for good",
        answer="yes",
        now=later,
    )
    # Another type of action is another memory.
    await told(
        session,
        person_id=alex.id,
        action_type="profile.rename",
        reason="x",
        answer="yes",
        now=later,
    )
    found = (
        await session.scalars(select(Memory).where(Memory.action_type == "household.remove"))
    ).all()
    [memory] = found
    assert (memory.content, memory.changed_at, memory.created_at) == ("gone for good", later, first)


async def test_a_reason_the_memories_refuse_is_not_kept_and_raises_nothing(session, add_person):
    alex = await add_person("Alex", "adult")
    await told(
        session,
        person_id=alex.id,
        action_type="household.remove",
        reason="sk-ant-0123456789abcdefghijkl",
        answer="yes",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert (await session.scalars(select(Memory))).all() == []


async def test_what_he_told_keeps_the_answer(session, add_person):
    alex = await add_person("Alex", "adult")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    await told(session, person_id=alex.id, action_type="x.y", reason="r", answer="no", now=now)
    [memory] = (await session.scalars(select(Memory))).all()
    assert (memory.told_answer, memory.told_keep_asking) == ("no", None)


async def test_a_changed_answer_forgets_what_was_agreed(session, add_person):
    alex = await add_person("Alex", "adult")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    await told(session, person_id=alex.id, action_type="x.y", reason="r", answer="yes", now=now)
    assert await agreed_asking(
        session, person_id=alex.id, action_type="x.y", keep_asking=False, now=now
    )
    await told(session, person_id=alex.id, action_type="x.y", reason="r2", answer="yes", now=now)
    [memory] = (await session.scalars(select(Memory))).all()
    assert (memory.told_answer, memory.told_keep_asking) == ("yes", False)
    await told(session, person_id=alex.id, action_type="x.y", reason="r3", answer="no", now=now)
    await session.refresh(memory)
    assert (memory.told_answer, memory.told_keep_asking) == ("no", None)


async def test_agreed_asking_is_kept_per_person_and_type(session, add_person):
    alex = await add_person("Alex", "adult")
    dana = await add_person("Dana", "adult")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    for person in (alex, dana):
        await told(
            session, person_id=person.id, action_type="x.y", reason="r", answer="yes", now=now
        )
    await told(session, person_id=alex.id, action_type="z.w", reason="r", answer="yes", now=now)
    assert await agreed_asking(
        session, person_id=alex.id, action_type="x.y", keep_asking=True, now=now
    )
    assert not await agreed_asking(
        session, person_id=dana.id, action_type="nothing.here", keep_asking=True, now=now
    )
    rows = (await session.scalars(select(Memory))).all()
    kept = {(m.said_by_id, m.action_type): m.told_keep_asking for m in rows}
    assert kept == {(alex.id, "x.y"): True, (alex.id, "z.w"): None, (dana.id, "x.y"): None}


# --- what the judge reads as the person's own ---------------------------------------------------

INVITE = DescribedAction("core", "household.invite", None, "permissions", "household", {})


async def case_of(session, person_id: uuid.UUID, says: str) -> Gathered:
    return await gather(
        session,
        about=person_id,
        private_of=person_id,
        action=INVITE,
        subject=Subject("invitee:tom", HOUSE),
        says=says,
        extra_query=None,
        embedder=NO_EMBEDDER,
        zone=None,
        now=datetime(2030, 1, 2, tzinfo=UTC),
        start_trust=50,
    )


async def _keep(
    session, said_by, content, origin, *, private=False, action_type=None, subject="the guest room"
):
    return await keep(
        session,
        said_by=said_by,
        subject=subject,
        content=content,
        kind="fact",
        origin=origin,
        kept_private=private,
        core=False,
        now=datetime(2030, 1, 1, tzinfo=UTC),
        action_type=action_type,
    )


async def test_the_judge_reads_what_it_told_them_first_then_their_own_words(session, add_person):
    alex = await add_person("Alex", "adult")
    words = await _keep(session, alex.id, "Tom may sleep in the guest room", "own_words")
    warned = await _keep(
        session, alex.id, "Tom keeps the key", "told_by_domo", action_type="household.invite"
    )
    other_type = await _keep(
        session,
        alex.id,
        "a rename is final",
        "told_by_domo",
        action_type="profile.rename",
        subject="names",
    )
    case = await case_of(session, alex.id, "Invite Tom to the guest room")
    found = [line.content for line in case.remembered]
    assert found[0] == warned.content
    assert words.content in found
    # What Domo told them before another action is not about this one; the search may still
    # find it by its words, but here it shares none.
    assert other_type.content not in found
    assert all(line.by == "them" for line in case.remembered)


async def test_what_he_told_them_comes_with_their_answer(session, add_person):
    alex = await add_person("Alex", "adult")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    await told(
        session,
        person_id=alex.id,
        action_type="household.invite",
        reason="a guest keeps the key",
        answer="no",
        now=now,
    )
    await agreed_asking(
        session, person_id=alex.id, action_type="household.invite", keep_asking=True, now=now
    )
    [line] = (await case_of(session, alex.id, "Invite Tom")).remembered
    assert (line.content, line.answer, line.keep_asking) == ("a guest keeps the key", "no", True)


async def test_the_judge_never_reads_what_someone_else_said_as_theirs(session, add_person):
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    # Shared, about the very same thing, in Sam's own words: still not Alex's.
    await _keep(session, sam.id, "Tom may sleep in the guest room", "own_words")
    await _keep(
        session, sam.id, "Tom keeps the key", "told_by_domo", action_type="household.invite"
    )
    case = await case_of(session, alex.id, "Invite Tom to the guest room")
    assert case.remembered == ()
    assert {(line.by, line.who) for line in case.others_said} == {("other", "Sam")}


async def test_the_judge_never_reads_others_words_or_deductions_as_theirs(session, add_person):
    alex = await add_person("Alex", "adult")
    for origin in ("others_words", "deduced", "external"):
        await _keep(session, alex.id, f"Tom may sleep in the guest room ({origin})", origin)
    case = await case_of(session, alex.id, "Invite Tom to the guest room")
    assert case.remembered == ()
    assert len(case.others_said) == 2 and len(case.lessons) == 1
    mine = await _keep(session, alex.id, "Tom may sleep in the guest room", "own_words")
    case = await case_of(session, alex.id, "Invite Tom to the guest room")
    assert [line.content for line in case.remembered] == [mine.content]


async def test_the_judge_reads_at_most_its_limit(session, add_person):
    alex = await add_person("Alex", "adult")
    for n in range(CASE_LINES + 2):
        await _keep(
            session, alex.id, f"warning {n}", "told_by_domo", action_type="household.invite"
        )
    await _keep(session, alex.id, "Tom may sleep in the guest room", "own_words")
    case = await case_of(session, alex.id, "Invite Tom to the guest room")
    assert len(case.remembered) == CASE_LINES
    assert all(line.content.startswith("warning") for line in case.remembered)


# --- whose it is, and what he is told after (Task 26) --------------------------------------------

SETTLE = (
    "When it could not be undone: see with them whether you should keep asking next time, and "
    "keep it with their words (asking_agreed, its type as could_not_be_undone says)."
)


async def test_after_a_warned_request_is_answered_he_is_told_to_settle_the_next_times(
    adult_chat,
):
    await adult_chat.say_with_tool(*RENAME, judge=WARNING)
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("oui")
    assert any(SETTLE in call.system for call in model.calls)
    # A plain yes, with nothing they had to know first: nothing to settle.
    await adult_chat.say_with_tool(*RENAME, judge="yes: a first time")
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("oui")
    assert model.calls and not any(SETTLE in call.system for call in model.calls)


WARNS = Judgement("warn", reason="you cannot take it back")


async def _danas_answered_warning(session_factory, monkeypatch, *answers: Judgement):
    """His idea about Dana's thing, born in Alex's turn on his own: the judge warns, the request
    waits for Dana, who says yes. The people, Alex's turn, the judge and the request."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    judged = _judge(monkeypatch, WARNS, *answers)
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    # Waiting for Dana: the fact that it was warned is kept, never the judge's words (R69); she is
    # shown the code's own line that it cannot be undone, on her card and to her assistant (R72).
    assert (request.requester_id, request.concerned_id) == (alex.id, dana.id)
    assert (request.warned, request.ask_why) == (True, CANNOT_BE_UNDONE_REASON)
    card = asked_on_the_card(request, dana, REFERENCE)
    assert card is not None and t("confirmation.asked.domo.cannot_be_undone", "en") in card
    assert "you cannot take it back" not in card
    outcome, _ = await _yes(ctx, request, dana)
    assert outcome == "executed"
    return alex, dana, ctx, judged, request, reply_id


async def test_for_his_own_idea_it_is_kept_for_the_person_concerned(session_factory, monkeypatch):
    alex, dana, *_ = await _danas_answered_warning(session_factory, monkeypatch, DO)
    # Kept for Dana alone: never for the one whose turn it was, the owner included.
    assert await told_memories(session_factory, alex.id) == []
    [memory] = await told_memories(session_factory, dana.id)
    assert (memory.content, memory.action_type, memory.told_answer) == (
        CANNOT_BE_UNDONE_REASON,
        "device.remove",
        "yes",
    )
    assert memory.kept_private


async def test_his_later_idea_reads_what_was_agreed_with_the_person_concerned(
    session_factory, monkeypatch
):
    alex, dana, ctx, _, _, reply_id = await _danas_answered_warning(
        session_factory, monkeypatch, DO
    )
    async with session_factory() as writer:
        assert await agreed_asking(
            writer, person_id=dana.id, action_type="device.remove", keep_asking=False, now=LIVE
        )
        await told(
            writer,
            person_id=alex.id,
            action_type="device.remove",
            reason="Alex was told this",
            answer="no",
            now=LIVE,
        )
        await writer.commit()
    # A later idea of his about Dana's thing, born in Alex's turn: Dana's answer and agreement.
    judged = _judge(monkeypatch, Judgement("yes", reason="I am not sure"))
    record = await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    # keep_asking false skips no yes in code (5): the judge still had the case, and its yes asks.
    assert len(judged.cases) == 1
    assert record.status == "waiting"
    told_lines = [line for line in judged.cases[-1].remembered if line.answer is not None]
    assert [(line.content, line.answer, line.keep_asking) for line in told_lines] == [
        (CANNOT_BE_UNDONE_REASON, "yes", False)
    ]
    assert all("Alex was told" not in line.content for line in judged.cases[-1].remembered)
    # Marked as what was told to the person the thing belongs to, not the words of who asks.
    assert all(line.concerned for line in told_lines)
    # Alex's own request (no idea of his) reads Alex's, never Dana's.
    own, own_reply, _ = await _turn(session_factory, alex)
    await handle_tool_call(take_back(alex), ctx=own, reply_id=own_reply)
    mine = [line for line in judged.cases[-1].remembered if line.answer is not None]
    assert [(line.content, line.answer) for line in mine] == [("Alex was told this", "no")]


async def test_an_unwarned_request_moved_to_them_says_nothing_of_it(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, Judgement("yes", reason="I am not sure of it"))
    ctx, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    assert (request.concerned_id, request.warned, request.ask_why) == (dana.id, False, None)
    card = asked_on_the_card(request, dana, REFERENCE)
    assert card is not None
    assert t("confirmation.asked.domo.cannot_be_undone", "en") not in card


async def test_the_settle_line_is_only_for_what_they_settled_now(session_factory, monkeypatch):
    _, dana, _, _, request, _ = await _danas_answered_warning(session_factory, monkeypatch, DO)
    async with session_factory() as reader:
        decided = await reader.get(Confirmation, request.id)
        assert decided is not None and decided.decided_by == dana.id

        async def settle(outcome: str, decider=dana, woken: bool = False) -> str | None:
            told_of = await just_decided(
                reader,
                decided,
                outcome=outcome,
                record=None,
                decider=decider,
                via="words",
                woken=woken,
            )
            return told_of.could_not_be_undone

        assert await settle("executed") == "device.remove"
        assert await settle("refused") == "device.remove"
        # Nothing was settled by their answer: no line.
        for outcome in (
            "needs_explicit",
            "needs_proof",
            "asked_again",
            "approver_changed",
            "paused",
            "expired",
            "already_decided",
        ):
            assert await settle(outcome) is None, outcome
        assert await settle("executed", woken=True) is None
        # Not decided by them now: no line either.
        decided.decided_by = None
        assert await settle("executed") is None


async def test_keep_asking_false_never_spares_a_silent_judge_s_yes(session_factory, monkeypatch):
    _, dana, *_ = await _danas_answered_warning(session_factory, monkeypatch, DO)
    async with session_factory() as writer:
        await agreed_asking(
            writer, person_id=dana.id, action_type="device.remove", keep_asking=False, now=LIVE
        )
        await writer.commit()

    async def silent(*_: object, **__: object) -> Judgement:
        raise RuntimeError("no answer")

    monkeypatch.setattr(execution_module, "judge", silent)
    # Her turn, with words from outside in it: a silent judge asks her a yes, whatever she agreed.
    ctx, reply_id, _ = await _turn(session_factory, dana, told_outside=True)
    record = await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
