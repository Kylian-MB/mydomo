"""The tools of the voice (spec 6.2, 6.5, 6.9): agreeing, changing and removing the spoken phrase,
stopping the listening, "that was not me", and the assistant's own reading of how someone speaks.

The phrase itself never reaches these tools: they open and close a reserved moment
(``channels.voice.moments``) in which the voice channel sends the two sayings to the vault, and
remove the phrase at the vault. Agreeing and changing are the person's own, with a sure speaker
and, when they have another means, after a proof by it (ruling T11).
"""

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Account, Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.voice.live import LIVE_VOICES
from domo_core.channels.voice.models import VoiceMoment, VoicePrint, VoiceRef
from domo_core.channels.voice.prints import reference_for
from domo_core.conversation.channel import Abilities
from domo_core.conversation.speaker import Clue, Speaker, heeded_proofs, standing
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.identity.clues import PROOF_CLUE_FOR, kept_clues
from domo_core.notes.models import Note
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, is_silent
from domo_core.policy.roles import Actor, domo_actor
from domo_core.policy.subjects import Subject, subject_of
from domo_core.prompts import DuePlan
from domo_core.proofs.kinds import phrase_ref
from domo_core.proofs.models import ProofMean
from domo_core.proofs.service import agree
from domo_core.tools.context import InvalidArguments, NamesPeople, ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS
from domo_core.tools.voice_tools import remember_the_weakness_told
from sqlalchemy import select
from voice_support import ALEX, DANA, MIC, NOW, TOM, learnt_voice, print_of

VOICE_LIKE = Abilities(
    buttons=False,
    shows_once=False,
    safe_screen=False,
    hears_unasked=True,
    proof_kinds=frozenset({"password", "spoken_phrase"}),
)
PHRASE_TOOLS = ["spoken_phrase_agree", "spoken_phrase_change", "spoken_phrase_remove"]
PHRASE_TYPES = ["proof.phrase_agree", "proof.phrase_change", "proof.phrase_remove"]
ALL_TOOLS = [*PHRASE_TOOLS, "voice_stop_listening", "voice_unlearn", "speaker_manner_note"]


class FakeVault:
    """The gatekeeper as these tools use it: only ``phrase_remove``."""

    def __init__(self, *, down: bool = False) -> None:
        self.removed: list[str] = []
        self.down = down

    async def phrase_remove(self, ref: str) -> bool:
        if self.down:
            raise GatekeeperError("DELETE /internal/phrases/{ref}", status=503)
        self.removed.append(ref)
        return True


def sure(person: Person, *, proof_by: str | None = None) -> Speaker:
    """A speaker the code is sure of (a strong clue of the code), with a proof when asked."""
    clues = [Clue("session", "for", "strong", "code", NOW)]
    if proof_by is not None:
        clues.append(Clue("proof", "for", "strong", "code", NOW, means=proof_by))
    return Speaker(person.id, "voice", clues=tuple(clues))


def context(
    person: Person,
    session_factory: SessionFactory,
    *,
    vault: FakeVault | None = None,
    speaker: Speaker | None = None,
    abilities: Abilities = VOICE_LIKE,
    answers: bool = True,
    told_outside: bool = False,
    told_unknown: bool = False,
    approved_by: uuid.UUID | None = None,
) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=vault or FakeVault(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4() if answers else None,
        abilities=abilities,
        speaker=speaker if speaker is not None else sure(person),
        told_outside=told_outside,
        told_unknown=told_unknown,
        approved_by=approved_by,
    )


async def call(name: str, ctx: ToolContext, arguments: dict[str, Any] | None = None) -> ToolOutcome:
    """Describe then execute, as a turn does."""
    tool = TOOLS[name]
    await tool.describe(arguments or {}, ctx.actor, ctx)
    return await tool.execute(arguments or {}, ctx)


async def refusal(name: str, ctx: ToolContext, arguments: dict[str, Any] | None = None) -> str:
    """The code the tool refuses with, in ``describe`` and again in ``execute``."""
    tool = TOOLS[name]
    with pytest.raises(InvalidArguments) as at_describe:
        await tool.describe(arguments or {}, ctx.actor, ctx)
    with pytest.raises(InvalidArguments) as at_execute:
        await tool.execute(arguments or {}, ctx)
    assert at_describe.value.code == at_execute.value.code
    return at_describe.value.code


async def alex_and_dana(session_factory: SessionFactory) -> tuple[Person, Person]:
    """Alex and Dana, each with an account: a turn can reach them (``people.reach``)."""
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        dana = Person(display_name="Dana", role="adult")
        writer.add_all([alex, dana])
        await writer.flush()
        writer.add_all(
            [
                Account(person_id=alex.id, username="alex", password_hash="x"),
                Account(person_id=dana.id, username="dana", password_hash="x"),
            ]
        )
        await writer.commit()
    return alex, dana


async def means(session_factory: SessionFactory, person: Person, kind: str) -> list[ProofMean]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(ProofMean).where(ProofMean.person_id == person.id, ProofMean.kind == kind)
        )
        return list(found.scalars())


async def agreed(session_factory: SessionFactory, person: Person, kind: str) -> None:
    async with session_factory() as writer:
        await agree(writer, person.id, kind, proposed_by="person", now=NOW)
        await writer.commit()


async def moments(session_factory: SessionFactory) -> list[VoiceMoment]:
    async with session_factory() as reader:
        return list((await reader.execute(select(VoiceMoment))).scalars())


# --- agreeing -------------------------------------------------------------------------------------


