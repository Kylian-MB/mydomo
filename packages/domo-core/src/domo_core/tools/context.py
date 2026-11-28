"""What a tool is given, what it gives back, and how it refuses arguments.

``ToolContext`` is the moment of the call: who asks, in which conversation, and the means to act
(database sessions, the gatekeeper, the clock). ``ToolOutcome`` is what came of it, in two parts
that never mix: ``model_result`` goes to the model, ``card`` (and ``card_once``) to the people.

The protocol of a tool (``InternalTool``) and ``InvalidArguments`` are defined here, next to the
context they use, and re-exported by ``tools.registry``, which is where callers find them. So is
``tools_for``, the tools a person is shown: a chain of actions (``tools.chain``) holds only those,
and the registry holds the chain.

A few tools say more of themselves: a tool that always refuses some situations
(``RefusesWhateverTheRights``), one that bounds each of its own steps (``BoundsItsOwnSteps``), one
whose call names whom or what it acts on (``ActsOn``), one that touches more than that
(``WillTouch``, read through ``touched_by``), one that plans several steps in place of
describing one call (``PlansSteps``: the chain), and one whose every call is Domo's own act
(``ActsAsDomo``: making his tools), judged with his own rights in anyone's turn.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import is_active
from domo_core.conversation.channel import NOBODY_LISTENS, Abilities
from domo_core.conversation.speaker import Speaker, heard_unsure
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.listing import ModelLister
from domo_core.llm.picker import ModelPicker
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.memory.embedder import NO_EMBEDDER, Embedder
from domo_core.people.hooks import RecordListeners
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.asks import Ask, Asked
from domo_core.policy.catalog import HOUSE_SET, PROGRAM_RUN, PolicyTables, is_made, is_silent
from domo_core.policy.rights import NO_RIGHTS, Approver, RightsTable, load_rights
from domo_core.policy.roles import Actor, domo_actor
from domo_core.prompts import DuePlan
from domo_core.texts.view import REFERENCE, Texts

if TYPE_CHECKING:
    # Only for the types of ``PlansSteps.plan``, of ``WakeStarter``, of ``text_writing`` and of
    # ``launch_deps`` (a launch's module reads the household's tools, which import this one): the
    # chain's module, the chat's records and the confirmations' service import this one, the
    # barrier's decision reads the household, whose service imports this one too, and the writing
    # of the texts reads the keys, whose tools import this one.
    from domo_core.chat.agent import ActionRecord
    from domo_core.confirmations.service import AmendOutcome, DecideOutcome
    from domo_core.madetools.launch import LaunchDeps
    from domo_core.policy.decision import ChainBasis
    from domo_core.texts.scheduler import TextWriting
    from domo_core.tools.chain import ChainPlan

# Why arguments were rejected: they make no sense (``invalid``), they name more than one person
# or thing (``ambiguous``) or nothing that exists (``not_found``), or a value is too long. A call
# that is right but that the household's state refuses, whatever the rights (an address the owner
# closed, reached by a request run without his yes), or that a rule of the household or of the tool
# refuses (a fact of trust noted in a turn that may not note it, say), is ``not_possible``: no
# mistake of the model's form, so it never makes the request take a stronger model (``chat.agent``).
InvalidCode = Literal["invalid", "ambiguous", "not_found", "too_long", "not_possible"]


# Wakes Domo in the conversation of the one who asked for the request (the id), once someone
# else decided it in words in their own conversation (spec 5.1), as after a button
# (``conversation.starts.spawn_wake``): the outcome of the decision, the record of the call that ran
# (if one did) and the person who decided. It only starts the wake (in a task of the application),
# and wakes nobody for an outcome that decided nothing.
WakeStarter = Callable[[UUID, "DecideOutcome | AmendOutcome", "ActionRecord | None", Person], None]
# Tells the core that news were just kept for someone (the id), once they are committed
# (``conversation.visits.news_for``): a turn for them may be due now
# (``conversation.starts.news_kept``). It returns at once and never raises.
Notify = Callable[[UUID], None]


# The longest reason Domo keeps for an idea of his (``MessageAction.idea_reason``).
MAX_IDEA_REASON_LENGTH: Final = 200


@dataclass(frozen=True)
class OwnIdea:
    """An act that is the assistant's own idea, as the yes-judge is told of it
    (``conversation.execution.consult_the_judge``): his ``reason`` for it, the person whose words
    it was born of (``born_of``, ``None`` when nobody's), and whether the person concerned lacks
    the right to do it themselves (``lacks_the_right``). Data, never instructions: nothing in the
    reason moves who asks or whose private memories the case reads."""

    reason: str
    born_of: UUID | None = None
    lacks_the_right: bool = False


class NothingDoneInTime(TimeoutError):
    """A step of a tool that changes nothing (describing, planning) took too long: nothing was
    done, and it may be tried again. Any other ``TimeoutError`` out of a tool leaves it unknown
    whether the call happened (``conversation.execution.execute_allowed``)."""


class InvalidArguments(Exception):
    """The arguments of a call cannot be acted on. ``code`` says why, in the words the model is
    given back; ``detail`` (optional) says more, for the model to correct its call.

    ``detail`` is written by the code of the tool: it never repeats a value of the arguments, a
    secret, a token or a link. ``setting`` is the setting of Domo's that refused it, when one did
    (its key and figure, ``madetools.settings.hit``), given back with the reason.
    """

    def __init__(
        self,
        code: InvalidCode,
        detail: str | None = None,
        *,
        setting: tuple[str, int] | None = None,
    ) -> None:
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code: InvalidCode = code
        self.detail = detail
        self.setting = setting


# Which kind of text nobody of the house said came into a turn, as far as the code knows it: what a
# tool Domo made gave (it may carry a site's text: the code cannot tell), what a program he ran by
# hand gave, or what a tool's or a program's launch gave, which started the turn
# (``ToolContext.tool_woke``); or a fact a channel of the house's own code reports, which started
# it (``channel``: not a program's text, nor anyone's words).
OutsideKind = Literal["tool", "program", "wake", "channel"]


@dataclass(frozen=True)
class OutsideSource:
    """Which program's text came into a turn, as the code knows it (what the context already tells
    Domo with it: ``madetools.offered.said_by``, ``prompts.ToolWake``): its ``kind``, the tool's
    name of the household (``name``) or the id of a program run by hand (``run``), and what it is
    built to reach as declared (``reaches``, ``madetools.risks.reaches_told``; ``None`` when it is
    not known). Neither ``name`` nor ``run``: the code does not know which (``program``: a program
    run by hand, whose run is not known). Never what the program says of itself. A ``channel``
    source is the house's own code: it has no name."""

    kind: OutsideKind
    name: str | None = None
    run: str | None = None
    reaches: tuple[str, ...] | None = None
    program: bool = False


