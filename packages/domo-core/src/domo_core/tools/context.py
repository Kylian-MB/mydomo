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

from domo_core.accounts.models import Person
from domo_core.conversation.channel import NOBODY_LISTENS, Abilities
from domo_core.conversation.speaker import Speaker
from domo_core.db import SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.listing import ModelLister
from domo_core.llm.picker import ModelPicker
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.memory.embedder import NO_EMBEDDER, Embedder
from domo_core.policy.actions import DescribedAction, FactValue, Present
from domo_core.policy.asks import Ask, Asked
from domo_core.policy.catalog import PolicyTables, is_silent
from domo_core.policy.rights import NO_RIGHTS, Approver, RightsTable
from domo_core.policy.roles import Actor, Role, domo_actor
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
# that is right but that the household's state refuses, whatever the rights (the last key of the
# household, which nobody could talk without), or that a rule of the household or of the tool
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
    """

    status: Literal["done", "failed"]
    model_result: JsonObject
    card: JsonObject
    card_once: JsonObject | None = None
    confirmation_id: UUID | None = None
    recorded_as: str | None = None


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
    are marked as they run. It is ``None`` when a call runs without one.

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
    is never guessed.

    ``told_outside`` says that the turn was given words that are neither the person's nor
    Domo's own: a note someone left them, a request of someone else that waits for their yes,
    what an approver said of a request of theirs, waiting or quoted in the history
    (``conversation.prepare.prepare_turn``). They are data, and what Domo does in such a turn is
    not known to be what the person asked for (``they_asked``).

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
    served once it is over (``conversation.turn``), never in the call.

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
    due_plans: tuple[DuePlan, ...] = ()
    asked_before: bool = False
    embedder: Embedder = NO_EMBEDDER
    memories_found: set[UUID] = field(default_factory=lambda: set[UUID](), repr=False)
    made_tool_named: "Callable[[str], Awaitable[InternalTool | None]] | None" = None
    launch_deps: "LaunchDeps | None" = None
    on_his_own: bool = False
    # Never in a representation of the context (a log, an error): someone's own words.
    said: str | None = field(default=None, repr=False)

    @property
    def turn_subject(self) -> Actor:
        """Whose turn this is, as a tool's own guard reads it: Domo himself in a turn on his own
        (``on_his_own``, ``policy.roles.domo_actor``), else the person who asks (``actor``): the
        one a tool is closed to or not (``made.<name>``), who sets a trigger, who keeps an
        agreement. Who a call is *judged* as, by the barrier, the limits, the judge and the
        audit, is ``judged_actor_of``'s alone."""
        return domo_actor() if self.on_his_own else self.actor

    @property
    def may_act_for_the_house(self) -> bool:
        """Whether what this turn sets for the house (an agreement that lasts, a trigger that
        launches a tool for the house) may be set here: by the owner, or by Domo in a turn he
        acts in on his own. Anyone else sets them for themselves: a launch for the house is judged
        with Domo's own rights, and his own yes answers its cost (Task 9, R15)."""
        return self.on_his_own or self.person.role == "owner"

    def take_ask_more(self) -> tuple[Ask, str] | None:
        """What Domo asked for the next action (the ask and his reason), and forget it."""
        raised, self.ask_more = self.ask_more, None
        return raised

    @property
    def they_asked(self) -> bool:
        """Whether what Domo does in this turn is taken as a request the person made: the turn
        answers a message of theirs (``reply_message_id``), or a plan due keeps the exact words
        they asked it with (``asked_before``), and it was given nothing from outside
        (``told_outside``). The owner's rule "what is asked for and understood is done" is about
        such a request: a judge that does not answer asks nothing then, and a yes otherwise
        (``confirmations.judge.fallback``)."""
        return (self.reply_message_id is not None or self.asked_before) and not self.told_outside

    @property
    def answers_them(self) -> bool:
        """Whether the reply of this turn is Domo's own answer to a message of the person, with
        nothing from outside in the turn (``Message.own_answer``); the judge reads any other
        reply as relayed. A turn a plan woke him for answers no message, even when the
        plan keeps what they asked (``asked_before``)."""
        return self.reply_message_id is not None and not self.told_outside

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


