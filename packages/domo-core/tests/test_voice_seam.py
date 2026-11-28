"""Where the voice will plug in (spec 6.12): what this branch gives a channel that knows people by
something of its own, and nothing that listens.

A speaker may be a record with no account: the actor carries the record's marks, and is trusted
only when the code checked who speaks. A discussion, a turn and what it keeps are a person's, by
their id alone. ``people.reach.can_answer`` is the one switch the voice widens. What an unsure
speaker may do is a list derived from the catalog (``policy.barrier.may_act_weakly``), which the
barrier reads since the voice (its rule 6: judged, never refused out of hand). A channel claims
an opaque reference for a person (``people.hooks``), finds again what exists after a missed
notice, and is told of merges, splits, erasures and forgettings after the commit."""

import ast
import inspect
import json
import logging
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conversation_support import said
from domo_core.accounts.actors import actor_of as accounts_actor_of
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.turns import turn_deps_of
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.conversation.channel import Abilities
from domo_core.conversation.execution import handle_tool_call
from domo_core.conversation.prepare import _speaking
from domo_core.conversation.speaker import Clue, Speaker, actor_of, established
from domo_core.identity.clues import keep_proof_clue
from domo_core.llm.types import ToolCall
from domo_core.madetools.asks import ActAsk, OutAsk
from domo_core.madetools.risks import minimum_risks
from domo_core.memory.models import Memory
from domo_core.people import forget, hooks, made_roles, merge, reach
from domo_core.people.directives import set_directive
from domo_core.people.hooks import (
    NoSuchRecord,
    RecordEvent,
    RecordListeners,
    ReferenceTaken,
    claim_reference,
    person_of_ref,
    refs_of,
)
from domo_core.people.models import ChannelRef
from domo_core.people.news import news_for
from domo_core.plans import service as plans
from domo_core.policy import barrier, decision
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, Refuse, may_act_weakly
from domo_core.policy.catalog import (
    AGREEMENT_ACTIONS,
    DEFAULT_POLICY,
    DELIVERY_ACTIONS,
    HOUSE_SET,
    KNOWN_ACTIONS,
    NET_OPEN_HOST,
    NOT_WEAKLY,
    OUTWARD_ACTIONS,
    OWNER_DIRECTIVE_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PEOPLE_GESTURES,
    PEOPLE_READ_ACTIONS,
    PERMISSION_TYPES,
    PROGRAM_ACTIONS,
    PROGRAM_OUT,
    PROGRAM_REQUESTS,
    PROGRAM_RUN,
    SECRET_TYPES,
    TOOL_ACTIONS,
    TRUST_ACTIONS,
    WEAKLY_FINE,
    OpenKind,
    Risks,
    risks_of,
)
from domo_core.policy.models import PolicyRight
from domo_core.proofs.kinds import ProofPlace
from domo_core.proofs.service import offers
from domo_core.tools.people_tools import RecordOpen
from domo_core.tools.plan_tools import PlanSet
from sqlalchemy import event, func, select, true
from test_concerned_person import _judge, _people, _turn
from test_own_idea import _barrier_actors, mark

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
WHY = "Tom says the voice at the door is his brother, who stays the weekend"
MERGE_WHY = "Tom and the voice at the door are one person: he said his own name"


def call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


def a_record(name: str, **columns: Any) -> Person:
    """A record the assistant knows by name: no access, no account."""
    values: dict[str, Any] = {
        "role": "guest",
        "state": "known",
        "standing": "visiting",
        "origin": "domo",
        "origin_reason": "Alex spoke of them",
    } | columns
    return Person(display_name=name, **values)


def lives_here(name: str, **columns: Any) -> Person:
    """Someone who lives here and has an access, with no account."""
    return a_record(
        name,
        role="adult",
        state="has_access",
        standing="lives_here",
        first_access_at=NOW - timedelta(days=3),
        **columns,
    )


def proven(person: Person) -> Speaker:
    """A speaker the code is sure of: a proof it checked itself."""
    return Speaker(person.id, "voice", clues=(Clue("proof", "for", "strong", "code", NOW, "pin"),))


def probably(person: Person) -> Speaker:
    """A speaker a channel thinks is ``person``: its own clue, weak."""
    return Speaker(person.id, "voice", clues=(Clue("voice", "for", "weak", "code", NOW),))


class Told:
    """A listener of the records that keeps what it is told."""

    def __init__(self) -> None:
        self.events: list[RecordEvent] = []

    async def __call__(self, told: RecordEvent) -> None:
        self.events.append(told)


