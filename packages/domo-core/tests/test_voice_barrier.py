"""Someone the code is not sure of is judged, not refused (spec 6.5 of the voice, audit POL-4).

The barrier decides with the rights of the person who probably speaks. What is ordinary
(``barrier.may_act_weakly``: a launch follows the risks its operations declare) is done as for
anyone, unless the owner said to judge it too (``voice.ordinary_when_unsure``); what is sensitive
is marked ``judge_identity`` and the yes-judge weighs it with the clues, a silent judge asking a
proof. Nobody known may not act. The socle comes first and is never lowered: a missing right, the
owner's "ask me first", a proof the rights ask."""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice.prints import NOT_LEARNED, Reading, read_voice
from domo_core.channels.voice.speaker import voice_speaker
from domo_core.chat.models import Conversation, Message
from domo_core.confirmations.judge import JudgeCase, Judgement, fallback
from domo_core.confirmations.models import Confirmation
from domo_core.conversation import execution as execution_module
from domo_core.conversation.execution import handle_tool_call
from domo_core.conversation.speaker import Clue, Speaker, actor_of
from domo_core.llm.speech.client import Print
from domo_core.llm.types import ToolCall, ToolDefinition
from domo_core.madetools.launch import ForWhom, launch, reach_of
from domo_core.madetools.models import MadeTool, MadeToolTrigger
from domo_core.madetools.offered import MadeToolOffer, offered_name
from domo_core.madetools.settings import set_for_tool
from domo_core.policy import barrier
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, NeedsApproval, Refuse, may_act_weakly
from domo_core.policy.catalog import (
    ALLOWED_ACTIONS,
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    NOT_WEAKLY,
    PROGRAM_OUT,
    PROGRAM_REQUESTS,
    TOOL_ACTIONS,
    WEAKLY_FINE,
    OpenKind,
    PolicyTables,
    Risks,
)
from domo_core.policy.decision import decide_now
from domo_core.policy.facts import facts_of
from domo_core.policy.models import PolicySetting
from domo_core.policy.rights import NO_RIGHTS, Right, RightsTable
from domo_core.policy.roles import ROLES, Actor, Role, Standing
from domo_core.policy.settings import VOICE_UNSURE_KEY, InvalidSetting, validate_setting
from domo_core.tools.context import ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from fakes import FakeRefused, FakeSandbox
from sqlalchemy import select
from test_madetools_handover import BEGIN as HANDOVER_BEGIN
from test_madetools_handover import add_tool as add_handover_tool
from test_madetools_handover import by_program, deps_of
from test_madetools_in_turn import add_tool, answers, done, launch_deps
from test_madetools_out import GET as OUT_GET
from test_madetools_out import FakeOut, account, going_out, opened, sending
from test_madetools_yes import policy_of
from test_policy_barrier import described

