"""The yes-judge (spec 4): its case, built from the database, its prompt, its call, isolated like
the classifier of the replies, and what is asked in the end, the most demanding of what the judge,
the owner's rules and Domo ask."""

import inspect
import json
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import fakes
import pytest
from chat_support import last_audit
from domo_core.accounts.models import Person
from domo_core.chat.models import MessageAction
from domo_core.confirmations import judge as judge_module
from domo_core.confirmations.chain import ago
from domo_core.confirmations.describe import describe_for_people, step_sentence
from domo_core.confirmations.judge import (
    JUDGE_MESSAGE_CHARS,
    JUDGE_MESSAGES,
    JUDGE_OUTPUT_TOKENS,
    JUDGE_SECONDS,
    MAX_JUDGE_REASON_LENGTH,
    VERDICTS,
    JudgeCase,
    Judgement,
    RememberedLine,
    RequestTold,
    RightsSay,
    StepTold,
    YesTold,
    ask_after_a_change,
    audit_judgement,
    build_case,
    fallback,
    given_by_the_change,
    judge,
    judge_prompt,
    owner_rule_of,
    planned_step_told,
    read_judgement,
    step_told,
    what_to_ask,
)
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.steps import PlannedStep
from domo_core.conversation import execution as execution_module
from domo_core.llm.types import ChatMessage, ToolCall
from domo_core.memory.models import Memory
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked, NeverAsk
from domo_core.policy.facts import PersonHistory, person_history
from domo_core.policy.roles import Actor
from domo_core.texts.view import CatalogTexts
from domo_core.trust.service import note as note_fact
from fakes import FakeChatModel
from sqlalchemy import select

from tests.fakes.fake_llm.app import JUDGE_FIRST_LINE as FAKE_LLM_JUDGE_FIRST_LINE

INVITE = DescribedAction(
    "core",
    "household.invite",
    None,
    "permissions",
    "household",
    {"role": "guest", "days": 2, "display_name": "Robin"},
)
ALLOWED = RightsSay(allowed=True, needs=None, question=None)
NO_HISTORY = PersonHistory(0, 0, 0, 0, None, 0)


def a_case(**changes: object) -> JudgeCase:
    """A case as the code would build it, for the tests that need no database."""
    case = JudgeCase(
        person={"name": "Alex", "role": "adult"},
        action={
            "type": "household.invite",
            "target": None,
            "facts": {"role": "guest", "days": 2},
            "says": "Invite Robin as a guest for 2 days.",
        },
        rights=ALLOWED,
        words=("Can you invite Robin for the weekend?",),
        history=NO_HISTORY,
        now={"weekday": "Saturday", "date": "2030-01-05", "hour": 10, "minute": 0, "zone": None},
        household=({"name": "Alex", "role": "adult"},),
        pending=(),
        language="en",
    )
    return replace(case, **changes)  # type: ignore[arg-type]


def test_the_fixed_values_are_the_plans():
    assert (JUDGE_SECONDS, JUDGE_MESSAGES, JUDGE_MESSAGE_CHARS) == (20.0, 5, 1000)
    # The plan gave the judge 80 tokens: a real model that thinks first needs more (2026-11-15).
    assert (JUDGE_OUTPUT_TOKENS, MAX_JUDGE_REASON_LENGTH) == (500, 200)
    # ``warn``: a yes to the code, that tells the person something first (spec 6.8).
    assert VERDICTS == ("do", "yes", "warn", "explicit_yes", "proof", "do_not")


# --- what the judge answers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,verdict,reason,fallback_kind",
    [
        ("do: looks usual for them", "do", "looks usual for them", None),
        ("YES", "yes", "", None),
        ("explicit_yes: a big change", "explicit_yes", "a big change", None),
        ("Warn: no way back", "warn", "no way back", None),
        ("Proof: never at this hour", "proof", "never at this hour", None),
        ("do_not: that would be rude", "do_not", "that would be rude", None),
        ("maybe", "do", "", "unreadable"),
        ("do: " + "x" * 300, "do", "x" * 200, None),
        ("do: first line\nsecond line", "do", "first line", None),
        ("  do  :  spaced  ", "do", "spaced", None),
        ("do_not: it is 3:00 at night", "do_not", "it is 3:00 at night", None),
        ("", "do", "", "unreadable"),
        # A verdict on the last line is read when the answer does not begin with one: a model
        # asked not to think first reasons aloud before it (a real one, 2026-11-15).
        ("Sure.\ndo: fine", "do", "fine", None),
        ("They did not ask.\n\nyes: tu confirmes ?\n", "yes", "tu confirmes ?", None),
        ("Sure.\nmaybe: fine", "do", "", "unreadable"),
        ("yes: first\ndo_not: last", "yes", "first", None),
        ("explicit yes: spelled apart", "do", "", "unreadable"),
    ],
)
def test_a_judgement_is_read_strictly(text, verdict, reason, fallback_kind):
    assert read_judgement(text) == Judgement(verdict, reason, fallback_kind)


def test_a_judgement_keeps_only_a_plain_line():
    # A direction mark or a control character can make a text look like another one on a card.
    assert read_judgement("do: a‏b") == Judgement("do", "")
    assert read_judgement("do: a\x07b") == Judgement("do", "")
    assert read_judgement("do: fine") == Judgement("do", "fine")


def test_a_judgement_says_what_it_asks_and_where_it_comes_from():
    assert [Judgement(verdict).as_ask() for verdict in VERDICTS] == [
        None,
        "yes",
        "yes",
        "explicit_yes",
        "proof",
        None,
    ]
    assert Judgement("do").source == "model"
    assert fallback("timeout") == Judgement("do", "", "timeout")
    assert fallback("timeout").source == "fallback"
    # Nobody is known to have asked (Domo acts on his own, or was told words from outside): a
    # judge that is silent means a yes, never a do.
    assert fallback("timeout", they_asked=False) == Judgement("yes", "", "timeout")
    assert fallback("timeout", they_asked=False).source == "fallback"