@dataclass(frozen=True)
class ToolOutcome:
    """What came of a call that was executed.

    ``status`` is ``done``, or ``failed`` when the tool could not do it. ``model_result`` is what
    the model reads: it **never** holds a link, a token or a secret. ``card`` is what the people
    see under the reply (a catalog key and its parameters, ``{"key": ..., "params": {...}}``, and
    the dates among them as they are, in ISO 8601, ``"dates": {...}``: they are put in words when
    the card is shown, for whoever reads it): it is stored with the reply, so it never holds a
    secret, a token or a link either.

    ``card_once`` is for what may be shown only once, to the person who asked (an invitation
    link, say): it is in the answer to the request that executed the tool and nowhere else. It is
    never stored (not with the reply, not in a confirmation, not in the audit log or the logs)
    and never given to the model.

    ``confirmation_id`` is the pending request the call was about, for a tool that manages
    requests (``tools.confirmation_tools``): the card under the reply is then that request's
    card, as it is now, and the action is stored with it.

    ``recorded_as`` is the type of action the call is stored under, when it is not the tool's own
    (``conversation.execution.execute_allowed``): the rest of a chain continued that runs at once
    is stored as a chain that ran at once (``chain.run``), so that the history a limit reads counts
    its steps (``policy.facts``). ``None``: the tool's own type.

    ``source`` is which program's own words the result holds, when it holds some (a tool Domo
    made, a program he ran by hand: ``madetools.offered.said_by``): the judge is told it.
    """

    status: Literal["done", "failed"]
    model_result: JsonObject
    card: JsonObject
    card_once: JsonObject | None = None
    confirmation_id: UUID | None = None
    recorded_as: str | None = None
    source: OutsideSource | None = None


@dataclass
class OutsideText:
    """The one mutable mark of a turn that text of a program came into it (``raised``), of
    which kinds (``kinds``) and from which programs, when the code knows (``sources``), shared by
    its ``ToolContext`` and every copy of it (``ToolContext.outside_text``)."""

    raised: bool = False
    kinds: set[OutsideKind] = field(default_factory=lambda: set[OutsideKind]())
    sources: list[OutsideSource] = field(default_factory=lambda: list[OutsideSource]())


@dataclass
class HeardMeanwhile:
    """The one mutable mark of a turn that someone's words came into it while it ran (``raised``:
    what they wrote or said, kept and read between two rounds, ``conversation.turn``), shared by
    its ``ToolContext`` and every copy of it: in a turn Domo started himself, someone spoke after
    all (the voice's gate, ``tools.voice_tools``)."""

    raised: bool = False


def brings_outside_text(action_type: str) -> bool:
    """Whether what a call of this type gives back is a program's output or error text: a tool
    Domo made, or a program he runs by hand."""
    return is_made(action_type) or action_type == PROGRAM_RUN


