"""The owner changes rights in words (spec 5.6): ``rights_propose`` has the writer draft them and
the code check them on examples; ``rights_apply`` applies a draft, judged like any action (at
once when the judge lets it go; after the yes the judge, a rule or Domo asks otherwise).

Domo and the writer are one scripted model here (``DomoAndWriter``): a call whose system
prompt starts with ``WRITER_FIRST_LINE`` is the writer's, the others are Domo's.
"""

import asyncio
import html
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import UTC, timedelta
from typing import Any

import pytest
from chat_support import ChatPilot, Reply, confirmation_cards, status_of, tool_results
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.confirmations.models import Confirmation
from domo_core.llm.models import ProviderModel
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.models import PolicyRight, RightsDraft
from domo_core.policy.rights import load_rights, pause_personal_rights
from domo_core.policy.rights_writer import MAX_OWNER_WORDS, WRITER_FIRST_LINE, WRITER_TOOL
from domo_core.tools import rights_tools
from domo_core.tools.context import BoundsItsOwnSteps, InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.tools.rights_tools import (
    WRITER_TIMEOUT_WINDOW,
    Redraft,
    redraft_rights,
    writer_timeouts,
)
from fakes import JUDGE_DEFAULT, FakeCall, FakeChatModel, is_judge_call
from sqlalchemy import delete, func, select


def days_at_most(days: int) -> str:
    """The limit of guests invited for ``days`` days at most (a stay may have no end: the days
    are read only when they are there)."""
    return (
        'action.facts.role == "guest" && action.facts.days != null '
        f'&& action.facts.days <= {days} ? "allow" : "refuse"'
    )


THREE_DAYS = days_at_most(3)
SUMMARY = "Alex peut inviter des invités, 3 jours au plus."
WORDS = "Alex peut inviter des invités, 3 jours au plus"


def alex_invites(alex: uuid.UUID, *, summary: str = SUMMARY, days: int = 3) -> dict[str, Any]:
    """The writer's draft of "Alex may invite guests, ``days`` days at most" (2 or more)."""

    def invite(
        role: str, days: int | None, name: str, expected: str, *, first: bool = False
    ) -> dict[str, Any]:
        return {
            "person_id": str(alex),
            "action_type": "household.invite",
            "facts": {
                "role": role,
                "days": days,
                "no_end": False,
                "display_name": name,
                "owner_first": first,
            },
            "expected": expected,
        }

    return {
        "changes": [
            {
                "person_id": str(alex),
                "role": None,
                "action_type": "household.invite",
                "remove": False,
                "access": "allow",
                "approver": None,
                "limit_expression": days_at_most(days),
                "judge_question": None,
                "summary": summary,
            }
        ],
        "examples": [
            invite("guest", 2, "Dana", "domo_judges"),
            invite("adult", None, "Eve", "refused"),
            invite("guest", 40, "Finn", "refused"),
            # The right's limit replaces the default one of inviting: it shows it no longer holds.
            invite("guest", 2, "Tom", "domo_judges", first=True),
        ],
    }


# A writer's answer that never comes.
SILENT = "<the writer says nothing>"

# What the writer answers: the arguments of a ``submit_rights`` call, a text, or something that
# happens while it writes (awaited before it takes its next answer).
WriterAnswer = dict[str, Any] | str | Callable[[], Awaitable[None]]


class DomoAndWriter:
    """Domo makes ``call`` (or each of the ``call`` given, one per round), then says "ok"; a
    call without tools, the reader of the replies, gets ``verdict`` as its line for the first
    request shown ("1: change"), and the yes-judge's gets ``judge`` (a yes by default, as the
    other fakes answer). The writer answers with the next of ``writer_answers``: the arguments of
    a ``submit_rights`` call, or a text (``SILENT``: nothing, ever); a function among them is
    awaited first (something happens while the writer writes). ``domo_calls`` and
    ``writer_calls`` are what each received."""

    def __init__(
        self,
        call: ToolCall | Sequence[ToolCall],
        *writer_answers: WriterAnswer,
        verdict: str = "unclear",
        judge: str = JUDGE_DEFAULT,
    ) -> None:
        self._calls = [call] if isinstance(call, ToolCall) else list(call)
        self._verdict = verdict
        self._judge = judge
        self._answers = list(writer_answers)
        self.domo_calls: list[FakeCall] = []
        self.writer_calls: list[FakeCall] = []

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        received = FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
        if system.startswith(WRITER_FIRST_LINE):
            self.writer_calls.append(received)
            answer = self._answers.pop(0)
            while callable(answer):
                await answer()
                answer = self._answers.pop(0)
            if answer == SILENT:
                await asyncio.sleep(3600)
            if isinstance(answer, str):
                yield ChatChunk(answer)
            else:
                yield ToolCall(f"w{len(self.writer_calls)}", "submit_rights", json.dumps(answer))
        else:
            self.domo_calls.append(received)
            if is_judge_call(system):
                yield ChatChunk(self._judge)
            elif not tools:
                # The reader of the owner's words: its word, for the one request shown.
                yield ChatChunk(f"1: {self._verdict}")
            elif tool_choice == "auto" and self._calls:
                yield self._calls.pop(0)
            else:
                yield ChatChunk("ok")
        yield ChatUsage(3, 2)

    @property
    def results(self) -> list[dict[str, Any]]:
        """What Domo read back from its tools in the turn."""
        return tool_results(self.domo_calls)


async def propose(
    owner: ChatPilot, words: str, *writer_answers: WriterAnswer
) -> tuple[Reply, DomoAndWriter]:
    """The owner says ``words``; Domo calls ``rights_propose`` with them, and the writer answers
    ``writer_answers``."""
    call = ToolCall("propose", "rights_propose", json.dumps({"request": words}))
    model = owner.use_model(DomoAndWriter(call, *writer_answers))
    try:
        return await owner.say(words), model
    finally:
        owner.use_model(FakeChatModel(["ok"]))


async def drafts(session_factory) -> list[RightsDraft]:
    async with session_factory() as reader:
        result = await reader.execute(select(RightsDraft).order_by(RightsDraft.created_at))
        return list(result.scalars())


async def rights_of(session_factory, action_type: str = "household.invite") -> list[PolicyRight]:
    """The rows of ``policy_rights`` for that action type, oldest first."""
    async with session_factory() as reader:
        result = await reader.execute(
            select(PolicyRight)
            .where(PolicyRight.action_type == action_type)
            .order_by(PolicyRight.created_at, PolicyRight.id)
        )
        return list(result.scalars())


def live(rights: list[PolicyRight]) -> list[PolicyRight]:
    return [r for r in rights if r.revoked_at is None and r.paused_at is None]


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


# --- proposing -----------------------------------------------------------------------------------