async def test_a_judge_that_fails_or_takes_too_long_or_calls_a_tool_asks_nothing(monkeypatch):
    case = a_case()
    # A judge that is silent asks nothing: what was asked for and understood is done.
    assert await judge(FakeChatModel(fail_after=0, judge=None), case) == Judgement(
        "do", "", "error"
    )

    monkeypatch.setattr(judge_module, "JUDGE_SECONDS", 0.05)
    # Two pieces: the fake pauses between two items it streams.
    slow = FakeChatModel(["do", ": fine"], delay_s=0.2, judge=None)
    assert await judge(slow, case) == Judgement("do", "", "timeout")
    assert slow.started == slow.closed == 1

    # The judge is given no tool: a model that calls one anyway is not understood.
    class CallsAnyway:
        calls = 0

        async def stream(self, **_):
            CallsAnyway.calls += 1
            yield ToolCall("c1", "household_invite", '{"display_name": "Robin"}')

    assert await judge(CallsAnyway(), case) == Judgement("do", "", "unreadable")
    assert CallsAnyway.calls == 1


async def test_a_silent_judge_means_a_yes_when_nobody_is_known_to_have_asked(monkeypatch):
    # The rule "asked for and understood is done" is about a request the person made. In a turn
    # that answers no message of theirs, or that was told words from outside, each way the judge
    # may be silent is a yes (review I2).
    case = a_case()
    assert await judge(None, case, they_asked=False) == Judgement("yes", "", "no_model")
    assert await judge(
        FakeChatModel(fail_after=0, judge=None), case, they_asked=False
    ) == Judgement("yes", "", "error")
    learns = Learns()
    unreadable = await judge(
        FakeChatModel(["perhaps"], judge=None), case, learns=learns, they_asked=False
    )
    assert unreadable == Judgement("yes", "", "unreadable")
    assert learns.noted == [("judge", "unreadable verdict")]
    assert read_judgement("maybe", they_asked=False) == Judgement("yes", "", "unreadable")

    class CallsAnyway:
        async def stream(self, **_):
            yield ToolCall("c1", "household_invite", '{"display_name": "Robin"}')

    assert await judge(CallsAnyway(), case, they_asked=False) == Judgement("yes", "", "unreadable")
    monkeypatch.setattr(judge_module, "JUDGE_SECONDS", 0.05)
    slow = FakeChatModel(["do", ": fine"], delay_s=0.2, judge=None)
    assert await judge(slow, case, they_asked=False) == Judgement("yes", "", "timeout")
    # A verdict the judge gave is the judge's, whoever asked.
    assert await judge(
        FakeChatModel(["do: fine"], judge=None), case, they_asked=False
    ) == Judgement("do", "fine")


class Learns:
    """Notes what the judge says went wrong with its model (``note_failure``)."""

    def __init__(self) -> None:
        self.noted: list[tuple[str, str]] = []

    async def note_failure(self, task: str, reason: str) -> None:
        self.noted.append((task, reason))


async def test_an_unreadable_verdict_is_noted_as_a_failure_of_the_judge():
    learns = Learns()
    case = a_case()

    said = await judge(FakeChatModel(["perhaps"], judge=None), case, learns=learns)

    assert said == Judgement("do", "", "unreadable")
    assert learns.noted == [("judge", "unreadable verdict")]
    # A verdict, a model that fails or none at all teach nothing about the model's answers.
    assert await judge(FakeChatModel(["do: fine"], judge=None), case, learns=learns) == Judgement(
        "do", "fine"
    )
    await judge(FakeChatModel(fail_after=0, judge=None), case, learns=learns)
    await judge(None, case, learns=learns)
    assert learns.noted == [("judge", "unreadable verdict")]


async def test_without_a_model_the_judge_asks_nothing():
    judgement = await judge(None, a_case())
    assert judgement == Judgement("do", "", "no_model")
    assert judgement.fallback == "no_model" and judgement.source == "fallback"


async def test_the_judge_is_called_apart_with_no_tool_and_no_history():
    model = FakeChatModel(["do: fine"], judge=None)
    case = a_case()
    assert await judge(model, case) == Judgement("do", "fine")
    [call] = model.calls
    assert call.tools == ()
    assert call.messages == [ChatMessage("user", json.dumps(case.as_json(), ensure_ascii=False))]
    assert call.system == judge_prompt()
    # One short line, asked without thinking first; the room is for a model that thinks anyway.
    assert call.think is False
    assert call.max_output_tokens == 500


# --- what the judge sees ------------------------------------------------------------------------


async def test_the_judge_sees_only_the_persons_words_of_this_conversation(
    session, add_person, add_conversation, add_message
):
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    here = await add_conversation(alex)
    elsewhere = await add_conversation(alex)
    said = [f"message {number}" for number in range(6)] + ["y" * 1500]
    for number, text in enumerate(said):
        await add_message(here, content=text)
        if number in (0, 2, 4):
            await add_message(
                here,
                role="assistant",
                provenance="model",
                content=f"The assistant says {number}",
                own_answer=True,
            )
    # What Domo wrote under the person's turn is never theirs, whatever its role.
    await add_message(here, role="user", provenance="model", content="The assistant was woken")
    await add_message(elsewhere, content="from another conversation")
    sams = await add_conversation(sam)
    await add_message(sams, content="Sam's own words")

    now = datetime(2030, 1, 5, 12, 30, tzinfo=UTC)  # a Saturday
    actor = Actor(alex.id, "adult")
    case = await build_case(
        session,
        actor=actor,
        action=INVITE,
        says="Invite Robin as a guest for 2 days.",
        rights=ALLOWED,
        conversation_id=here.id,
        now=now,
        zone=ZoneInfo("Europe/Paris"),
    )
    assert case.words == ("message 2", "message 3", "message 4", "message 5", "y" * 1000)
    # Of Domo's replies, only the last one before their last message: what they answered.
    assert case.asked_them == "The assistant says 4"
    shown = json.dumps(case.as_json(), ensure_ascii=False)
    assert "The assistant says 2" not in shown and "The assistant was woken" not in shown
    assert "another conversation" not in shown and "Sam's" not in shown
    assert case.person == {"name": "Alex", "role": "adult"}
    assert case.action == {
        "type": "household.invite",
        "target": None,
        "facts": {"role": "guest", "days": 2, "display_name": "Robin"},
        "says": "Invite Robin as a guest for 2 days.",
    }
    assert case.rights == ALLOWED
    assert case.now == {
        "weekday": "Saturday",
        "date": "2030-01-05",
        "hour": 13,
        "minute": 30,
        "zone": "Europe/Paris",
    }
    assert case.steps == () and case.request is None and case.owner_rule is None

    # Without the household's time zone, the moment is told in UTC, and says so.
    in_utc = await build_case(
        session,
        actor=actor,
        action=INVITE,
        says="Invite Robin.",
        rights=ALLOWED,
        conversation_id=here.id,
        now=now,
        zone=None,
    )
    assert in_utc.now == {
        "weekday": "Saturday",
        "date": "2030-01-05",
        "hour": 12,
        "minute": 30,
        "zone": None,
    }

    # Someone else's conversation gives no words: they are that person's own.
    not_theirs = await build_case(
        session,
        actor=actor,
        action=INVITE,
        says="Invite Robin.",
        rights=ALLOWED,
        conversation_id=sams.id,
        now=now,
        zone=None,
    )
    assert not_theirs.words == () and not_theirs.asked_them is None