async def add_conversation_for(session: Any, person: Person) -> uuid.UUID:
    conversation = Conversation(person_id=person.id)
    session.add(conversation)
    await session.flush()
    return conversation.id


def listening() -> tuple[RecordListeners, Told]:
    listeners = RecordListeners()
    told = Told()
    listeners.subscribe(told)
    return listeners, told


async def _add(session_factory: Any, *people: Person) -> None:
    async with session_factory() as writer:
        writer.add_all(people)
        await writer.commit()


async def _claim(session_factory: Any, person: Person, *references: str) -> None:
    async with session_factory() as writer:
        for reference in references:
            await claim_reference(
                writer, channel="voice", reference=reference, person_id=person.id, now=NOW
            )
        await writer.commit()


async def _owner_turn(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, listeners: RecordListeners
) -> tuple[Any, uuid.UUID, Person]:
    people = await _people(session_factory, ("Alex", "owner"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], record_listeners=listeners)
    return ctx, reply_id, people["Alex"]


# --- a speaker without an account ----------------------------------------------------------------


async def test_a_speaker_without_an_account_is_an_actor_with_the_records_marks(session, add_person):
    await add_person("Alex", "owner")
    paul = a_record("Paul", role="child", is_child=True, standing="lives_here")
    session.add(paul)
    await session.flush()
    nephew = await made_roles.create(
        session, name="nephew", base="child", allow=(), deny=(), by="domo", reason=WHY, now=NOW
    )
    paul.made_role_id = nephew.id
    await session.flush()
    sure = actor_of(proven(paul), paul)
    assert (sure.person_id, sure.trusted, sure.child, sure.made_role) == (
        paul.id,
        True,
        True,
        nephew.id,
    )
    unsure = actor_of(probably(paul), paul)
    # A channel's "it is probably them" establishes nobody: the marks stay, the trust does not.
    assert not established(probably(paul))
    assert (unsure.trusted, unsure.child, unsure.made_role) == (False, True, nephew.id)