async def test_the_owner_proposes_a_right_and_sees_its_examples(
    owner_chat, adult_member, session_factory
):
    alex = adult_member.person
    reply, model = await propose(owner_chat, WORDS, alex_invites(alex.id))
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    [result] = model.results
    [draft] = await drafts(session_factory)
    assert result == {
        "status": "done",
        "draft": str(draft.id),
        "rights": [SUMMARY],
        "examples": [
            {
                "situation": t(
                    "rights.example.household.invite",
                    "en",
                    who="Alex",
                    display_name="Dana",
                    role="guest",
                    days="2 days",
                    no_end="no",
                    owner_first="no",
                ),
                "outcome": "domo_judges",
            },
            {
                "situation": t(
                    "rights.example.household.invite",
                    "en",
                    who="Alex",
                    display_name="Eve",
                    role="adult",
                    days=t("rights.example.no_end", "en"),
                    no_end="no",
                    owner_first="no",
                ),
                "outcome": "refused",
            },
            {
                "situation": t(
                    "rights.example.household.invite",
                    "en",
                    who="Alex",
                    display_name="Finn",
                    role="guest",
                    days="40 days",
                    no_end="no",
                    owner_first="no",
                ),
                "outcome": "refused",
            },
            {
                "situation": t(
                    "rights.example.household.invite",
                    "en",
                    who="Alex",
                    display_name="Tom",
                    role="guest",
                    days="2 days",
                    no_end="no",
                    owner_first="yes",
                ),
                "outcome": "domo_judges",
            },
        ],
    }
    # The draft is stored as it was checked, and nothing is applied yet.
    assert draft.owner_words == WORDS
    assert draft.created_by == owner_chat.person.id
    assert draft.applied_at is None
    assert [c["summary"] for c in draft.content["changes"]] == [SUMMARY]
    assert [e["outcome"] for e in draft.content["examples"]] == [
        "domo_judges",
        "refused",
        "refused",
        "domo_judges",
    ]
    assert await rights_of(session_factory) == []


async def test_the_writer_sees_only_the_owners_words_and_the_catalog(
    owner_chat, adult_member, chat_server
):
    owner_chat.use_model(FakeChatModel(["Noted."]))
    await owner_chat.say("mon code secret est ZK-4242")
    _, model = await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    [call] = model.writer_calls
    assert call.tools == (WRITER_TOOL,)
    [message] = call.messages
    assert isinstance(message, ChatMessage) and message.role == "user"
    assert WORDS in message.content
    assert str(adult_member.person.id) in message.content
    # Nothing of the conversation: not the earlier message, not Domo's own prompt.
    assert "ZK-4242" not in message.content and "ZK-4242" not in call.system
    assert "Noted." not in message.content
    [domo_first, *_] = model.domo_calls
    assert call.system != domo_first.system
    assert not any("ZK-4242" in json.dumps(str(item)) for item in call.messages)


async def test_a_writer_that_answers_badly_stores_nothing(
    owner_chat, adult_member, session_factory, audit_actions
):  # Review Focus 3
    for answer in ("I will not.", {"changes": "all of them", "examples": []}):
        reply, model = await propose(owner_chat, WORDS, answer)
        assert reply.actions == [{"tool": "rights_propose", "status": "failed"}]
        [result] = model.results
        assert result["status"] == "failed" and result["why"]
    # Sent back once, still wrong: the same.
    wrong = alex_invites(adult_member.person.id)
    wrong["examples"][0]["expected"] = "asks_them"
    reply, model = await propose(owner_chat, WORDS, wrong, wrong)
    assert len(model.writer_calls) == 2
    assert model.results[-1] == {
        "status": "failed",
        "why": ["example 1: expected asks_them, but the code finds domo_judges"],
        "detail": "the rights could not be written: tell the owner plainly, and ask them to say "
        "it another way",
    }
    assert await drafts(session_factory) == []
    assert await rights_of(session_factory) == []
    assert "rights.changed" not in await audit_actions()


async def test_the_writer_asks_and_domo_is_told_the_question(owner_chat, session_factory):
    # Review Focus 4: the words are not enough; the writer asks rather than guess.
    reply, model = await propose(owner_chat, "Alex peut inviter", {"question": "Which Alex?"})
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    assert model.results == [{"status": "done", "question": "Which Alex?"}]
    assert await drafts(session_factory) == []


async def test_without_a_model_nothing_is_proposed(session_factory):
    # A yes that a button replays has no model in its context: the writer cannot be reached.
    from domo_core.tools.rights_tools import RightsPropose

    class NoModelContext:
        models = None

    outcome = await RightsPropose().execute({"request": WORDS}, NoModelContext())  # type: ignore[arg-type]
    assert outcome.status == "failed"
    assert await drafts(session_factory) == []


async def test_without_a_model_strong_enough_nothing_is_written_and_domo_knows_why(
    owner_chat, adult_member, session_factory
):
    from domo_core.tools.rights_tools import RightsPropose

    # The key of the request lists a small model only: the writer never takes it.
    async with session_factory() as writer:
        await writer.execute(
            delete(ProviderModel).where(ProviderModel.model_id != "example-haiku-1")
        )
        await writer.commit()
    model = DomoAndWriter([], alex_invites(adult_member.person.id))
    outcome = await RightsPropose().execute({"request": WORDS}, owners_turn(owner_chat, model))
    assert outcome.status == "failed"
    assert "no model strong enough" in outcome.model_result["detail"]
    assert model.writer_calls == []
    assert await drafts(session_factory) == []


async def test_a_request_of_rights_is_checked(owner_chat):
    for arguments in ({}, {"request": ""}, {"request": "x" * 1001}, {"request": 3}, {"w": "x"}):
        reply = await owner_chat.say_with_tool("rights_propose", arguments)
        assert reply.actions[0]["status"] == "invalid", arguments


# --- applying ------------------------------------------------------------------------------------


async def apply(owner: ChatPilot, draft_id: uuid.UUID | str) -> Reply:
    return await owner.say_with_tool("rights_apply", {"draft": str(draft_id)})


class DraftsThenApplies(DomoAndWriter):
    """Domo as a clear request has him act: he has the rights drafted, then applies the draft
    the writer gave, in the same answer (its id read from the tool's result), then says "ok"."""

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        results = [json.loads(item.content) for item in messages if isinstance(item, ToolResult)]
        domo = tools and not system.startswith(WRITER_FIRST_LINE)
        if domo and len(results) == 1 and results[0].get("draft"):
            self.domo_calls.append(
                FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
            )
            yield ToolCall("apply", "rights_apply", json.dumps({"draft": results[0]["draft"]}))
            yield ChatUsage(3, 2)
            return
        async for item in super().stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