LIVE = datetime.now(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


LIGHTS = "made.lights"
# Acting in the house, declared harmless by him (a light): the owner's O1/O3.
HARMLESS = Risks(physical=True, harmless=True)
INVITE_TOM = {"display_name": "Tom", "role": "guest", "guest_days": 1}
# The clue of a voice that fits the page's person badly: Alex with a cold.
COLD = Clue("voice", "against", "weak", "code", LIVE, degree=0.45)


def _kind(**risks: bool) -> OpenKind:
    return OpenKind(
        action_type=LIGHTS,
        costs=risks.get("costs", False),
        irreversible=risks.get("irreversible", False),
        physical=risks.get("physical", False),
        outward=risks.get("outward", False),
        facts=MappingProxyType({}),
        default_roles=frozenset({"owner", "adult", "child"}),
        description="Turns the lights on.",
    )


def _with_lights() -> PolicyTables:
    allowed = {
        role: actions | ({LIGHTS} if role != "guest" else frozenset())
        for role, actions in ALLOWED_ACTIONS.items()
    }
    return replace(
        DEFAULT_POLICY,
        known=KNOWN_ACTIONS | {LIGHTS},
        allowed=allowed,
        kinds=MappingProxyType({LIGHTS: _kind()}),
    )


def _described(action_type: str, effect: str = "read") -> DescribedAction:
    """An action of ``action_type`` with plain facts (``test_policy_barrier.described``)."""
    return described(action_type, effect=effect)


def _decide(
    actor: Actor,
    action: DescribedAction,
    policy: PolicyTables = DEFAULT_POLICY,
    rights: RightsTable = NO_RIGHTS,
    **kwargs: Any,
) -> barrier.Decision:
    facts = facts_of(actor, action, name="Someone", local_now=None)
    return barrier.decide(actor, action, policy, rights, facts, **kwargs)


def _right(**fields: Any) -> Right:
    said: dict[str, Any] = {"person_id": None, "role": None, "access": None, "approver": None}
    said |= {"limit": None, "ask": None, "ask_when": None}
    said |= fields
    return Right(
        uuid.uuid4(),
        said["person_id"],
        said["role"],
        said["action_type"],
        said["access"],
        said["approver"],
        said["limit"],
        said["ask"],
        said["ask_when"],
        "a right",
    )


INVITE = _described("household.invite", "permissions")
RENAME = _described("profile.rename", "self_profile")


# --- the barrier ----------------------------------------------------------------------------------


def test_unsure_and_ordinary_is_done_with_the_probable_persons_rights() -> None:
    # Paul at Alex's page, his voice clearly his: unsure, acting as Paul. A launch whose
    # operations only act on the house, harmlessly as declared, is ordinary: done, as for anyone.
    paul = Actor(uuid.uuid4(), "child", standing="unsure")
    lights = _described(LIGHTS)
    assert _decide(paul, lights, _with_lights(), risks=HARMLESS) == Allow()
    # Alex's rights are not his: what Alex may do and a child may not is refused to Paul.
    alex = Actor(uuid.uuid4(), "owner")
    assert isinstance(_decide(alex, INVITE), Allow)
    assert _decide(paul, INVITE) == Refuse("not_allowed")


def test_unknown_may_not_act() -> None:
    for role in ROLES:
        nobody = Actor(uuid.uuid4(), role, standing="unknown")
        for action in (RENAME, INVITE, _described("household.list")):
            assert _decide(nobody, action) == Refuse("untrusted_identity")
        assert _decide(nobody, _described(LIGHTS), _with_lights(), risks=Risks()) == Refuse(
            "untrusted_identity"
        )


def test_a_missing_right_refuses_before_any_judgement() -> None:
    paul = Actor(uuid.uuid4(), "child", standing="unsure")
    assert _decide(paul, INVITE) == Refuse("not_allowed")
    denied = RightsTable(
        (_right(person_id=paul.person_id, action_type="profile.rename", access="deny"),)
    )
    assert _decide(paul, RENAME, rights=denied) == Refuse("not_allowed")


def test_an_owners_ask_me_first_still_asks_beside_the_judge() -> None:
    adult = Actor(uuid.uuid4(), "adult", standing="unsure")
    rights = RightsTable((_right(role="adult", action_type="household.invite", ask="proof"),))
    assert _decide(adult, INVITE, rights=rights) == Allow("proof", judge_identity=True)
    # An approver the owner names is still his to give, and the judge weighs who asks too.
    rights = RightsTable((_right(role="adult", action_type="household.invite", approver="owner"),))
    decided = _decide(adult, INVITE, rights=rights)
    assert isinstance(decided, NeedsApproval) and decided.approver == "owner"
    assert decided.judge_identity


def test_the_owner_may_have_ordinary_things_judged_too() -> None:
    alex = Actor(uuid.uuid4(), "owner", standing="unsure")
    assert _decide(alex, RENAME) == Allow()
    assert _decide(alex, RENAME, ordinary_when_unsure="judge") == Allow(judge_identity=True)
    # Someone the code is sure of is never judged on who they are.
    sure = replace(alex, standing="sure")
    assert _decide(sure, RENAME, ordinary_when_unsure="judge") == Allow()
    validate_setting(VOICE_UNSURE_KEY, "judge")
    validate_setting(VOICE_UNSURE_KEY, "do")
    with pytest.raises(InvalidSetting):
        validate_setting(VOICE_UNSURE_KEY, "refuse")


async def test_the_owners_setting_is_read_when_unsure(session_factory: Any) -> None:
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        writer.add(alex)
        await writer.flush()
        writer.add(PolicySetting(key=VOICE_UNSURE_KEY, value="judge", updated_by=alex.id))
        await writer.commit()
    async with session_factory() as session:
        unsure = Actor(alex.id, "owner", standing="unsure")
        judged = await decide_now(session, unsure, RENAME, DEFAULT_POLICY, LIVE)
        assert judged == Allow(judge_identity=True)
        sure = await decide_now(
            session, replace(unsure, standing="sure"), RENAME, DEFAULT_POLICY, LIVE
        )
        assert sure == Allow()


def test_unreadable_risks_are_sensitive() -> None:
    policy = _with_lights()
    alex = Actor(uuid.uuid4(), "owner", standing="unsure")
    lights = _described(LIGHTS)
    assert _decide(alex, lights, policy, risks=None) == Allow(judge_identity=True)
    # The owner (O1/O3): physical with a risk is judged; declared harmless, it passes.
    assert _decide(alex, lights, policy, risks=Risks(physical=True)) == Allow(judge_identity=True)
    assert _decide(alex, lights, policy, risks=HARMLESS) == Allow()
    assert not may_act_weakly(LIGHTS, policy, risks=None)
    assert may_act_weakly(LIGHTS, policy, risks=Risks())
    assert may_act_weakly(LIGHTS, policy, risks=HARMLESS)
    assert not may_act_weakly(LIGHTS, policy, risks=Risks(physical=True))
    for risky in (
        Risks(costs=True),
        Risks(outward=True),
        Risks(irreversible=True),
        Risks(physical=True),
    ):
        assert not may_act_weakly(LIGHTS, policy, risks=risky)
        assert not may_act_weakly(LIGHTS, policy, risks=risky | HARMLESS)
        assert not may_act_weakly(LIGHTS, policy, risks=HARMLESS | risky)
        assert not may_act_weakly(PROGRAM_OUT, policy, risks=risky)
    assert may_act_weakly(LIGHTS, policy, risks=HARMLESS | Risks() | HARMLESS)
    assert may_act_weakly(PROGRAM_OUT, policy, risks=Risks())
    assert not may_act_weakly(PROGRAM_OUT, policy, risks=None)
    # A launch of a kind the catalog does not know, and a type nobody classified: sensitive.
    assert not may_act_weakly("made.unknown", policy, risks=Risks())
    assert not may_act_weakly("brand.new", policy)


def test_what_is_ordinary_is_exactly_the_list() -> None:
    assert KNOWN_ACTIONS - NOT_WEAKLY - PROGRAM_REQUESTS == WEAKLY_FINE
    for action_type in KNOWN_ACTIONS:
        sets = [action_type in NOT_WEAKLY, action_type in WEAKLY_FINE]
        sets.append(action_type in PROGRAM_REQUESTS)
        assert sets.count(True) == 1, action_type
    assert may_act_weakly("voice.stop_listening", DEFAULT_POLICY)
    for sensitive in ("proof.phrase_agree", "proof.phrase_change", "proof.phrase_remove"):
        assert not may_act_weakly(sensitive, DEFAULT_POLICY)
    assert not may_act_weakly("voice.unlearn", DEFAULT_POLICY)
    for plan_type in ("plan.set", "plan.change", "plan.cancel"):
        assert not may_act_weakly(plan_type, DEFAULT_POLICY)
    for tool_type in TOOL_ACTIONS | {"program.run"}:
        assert not may_act_weakly(tool_type, DEFAULT_POLICY, risks=Risks())


@pytest.mark.parametrize("action_type", sorted(KNOWN_ACTIONS))
@pytest.mark.parametrize("role", ROLES)
def test_sure_is_decided_exactly_as_before(action_type: str, role: Role) -> None:
    sure = Actor(uuid.uuid4(), role)
    action = _described(action_type)
    decided = _decide(sure, action)
    assert decided == _decide(sure, action, ordinary_when_unsure="judge", risks=None)
    if not isinstance(decided, Refuse):
        assert not decided.judge_identity
    # Unsure: the same decision, a refusal unchanged, the rest marked only when sensitive.
    unsure = _decide(replace(sure, standing="unsure"), action)
    if isinstance(decided, Refuse):
        assert unsure == decided
    else:
        assert unsure == replace(decided, judge_identity=action_type not in WEAKLY_FINE)


def test_stricter_keeps_a_doubt_on_who_asks() -> None:
    judged = barrier.stricter(Allow(judge_identity=True), Allow())
    assert judged == Allow(judge_identity=True)
    asked = barrier.stricter(Allow(), NeedsApproval("owner", judge_identity=True))
    assert isinstance(asked, NeedsApproval) and asked.judge_identity


# --- a short sound (ruling T22) -------------------------------------------------------------------

SIGNED_IN = LIVE - timedelta(hours=1)
PRINT = Print(
    vector=(0.1,) * 4, model="m", seconds=0.4, pitch_hz=None, pitch_range_hz=None, rate=None
)
PROOF = Clue("proof", "for", "strong", "code", LIVE, means="password")


def _heard(alex: Person, *, short: bool, proofs: tuple[Clue, ...] = ()) -> Speaker:
    reading = Reading(
        weighs="unknown" if short else "for",
        degree=0.2 if short else 0.9,
        learnt=True,
        best_other=None,
        other_degree=0.0,
        intonation_fits=None,
        short=short,
    )
    return voice_speaker(
        page_person=alex,
        reading=reading,
        print_=PRINT,
        microphone="mic",
        signed_in_at=SIGNED_IN,
        proofs=proofs,
        now=LIVE,
    )


def test_a_short_yes_alone_is_unsure_for_what_is_sensitive_only() -> None:
    alex = Person(id=uuid.uuid4(), display_name="Alex", role="owner")
    for proofs in ((), (PROOF,)):
        alone = actor_of(_heard(alex, short=True, proofs=proofs), alex)
        assert _decide(alone, INVITE) == Allow(judge_identity=True)
        # An ordinary ask is served as by the page, even when the owner has ordinary things
        # judged: the page is sure of who it is.
        assert _decide(alone, RENAME) == Allow()
        assert _decide(alone, RENAME, ordinary_when_unsure="judge") == Allow()
    # Right after the person's own long sentence, joined to it: not short, sure.
    joined = actor_of(_heard(alex, short=False), alex)
    assert _decide(joined, INVITE) == Allow()
    assert _decide(joined, RENAME) == Allow()


# --- the judge -----------------------------------------------------------------------------------


async def _household(session_factory: Any) -> tuple[Person, Person, Conversation, uuid.UUID]:
    """Alex, the owner, and Paul, a child, with Alex's conversation and a reply in it."""
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        paul = Person(display_name="Paul", role="child")
        writer.add_all([alex, paul])
        await writer.flush()
        conversation = Conversation(person_id=alex.id)
        writer.add(conversation)
        await writer.flush()
        writer.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content="invite Tom",
                status="complete",
                provenance="person",
            )
        )
        reply = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="complete",
            provenance="model",
        )
        writer.add(reply)
        await writer.flush()
        reply_id = reply.id
        await writer.commit()
    return alex, paul, conversation, reply_id