async def test_the_case_holds_what_domo_had_just_said_to_the_person(
    session, add_person, add_conversation, add_message
):
    alex = await add_person("Alex", "adult")
    conversation = await add_conversation(alex)

    async def case_now() -> JudgeCase:
        return await build_case(
            session,
            actor=Actor(alex.id, "adult"),
            action=INVITE,
            says="Invite Robin.",
            rights=ALLOWED,
            conversation_id=conversation.id,
            now=datetime(2030, 1, 5, 12, 30, tzinfo=UTC),
            zone=None,
        )

    # Nothing said yet, then their first message: Domo had asked them nothing.
    assert (await case_now()).asked_them is None
    await add_message(conversation, content="Invite Robin for the weekend")
    case = await case_now()
    assert case.asked_them is None and "asked_them" not in case.as_json()

    # Domo asks, they answer: the judge reads what their yes answers, cut like their words.
    asked = "Robin would stay two days as a guest. Shall I? " + "z" * 1500
    await add_message(
        conversation, role="assistant", provenance="model", content=asked, own_answer=True
    )
    await add_message(conversation, content="yes")
    # The reply of this very turn, being written, comes after their words: never read.
    await add_message(
        conversation, role="assistant", provenance="model", content="Done.", status="streaming"
    )
    case = await case_now()
    assert case.words == ("Invite Robin for the weekend", "yes")
    assert case.asked_them == asked[:JUDGE_MESSAGE_CHARS]
    assert case.as_json()["asked_them"] == asked[:JUDGE_MESSAGE_CHARS]
    # An empty reply (Domo answered nothing) asked them nothing.
    await add_message(
        conversation, role="assistant", provenance="model", content="", own_answer=True
    )
    await add_message(conversation, content="and Dana too")
    assert (await case_now()).asked_them is None


async def test_a_reply_that_relayed_someone_is_in_the_case_and_says_so(
    session, add_person, add_conversation, add_message
):
    # The owner's decision (2026-11-17): his yes to what Domo just told him counts, even when
    # Domo was telling him a word someone left (or spoke first). The judge reads that reply as
    # the person read it, with "relayed": never the note itself, nor an older reply in its place.
    alex = await add_person("Alex", "owner")
    conversation = await add_conversation(alex)

    async def case_now() -> JudgeCase:
        return await build_case(
            session,
            actor=Actor(alex.id, "owner"),
            action=INVITE,
            says="Invite Robin.",
            rights=ALLOWED,
            conversation_id=conversation.id,
            now=datetime(2030, 1, 5, 12, 30, tzinfo=UTC),
            zone=None,
        )

    # A plain question of Domo, in answer to them: it is in the case.
    await add_message(conversation, content="Invite Robin")
    await add_message(
        conversation,
        role="assistant",
        provenance="model",
        content="As a guest for two days?",
        own_answer=True,
    )
    await add_message(conversation, content="yes")
    case = await case_now()
    assert case.asked_them == "As a guest for two days?"
    # His own answer is not relayed: the key is absent.
    assert case.relayed is False and "relayed" not in case.as_json()

    # The reply that told a note (not his own answer): it is what the person read, so it is in
    # the case, said to be relayed; the older reply is not read in its place.
    told = "Dana asks you to add her son Paul as a child. Shall I?"
    await add_message(conversation, role="assistant", provenance="model", content=told)
    await add_message(conversation, content="Oui, crée le")
    case = await case_now()
    assert case.words == ("Invite Robin", "yes", "Oui, crée le")
    assert case.asked_them == told and case.relayed is True
    shown = case.as_json()
    assert shown["asked_them"] == told and shown["relayed"] is True
    assert "two days" not in json.dumps(shown, ensure_ascii=False)

    # An empty relayed reply asked nothing: neither key.
    await add_message(conversation, role="assistant", provenance="model", content=" ")
    await add_message(conversation, content="and?")
    case = await case_now()
    assert case.asked_them is None and case.relayed is False
    assert "asked_them" not in case.as_json() and "relayed" not in case.as_json()


async def test_the_judge_never_reads_whether_domo_sends_an_invitation_to_the_owner_first(
    session, add_person, add_conversation
):
    # Review of trial fix 1 (ruling): owner_first is Domo's own judgement for the barrier; the
    # judge would read it as a sign of doubt. The barrier, the limit and the audit keep it.
    alex = await add_person("Alex", "adult")
    conversation = await add_conversation(alex)
    invite = DescribedAction(
        "core",
        "household.invite",
        None,
        "permissions",
        "household",
        {"role": "guest", "days": 2, "display_name": "Robin", "owner_first": True},
    )
    case = await build_case(
        session,
        actor=Actor(alex.id, "adult"),
        action=invite,
        says="Invite Robin.",
        rights=ALLOWED,
        conversation_id=conversation.id,
        now=datetime(2030, 1, 5, 12, 30, tzinfo=UTC),
        zone=None,
    )
    assert case.action["facts"] == {"role": "guest", "days": 2, "display_name": "Robin"}
    assert "owner_first" not in json.dumps(case.as_json())
    # The action itself is untouched.
    assert invite.facts["owner_first"] is True


async def test_a_turn_a_plan_woke_reads_neither_what_domo_said_nor_relayed(
    session, add_person, add_conversation, add_message
):
    alex = await add_person("Alex", "owner")
    conversation = await add_conversation(alex)
    await add_message(conversation, content="Invite Robin")
    await add_message(
        conversation, role="assistant", provenance="model", content="Dana asks for Paul. Shall I?"
    )
    await add_message(conversation, content="yes")
    case = await build_case(
        session,
        actor=Actor(alex.id, "owner"),
        action=INVITE,
        says="Invite Robin.",
        rights=ALLOWED,
        conversation_id=conversation.id,
        now=datetime(2030, 1, 5, 12, 30, tzinfo=UTC),
        zone=None,
        words_override=["invite Robin at noon"],
    )
    assert case.words == ("invite Robin at noon",)
    assert case.asked_them is None and case.relayed is False
    shown = case.as_json()
    assert "asked_them" not in shown and "relayed" not in shown