async def test_a_clear_request_of_rights_is_drafted_and_applied_in_the_same_answer(
    owner_chat, adult_member, session_factory, chat_server
):
    """The owner's trial of 0.8.0: he asked for a right in plain words, and got a draft and "shall
    I apply it?" ("why only a draft? my request was clear"). Applying rights asks nothing by
    default any more, and the tools tell Domo to apply what was clearly asked: the draft, then
    the change, in one answer, when the judge lets it go."""
    alex = adult_member.person
    call = ToolCall("propose", "rights_propose", json.dumps({"request": WORDS}))
    owner_chat.use_model(DraftsThenApplies(call, alex_invites(alex.id), judge="do"))
    reply = await owner_chat.say(WORDS)
    assert reply.actions == [
        {"tool": "rights_propose", "status": "done"},
        {"tool": "rights_apply", "status": "done"},
    ]
    assert reply.pending_confirmation_id is None
    [right] = live(await rights_of(session_factory))
    assert (right.person_id, right.summary, right.owner_words) == (alex.id, SUMMARY, WORDS)
    # What the owner reads: the draft, then the change. Both sentences are true once it is
    # applied without a yes: neither says that a yes was awaited or given.
    read = plain(reply.cards_html)
    for language in ("en", "fr"):
        drafted = t("action.card.done.rights.propose", language)
        applied = t("action.card.done.rights.apply", language)
        for sentence in (drafted, applied):
            for word in (" yes", "approved", " oui", "accepté"):
                assert word not in sentence, sentence
    drafted, applied = (
        t("action.card.done.rights.propose", "en"),
        t("action.card.done.rights.apply", "en"),
    )
    assert drafted in read and applied in read and read.index(drafted) < read.index(applied)


def test_the_rights_tools_have_domo_apply_what_was_clearly_asked():
    """What made the model stop after the draft: ``rights_propose`` said "nothing changes yet.
    Tell them each right and each example (...), then call rights_apply", and ``rights_apply``
    "once you told them its rights and examples. It waits for their yes": telling them first
    ends the answer, and a yes was announced. They now say to apply in the same answer."""
    propose_says = TOOLS["rights_propose"].definition.description
    apply_says = TOOLS["rights_apply"].definition.description
    assert "call rights_apply with the draft in this same answer" in propose_says
    assert "not sure you understood" in propose_says and "does more than they said" in propose_says
    assert "in the same answer" in apply_says
    for gone in ("nothing changes yet", "Tell them each right", "once you told them", "their yes"):
        assert gone not in propose_says and gone not in apply_says, gone


async def test_applying_a_draft_the_judge_asks_about_replaces_the_old_right_after_the_yes(
    owner_chat, adult_member, grant, session_factory, last_audit_entry
):
    alex = adult_member.person
    old = await grant("household.invite", person=alex, access="allow", summary="Alex invites.")
    await propose(owner_chat, WORDS, alex_invites(alex.id))
    [draft] = await drafts(session_factory)

    reply = await apply(owner_chat, draft.id)
    assert reply.actions == [{"tool": "rights_apply", "status": "waiting"}]
    assert reply.model_results[-1]["by"] == "you"
    # The card shows each right and each example, as the code worked them out.
    [card] = confirmation_cards(reply.cards_html)
    assert t("action.describe.rights.apply", "en") in card
    assert SUMMARY in card
    assert t("rights.example.outcome.domo_judges", "en") in card
    assert t("rights.example.outcome.refused", "en") in card
    assert "Dana" in card and "Finn" in card
    # Nothing changes before the yes.
    assert [r.id for r in live(await rights_of(session_factory))] == [old.id]

    answer = await owner_chat.confirm_last(reply)
    assert answer.status == "executed"
    rows = await rights_of(session_factory)
    [before] = [r for r in rows if r.id == old.id]
    [after] = [r for r in rows if r.id != old.id]
    assert before.revoked_at is not None
    assert live(rows) == [after]
    assert after.person_id == alex.id and after.role is None
    assert (after.access, after.approver) == ("allow", None)
    assert after.limit_expression == THREE_DAYS and after.summary == SUMMARY
    assert after.owner_words == WORDS
    assert after.draft_id == draft.id
    assert after.created_by == owner_chat.person.id
    [applied] = await drafts(session_factory)
    assert applied.applied_at is not None
    entry = await last_audit_entry("rights.changed")
    assert entry.details == {
        "draft_id": str(draft.id),
        "changes": [
            {
                "subject": f"person:{alex.id}",
                "name": "Alex",
                "action_type": "household.invite",
                "before": "Alex invites.",
                "after": SUMMARY,
            }
        ],
    }


async def test_the_owner_can_apply_in_words(owner_chat, adult_member, session_factory):
    await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    [draft] = await drafts(session_factory)
    await apply(owner_chat, draft.id)
    owner_chat.use_model(FakeChatModel(["unclear"]))
    reply = await owner_chat.say("je confirme")
    assert reply.actions == [{"tool": "rights_apply", "status": "done"}]
    assert len(live(await rights_of(session_factory))) == 1


async def test_a_draft_is_applied_once(owner_chat, adult_member, session_factory, chat_server):
    await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    [draft] = await drafts(session_factory)
    first = await apply(owner_chat, draft.id)
    second = await apply(owner_chat, draft.id)
    assert (await owner_chat.confirm_last(first)).status == "executed"
    # The second request for the same draft can no longer be carried out.
    answer = await owner_chat.post(f"/confirmations/{second.pending_confirmation_id}/approve")
    assert status_of(answer) == "refused"
    assert t("confirmation.outcome.no_longer_possible", "en") in answer.text
    # And a new request for it is not even made.
    again = await apply(owner_chat, draft.id)
    assert again.actions == [{"tool": "rights_apply", "status": "invalid", "reason": "invalid"}]
    assert again.model_results[-1]["detail"] == "this draft was applied already: nothing more to do"
    assert len(await rights_of(session_factory)) == 1


async def test_a_draft_older_than_the_rights_in_force_is_not_applied(
    owner_chat, adult_member, grant, session_factory
):
    # What the owner reads is what is applied: the examples were worked out on the rights of the
    # moment the draft was written.
    await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    [draft] = await drafts(session_factory)
    reply = await apply(owner_chat, draft.id)
    await grant("note.leave", role="child", ask="yes", summary="Children's notes wait.")
    answer = await owner_chat.confirm_last(reply)
    assert answer.status == "refused"
    assert await rights_of(session_factory) == []


async def test_two_drafts_applied_at_once_leave_one_live_right(
    owner_chat, adult_member, session_factory
):  # Review Focus 2
    alex = adult_member.person.id
    await propose(owner_chat, WORDS, alex_invites(alex, summary="First."))
    await propose(owner_chat, WORDS, alex_invites(alex, summary="Second."))
    first, second = await drafts(session_factory)
    asked = [await apply(owner_chat, first.id), await apply(owner_chat, second.id)]
    answers = await asyncio.gather(
        *(
            owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
            for reply in asked
        )
    )
    assert sorted(status_of(answer) for answer in answers) in (
        ["executed", "failed"],
        ["executed", "refused"],
    )
    # The other one ends for being out of date: the rights changed under it. Refused when the
    # replay at its yes found it so (its draft can then no longer be carried out), failed when it
    # found it so only once it had the table.
    [other] = [answer for answer in answers if status_of(answer) != "executed"]
    if status_of(other) == "refused":
        assert t("confirmation.outcome.no_longer_possible", "en") in plain(other.text)
    else:
        assert t("action.card.failed.rights.apply.out_of_date", "en") in plain(other.text)
    rows = await rights_of(session_factory)
    assert len(live(rows)) == 1
    assert live(rows)[0].summary in ("First.", "Second.")
    assert len([d for d in await drafts(session_factory) if d.applied_at is not None]) == 1