@dataclass
class ToolContext:
    """The moment of a call: what a tool may use to describe it and to do it.

    ``actor`` and ``person`` are who asks (the action is theirs, even when someone else approved
    it later), read again before each call of the model in a turn (``conversation.execution``);
    ``conversation_id`` where. ``reply_message_id`` is the person's message that this
    turn answers (``None`` outside a turn), and ``domo_message_id`` the reply of Domo that the
    turn writes, under which the cards of its actions are shown (``None`` outside a turn).
    ``origin`` is the address the person reaches Domo at, for the links a tool shows them (``""``
    when Domo starts the turn), and ``texts`` the texts of the person who asked
    (``texts.view.person_texts``), for what a tool shows them outside of its card (the purpose of a
    vault form, say). ``abilities`` are what the channel the person speaks through can do
    (``conversation.channel.Abilities``): nobody listens outside a turn.
    ``speaker`` is who speaks, with the clues that it is them (``conversation.speaker``): the
    yes-judge reads it (``None`` outside a turn, or when nobody speaks).
    ``session_factory`` opens database sessions: a tool commits what it does in a session of its
    own. ``registry`` and ``policy`` are the ones the barrier used for this call, for a tool that
    acts on another action (a pending request).

    ``change_grant`` is the pending request that the person has just answered with a yes and a
    change, read from their own words at the start of the turn (decision D2): for this turn only,
    Domo may apply that change (``confirmation_amend``), once. Applying it uses the grant up
    (it is set back to ``None``).

    ``described_action`` is, when a stored request runs (a person said yes to it), the action as
    its tool described it when the request was made, which is what the person said yes to: a tool
    that names its target by a name checks that the name still means that target
    (``tools.household_admin``). It is ``None`` when a call runs right after it was described.

    ``models`` are the models of the turn (``conversation.turn``: ``llm.picker.ModelPicker``), on
    the key of the request, for what makes a call of its own apart from the conversation (the yes
    judge, the rights writer: ``tools.rights_tools``), each at the level its kind of task needs
    (``model_for``). It is ``None`` outside a turn (a yes a button gives replays a stored request).
    ``model_lister`` lists the models a key opens (``web.deps.get_model_lister``), given to a turn
    as its model factory is, for a tool that reads the list of the turn's key again
    (``models_refresh``); ``None`` outside a turn.

    ``approved_by`` is, when a stored request runs after a yes, the person who said it
    (``confirmations.service``); it is ``None`` when a call runs without one (the barrier let it
    through). ``no_reader`` is set for a turn whose channel cannot show something once to the
    person alone (``Abilities.shows_once``: a turn Domo starts, ``conversation.starts``), or
    that nobody reads any more (the page left): what a call would show only once would be seen by
    nobody. ``in_a_chain`` is
    set for a step of a chain of actions (``confirmations.chain``): a chain may make several such
    things, and one that runs at once has no request to keep a part shown once with, so each step
    leaves its own behind a button on the chain's card (spec 4.5). A tool that makes something
    only the one who asked may see (an invitation's link, the form for the model's key) shows it at
    once when ``approved_by`` is ``None`` or ``person``, who asked, a page reads the turn and it is
    no step of a chain; otherwise it leaves it for the one who asked to ask for, on their card
    (``leave_on_their_card``). A vault form (the key of a model) needs a safe screen as well
    (``Abilities.safe_screen``): on a channel that cannot open one, it is left on their card too,
    where their page opens it (``leave_the_form_on_their_card``).

    ``request_id`` is, when a stored request runs after a yes (``confirmations.service``), that
    request: a chain of actions (``tools.chain``) reads its steps there, and the rows of its steps
    are marked as they run. It is ``None`` when a call runs without one. ``checked_first`` is set
    while a call whose target was described when it was first asked is described again or run
    after a yes (``confirmations``: a stored request; a step of a chain that had its action when
    the chain was asked for): what was checked of the turn it was first asked in is not checked
    again in the turn of the yes. A step checked in its turn never has it.

    ``wake`` wakes Domo in the conversation of the one who asked for a request that this
    person, as its approver, decided in their own words (``confirmation_amend``: a yes with a
    change, spec 5.1), as a button does. It is ``None`` in a turn that no page reads (a wake:
    nothing Domo does there wakes anyone) and outside a turn.

    ``notify`` tells the core, once a tool committed news for someone (``note_leave``), that a turn
    for them may be due now (``Notify``). ``None`` outside a turn: the news then wait for the next
    sign that the person is there.

    ``text_writing`` writes people's texts in the background (``texts.scheduler``): what a call
    shows someone outside a page (the texts of a vault form, say) that is not their own yet is
    asked of it. ``None`` where there is none: nothing is asked then.

    ``said`` is the content of the person's message that started this turn, exactly as it is
    stored (``conversation.prepare.prepare_turn``): a tool keeps it as the person wrote it, copied
    by the code and never retyped by a model (``note_leave``: the exact words of a note). It is
    ``None`` in a turn without a message of theirs (a wake, an arrival), outside a turn, and when
    a stored request runs after a yes (``confirmations.service``) or a chain is continued in a
    later turn (``tools.chain``): the message of the turn that made the call is not kept, and it
    is never guessed. ``said_later`` are the messages they wrote while the turn ran, that it read
    (spec 11.3), each as it is stored; never set without ``said``. Their messages of the turn
    (``said_messages``) are checked for an exact passage one by one, never across two (ruling
    R62: ``tools.notes.exact_text_in``); what they said in the turn as a whole, as a reason, is
    ``said_in_turn``.

    ``told_outside`` says that the turn was given words that are neither the person's nor
    Domo's own: a note someone left them, a request of someone else that waits for their yes,
    what an approver said of a request of theirs, waiting or quoted in the history
    (``conversation.prepare.prepare_turn``). They are data, and what Domo does in such a turn is
    not known to be what the person asked for (``they_asked``). They close no act of his: he
    judges, weighing where each text comes from (the owner, 2026-11-26); an act that says "this
    person said so" reads ``reply_message_id``, or checks their passage (``said_messages``).

    ``told_unknown``: among those words, some are of a source nobody of the household is known
    for: a note the assistant left himself born of a voice nobody knows, or of a program's, a
    site's or a tool's text (``Note.from_unknown``). They keep their source: they are never a
    member's words. His other notes, and a member's, are only ``told_outside`` (the judge weighs
    them).

    ``told_unsure``: the turn was given sentences of the person heard from a voice the house was
    not sure was theirs (``Message.heard_unsure``), shown apart from their own messages (a review
    of what was said, ``memory.review``): probably theirs, never their own words, so what is kept
    from them keeps that source (``tools.memory_tools.origin_for``: ``unsure_voice``). In a turn
    that answers such a sentence, the same holds of their messages of the turn
    (``said_unsure``).

    ``ask_more`` is what Domo asked for the next action he calls in this answer
    (``ask_before_acting``): the ask and his reason. The next call the judge is consulted about
    takes it (``take_ask_more``), used up or not, so that it reaches one action only; it lives with
    the context of the turn, and nothing carries it to the next turn.

    ``due_plans`` are the plans of the person whose time came, for a turn one woke Domo for
    (``conversation.starts``, spec 6.9): the judge reads their exact words as what the person
    said (``conversation.execution.consult_the_judge``). ``asked_before`` is set when every one of
    them keeps such words: what Domo does then is what the person asked for, earlier
    (``they_asked``), though the reply answers no message of theirs (``answers_them``). A plan
    Domo made alone keeps none: a turn it is due in, even with others, is nobody's request.

    ``embedder`` is the local model that gives the meaning of memories (``memory.embedder``), for
    a tool that searches them: ``NO_EMBEDDER`` (found by their words alone) outside a turn.
    ``memories_found`` are the memories a search gave Domo in this turn: the turn marks them as
    served once it is over (``conversation.turn``), never in the call. ``forgotten_in_turn`` are
    the people forgotten at their own request in this turn: what the turn runs in (their
    conversations, requests and account) goes once it is over (``people.forget.finish``).

    ``made_tool_named`` finds, when the model calls it, a tool Domo made that the turn did not
    offer (``t_<name>``, past the ones a turn offers: ``madetools.offered.offer_named``), which
    then goes the same way as an offered one; ``None`` where no tool of his is offered (an
    arrival, outside a turn). ``launch_deps`` is what a program Domo runs by hand needs to run
    (``tools.program_run``: the application's, ``conversation.prepare.made_tool_deps``), wherever
    such a call may run, a yes given outside a turn included; ``None`` where none runs.

    ``on_his_own`` is set for a turn nobody asked for in which Domo acts on his own, for the
    house (spec 5.1, 5.8: a launch for the house woke him, ``conversation.tool_wakes``): the
    conversation is still the person's (``person``, the owner: what he says there is said to
    them, and a request that waits there waits for them), but every call of the turn is judged
    with Domo's own rights (``judged_actor_of``), and what he launches there is for the house.

    ``own_idea`` is what Domo marked as his own idea for the next action he calls in this answer
    (``act_on_my_own``): that action is judged with his own rights and what he knows. The next
    call that changes something takes it (``take_own_idea``), used up or not, as ``ask_more``.
    ``turn_reason`` is why Domo began this turn himself, when he did (``None`` otherwise): what he
    does there is his idea (``idea_of``). ``judged_as`` is, while a call runs, who it was judged
    as (``judged_actor_of``: Domo for an idea of his), set by whoever runs it and set back after
    (``None`` otherwise): a tool that records who acted reads it (``acting``).

    ``record_listeners`` are the channels told of a merge, a split, an erasure or a forgetting
    once the tool that made it committed (``people.hooks``): the application's, in a turn, a
    review and a request run after a yes; nobody, where none is given.

    ``in_review`` is set for a review of what was said (``memory.review``): nobody reads it and
    no reply is written; every call of a gesture on people there is the assistant's own idea, born
    of the person reviewed, who may have left (``conversation.execution
    .needs_them_in_the_household``).
    """

    session_factory: SessionFactory
    actor: Actor
    person: Person
    conversation_id: UUID
    origin: str
    gatekeeper: GatekeeperClient
    clock: Callable[[], datetime]
    registry: Mapping[str, "InternalTool"]
    policy: PolicyTables
    reply_message_id: UUID | None
    domo_message_id: UUID | None = None
    change_grant: UUID | None = None
    described_action: DescribedAction | None = None
    texts: Texts = REFERENCE
    models: ModelPicker | None = None
    model_lister: ModelLister | None = None
    approved_by: UUID | None = None
    request_id: UUID | None = None
    no_reader: bool = False
    in_a_chain: bool = False
    wake: WakeStarter | None = None
    notify: Notify | None = None
    ask_more: tuple[Ask, str] | None = None
    text_writing: "TextWriting | None" = None
    abilities: Abilities = NOBODY_LISTENS
    speaker: Speaker | None = None
    told_outside: bool = False
    told_unknown: bool = False
    told_unsure: bool = False
    tool_woke: bool = False
    woke_by: tuple[OutsideSource, ...] = ()
    outside: OutsideText = field(default_factory=lambda: OutsideText(), repr=False)
    heard_meanwhile: HeardMeanwhile = field(default_factory=lambda: HeardMeanwhile(), repr=False)
    checked_first: bool = False
    due_plans: tuple[DuePlan, ...] = ()
    asked_before: bool = False
    embedder: Embedder = NO_EMBEDDER
    memories_found: set[UUID] = field(default_factory=lambda: set[UUID](), repr=False)
    forgotten_in_turn: set[UUID] = field(default_factory=lambda: set[UUID](), repr=False)
    made_tool_named: "Callable[[str], Awaitable[InternalTool | None]] | None" = None
    launch_deps: "LaunchDeps | None" = None
    on_his_own: bool = False
    own_idea: OwnIdea | None = None
    turn_reason: str | None = None
    judged_as: Actor | None = None
    in_review: bool = False
    record_listeners: RecordListeners = field(default_factory=RecordListeners, repr=False)
    # Never in a representation of the context (a log, an error): someone's own words.
    said: str | None = field(default=None, repr=False)
    said_later: tuple[str, ...] = field(default=(), repr=False)

    @property
    def said_messages(self) -> tuple[str, ...]:
        """The person's messages of this turn, each on its own: ``said``, then ``said_later``
        (none without ``said``)."""
        return () if self.said is None else (self.said, *self.said_later)

    @property
    def said_unsure(self) -> bool:
        """Whether the person's messages this turn answers were heard from a voice the house was
        not sure was theirs (``conversation.speaker.heard_unsure``, as their messages are marked
        where they are stored): no passage of them makes a memory their own words."""
        return (
            self.reply_message_id is not None
            and self.speaker is not None
            and heard_unsure(self.speaker)
        )

    @property
    def unsure_words(self) -> bool:
        """Whether a sentence of a voice not sure to be the person's is in this turn: told to it
        (``told_unsure``) or answered by it (``said_unsure``)."""
        return self.told_unsure or self.said_unsure

    @property
    def said_in_turn(self) -> str | None:
        """What the person said in this turn as a whole, their messages one after the other
        (``None`` without ``said``): their own words, as the reason of what they undo or
        complain about. Never read for an exact passage (``said_messages``)."""
        return "\n\n".join(self.said_messages) if self.said is not None else None

    @property
    def turn_subject(self) -> Actor:
        """Whose turn this is, as a tool's own guard reads it: Domo himself in a turn on his own
        (``on_his_own``, ``policy.roles.domo_actor``), else the person who asks (``actor``): the
        one a tool is closed to or not (``made.<name>``), who sets a trigger, who keeps an
        agreement. Who a call is *judged* as, by the barrier, the limits, the judge and the
        audit, is ``judged_actor_of``'s alone."""
        return domo_actor() if self.on_his_own else self.actor

    @property
    def acting(self) -> Actor:
        """Who the call that runs was judged as (``judged_as``), else whose turn it is
        (``turn_subject``): whom a tool records as having acted, and audits."""
        return self.turn_subject if self.judged_as is None else self.judged_as

    async def may_set_for_the_house(self, session: AsyncSession) -> bool:
        """Whether what this call sets for the house (an agreement that lasts, a trigger that
        launches a tool for the house) may be set at once (ruling R17, Task 27): by Domo in a turn
        he acts in on his own, by the person of the turn when the rights in force allow them
        ``house.set`` (the owner by default, anyone a right gives it to), or after the yes of
        someone they allow it (``approved_by``, in the household now). Anyone else's call waits
        for the owner's yes. A launch for the house is judged with Domo's own rights, and his own
        yes answers its cost (Task 9, R15). The right is read for whoever sets it, Domo too
        (``_sets_at_once``): a deny the owner wrote refuses it (``InvalidArguments``
        ``not_possible``); a right that asks first, names an approver or sets a limit is no "at
        once": it waits for the owner's yes as without a right, and that yes then sets it, his
        own as a person's (re-review R2). Reads only."""
        rights = await load_rights(session)
        setter = domo_actor() if self.on_his_own else self.actor
        if _sets_at_once(rights, setter, self.policy):
            return True
        if self.approved_by is None:
            return False
        # A person's own yes is not someone else's; on his own, the person of the conversation
        # (the owner) who said yes to his request is.
        if self.approved_by == self.person.id and not self.on_his_own:
            return False
        approver = await session.get(Person, self.approved_by)
        if approver is None or not is_active(approver, self.clock()):
            return False
        who = actor_of(approver)
        return _sets_at_once(rights, who, self.policy, refuses=False)

    def take_ask_more(self) -> tuple[Ask, str] | None:
        """What Domo asked for the next action (the ask and his reason), and forget it."""
        raised, self.ask_more = self.ask_more, None
        return raised

    def take_own_idea(self) -> OwnIdea | None:
        """What Domo marked as his own idea for the next action, and forget it."""
        taken, self.own_idea = self.own_idea, None
        return taken

    @property
    def they_asked(self) -> bool:
        """Whether what Domo does in this turn is taken as a request the person made: the turn
        answers a message of theirs (``reply_message_id``), or a plan due keeps the exact words
        they asked it with (``asked_before``), and no text from outside is in it
        (``outside_text``: words told to the turn, or what a tool he made or a program gave in
        it, from then on). The owner's rule "what is asked for and understood is done" is about
        such a request: a judge that does not answer asks nothing then, and a yes otherwise
        (``confirmations.judge.fallback``)."""
        return (self.reply_message_id is not None or self.asked_before) and not self.outside_text

    @property
    def answers_them(self) -> bool:
        """Whether the reply of this turn is Domo's own answer to a message of the person, with
        no text from outside in the turn (``outside_text``, ``Message.own_answer``); the judge
        reads any other reply as relayed. A turn a plan woke him for answers no message, even
        when the plan keeps what they asked (``asked_before``). Words of a household member told
        to the turn make it false too. Whether the person wrote in the turn at all is
        ``reply_message_id``."""
        return self.reply_message_id is not None and not self.outside_text

    @property
    def outside_text(self) -> bool:
        """Whether text that is not the person's own is in this turn: words told to the turn
        (``told_outside``), a turn a tool or a program woke him for, which carries what it gave
        (``tool_woke``), or what a tool he made, or a program, said came into it (``outside``,
        shared by every copy of the context: ``raise_outside_text``). What a tool that only reads
        what he knows or holds gave is not in it. It says whether the turn is their request and
        his own answer (``they_asked``, ``answers_them``) and where a memory written in it comes
        from (``tools.memory_tools.origin_for``); it closes no act of his: he weighs where each
        text comes from."""
        return self.told_outside or self.tool_woke or self.outside.raised

    def raise_outside_text(
        self, kind: OutsideKind = "tool", source: OutsideSource | None = None
    ) -> None:
        """Say that text of a program came into this turn, for good, of which kind (a tool he
        made, by default) and from which program when the code knows it (``source``): in this
        context and in every copy of it (``dataclasses.replace`` keeps the same ``outside``)."""
        self.outside.raised = True
        self.outside.kinds.add(kind)
        if source is not None and source not in self.outside.sources:
            self.outside.sources.append(source)

    @property
    def outside_kinds(self) -> tuple[OutsideKind, ...]:
        """The kinds of a program's, a site's or a tool's text in this turn, in a fixed order:
        what the judge is told (``confirmations.case.outside_words``)."""
        channel = {source.kind for source in self.woke_by} & {"channel"}
        # A turn the house's own channel alone woke him for carries no program's text of its own.
        woke = self.tool_woke and (
            not self.woke_by or any(source.kind != "channel" for source in self.woke_by)
        )
        found = {*self.outside.kinds, *(("wake",) if woke else ()), *channel}
        order: tuple[OutsideKind, ...] = ("tool", "program", "wake", "channel")
        return tuple(kind for kind in order if kind in found)

    @property
    def outside_sources(self) -> tuple[OutsideSource, ...]:
        """Which programs' text is in this turn, as far as the code knows: those whose launch
        started it (``woke_by``), then those that spoke in it, each once (``raise_outside_text``):
        what the judge is told (``confirmations.case.outside_words``)."""
        found: list[OutsideSource] = []
        for source in (*self.woke_by, *self.outside.sources):
            if source not in found:
                found.append(source)
        return tuple(found)

    @property
    def anyones_words(self) -> bool:
        """Whether anyone's words are in this turn: the person's message (``reply_message_id``),
        words told to it (a note of any mark: ``told_outside``, ``told_unknown``), a plan of
        theirs keeping their words (``asked_before``), words heard while it ran
        (``heard_meanwhile``), or a speaker who spoke. A program's, a site's or a tool's raw text
        is none of them."""
        speaker = self.speaker
        return (
            self.reply_message_id is not None
            or self.told_outside
            or self.told_unknown
            or self.asked_before
            or self.heard_meanwhile.raised
            or (speaker is not None and not speaker.nobody_spoke)
        )

    @property
    def someone_else_said_yes(self) -> bool:
        """Whether a person other than the one who asked said yes to this call: what it makes for
        the one who asked is then theirs to ask for (see ``approved_by``)."""
        return self.approved_by is not None and self.approved_by != self.person.id

    @property
    def leave_on_their_card(self) -> bool:
        """Whether what this call makes for the one who asked alone (a link, a form shown once)
        is left on their card, behind a button they press, rather than shown at once: someone
        else said yes to it, no page reads the turn (``no_reader``), so that nobody would see it,
        or it is a step of a chain (``in_a_chain``), whose card has a button for each. The model
        is then told it waits there, never that it was shown."""
        return self.someone_else_said_yes or self.no_reader or self.in_a_chain

    @property
    def leave_the_form_on_their_card(self) -> bool:
        """Whether a vault form this call would open for the one who asked (the key of a model)
        is left on their card, behind a button their page has, rather than opened now: as
        ``leave_on_their_card``, and also when the channel cannot open a safe screen
        (``Abilities.safe_screen``: the API's). Nothing is then asked of the vault, and the model
        is told the form waits on their card."""
        return self.leave_on_their_card or not self.abilities.safe_screen

    def form_left_card(self, action: str) -> str:
        """The key of the card of a vault form left on the card of the one who asked
        (``leave_the_form_on_their_card``), for the action ``action`` (``key.add``). When someone
        else said yes, the one who said it reads the card too: it speaks of whoever asked
        (``.on_request``). In every other case only the one who asked reads it, in their own
        conversation: it speaks to them (``.yours``)."""
        reader = "on_request" if self.someone_else_said_yes else "yours"
        return f"action.card.done.{action}.{reader}"