def _request(person: Person, conversation_id, action_type: str, status: str, **values):
    return Confirmation(
        requester_id=person.id,
        approver_kind="self",
        conversation_id=conversation_id,
        tool_name=action_type.replace(".", "_"),
        arguments={},
        action={"action_type": action_type},
        ask="yes",
        status=status,
        **values,
    )


async def test_the_judge_writes_its_reason_in_the_askers_language(
    session, add_person, add_conversation
):  # scenario 30
    sam = await add_person("Sam", "child")
    conversation = await add_conversation(sam)

    async def case_of_sam() -> JudgeCase:
        return await build_case(
            session,
            actor=Actor(sam.id, "child"),
            action=INVITE,
            says="Invite Robin as a guest for 2 days.",
            rights=ALLOWED,
            conversation_id=conversation.id,
            now=datetime(2030, 1, 5, 12, 30, tzinfo=UTC),
            zone=None,
        )

    # Sam has no language of his own yet: he reads the house's, English.
    case = await case_of_sam()
    assert case.language == "en" and case.as_json()["language"] == "en"
    sam.language = "pt-BR"
    await session.flush()
    case = await case_of_sam()
    assert case.language == "pt-BR"
    # The judge is told the language, and that its reason is written in it.
    model = FakeChatModel(["yes: primeira vez"], judge=None)
    assert await judge(model, case) == Judgement("yes", "primeira vez")
    [call] = model.calls
    assert json.loads(call.messages[0].content)["language"] == "pt-BR"
    prompt = judge_prompt()
    assert '"language"' in prompt and "reason" in prompt


async def test_the_case_says_a_first_time_and_the_counts(
    session, add_person, add_conversation, add_message
):
    alex = await add_person("Alex", "adult")
    now = datetime.now(UTC)
    nothing = await person_history(session, alex.id, "household.invite", now)
    assert nothing == PersonHistory(0, 0, 0, 0, None, 0)
    assert nothing.first_time

    conversation = await add_conversation(alex)
    reply = await add_message(conversation, role="assistant", provenance="model")
    decided = now - timedelta(days=40)
    session.add_all(
        [
            _request(alex, conversation.id, "household.invite", "executed", decided_at=decided),
            _request(alex, conversation.id, "household.invite", "refused", decided_at=decided),
            _request(alex, conversation.id, "household.invite", "expired"),
            # Not of this type, or not refused nor dropped.
            _request(alex, conversation.id, "household.remove", "refused"),
            _request(alex, conversation.id, "household.invite", "replaced"),
        ]
    )
    for status, reason in (("refused", "judge"), ("refused", "not_allowed"), ("invalid", "judge")):
        # The judge's own refusals create no request: they are counted by their card.
        session.add(
            MessageAction(
                message_id=reply.id,
                tool_name="household_invite",
                action_type="household.invite",
                status=status,
                summary={"reason": reason},
                created_at=now - timedelta(hours=1),
            )
        )
    await session.flush()

    history = await person_history(session, alex.id, "household.invite", now)
    assert history == PersonHistory(0, 0, 0, 1, decided, 3)
    assert not history.first_time

    # The last one is the newest of every kind of action done.
    done_at = now - timedelta(hours=2)
    session.add(
        MessageAction(
            message_id=reply.id,
            tool_name="household_invite",
            action_type="household.invite",
            status="done",
            summary={},
            created_at=done_at,
        )
    )
    await session.flush()
    assert await person_history(session, alex.id, "household.invite", now) == PersonHistory(
        1, 1, 1, 2, done_at, 3
    )


async def test_the_case_names_who_is_in_the_household_and_tells_a_waiting_note_without_its_words(
    session, add_person, add_conversation
):
    await add_person("Robin", "guest")
    sam = await add_person("Sam", "child")
    await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    await add_person("Dana", "adult")
    session.add(Person(display_name="Gone", role="adult", disabled_at=datetime.now(UTC)))
    conversation = await add_conversation(sam)
    note = DescribedAction(
        "core",
        "note.leave",
        f"person:{alex.id}",
        "message",
        "household",
        {"recipient": f"person:{alex.id}", "recipient_role": "adult", "length": 19},
    )
    rename = DescribedAction("core", "profile.rename", None, "self_profile", "household")
    session.add(
        Confirmation(
            requester_id=sam.id,
            approver_kind="self",
            conversation_id=conversation.id,
            tool_name="profile_rename",
            arguments={"name": "Sammy"},
            action=rename.as_json(),
            ask="yes",
            status="pending",
        )
    )
    await session.flush()
    session.add(
        Confirmation(
            requester_id=sam.id,
            approver_kind="adult",
            conversation_id=conversation.id,
            tool_name="note_leave",
            arguments={"recipient": "Alex", "text": "meet me at the park"},
            action=note.as_json(),
            ask="yes",
            status="pending",
        )
    )
    await session.flush()

    case = await build_case(
        session,
        actor=Actor(sam.id, "child"),
        action=INVITE,
        says="Invite Robin.",
        rights=RightsSay(allowed=True, needs="adult", question=None),
        conversation_id=conversation.id,
        now=datetime.now(UTC),
        zone=None,
    )
    assert case.household == (
        {"name": "Owner", "role": "owner"},
        {"name": "Alex", "role": "adult"},
        {"name": "Dana", "role": "adult"},
        {"name": "Sam", "role": "child"},
        {"name": "Robin", "role": "guest"},
    )
    # Their own request in their words; the one that waits for an adult without the note's.
    assert case.pending == ("Change your name to “Sammy”.", "a note to Alex (19 characters)")
    assert "park" not in json.dumps(case.as_json(), ensure_ascii=False)
    assert case.rights == RightsSay(allowed=True, needs="adult", question=None)