async def test_a_paused_right_is_given_back_by_writing_it_again(
    owner_chat, adult_member, grant, session_factory, live_clock
):
    alex = adult_member.person
    paused = await grant("household.invite", person=alex, access="allow", summary="Alex invites.")
    async with session_factory() as writer:
        await pause_personal_rights(writer, alex.id, live_clock())
        await writer.commit()
    back = {
        "changes": [
            {
                "person_id": str(alex.id),
                "action_type": "household.invite",
                "access": "allow",
                "summary": "Alex invites.",
            }
        ],
        "examples": [
            {
                "person_id": str(alex.id),
                "action_type": "household.invite",
                "facts": {
                    "role": "guest",
                    "days": 2,
                    "no_end": False,
                    "display_name": "Dana",
                    "owner_first": False,
                },
                "expected": "domo_judges",
            }
        ],
    }
    _, model = await propose(owner_chat, "Rends à Alex son droit d'inviter", back)
    # The writer was told of the paused right.
    assert "Alex invites." in model.writer_calls[0].messages[0].content  # type: ignore[union-attr]
    [draft] = await drafts(session_factory)
    assert (await owner_chat.confirm_last(await apply(owner_chat, draft.id))).status == "executed"
    rows = await rights_of(session_factory)
    [before] = [r for r in rows if r.id == paused.id]
    assert before.revoked_at is not None
    assert [r.summary for r in live(rows)] == ["Alex invites."]
    assert len([r for r in rows if r.revoked_at is None]) == 1


def takes_away_alexs_right(owner: uuid.UUID, alex: uuid.UUID) -> dict[str, Any]:
    """The writer's draft of "Alex left, take away the right to invite": one change that removes,
    and an example about somebody who is here (the owner invites a guest for two days)."""
    return {
        "changes": [
            {
                "person_id": str(alex),
                "action_type": "household.invite",
                "remove": True,
                "summary": "Alex n'invite plus.",
            }
        ],
        "examples": [
            {
                "person_id": str(owner),
                "action_type": "household.invite",
                "facts": {
                    "role": "guest",
                    "days": 2,
                    "no_end": False,
                    "display_name": "Dana",
                    "owner_first": False,
                },
                "expected": "domo_judges",
            }
        ],
    }


async def test_the_owner_takes_a_right_away_from_someone_who_left(
    owner_chat, adult_member, grant, session_factory, last_audit_entry
):  # spec 5.7: "Paul est parti, retire-lui le droit d'inviter"
    alex = adult_member.person
    right = await grant("household.invite", person=alex, access="allow", summary="Alex invites.")
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    )  # Alex left: the right is paused, not gone
    [paused] = await rights_of(session_factory)
    assert paused.paused_at is not None and paused.revoked_at is None

    reply, model = await propose(
        owner_chat,
        "Alex est parti, retire-lui le droit d'inviter",
        takes_away_alexs_right(owner_chat.person.id, alex.id),
    )
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    # The writer was told who left, and the right that waits for them.
    told = model.writer_calls[0].messages[0].content  # type: ignore[union-attr]
    shown = json.loads(told[told.index("{") :])
    [former] = shown["former_people"]
    assert (former["id"], former["name"], former["role"]) == (str(alex.id), "Alex", "adult")
    assert former["left_on"] == owner_chat.clock().astimezone(UTC).date().isoformat()
    assert str(alex.id) not in {person["id"] for person in shown["people"]}
    assert [r["name"] for r in shown["paused_rights"]] == ["Alex"]

    [draft] = await drafts(session_factory)
    card = await owner_chat.confirm_last(await apply(owner_chat, draft.id))
    assert card.status == "executed"
    # Taken away for good: revoked, and nothing written in its place.
    [after] = await rights_of(session_factory)
    assert after.id == right.id and after.revoked_at is not None
    assert live(await rights_of(session_factory)) == []
    entry = await last_audit_entry("rights.changed")
    assert entry.details["changes"] == [
        {
            "subject": f"person:{alex.id}",
            "name": "Alex",
            "action_type": "household.invite",
            "before": "Alex invites.",
            "after": None,
        }
    ]


REMOVE_RULE = "Demande-moi toujours un oui exprès avant de retirer quelqu'un."


def asks_before_removing(owner: uuid.UUID, alex: uuid.UUID, **change: Any) -> dict[str, Any]:
    """The writer's draft of ``REMOVE_RULE``: a rule for everyone (no person, no role) that only
    says what to ask; ``change`` replaces parts of it (``remove: True`` takes it away again), and
    the example (the owner removes Alex) expects what follows."""
    rule: dict[str, Any] = {
        "action_type": "household.remove",
        "ask": "explicit_yes",
        "summary": REMOVE_RULE,
        **change,
    }
    return {
        "changes": [rule],
        "examples": [
            {
                "person_id": str(owner),
                "action_type": "household.remove",
                "facts": {
                    "person": f"person:{alex}",
                    "current_role": "adult",
                    "is_self": False,
                    # What a removal says of the record it acts on.
                    "made_role": None,
                    "has_account": True,
                    "state": "has_access",
                },
                "expected": "domo_judges" if rule.get("remove") else "asks_them",
            }
        ],
    }


async def test_a_rule_for_everyone_is_applied_with_what_to_ask_and_taken_away_again(
    owner_chat, adult_member, session_factory, last_audit_entry
):
    owner, alex = owner_chat.person.id, adult_member.person.id
    reply, model = await propose(owner_chat, REMOVE_RULE, asks_before_removing(owner, alex))
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    assert [e["outcome"] for e in model.results[-1]["examples"]] == ["asks_them"]
    [draft] = await drafts(session_factory)
    assert (await owner_chat.confirm_last(await apply(owner_chat, draft.id))).status == "executed"
    [row] = await rights_of(session_factory, "household.remove")
    assert (row.person_id, row.role, row.ask, row.ask_when) == (None, None, "explicit_yes", None)
    # Nothing but what to ask.
    assert (row.access, row.approver, row.limit_expression) == (None, None, None)
    entry = await last_audit_entry("rights.changed")
    assert entry.details["changes"] == [
        {
            "subject": "everyone",
            "action_type": "household.remove",
            "before": None,
            "after": REMOVE_RULE,
        }
    ]

    # The owner puts it away the same way: a change that takes the rule away (F25).
    away = asks_before_removing(owner, alex, remove=True, summary="Plus de oui exprès.")
    del away["changes"][0]["ask"]
    reply, _ = await propose(owner_chat, "Plus besoin de oui exprès pour retirer", away)
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    second = (await drafts(session_factory))[-1]
    assert (await owner_chat.confirm_last(await apply(owner_chat, second.id))).status == "executed"
    [taken_away] = await rights_of(session_factory, "household.remove")
    assert taken_away.id == row.id and taken_away.revoked_at is not None
    entry = await last_audit_entry("rights.changed")
    assert entry.details["changes"] == [
        {
            "subject": "everyone",
            "action_type": "household.remove",
            "before": REMOVE_RULE,
            "after": None,
        }
    ]