async def test_agreeing_opens_a_moment_and_stores_nothing_else(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    vault = FakeVault()
    outcome = await call("spoken_phrase_agree", context(alex, session_factory, vault=vault))
    assert outcome.status == "done"
    assert outcome.card == {"key": "action.card.phrase_agree_opened", "params": {}}
    [moment] = await moments(session_factory)
    assert (moment.person_id, moment.purpose, moment.step) == (alex.id, "phrase_agree", 0)
    assert moment.expires_at > NOW
    # No means is agreed yet: that is for the end of the second saying.
    assert await means(session_factory, alex, "spoken_phrase") == []
    # A stored phrase is never revived: agreeing starts afresh at the vault.
    assert vault.removed == [phrase_ref(alex.id)]
    # Nothing of a phrase is in the result: only what the model needs to go on.
    assert set(outcome.model_result) == {"moment", "tell_the_weakness"}


async def test_agreeing_is_refused_where_no_phrase_can_be_said(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    ctx = context(alex, session_factory, abilities=PAGE)
    for name in PHRASE_TOOLS[:2]:
        assert await refusal(name, ctx) == "not_possible"
    assert await moments(session_factory) == []


async def test_changing_needs_one_agreed(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    vault = FakeVault()
    ctx = context(alex, session_factory, vault=vault)
    assert await refusal("spoken_phrase_change", ctx) == "not_possible"
    await agreed(session_factory, alex, "spoken_phrase")
    outcome = await call("spoken_phrase_change", ctx)
    assert outcome.status == "done"
    [moment] = await moments(session_factory)
    assert moment.purpose == "phrase_change"
    # The old phrase stays until the new one is agreed: nothing is removed now.
    assert vault.removed == []
    assert len(await means(session_factory, alex, "spoken_phrase")) == 1


async def test_agreeing_a_second_phrase_is_refused_it_is_a_change(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    assert await refusal("spoken_phrase_agree", context(alex, session_factory)) == "not_possible"


async def test_removing_takes_it_from_the_vault_and_from_the_means(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await agreed(session_factory, alex, "password")
    await agreed(session_factory, dana, "spoken_phrase")
    vault = FakeVault()
    outcome = await call("spoken_phrase_remove", context(alex, session_factory, vault=vault))
    assert outcome.status == "done"
    assert outcome.card == {"key": "action.card.phrase_removed", "params": {}}
    assert vault.removed == [phrase_ref(alex.id)]
    [phrase] = await means(session_factory, alex, "spoken_phrase")
    assert phrase.removed_at == NOW
    # Another means of the same person, and the means of another person, stay.
    [password] = await means(session_factory, alex, "password")
    assert password.removed_at is None
    [theirs] = await means(session_factory, dana, "spoken_phrase")
    assert theirs.removed_at is None


async def test_removing_leaves_the_means_when_the_vault_does_not_answer(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    ctx = context(alex, session_factory, vault=FakeVault(down=True))
    outcome = await call("spoken_phrase_remove", ctx)
    assert outcome.status == "failed"
    [phrase] = await means(session_factory, alex, "spoken_phrase")
    assert phrase.removed_at is None


async def test_removing_clears_a_phrase_left_at_the_vault_with_no_means(session_factory):
    """A phrase may be left at the vault with no means (a half agreement): removing clears it."""
    alex, _ = await alex_and_dana(session_factory)
    vault = FakeVault()
    outcome = await call("spoken_phrase_remove", context(alex, session_factory, vault=vault))
    assert outcome.status == "done"
    assert vault.removed == [phrase_ref(alex.id)]
    assert outcome.model_result == {"had_a_phrase": True, "had_a_means": False}


async def test_a_phrase_tool_from_someone_elses_words_is_refused(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    vault = FakeVault()
    # No words of anyone and no text at all in the turn: never.
    no_words = context(alex, session_factory, vault=vault, answers=False)
    for name in PHRASE_TOOLS:
        assert await refusal(name, no_words) == "not_possible", name
    # A program's text alone (the owner, 2026-11-26: it all depends on the source): the judge
    # weighs it, told which program; agreeing one stays refused here, one is agreed already.
    by_a_program = context(alex, session_factory, vault=vault, answers=False)
    by_a_program.raise_outside_text("program")
    woken = context(alex, session_factory, vault=vault, answers=False)
    woken.tool_woke = True
    for ctx in (by_a_program, woken):
        assert await refusal("spoken_phrase_agree", ctx) == "not_possible"
        for name in PHRASE_TOOLS[1:]:
            assert (await TOOLS[name].describe({}, ctx.actor, ctx)).whose_judged, name
    # Beside their own words, the judge weighs it (the owner, 2026-11-26).
    beside = context(alex, session_factory, vault=vault)
    beside.raise_outside_text("tool")
    for name in PHRASE_TOOLS[1:]:
        assert (await TOOLS[name].describe({}, beside.actor, beside)).whose_judged, name
    assert await moments(session_factory) == []
    assert vault.removed == []
    assert [m.removed_at for m in await means(session_factory, alex, "spoken_phrase")] == [None]


async def test_a_yes_of_the_person_themself_lets_a_stored_request_run(session_factory):
    """A call the judge held for the person's yes runs in no turn of words: their own yes is
    theirs, nobody else's."""
    alex, dana = await alex_and_dana(session_factory)
    own = context(alex, session_factory, answers=False, approved_by=alex.id)
    assert (await call("spoken_phrase_agree", own)).status == "done"
    elsewhere = context(alex, session_factory, answers=False, approved_by=dana.id)
    assert await refusal("spoken_phrase_agree", elsewhere) == "not_possible"


async def test_agreeing_and_changing_are_for_the_person_themself(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    someone_else = sure(dana)
    nobody = Speaker(None, "voice")
    for speaker in (someone_else, nobody):
        ctx = context(alex, session_factory, speaker=speaker)
        for name in PHRASE_TOOLS[:2]:
            assert await refusal(name, ctx) == "not_possible", (name, speaker)
    # A voice the code is not sure of is the judge's to weigh (case 2), never a refusal here.
    unsure = Speaker(alex.id, "voice", clues=(Clue("voice", "for", "weak", "code", NOW),))
    by_domo = Speaker(alex.id, "voice", clues=(Clue("style", "for", "strong", "domo", NOW),))
    for speaker in (unsure, by_domo):
        ctx = context(alex, session_factory, speaker=speaker)
        await TOOLS["spoken_phrase_change"].describe({}, ctx.actor, ctx)
    ctx = context(alex, session_factory)
    ctx.speaker = None
    assert await refusal("spoken_phrase_change", ctx) == "not_possible"
    assert await moments(session_factory) == []


async def test_a_person_with_another_means_proves_it_before_agreeing(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "password")
    # Sure of the speaker is not enough: the person has a password, so a proof by it is asked.
    assert await refusal("spoken_phrase_agree", context(alex, session_factory)) == "not_possible"
    # A proof by the phrase itself is no other means.
    said = sure(alex, proof_by="spoken_phrase")
    ctx = context(alex, session_factory, speaker=said)
    assert await refusal("spoken_phrase_agree", ctx) == "not_possible"
    proven = sure(alex, proof_by="password")
    outcome = await call("spoken_phrase_agree", context(alex, session_factory, speaker=proven))
    assert outcome.status == "done"


async def test_the_weakness_is_to_be_told_until_a_first_saying_is_received(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    first = await call("spoken_phrase_agree", context(alex, session_factory))
    assert first.model_result["tell_the_weakness"] is True
    # Opened again before anything was said: nothing was heard yet, so it is still to be told.
    again = await call("spoken_phrase_agree", context(alex, session_factory))
    assert again.model_result["tell_the_weakness"] is True
    # The voice channel received a first saying (ruling T14): remembered as told, once.
    for _ in range(2):
        async with session_factory() as writer:
            await remember_the_weakness_told(writer, alex.id)
            await writer.commit()
    later = await call("spoken_phrase_agree", context(alex, session_factory))
    assert later.model_result["tell_the_weakness"] is False
    await agreed(session_factory, alex, "spoken_phrase")
    changed = await call("spoken_phrase_change", context(alex, session_factory))
    assert changed.model_result["tell_the_weakness"] is False
    # It is each person's own: Dana has not been told.
    theirs = await call("spoken_phrase_agree", context(dana, session_factory))
    assert theirs.model_result["tell_the_weakness"] is True
    async with session_factory() as reader:
        lines = (await reader.execute(select(AuditEntry.action, AuditEntry.actor))).all()
    told = [actor for action, actor in lines if action == "voice.phrase_weakness_told"]
    assert told == [f"person:{alex.id}"]


# --- stopping the listening -----------------------------------------------------------------------


async def test_stop_listening_is_silent_and_turns_the_pages_off(session_factory):
    assert is_silent("voice.stop_listening")
    assert TOOLS["voice_stop_listening"].action_type == "voice.stop_listening"
    # Anyone's turn, whoever's words are in it, a guest's too: it only turns a microphone off.
    async with session_factory() as writer:
        guest = Person(display_name="Tom", role="guest")
        writer.add(guest)
        await writer.commit()

    class Listening:
        def __init__(self) -> None:
            self.off = 0

        def says(self, reply_id: uuid.UUID) -> bool:
            return False

        async def turn_off(self) -> None:
            self.off += 1

    page = Listening()
    leave = LIVE_VOICES.enter(guest.id, page)
    try:
        outcome = await call(
            "voice_stop_listening", context(guest, session_factory, abilities=PAGE)
        )
    finally:
        leave()
    assert outcome.status == "done"
    assert outcome.model_result == {"pages_turned_off": 1}
    assert page.off == 1
    # Acted on at once: nothing is left to act on later.
    assert await moments(session_factory) == []


async def test_stop_listening_is_for_the_persons_own_words(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    # No words of anyone and no text at all never turns a microphone off.
    nothing = context(alex, session_factory, answers=False)
    assert await refusal("voice_stop_listening", nothing) == "not_possible"
    # A program's text alone: the judge weighs whose wish it is (the owner, 2026-11-26).
    by_a_program = context(alex, session_factory, answers=False)
    by_a_program.raise_outside_text("tool")
    stop = TOOLS["voice_stop_listening"]
    assert (await stop.describe({}, by_a_program.actor, by_a_program)).whose_judged
    # A program's text beside their own words: the judge weighs it (the owner, 2026-11-26).
    beside = context(alex, session_factory)
    beside.raise_outside_text("tool")
    assert (await TOOLS["voice_stop_listening"].describe({}, beside.actor, beside)).whose_judged
    elsewhere = context(alex, session_factory, answers=False, approved_by=dana.id)
    assert await refusal("voice_stop_listening", elsewhere) == "not_possible"
    assert await moments(session_factory) == []


async def test_removing_and_unlearning_are_for_the_person_themself(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await two_voices(session_factory, alex, dana)
    unsure = Speaker(alex.id, "voice", clues=(Clue("voice", "for", "weak", "code", NOW),))
    vault = FakeVault()
    # Someone else's voice at their place: neither tool, whatever they say.
    for speaker in (sure(dana), Speaker(None, "voice")):
        ctx = context(alex, session_factory, vault=vault, speaker=speaker)
        assert await refusal("spoken_phrase_remove", ctx) == "not_possible"
        assert await refusal("voice_unlearn", ctx, {"since_minutes": 30}) == "not_possible"
    assert vault.removed == []
    assert len(await prints_of(session_factory)) == 4
    # A voice the code is not sure of: judged by the barrier (both types are never ordinary),
    # never refused by the tool (case 2).
    from domo_core.policy import barrier
    from domo_core.policy.roles import Actor as TheActor

    ctx = context(alex, session_factory, vault=vault, speaker=unsure)
    ctx.actor = TheActor(alex.id, "owner", standing="unsure")
    for name, arguments in (
        ("spoken_phrase_remove", {}),
        ("voice_unlearn", {"since_minutes": None}),
    ):
        described = await TOOLS[name].describe(arguments, ctx.actor, ctx)
        decision = barrier.decide(ctx.actor, described, DEFAULT_POLICY)
        assert getattr(decision, "judge_identity", False), name
    # A recent print taken back on a voice that merely seems theirs is allowed.
    assert (await call("voice_unlearn", ctx, {"since_minutes": 30})).status == "done"


# --- someone else's phrase removed: the owner, or an adult for a child (the owner, 2026-11-27) ----


async def the_house(session_factory: SessionFactory) -> tuple[Person, Person, Person, Person]:
    """Alex (owner), Dana (adult), Paul (a child Dana invited), Tom (another adult)."""
    alex, dana = await alex_and_dana(session_factory)
    async with session_factory() as writer:
        paul = Person(display_name="Paul", role="child", invited_by=dana.id)
        tom = Person(display_name="Tom", role="adult", invited_by=alex.id)
        writer.add_all([paul, tom])
        await writer.commit()
    for person in (alex, dana, paul, tom):
        await agreed(session_factory, person, "spoken_phrase")
    return alex, dana, paul, tom


async def test_the_owner_may_ask_a_members_phrase_removed_always_judged(session_factory):
    alex, dana, _, _ = await the_house(session_factory)
    ctx = context(alex, session_factory)
    described = await TOOLS["spoken_phrase_remove"].describe({"person": "Dana"}, ctx.actor, ctx)
    # Never done on the code's word alone: the judge weighs it, told whose phrase and the relation.
    assert (described.target, described.whose_judged) == (f"person:{dana.id}", True)
    # Only a verdict of the judge lets it go, kept when the request is stored.
    assert described.verdict_needed
    assert DescribedAction.from_json(described.as_json()) == described
    assert dict(described.facts) == {
        "person": f"person:{dana.id}",
        "person_role": "adult",
        "is_self": False,
        "invited_them": False,
    }
    from domo_core.policy import barrier

    decision = barrier.decide(ctx.actor, described, DEFAULT_POLICY)
    assert getattr(decision, "judge_identity", False)
    # Their own, as before: nothing to weigh with a sure speaker answering them.
    own = await TOOLS["spoken_phrase_remove"].describe({}, ctx.actor, ctx)
    assert (own.target, own.whose_judged, own.verdict_needed) == (f"person:{alex.id}", False, False)
    assert own.facts["is_self"] is True
    named_self = await TOOLS["spoken_phrase_remove"].describe({"person": "Alex"}, ctx.actor, ctx)
    assert named_self == own


async def test_a_removed_phrase_of_someone_else_goes_everywhere_and_they_are_told(session_factory):
    alex, dana, _, _ = await the_house(session_factory)
    vault = FakeVault()
    told: list[uuid.UUID] = []
    ctx = replace(context(alex, session_factory, vault=vault), notify=told.append)
    outcome = await call("spoken_phrase_remove", ctx, {"person": "Dana"})
    assert outcome.status == "done"
    assert outcome.model_result == {"had_a_phrase": True, "had_a_means": True, "word_left": True}
    assert outcome.card == {"key": "action.card.phrase_removed_theirs", "params": {}}
    # At the gatekeeper and as a means, Dana's alone.
    assert vault.removed == [phrase_ref(dana.id)]
    [hers] = await means(session_factory, dana, "spoken_phrase")
    assert hers.removed_at == NOW
    [his] = await means(session_factory, alex, "spoken_phrase")
    assert his.removed_at is None
    # Dana is told by a word of his own, in his words, never silently.
    [word] = await words_left(session_factory, dana)
    assert (word.by_domo, word.author_id) == (True, None)
    assert word.text and "Alex" not in word.text
    assert told == [dana.id]


class NoPhraseVault(FakeVault):
    """A vault that held no phrase for the person."""

    async def phrase_remove(self, ref: str) -> bool:
        await super().phrase_remove(ref)
        return False


async def test_with_no_word_left_the_card_never_says_they_will_be_told(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    ctx = context(alex, session_factory, vault=NoPhraseVault())
    outcome = await call("spoken_phrase_remove", ctx, {"person": "Dana"})
    assert outcome.status == "done"
    assert outcome.model_result == {"had_a_phrase": False, "had_a_means": False, "word_left": False}
    assert outcome.card == {"key": "action.card.phrase_removed_theirs_untold", "params": {}}
    assert await words_left(session_factory, dana) == []


async def test_an_adult_may_ask_their_childs_phrase_removed(session_factory):
    _, dana, paul, tom = await the_house(session_factory)
    vault = FakeVault()
    ctx = context(dana, session_factory, vault=vault)
    described = await TOOLS["spoken_phrase_remove"].describe({"person": "Paul"}, ctx.actor, ctx)
    assert described.whose_judged
    assert dict(described.facts) == {
        "person": f"person:{paul.id}",
        "person_role": "child",
        "is_self": False,
        "invited_them": True,
    }
    # An adult who did not invite the child has the same say (an adult agrees for a child); the
    # judge reads that they did not invite them.
    by_tom = context(tom, session_factory)
    theirs = await TOOLS["spoken_phrase_remove"].describe({"person": "Paul"}, by_tom.actor, by_tom)
    assert theirs.facts["invited_them"] is False
    outcome = await call("spoken_phrase_remove", ctx, {"person": "Paul"})
    assert outcome.status == "done"
    assert vault.removed == [phrase_ref(paul.id)]
    assert len(await words_left(session_factory, paul)) == 1


async def test_an_adult_may_ask_the_phrase_of_a_record_marked_as_a_child_removed(session_factory):
    # Paul's record is marked as a child, his role an adult's (the merge of "Domo makes the
    # people"): an adult has the same say as for a child of the child role.
    _, dana, paul, _ = await the_house(session_factory)
    async with session_factory() as writer:
        row = await writer.get(Person, paul.id)
        assert row is not None
        row.role, row.is_child = "adult", True
        await writer.commit()
    vault = FakeVault()
    ctx = context(dana, session_factory, vault=vault)
    outcome = await call("spoken_phrase_remove", ctx, {"person": "Paul"})
    assert outcome.status == "done"
    assert vault.removed == [phrase_ref(paul.id)]


async def test_an_unsure_adult_asking_a_childs_phrase_removed_is_judged_not_refused(
    session_factory,
):
    # The voice's rule: who asks is weighed by the barrier and the judge, never refused by the
    # tool for a doubt on who speaks.
    _, dana, paul, _ = await the_house(session_factory)
    unsure = Speaker(dana.id, "voice", clues=(Clue("voice", "for", "weak", "code", NOW),))
    ctx = context(dana, session_factory, speaker=unsure)
    ctx.actor = replace(ctx.actor, standing="unsure")
    described = await TOOLS["spoken_phrase_remove"].describe({"person": "Paul"}, ctx.actor, ctx)
    assert (described.target, described.whose_judged) == (f"person:{paul.id}", True)


async def test_nobody_else_may_ask_another_persons_phrase_removed(session_factory):
    alex, dana, paul, tom = await the_house(session_factory)
    async with session_factory() as writer:
        guest = Person(display_name="Sam", role="guest", invited_by=alex.id)
        writer.add(guest)
        await writer.commit()
    vault = FakeVault()
    for asker, whose in (
        (dana, "Tom"),  # an adult, for another adult
        (dana, "Alex"),  # an adult, for the owner
        (paul, "Dana"),  # a child, for an adult
        (guest, "Paul"),  # a guest, for a child
        (tom, "Dana"),
    ):
        ctx = context(asker, session_factory, vault=vault)
        assert await refusal("spoken_phrase_remove", ctx, {"person": whose}) == "not_possible"
    assert vault.removed == []
    for person in (alex, dana, paul, tom):
        assert len(await means(session_factory, person, "spoken_phrase")) == 1


async def test_agreeing_or_changing_a_phrase_stays_the_holders_alone(session_factory):
    # A phrase is a proof: only its holder sets it. Nobody names someone else's, the owner
    # neither: the tools take no argument.
    alex, _, _, _ = await the_house(session_factory)
    ctx = context(alex, session_factory)
    for name in ("spoken_phrase_agree", "spoken_phrase_change"):
        with pytest.raises(InvalidArguments) as error:
            await TOOLS[name].describe({"person": "Dana"}, ctx.actor, ctx)
        assert error.value.code == "invalid"
        assert "person" not in TOOLS[name].definition.parameters["properties"]
    assert await moments(session_factory) == []


async def test_someone_elses_phrase_is_never_his_idea_nor_a_programs_text(session_factory):
    alex, dana, _, _ = await the_house(session_factory)
    assert await his_refusal(
        "spoken_phrase_remove", context(alex, session_factory), {"person": "Dana"}
    ) == ("not_possible")
    # A program's text alone: weighed by the judge, whose verdict alone lets it go.
    by_a_program = context(alex, session_factory, answers=False)
    by_a_program.raise_outside_text("program")
    described = await TOOLS["spoken_phrase_remove"].describe(
        {"person": "Dana"}, by_a_program.actor, by_a_program
    )
    assert (described.whose_judged, described.verdict_needed) == (True, True)
    # Someone else speaking at Alex's page does not have Alex's say.
    at_his_page = context(alex, session_factory, speaker=sure(dana))
    assert await refusal("spoken_phrase_remove", at_his_page, {"person": "Dana"}) == (
        "not_possible"
    )
    assert len(await means(session_factory, dana, "spoken_phrase")) == 1


async def test_a_waited_removal_removes_only_the_phrase_it_was_described_for(session_factory):
    alex, dana, paul, _ = await the_house(session_factory)
    vault = FakeVault()
    ctx = context(alex, session_factory, vault=vault)
    ctx.described_action = await TOOLS["spoken_phrase_remove"].describe(
        {"person": "Dana"}, ctx.actor, ctx
    )
    with pytest.raises(InvalidArguments) as error:
        await TOOLS["spoken_phrase_remove"].execute({"person": "Paul"}, ctx)
    assert error.value.code == "not_found"
    assert vault.removed == []
    assert len(await means(session_factory, paul, "spoken_phrase")) == 1


async def test_through_a_turn_the_owners_removal_of_danas_phrase_is_judged(
    session_factory, monkeypatch
):
    import json

    from domo_core.confirmations.judge import Judgement, fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall

    alex, dana, _, _ = await the_house(session_factory)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    call_it = ToolCall("r", "spoken_phrase_remove", json.dumps({"person": "Dana"}))
    # Silent (no model): "the assistant judges it" (the owner, 2026-11-27): with no judgement
    # nothing is removed, and no proof of Alex stands in for one; he is told it cannot be decided
    # now and may ask again later.
    cases = _judge(monkeypatch, fallback("no_model", they_asked=True))
    vault = FakeVault()
    ctx = replace(
        context(alex, session_factory, vault=vault),
        conversation_id=conversation_id,
        domo_message_id=reply_id,
    )
    record = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert record.status == "refused", record.model_result
    assert record.model_result["reason"] == "not_weighed"
    assert record.card is not None and record.card["key"] == "action.card.refused.not_weighed"
    assert vault.removed == []
    async with session_factory() as reader:
        assert list(await reader.scalars(select(Confirmation))) == []
    # A judge that answers and asks a proof itself: that verdict stands, Alex is asked one.
    _judge(monkeypatch, Judgement("proof", "say it is you"))
    record = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    assert vault.removed == []
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert (request.ask, request.asked_by) == ("proof", "judge")
    # The judge was told whose phrase, how the asker stands to them, and who speaks.
    told = cases[0].as_json()
    assert told["action"]["target"] == f"person:{dana.id}"
    assert told["action"]["facts"]["person_role"] == "adult"
    assert told["action"]["facts"]["is_self"] is False
    assert told["person"]["role"] == "owner"
    assert "speaker" in told
    # The judge refuses: nothing removed.
    _judge(monkeypatch, Judgement("do_not", "she may not know"))
    refused = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert refused.status == "refused"
    assert vault.removed == []
    # The judge agrees: removed, at the gatekeeper too, and Dana is told.
    _judge(monkeypatch, Judgement("do"))
    done = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert done.status == "done", done.model_result
    assert vault.removed == [phrase_ref(dana.id)]
    assert len(await words_left(session_factory, dana)) == 1


async def test_a_silent_judge_never_lets_someone_elses_phrase_go_in_a_chain_either(
    session_factory, monkeypatch
):
    import json

    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall

    alex, _, _, _ = await the_house(session_factory)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    steps = [{"tool": "spoken_phrase_remove", "arguments": {"person": "Dana"}}]
    call_it = ToolCall("r", "do_in_order", json.dumps({"steps": steps}))
    _judge(monkeypatch, fallback("error", they_asked=True))
    vault = FakeVault()
    ctx = replace(
        context(alex, session_factory, vault=vault),
        conversation_id=conversation_id,
        domo_message_id=reply_id,
    )
    record = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert record.model_result["reason"] == "not_weighed", record.model_result
    assert vault.removed == []
    async with session_factory() as reader:
        assert list(await reader.scalars(select(Confirmation))) == []


async def test_a_chain_step_waiting_for_an_approver_never_removes_a_phrase_on_a_silent_judge(
    session_factory, monkeypatch, grant
):
    # A chain whose step removes someone else's phrase and must wait for an approver's yes (the
    # owner's rule on Dana's removals): with the judge silent it is never stored to wait, so no
    # yes can ever let it go. Nothing removed, nothing asked.
    import json

    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall

    _, dana, paul, _ = await the_house(session_factory)
    await grant("proof.phrase_remove", person=dana, access="allow", approver="owner")
    conversation_id, reply_id = await _alex_s_reply(session_factory, dana)
    steps = [{"tool": "spoken_phrase_remove", "arguments": {"person": "Paul"}}]
    call_it = ToolCall("r", "do_in_order", json.dumps({"steps": steps}))
    vault = FakeVault()
    ctx = replace(
        context(dana, session_factory, vault=vault),
        conversation_id=conversation_id,
        domo_message_id=reply_id,
    )
    # The step waits for the owner's yes when the judge answers (the rule is read).
    from domo_core.confirmations.judge import Judgement

    _judge(monkeypatch, Judgement("do"))
    waits = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert waits.status == "waiting", waits.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert request.approver_kind == "owner"
    # Silent: nothing is stored to wait for that yes.
    _judge(monkeypatch, fallback("no_model", they_asked=True))
    record = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert record.model_result["reason"] == "not_weighed", record.model_result
    assert vault.removed == []
    async with session_factory() as reader:
        assert [c.id for c in await reader.scalars(select(Confirmation))] == [request.id]
    assert len(await means(session_factory, paul, "spoken_phrase")) == 1


async def test_a_chain_step_checked_in_its_turn_never_removes_someone_elses_phrase(
    session_factory,
):
    # A step left to be checked in its turn (after a wait for an arrival, its person did not
    # exist yet) was never shown to the judge as someone else's phrase: when it turns out to be
    # one as it runs, no verdict was given to it, so nothing is removed and nothing is asked.
    from domo_core.confirmations.chain import run_steps
    from domo_core.confirmations.steps import PlannedStep

    alex, dana, _, _ = await the_house(session_factory)
    vault = FakeVault()
    ctx = context(alex, session_factory, vault=vault)
    step = PlannedStep(
        "spoken_phrase_remove", "proof.phrase_remove", {"person": "Dana"}, action=None
    )
    run = await run_steps(ctx, [step], basis=None, request_id=None, decider=None)
    assert run.status == "failed"
    assert run.failed is not None and run.failed.reason == "not_weighed"
    assert vault.removed == []
    assert len(await means(session_factory, dana, "spoken_phrase")) == 1
    # Their own phrase, checked in its turn, needs no verdict: removed as before.
    own = PlannedStep("spoken_phrase_remove", "proof.phrase_remove", {}, action=None)
    run = await run_steps(ctx, [own], basis=None, request_id=None, decider=None)
    assert run.status == "done", run.failed
    assert vault.removed == [phrase_ref(alex.id)]


async def test_a_silent_judge_on_their_own_phrase_still_asks_them_a_proof(
    session_factory, monkeypatch
):
    # Their own phrase, weighed because someone else's words were read in the turn: the proof
    # asked is of the phrase's own holder, so a silent judge asks it as before.
    import json

    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall

    alex, _, _, _ = await the_house(session_factory)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    _judge(monkeypatch, fallback("no_model", they_asked=True))
    vault = FakeVault()
    ctx = replace(
        context(alex, session_factory, vault=vault, told_outside=True),
        conversation_id=conversation_id,
        domo_message_id=reply_id,
    )
    call_it = ToolCall("r", "spoken_phrase_remove", json.dumps({}))
    record = await handle_tool_call(call_it, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert request.ask == "proof"
    assert vault.removed == []


# --- "that was not me" ----------------------------------------------------------------------------


async def two_voices(session_factory, alex: Person, dana: Person) -> None:
    async with session_factory() as writer:
        await learnt_voice(writer, alex, ALEX, microphone=MIC)
        await learnt_voice(writer, dana, DANA, microphone=MIC)
        await writer.commit()


async def prints_of(session_factory) -> list[tuple[str, str]]:
    async with session_factory() as reader:
        rows = await reader.execute(
            select(VoiceRef.person_id, VoicePrint.kind).join(
                VoiceRef, VoiceRef.id == VoicePrint.ref_id
            )
        )
        return sorted((str(person), kind) for person, kind in rows.all())


async def test_unlearn_removes_recent_prints_of_the_person_alone(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    ctx = context(alex, session_factory)
    outcome = await call("voice_unlearn", ctx, {"since_minutes": 30})
    assert outcome.status == "done"
    assert outcome.card == {"key": "action.card.voice_unlearnt", "params": {}}
    assert outcome.model_result == {"removed": 1, "since_minutes": 30}
    left = await prints_of(session_factory)
    # Alex's recent print went (it moved after the time); his base, made before, and all of
    # Dana's, stay.
    assert (str(alex.id), "recent") not in left
    assert (str(alex.id), "base") in left
    assert (str(dana.id), "base") in left and (str(dana.id), "recent") in left


async def test_unlearn_with_no_time_forgets_the_voice_entirely(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    outcome = await call("voice_unlearn", context(alex, session_factory), {"since_minutes": None})
    assert outcome.model_result == {"removed": 2, "since_minutes": None}
    assert {person for person, _ in await prints_of(session_factory)} == {str(dana.id)}


async def test_unlearn_is_the_persons_own_and_checks_its_argument(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    # A program's text alone, or beside their words: judged.
    by_a_program = context(alex, session_factory, answers=False)
    by_a_program.raise_outside_text("program")
    beside = context(alex, session_factory)
    beside.raise_outside_text("program")
    whole = {"since_minutes": None}
    for ctx in (by_a_program, beside):
        assert (await TOOLS["voice_unlearn"].describe(whole, ctx.actor, ctx)).whose_judged
    ctx = context(alex, session_factory)
    tool = TOOLS["voice_unlearn"]
    wrong = (
        {},
        {"since_minutes": 0},
        {"since_minutes": -5},
        {"since_minutes": "10"},
        {"since_minutes": True},
        {"since_minutes": 10**9},
        {"since_minutes": None, "colour": "blue"},
        {"since_minutes": None, "person": ""},
        {"since_minutes": None, "person": 3},
        {"since_minutes": None, "voice": "Dana"},
        {"since_minutes": None, "person": "Dana", "voice": UNKNOWN},
        # An unknown voice is erased whole.
        {"since_minutes": 5, "voice": UNKNOWN},
    )
    for bad in wrong:
        with pytest.raises(InvalidArguments) as error:
            await tool.describe(bad, ctx.actor, ctx)
        assert error.value.code == "invalid", bad
    # A voice nobody knows is never forgotten on a person's word.
    assert await refusal("voice_unlearn", ctx, {"since_minutes": None, "voice": UNKNOWN}) == (
        "not_possible"
    )
    # Someone else's: weighed by the judge (case 4), never refused here.
    described = await tool.describe({"since_minutes": 5, "person": "Dana"}, ctx.actor, ctx)
    assert (described.target, described.whose_judged) == (f"person:{dana.id}", True)
    assert len(await prints_of(session_factory)) == 4
    # Naming themself is the same as naming nobody.
    outcome = await call("voice_unlearn", ctx, {"since_minutes": 30, "person": "Alex"})
    assert outcome.model_result == {"removed": 1, "since_minutes": 30}


# --- prints are the assistant's recordings: he may erase them as his own idea --------------------

UNKNOWN = "v-0123456789abcdef"


async def his_call(name: str, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutcome:
    """Describe then execute a call judged as the assistant's own idea, as a turn does."""
    tool = TOOLS[name]
    his = domo_actor()
    action = await tool.describe(arguments, his, ctx)
    assert action.action_type == tool.action_type
    ctx.judged_as = his
    try:
        return await tool.execute(arguments, ctx)
    finally:
        ctx.judged_as = None


async def his_refusal(name: str, ctx: ToolContext, arguments: dict[str, Any]) -> str:
    with pytest.raises(InvalidArguments) as error:
        await TOOLS[name].describe(arguments, domo_actor(), ctx)
    return error.value.code


def nobody_spoke(person: Person, session_factory: SessionFactory, **kwargs: Any) -> ToolContext:
    """A turn of his own at ``person``'s (a wake, the upkeep): no message, nobody spoke."""
    return context(
        person,
        session_factory,
        answers=False,
        speaker=Speaker(None, "voice", nobody_spoke=True),
        **kwargs,
    )


async def words_left(session_factory: SessionFactory, person: Person) -> list[Note]:
    async with session_factory() as reader:
        found = await reader.scalars(select(Note).where(Note.recipient_id == person.id))
        return list(found)


async def test_his_idea_on_a_known_voice_where_someone_spoke_is_judged(session_factory):
    # Case 4 of the final wave: where someone spoke, or a member's words came in, the judge
    # weighs his idea from the context; never refused by the code.
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    whole = {"since_minutes": None, "person": "Alex"}
    unsure = Speaker(alex.id, "voice", clues=(Clue("voice", "for", "weak", "code", NOW),))
    for ctx in (
        context(alex, session_factory, speaker=unsure),
        context(alex, session_factory, speaker=unsure, answers=False),
        context(alex, session_factory),
        nobody_spoke(alex, session_factory, told_outside=True),
    ):
        described = await TOOLS["voice_unlearn"].describe(whole, domo_actor(), ctx)
        assert described.whose_judged
    # A program's text alone, with nobody of the house in the turn: judged too.
    by_a_program = nobody_spoke(alex, session_factory)
    by_a_program.raise_outside_text()
    assert (await TOOLS["voice_unlearn"].describe(whole, domo_actor(), by_a_program)).whose_judged
    # Alone with nobody's words, as before: nothing to weigh.
    alone = await TOOLS["voice_unlearn"].describe(
        whole, domo_actor(), nobody_spoke(alex, session_factory)
    )
    assert not alone.whose_judged
    assert len(await prints_of(session_factory)) == 4
    assert await words_left(session_factory, alex) == []


async def test_a_persons_wish_on_someone_elses_voice_leaves_them_a_word(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    told: list[uuid.UUID] = []
    ctx = replace(context(alex, session_factory), notify=told.append)
    outcome = await call("voice_unlearn", ctx, {"since_minutes": None, "person": "Dana"})
    assert outcome.model_result == {"removed": 2, "since_minutes": None, "word_left": True}
    assert {person for person, _ in await prints_of(session_factory)} == {str(alex.id)}
    assert len(await words_left(session_factory, dana)) == 1
    assert told == [dana.id]


async def test_his_idea_in_a_turn_nobody_spoke_in_erases_and_leaves_them_a_word(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    told: list[uuid.UUID] = []
    ctx = replace(nobody_spoke(alex, session_factory), notify=told.append)
    arguments = {"since_minutes": None, "person": "Dana"}
    action = await TOOLS["voice_unlearn"].describe(arguments, domo_actor(), ctx)
    assert action.target == f"person:{dana.id}"
    outcome = await his_call("voice_unlearn", ctx, arguments)
    assert outcome.status == "done"
    assert outcome.model_result == {"removed": 2, "since_minutes": None, "word_left": True}
    assert {person for person, _ in await prints_of(session_factory)} == {str(alex.id)}
    # Never silently: a word of his own for Dana, in the assistant's words, and news for her.
    [word] = await words_left(session_factory, dana)
    assert (word.by_domo, word.author_id) == (True, None)
    assert word.text and "Dana" not in word.text
    assert told == [dana.id]
    async with session_factory() as reader:
        assert (await reader.get(Person, dana.id)).news_at == NOW  # type: ignore[union-attr]
    # Nothing left to erase: nothing to tell.
    again = await his_call("voice_unlearn", ctx, arguments)
    assert again.model_result == {"removed": 0, "since_minutes": None, "word_left": False}
    assert len(await words_left(session_factory, dana)) == 1


async def test_a_waited_call_erases_only_the_voice_it_was_described_for(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    described = DescribedAction(
        connector="core",
        action_type="voice.unlearn",
        target=f"person:{alex.id}",
        effect="self_profile",
        destination="household",
    )
    ctx = replace(nobody_spoke(alex, session_factory), described_action=described)
    ctx.judged_as = domo_actor()
    with pytest.raises(InvalidArguments) as error:
        await TOOLS["voice_unlearn"].execute({"since_minutes": None, "person": "Dana"}, ctx)
    assert error.value.code == "not_found"
    assert len(await prints_of(session_factory)) == 4


async def test_his_idea_erases_an_unknown_voice_in_any_turn_and_never_a_known_one_by_reference(
    session_factory,
):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    async with session_factory() as writer:
        stranger = await reference_for(
            writer, person_id=None, print_=print_of(TOM), microphone=MIC, now=NOW
        )
        dana_ref = await writer.scalar(
            select(VoiceRef.reference).where(VoiceRef.person_id == dana.id)
        )
        await writer.commit()
    assert dana_ref is not None
    # In Alex's own turn, while he speaks.
    ctx = context(alex, session_factory)
    # A known person's voice is named by the person, never by a reference.
    by_reference = {"since_minutes": None, "voice": dana_ref}
    assert await his_refusal("voice_unlearn", ctx, by_reference) == "not_possible"
    gone = {"since_minutes": None, "voice": UNKNOWN}
    assert await his_refusal("voice_unlearn", ctx, gone) == "not_found"
    action = await TOOLS["voice_unlearn"].describe(
        {"since_minutes": None, "voice": stranger}, domo_actor(), ctx
    )
    assert action.target == f"voice:{stranger}"
    outcome = await his_call("voice_unlearn", ctx, {"since_minutes": None, "voice": stranger})
    assert outcome.model_result == {"removed": 2, "since_minutes": None, "word_left": False}
    async with session_factory() as reader:
        assert await reader.scalar(select(VoiceRef).where(VoiceRef.reference == stranger)) is None
    assert len(await prints_of(session_factory)) == 4


async def test_words_heard_during_a_turn_of_his_own_close_the_gate(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    ctx = nobody_spoke(alex, session_factory)
    # Something was said in the conversation while his turn ran (read between two rounds): the
    # mark is shared by every copy of the context.
    replace(ctx).heard_meanwhile.raised = True
    whole = {"since_minutes": None, "person": "Dana"}
    # Weighed by the judge from then on (case 4 of the final wave), never refused.
    assert (await TOOLS["voice_unlearn"].describe(whole, domo_actor(), ctx)).whose_judged
    assert len(await prints_of(session_factory)) == 4


async def _a_due_plan(session_factory: SessionFactory, person: Person, *, his: bool) -> DuePlan:
    from domo_core.plans.models import Plan

    async with session_factory() as writer:
        plan = Plan(person_id=person.id, why="tonight", due_at=NOW, made_by_domo=his)
        writer.add(plan)
        await writer.commit()
    return DuePlan(id=str(plan.id), why="tonight", their_words=None, late_minutes=None)


async def test_only_a_plan_of_his_own_counts_as_nobody_speaking(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    whole = {"since_minutes": None, "person": "Dana"}
    # Tom said "tonight, forget Dana's voice" and a plan was set with no words kept: still a
    # person's plan.
    theirs = await _a_due_plan(session_factory, alex, his=False)
    ctx = replace(nobody_spoke(alex, session_factory), due_plans=(theirs,))
    # Weighed by the judge (case 4 of the final wave), never refused.
    assert (await TOOLS["voice_unlearn"].describe(whole, domo_actor(), ctx)).whose_judged
    assert len(await prints_of(session_factory)) == 4
    his = await _a_due_plan(session_factory, alex, his=True)
    ctx = replace(nobody_spoke(alex, session_factory), due_plans=(his,))
    assert (await his_call("voice_unlearn", ctx, whole)).status == "done"
    # One of theirs among his own is enough to have the judge weigh it.
    ctx = replace(nobody_spoke(alex, session_factory), due_plans=(his, theirs))
    alex_whole = {"since_minutes": None, "person": "Alex"}
    assert (await TOOLS["voice_unlearn"].describe(alex_whole, domo_actor(), ctx)).whose_judged


async def test_his_idea_names_whose_voice(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    ctx = nobody_spoke(alex, session_factory)
    assert await his_refusal("voice_unlearn", ctx, {"since_minutes": None}) == "invalid"
    nobody = {"since_minutes": 5, "person": "Nobody"}
    assert await his_refusal("voice_unlearn", ctx, nobody) == "not_found"


async def test_the_spoken_phrase_is_never_an_idea_of_his(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    ctx = context(alex, session_factory)
    for name in PHRASE_TOOLS:
        assert await his_refusal(name, ctx, {}) == "not_possible", name


# --- how someone speaks: the assistant's own reading (Task 23) ----------------------------------


def heard(person: Person, *, against: bool = False) -> Speaker:
    """A sentence heard on ``MIC`` at the person's page: sure by the page, or a voice against."""
    clues = [Clue("session", "for", "strong", "code", NOW)]
    if against:
        clues.append(Clue("voice", "against", "weak", "code", NOW, degree=0.45))
    return Speaker(person.id, "voice", clues=tuple(clues), microphone=MIC)


async def manner_kept(session_factory: SessionFactory, person: Person) -> tuple[Clue, ...]:
    async with session_factory() as reader:
        return await kept_clues(
            reader, person_id=person.id, channel="voice", device_id=None, now=NOW, microphone=MIC
        )


async def test_a_manner_clue_is_domo_s_and_never_changes_the_standing(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    note = {"weighs": "against", "why": "not the way they usually put it"}
    done = await call(
        "speaker_manner_note", context(alex, session_factory, speaker=heard(alex)), note
    )
    assert done.status == "done"
    [kept] = await manner_kept(session_factory, alex)
    assert (kept.kind, kept.weighs, kept.strength, kept.source) == (
        "manner",
        "against",
        "weak",
        "domo",
    )
    # Never shown, never judged: it is his own reading.
    assert is_silent("voice.manner_note")
    # His reading against a sure speaker leaves them sure; his reading for an unsure one, unsure.
    sure_with = Speaker(alex.id, "voice", clues=(*heard(alex).clues, kept), microphone=MIC)
    assert standing(sure_with) == "sure"
    doubted = heard(alex, against=True)
    for_them = replace(kept, weighs="for")
    assert standing(replace(doubted, clues=(*doubted.clues, for_them))) == "unsure"
    # It lives as long as a proof, on this microphone alone.
    async with session_factory() as reader:
        later = await kept_clues(
            reader,
            person_id=alex.id,
            channel="voice",
            device_id=None,
            now=NOW + PROOF_CLUE_FOR + timedelta(seconds=1),
            microphone=MIC,
        )
        elsewhere = await kept_clues(
            reader, person_id=alex.id, channel="voice", device_id=None, now=NOW, microphone="hall"
        )
    assert later == () and elsewhere == ()


async def test_a_manner_clue_reaches_the_judge_on_the_next_utterance(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    note = {"weighs": "for", "why": "their usual words"}
    await call("speaker_manner_note", context(alex, session_factory, speaker=heard(alex)), note)
    # The next sentence, even one whose voice weighs against them: the turn adds what is kept
    # there as ``prepare`` does (``kept_clues`` then ``heeded_proofs``), and the judge reads his
    # reading beside the code's clues (a proof would be dropped, his reading is not).
    doubted = heard(alex, against=True)
    kept = await manner_kept(session_factory, alex)
    speaker = replace(doubted, clues=(*doubted.clues, *heeded_proofs(doubted.clues, kept)))
    seen = speaker.as_json(NOW)
    assert {
        "kind": "manner",
        "weighs": "for",
        "strength": "weak",
        "source": "domo",
        "minutes_ago": 0,
    } in (seen["clues"])
    assert standing(speaker) == "unsure"
    assert MIC not in str(seen)


async def test_a_manner_note_needs_someone_who_spoke(session_factory):
    alex, _ = await alex_and_dana(session_factory)
    note = {"weighs": "for", "why": "their usual words"}
    nobody = Speaker(None, "voice", microphone=MIC)
    assert await refusal(
        "speaker_manner_note", context(alex, session_factory, speaker=nobody), note
    ) == ("not_possible")
    woke = Speaker(alex.id, "wake", nobody_spoke=True)
    assert await refusal(
        "speaker_manner_note", context(alex, session_factory, speaker=woke), note
    ) == ("not_possible")
    ctx = context(alex, session_factory, speaker=heard(alex))
    for wrong in (
        {"weighs": "maybe", "why": "x"},
        {"weighs": "for", "why": ""},
        {"weighs": "for"},
        {"weighs": "for", "why": "x" * 201},
        {"weighs": "for", "why": "x", "who": "Dana"},
    ):
        assert await refusal("speaker_manner_note", ctx, wrong) == "invalid", wrong
    assert await manner_kept(session_factory, alex) == ()


async def test_a_manner_note_is_refused_after_outside_text(session_factory):
    # Words nobody of the house said in the turn (a page, a note, a tool's result) never plant
    # his reading of who speaks, as for a trust fact (review of Task 23, finding 1).
    alex, _ = await alex_and_dana(session_factory)
    note = {"weighs": "for", "why": "the page says it is them"}
    told = context(alex, session_factory, speaker=heard(alex, against=True), told_outside=True)
    assert await refusal("speaker_manner_note", told, note) == "not_possible"
    assert await manner_kept(session_factory, alex) == ()
    description = TOOLS["speaker_manner_note"].definition.description
    assert "never note for because they say who they are" in description


# --- what the model reads -------------------------------------------------------------------------


def test_the_tools_are_declared_in_the_house_form():
    expected = {
        "spoken_phrase_agree": "proof.phrase_agree",
        "spoken_phrase_change": "proof.phrase_change",
        "spoken_phrase_remove": "proof.phrase_remove",
        "voice_stop_listening": "voice.stop_listening",
        "voice_unlearn": "voice.unlearn",
        "speaker_manner_note": "voice.manner_note",
    }
    for name, action_type in expected.items():
        tool = TOOLS[name]
        assert tool.definition.name == name
        assert tool.action_type == action_type


def test_the_descriptions_carry_what_the_assistant_must_know():
    agree_text = TOOLS["spoken_phrase_agree"].definition.description.lower()
    assert "heard by" in agree_text and "room" in agree_text
    assert "once" in agree_text
    assert "another proof" in agree_text
    assert "never" in agree_text
    unlearn = TOOLS["voice_unlearn"].definition.description.lower()
    assert "since" in unlearn and "again" in unlearn


def test_the_unlearning_says_it_is_final_and_open_to_his_own_idea():
    unlearn = TOOLS["voice_unlearn"].definition.description.lower()
    assert "cannot be undone" in unlearn
    assert "your own idea" in unlearn and "someone spoke" in unlearn
    assert "never because someone" in unlearn
    # The person it names is named by their whole name before the call is described.
    tool = TOOLS["voice_unlearn"]
    assert isinstance(tool, NamesPeople)
    assert tool.person_arguments == ("person",)


def test_the_subjects_of_the_voice_name_the_person_never_a_word():
    alex, dana = uuid.uuid4(), uuid.uuid4()

    def described(action_type: str, target: str | None) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type=action_type,
            target=target,
            effect="self_profile",
            destination="household",
        )

    for action_type in PHRASE_TYPES:
        given = described(action_type, f"person:{alex}")
        assert subject_of(given, {"had_a_phrase": True}, alex) == Subject(f"phrase:{alex}", alex)
        # What is not a person's id gives no key: never a word that was said.
        for wrong in ("person:open sesame", "open sesame", None):
            assert subject_of(described(action_type, wrong), None, alex).key is None, wrong
    # What was learnt of a voice: the key names whose, and the person the target names owns it,
    # not the person of the turn.
    theirs = described("voice.unlearn", f"person:{dana}")
    assert subject_of(theirs, {"removed": 2}, alex) == Subject(f"voice:{dana}", dana)
    unknown = described("voice.unlearn", f"voice:{UNKNOWN}")
    assert subject_of(unknown, None, alex) == Subject(f"voice:{UNKNOWN}", "house")
    for wrong in ("person:open sesame", "voice:open sesame", "open sesame", None):
        found = subject_of(described("voice.unlearn", wrong), None, alex)
        assert found.key is None, wrong


def test_no_description_names_the_owner_or_a_product():
    banned = ("owner", "domo", "jarvis", "alex", "whisper", "piper", "silero", "sonos", "google")
    for name in ALL_TOOLS:
        definition = TOOLS[name].definition
        text = (definition.description + str(definition.parameters)).lower()
        for word in banned:
            assert word not in text, (name, word)


# --- his idea through a turn ----------------------------------------------------------------------


async def _alex_s_reply(
    session_factory: SessionFactory, alex: Person
) -> tuple[uuid.UUID, uuid.UUID]:
    """A conversation of Alex's with a reply of the assistant in it."""
    from domo_core.chat.models import Conversation, Message

    async with session_factory() as writer:
        conversation = Conversation(person_id=alex.id)
        writer.add(conversation)
        await writer.flush()
        reply = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="complete",
            provenance="model",
        )
        writer.add(reply)
        await writer.commit()
        return conversation.id, reply.id


def _judge(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> list[Any]:
    """The judge of the test: it answers ``answers`` in turn (the last one again); its cases."""
    from domo_core.conversation import execution as execution_module

    cases: list[Any] = []

    async def judge(_model: object, case: Any, **_: object) -> Any:
        cases.append(case)
        return answers[min(len(cases), len(answers)) - 1]

    monkeypatch.setattr(execution_module, "judge", judge)
    return cases


def _erase(arguments: dict[str, Any]) -> Any:
    import json

    from domo_core.llm.types import ToolCall

    return ToolCall("u", "voice_unlearn", json.dumps(arguments))


def _mark(reason: str) -> Any:
    import json

    from domo_core.llm.types import ToolCall

    return ToolCall("m", "act_on_my_own", json.dumps({"reason": reason}))


def _his_own_turn(ctx: ToolContext) -> ToolContext:
    """A turn he acts in on his own at Alex's (a wake): nobody spoke, nothing from outside."""
    from domo_core.policy.catalog import on_his_own_tables

    return replace(
        ctx,
        on_his_own=True,
        reply_message_id=None,
        speaker=None,
        policy=on_his_own_tables(DEFAULT_POLICY),
    )


async def test_through_a_turn_tom_cannot_push_his_idea_onto_alex_s_voice(
    session_factory, monkeypatch
):
    from domo_core.confirmations.judge import Judgement
    from domo_core.conversation.execution import handle_tool_call

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    cases = _judge(monkeypatch, Judgement("do_not"))
    unsure = Speaker(alex.id, "voice", clues=(Clue("voice", "for", "weak", "code", NOW),))
    ctx = replace(context(alex, session_factory, speaker=unsure), conversation_id=conversation_id)
    mark = _mark("they say I got their voice wrong")
    assert (await handle_tool_call(mark, ctx=ctx, reply_id=reply_id)).status == "done"
    record = await handle_tool_call(
        _erase({"since_minutes": None, "person": "Alex"}), ctx=ctx, reply_id=reply_id
    )
    # Weighed by the judge (case 4 of the final wave), who sees it is not Alex's wish: nothing
    # is erased.
    assert record.status == "refused"
    assert len(cases) == 1
    assert len(await prints_of(session_factory)) == 4


async def test_through_a_wake_his_idea_is_judged_done_his_and_told(session_factory, monkeypatch):
    from domo_core.chat.models import MessageAction
    from domo_core.confirmations.judge import Judgement
    from domo_core.conversation.execution import handle_tool_call

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    cases = _judge(monkeypatch, Judgement("do"))
    ctx = _his_own_turn(replace(context(alex, session_factory), conversation_id=conversation_id))
    record = await handle_tool_call(
        _erase({"since_minutes": None, "person": "Dana"}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "done", record.model_result
    assert len(cases) == 1
    assert {person for person, _ in await prints_of(session_factory)} == {str(alex.id)}
    [word] = await words_left(session_factory, dana)
    assert word.by_domo
    async with session_factory() as reader:
        stored = await reader.scalar(
            select(MessageAction).where(
                MessageAction.message_id == reply_id, MessageAction.action_type == "voice.unlearn"
            )
        )
    assert stored is not None
    assert (stored.status, stored.by_domo) == ("done", True)


async def test_through_a_wake_a_silent_judge_on_his_idea_does_nothing(session_factory, monkeypatch):
    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from sqlalchemy import func

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    cases = _judge(monkeypatch, fallback("no_model", they_asked=False))
    ctx = _his_own_turn(replace(context(alex, session_factory), conversation_id=conversation_id))
    record = await handle_tool_call(
        _erase({"since_minutes": None, "person": "Dana"}), ctx=ctx, reply_id=reply_id
    )
    assert len(cases) == 2
    assert record.model_result["reason"] == "judge_silent"
    assert len(await prints_of(session_factory)) == 4
    assert await words_left(session_factory, dana) == []
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Confirmation)) == 0


async def test_through_a_wake_a_warn_asks_the_person_whose_voice_it_is(
    session_factory, monkeypatch
):
    from domo_core.confirmations.judge import Judgement
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    _judge(monkeypatch, Judgement("warn", "it cannot be undone and she may not know"))
    ctx = _his_own_turn(replace(context(alex, session_factory), conversation_id=conversation_id))
    record = await handle_tool_call(
        _erase({"since_minutes": None, "person": "Dana"}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting", record.model_result
    assert len(await prints_of(session_factory)) == 4
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    # Dana answers for her voice, not Alex in whose conversation the wake ran.
    assert (request.concerned_id, request.by_domo) == (dana.id, True)


@pytest.mark.parametrize("spoken", [True, False], ids=["spoken", "typed"])
async def test_dana_s_yes_in_words_to_his_warned_idea_erases_her_voice(
    session_factory, monkeypatch, spoken
):
    from domo_core.confirmations.judge import Judgement
    from domo_core.confirmations.models import Confirmation
    from domo_core.confirmations.service import decide_confirmation, requester_context
    from domo_core.conversation.execution import handle_tool_call

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    _judge(monkeypatch, Judgement("warn", "it cannot be undone and she may not know"))
    ctx = _his_own_turn(replace(context(alex, session_factory), conversation_id=conversation_id))
    record = await handle_tool_call(
        _erase({"since_minutes": None, "person": "Dana"}), ctx=ctx, reply_id=reply_id
    )
    assert record.status == "waiting", record.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    # Dana's own turn, where she says or types "yes, go ahead".
    _, her_reply = await _alex_s_reply(session_factory, dana)
    hers = context(dana, session_factory, speaker=sure(dana) if spoken else None)
    if not spoken:
        hers = replace(hers, speaker=None)
    outcome, ran = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=request.id,
        decider=dana,
        approve=True,
        via="phrase",
        reply_message_id=her_reply,
        ctx_factory=requester_context(hers),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        now=NOW,
    )
    assert outcome == "executed", ran
    assert {person for person, _ in await prints_of(session_factory)} == {str(alex.id)}
    [word] = await words_left(session_factory, dana)
    assert word.by_domo


# --- ruling R: what his erasure buys is taken away ------------------------------------------------


async def _marked(
    session_factory: SessionFactory, person: Person, vector: tuple[float, ...]
) -> bool:
    """Whether ``person``'s voice reads "to be learnt again", in a session of its own (what the
    database holds: a restart keeps it)."""
    from domo_core.channels.voice.prints import read_voice
    from voice_support import THRESHOLDS

    async with session_factory() as reader:
        reading = await read_voice(
            reader,
            person_id=person.id,
            microphone=MIC,
            print_=print_of(vector),
            thresholds=THRESHOLDS,
            now=NOW,
        )
    return reading.to_relearn


async def test_his_erasure_marks_the_voice_to_be_learnt_again_until_it_is(session_factory):
    from domo_core.channels.voice.prints import learn
    from domo_core.channels.voice.thresholds import VISIT_GAP
    from voice_support import THRESHOLDS

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    ctx = nobody_spoke(alex, session_factory)
    done = await his_call("voice_unlearn", ctx, {"since_minutes": None, "person": "Dana"})
    assert done.status == "done"
    assert await _marked(session_factory, dana, DANA)
    assert not await _marked(session_factory, alex, ALEX)
    # Learnt again by the usual rule: three like sentences over two visits.
    results = []
    for at in (NOW, NOW + timedelta(minutes=1), NOW + VISIT_GAP + timedelta(minutes=5)):
        async with session_factory() as writer:
            results.append(
                await learn(
                    writer,
                    person_id=dana.id,
                    microphone=MIC,
                    print_=print_of(DANA),
                    thresholds=THRESHOLDS,
                    sure_otherwise=True,
                    visit_at=at,
                    now=at,
                )
            )
            await writer.commit()
    assert results[-1] == "learnt"
    assert not await _marked(session_factory, dana, DANA)


async def test_a_person_s_own_wish_to_forget_their_voice_leaves_no_mark(session_factory):
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    outcome = await call("voice_unlearn", context(alex, session_factory), {"since_minutes": None})
    assert outcome.status == "done"
    assert not await _marked(session_factory, alex, ALEX)


# --- review 3, A: a chain step is checked where it is first known ------------------------------


async def test_a_chain_step_checked_in_its_turn_is_checked_when_it_runs(session_factory):
    from domo_core.confirmations.chain import run_steps
    from domo_core.confirmations.steps import PlannedStep

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    arguments = {"since_minutes": None, "person": "Dana"}
    # Run after a yes (a request), in a turn where Alex speaks, as the assistant's: where
    # someone spoke his idea is weighed by the judge when it is asked (case 4 of the final wave),
    # so a step run after the yes is not refused for it.
    ctx = replace(context(alex, session_factory), actor=domo_actor(), request_id=uuid.uuid4())
    # A step whose target was checked when the chain was first asked runs on it.
    checked = DescribedAction(
        connector="core",
        action_type="voice.unlearn",
        target=f"person:{dana.id}",
        effect="self_profile",
        destination="household",
    )
    first = PlannedStep("voice_unlearn", "voice.unlearn", arguments, action=checked)
    run = await run_steps(ctx, [first], basis=None, request_id=None, decider=None)
    assert run.status == "done"
    assert {person for person, _ in await prints_of(session_factory)} == {str(alex.id)}


# --- a member's words in the turn are judged, never a refusal (final fix wave, case 1) -----------


async def test_a_members_note_in_the_turn_has_the_judge_weigh_the_persons_own_wish(
    session_factory,
):
    from domo_core.policy import barrier

    alex, _ = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    # Dana left Alex a note, read in this turn; Alex says "and stop listening now".
    ctx = context(alex, session_factory, told_outside=True)
    for name, arguments in (
        ("voice_stop_listening", {}),
        ("spoken_phrase_change", {}),
        ("spoken_phrase_remove", {}),
        ("voice_unlearn", {"since_minutes": None}),
    ):
        described = await TOOLS[name].describe(arguments, ctx.actor, ctx)
        assert described.whose_judged, name
        # The barrier has the judge weigh who it is from, as for a speaker it is not sure of.
        decision = barrier.decide(ctx.actor, described, DEFAULT_POLICY)
        assert getattr(decision, "judge_identity", False), name
    # Their own words alone: nothing to weigh.
    alone = context(alex, session_factory)
    described = await TOOLS["voice_stop_listening"].describe({}, alone.actor, alone)
    assert not described.whose_judged
    # Stored with a request, it reads back.
    assert DescribedAction.from_json(described.as_json()) == described
    marked = replace(described, whose_judged=True)
    assert DescribedAction.from_json(marked.as_json()) == marked


VOICE_TOOLS_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("voice_stop_listening", {}),
    ("spoken_phrase_change", {}),
    ("spoken_phrase_remove", {}),
    ("voice_unlearn", {"since_minutes": None}),
)


async def test_a_note_in_a_turn_with_no_words_of_the_person_is_judged_never_refused(
    session_factory,
):
    # The owner's rule (2026-11-26): it all depends on the source, and the judge weighs it like a
    # person would. A stranger's words (or a program's) relayed as his own note, or a member's
    # note, in a turn with no words of the person themself (a wake, the note alone): the call
    # goes to the judge, never refused by code; nothing is done before it.
    from domo_core.policy import barrier

    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await two_voices(session_factory, alex, dana)
    for unknown in (True, False):
        alone = context(
            alex, session_factory, answers=False, told_outside=True, told_unknown=unknown
        )
        for name, arguments in VOICE_TOOLS_CALLS:
            described = await TOOLS[name].describe(arguments, alone.actor, alone)
            assert described.whose_judged, (name, unknown)
            decision = barrier.decide(alone.actor, described, DEFAULT_POLICY)
            assert getattr(decision, "judge_identity", False), (name, unknown)
    # An idea of his fed by such a note, in a wake or a turn of his own: judged too.
    whole = {"since_minutes": None, "person": "Alex"}
    for his in (
        context(alex, session_factory, answers=False, told_outside=True, told_unknown=True),
        nobody_spoke(alex, session_factory, told_outside=True, told_unknown=True),
    ):
        described = await TOOLS["voice_unlearn"].describe(whole, domo_actor(), his)
        assert described.whose_judged


async def test_a_plan_keeping_the_persons_words_is_judged_never_refused(session_factory):
    # A plan of theirs ("stop listening at ten") whose time came wakes him with no message: their
    # own words, kept by the plan; the judge weighs them (A8), never a refusal by code.
    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    due = replace(
        context(alex, session_factory, answers=False),
        due_plans=(await _a_due_plan(session_factory, alex, his=False),),
        asked_before=True,
    )
    described = await TOOLS["voice_stop_listening"].describe({}, due.actor, due)
    assert described.whose_judged


async def test_a_programs_text_alone_is_judged_on_both_paths_and_nothing_at_all_refused(
    session_factory,
):
    # The owner (2026-11-26): it all depends on the source; the judge weighs it like a person
    # would. A program's, a site's or a tool's raw text alone in the turn, no word of anyone: the
    # call goes to the judge, on the person's path and his idea's alike. Still refused by code: a
    # turn with no word of anyone and no text at all.
    from domo_core.policy import barrier

    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await two_voices(session_factory, alex, dana)
    nothing = context(alex, session_factory, answers=False)
    for name, arguments in VOICE_TOOLS_CALLS:
        assert await refusal(name, nothing, arguments) == "not_possible", name
    woken = context(alex, session_factory, answers=False)
    woken.tool_woke = True
    read = context(alex, session_factory, answers=False)
    read.raise_outside_text("program")
    for ctx in (woken, read):
        for name, arguments in VOICE_TOOLS_CALLS:
            described = await TOOLS[name].describe(arguments, ctx.actor, ctx)
            assert described.whose_judged, name
            decision = barrier.decide(ctx.actor, described, DEFAULT_POLICY)
            assert getattr(decision, "judge_identity", False), name
    # His idea where the only outside words are a program's text: judged.
    whole = {"since_minutes": None, "person": "Dana"}
    for kind in ("wake", "tool", "program"):
        only_a_program = nobody_spoke(alex, session_factory)
        if kind == "wake":
            only_a_program.tool_woke = True
        else:
            only_a_program.raise_outside_text(kind)  # type: ignore[arg-type]
        described = await TOOLS["voice_unlearn"].describe(whole, domo_actor(), only_a_program)
        assert described.whose_judged, kind
    # Nobody's words and no text at all: his own idea, as before, nothing to weigh.
    alone = await TOOLS["voice_unlearn"].describe(
        whole, domo_actor(), nobody_spoke(alex, session_factory)
    )
    assert not alone.whose_judged
    assert len(await prints_of(session_factory)) == 4


def test_the_judge_is_told_which_program_what_it_reaches_and_that_nobody_wrote():
    from domo_core.confirmations.case import (
        NOT_KNOWN_WHICH,
        PROGRAM_TEXT_ALONE_WORDS,
        PROGRAM_TEXT_WORDS,
        outside_words,
    )
    from domo_core.tools.context import OutsideSource

    doors = OutsideSource("wake", name="doors", reaches=("doors.lan:8123 (in the house)",))
    told = outside_words(
        unknown=False, kinds=("wake",), their_words=False, sources=(doors,), alone=True
    )
    assert told is not None
    assert PROGRAM_TEXT_WORDS["wake"] in told
    assert 'the tool "doors", built to reach ["doors.lan:8123 (in the house)"]' in told
    assert PROGRAM_TEXT_ALONE_WORDS in told
    assert NOT_KNOWN_WHICH not in told
    # Which tool, the code does not know: said so, never invented.
    unknown = outside_words(unknown=False, kinds=("tool",), their_words=False, alone=True)
    assert unknown is not None and NOT_KNOWN_WHICH in unknown
    # A program run by hand: named by its run; a tool declaring no address says so.
    by_hand = OutsideSource("program", run="ab12", reaches=())
    quiet = OutsideSource("tool", name="tally", reaches=())
    both = outside_words(
        unknown=False, kinds=("tool", "program"), their_words=True, sources=(by_hand, quiet)
    )
    assert both is not None
    assert 'a program run by hand (run "ab12"), built to reach no address' in both
    assert 'the tool "tally", built to reach no address' in both
    # Beside the person's own words, nobody-wrote is never said.
    assert PROGRAM_TEXT_ALONE_WORDS not in both
    # A tool no longer ready: what it reaches is not known.
    gone = OutsideSource("wake", name="gone")
    told = outside_words(unknown=False, kinds=("wake",), their_words=False, sources=(gone,))
    assert told is not None and 'the tool "gone", what it is built to reach is not known' in told


async def test_a_fact_of_the_voice_channel_is_the_houses_own_and_a_program_unknown_is_a_program(
    session_factory,
):
    # The voice channel's own fact (a phrase held back, hearing unavailable) is the house's code
    # speaking: never a tool's name, never "a program's text"; a program run by hand whose run
    # the code lost is "a program, not known which", never the tool "program_run".
    from domo_core.confirmations.case import (
        CHANNEL_FACT_WORDS,
        PROGRAM_TEXT_ALONE_WORDS,
        outside_words,
    )
    from domo_core.conversation.prepare import wake_sources
    from domo_core.prompts import ToolWake
    from domo_core.tools.context import OutsideSource

    fact = ToolWake("voice_channel", {"hearing": "no_model"}, given_by="channel")
    lost = ToolWake("program_run", {"status": "done"}, given_by="program")
    # A tool Domo made, whatever its name, stays a tool by its name.
    named = ToolWake("voice_channel", {"n": 1})
    sources = wake_sources((fact, lost, named), {"voice_channel": ()})
    assert sources == (
        OutsideSource("channel"),
        OutsideSource("wake", program=True),
        OutsideSource("wake", name="voice_channel", reaches=()),
    )
    alex, _ = await alex_and_dana(session_factory)
    ctx = replace(
        context(alex, session_factory),
        tool_woke=True,
        woke_by=(sources[0],),
    )
    assert ctx.outside_kinds == ("channel",)
    told = outside_words(
        unknown=False,
        kinds=ctx.outside_kinds,
        their_words=False,
        sources=ctx.outside_sources,
        alone=True,
    )
    assert told is not None and CHANNEL_FACT_WORDS in told
    assert "voice_channel" not in told and "nobody's wish" not in told
    assert PROGRAM_TEXT_ALONE_WORDS in told
    both = outside_words(unknown=False, kinds=("wake",), their_words=False, sources=sources[1:])
    assert both is not None and "a program, not known which" in both
    assert 'the tool "program_run"' not in both
    assert 'the tool "voice_channel", built to reach no address' in both
    mixed = replace(ctx, woke_by=sources)
    assert mixed.outside_kinds == ("wake", "channel")


async def test_a_tools_wake_alone_saying_stop_listening_is_judged_and_a_silent_judge_asks_a_proof(
    session_factory, monkeypatch
):
    # The house's hub woke him through a tool of his ("doors"), nobody wrote: what it gave asks
    # to stop listening. Judged, the judge told the tool, what it reaches and that nobody wrote;
    # silent, a proof is asked: never a silent "do". Trusted, it is done.
    from domo_core.confirmations.case import PROGRAM_TEXT_ALONE_WORDS
    from domo_core.confirmations.judge import Judgement, fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall
    from domo_core.tools.context import OutsideSource

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    doors = OutsideSource("wake", name="doors", reaches=("doors.lan:8123 (in the house)",))
    ctx = replace(
        context(alex, session_factory, answers=False),
        speaker=None,
        conversation_id=conversation_id,
        domo_message_id=reply_id,
        tool_woke=True,
        woke_by=(doors,),
    )
    cases = _judge(monkeypatch, fallback("no_model", they_asked=False))
    stop = ToolCall("s", "voice_stop_listening", "{}")
    record = await handle_tool_call(stop, ctx=ctx, reply_id=reply_id)
    told = cases[0].as_json()["idea"]["after_outside_words"]
    assert isinstance(told, str)
    assert 'the tool "doors", built to reach ["doors.lan:8123 (in the house)"]' in told
    assert PROGRAM_TEXT_ALONE_WORDS in told
    assert record.status == "waiting", record.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert (request.ask, request.asked_by) == ("proof", "judge")
    _judge(monkeypatch, Judgement("do"))
    record = await handle_tool_call(stop, ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result


async def test_his_idea_on_a_programs_text_alone_is_judged_and_left_undone_when_silent(
    session_factory, monkeypatch
):
    from domo_core.confirmations.case import NOT_KNOWN_WHICH, PROGRAM_TEXT_ALONE_WORDS
    from domo_core.confirmations.judge import Judgement, fallback
    from domo_core.conversation.execution import handle_tool_call

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    ctx = _his_own_turn(replace(context(alex, session_factory), conversation_id=conversation_id))
    # What a tool gave is in the turn, which tool the code does not know here.
    ctx.raise_outside_text("tool")
    cases = _judge(monkeypatch, fallback("no_model", they_asked=False))
    erase = _erase({"since_minutes": None, "person": "Dana"})
    record = await handle_tool_call(erase, ctx=ctx, reply_id=reply_id)
    assert len(cases) == 2
    told = cases[0].as_json()["idea"]["after_outside_words"]
    assert NOT_KNOWN_WHICH in told and PROGRAM_TEXT_ALONE_WORDS in told
    assert record.model_result["reason"] == "judge_silent"
    assert len(await prints_of(session_factory)) == 4
    assert await words_left(session_factory, dana) == []
    _judge(monkeypatch, Judgement("do"))
    record = await handle_tool_call(erase, ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert len(await words_left(session_factory, dana)) == 1


async def test_a_programs_text_beside_a_note_or_their_words_is_judged_on_both_paths(
    session_factory,
):
    # A note (his own, a member's, whatever its mark) or the person's words, kept by a plan or
    # said in the turn, is something to judge, even with a program's text in the turn: the
    # person's path and his idea's alike (review MINOR 1).
    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await two_voices(session_factory, alex, dana)
    plan = await _a_due_plan(session_factory, alex, his=False)
    woken_with_a_note = context(
        alex, session_factory, answers=False, told_outside=True, told_unknown=True
    )
    woken_with_a_note.tool_woke = True
    read_with_a_note = context(alex, session_factory, answers=False, told_outside=True)
    read_with_a_note.raise_outside_text("tool")
    planned = replace(
        context(alex, session_factory, answers=False), due_plans=(plan,), asked_before=True
    )
    planned.tool_woke = True
    for ctx in (woken_with_a_note, read_with_a_note, planned):
        for name, arguments in VOICE_TOOLS_CALLS:
            assert (await TOOLS[name].describe(arguments, ctx.actor, ctx)).whose_judged, name
    whole = {"since_minutes": None, "person": "Dana"}
    for his in (
        replace(nobody_spoke(alex, session_factory, told_outside=True), tool_woke=True),
        replace(nobody_spoke(alex, session_factory), due_plans=(plan,), asked_before=True),
    ):
        his.raise_outside_text("tool")
        assert (await TOOLS["voice_unlearn"].describe(whole, domo_actor(), his)).whose_judged
    assert len(await prints_of(session_factory)) == 4


async def test_read_me_this_page_then_stop_listening_is_judged_and_a_silent_judge_asks_a_proof(
    session_factory, monkeypatch
):
    # Alex says "read me this page, then stop listening": a tool of his read the page, so its
    # text is in the turn beside Alex's words. "Stop listening" is judged, never refused by code;
    # the judge is told which kind of text is there and that Alex's words are his. Silent, it asks
    # a proof: never a silent "do".
    from domo_core.confirmations.case import PROGRAM_TEXT_WORDS
    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall
    from domo_core.policy import barrier

    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    ctx = replace(
        context(alex, session_factory), conversation_id=conversation_id, domo_message_id=reply_id
    )
    ctx.raise_outside_text("tool")
    for name, arguments in VOICE_TOOLS_CALLS:
        described = await TOOLS[name].describe(arguments, ctx.actor, ctx)
        assert described.whose_judged, name
        decision = barrier.decide(ctx.actor, described, DEFAULT_POLICY)
        assert getattr(decision, "judge_identity", False), name
    cases = _judge(monkeypatch, fallback("no_model", they_asked=False))
    record = await handle_tool_call(
        ToolCall("s", "voice_stop_listening", "{}"), ctx=ctx, reply_id=reply_id
    )
    # Silent, the judge is asked once more first (``handle_tool_call``).
    assert len(cases) in (1, 2)
    told = cases[0].as_json()["idea"]["after_outside_words"]
    assert all(case.as_json()["idea"]["after_outside_words"] == told for case in cases)
    assert isinstance(told, str)
    assert PROGRAM_TEXT_WORDS["tool"] in told
    assert PROGRAM_TEXT_WORDS["program"] not in told
    assert "the person's own words" in told
    assert record.status == "waiting", record.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert (request.ask, request.asked_by) == ("proof", "judge")


def test_the_judge_is_told_each_kind_of_outside_text_and_an_unknown_note_together():
    from domo_core.confirmations.case import (
        PROGRAM_TEXT_WORDS,
        UNKNOWN_SOURCE_WORDS,
        outside_words,
    )

    assert outside_words(unknown=False, kinds=(), their_words=True) is None
    assert outside_words(unknown=True, kinds=(), their_words=False) == UNKNOWN_SOURCE_WORDS
    both = outside_words(unknown=True, kinds=("program", "wake"), their_words=False)
    assert both is not None
    assert UNKNOWN_SOURCE_WORDS in both
    assert PROGRAM_TEXT_WORDS["program"] in both and PROGRAM_TEXT_WORDS["wake"] in both
    assert PROGRAM_TEXT_WORDS["tool"] not in both
    assert "the person's own words" not in both


async def test_a_plan_keeping_their_words_with_a_silent_judge_asks_a_proof(
    session_factory, monkeypatch
):
    # Review MINOR 3: a plan of Alex's ("stop listening at ten") keeps his words; its time came
    # with no message. Judged; the judge silent (even its "do" fallback), a proof is asked.
    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    plan = await _a_due_plan(session_factory, alex, his=False)
    due = replace(
        context(alex, session_factory, answers=False),
        conversation_id=conversation_id,
        domo_message_id=reply_id,
        due_plans=(replace(plan, their_words="stop listening at ten"),),
        asked_before=True,
    )
    cases = _judge(monkeypatch, fallback("no_model", they_asked=True))
    record = await handle_tool_call(
        ToolCall("p", "voice_stop_listening", "{}"), ctx=due, reply_id=reply_id
    )
    assert len(cases) >= 1
    assert record.status == "waiting", record.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert (request.ask, request.asked_by) == ("proof", "judge")


async def test_the_judge_is_told_a_note_of_an_unknown_source_is_in_the_turn(
    session_factory, monkeypatch
):
    # A wake at Alex's with a note born of a voice nobody knows: the judge reads plainly where the
    # words not theirs come from. Silent, it asks a proof (the code is not sure the wish is
    # theirs): nothing is done before it.
    from domo_core.confirmations.case import UNKNOWN_SOURCE_WORDS
    from domo_core.confirmations.judge import fallback
    from domo_core.confirmations.models import Confirmation
    from domo_core.conversation.execution import handle_tool_call
    from domo_core.llm.types import ToolCall

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    cases = _judge(monkeypatch, fallback("no_model", they_asked=False))
    ctx = replace(
        context(alex, session_factory, answers=False, told_outside=True, told_unknown=True),
        conversation_id=conversation_id,
        domo_message_id=reply_id,
    )
    record = await handle_tool_call(
        ToolCall("s", "voice_stop_listening", "{}"), ctx=ctx, reply_id=reply_id
    )
    [case] = cases
    assert case.as_json()["idea"]["after_outside_words"] == UNKNOWN_SOURCE_WORDS
    assert record.status == "waiting", record.model_result
    async with session_factory() as reader:
        [request] = await reader.scalars(select(Confirmation))
    assert (request.ask, request.asked_by) == ("proof", "judge")
    # A member's note only: the judge reads that words not theirs are in the turn, no more.
    cases.clear()
    member = replace(ctx, told_unknown=False)
    await handle_tool_call(
        ToolCall("t", "voice_stop_listening", "{}"), ctx=member, reply_id=reply_id
    )
    assert cases[0].as_json()["idea"]["after_outside_words"] is True


async def test_his_idea_fed_by_a_note_of_an_unknown_source_is_judged_and_done_or_not(
    session_factory, monkeypatch
):
    from domo_core.confirmations.case import UNKNOWN_SOURCE_WORDS
    from domo_core.confirmations.judge import Judgement, fallback
    from domo_core.conversation.execution import handle_tool_call

    alex, dana = await alex_and_dana(session_factory)
    await two_voices(session_factory, alex, dana)
    conversation_id, reply_id = await _alex_s_reply(session_factory, alex)
    ctx = _his_own_turn(
        replace(
            context(alex, session_factory, told_outside=True, told_unknown=True),
            conversation_id=conversation_id,
        )
    )
    # Silent twice: he does nothing for now and asks nobody.
    cases = _judge(monkeypatch, fallback("no_model", they_asked=False))
    erase = _erase({"since_minutes": None, "person": "Dana"})
    record = await handle_tool_call(erase, ctx=ctx, reply_id=reply_id)
    assert len(cases) == 2
    assert cases[0].as_json()["idea"]["after_outside_words"] == UNKNOWN_SOURCE_WORDS
    assert record.model_result["reason"] == "judge_silent"
    assert len(await prints_of(session_factory)) == 4
    # The judge finds it sensible: done, and Dana is left a word.
    _judge(monkeypatch, Judgement("do"))
    record = await handle_tool_call(erase, ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert {person for person, _ in await prints_of(session_factory)} == {str(alex.id)}
    assert len(await words_left(session_factory, dana)) == 1


async def test_the_persons_own_words_beside_a_note_of_an_unknown_source_are_judged(
    session_factory,
):
    # Final fix wave C (B review I-3): the person said it in this turn ("stop listening for the
    # evening") while a note born of a voice nobody knows waits untold: never refused by code, in
    # this turn or the next; the judge weighs it, told words not theirs are in the turn.
    from domo_core.policy import barrier

    alex, dana = await alex_and_dana(session_factory)
    await agreed(session_factory, alex, "spoken_phrase")
    await two_voices(session_factory, alex, dana)
    for _turn in range(2):
        ctx = context(alex, session_factory, told_outside=True, told_unknown=True)
        assert ctx.outside_text
        for name, arguments in VOICE_TOOLS_CALLS:
            described = await TOOLS[name].describe(arguments, ctx.actor, ctx)
            assert described.whose_judged, name
            decision = barrier.decide(ctx.actor, described, DEFAULT_POLICY)
            assert getattr(decision, "judge_identity", False), name
    # An idea of his, in a turn where the person spoke, is weighed by the judge too.
    his = context(alex, session_factory, told_outside=True, told_unknown=True)
    whole = {"since_minutes": None, "person": "Dana"}
    described = await TOOLS["voice_unlearn"].describe(whole, domo_actor(), his)
    assert described.whose_judged
    assert len(await prints_of(session_factory)) == 4