def test_the_steps_of_a_chain_are_told_in_words():
    arguments = {"display_name": "Robin", "role": "guest", "days": 2}
    planned = PlannedStep("household_invite", "household.invite", arguments, INVITE, "confirm")
    assert planned_step_told(planned) == StepTold(
        "household.invite", describe_for_people(INVITE, arguments, CatalogTexts("en")), False, None
    )
    # A step checked in its turn has no described action: the sentence of its type, in words.
    in_its_turn = PlannedStep(
        "profile_rename", "profile.rename", {"name": "Sammy"}, None, after_arrival_of="Robin"
    )
    assert planned_step_told(in_its_turn) == StepTold(
        "profile.rename", "Change your name to “Sammy”. (checked in its turn)", True, "Robin"
    )
    unknown = PlannedStep("nothing", "no.such_type", {}, None)
    assert planned_step_told(unknown).says == (
        "An action the assistant was asked to do. (checked in its turn)"
    )
    row = ConfirmationStep(
        position=0,
        tool_name="profile_rename",
        action_type="profile.rename",
        arguments={"name": "Sammy"},
        action=None,
        status="planned",
        after_arrival_of="Robin",
    )
    assert step_told(row) == StepTold(
        "profile.rename", step_sentence(row, CatalogTexts("en")), True, "Robin"
    )


async def test_the_last_time_is_told_in_the_households_day_with_how_long_ago_it_was(
    session, add_person, add_conversation, add_message
):
    alex = await add_person("Alex", "adult")
    conversation = await add_conversation(alex)
    reply = await add_message(conversation, role="assistant", provenance="model")
    # Late in the UTC evening: already the next day in Paris (UTC+1 in January).
    session.add(
        MessageAction(
            message_id=reply.id,
            tool_name="household_invite",
            action_type="household.invite",
            status="done",
            summary={},
            created_at=datetime(2030, 1, 1, 23, 30, tzinfo=UTC),
        )
    )
    await session.flush()
    now = datetime(2030, 1, 5, 23, 30, tzinfo=UTC)  # Sunday 6 January, 00:30, in Paris

    async def told(zone: ZoneInfo | None) -> JudgeCase:
        return await build_case(
            session,
            actor=Actor(alex.id, "adult"),
            action=INVITE,
            says="Invite Robin.",
            rights=ALLOWED,
            conversation_id=conversation.id,
            now=now,
            zone=zone,
        )

    in_paris = (await told(ZoneInfo("Europe/Paris"))).as_json()
    assert in_paris["now"] == {
        "weekday": "Sunday",
        "date": "2030-01-06",
        "hour": 0,
        "minute": 30,
        "zone": "Europe/Paris",
    }
    assert (in_paris["history"]["last"], in_paris["history"]["days_ago"]) == ("2030-01-02", 4)
    # Without the household's time zone, both are told in UTC.
    in_utc = (await told(None)).as_json()
    assert in_utc["now"]["date"] == "2030-01-05"
    assert (in_utc["history"]["last"], in_utc["history"]["days_ago"]) == ("2030-01-01", 4)
    # Today: 0 days ago.
    same_day = a_case(
        now={
            "weekday": "Wednesday",
            "date": "2030-01-02",
            "hour": 8,
            "minute": 0,
            "zone": "Europe/Paris",
        },
        history=PersonHistory(1, 1, 1, 1, datetime(2030, 1, 1, 23, 30, tzinfo=UTC), 0),
    ).as_json()
    assert (same_day["history"]["last"], same_day["history"]["days_ago"]) == ("2030-01-02", 0)
    # Never done: no last time, and no days ago.
    assert "days_ago" not in a_case().as_json()["history"]


def test_the_case_is_data_the_judge_reads_as_json():
    base = {
        "person",
        "action",
        "rights",
        "words",
        "history",
        "now",
        "household",
        "pending",
        "language",
    }
    data = a_case().as_json()
    assert set(data) == base
    assert data["history"] == {
        "day": 0,
        "week": 0,
        "month": 0,
        "ever": 0,
        "last": None,
        "refused_or_dropped": 0,
        "first_time": True,
    }
    assert data["rights"] == {"allowed": True, "needs": None, "question": None}
    assert data["words"] == ["Can you invite Robin for the weekend?"]

    last = datetime(2030, 1, 1, 23, 30, tzinfo=UTC)
    step = StepTold(
        type="household.invite",
        says="Invite Robin.",
        checked_in_turn=False,
        waits_for=None,
    )
    request = RequestTold(
        yes=YesTold(when="2 hours ago", by="the person", how="button"),
        done=("Invite Robin.",),
        failed="Change Robin's role.",
        failed_reason="timeout",
        may_have_acted=True,
        before=("Change Robin's role to adult.",),
        after=("Change Robin's role to child.",),
        asked="explicit_yes",
    )
    rule = {"never_ask": True, "when": None, "at_all": False}
    full = a_case(
        history=PersonHistory(1, 2, 3, 4, last, 1),
        steps=(step,),
        request=request,
        owner_rule=rule,
    ).as_json()
    assert set(full) == base | {"steps", "request", "owner_rule"}
    assert full["history"] == {
        "day": 1,
        "week": 2,
        "month": 3,
        "ever": 4,
        "last": "2030-01-01",
        "days_ago": 4,
        "refused_or_dropped": 1,
        "first_time": False,
    }
    assert full["steps"] == [
        {
            "type": "household.invite",
            "says": "Invite Robin.",
            "checked_in_turn": False,
            "waits_for": None,
        }
    ]
    assert full["request"]["asked"] == "explicit_yes"
    assert full["request"]["yes"] == {"when": "2 hours ago", "by": "the person", "how": "button"}
    assert full["request"]["may_have_acted"] is True
    assert full["owner_rule"] == rule
    # Plain JSON, sendable as it is: a lone surrogate the person typed is replaced.
    assert json.loads(json.dumps(full)) == full
    assert a_case(words=("a\ud800b",)).as_json()["words"] == ["a�b"]


async def test_the_owners_never_ask_reaches_the_case_with_its_conditions(
    session, add_person, add_conversation
):
    assert owner_rule_of(None) is None
    assert owner_rule_of(NeverAsk(None)) == {"never_ask": True, "when": None, "at_all": False}
    assert owner_rule_of(NeverAsk("only for guests")) == {
        "never_ask": True,
        "when": "only for guests",
        "at_all": False,
    }
    assert owner_rule_of(NeverAsk(None, at_all=True))["at_all"] is True

    owner = await add_person("Owner", "owner")
    conversation = await add_conversation(owner)
    rule = owner_rule_of(NeverAsk("only for guests"))
    case = await build_case(
        session,
        actor=Actor(owner.id, "owner"),
        action=INVITE,
        says="Invite Robin.",
        rights=ALLOWED,
        conversation_id=conversation.id,
        now=datetime.now(UTC),
        zone=None,
        owner_rule=rule,
    )
    assert case.as_json()["owner_rule"] == rule