# What Domo reads with something set for the house on the yes of someone the rights allow it, for
# a person they do not (ruling R17): the owner may let it hold for them from now on.
HOUSE_SET_ON_A_YES: Final = (
    "set for the house on the owner's yes: if he wants this person to set such things for the "
    "house from now on without asking him, he may say so in his words, and the rights tools write "
    "it (a right on house.set)"
)


async def set_on_a_yes(session: AsyncSession, ctx: ToolContext) -> bool:
    """Whether what this call sets for the house is set on someone else's yes (``approved_by``)
    for a person of the conversation whom the rights in force do not allow ``house.set``: the
    result then tells Domo that the owner may let it hold (``HOUSE_SET_ON_A_YES``). Reads only."""
    if ctx.approved_by is None or ctx.approved_by == ctx.person.id:
        return False
    rights = await load_rights(session)
    person = actor_of(ctx.person)
    return not rights.resolve(person, HOUSE_SET, ctx.policy).allowed


@runtime_checkable
class InternalTool(Protocol):
    """A tool of the core.

    ``definition`` is what the model is shown (its ``name`` is the key of the registry);
    ``action_type`` the type of action of the catalog it does, whatever its arguments.
    """

    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def action_type(self) -> str: ...

    def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> Awaitable[DescribedAction]:
        """What the call would do, for the barrier. It may read the database (to find who a
        name means, say) and changes nothing. Raises ``InvalidArguments``."""
        ...

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        """Do it. Only called once the barrier allowed it, or a person said yes. The arguments
        are checked again (they may come from a stored request): raises ``InvalidArguments``."""
        ...


