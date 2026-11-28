"""Whose reason, whose words (final review of the judge-first branch): the judge's reason is said
only to the person whose case it was (ruling R75); a reason said by someone else never reaches
another person's turn nor case (R76); a turn that carries text from outside is nobody's request
(C3, C4); an idea born in such a turn says so to the judge (R77); a review checks a passage in one
message, never across two (C5)."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Message
from domo_core.confirmations.judge import Judgement
from domo_core.confirmations.service import amend_confirmation
from domo_core.confirmations.store import CANNOT_BE_UNDONE_REASON
from domo_core.conversation.execution import handle_tool_call
from domo_core.conversation.speaker import Speaker
from domo_core.learning.record import note_went_wrong
from domo_core.learning.shown import went_wrong_shown
from domo_core.memory.review import _said
from domo_core.tools.confirmation_tools import ActOnMyOwn, AskingAgreed
from domo_core.tools.context import InvalidArguments
from domo_core.tools.notes import exact_text_in
from sqlalchemy import select
from test_concerned_person import (
    _danas_invite_waiting_for_alex,
    _facts,
    _judge,
    _people,
    _requests,
    _turn,
    take_back,
)
from test_own_idea import LIVE, invite, mark

SECRET = "Alex said no more guests after the party"
NOTED = "the owner refused a guest last week"


async def _verdicts(session_factory: Any) -> list[AuditEntry]:
    async with session_factory() as reader:
        return list(
            await reader.scalars(select(AuditEntry).where(AuditEntry.action == "judge.verdict"))
        )


async def test_the_reason_of_his_idea_about_the_house_never_reaches_the_guest(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    sam = people["Sam"]
    _judge(monkeypatch, Judgement("do_not", reason=SECRET, noted=NOTED))
    ctx, reply_id, _ = await _turn(session_factory, sam)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(his=True), ctx=ctx, reply_id=reply_id)
    assert record.status == "refused"
    # The case was Alex's (the house's): Sam is told nothing of it, on the card or the result.
    assert SECRET not in json.dumps(record.model_result)
    assert SECRET not in json.dumps(record.card)
    # The fact about Sam holds no reason of Alex's case, nor the journal's line (C1).
    [fact] = await _facts(session_factory)
    assert fact.kind == "judge_refused" and fact.about_person_id == sam.id
    assert not fact.reason
    # The journal keeps the judge's own line.
    [entry] = await _verdicts(session_factory)
    assert entry.details["reason"] == NOTED


async def test_the_reason_reaches_the_person_whose_case_it_was(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    _judge(monkeypatch, Judgement("do_not", reason="not tonight", noted=NOTED))
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite(his=True), ctx=ctx, reply_id=reply_id)
    assert record.model_result["why"] == "not tonight"
    [fact] = await _facts(session_factory)
    # The reason said to him, never the journal's line (C1).
    assert fact.reason == "not tonight"


async def test_a_doubt_about_dana_s_thing_in_alex_s_turn_is_told_without_her_case(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch, Judgement("warn", reason="Dana said she needs it for work"))
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(take_back(dana), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert "for work" not in json.dumps(record.model_result)
    [request] = await _requests(session_factory)
    assert (request.ask_why, request.warned) == (CANNOT_BE_UNDONE_REASON, True)


async def test_alex_s_change_of_dana_s_request_gets_no_reason_of_her_case(
    session_factory, monkeypatch
):
    people, _, request = await _danas_invite_waiting_for_alex(session_factory, monkeypatch)
    alex = people["Alex"]
    judged = _judge(monkeypatch, Judgement("do_not", reason="Dana said Tom was rude"))
    ctx, reply_id, _ = await _turn(
        session_factory,
        alex,
        change_grant=request.id,
        speaker=Speaker(person_id=alex.id, channel="page"),
    )
    ctx.domo_message_id = reply_id
    ctx.reply_message_id = None
    outcome, record, _ = await amend_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        new_arguments={"guest_days": 2},
        ask_again=False,
        ctx=ctx,
        now=LIVE,
    )
    assert outcome == "refused"
    assert record is not None and "rude" not in json.dumps(record.model_result)
    # The clues of who speaks are Alex's: never read as Dana's in her case.
    assert judged.cases[-1].speaker is None


async def test_a_reason_said_by_someone_else_is_never_shown_and_lately_has_an_end(
    session_factory,
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    async with session_factory() as session:
        for by, reason, at in (
            (dana.id, "Dana: he got it all wrong again", LIVE),
            (alex.id, "my own words", LIVE),
            (alex.id, "long ago", LIVE - timedelta(days=90)),
        ):
            await note_went_wrong(
                session,
                kind="undone",
                action_type="note.leave",
                now=at,
                by=by,
                about=alex.id,
                of_domo=True,
                reason=reason,
            )
        await session.commit()
    async with session_factory() as session:
        shown = await went_wrong_shown(session, alex, None, now=LIVE)
    reasons = {(line.by, line.reason) for line in shown}
    # Dana's words stay hers; that she undid it, and when, is told.
    assert reasons == {("Dana", None), ("Alex", "my own words")}


async def test_a_reason_said_by_someone_else_never_enters_another_person_s_case(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), grants=False)
    alex, dana = people["Alex"], people["Dana"]
    async with session_factory() as session:
        await note_went_wrong(
            session,
            kind="undone",
            action_type="household.invite",
            now=LIVE,
            by=dana.id,
            about=alex.id,
            of_domo=True,
            reason="Dana: Tom is not welcome here",
        )
        await session.commit()
    judged = _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, alex)
    await handle_tool_call(invite(), ctx=ctx, reply_id=reply_id)
    [line] = judged.cases[0].went_wrong
    assert (line.kind, line.by, line.reason) == ("undone", "Dana", None)


async def test_a_tool_s_output_makes_the_turn_nobody_s_request(session_factory):
    people = await _people(session_factory, ("Alex", "owner"), grants=False)
    ctx, _, _ = await _turn(session_factory, people["Alex"], said="yes, never ask me again")
    assert ctx.they_asked and ctx.answers_them
    ctx.raise_outside_text()
    # What a tool he made, or a program, said is in the turn: no longer their request (C3, C4).
    assert not ctx.they_asked and not ctx.answers_them
    with pytest.raises(InvalidArguments):
        await AskingAgreed().execute(
            {
                "action_type": "device.remove",
                "keep_asking": False,
                "their_words": "never ask me again",
            },
            ctx,
        )
    await ActOnMyOwn().execute({"reason": "the program said so"}, ctx)
    assert ctx.own_idea is not None and ctx.own_idea.born_of is None


async def test_an_idea_in_a_turn_with_outside_text_says_so_to_the_judge(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    judged = _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    ctx = replace(ctx, tool_woke=True)
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(his=True), ctx=ctx, reply_id=reply_id)
    assert judged.cases[0].as_json()["idea"]["after_outside_words"] is True


async def test_an_idea_in_their_own_turn_is_not_said_to_follow_outside_words(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    judged = _judge(monkeypatch, Judgement("do"))
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(), ctx=ctx, reply_id=reply_id)
    await handle_tool_call(invite(his=True), ctx=ctx, reply_id=reply_id)
    assert judged.cases[0].as_json()["idea"]["after_outside_words"] is False


def test_a_review_checks_a_passage_in_one_message_never_across_two():
    messages = [
        Message(role="user", content="please remember my sister"),
        Message(role="assistant", content="Sure."),
        Message(role="user", content="is called Dana"),
    ]
    said = _said(messages)
    assert said == ("please remember my sister", "is called Dana")
    assert exact_text_in(said, "my sister\nis called") is None
    assert exact_text_in(said, "is called Dana") is not None