# --- what is asked in the end -------------------------------------------------------------------


@pytest.mark.parametrize(
    "judgement,rule,raised,expected",
    [
        (Judgement("do"), None, None, None),
        (Judgement("yes", "odd"), None, None, Asked("yes", "judge", "odd")),
        (Judgement("do"), "yes", None, Asked("yes", "rule", None)),
        (
            Judgement("yes"),
            None,
            ("explicit_yes", "unsure"),
            Asked("explicit_yes", "domo", "unsure"),
        ),
        (
            Judgement("proof", "never"),
            "yes",
            ("explicit_yes", "x"),
            Asked("proof", "judge", "never"),
        ),
        (Judgement("do"), "explicit_yes", None, Asked("explicit_yes", "rule", None)),
        # A judge that did not answer asks nothing; the owner's own rule still asks what it asks.
        (fallback("error"), None, None, None),
        (fallback("no_model"), "proof", None, Asked("proof", "rule", None)),
        # Equal asks: the first of rule, judge, domo.
        (Judgement("yes", "odd"), "yes", ("yes", "x"), Asked("yes", "rule", None)),
        (Judgement("yes", "odd"), None, ("yes", "x"), Asked("yes", "judge", "odd")),
    ],
)
def test_the_most_demanding_wins_and_says_who_asked(judgement, rule, raised, expected):
    assert what_to_ask(judgement, rule=rule, raised=raised) == expected


@pytest.mark.parametrize(
    "judgement,raised,expected",
    [
        (Judgement("yes", "to be sure"), None, None),
        (Judgement("explicit_yes", "x"), None, None),
        (
            Judgement("proof", "not sure it is you"),
            None,
            Asked("proof", "judge", "not sure it is you"),
        ),
        (Judgement("do"), ("explicit_yes", "unsure"), None),
        (Judgement("do"), ("proof", "odd"), Asked("proof", "domo", "odd")),
    ],
)
def test_not_at_all_drops_every_yes_but_a_proof(judgement, raised, expected):
    not_at_all = NeverAsk(None, at_all=True)
    assert what_to_ask(judgement, rule=None, raised=raised, never_ask=not_at_all) == expected
    # A rule still asks.
    with_a_rule = what_to_ask(judgement, rule="yes", raised=raised, never_ask=not_at_all)
    assert with_a_rule == (expected or Asked("yes", "rule", None))


def test_what_to_ask_refuses_a_do_not():
    with pytest.raises(ValueError):
        what_to_ask(Judgement("do_not", "x"), rule=None, raised=None)


def step_of(action_type: str, rule=None, never_ask=None) -> PlannedStep:
    """A step of a chain planned again, with the owner's rule and "never ask me" on it."""
    return PlannedStep("tool", action_type, {}, None, never_ask=never_ask, rule=rule)


INVITE_STEP = step_of("household.invite", rule="explicit_yes")
HAND_OVER_STEP = step_of("household.transfer_ownership")
NOTE_STEP = step_of("note.leave")


def test_a_change_counts_every_step_and_never_asks_less_than_before():  # F2
    # Nobody said yes yet: the rule of a step kept counts.
    assert ask_after_a_change(
        Judgement("do"),
        old_ask=None,
        steps=[NOTE_STEP, INVITE_STEP],
        new=[True, False],
        raised=None,
        never_ask=None,
        yes_already_given=False,
    ) == Asked("explicit_yes", "rule", None)
    # No type asks anything by itself: the hand-over of the owner's role neither.
    assert (
        ask_after_a_change(
            Judgement("do"),
            old_ask=None,
            steps=[NOTE_STEP, HAND_OVER_STEP],
            new=[True, False],
            raised=None,
            never_ask=None,
            yes_already_given=False,
        )
        is None
    )
    # What the request asked stays the least, when words never give it, with who asked and why.
    before = Asked("proof", "judge", "unusual")
    for asked in (before, Asked("explicit_yes", "domo", "odd")):
        assert (
            ask_after_a_change(
                Judgement("do"),
                old_ask=asked,
                steps=[NOTE_STEP],
                new=[True],
                raised=None,
                never_ask=None,
                yes_already_given=False,
            )
            == asked
        )
    # A plain yes asked before is given by the words: not a minimum.
    assert (
        ask_after_a_change(
            Judgement("do"),
            old_ask=Asked("yes", "judge", None),
            steps=[NOTE_STEP],
            new=[True],
            raised=None,
            never_ask=None,
            yes_already_given=False,
        )
        is None
    )
    # The owner's rule on chains counts for a change.
    assert ask_after_a_change(
        Judgement("do"),
        old_ask=None,
        steps=[NOTE_STEP],
        new=[False],
        raised=None,
        never_ask=None,
        yes_already_given=False,
        chain_rule="yes",
    ) == Asked("yes", "rule", None)


def test_a_yes_given_covers_what_it_was_given_to():  # F2 (4): chain_continue
    kept = {
        "old_ask": Asked("explicit_yes", "rule", None),
        "steps": [INVITE_STEP, HAND_OVER_STEP],
        "raised": None,
        "never_ask": None,
        "yes_already_given": True,
        "yes_gave": "explicit_yes",
        "chain_rule": "proof",
    }
    assert ask_after_a_change(Judgement("do"), new=[False, False], **kept) is None
    assert ask_after_a_change(Judgement("do"), new=[True, False], **kept) == Asked(
        "explicit_yes", "rule", None
    )
    # A step added that no rule is about asks nothing more.
    assert ask_after_a_change(Judgement("do"), new=[False, True], **kept) is None
    assert ask_after_a_change(Judgement("yes", "check"), new=[False, False], **kept) == Asked(
        "yes", "judge", "check"
    )