@runtime_checkable
class RefusesWhateverTheRights(Protocol):
    """A tool that always refuses some situations, whatever the rights (spec 5.2): it says them
    in ``fixed_refusal`` (a ``policy.actions.FixedRefusal``, pure: who asks, the facts of the
    action, who is in the household), which its own ``describe`` and ``execute`` use, and which
    the rights writer uses so that an example never shows what the tool would refuse
    (``tools.rights_tools``)."""

    def fixed_refusal(
        self, actor: Actor, facts: Mapping[str, FactValue], present: Present
    ) -> str | None: ...


@runtime_checkable
class BoundsItsOwnSteps(Protocol):
    """A tool whose ``execute`` puts a time limit on each of its own steps, because it carries
    another call to its record (``confirmation_amend`` runs a changed call under that call's own
    limit, then stores what came of it). ``conversation.execution.execute_allowed`` puts no limit
    of its own around such a tool: one around the whole would cut a run that must reach its record,
    or tell the model that an outcome which is known is not."""

    bounds_its_own_steps: Literal[True]


@runtime_checkable
class ActsAsDomo(Protocol):
    """A tool whose every call is Domo's own act, whoever's turn it is in (making, changing,
    removing, looking at, finding and timing his tools, spec 5.1: "making a tool is an act of
    Domo's, no person's right is looked at"): the barrier judges it with his own rights
    (``policy.roles.domo_actor``), and the owner stops it with a right of Domo's
    (``conversation.execution.judged_actor_of``). A person's right on its type changes nothing."""

    acts_as_domo: Literal[True]