async def test_the_owner_lifts_his_own_yes_with_his_conditions(owner_chat, session_factory):
    owner = owner_chat.person.id
    conditions = "sauf si ça donne plus de droits à un enfant"
    lift = {
        "changes": [
            {
                "person_id": str(owner),
                "action_type": "rights.apply",
                "ask": "judge",
                "ask_when": conditions,
                "summary": "Applique mes droits sans me demander, sauf pour un enfant.",
            }
        ],
        "examples": [
            {
                "person_id": str(owner),
                "action_type": "rights.apply",
                "facts": {},
                "expected": "domo_judges",
            }
        ],
    }
    reply, model = await propose(owner_chat, conditions, lift)
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    assert [e["outcome"] for e in model.results[-1]["examples"]] == ["domo_judges"]
    [draft] = await drafts(session_factory)
    # The change that lifts his yes waits for the yes applying rights asks now.
    applying = await apply(owner_chat, draft.id)
    assert applying.actions == [{"tool": "rights_apply", "status": "waiting"}]
    assert (await owner_chat.confirm_last(applying)).status == "executed"
    [row] = await rights_of(session_factory, "rights.apply")
    assert (row.person_id, row.role, row.ask, row.ask_when) == (owner, None, "judge", conditions)
    assert (row.access, row.approver) == (None, None)


async def test_a_right_is_not_given_to_someone_who_left(
    owner_chat, adult_member, grant, session_factory
):
    alex = adult_member.person
    await grant("household.invite", person=alex, access="allow", summary="Alex invites.")
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    )
    gives = alex_invites(alex.id)
    reply, model = await propose(owner_chat, WORDS, gives, gives)
    assert reply.actions == [{"tool": "rights_propose", "status": "failed"}]
    # The writer was sent back once, with the reason in the code's own words.
    sent_back = model.writer_calls[1].messages[-1].content  # type: ignore[union-attr]
    assert "a right for someone who left the household is only taken away" in sent_back
    assert await drafts(session_factory) == []


async def test_only_the_owner_proposes_or_applies_rights(adult_chat, owner_chat, session_factory):
    await propose(owner_chat, WORDS, alex_invites(adult_chat.person.id))
    [draft] = await drafts(session_factory)
    reply = await adult_chat.say_with_tool("rights_propose", {"request": WORDS})
    assert reply.actions == [
        {"tool": "rights_propose", "status": "refused", "reason": "not_allowed"}
    ]
    # The owner's draft is not someone else's to apply: nobody learns it exists.
    reply = await adult_chat.say_with_tool("rights_apply", {"draft": str(draft.id)})
    assert reply.actions[0]["status"] in ("refused", "invalid")
    assert reply.pending_confirmation_id is None
    async with session_factory() as reader:
        waiting = await reader.execute(select(func.count()).select_from(Confirmation))
        assert waiting.scalar_one() == 0
    assert await rights_of(session_factory) == []
    # And the model of anyone but the owner is not shown the tools.
    adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("hello")
    last = adult_chat.server.calls[-1]
    assert not {"rights_propose", "rights_apply"} & {tool.name for tool in last.tools}


async def test_the_rights_tools_are_the_owners_and_described_for_the_barrier():
    assert TOOLS["rights_propose"].action_type == "rights.propose"
    assert TOOLS["rights_apply"].action_type == "rights.apply"
    # Ruling R16: proposing bounds each of its own steps (each writer call has its own limit).
    assert isinstance(TOOLS["rights_propose"], BoundsItsOwnSteps)


# --- a role nobody has yet, and a writer that never answers (Rulings R15, R16) -------------------


async def test_the_owner_sets_a_rule_for_guests_before_the_first_guest(
    owner_chat, adult_member, session_factory
):
    def as_a_guest(
        role: str, days: int | None, expected: str, *, first: bool = False
    ) -> dict[str, Any]:
        return {
            "person_id": None,
            "role": "guest",
            "action_type": "household.invite",
            "facts": {
                "role": role,
                "days": days,
                "no_end": False,
                "display_name": "Dana",
                "owner_first": first,
            },
            "expected": expected,
        }

    summary = "Les invités invitent d'autres invités, 3 jours au plus."
    draft = {
        "changes": [
            {
                "role": "guest",
                "action_type": "household.invite",
                "access": "allow",
                "limit_expression": THREE_DAYS,
                "summary": summary,
            }
        ],
        "examples": [
            as_a_guest("guest", 2, "domo_judges"),
            as_a_guest("adult", None, "refused"),
            as_a_guest("guest", 40, "refused"),
            as_a_guest("guest", 2, "domo_judges", first=True),
        ],
    }
    reply, model = await propose(owner_chat, "Les invités peuvent inviter", draft)
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    message = model.writer_calls[0].messages[0].content  # type: ignore[union-attr]
    shown = json.loads(message[message.index("{") :])
    assert shown["roles_without_people"] == ["child", "guest"]  # the owner and Alex only
    [result] = model.results
    assert result["rights"] == [summary]
    assert [e["outcome"] for e in result["examples"]] == [
        "domo_judges",
        "refused",
        "refused",
        "domo_judges",
    ]
    assert result["examples"][0]["situation"] == t(
        "rights.example.household.invite",
        "en",
        who="A guest",
        display_name="Dana",
        role="guest",
        days="2 days",
        no_end="no",
        owner_first="no",
    )
    [stored] = await drafts(session_factory)
    assert stored.content["examples"][0]["role"] == "guest"
    assert stored.content["examples"][0]["person_id"] is None


def spy_on_the_writer_time(monkeypatch, *, really_given: float = 0.2) -> list[float]:
    """The seconds ``rights_propose`` gives each call of the writer, in a list; the writer itself
    is given ``really_given`` seconds, so that a writer that never answers does not hold the test
    for as long as the owner's setting."""
    given: list[float] = []
    real = rights_tools.write_draft

    async def write_draft(model, owner_words, context, *, seconds):
        given.append(seconds)
        return await real(model, owner_words, context, seconds=really_given)

    monkeypatch.setattr(rights_tools, "write_draft", write_draft)
    return given


async def test_a_writer_that_never_answers_ends_failed_and_stores_nothing(
    owner_chat, session_factory, monkeypatch
):
    given = spy_on_the_writer_time(monkeypatch)
    reply, model = await propose(owner_chat, WORDS, SILENT)
    assert reply.actions == [{"tool": "rights_propose", "status": "failed"}]
    [result] = model.results
    assert result["why"] == ["the writer took too long"]
    # The owner has not set the time: the writer had the default one.
    assert given == [60]
    assert await drafts(session_factory) == []