def test_a_kept_step_counts_what_the_owner_asks_above_what_the_yes_gave():  # final review M14
    def asked(step: PlannedStep, gave: str | None) -> Asked | None:
        return ask_after_a_change(
            Judgement("do"),
            old_ask=None,
            steps=[step],
            new=[False],
            raised=None,
            never_ask=None,
            yes_already_given=True,
            yes_gave=gave,  # type: ignore[arg-type]
        )

    # A yes in words never gave an explicit yes: the rule asks it of a step kept, too.
    assert asked(INVITE_STEP, "yes") == Asked("explicit_yes", "rule", None)
    assert asked(HAND_OVER_STEP, "yes") is None
    # The button gave it, a proof gives everything.
    assert asked(INVITE_STEP, "explicit_yes") is None
    proof_step = step_of("household.invite", rule="proof")
    assert asked(proof_step, "explicit_yes") == Asked("proof", "rule", None)
    assert asked(proof_step, "proof") is None
    # The judge's leave gave nothing: whatever the rules ask counts.
    assert asked(step_of("household.invite", rule="yes"), None) == Asked("yes", "rule", None)
    assert asked(NOTE_STEP, None) is None


def test_after_a_change_the_judge_and_domo_come_first_on_a_tie():
    rule_yes = [step_of("household.invite", rule="yes")]
    common = {"old_ask": None, "steps": rule_yes, "new": [True], "never_ask": None}
    asked = ask_after_a_change(
        Judgement("yes", "hmm"), raised=None, yes_already_given=False, **common
    )
    assert asked == Asked("yes", "judge", "hmm") and not given_by_the_change(asked)
    asked = ask_after_a_change(
        Judgement("do"), raised=("yes", "not sure"), yes_already_given=False, **common
    )
    assert asked == Asked("yes", "domo", "not sure") and not given_by_the_change(asked)
    asked = ask_after_a_change(Judgement("do"), raised=None, yes_already_given=False, **common)
    assert asked == Asked("yes", "rule", None) and given_by_the_change(asked)


def test_what_the_words_of_a_change_give():
    assert given_by_the_change(None)
    assert given_by_the_change(Asked("yes", "rule", None))
    for by in ("judge", "domo", "approver"):
        assert not given_by_the_change(Asked("yes", by, None))
    for ask in ("explicit_yes", "proof"):
        for by in ("rule", "judge"):
            assert not given_by_the_change(Asked(ask, by, None))


def test_not_at_all_drops_the_judges_yes_after_a_change_too():
    not_at_all = NeverAsk(None, at_all=True)
    lifted = step_of("household.transfer_ownership", never_ask=not_at_all)
    common = {"old_ask": None, "steps": [lifted], "new": [True], "raised": ("yes", "x")}
    assert (
        ask_after_a_change(
            Judgement("explicit_yes", "x"), never_ask=not_at_all, yes_already_given=False, **common
        )
        is None
    )
    assert ask_after_a_change(
        Judgement("proof", "is it you"), never_ask=not_at_all, yes_already_given=False, **common
    ) == Asked("proof", "judge", "is it you")


def test_ask_after_a_change_refuses_a_do_not_and_steps_without_their_news():
    with pytest.raises(ValueError):
        ask_after_a_change(
            Judgement("do_not", "x"),
            old_ask=None,
            steps=[NOTE_STEP],
            new=[True],
            raised=None,
            never_ask=None,
            yes_already_given=False,
        )
    with pytest.raises(ValueError):
        ask_after_a_change(
            Judgement("do"),
            old_ask=None,
            steps=[NOTE_STEP],
            new=[],
            raised=None,
            never_ask=None,
            yes_already_given=False,
        )


def test_how_long_ago_is_said_in_plain_words():
    now = datetime(2030, 1, 4, 12, 0, tzinfo=UTC)
    assert ago(now, now) == "just now"
    assert ago(now - timedelta(seconds=59), now) == "just now"
    assert ago(now - timedelta(minutes=1), now) == "1 minute ago"
    assert ago(now - timedelta(minutes=5), now) == "5 minutes ago"
    assert ago(now - timedelta(hours=2, minutes=30), now) == "2 hours ago"
    assert ago(now - timedelta(days=3, hours=1), now) == "3 days ago"
    assert ago(now + timedelta(minutes=1), now) == "just now"


def test_the_judge_prompt_says_what_it_needs_to():
    prompt = judge_prompt()
    first_line = prompt.splitlines()[0]
    assert first_line == fakes.JUDGE_FIRST_LINE == FAKE_LLM_JUDGE_FIRST_LINE
    for needed in (
        "data",
        # What was asked for is done, with no "are you sure" (the owner, 2026-11-15)...
        "is done: answer do",
        "Nobody is asked again whether they are sure",
        # What cannot be undone: only whether they know it matters (the owner, 2026-11-15).
        "what matters is whether they know that",
        # ... and it warns them once, then what it told them is remembered (spec 6.8).
        "if nothing does, warn, and your reason tells them",
        "- warn: ask their yes only because of something they must know first; your reason "
        "tells it;",
        '"remembered": what this person said before, and what you already told them; if it '
        "shows they know, do.",
        # ... and whether they asked for it is what the judge looks at, never a premise it is
        # given (review I3).
        "The assistant is about to do an action for a person",
        'Look whether this action is what the person asked for in "words"',
        'If nothing in "words" or "asked_them" shows they asked for it, they did not: yes',
        # ... but for what the later paragraphs say: a yes already given, the owner's "never
        # ask" (re-review m3: which one wins is said).
        'they did not: yes (never do_not for that alone), unless "request" or "never_ask" '
        "below says otherwise.",
        # What their last message says now; the earlier ones were answered (trial 2026-11-17).
        "the last one is what they say now",
        # A yes to what the reply spoke of is given, even when it relayed someone's word or
        # spoke first, but only for what it plainly names (the owner, 2026-11-17).
        "When it spoke of this very action and their last message says yes to it, that yes is "
        "given: do",
        'With "relayed" true',
        "only for an action the reply itself plainly names",
        # do_not is never a missing ask.
        "never because you only miss their ask (that is yes)",
        # With the owner's rule, where it belongs (review M8).
        "not outside content. Never lower what it asks.",
        "do_not:",
        "<verdict>: <reason>",
        "bereavement",
        "owner_rule",
        "at_all",
        "without bothering them",
        "speaker",
        # The reason is read by the person as written: said to them, as Domo speaks (a real
        # model wrote "vous", 2026-11-15).
        "said to the person, in their language",
        'informally ("tu" in French)',
    ):
        assert needed in prompt, needed
    # A proof is any of the person's agreed means, whatever the channel offers (spec 6.4).
    assert "one of their agreed means of proof" in prompt
    assert "passkey" not in prompt and "password" not in prompt
    # A verdict is the same on every channel: none of them names a button some channels lack.
    assert "button" not in prompt
    # No list of what should make it ask, and no leaning towards asking.
    assert "Be wary" not in prompt and "unusual" not in prompt
    assert "answer yes" not in prompt and "In doubt" not in prompt
    # The person having asked is never stated as a fact.
    assert "asked their assistant" not in prompt and "the assistant understood" not in prompt
    # Raised from 3 700 on the controller's ruling (trial fix 2): "relayed" and the last message;
    # then to 4 200 for the sentence that reads the last message with the reply (the owner's own
    # exchange, "Oui, crée le" after a relayed word, still got a second question on a real model).
    assert "Read their last message with that reply" in prompt
    assert len(prompt) <= 4200