def judged_actor_of(tool: InternalTool, ctx: ToolContext, idea: OwnIdea | None = None) -> Actor:
    """Who a call of ``tool`` in ``ctx`` is judged as, by the barrier, the limits, the yes-judge
    and the audit, wherever it is asked (``conversation.execution``, ``confirmations``): Domo
    himself (``policy.roles.domo_actor``) for a tool whose every call is his own act
    (``ActsAsDomo``), for every call of a turn he acts in on his own
    (``ToolContext.on_his_own``) and for a call he marked as his own idea (``idea``, the mark
    taken: ``ToolContext.take_own_idea``); the person who asks (``ctx.actor``, as read again
    before the call) otherwise. The one source of it."""
    if ctx.on_his_own or idea is not None or isinstance(tool, ActsAsDomo):
        return domo_actor()
    return ctx.actor


def acts_alone(ctx: ToolContext) -> bool:
    """Whether what Domo keeps, plans or notes in this turn is his own, not the person's: a turn he
    acts in on his own (``ToolContext.on_his_own``), or a turn that answers no message of the
    person and in which no plan due keeps their words (a review, a turn he started, a wake):
    nobody asked for what he does there (``not they_asked and reply_message_id is None``)."""
    return ctx.on_his_own or (not ctx.they_asked and ctx.reply_message_id is None)