# --- the writer's time, an owner's setting ---------------------------------------------------


def writer_time_block(*, how_often: str, seconds: int) -> str:
    """What the owner's context says when the writer ran out of time (``prompts.WriterTime``);
    ``how_often`` is "once" or "<n> times"."""
    return (
        f"The rights writer ran out of time {how_often} over the last 30 days; it has "
        f"{seconds} seconds per call (setting rights_writer.seconds, 30 to 240). If it happens "
        "often, you may suggest more time, and change it when they agree."
    )


async def test_a_writer_out_of_time_is_counted_for_the_owner(
    owner_chat,
    adult_chat,
    session_factory,
    set_setting,
    last_audit_entry,
    audit_actions,
    clock,
    monkeypatch,
):
    await set_setting("rights_writer.seconds", 30)
    given = spy_on_the_writer_time(monkeypatch)
    # Nothing ran out of time yet: Domo is told nothing about the writer's time.
    owner_model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    assert "rights_writer.seconds" not in owner_model.calls[0].system
    async with session_factory() as reader:
        assert await writer_timeouts(reader, clock()) == 0

    reply, model = await propose(owner_chat, WORDS, SILENT)
    assert reply.actions == [{"tool": "rights_propose", "status": "failed"}]
    [result] = model.results
    assert result["status"] == "failed"
    assert result["why"] == ["the writer took too long"]
    assert result["seconds"] == 30
    assert given == [30]
    assert await drafts(session_factory) == []

    # The audit log notes it, for the owner, with the time the writer had and nothing else.
    entry = await last_audit_entry("rights.writer_timeout")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {"seconds": 30}
    assert (await audit_actions()).count("rights.writer_timeout") == 1
    async with session_factory() as reader:
        assert await writer_timeouts(reader, clock()) == 1
        # Only the last 30 days count: a day before the end of the window it still does, a day
        # after it does not.
        inside = clock() + WRITER_TIMEOUT_WINDOW - timedelta(days=1)
        assert await writer_timeouts(reader, inside) == 1
        outside = clock() + WRITER_TIMEOUT_WINDOW + timedelta(days=1)
        assert await writer_timeouts(reader, outside) == 0

    # The owner's context says it, and says what the writer has and what Domo may do about it.
    owner_model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    assert writer_time_block(how_often="once", seconds=30) in owner_model.calls[0].system
    # Nobody else is told: not an adult, whatever the owner's tools did.
    adult_model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("salut")
    assert "rights_writer.seconds" not in adult_model.calls[0].system
    assert "ran out of time" not in adult_model.calls[0].system


async def hangs(*args, **kwargs) -> None:
    """An audit write that never ends: only the time bound of the step stops it."""
    await asyncio.sleep(3600)


async def breaks(*args, **kwargs) -> None:
    raise ConnectionError("the database said: secret detail")


@pytest.mark.parametrize(
    ("audit_write", "kind"), [(breaks, "ConnectionError"), (hangs, "TimeoutError")]
)
async def test_a_writer_out_of_time_is_told_even_when_its_audit_entry_cannot_be_written(
    owner_chat, session_factory, set_setting, audit_actions, monkeypatch, caplog, audit_write, kind
):
    await set_setting("rights_writer.seconds", 30)
    given = spy_on_the_writer_time(monkeypatch)
    monkeypatch.setattr(rights_tools, "append_audit", audit_write)
    # The reads of the step are quick; only the audit write that hangs runs into the bound.
    monkeypatch.setattr(rights_tools, "_STEP_SECONDS", 2.0)
    with caplog.at_level(logging.ERROR, logger="domo_core.tools.rights_tools"):
        reply, model = await propose(owner_chat, WORDS, SILENT)

    # The note is lost, but Domo is told the time the writer had, as when it is written.
    assert reply.actions == [{"tool": "rights_propose", "status": "failed"}]
    [result] = model.results
    assert result["status"] == "failed"
    assert result["why"] == ["the writer took too long"]
    assert result["seconds"] == 30
    assert given == [30]
    assert await drafts(session_factory) == []
    assert "rights.writer_timeout" not in await audit_actions()
    # The log says the kind of error and nothing the error said.
    [failure] = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert kind in failure.getMessage()
    assert "secret detail" not in caplog.text
    assert failure.exc_info is None


async def test_a_step_of_its_own_cut_by_its_deadline_is_told_as_not_known(
    owner_chat, session_factory, monkeypatch
):
    # A tool that bounds its own steps raises when one runs out of time (a machine that stalls
    # longer than that): the model is told it took too long and that it is not known whether it
    # happened, never a bare failure.
    monkeypatch.setattr(rights_tools, "_STEP_SECONDS", 1.0)
    monkeypatch.setattr(rights_tools, "load_rights", hangs)
    reply, model = await propose(owner_chat, WORDS, SILENT)
    assert reply.actions == [{"tool": "rights_propose", "status": "failed", "reason": "timeout"}]
    [result] = model.results
    assert (result["status"], result["reason"], result["outcome"]) == (
        "failed",
        "timeout",
        "unknown",
    )
    assert "may have happened" in result["detail"]
    assert await drafts(session_factory) == []


async def test_a_writer_that_asks_or_answers_is_not_counted_as_slow(
    owner_chat, adult_member, session_factory, clock
):
    # A question, a draft and a refusal of the code are not a lack of time.
    await propose(owner_chat, WORDS, {"question": "Which Alex?"})
    await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    await propose(owner_chat, WORDS, "I will not.")
    async with session_factory() as reader:
        assert await writer_timeouts(reader, clock()) == 0


async def test_the_owner_changes_the_writer_time_in_words(
    owner_chat, adult_member, current_settings, last_audit_entry, monkeypatch
):
    reply = await owner_chat.say_with_tool(
        "policy_update", {"key": "rights_writer.seconds", "value": 120}
    )
    assert reply.actions == [{"tool": "policy_update", "status": "waiting"}]
    # The card says it in plain words, and nothing changed before the yes.
    [card] = confirmation_cards(reply.cards_html)
    sentence = t("policy.change.writer_seconds", "en", seconds="120")
    assert sentence == "The rights writer will have 120 seconds per call."
    assert sentence in plain(card)
    assert (await current_settings()).writer_seconds == 60
    await owner_chat.confirm_last(reply)
    assert (await current_settings()).writer_seconds == 120
    entry = await last_audit_entry("policy.updated")
    assert entry.details == {"key": "rights_writer.seconds", "old": None, "new": 120}
    # The writer of the next request has the time the owner chose.
    given = spy_on_the_writer_time(monkeypatch, really_given=30)
    reply, _ = await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    assert given == [120]


async def test_the_owner_cannot_give_the_writer_a_time_out_of_the_range(
    owner_chat, current_settings
):
    for value in (29, 241, True, "60"):
        reply = await owner_chat.say_with_tool(
            "policy_update", {"key": "rights_writer.seconds", "value": value}
        )
        assert reply.actions == [
            {"tool": "policy_update", "status": "invalid", "reason": "invalid"}
        ]
        assert reply.pending_confirmation_id is None
    assert (await current_settings()).writer_seconds == 60


