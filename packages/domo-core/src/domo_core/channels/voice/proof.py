"""The spoken phrase at the voice channel (spec 6.9): agreeing it in a reserved moment, and giving
it as a proof inside a proof window of the microphone that hears it.

Agreeing (or changing): a tool of the assistant opened a moment (``channels.voice.moments``); each
saying heard in it goes to the vault and nowhere else (``take_for_the_vault``): the gatekeeper's
``phrase_enrol`` keeps the first, agrees the second when it is the same, forgets both when it is
not (the moment then starts again), and a saying too short is no phrase. When the phrase is agreed,
the means is the person's (``proofs.service.agree``) and the moment is over. The moment is bound
to the microphone of the reply that showed the tool opening it, or else to the microphone of its
first saying (``_claimed``: one statement, so that one sentence heard by two pages is taken once),
and the weakness of a phrase said aloud is remembered as
told when that first saying arrives (``tools.voice_tools.remember_the_weakness_told``).

Giving (fix round 5 of Task 24): the requests a person waits on to give a proof by their phrase
are read here (``proofs_awaited``: they have a phrase, held back or not, and the request waits for
a proof they may give); WHEN a sentence is weighed is the voice session's proof window (the
assistant spoke to that person at this microphone lately, ``session.PROOF_WINDOW_S``). A sentence
weighed (``give_the_phrase``) reaches no model, buffer or discussion before; what the gatekeeper
says comes back typed, and nothing of what was heard is kept:

* it is the phrase: the proof is given (``proofs.service.prove``), nothing is written. Said by
  a voice that is not surely the person's (``doubted``: someone who overheard it, nobody known, a
  sound too short), the proof is still checked by code, and the judge weighs it with the clues
  before anything is decided (``_the_judge_lets_it``, spec 6.9): when he asks another proof (or
  is silent), nothing is decided and the request still waits (``another_proof``);
* almost: the sentence is dropped (``did_not_work``);
* held back (too many near misses or comparisons lately): not compared, dropped
  (``held_back``); the session tells it as a failed try, so that a bystander cannot tell a wrong
  phrase from a held one;
* nothing like it: an ordinary sentence (``not_an_answer``), which goes its usual way;
* the gatekeeper cannot be asked: the sentence is dropped, the request still waits
  (``not_checked``);
* the request no longer waits for this person (answered, expired): the sentence is dropped
  (``decided``, with what ``prove`` said).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal

import httpx
from sqlalchemy import or_, select, update

from domo_core.accounts.models import Person
from domo_core.channels.voice.abilities import VOICE
from domo_core.channels.voice.deps import VoiceDeps
from domo_core.channels.voice.models import VoiceMoment
from domo_core.channels.voice.moments import advance, close_moment, moment_of
from domo_core.chat.agent import ActionRecord
from domo_core.confirmations.case import RightsSay, YesTold
from domo_core.confirmations.chain import request_told
from domo_core.confirmations.describe import description_for_a_model
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import (
    DecideOutcome,
    confirmations_by_id,
    may_decide,
    requester_context,
)
from domo_core.confirmations.store import ask_of, conversation_made_in
from domo_core.conversation.execution import consult_the_judge
from domo_core.conversation.prepare import made_tool_deps, made_tool_finder
from domo_core.conversation.service import Conversation
from domo_core.conversation.speaker import Speaker, actor_of
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.person_picker import person_picker
from domo_core.llm.picker import ModelPicker
from domo_core.policy.actions import DescribedAction
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.roles import Actor, as_role
from domo_core.proofs.kinds import MAX_HEARD_LENGTH, ProofPlace, phrase_ref
from domo_core.proofs.service import (
    NotAnAnswer,
    NotWeighed,
    ProofHeldBack,
    agree,
    could_offer,
    prove,
)
from domo_core.texts.view import person_texts
from domo_core.tools.context import ToolContext
from domo_core.tools.voice_tools import remember_the_weakness_told

logger = logging.getLogger(__name__)

SPOKEN_PHRASE: Final = "spoken_phrase"
# The actions that open a phrase moment.
PHRASE_ACTIONS: Final = frozenset({"proof.phrase_agree", "proof.phrase_change"})
# What a gatekeeper that cannot be reached raises: its own error, or the transport's.
_UNREACHED: Final = (GatekeeperError, httpx.HTTPError, OSError)

# What became of a saying of a phrase being agreed: the gatekeeper's word; for a saying never
# sent, ``too_long`` (no phrase is that long), ``not_kept`` (the vault could not be reached) and
# ``elsewhere`` (the moment is another microphone's: this page's copy is not taken).
PhraseOutcome = Literal[
    "first_kept", "agreed", "not_the_same", "too_short", "too_long", "not_kept", "elsewhere"
]
# What became of a sentence weighed as a proof (see the module docstring).
Given = Literal[
    "decided", "did_not_work", "held_back", "not_an_answer", "not_checked", "another_proof"
]


def voice_place(gatekeeper: GatekeeperClient) -> ProofPlace:
    """Where a proof is given by voice: what the voice carries, and the gatekeeper's comparison
    of what was heard with the person's phrase."""

    async def compare(
        person_id: uuid.UUID, heard: str
    ) -> Literal["it_is", "almost", "nothing_like"]:
        return await gatekeeper.phrase_compare(phrase_ref(person_id), heard)

    return ProofPlace(VOICE, phrase_check=compare)