def judged_actor_of(tool: InternalTool, ctx: ToolContext) -> Actor:
    """Who a call of ``tool`` in ``ctx`` is judged as, by the barrier, the limits, the yes-judge
    and the audit, wherever it is asked (``conversation.execution``, ``confirmations``): Domo
    himself (``policy.roles.domo_actor``) for a tool whose every call is his own act
    (``ActsAsDomo``) and for every call of a turn he acts in on his own
    (``ToolContext.on_his_own``); the person who asks (``ctx.actor``, as read again before the
    call) otherwise. The one source of it."""
    if ctx.on_his_own or isinstance(tool, ActsAsDomo):
        return domo_actor()
    return ctx.actor


@dataclass(frozen=True)
class WaitsForAYes:
    """What a call that carries a yes waits for (``AsksItsYes``): whose yes (``approver``) and what
    it asks, who asked it and why (``asked``). ``domo_asks``: the request is Domo's own, waiting
    for ``approver`` in the person's conversation (``Confirmation.by_domo``, ruling R17: an
    opening of his that takes the owner's yes, in the owner's own turn), so that the owner answers
    it there, by the button or in words, as the approver of a request he did not make himself."""

    approver: Approver
    asked: Asked
    domo_asks: bool = False


@runtime_checkable
class AsksItsYes(Protocol):
    """A tool whose call may carry the yes of a stop (a tool of Domo's called with
    ``yes_for``, ``madetools.offered``): such a call is never judged, and always waits for that
    yes, asked as the stop says (``conversation.execution.handle_tool_call``)."""

    async def waits_for(self, arguments: JsonObject, ctx: ToolContext) -> WaitsForAYes | None:
        """What the call waits for, when it carries a yes; ``None`` for any other call. Reads
        only; ``InvalidArguments`` when the yes it names cannot be given."""
        ...


@runtime_checkable
class AsksWhomItsYes(Protocol):
    """A tool whose call, when a yes is asked before it (by the yes-judge or a rule of the owner),
    may wait for someone else's yes than the person's own (ruling R24: reopening an address the
    owner closed, in anyone's turn but his, waits for his yes as Domo's request)."""

    async def whose_yes(self, arguments: JsonObject, ctx: ToolContext) -> Approver | None:
        """Whose yes such a request waits for, as Domo's own (``WaitsForAYes.domo_asks``);
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


# Who is shown the tools when no person is named: nobody's own rights are theirs (no person has
# this id: the database gives random ones).
_NOBODY: Final = UUID(int=0)


def tools_for(
    role: Role,
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    *,
    person_id: UUID | None = None,
    rights: RightsTable = NO_RIGHTS,
    on_his_own: bool = False,
) -> list[ToolDefinition]:
    """The definitions of the tools of ``registry`` that the person ``person_id``, of ``role``,
    may use: those whose type of action ``rights`` resolve as allowed for them on top of the
    catalog ``policy`` (``RightsTable.resolve``: the catalog knows the type, and their own right,
    else their role's, else the catalog gives it; the owner's own actions to the owner only), in
    the registry's order. Without ``person_id`` only the rights of the role count. A tool that is
    Domo's own act (``ActsAsDomo``), and every tool in a turn ``on_his_own``, is resolved for
    Domo himself (``policy.roles.domo_actor``), as the barrier judges its calls."""
    actor = Actor(_NOBODY if person_id is None else person_id, role)
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
    role: Role,
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
    *,
    person_id: UUID | None = None,
    rights: RightsTable = NO_RIGHTS,
    on_his_own: bool = False,
) -> tuple[str, ...]:
    """The names of the tools of ``registry`` that the rights in force keep from the person
    ``person_id``, of ``role`` (``tools_for`` leaves them out), in the registry's order, but the
    silent ones (``policy.catalog.is_silent``): they exist in the household and are not theirs to
    ask for (the owner's trial of 0.9.1: Domo said he lacked a tool to add someone). A tool the
    turn left out for another reason (an arrival's) is not in ``registry``, so not here."""
    offered = {
        definition.name
        for definition in tools_for(
            role, registry, policy, person_id=person_id, rights=rights, on_his_own=on_his_own
        )
    }
    return tuple(
        name
        for name, tool in registry.items()
        if name not in offered and not is_silent(tool.action_type)
    )