# --- the moment an example imagines (Ruling R25) -------------------------------------------------


async def test_the_card_and_the_draft_hold_the_hour_each_example_was_checked_at(
    owner_chat, child_member, set_setting, session_factory
):
    await set_setting("household.time_zone", "Europe/Paris")
    sam = str(child_member.person.id)

    def renames(expected: str, weekday: int, hour: int) -> dict[str, Any]:
        return {
            "person_id": sam,
            "action_type": "profile.rename",
            "facts": {},
            "expected": expected,
            "now": {"weekday": weekday, "hour": hour, "minute": 0},
        }

    draft = {
        "changes": [
            {
                "person_id": sam,
                "action_type": "profile.rename",
                "limit_expression": 'now.hour >= 8 && now.hour < 20 ? "allow" : "refuse"',
                "summary": "Sam se renomme entre 8 h et 20 h.",
            }
        ],
        "examples": [
            renames("domo_judges", 6, 10),
            renames("refused", 6, 22),
            renames("refused", 1, 7),
        ],
    }
    # Whatever the hour of the test, each example is worked out at the hour it states.
    reply, model = await propose(owner_chat, "Sam se renomme entre 8 h et 20 h", draft)
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    [result] = model.results
    assert [(e["outcome"], e["moment"]) for e in result["examples"]] == [
        ("domo_judges", "On a Saturday at 10:00."),
        ("refused", "On a Saturday at 22:00."),
        ("refused", "On a Monday at 7:00."),
    ]
    [stored] = await drafts(session_factory)
    assert [
        {part: e["now"][part] for part in ("weekday", "hour", "minute")}
        for e in stored.content["examples"]
    ] == [
        {"weekday": 6, "hour": 10, "minute": 0},
        {"weekday": 6, "hour": 22, "minute": 0},
        {"weekday": 1, "hour": 7, "minute": 0},
    ]
    # The card the owner says yes to shows the hour after each example.
    applying = await apply(owner_chat, stored.id)
    [card] = confirmation_cards(applying.cards_html)
    shown = plain(card)
    for moment in ("On a Saturday at 10:00.", "On a Saturday at 22:00.", "On a Monday at 7:00."):
        assert moment in shown


# --- the whole way (spec 9) ----------------------------------------------------------------------


async def test_justine_invites_a_guest_within_her_limit_and_not_an_adult(
    owner_chat, adult_chat, session_factory, count_invitations
):
    alex = adult_chat.person
    await propose(owner_chat, WORDS, alex_invites(alex.id))
    [draft] = await drafts(session_factory)
    assert (await owner_chat.confirm_last(await apply(owner_chat, draft.id))).status == "executed"

    # A guest for two days: within the limit, it waits for Alex's own yes.
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "guest", "guest_days": 2}
    )
    assert reply.actions == [{"tool": "household_invite", "status": "waiting"}]
    assert reply.model_results[-1]["by"] == "you"
    assert (await adult_chat.confirm_last(reply)).status == "executed"
    assert await count_invitations() == 1

    # An adult: refused, and Domo may tell the limit as the owner approved it.
    for _ in range(2):  # Alex insists: refused again
        reply = await adult_chat.say_with_tool(
            "household_invite", {"display_name": "Eve", "role": "adult"}
        )
        assert reply.model_results[-1] == {"status": "refused", "reason": "limit", "limit": SUMMARY}
        assert reply.pending_confirmation_id is None
    assert await count_invitations() == 1


async def test_an_example_the_tools_always_refuse_is_never_shown(
    owner_chat, adult_member, session_factory
):  # Ruling R19, with the tools of the registry
    alex = str(adult_member.person.id)
    own_role = {
        "changes": [
            {
                "person_id": alex,
                "action_type": "household.change_role",
                "access": "allow",
                "summary": "Alex peut changer les rôles.",
            }
        ],
        "examples": [
            {
                "person_id": alex,
                "action_type": "household.change_role",
                "facts": {
                    "person": f"person:{alex}",
                    "current_role": "adult",
                    "role": "child",
                    "days": None,
                    "no_end": False,
                    "made_role": None,
                    "has_account": True,
                    "state": "has_access",
                    "comes_to_live": False,
                    "off_the_guests_key_rule": False,
                    "may_then_agree_for_a_child": False,
                },
                "expected": "domo_judges",
            }
        ],
    }
    reply, model = await propose(owner_chat, "Alex peut changer les rôles", own_role, own_role)
    assert reply.actions == [{"tool": "rights_propose", "status": "failed"}]
    assert model.results[-1]["why"] == [
        "example 1: this situation is always refused, whatever the rights: choose another"
    ]
    assert await drafts(session_factory) == []


# --- a draft written again with a change (spec 5.3 of the 2026-11-02 design) ---------------------


def owners_turn(owner: ChatPilot, model: ChatModel | None, *, person: Person | None = None):
    """The context in which ``confirmation_amend`` has the writer draft rights again: the turn of
    ``person`` (the owner by default), whose conversation's model is ``model``."""
    who = owner.person if person is None else person
    return ToolContext(
        session_factory=owner.server.session_factory,
        actor=actor_of(who),
        person=who,
        conversation_id=uuid.uuid4(),
        origin="http://127.0.0.1/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=owner.clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        models=owner.server.picker(model, who),
    )


async def test_the_writer_drafts_again_from_the_owners_words_and_their_change(
    owner_chat, adult_member, session_factory
):
    alex = adult_member.person.id
    await propose(owner_chat, WORDS, alex_invites(alex))
    [first] = await drafts(session_factory)
    five = alex_invites(alex, summary="Alex peut inviter des invités, 5 jours au plus.", days=5)
    writer = DomoAndWriter([], five)
    redraft = await redraft_rights(owners_turn(owner_chat, writer), first.id, " 5 jours, pas 3 ")
    [kept, second] = await drafts(session_factory)
    assert redraft == Redraft(second.id, None)
    words = f"{WORDS}\n\n5 jours, pas 3"
    assert second.owner_words == words
    assert (second.created_by, second.applied_at) == (owner_chat.person.id, None)
    assert [c["limit_expression"] for c in second.content["changes"]] == [days_at_most(5)]
    # The writer read the owner's words with the change after them, and knows the change wins.
    [call] = writer.writer_calls
    assert words in call.messages[0].content  # type: ignore[union-attr]
    assert "a later change" in call.system
    # The draft it was asked from is as it was.
    assert (kept.id, kept.owner_words, kept.applied_at) == (first.id, WORDS, None)