async def test_the_judgement_is_audited_with_its_reason(session, add_person):
    owner = await add_person("Owner", "owner")
    actor = Actor(owner.id, "owner")
    await audit_judgement(
        session,
        actor=actor,
        tool_name="household_invite",
        action=INVITE,
        judgement=Judgement("proof", "never at this hour"),
    )
    entry = await last_audit(session, "judge.verdict")
    assert entry.actor == f"person:{owner.id}"
    assert entry.details == {
        "tool": "household_invite",
        "action_type": "household.invite",
        "target": None,
        "verdict": "proof",
        "reason": "never at this hour",
        "source": "model",
    }

    await audit_judgement(
        session,
        actor=actor,
        tool_name="household_invite",
        action=INVITE,
        judgement=fallback("timeout"),
    )
    entry = await last_audit(session, "judge.verdict")
    assert (entry.details["verdict"], entry.details["reason"], entry.details["source"]) == (
        "do",
        "",
        "fallback",
    )


# --- a warning, and what the judge remembers (spec 6.8) -----------------------------------------


def test_warn_asks_a_yes_like_yes():
    warned = Judgement("warn", "no way back")
    assert warned.as_ask() == "yes" and warned.warns
    assert what_to_ask(warned, rule=None, raised=None) == Asked(
        "yes", "judge", "no way back", warned=True
    )
    # Everything else as for a yes: a rule that asks more wins, and the warning is not its own.
    assert what_to_ask(warned, rule="explicit_yes", raised=None) == Asked(
        "explicit_yes", "rule", None
    )
    assert what_to_ask(Judgement("yes", "hmm"), rule=None, raised=None) == Asked(
        "yes", "judge", "hmm"
    )
    # A warning that says nothing warns of nothing.
    assert not Judgement("warn").warns
    assert what_to_ask(Judgement("warn"), rule=None, raised=None) == Asked("yes", "judge", None)
    # The owner's "not at all": no yes is asked, so no warning either.
    at_all = NeverAsk(None, at_all=True)
    assert what_to_ask(warned, rule=None, raised=None, never_ask=at_all) is None
    assert ask_after_a_change(
        warned,
        old_ask=None,
        steps=[],
        new=[],
        raised=None,
        never_ask=None,
        yes_already_given=False,
    ) == Asked("yes", "judge", "no way back", warned=True)


def test_the_case_tells_what_the_judge_remembers_only_when_there_is_some():
    assert "remembered" not in a_case().as_json()
    line = RememberedLine("no way back", "2030-01-01", "told_by_domo")
    assert a_case(remembered=(line,)).as_json()["remembered"] == [
        {"content": "no way back", "when": "2030-01-01", "origin": "told_by_domo"}
    ]


async def test_the_judge_reads_what_it_told_them_before(adult_chat, chat_server):
    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="warn: you cannot take it back"
    )
    assert reply.pending_confirmation_id is not None
    await adult_chat.say("oui")
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex C"}, judge="do: they know")
    async with chat_server.session_factory() as reader:
        [memory] = (
            await reader.scalars(select(Memory).where(Memory.origin == "told_by_domo"))
        ).all()
    cases = [
        json.loads(call.messages[0].content)
        for call in chat_server.calls
        if fakes.is_judge_call(call.system)
    ]
    # Nothing was remembered the first time; the second time, the warning, with its day.
    assert "remembered" not in cases[0]
    assert cases[-1]["remembered"] == [
        {
            "content": "you cannot take it back",
            "when": memory.changed_at.astimezone(UTC).date().isoformat(),
            "origin": "told_by_domo",
        }
    ]


async def test_memories_that_cannot_be_read_never_stop_the_judge(
    adult_chat, chat_server, monkeypatch
):
    async def broken(*_: object, **__: object) -> list[Memory]:
        raise RuntimeError("the memories are gone")

    monkeypatch.setattr(execution_module, "for_judge", broken)
    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="do: they asked"
    )
    assert reply.actions == [{"tool": "profile_rename", "status": "done"}]
    [case] = [
        json.loads(call.messages[0].content)
        for call in chat_server.calls
        if fakes.is_judge_call(call.system)
    ]
    assert "remembered" not in case


async def test_the_judge_reads_no_trust(session, add_person, add_conversation, add_message):
    """Trust per person (spec 6.4): what a person's word has been worth is Domo's to weigh,
    never the judge's. Nothing of what it reads has room for a figure or a fact, and a case built
    for someone with facts holds none."""
    names = {field.name for field in (*fields(JudgeCase), *fields(RememberedLine))}
    names |= set(inspect.signature(build_case).parameters)
    assert not any("trust" in name for name in names), names
    alex = await add_person("Alex", "adult")
    here = await add_conversation(alex)
    await add_message(here, content="rename me")
    now = datetime(2030, 1, 5, 12, 30, tzinfo=UTC)
    about = "said the boiler was fixed and it was not"
    for outcome in ("lied", "held"):
        await note_fact(
            session, person_id=alex.id, outcome=outcome, established="seen", about=about, now=now
        )
    case = await build_case(
        session,
        actor=Actor(alex.id, "adult"),
        action=INVITE,
        says="Invite Robin.",
        rights=ALLOWED,
        conversation_id=here.id,
        now=now,
        zone=None,
    )
    shown = json.dumps(case.as_json(), ensure_ascii=False)
    assert '"trust"' not in shown and about not in shown