def nobody_spoke_in(ctx: ToolContext) -> bool:
    """Whether nobody spoke in this turn and no words from outside are in it: a turn he acts in
    on his own or began himself (``acts_alone``), with no sentence heard (``speaker``), nothing
    told from outside (``outside_text``) and no words that came while it ran
    (``heard_meanwhile``). What it does is his alone."""
    speaker = ctx.speaker
    return (
        acts_alone(ctx)
        and not ctx.outside_text
        and not ctx.heard_meanwhile.raised
        and (speaker is None or speaker.nobody_spoke)
    )


def idea_of(ctx: ToolContext, taken: OwnIdea | None) -> OwnIdea | None:
    """Whose idea a call is, as the judge is told: the mark Domo put on it (``taken``), else the
    reason of a turn he began himself (``ToolContext.turn_reason``), else his idea with no reason
    in a turn he acts in on his own; ``None`` for what a person asked."""
    if taken is not None:
        return taken
    if ctx.turn_reason is not None:
        return OwnIdea(ctx.turn_reason)
    if ctx.on_his_own:
        return OwnIdea("")
    return None


# What the model is told when the owner denied setting something for the house to whoever sets it.
_HOUSE_SET_DENIED: Final = (
    "the owner does not let this be set for the house by whoever asks it now: nothing was done"
)


def _sets_at_once(
    rights: RightsTable, actor: Actor, policy: PolicyTables, *, refuses: bool = True
) -> bool:
    """Whether ``actor`` sets something for the house at once (``house.set``, read for them,
    Domo too): a right that lets them with nothing asked first, no approver and no limit. Domo
    with no right of his own on it does, in a turn of his own (ruling R17). A deny the owner
    wrote for them raises ``InvalidArguments`` ``not_possible`` (``refuses``), else counts as
    not at once."""
    if actor.subject == "domo":
        his = next((r for r in rights.rights if r.for_domo and r.action_type == HOUSE_SET), None)
        if his is None:
            return True
        if his.access == "deny":
            if refuses:
                raise InvalidArguments("not_possible", _HOUSE_SET_DENIED)
            return False
        asks = his.ask in ("yes", "explicit_yes", "proof")
        return not asks and his.limit is None and his.approver in (None, "self")
    if rights.denied_on_purpose(actor, HOUSE_SET):
        if refuses:
            raise InvalidArguments("not_possible", _HOUSE_SET_DENIED)
        return False
    resolved = rights.resolve(actor, HOUSE_SET, policy)
    return (
        resolved.allowed
        and resolved.ask is None
        and resolved.limit is None
        and resolved.approver in (None, "self")
    )


@dataclass(frozen=True)
class WaitsForAYes:
    """What a call that carries a yes waits for (``AsksItsYes``): whose yes (``approver``) and what
    it asks, who asked it and why (``asked``). ``domo_asks``: the request is Domo's own, waiting
    for ``approver`` in the person's conversation (``Confirmation.by_domo``, ruling R17: an
    opening of his that takes the owner's yes, in the owner's own turn), so that the owner answers
    it there, by the button or in words, as the approver of a request he did not make himself.

    ``judge_first``: the yes is the person's own, for a cost within their limit (spec part 12): the
    yes-judge weighs it first, with ``cost`` (the amount, the tool, the operation, their
    agreements on it and their own earlier spendings of the kind), and a usual one runs without
    it (``conversation.execution.handle_tool_call``)."""

    approver: Approver
    asked: Asked
    domo_asks: bool = False
    judge_first: bool = False
    cost: JsonObject | None = None


@runtime_checkable
class AsksItsYes(Protocol):
    """A tool whose call may carry the yes of a stop (a tool of Domo's called with
    ``yes_for``, ``madetools.offered``): such a call waits for that yes, asked as the stop says
    (``conversation.execution.handle_tool_call``); only the person's own yes for a cost
    (``WaitsForAYes.judge_first``) is weighed by the judge first."""

    async def waits_for(self, arguments: JsonObject, ctx: ToolContext) -> WaitsForAYes | None:
        """What the call waits for, when it carries a yes; ``None`` for any other call. Reads
        only; ``InvalidArguments`` when the yes it names cannot be given."""
        ...


@runtime_checkable
class AsksWhomItsYes(Protocol):
    """A tool whose call, when a yes is asked before it (by the yes-judge or a rule of the owner),
    may wait for someone else's yes than the person's own (ruling R24: reopening an address the
    owner closed, in anyone's turn but his, waits for his yes as Domo's request; Task 27: a house
    account the owner removed, asked again, waits for his yes, the request staying the person's)."""

    async def whose_yes(self, arguments: JsonObject, ctx: ToolContext) -> Approver | None:
        """Whose yes such a request waits for: as Domo's own (``WaitsForAYes.domo_asks``) for a
        tool whose every call is his act (``ActsAsDomo``), else as the person's own request;
        ``None`` for the usual. Reads only."""
        ...