async def test_a_question_of_the_writer_is_an_answer_and_stores_nothing(
    owner_chat, adult_member, session_factory
):  # F33: the question is the writer's, never in the detail of an error
    await propose(owner_chat, WORDS, alex_invites(adult_member.person.id))
    [first] = await drafts(session_factory)
    writer = DomoAndWriter([], {"question": "Pour quels invités ?"})
    redraft = await redraft_rights(owners_turn(owner_chat, writer), first.id, "et pour Léa ?")
    assert redraft == Redraft(None, "Pour quels invités ?")
    assert [d.id for d in await drafts(session_factory)] == [first.id]


async def test_a_draft_is_written_again_for_the_owner_only_and_within_the_words(
    owner_chat, adult_member, session_factory
):
    alex = adult_member.person
    await propose(owner_chat, WORDS, alex_invites(alex.id))
    [first] = await drafts(session_factory)
    writer = DomoAndWriter([], "never read")
    owner = owners_turn(owner_chat, writer)
    too_long = "x" * (MAX_OWNER_WORDS - len(WORDS) - 1)
    cases = [
        # Only the owner has rights written (the foundation): not even the draft is read.
        (owners_turn(owner_chat, writer, person=alex), first.id, "3 jours", "invalid"),
        (owners_turn(owner_chat, None), first.id, "3 jours", "invalid"),
        (owner, uuid.uuid4(), "3 jours", "not_found"),
        (owner, first.id, too_long, "too_long"),
        (owner, first.id, "  ", "invalid"),
        (owner, first.id, "a\x00b", "invalid"),
    ]
    for ctx, draft_id, change, code in cases:
        with pytest.raises(InvalidArguments) as refused:
            await redraft_rights(ctx, draft_id, change)
        assert refused.value.code == code, (ctx.person.display_name, change[:10])
    assert writer.writer_calls == []
    # A writer that cannot write the rights stores nothing, and Domo is told plainly.
    with pytest.raises(InvalidArguments) as refused:
        await redraft_rights(
            owners_turn(owner_chat, DomoAndWriter([], "I will not.")), first.id, "3 jours"
        )
    assert refused.value.detail == (
        "the rights could not be written: tell the owner plainly, and ask them to say it another "
        "way"
    )
    # A draft applied already is not written again.
    assert (await owner_chat.confirm_last(await apply(owner_chat, first.id))).status == "executed"
    with pytest.raises(InvalidArguments) as refused:
        await redraft_rights(owner, first.id, "3 jours")
    assert refused.value.detail == "this draft was applied already: nothing more to do"
    assert [d.id for d in await drafts(session_factory)] == [first.id]


# --- Domo's own rights (the owner, 2026-11-18) -------------------------------------------------

NO_NOTES = "L'assistant ne laisse plus de mots de lui-même."
NOTES_ASK = "Demande-moi un oui avant de laisser un mot."


def a_note(to: uuid.UUID, role: str) -> dict[str, Any]:
    return {"recipient": f"person:{to}", "recipient_role": role, "length": 5}


def domo_leaves_no_notes(owner: uuid.UUID, alex: uuid.UUID) -> dict[str, Any]:
    """The writer's draft of "leave no more notes of your own": Domo's right, an example of him
    acting on his own, and one of Alex, whom it does not touch."""
    return {
        "changes": [
            {"domo": True, "action_type": "note.leave", "access": "deny", "summary": NO_NOTES}
        ],
        "examples": [
            {
                "who": "domo",
                "action_type": "note.leave",
                "facts": a_note(alex, "adult"),
                "expected": "refused",
            },
            {
                "person_id": str(alex),
                "action_type": "note.leave",
                "facts": a_note(owner, "owner"),
                "expected": "domo_judges",
            },
        ],
    }


def everyone_asks_before_a_note(owner: uuid.UUID, alex: uuid.UUID) -> dict[str, Any]:
    """The writer's draft of a rule for everyone on notes: what to ask, nothing else."""
    return {
        "changes": [{"action_type": "note.leave", "ask": "yes", "summary": NOTES_ASK}],
        "examples": [
            {
                "person_id": str(alex),
                "action_type": "note.leave",
                "facts": a_note(owner, "owner"),
                "expected": "asks_them",
            },
            # Domo's own right stays as it is: still refused.
            {
                "who": "domo",
                "action_type": "note.leave",
                "facts": a_note(alex, "adult"),
                "expected": "refused",
            },
        ],
    }


async def test_the_owner_takes_a_right_from_domo_in_words(
    owner_chat, adult_member, session_factory, last_audit_entry
):
    owner, alex = owner_chat.person.id, adult_member.person.id
    reply, model = await propose(
        owner_chat, "Ne laisse plus de mots de toi-même", domo_leaves_no_notes(owner, alex)
    )
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    [first, second] = model.results[-1]["examples"]
    assert first["outcome"] == "refused"
    assert first["situation"].startswith(t("rights.example.as_domo", "en"))
    assert (second["outcome"], second["situation"].startswith("Alex")) == ("domo_judges", True)
    [draft] = await drafts(session_factory)
    assert (await owner_chat.confirm_last(await apply(owner_chat, draft.id))).status == "executed"

    [row] = await rights_of(session_factory, "note.leave")
    assert (row.for_domo, row.person_id, row.role, row.access) == (True, None, None, "deny")
    async with session_factory() as reader:
        rights = await load_rights(reader)
    [right] = rights.rights
    assert right.for_domo and right.summary == NO_NOTES
    entry = await last_audit_entry("rights.changed")
    assert entry.details["changes"] == [
        {"subject": "domo", "action_type": "note.leave", "before": None, "after": NO_NOTES}
    ]

    # A rule for everyone on the same type is another right: it leaves Domo's in force.
    reply, _ = await propose(owner_chat, NOTES_ASK, everyone_asks_before_a_note(owner, alex))
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    second_draft = (await drafts(session_factory))[-1]
    applying = await apply(owner_chat, second_draft.id)
    assert (await owner_chat.confirm_last(applying)).status == "executed"
    rows = live(await rights_of(session_factory, "note.leave"))
    assert sorted((r.for_domo, r.access, r.ask) for r in rows) == [
        (False, None, "yes"),
        (True, "deny", None),
    ]
    entry = await last_audit_entry("rights.changed")
    assert entry.details["changes"] == [
        {"subject": "everyone", "action_type": "note.leave", "before": None, "after": NOTES_ASK}
    ]

    # And taking Domo's right away leaves the rule for everyone.
    gives_back = {
        "changes": [
            {"domo": True, "action_type": "note.leave", "remove": True, "summary": "Rendu."}
        ],
        "examples": [
            {
                "who": "domo",
                "action_type": "note.leave",
                "facts": a_note(alex, "adult"),
                "expected": "asks_them",
            }
        ],
    }
    reply, _ = await propose(owner_chat, "Tu peux de nouveau laisser des mots", gives_back)
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    third = (await drafts(session_factory))[-1]
    assert (await owner_chat.confirm_last(await apply(owner_chat, third.id))).status == "executed"
    [left] = live(await rights_of(session_factory, "note.leave"))
    assert (left.for_domo, left.ask, left.summary) == (False, "yes", NOTES_ASK)