async def test_nothing_sensitive_on_the_word_of_someone_without_a_means_of_proof(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    tom = lives_here("Tom")
    session.add(tom)
    session.add(
        PolicyRight(
            action_type="memory.forget",
            ask="proof",
            summary="Prove it before the assistant forgets something.",
            owner_words="ask a proof",
            created_by=alex.id,
        )
    )
    await session.flush()
    forgetting = DescribedAction(
        connector="core",
        action_type="memory.forget",
        target="memory:1",
        effect="self_profile",
        destination="household",
    )
    decided = await decision.decide_now(
        session, actor_of(probably(tom), tom), forgetting, DEFAULT_POLICY, NOW
    )
    # Since the voice, never refused out of hand: the judge weighs who speaks, and the owner's
    # "ask a proof" still stands.
    assert decided == Allow(rule="proof", judge_identity=True)
    place = ProofPlace(Abilities(True, True, True, True, frozenset({"password", "passkey"})))
    assert await offers(session, tom, place, NOW) == ()


async def test_a_discussion_and_a_turn_can_belong_to_someone_without_an_account(
    session_factory, engine, monkeypatch
):
    await _people(session_factory, ("Alex", "owner"))
    tom = lives_here("Tom")
    await _add(session_factory, tom)
    reply = await said(session_factory, tom, "Remember that I like tea.")
    actors = _barrier_actors(monkeypatch)
    ctx, _, _ = await _turn(
        session_factory,
        tom,
        actor=actor_of(proven(tom), tom),
        speaker=proven(tom),
        conversation_id=reply.conversation_id,
        clock=lambda: NOW,
    )
    statements: list[str] = []

    def seen(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", seen)
    try:
        kept = await handle_tool_call(
            call(
                "memory_keep",
                content="Tom likes tea.",
                subject="Tom",
                kind="preference",
                private=False,
                core=False,
            ),
            ctx=ctx,
            reply_id=reply.id,
        )
        looked = await handle_tool_call(call("record_look"), ctx=ctx, reply_id=reply.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", seen)
    assert kept.status == "done", kept.model_result
    assert looked.status == "done", looked.model_result
    # Nothing of an account or a session was read or written for any of it.
    assert not [s for s in statements if re.search(r"\b(accounts|web_sessions)\b", s)]
    assert actors == [accounts_actor_of(tom), accounts_actor_of(tom)]
    async with session_factory() as reader:
        memory = (await reader.scalars(select(Memory))).one()
        assert memory.said_by_id == tom.id
        stored = list(
            await reader.scalars(
                select(MessageAction.action_type)
                .join(Message, Message.id == MessageAction.message_id)
                .where(Message.conversation_id == reply.conversation_id)
            )
        )
        assert sorted(stored) == ["memory.keep", "people.look"]
        lines = list(
            await reader.scalars(select(AuditEntry).where(AuditEntry.action == "policy.decision"))
        )
        assert {line.actor for line in lines} == {f"person:{tom.id}"}


async def test_someone_without_an_account_still_cannot_be_left_a_note_until_can_answer_widens(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"))
    tom = lives_here("Tom")
    await _add(session_factory, tom)
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"], said="Tell Tom dinner is at 8.")
    leave = call("note_leave", recipient="Tom", text="Dinner is at 8.")
    refused = await handle_tool_call(leave, ctx=ctx, reply_id=reply_id)
    assert refused.status != "done"
    assert "cannot be left a note" in json.dumps(refused.model_result)
    zone = ZoneInfo("UTC")
    async with session_factory() as writer:
        with pytest.raises(ValueError):
            await plans.make(
                writer, person_id=tom.id, why="Dinner", now=NOW, zone=zone, due_at=NOW + HOUR
            )
        await news_for(writer, tom.id, now=NOW)
        await writer.commit()
        assert (await writer.get(Person, tom.id)).news_at is None  # type: ignore[union-attr]
        assert tom.id not in {person.id for person in await reach.answerers(writer, NOW)}

    # The one switch: once it says a turn can reach Tom, everything that waits on it follows.
    monkeypatch.setattr(reach, "reachable", true)
    left = await handle_tool_call(
        call("note_leave", recipient="Tom", text="Dinner is at 8."), ctx=ctx, reply_id=reply_id
    )
    assert left.status == "done", left.model_result
    async with session_factory() as writer:
        found = await writer.get(Person, tom.id, populate_existing=True)
        assert found is not None and found.news_at is not None
        plan = await plans.make(
            writer, person_id=tom.id, why="Dinner", now=NOW, zone=zone, due_at=NOW + HOUR
        )
        await writer.commit()
        assert plan.id in {one.id for one in await plans.due(writer, NOW + 2 * HOUR)}
        assert tom.id in {person.id for person in await reach.answerers(writer, NOW)}


async def test_an_earlier_proof_counts_for_a_voice_unless_the_voice_weighs_against(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    await keep_proof_clue(
        session, person_id=alex.id, channel="voice", device_id=None, means="password", now=NOW
    )
    conversation = await add_conversation_for(session, alex)
    # The voice decided what such a proof is worth (its spec 6.5, "Après une preuve"): a voice
    # that does not contradict it keeps it.
    guessed = await _speaking(session, probably(alex), conversation, NOW)
    assert [clue.kind for clue in guessed.clues] == ["voice", "proof"]
    assert actor_of(guessed, alex).trusted
    # One that weighs against Alex gets none of it.
    against = Speaker(alex.id, "voice", clues=(Clue("voice", "against", "weak", "code", NOW),))
    contradicted = await _speaking(session, against, conversation, NOW)
    assert [clue.kind for clue in contradicted.clues] == ["voice"]
    assert not actor_of(contradicted, alex).trusted
    # Where the code tied the turn to Alex itself, the proof kept counts as it always did.
    sure = Speaker(alex.id, "voice", clues=(Clue("session", "for", "strong", "code", NOW),))
    told = await _speaking(session, sure, conversation, NOW)
    assert [clue.kind for clue in told.clues] == ["session", "proof"]


def _deps_of(app: Any) -> Any:
    return turn_deps_of(
        app,
        session_factory=None,  # type: ignore[arg-type]
        model_factory=None,  # type: ignore[arg-type]
        model_lister=None,  # type: ignore[arg-type]
        streaming_replies=set(),
        settings=None,  # type: ignore[arg-type]
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry={},
        policy=DEFAULT_POLICY,
    )


def test_the_application_has_one_record_listeners_for_every_turn(app):
    assert isinstance(app.state.record_listeners, RecordListeners)
    assert _deps_of(app).record_listeners is app.state.record_listeners
    assert _deps_of(app).record_listeners is app.state.record_listeners
    # Never a second one nobody subscribed to: an application without it is a mistake.
    del app.state.record_listeners
    with pytest.raises(AttributeError):
        _deps_of(app)


# --- what an unsure speaker may do ---------------------------------------------------------------

_CONFIRMATIONS = frozenset(t for t in KNOWN_ACTIONS if t.startswith("confirmation.")) - {
    "confirmation.own_idea"
}
_RISKY = frozenset(
    t
    for t in KNOWN_ACTIONS
    if (r := risks_of(t, DEFAULT_POLICY)).costs or r.outward or r.irreversible
)
# Each row of the table of what is sensitive (the brief of Task 24), as the catalog names it.
SENSITIVE_ROWS: dict[str, frozenset[str]] = {
    "the owner's own": OWNER_ONLY_ACTIONS,
    "who may do what": PERMISSION_TYPES,
    "people and directives": PEOPLE_GESTURES
    | PEOPLE_READ_ACTIONS
    | OWNER_DIRECTIVE_ACTIONS
    | {"people.forget_me", "household.show_link", "household.leave"},
    "rights to read": frozenset({"child.agree", HOUSE_SET}),
    "secrets and addresses": SECRET_TYPES
    | OUTWARD_ACTIONS
    | {"account.remove", "account.share", NET_OPEN_HOST},
    "spending": frozenset({"spending.limit", "spending.show"})
    | {t for t in KNOWN_ACTIONS if risks_of(t, DEFAULT_POLICY).costs},
    "outward or irreversible": _RISKY | {"note.leave", "note.withdraw", "note.keep"},
    "devices": frozenset({"device.list", "device.remove"}),
    "private memory and trust": frozenset({"memory.forget", "memory.change", "memory.search"})
    | TRUST_ACTIONS
    | DELIVERY_ACTIONS
    | {"note.told", "note.show", "note.list_sent", "texts.find"},
    "another's things": frozenset({"texts.for_someone"}),
    "a yes, an agreement, a chain": _CONFIRMATIONS
    | AGREEMENT_ACTIONS
    | {"chain.run", "chain.continue"},
    "changing a plan": frozenset({"plan.change", "plan.cancel"}),
    "writing or timing a tool": TOOL_ACTIONS | PROGRAM_ACTIONS | {PROGRAM_RUN},
}

# The ordinary things, as the brief lists them, and what the difference left that was reviewed.
ORDINARY = frozenset(
    {
        "household.list",
        "policy.show",
        "profile.rename",
        "profile.name_domo",
        "texts.rewrite",
        "texts.restore",
        "texts.language",
        "texts.note",
        "writing.choose",
        "models.choose",
        "models.refresh",
        "memory.keep",
        "people.nickname_keep",
        "people.nickname_forget",
        "confirmation.own_idea",
        "learning.note",
        "learning.asked",
        # Of the voice.
        "voice.stop_listening",
        "voice.manner_note",
    }
)


@pytest.mark.parametrize("row", sorted(SENSITIVE_ROWS))
def test_nothing_sensitive_is_weak(row):
    for action_type in SENSITIVE_ROWS[row]:
        assert action_type in NOT_WEAKLY, action_type
        assert not may_act_weakly(action_type, DEFAULT_POLICY), action_type


def test_what_is_weak_is_exactly_the_ordinary_list():
    assert KNOWN_ACTIONS - NOT_WEAKLY - PROGRAM_REQUESTS == WEAKLY_FINE
    assert WEAKLY_FINE == ORDINARY
    for action_type in KNOWN_ACTIONS:
        sets = [action_type in one for one in (NOT_WEAKLY, WEAKLY_FINE, PROGRAM_REQUESTS)]
        assert sets.count(True) == 1, action_type
    for action_type in WEAKLY_FINE:
        assert may_act_weakly(action_type, DEFAULT_POLICY), action_type


def test_a_plan_made_changed_or_cancelled_is_never_weak():
    # Making one too, since the voice's merge: it runs later as the person, sure.
    assert not may_act_weakly("plan.set", DEFAULT_POLICY)
    assert not may_act_weakly("plan.change", DEFAULT_POLICY)
    assert not may_act_weakly("plan.cancel", DEFAULT_POLICY)
    arguments = PlanSet.definition.parameters["properties"]
    assert isinstance(arguments, dict)
    named = {"person", "recipient", "for", "for_person", "about", "who", "target"}
    assert not named & set(arguments)


def test_a_new_unknown_type_is_not_weak_by_default():
    assert not may_act_weakly("brand.new", DEFAULT_POLICY)
    known = replace(DEFAULT_POLICY, known=DEFAULT_POLICY.known | {"brand.new"})
    assert not may_act_weakly("brand.new", known)


def _with_lights() -> Any:
    lights = OpenKind(
        "made.lights", False, False, True, False, {}, frozenset(), "Turns the lights on."
    )
    return replace(
        DEFAULT_POLICY,
        known=DEFAULT_POLICY.known | {"made.lights"},
        kinds={"made.lights": lights},
    )


def test_a_launch_follows_the_risks_its_operation_declares():
    policy = _with_lights()
    # Acting in the house is ordinary only when it was declared harmless (the voice's rule).
    assert not may_act_weakly("made.lights", policy, risks=Risks(physical=True))
    assert may_act_weakly("made.lights", policy, risks=Risks(physical=True, harmless=True))
    assert may_act_weakly("made.lights", policy, risks=Risks())
    for risky in (Risks(costs=True), Risks(outward=True), Risks(irreversible=True)):
        assert not may_act_weakly("made.lights", policy, risks=risky)
        assert not may_act_weakly("made.lights", policy, risks=risky | Risks(physical=True))
    assert not may_act_weakly("made.lights", policy, risks=None)
    # A launch of a kind the catalog does not hold is not weak, whatever is given.
    assert not may_act_weakly("made.unknown", policy, risks=Risks())
    # What a running program asks follows the risks the code sees in it.
    inside = OutAsk("GET", "http://192.168.1.20/state", {}, None, None)
    outside = OutAsk("GET", "https://93.184.216.34/state", {}, None, None)
    assert may_act_weakly(PROGRAM_OUT, policy, risks=minimum_risks(inside, policy))
    assert not may_act_weakly(PROGRAM_OUT, policy, risks=minimum_risks(outside, policy))
    assert not may_act_weakly(PROGRAM_OUT, policy, risks=Risks(physical=True))
    assert may_act_weakly(PROGRAM_OUT, policy, risks=Risks(physical=True, harmless=True))
    for risky in (Risks(costs=True), Risks(outward=True), Risks(irreversible=True)):
        assert not may_act_weakly(PROGRAM_OUT, policy, risks=risky)
    assert not may_act_weakly(PROGRAM_OUT, policy, risks=None)
    forgetting = ActAsk(action="memory.forget", arguments={})
    assert not may_act_weakly(PROGRAM_OUT, policy, risks=minimum_risks(forgetting, policy))


def test_writing_changing_removing_or_timing_a_tool_is_never_weak():
    for action_type in TOOL_ACTIONS | PROGRAM_ACTIONS | {PROGRAM_RUN}:
        for risks in (None, Risks(), Risks(physical=True)):
            assert not may_act_weakly(action_type, DEFAULT_POLICY, risks=risks), action_type


def test_decide_reads_it_for_someone_the_code_is_not_sure_of(monkeypatch):
    asked: list[str] = []

    def weakly(action_type: str, *_: Any, **__: Any) -> bool:
        asked.append(action_type)
        return True

    monkeypatch.setattr(barrier, "may_act_weakly", weakly)
    tom = lives_here("Tom", id=uuid.uuid4())
    keeping = DescribedAction(
        connector="core",
        action_type="memory.keep",
        target="memory:new",
        effect="self_profile",
        destination="household",
    )
    unsure = actor_of(probably(tom), tom)
    assert barrier.decide(unsure, keeping) == Allow()
    assert asked == ["memory.keep"]
    # Someone the code is sure of is never asked about.
    asked.clear()
    assert barrier.decide(actor_of(proven(tom), tom), keeping) == Allow()
    assert asked == []


async def test_the_directives_are_checked_before_anything_may_act_weakly_could_allow(
    session, add_person, monkeypatch
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = a_record("Sam")
    session.add(sam)
    await session.flush()
    await set_directive(
        session,
        kind="refuse",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    called: list[str] = []

    def allows(*args: Any, **_: Any) -> Allow:
        called.append("decide")
        return Allow()

    monkeypatch.setattr(decision, "decide", allows)
    monkeypatch.setattr(barrier, "may_act_weakly", lambda *_, **__: True)
    giving = DescribedAction(
        connector="core",
        action_type="household.change_role",
        target=f"person:{sam.id}",
        effect="permissions",
        destination="household",
    )
    decided = await decision.decide_now(
        session, actor_of(probably(dana), dana), giving, DEFAULT_POLICY, NOW
    )
    assert decided == Refuse("owner_directive")
    assert called == []


# --- a reference a channel owns ------------------------------------------------------------------


async def test_a_reference_is_claimed_for_a_person(session, add_person):
    await add_person("Alex", "owner")
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    before = (tom.state, tom.role, tom.made_role_id, tom.first_access_at)
    await claim_reference(session, channel="voice", reference="v-1", person_id=tom.id, now=NOW)
    assert await person_of_ref(session, channel="voice", reference="v-1") == tom.id
    assert "v-1" in await refs_of(session, "voice")
    await session.refresh(tom)
    assert (tom.state, tom.role, tom.made_role_id, tom.first_access_at) == before
    assert set(ChannelRef.__table__.columns.keys()) == {
        "id",
        "person_id",
        "channel",
        "reference",
        "created_at",
    }


async def test_someone_who_already_has_references_may_claim_another(session):
    tom = a_record("Tom")
    session.add(tom)
    await session.flush()
    await claim_reference(session, channel="voice", reference="v-1", person_id=tom.id, now=NOW)
    await claim_reference(session, channel="voice", reference="v-2", person_id=tom.id, now=NOW)
    assert await refs_of(session, "voice") == {"v-1", "v-2"}
    for reference in ("v-1", "v-2"):
        assert await person_of_ref(session, channel="voice", reference=reference) == tom.id


async def test_claiming_twice_changes_nothing_and_another_person_cannot_take_it(session):
    tom, sam = a_record("Tom"), a_record("Sam")
    session.add_all([tom, sam])
    await session.flush()
    await claim_reference(session, channel="voice", reference="v-1", person_id=tom.id, now=NOW)
    await claim_reference(session, channel="voice", reference="v-1", person_id=tom.id, now=NOW)
    count = select(func.count()).select_from(ChannelRef)
    assert await session.scalar(count) == 1
    with pytest.raises(ReferenceTaken) as taken:
        await claim_reference(session, channel="voice", reference="v-1", person_id=sam.id, now=NOW)
    assert taken.value.holder == tom.id
    await claim_reference(session, channel="door", reference="v-1", person_id=sam.id, now=NOW)
    assert await person_of_ref(session, channel="door", reference="v-1") == sam.id
    assert await person_of_ref(session, channel="voice", reference="v-1") == tom.id
    for channel, reference in (("", "v-2"), ("c" * 41, "v-2"), ("voice", ""), ("voice", "r" * 201)):
        with pytest.raises(ValueError):
            await claim_reference(
                session, channel=channel, reference=reference, person_id=tom.id, now=NOW
            )
    with pytest.raises(ValueError):
        await claim_reference(
            session, channel="voice", reference="two\nlines", person_id=tom.id, now=NOW
        )


async def test_no_reference_for_a_merged_forgotten_or_unknown_person(session, add_person):
    await add_person("Alex", "owner")
    tom, sam, dana = a_record("Tom"), a_record("Sam"), a_record("Dana")
    session.add_all([tom, sam, dana])
    await session.flush()
    await merge.merge(
        session, kept=tom, absorbed=sam, by="domo", reason=MERGE_WHY, turn_of=None, now=NOW
    )
    await forget.forget(session, dana, by="domo", turn_of=None, now=NOW)
    for person_id in (sam.id, dana.id, uuid.uuid4()):
        with pytest.raises(NoSuchRecord):
            await claim_reference(
                session, channel="voice", reference="v-1", person_id=person_id, now=NOW
            )
    assert await refs_of(session, "voice") == set()


async def test_a_record_opened_for_a_voice_carries_the_assistants_own_reason(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    properties = RecordOpen.definition.parameters["properties"]
    assert isinstance(properties, dict)
    # exception_of: the owner's rule whose words he judged make it an exception (final review B-I4).
    assert set(properties) == {"name", "standing", "is_child", "ground", "reason", "exception_of"}
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], said="Tom is at the door.")
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    opened = await handle_tool_call(
        call("record_open", name="Tom", standing="visiting", ground="comes", reason=WHY),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert opened.status == "done", opened.model_result
    tom_id = uuid.UUID(opened.model_result["id"])
    async with session_factory() as writer:
        tom = await writer.get(Person, tom_id)
        assert tom is not None and (tom.origin, tom.origin_reason) == ("domo", WHY)
        await claim_reference(writer, channel="voice", reference="v-1", person_id=tom_id, now=NOW)
        await writer.commit()
        assert await person_of_ref(writer, channel="voice", reference="v-1") == tom_id


async def test_no_record_is_opened_under_no_new_people(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    async with session_factory() as writer:
        owner = await writer.get(Person, people["Alex"].id)
        assert owner is not None
        await set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=NOW,
        )
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], said="Tom is at the door.")
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    opened = await handle_tool_call(
        call("record_open", name="Tom", standing="visiting", ground="comes", reason=WHY),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert opened.status == "refused"
    assert opened.model_result["reason"] == "owner_directive"
    async with session_factory() as reader:
        assert (await reader.scalar(select(func.count()).where(Person.display_name == "Tom"))) == 0


# --- the notices ---------------------------------------------------------------------------------


async def test_a_merge_tells_the_absorbed_the_kept_and_the_refs_moved(session_factory, monkeypatch):
    listeners, told = listening()
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, listeners)
    tom, sam = a_record("Tom"), a_record("Sam")
    await _add(session_factory, tom, sam)
    await _claim(session_factory, sam, "v-1")
    done = await handle_tool_call(
        call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{sam.id}"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert done.status == "done", done.model_result
    [merged] = told.events
    assert (merged.kind, merged.person_id, merged.into_id, merged.refs) == (
        "merged",
        sam.id,
        tom.id,
        (("voice", "v-1"),),
    )
    assert merged.keep_minimum is False
    async with session_factory() as reader:
        assert await person_of_ref(reader, channel="voice", reference="v-1") == tom.id


async def test_a_split_tells_too_and_moves_the_references_back(session_factory, monkeypatch):
    listeners, told = listening()
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, listeners)
    tom, sam = a_record("Tom"), a_record("Sam")
    await _add(session_factory, tom, sam)
    await _claim(session_factory, sam, "v-1")
    await handle_tool_call(
        call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{sam.id}"),
        ctx=ctx,
        reply_id=reply_id,
    )
    # Claimed for the kept record since the merge: it stays on it.
    await _claim(session_factory, tom, "v-2")
    back = await handle_tool_call(
        call("records_split", person=f"person:{tom.id}"), ctx=ctx, reply_id=reply_id
    )
    assert back.status == "done", back.model_result
    split = told.events[-1]
    assert (split.kind, split.person_id, split.into_id, split.refs, split.keep_minimum) == (
        "split",
        sam.id,
        tom.id,
        (("voice", "v-1"),),
        False,
    )
    async with session_factory() as reader:
        assert await person_of_ref(reader, channel="voice", reference="v-1") == sam.id
        assert await person_of_ref(reader, channel="voice", reference="v-2") == tom.id


async def test_erase_tells_with_the_refs_it_had_and_leaves_none(session_factory, monkeypatch):
    listeners, told = listening()
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, listeners)
    tom = a_record("Tom")
    await _add(session_factory, tom)
    await _claim(session_factory, tom, "v-1", "v-2")
    done = await handle_tool_call(
        call("record_erase", person=f"person:{tom.id}"), ctx=ctx, reply_id=reply_id
    )
    assert done.status == "done", done.model_result
    [erased] = told.events
    assert (erased.kind, erased.person_id, erased.into_id, erased.keep_minimum) == (
        "erased",
        tom.id,
        None,
        False,
    )
    assert set(erased.refs) == {("voice", "v-1"), ("voice", "v-2")}
    async with session_factory() as reader:
        assert await refs_of(reader, "voice") == set()


async def test_after_a_forget_the_reference_is_gone_for_reconciliation(
    session_factory, monkeypatch
):
    listeners, told = listening()
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, listeners)
    tom = a_record("Tom")
    await _add(session_factory, tom)
    await _claim(session_factory, tom, "v-1")
    done = await handle_tool_call(
        call("record_forget", person=f"person:{tom.id}"), ctx=ctx, reply_id=reply_id
    )
    assert done.status == "done", done.model_result
    [forgotten] = told.events
    assert (forgotten.kind, forgotten.person_id, forgotten.into_id, forgotten.refs) == (
        "forgotten",
        tom.id,
        None,
        (("voice", "v-1"),),
    )
    assert forgotten.keep_minimum is False
    async with session_factory() as reader:
        assert await person_of_ref(reader, channel="voice", reference="v-1") is None
        assert "v-1" not in await refs_of(reader, "voice")


async def _refused_sam(session_factory: Any, owner: Person) -> Person:
    sam = a_record("Sam")
    await _add(session_factory, sam)
    await _claim(session_factory, sam, "v-1")
    async with session_factory() as writer:
        alex = await writer.get(Person, owner.id)
        row = await writer.get(Person, sam.id)
        assert alex is not None and row is not None
        await set_directive(
            writer,
            kind="refuse",
            person=row,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )
        await writer.commit()
    return sam


async def test_keep_minimum_is_false_by_default_even_under_a_refusal(session_factory, monkeypatch):
    listeners, told = listening()
    ctx, reply_id, alex = await _owner_turn(session_factory, monkeypatch, listeners)
    sam = await _refused_sam(session_factory, alex)
    done = await handle_tool_call(
        call("record_forget", person=f"person:{sam.id}"), ctx=ctx, reply_id=reply_id
    )
    assert done.status == "done", done.model_result
    [forgotten] = told.events
    assert (forgotten.kind, forgotten.refs, forgotten.keep_minimum) == (
        "forgotten",
        (("voice", "v-1"),),
        False,
    )
    async with session_factory() as reader:
        assert await refs_of(reader, "voice") == set()


async def test_with_the_constant_on_the_refs_of_a_refused_person_survive_the_forget(
    session_factory, monkeypatch
):
    monkeypatch.setattr(forget, "KEEP_RECOGNITION_OF_THE_REFUSED", True)
    listeners, told = listening()
    ctx, reply_id, alex = await _owner_turn(session_factory, monkeypatch, listeners)
    sam = await _refused_sam(session_factory, alex)
    tom = a_record("Tom")
    await _add(session_factory, tom)
    await _claim(session_factory, tom, "v-2")
    for person in (sam, tom):
        done = await handle_tool_call(
            call("record_forget", person=f"person:{person.id}"), ctx=ctx, reply_id=reply_id
        )
        assert done.status == "done", done.model_result
    kept, gone = told.events
    assert (kept.person_id, kept.refs, kept.keep_minimum) == (sam.id, (("voice", "v-1"),), True)
    assert (gone.person_id, gone.refs, gone.keep_minimum) == (tom.id, (("voice", "v-2"),), False)
    async with session_factory() as reader:
        stub = await reader.get(Person, sam.id)
        assert stub is not None and stub.forgotten_at is not None and stub.display_name == ""
        assert await person_of_ref(reader, channel="voice", reference="v-1") == sam.id
        assert await person_of_ref(reader, channel="voice", reference="v-2") is None
        await forget.finish(reader, sam.id, NOW)
        await reader.commit()
        assert await person_of_ref(reader, channel="voice", reference="v-1") == sam.id


async def test_notices_leave_after_the_commit(session_factory, monkeypatch):
    seen: list[uuid.UUID | None] = []

    async def reading(told: RecordEvent) -> None:
        async with session_factory() as reader:
            absorbed = await reader.get(Person, told.person_id)
            assert absorbed is not None
            seen.append(absorbed.merged_into_id)

    listeners = RecordListeners()
    listeners.subscribe(reading)
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, listeners)
    tom, sam = a_record("Tom"), a_record("Sam")
    await _add(session_factory, tom, sam)
    done = await handle_tool_call(
        call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{sam.id}"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert done.status == "done", done.model_result
    assert seen == [tom.id]


async def test_a_listener_that_fails_undoes_nothing_and_the_next_one_is_still_told(
    session_factory, monkeypatch, caplog
):
    async def failing(_: RecordEvent) -> None:
        raise RuntimeError("the channel's own secret detail")

    listeners = RecordListeners()
    listeners.subscribe(failing)
    told = Told()
    listeners.subscribe(told)
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, listeners)
    tom, sam = a_record("Tom"), a_record("Sam")
    await _add(session_factory, tom, sam)
    with caplog.at_level(logging.ERROR, logger=hooks.__name__):
        done = await handle_tool_call(
            call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{sam.id}"),
            ctx=ctx,
            reply_id=reply_id,
        )
    assert done.status == "done", done.model_result
    assert [e.kind for e in told.events] == ["merged"]
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    async with session_factory() as reader:
        absorbed = await reader.get(Person, sam.id)
        assert absorbed is not None and absorbed.merged_into_id == tom.id


async def test_with_no_listener_nothing_happens(session_factory, monkeypatch):
    await RecordListeners().emit(
        RecordEvent(
            kind="erased", person_id=uuid.uuid4(), into_id=None, refs=(), keep_minimum=False, at=NOW
        )
    )
    ctx, reply_id, _ = await _owner_turn(session_factory, monkeypatch, RecordListeners())
    tom, sam = a_record("Tom"), a_record("Sam")
    await _add(session_factory, tom, sam)
    done = await handle_tool_call(
        call("records_merge", keep=f"person:{tom.id}", absorb=f"person:{sam.id}"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert done.status == "done", done.model_result


def test_hooks_imports_no_channel():
    tree = ast.parse(inspect.getsource(hooks))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert imported
    assert not [name for name in imported if name.startswith("domo_core.channels")]
    assert not [name for name in imported if "voice" in name]