async def take_for_the_vault(
    deps: VoiceDeps,
    moment: VoiceMoment,
    heard: str,
    person: Person,
    *,
    microphone: str,
    now: datetime,
) -> PhraseOutcome:
    """A saying of ``moment`` (``phrase_agree`` or ``phrase_change``), heard on ``microphone``:
    to the vault, and the moment moves on (see the module docstring). The moment is claimed for
    ``microphone`` first, in one statement: when it is (or has just become) another's, nothing is
    sent (``elsewhere``)."""
    if not await _claimed(deps, moment, microphone, now):
        return "elsewhere"
    outcome: PhraseOutcome
    if len(heard) > MAX_HEARD_LENGTH:
        outcome = "too_long"
    else:
        try:
            outcome = (await deps.turn.gatekeeper.phrase_enrol(phrase_ref(person.id), heard)).step
        except _UNREACHED as error:
            logger.warning("A saying could not reach the vault (%s)", type(error).__name__)
            outcome = "not_kept"
    async with deps.turn.session_factory() as session:
        live = await moment_of(session, person_id=person.id, now=now)
        if outcome == "agreed":
            # The vault agreed it: the means follows, whatever became of the moment meanwhile.
            await agree(session, person.id, SPOKEN_PHRASE, proposed_by="person", now=now)
        if live is not None and live.id == moment.id:
            match outcome:
                case "first_kept":
                    await advance(session, live)
                case "agreed":
                    await close_moment(session, live)
                case "not_the_same":
                    live.step = 0  # both forgotten at the vault: it starts again
                    await session.flush()
                case _:
                    pass
            # Last, just before the commit: the audit log holds a lock until it.
            await remember_the_weakness_told(session, person.id)
        await session.commit()
    return outcome


async def _claimed(deps: VoiceDeps, moment: VoiceMoment, microphone: str, now: datetime) -> bool:
    """Whether the live ``moment`` is ``microphone``'s: it is bound to it now if it was bound to
    none (one statement: of two pages, one claims it)."""
    async with deps.turn.session_factory() as session:
        found = await session.execute(
            update(VoiceMoment)
            .where(
                VoiceMoment.id == moment.id,
                VoiceMoment.expires_at > now,
                or_(VoiceMoment.microphone.is_(None), VoiceMoment.microphone == microphone),
            )
            .values(microphone=microphone)
            .returning(VoiceMoment.id)
        )
        claimed = found.first() is not None
        await session.commit()
    return claimed


async def bind_phrase_moment(
    deps: VoiceDeps, person_id: uuid.UUID, microphone: str, now: datetime
) -> None:
    """The reply said on ``microphone`` shows the tool that opened ``person_id``'s phrase moment:
    the moment is that microphone's (when no other has it yet)."""
    async with deps.turn.session_factory() as session:
        await session.execute(
            update(VoiceMoment)
            .where(
                VoiceMoment.person_id == person_id,
                VoiceMoment.purpose.in_(["phrase_agree", "phrase_change"]),
                VoiceMoment.expires_at > now,
                VoiceMoment.microphone.is_(None),
            )
            .values(microphone=microphone)
        )
        await session.commit()


async def request_waits(
    deps: VoiceDeps, confirmation_id: uuid.UUID, person: Person, now: datetime
) -> bool:
    """Whether the request ``confirmation_id`` still waits for a proof that ``person`` may give."""
    async with deps.turn.session_factory() as session:
        c = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
        if c is None or c.status != "pending" or not may_decide(c, person, now):
            return False
        try:
            return ask_of(c) == "proof"
        except ValueError:
            return False


async def proofs_awaited(deps: VoiceDeps, person: Person, now: datetime) -> list[uuid.UUID]:
    """The requests that wait for a proof ``person`` can give by their phrase, the newest first:
    they have one (held back by the gatekeeper or not: a held phrase is still awaited, and what
    is said meanwhile must reach nothing), and the request waits for a proof they may give."""
    async with deps.turn.session_factory() as session:
        place = voice_place(deps.turn.gatekeeper)
        if SPOKEN_PHRASE not in await could_offer(session, person, place, now):
            return []
        found = await session.execute(
            select(Confirmation.id)
            .where(Confirmation.status == "pending", Confirmation.ask == "proof")
            .order_by(Confirmation.created_at.desc())
        )
        waiting = list(found.scalars())
    return [c for c in waiting if await request_waits(deps, c, person, now)]


@dataclass(frozen=True)
class Weighed:
    """A sentence weighed as a proof: what became of it, and, once decided, what ``prove``
    said."""

    given: Given
    outcome: DecideOutcome | None = None
    record: ActionRecord | None = None