def _unsure_ctx(session_factory: Any, person: Person, conversation: Conversation) -> ToolContext:
    session = Clue("session", "for", "strong", "code", SIGNED_IN)
    speaker = Speaker(person.id, "voice", clues=(session, COLD), microphone="mic")
    return ToolContext(
        session_factory=session_factory,
        actor=actor_of(speaker, person),
        person=person,
        conversation_id=conversation.id,
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: LIVE,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
        speaker=speaker,
    )


class Judged:
    def __init__(self, answer: Judgement) -> None:
        self.cases: list[JudgeCase] = []
        self._answer = answer

    async def __call__(self, model: object, case: JudgeCase, **_: object) -> Judgement:
        self.cases.append(case)
        return self._answer


def _judge(monkeypatch: pytest.MonkeyPatch, answer: Judgement) -> Judged:
    judged = Judged(answer)
    monkeypatch.setattr(execution_module, "judge", judged)
    return judged


def _invite() -> ToolCall:
    return ToolCall("i", "household_invite", json.dumps(INVITE_TOM))


async def _asked(session_factory: Any) -> list[tuple[str | None, str | None]]:
    async with session_factory() as reader:
        rows = await reader.execute(select(Confirmation.ask, Confirmation.asked_by))
        return [(ask, by) for ask, by in rows.all()]