@runtime_checkable
class NeverAStep(Protocol):
    """A tool some of whose calls are never a step of a chain (ruling R19): ``never_a_step`` says
    why, for such a call (``None`` for any other). A chain is refused when it is planned with such
    a step, and the tool refuses it again when a step runs (``ToolContext.in_a_chain``)."""

    async def never_a_step(self, arguments: JsonObject, ctx: ToolContext) -> str | None: ...


@runtime_checkable
class NamesPeople(Protocol):
    """A tool whose arguments name people of the household by their display name
    (``person_arguments``: the names of those arguments). Before a call of it is described, each
    name there that means one person of the household now, by their whole name, their first name
    or the beginning of their name (``household.service.resolve_person``), is replaced by that
    person's whole name (``household.service.whole_names``): what is described, judged, asked,
    stored and run then names the very person it acts on, never a name that could later mean
    someone else (spec 6.7)."""

    @property
    def person_arguments(self) -> tuple[str, ...]: ...


@runtime_checkable
class ActsOn(Protocol):
    """A tool whose call names whom or what it acts on, by a name (a person of the household, an
    invitation that waits). A chain of actions uses it to tell a step that cannot be described yet
    only because an earlier step changes whom it acts on (``tools.chain``)."""

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom or what the call acts on (``person:<id>``, ``invitation:<id>``), found by the name
        alone, with no check of what would happen to them; ``None`` when the arguments name
        nobody, or a name means nobody or several. It reads only, and never raises."""
        ...


@runtime_checkable
class WillTouch(Protocol):
    """A tool that says more than ``ActsOn`` of whom a call will touch once it runs: a call that
    touches several people, or one besides the person it names (the hand-over of the owner's role
    touches the giver as well as the receiver). A chain of actions uses it for a step that is
    checked in its turn (``tools.chain``): a later step may act on whom that step will touch."""

    async def will_touch(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> frozenset[str]:
        """Whom or what the call will touch once it runs (``person:<id>``...), found by the names
        alone, with no check of what would happen; empty when a name means nobody or several. It
        reads only, and never raises."""
        ...


async def touched_by(
    tool: InternalTool, arguments: JsonObject, actor: Actor, ctx: ToolContext
) -> frozenset[str]:
    """Whom or what a call of ``tool`` will touch, as the tool says it: ``will_touch`` for a tool
    that has it, else the one it acts on (``ActsOn``, none when its name means nobody), else
    nobody."""
    if isinstance(tool, WillTouch):
        return await tool.will_touch(arguments, actor, ctx)
    if isinstance(tool, ActsOn):
        whom = await tool.acts_on(arguments, ctx)
        return frozenset() if whom is None else frozenset({whom})
    return frozenset()


@runtime_checkable
class PlansSteps(Protocol):
    """A tool that does several of the other tools in order (the chain, ``tools.chain``): it plans
    its steps (``plan``: each one described and decided, and the chain's own decision), which
    ``conversation.execution`` uses in place of ``describe`` and the barrier's decision, and which
    a change of a chain that waits uses to plan it again (``confirmations.service``)."""

    async def plan(
        self,
        arguments: JsonObject,
        actor: Actor,
        ctx: ToolContext,
        *,
        done: Sequence[DescribedAction] = (),
        kept: Sequence[bool] | None = None,
        basis: "ChainBasis | None" = None,
        speaker: UUID | None = None,
    ) -> "ChainPlan":
        """The chain, as planned for ``actor`` now; planned again after the steps ``done``, each
        step ``kept`` as it was asked for judged under what the chain remembers (``basis``). The
        people its steps name are read as ``speaker`` names them (whose words the steps are: an
        approver who changes the chain; ``None`` for the person of ``ctx``). Raises
        ``InvalidArguments``."""
        ...


def tools_for(
    actor: Actor,
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    *,
    rights: RightsTable = NO_RIGHTS,
    on_his_own: bool = False,
) -> list[ToolDefinition]:
    """The definitions of the tools of ``registry`` that ``actor`` (a person, with the marks of
    their record: a made role and the child mark count) may use: those whose type of action
    ``rights`` resolve as allowed for them on top of the catalog ``policy``
    (``RightsTable.resolve``: the catalog knows the type, and their own right, else their role's,
    else the catalog gives it; the owner's own actions to the owner only), in the registry's
    order. A tool that is Domo's own act (``ActsAsDomo``), and every tool in a turn
    ``on_his_own``, is resolved for Domo himself (``policy.roles.domo_actor``), as the barrier
    judges its calls."""
    domo = domo_actor()
    return [
        tool.definition
        for tool in registry.values()
        if rights.resolve(
            domo if on_his_own or isinstance(tool, ActsAsDomo) else actor,
            tool.action_type,
            policy,
        ).allowed
    ]


def tools_kept_from(
    actor: Actor,
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    *,
    rights: RightsTable = NO_RIGHTS,
    on_his_own: bool = False,
) -> tuple[str, ...]:
    """The names of the tools of ``registry`` that the rights in force keep from the person
    ``actor`` (``tools_for`` leaves them out), in the registry's order, but the
    silent ones (``policy.catalog.is_silent``): they exist in the household and are not theirs to
    ask for (the owner's trial of 0.9.1: Domo said he lacked a tool to add someone). A tool the
    turn left out for another reason (an arrival's) is not in ``registry``, so not here."""
    offered = {
        definition.name
        for definition in tools_for(actor, registry, policy, rights=rights, on_his_own=on_his_own)
    }
    return tuple(
        name
        for name, tool in registry.items()
        if name not in offered and not is_silent(tool.action_type)
    )