async def give_the_phrase(
    deps: VoiceDeps,
    confirmation_id: uuid.UUID,
    *,
    person: Person,
    heard: str,
    speaker: Speaker,
    origin: str,
    now: datetime,
    doubted: bool,
) -> Weighed:
    """``heard`` weighed as ``person``'s proof of the request ``confirmation_id``, said by
    ``speaker`` (see the module docstring); ``doubted``: the voice is not surely the person's, and
    a phrase that holds goes to the judge with its clues."""
    turn = deps.turn
    context = await _moment_of_the_decision(deps, person, confirmation_id, origin)

    async def weighed_after() -> bool:
        return await _the_judge_lets_it(deps, context, confirmation_id, speaker)

    try:
        outcome, record = await prove(
            session_factory=turn.session_factory,
            confirmation_id=confirmation_id,
            person=person,
            means=SPOKEN_PHRASE,
            answer=heard,
            place=voice_place(turn.gatekeeper),
            throttle=deps.throttle,
            now=now,
            ctx_factory=requester_context(context),
            registry=turn.registry,
            policy=context.policy,
            speaker=speaker,
            weighed_after=weighed_after if doubted else None,
        )
    except (NotWeighed, ProofHeldBack):
        # Held back: not weighed, and dropped all the same (it may well be the phrase).
        return Weighed("held_back")
    except NotAnAnswer:
        return Weighed("not_an_answer")
    except _UNREACHED as error:
        logger.warning("A spoken proof could not be weighed (%s)", type(error).__name__)
        return Weighed("not_checked")
    if outcome == "proof_failed":
        return Weighed("did_not_work")
    if outcome == "needs_proof":
        return Weighed("another_proof")
    return Weighed("decided", outcome, record)


# The judge's verdicts that let a proof given against the voice stand: anything that asks no more
# than an explicit yes (the proof gave more). A proof asked, a ``do_not``, or a silent judge: no.
_THE_PROOF_STANDS: Final = frozenset({"do", "yes", "warn", "explicit_yes"})


async def _the_judge_lets_it(
    deps: VoiceDeps, context: ToolContext, confirmation_id: uuid.UUID, speaker: Speaker
) -> bool:
    """Whether the proof of ``confirmation_id`` that just held stands, though the voice that gave
    it weighs against the person (``speaker``'s clues): the judge reads the request, that a proof
    was just given, and who probably spoke (``consult_the_judge`` with the doubt on who asks, on
    the person's own key: a judge that does not answer asks another proof). Never raises: what
    cannot be read lets nothing stand."""
    person = context.person
    now = context.clock()
    try:
        async with deps.turn.session_factory() as session:
            c = (await confirmations_by_id(session, [confirmation_id])).get(confirmation_id)
            if c is None:
                return False
            action = DescribedAction.from_json(c.action)
            made_in = await conversation_made_in(session, c)
            told = replace(
                await request_told(session, c, before=(), after=(), now=now),
                yes=YesTold(when="just now", by="the person", how="proof"),
            )
        actor = actor_of(speaker, as_role(person.role))
        ctx = replace(
            context,
            actor=actor,
            speaker=speaker,
            conversation_id=made_in or context.conversation_id,
            models=await _models_of(deps, person),
        )
        judgement = await consult_the_judge(
            ctx,
            actor=actor,
            action=action,
            says=description_for_a_model(c),
            rights=RightsSay(allowed=True, needs=None, question=None),
            request=told,
            identity_doubted=True,
        )
    except Exception as error:
        logger.error(
            "A proof given against the voice could not be judged (%s)", type(error).__name__
        )
        return False
    return judgement.verdict in _THE_PROOF_STANDS


async def _models_of(deps: VoiceDeps, person: Person) -> ModelPicker | None:
    """The models the judge may use for ``person`` (``ModelPicker`` on the key ``choose_key``
    takes among the keys they may use, past none of its limits); ``None`` without one."""
    turn = deps.turn
    async with turn.session_factory() as session:
        return await person_picker(
            session,
            person,
            session_factory=turn.session_factory,
            model_factory=turn.model_factory,
            clock=turn.clock,
            now=turn.clock(),
        )


async def _moment_of_the_decision(
    deps: VoiceDeps, person: Person, confirmation_id: uuid.UUID, origin: str
) -> ToolContext:
    """The moment of the decision a proof makes (``decide_confirmation``'s), made the
    requester's by ``requester_context``, as the API's proof makes it."""
    turn = deps.turn
    async with turn.session_factory() as session:
        policy = await load_policy(session, turn.policy)
        texts = await person_texts(session, person)
    notify: Callable[[uuid.UUID], None] = Conversation(turn).notify
    return ToolContext(
        session_factory=turn.session_factory,
        actor=Actor(person_id=person.id, role=as_role(person.role)),
        person=person,
        conversation_id=confirmation_id,  # replaced by the request's own conversation
        origin=origin,
        gatekeeper=turn.gatekeeper,
        clock=turn.clock,
        registry=turn.registry,
        policy=policy,
        reply_message_id=None,
        texts=texts,
        text_writing=turn.text_writing,
        notify=notify,
        abilities=VOICE,
        made_tool_named=made_tool_finder(turn),
        launch_deps=made_tool_deps(turn),
    )