async def test_unsure_and_sensitive_goes_to_the_judge_with_the_clues(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, Judgement("do"))
    ctx = _unsure_ctx(session_factory, alex, conversation)
    record = await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    [case] = judged.cases
    assert case.speaker is not None
    voice = [clue for clue in case.speaker["clues"] if clue["kind"] == "voice"]  # type: ignore[union-attr]
    assert voice == [
        {
            "kind": "voice",
            "weighs": "against",
            "strength": "weak",
            "source": "code",
            "minutes_ago": 0,
            "degree": 0.45,
        }
    ]


async def test_a_silent_judge_asks_a_proof_never_does(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    for silent in (fallback("error"), fallback("no_model"), fallback("error", they_asked=False)):
        _judge(monkeypatch, silent)
        ctx = _unsure_ctx(session_factory, alex, conversation)
        record = await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
        assert record.status == "waiting"
    assert await _asked(session_factory) == [("proof", "judge")] * 3


async def test_a_missing_right_is_refused_with_no_judge(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, paul, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, Judgement("do"))
    ctx = _unsure_ctx(session_factory, paul, conversation)
    record = await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
    assert (record.status, record.model_result["reason"]) == ("refused", "not_allowed")
    assert judged.cases == []


async def test_a_proof_the_rights_ask_is_not_lowered_by_a_do(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, grant: Any
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    await grant("household.invite", person=alex, ask="proof")
    judged = _judge(monkeypatch, Judgement("do"))
    ctx = _unsure_ctx(session_factory, alex, conversation)
    record = await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert len(judged.cases) == 1
    assert await _asked(session_factory) == [("proof", "rule")]


async def test_an_owners_ask_me_first_still_asks(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, grant: Any
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    await grant("household.invite", person=alex, ask="yes")
    _judge(monkeypatch, Judgement("do"))
    ctx = _unsure_ctx(session_factory, alex, conversation)
    record = await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert await _asked(session_factory) == [("yes", "rule")]


# --- the paths around the barrier (fix round 1) ---------------------------------------------------


def _chain_of_invites() -> ToolCall:
    steps = [
        {"tool": "household_invite", "arguments": INVITE_TOM},
        {"tool": "household_invite", "arguments": {**INVITE_TOM, "display_name": "Gil"}},
    ]
    return ToolCall("c", "do_in_order", json.dumps({"steps": steps}))


async def test_an_unsure_speakers_chain_with_a_silent_judge_asks_a_proof(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, fallback("error"))
    ctx = _unsure_ctx(session_factory, alex, conversation)
    record = await handle_tool_call(_chain_of_invites(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert len(judged.cases) == 1
    assert await _asked(session_factory) == [("proof", "judge")]


async def test_a_chain_carries_the_doubt_of_its_steps_and_of_itself(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, _ = await _household(session_factory)
    ctx = _unsure_ctx(session_factory, alex, conversation)
    tool = TOOLS["do_in_order"]
    arguments = json.loads(_chain_of_invites().arguments)
    plan = await tool.plan(arguments, ctx.actor, ctx)  # type: ignore[attr-defined]
    assert not isinstance(plan.decision, Refuse) and plan.decision.judge_identity
    sure = replace(ctx, actor=replace(ctx.actor, standing="sure"))
    plan = await tool.plan(arguments, sure.actor, sure)  # type: ignore[attr-defined]
    assert not isinstance(plan.decision, Refuse) and not plan.decision.judge_identity


def test_a_plan_is_sensitive() -> None:
    # A plan made by an unsure voice runs later as the person, sure: it is judged when set.
    assert "plan.set" in NOT_WEAKLY and "plan.set" not in WEAKLY_FINE
    alex = Actor(uuid.uuid4(), "owner", standing="unsure")
    assert _decide(alex, _described("plan.set")) == Allow(judge_identity=True)


class _PlanLike:
    """A test-only tool of the type ``plan.set`` (silent: never judged by its type alone)."""

    definition = ToolDefinition(
        name="plan_like",
        description="A plan.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "plan.set"

    async def describe(self, arguments: Any, actor: Actor, ctx: Any) -> DescribedAction:
        return DescribedAction("core", self.action_type, None, "self_profile", "household")

    async def execute(self, arguments: Any, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome("done", {}, {"key": "action.card.done", "params": {}})


class _HisOwn(_PlanLike):
    """A test-only tool whose every call is the assistant's own act (``ActsAsDomo``)."""

    definition = ToolDefinition(
        name="his_own",
        description="His own act.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "net.close"
    acts_as_domo = True


async def test_an_unsure_speakers_plan_goes_to_the_judge(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, Judgement("do"))
    ctx = replace(
        _unsure_ctx(session_factory, alex, conversation),
        registry={**TOOLS, "plan_like": _PlanLike()},
    )
    record = await handle_tool_call(ToolCall("p", "plan_like", "{}"), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    assert len(judged.cases) == 1


async def test_a_tool_acting_as_the_assistant_keeps_the_doubt_on_who_asks(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, fallback("error"))
    ctx = replace(
        _unsure_ctx(session_factory, alex, conversation),
        registry={**TOOLS, "his_own": _HisOwn()},
    )
    record = await handle_tool_call(ToolCall("h", "his_own", "{}"), ctx=ctx, reply_id=reply_id)
    # Decided as the assistant, but asked by someone the code is not sure of: judged, and a
    # silent judge asks a proof.
    assert record.status == "waiting"
    assert len(judged.cases) == 1
    assert await _asked(session_factory) == [("proof", "judge")]
    # The same, asked by someone the code is sure of: decided as the assistant alone, not judged.
    judged.cases.clear()
    sure = replace(ctx, actor=replace(ctx.actor, standing="sure"))
    record = await handle_tool_call(ToolCall("h2", "his_own", "{}"), ctx=sure, reply_id=reply_id)
    assert record.status == "done"
    assert judged.cases == []


async def test_an_unsure_persons_side_reaches_the_stricter_decision_of_an_idea(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    _judge(monkeypatch, Judgement("do"))
    seen: list[Any] = []
    stricter = barrier.stricter  # what the execution calls, kept before it is replaced

    def recording(person: Any, domo: Any) -> Any:
        seen.append((person, stricter(person, domo)))
        return seen[-1][1]

    monkeypatch.setattr(execution_module, "stricter", recording)
    ctx = _unsure_ctx(session_factory, alex, conversation)
    mark = ToolCall("m", "act_on_my_own", json.dumps({"reason": "Tom comes tonight"}))
    await handle_tool_call(mark, ctx=ctx, reply_id=reply_id)
    await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
    assert seen
    person, decided = seen[0]
    assert person.judge_identity and decided.judge_identity


def test_a_sound_with_no_usable_print_is_too_short_to_tell() -> None:
    alex = Person(id=uuid.uuid4(), display_name="Alex", role="owner")
    unusable = replace(NOT_LEARNED, usable=False)
    speaker = voice_speaker(
        page_person=alex,
        reading=unusable,
        print_=PRINT,
        microphone="mic",
        signed_in_at=SIGNED_IN,
        proofs=(),
        now=LIVE,
    )
    assert speaker.voice_too_short
    assert _decide(actor_of(speaker, alex), INVITE) == Allow(judge_identity=True)


async def test_a_print_that_cannot_be_read_says_so(session_factory: Any) -> None:
    zeros = replace(PRINT, vector=(0.0,) * 4)
    async with session_factory() as session:
        reading = await read_voice(
            session,
            person_id=None,
            print_=zeros,
            microphone="mic",
            thresholds=None,
            recent=(),
            now=LIVE,
        )
    assert not reading.usable


# --- a launch, decided where it is asked (ruling T22, round 2) -----------------------------------

LIGHT_ON: dict[str, Any] = {
    "on": {"says": "Turns the light on.", "risks": ["physical"], "harmless": True}
}
WITH_MAIL: dict[str, Any] = {
    "on": {"says": "Turns the light on.", "risks": ["physical"], "harmless": True},
    "mail": {
        "says": "Writes to the house mail.",
        "risks": ["physical"],
        "harmless": True,
        "accounts": ["mail"],
    },
}
# Physical, not declared harmless: a lock (or any tool made before the declaration existed).
DOOR: dict[str, Any] = {"open": {"says": "Unlocks the front door.", "risks": ["physical"]}}
LIGHT_AND_DOOR: dict[str, Any] = {**LIGHT_ON, **DOOR}


async def _lights(session_factory: Any, operations: dict[str, Any], *, alone: bool) -> MadeTool:
    """A ready tool ``lights`` with ``operations``; ``alone``: its bounds keep it from launching
    another tool (``chain_depth`` 1)."""
    tool_id = await add_tool(session_factory, "lights", parameters={"type": "object"})
    async with session_factory() as writer:
        tool = await writer.get(MadeTool, tool_id)
        assert tool is not None
        tool.operations = operations
        if alone:
            await set_for_tool(writer, tool, {"chain_depth": 1})
        await writer.commit()
    async with session_factory() as reader:
        found = await reader.get(MadeTool, tool_id)
        assert found is not None
        return found


async def _launched_in_a_turn(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tool: MadeTool,
    *,
    standing: Standing,
    judge_says: Judgement,
) -> tuple[Any, Judged, FakeSandbox]:
    alex, _, conversation, reply_id = await _household(session_factory)
    judged = _judge(monkeypatch, judge_says)
    sandbox = FakeSandbox(answers(done({})))
    deps = launch_deps(session_factory, _now, sandbox)
    offer = MadeToolOffer(tool, deps)
    base = _unsure_ctx(session_factory, alex, conversation)
    ctx = replace(
        base,
        actor=replace(base.actor, standing=standing),
        registry={**TOOLS, offered_name(tool.name): offer},
        policy=await policy_of(session_factory),
    )
    call = ToolCall("l", offered_name(tool.name), "{}")
    record = await handle_tool_call(call, ctx=ctx, reply_id=reply_id)
    return record, judged, sandbox


@pytest.mark.parametrize("alone", [True, False])
async def test_an_unsure_speaker_launching_a_physical_tool_with_no_account_is_served(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, alone: bool
) -> None:
    # With the default bounds too (it may launch another tool: that one is decided on its own).
    tool = await _lights(session_factory, LIGHT_ON, alone=alone)
    record, judged, sandbox = await _launched_in_a_turn(
        session_factory, monkeypatch, tool, standing="unsure", judge_says=fallback("error")
    )
    assert record.status == "done"
    assert judged.cases == []
    assert len(sandbox.runs) == 1


@pytest.mark.parametrize("operations", [DOOR, LIGHT_AND_DOOR], ids=["door", "light_and_door"])
async def test_an_unsure_speaker_launching_a_physical_act_with_a_risk_is_judged(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, operations: Any
) -> None:
    # The owner (O1/O3): "physical with a risk is judged, like unlocking the door". A tool with a
    # light and a door is judged even for the light (the union of its operations, O5).
    tool = await _lights(session_factory, operations, alone=True)
    record, judged, sandbox = await _launched_in_a_turn(
        session_factory, monkeypatch, tool, standing="unsure", judge_says=Judgement("do")
    )
    assert record.status == "done", record.model_result
    [case] = judged.cases
    assert len(sandbox.runs) == 1
    # The judge is told which of its acts in the house are not declared harmless, in his words.
    told = json.dumps(case.as_json())
    assert "Unlocks the front door." in told
    assert "Turns the light on." not in told


async def test_a_silent_judge_on_a_risky_physical_act_asks_a_proof(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = await _lights(session_factory, DOOR, alone=True)
    record, judged, sandbox = await _launched_in_a_turn(
        session_factory, monkeypatch, tool, standing="unsure", judge_says=fallback("error")
    )
    assert record.status == "waiting"
    assert len(judged.cases) == 1
    assert await _asked(session_factory) == [("proof", "judge")]
    assert sandbox.runs == []


async def test_a_sure_speaker_opens_the_door_as_before(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = await _lights(session_factory, DOOR, alone=True)
    record, judged, sandbox = await _launched_in_a_turn(
        session_factory, monkeypatch, tool, standing="sure", judge_says=fallback("error")
    )
    assert record.status == "done"
    assert judged.cases == []
    assert len(sandbox.runs) == 1


async def test_a_tool_that_can_go_further_goes_to_the_judge(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An account that may write: not ordinary at the launch.
    tool = await _lights(session_factory, WITH_MAIL, alone=False)
    record, judged, sandbox = await _launched_in_a_turn(
        session_factory, monkeypatch, tool, standing="unsure", judge_says=fallback("error")
    )
    assert record.status == "waiting"
    assert len(judged.cases) == 1
    assert await _asked(session_factory) == [("proof", "judge")]
    assert sandbox.runs == []


@pytest.mark.parametrize(
    "operations, alone", [(LIGHT_ON, True), (WITH_MAIL, True), (LIGHT_ON, False)]
)
async def test_a_sure_speakers_launch_is_unchanged(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, operations: Any, alone: bool
) -> None:
    tool = await _lights(session_factory, operations, alone=alone)
    record, judged, sandbox = await _launched_in_a_turn(
        session_factory, monkeypatch, tool, standing="sure", judge_says=fallback("error")
    )
    assert record.status == "done"
    assert judged.cases == []
    assert len(sandbox.runs) == 1


def _asks_each(*names: str) -> Any:
    """A program that asks each tool of ``names`` in turn: what each answered, or its refusal."""

    async def program(request: Any, ask: Any) -> Any:
        said: list[Any] = []
        await ask(HANDOVER_BEGIN)
        for name in names:
            try:
                said.append(await ask({"do": "tool", "name": name, "arguments": {}}))
            except FakeRefused as refused:
                said.append(refused.reason)
        return said

    return program


async def _run_for(session_factory: Any, alex: Person, program: Any, standing: Standing) -> Any:
    """``program`` run as the tool ``outer``, launched in a turn for ``alex``, the code as sure
    of who asked it as ``standing`` says."""
    outer = await add_handover_tool(session_factory, "outer")
    sandbox = by_program(outer=program)
    deps = deps_of(session_factory, _now, sandbox, [])
    came = await launch(
        deps, outer.id, arguments={}, for_whom=ForWhom(alex, standing), cause="turn"
    )
    assert came.status == "done"
    return came.result


@pytest.mark.parametrize("standing", ["unsure", "sure"])
async def test_a_nested_launch_keeps_the_doubt(session_factory: Any, standing: Standing) -> None:
    alex, _, _, _ = await _household(session_factory)
    await add_handover_tool(session_factory, "bulb", LIGHT_ON)
    await add_handover_tool(session_factory, "mailer", WITH_MAIL)
    await add_handover_tool(session_factory, "door", DOOR)
    said = await _run_for(session_factory, alex, _asks_each("bulb", "mailer", "door"), standing)
    if standing == "unsure":
        # The harmless one is served; the one that may write, and the door, need someone's word.
        assert said == [None, "needs_agreement", "needs_agreement"]
    else:
        assert said == [None, None, None]


@pytest.mark.parametrize("standing", ["unsure", "sure"])
async def test_a_doubted_run_sets_no_launch_for_later(
    session_factory: Any, standing: Standing
) -> None:
    alex, _, _, _ = await _household(session_factory)
    at = (_now() + timedelta(hours=3)).replace(microsecond=0).isoformat()

    async def program(request: Any, ask: Any) -> Any:
        said: list[Any] = []
        await ask(HANDOVER_BEGIN)
        for asked in (
            {
                "do": "act",
                "action": "tool.launch_when",
                "arguments": {"tool": "outer", "when": at, "arguments": {}},
            },
            {"do": "later", "at": at, "arguments": {}},
        ):
            try:
                said.append(await ask(asked))
            except FakeRefused as refused:
                said.append(refused.reason)
        return said

    said = await _run_for(session_factory, alex, program, standing)
    async with session_factory() as reader:
        triggers = (await reader.scalars(select(MadeToolTrigger))).all()
    if standing == "unsure":
        assert said == ["needs_agreement", "needs_agreement"]
        assert triggers == []
    else:
        assert "needs_agreement" not in said
        assert len(triggers) == 2


async def test_what_a_launch_may_reach_is_read_from_the_tool(session_factory: Any) -> None:
    alone = await _lights(session_factory, LIGHT_ON, alone=True)
    async with session_factory() as session:
        assert await reach_of(session, alone) == HARMLESS
        alone.operations = DOOR
        assert await reach_of(session, alone) == Risks(physical=True)
        alone.operations = LIGHT_AND_DOOR
        # One risky act makes the whole launch risky.
        assert await reach_of(session, alone) == Risks(physical=True)
        alone.operations = WITH_MAIL
        assert await reach_of(session, alone) is None
        alone.operations = {}
        assert await reach_of(session, alone) is None
        house: dict[str, Any] = {
            "on": {
                "says": "x",
                "risks": ["physical"],
                "addresses": [{"host": "192.168.1.9", "port": 80}],
            }
        }
        alone.operations = house
        assert await reach_of(session, alone) == Risks(physical=True)
        named: dict[str, Any] = {
            "on": {
                "says": "x",
                "risks": ["physical"],
                "addresses": [{"host": "lamp.example.com", "port": 80}],
            }
        }
        alone.operations = named
        assert await reach_of(session, alone) is None


@pytest.mark.parametrize("standing", ["unsure", "sure"])
async def test_an_account_the_operation_does_not_declare_is_not_used_for_a_doubted_run(
    session_factory: Any, standing: Standing
) -> None:
    # `lights` declares `physical` alone and no account (ordinary at the launch); the hub's house
    # address is open and a house account exists. Its program writes through that account.
    alex, _, _, _ = await _household(session_factory)
    on = {"on": {"says": "Turns the light on.", "risks": ["physical"], "harmless": True}}
    tool = await add_handover_tool(session_factory, "lights", on)
    await opened(session_factory, "lights")
    hub = await account(session_factory, "hub", None, alex)
    gatekeeper = FakeOut()
    writes = {**OUT_GET, "method": "POST", "account": "hub"}
    deps = going_out(session_factory, _now, {"lights": sending(writes, operation="on")}, gatekeeper)
    came = await launch(deps, tool.id, arguments={}, for_whom=ForWhom(alex, standing), cause="turn")
    assert came.status == "done"
    if standing == "unsure":
        assert came.result == [{"refused": "needs_agreement"}]
        assert gatekeeper.asked == []
    else:
        assert came.result[0]["status"] == 200  # type: ignore[index]
        assert [request.account for request in gatekeeper.sent] == [hub.ref]


def test_a_voice_to_learn_again_is_unsure_for_what_is_sensitive_only() -> None:
    # Task 27, ruling R: his erasure of a known voice leaves it "to be learnt again"; until it
    # is, a voice taken for that person at their page is not sure for what is sensitive.
    alex = Person(id=uuid.uuid4(), display_name="Alex", role="owner")
    marked = replace(NOT_LEARNED, to_relearn=True)
    speaker = voice_speaker(
        page_person=alex,
        reading=marked,
        print_=PRINT,
        microphone="mic",
        signed_in_at=SIGNED_IN,
        proofs=(),
        now=LIVE,
    )
    assert speaker.person_id == alex.id and speaker.voice_too_short
    assert _decide(actor_of(speaker, alex), INVITE) == Allow(judge_identity=True)
    assert _decide(actor_of(speaker, alex), RENAME) == Allow()


# --- someone at another person's page, the judge silent (final fix wave, case 7) ----------------


async def test_at_someone_elses_page_a_silent_judge_lets_the_ordinary_be_done(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alex, _, conversation, reply_id = await _household(session_factory)
    async with session_factory() as writer:
        dana = Person(display_name="Dana", role="adult")
        writer.add(dana)
        await writer.commit()
    _judge(monkeypatch, fallback("error"))
    ctx = _unsure_ctx(session_factory, alex, conversation)
    # Alex heard at Dana's page: still judged, but the judge's silence asks no proof for a rename.
    ctx.actor = replace(ctx.actor, page_person_id=dana.id)
    rename = ToolCall("r", "profile_rename", json.dumps({"name": "Alex B"}))
    assert may_act_weakly("profile.rename", DEFAULT_POLICY)
    record = await handle_tool_call(rename, ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    # Something sensitive still asks a proof when the judge is silent.
    record = await handle_tool_call(_invite(), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert await _asked(session_factory) == [("proof", "judge")]


async def test_at_someone_elses_page_the_owners_judge_ordinary_rule_still_asks_a_proof(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Final fix wave B, I-5: the owner's "judge ordinary things too" is his rule: with the judge
    # silent, a proof is asked at another person's page as at the person's own.
    alex, _, conversation, reply_id = await _household(session_factory)
    async with session_factory() as writer:
        dana = Person(display_name="Dana", role="adult")
        writer.add(dana)
        writer.add(PolicySetting(key=VOICE_UNSURE_KEY, value="judge", updated_by=alex.id))
        await writer.commit()
    _judge(monkeypatch, fallback("error"))
    ctx = _unsure_ctx(session_factory, alex, conversation)
    ctx.actor = replace(ctx.actor, page_person_id=dana.id)
    rename = ToolCall("r", "profile_rename", json.dumps({"name": "Alex B"}))
    record = await handle_tool_call(rename, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting"
    assert await _asked(session_factory) == [("proof", "judge")]


# --- harmless declared by him, checked at the review (the owner, O1/O3, 2026-11-27) --------------


def test_harmless_is_read_only_on_an_operation_that_acts_in_the_house_alone() -> None:
    from domo_core.madetools.risks import OperationsNotRight, kept_operations, read_operations

    read = read_operations(LIGHT_AND_DOOR)
    assert read["on"].risks == HARMLESS
    # Undeclared (every tool made before): risky.
    assert read["open"].risks == Risks(physical=True)
    assert kept_operations(read) == LIGHT_AND_DOOR
    assert read_operations(
        {"on": {"says": "x", "risks": ["physical"], "harmless": False}}
    ) == read_operations({"on": {"says": "x", "risks": ["physical"]}})
    for wrong in (
        {"on": {"says": "x", "risks": ["physical", "costs"], "harmless": True}},
        {"on": {"says": "x", "risks": ["physical", "outward"], "harmless": True}},
        {"on": {"says": "x", "risks": [], "harmless": True}},
        {"on": {"says": "x", "risks": ["physical"], "harmless": "yes"}},
        {"on": {"says": "x", "risks": ["physical"], "harmless": 1}},
    ):
        with pytest.raises(OperationsNotRight):
            read_operations(wrong)
    with pytest.raises(ValueError):
        Risks(harmless=True)


def test_the_writer_and_the_reviewer_are_told_what_harmless_means() -> None:
    from domo_core.madetools.build import toolmaking_prompt
    from domo_core.madetools.checks import review_prompt

    writing = toolmaking_prompt()
    assert '"harmless": true' in writing
    for never in ("lock", "alarm", "garage", "heating", "oven", "valve"):
        assert never in writing.split('"harmless": true', 1)[1], never
    review = review_prompt()
    assert "harmless" in review
    for never in ("lock", "alarm", "garage", "heating", "oven", "valve"):
        assert never in review.split("harmless", 1)[1], never
