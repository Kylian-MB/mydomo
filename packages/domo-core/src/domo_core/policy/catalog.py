"""The catalog of the actions Domo knows, and who may do them.

This is the plan's decision D1. The barrier refuses an action type the catalog does not know
(``KNOWN_ACTIONS``). Spec 10.4 asks for "at least a confirmation" for the actions of a connector
that nobody has classified yet; 1b has only the core's own tools, so there is no such connector,
and refusing is the safe reading. Connector actions arrive in 1e and follow the spec there.

Whether a yes is asked before an action is not the catalog's: the yes-judge decides it
(``confirmations.judge``), after the barrier. The catalog marks what changes nothing
(``CHANGES_NOTHING``: the judge is not asked about it), what carries other actions and is judged
inside (``JUDGED_WITHIN``), and the types for which an adult's agreement stays a right when a
child asks (``ADULT_AGREES_FOR_A_CHILD``). No type asks the person who asked anything by default
(the owner's decision of 2026-11-15: what is asked for and understood is done): a yes before an
action comes from the judge, from Domo, or from a rule the owner set himself in his rights.

The tables are read-only. The barrier takes them as a parameter (``PolicyTables``) so that tests
can try others, and the core hands it ``DEFAULT_POLICY`` unless told otherwise.

The catalog is open (spec 5.3): the kinds of action Domo opens for the tools he makes
(``made.<name>``, ``OpenKind``) live in the database, and ``policy.open_catalog.load_policy`` lays
them over these tables once per turn; they are then known and allowed like the code's, and the
owner's rights speak of them the same way. Such an action is never shown and never judged
(``is_silent``, ``needs_the_judge``: by its prefix, ``MADE_PREFIX``).

Each action type also declares the facts its description carries (``ACTION_FACTS``), and a few
action types are the owner's whatever any table says (``OWNER_ONLY_ACTIONS``): the foundation of
the rights, which nothing given by the owner can move. Handing the owner's role over
(``household.transfer_ownership``) is one of them. The owner's list of successors
(``successors.*``) is another: seen and changed by the owner alone.

The owner's rights (``policy.rights``) sit on top of these tables: what they do not say, the
catalog says. A limit (``Limit``) is defined here, in the base module, because the catalog's own
limits are written with it too; ``policy.rights`` re-exports it (this module never imports that
one).

The catalog's own limits (``DEFAULT_LIMITS``, spec 5.11) are rules of the product the owner may
replace: an invitation the assistant judged should wait for the owner's yes waits for it. A right
that carries a limit replaces the default one, and a right whose limit is ``"allow"`` lifts it. No
limit of the catalog reads the length of a stay (audit POL-5): a stay of any length, or with no
end, is the owner's order or the assistant's judgement; a limit the owner writes himself on its
``days`` still applies before any judgement. What stays in the tools is technical or about
integrity: lengths of texts, names that tell people apart, the owner's role never given, the owner
never removed and never leaving (they hand their role over first).

Leaving the household oneself (``household.leave``, spec 5.9) is every person's, the guest's too (a
guest may leave early); an adult agrees first for a child. Removing someone else
(``household.remove``) is a gesture on people, in no role (``PEOPLE_GESTURES``): the assistant's
and the owner's, and whoever the owner gives the right to.

Each member's own keys (0.6.0, spec 4.2, 4.3) are theirs: adding one (``key.add``), giving it
again (``model.rekey``), removing it (``key.remove``), saying who else may use it (``key.share``)
and setting the firm limit of what it spends (``spending.limit``, spec 4.7) are every member's, and
a guest brings a key of their own too (all but sharing it). "Their own" is the tools'
(``tools.model_key``, ``tools.key_tools``): only the one who gave a key gives it again, removes it,
shares it or limits it. A child adds, shares or limits a key, and gives one again, with an adult's
agreement; removing their own key needs nobody's. The rules of the house on the keys
(``keys.house``: who is on the general key and who brings their own, what an invitee uses, whether
the general key serves to ask someone without a key for one, and the limits of the general key) are
the owner's by default, judged, and can be given like any right (``tools.keys_house``).

What Domo costs (``spending.show``, spec 4.8) is everyone's to ask, the guest's too: each person
their own spending, and what the keys they gave cost, person by person (``tools.spending_tools``);
it changes nothing (the judge is not asked). Giving the admin key of one's own key, with which the
real spending is read (``key.admin``), is every member's, with an adult's agreement for a child,
like adding a key; not a guest's (an admin key belongs to an organization). Reading again the
catalog of models, the euro's rate and the models of the key of a request (``models.refresh``)
is every member's, and changes nothing.

Domo's own choices of models (``models.choose``, spec 4.6) are every member's to ask for, and a
step up to a stronger model for the rest of a request (``models.step_up``) everyone's, the guest's
too: both change nothing in the household (the judge is not asked). The level of a model
(``models.set_level``) is the owner's word: it decides which models may judge a yes, so it is the
owner's alone, and judged.

A note may be left for a guest who is there: it has no default limit (the owner may still write
one, ``recipient_role`` is among the facts of a note). The notes a guest received are erased when
they leave (``notes.upkeep``), unless their author keeps them until a day, a year ahead at most
(``note.keep``, spec 5.10). Domo tells everyone the notes left for them, a guest too, and shows
their exact words when asked (``note.told``, ``note.show``, spec 4.8): neither changes anything in
the household, and neither has a fact (never the words of a note).

The texts people read (0.7.0, spec 4.7) are each person's own: everyone, a child and a guest too,
finds their texts (``texts.find``, which changes nothing), has them written again with an
indication (``texts.rewrite``), brings an earlier version back (``texts.restore``), changes their
language (``texts.language``) and the note Domo keeps on how to write to them (``texts.note``),
judged, and with nobody's agreement: they change only their own pages ("their own" is the tools',
``tools.text_tools``). The owner's own language moves the house's, which visitors read: the facts
of a change of language say it (``house_follows``). Changing someone else's texts, writing note or
language (``texts.for_someone``: one of ``TEXT_CHANGES``) is nobody's by default, the owner's
neither (socle 8): only a right of the owner opens it, and its facts say whom it is about. The
version of a language, which visitors read (``texts.language_version``), is the owner's to have
written again by default, judged, and given like any right. Domo's choices on writing
(``writing.choose``: what is written at once, and whether he speaks first when someone opens their
page) are every member's to ask for, like his choices of models, and change nothing in the
household.

A person may entrust a device to Domo (spec 6.5): the owner and the adults by default
(``device.entrust``), a child or a guest only by a right the owner gives. Everyone sees their own
devices (``device.list``, which changes nothing) and takes their own back (``device.remove``); the
owner takes back anyone's, and nobody else does (the tool's own refusal, whatever the rights say).

Inviting someone (``household.invite``), and every other gesture on the people of the house and
their roles (``PEOPLE_GESTURES``), is in no role (ruling 14): the owner's and Domo's, and the owner
may open one to a precise person by a right. Domo sends an invitation to the owner first when it
seems to deserve it; the call says so (``owner_first``, one of its facts), and the catalog's limit
of inviting then asks the owner. The roles Domo makes start from a built-in base and never hold
what is above their ceiling (``ROLE_CEILING``); a child-marked person gets nothing reserved to
adults from a role (``adults_only``). Who may agree for a child is a right to read
(``CHILD_AGREE``), the adults' by default.

Domo's memory (0.9.0, spec 6.2) is in everyone's turn, a guest's too: keeping, changing,
forgetting and looking up a memory (``memory.*``) are his judgement, never judged (they are with
what changes nothing) and never shown (``SILENT_ACTIONS``: no card, no line in the history).
So are the plans he sets for himself (``plan.*``, spec 6.9): setting, changing and cancelling one
of the person's plans is his judgement, in anyone's turn; what he does once it is due is checked
then, with the rights of that day. So is the name a person calls Domo by
(``profile.name_domo``, spec 6.7): everyone gives him one for themselves, a guest too, and so
are the names each person uses for the others (``people.nickname_*``). So is
his look at what reached someone of his replies (``delivery.look``, spec 6.10): in anyone's turn,
and about anyone else only the fact, never a word. So are the facts he notes about what a person's
word was worth, his look at a score and his corrections (``trust.*``, trust per person, spec 6.3
and 6.5): who may note what, in which turn, is the tools' own guard (``tools.trust_tools``), and
nothing of the policy reads a fact or a score.

The tools are Domo's own (2026-11-18). Every type has its four risks (``Risks``, ``CODE_RISKS``;
an open kind carries its own, ``risks_of``): they are given to whoever weighs an action, never a
reason for the code to refuse Domo anything. What a running program asks the core has types of
its own (``PROGRAM_TYPES``: everyone's, never judged nor shown), and the code's types a program may
do itself are few (``PROGRAM_ACTIONS``). Domo is a subject of rights for what he does on his own
for the house (``roles.domo_actor``): every type but the owner's own and, by default only, the
socle's secrets, spending limits, who may do what and what is nobody's (``DOMO_NOT_BY_DEFAULT``;
``DOMO_ACTIONS``, in ``PolicyTables.domo``). The owner's rights narrow it, or open to him
what is nobody's; secrets, spending limits and who may do what stay closed to him for now
(``CLOSED_TO_DOMO``, ruling R22).

Going out (step 2, spec 13.3 to 13.5, 2026-11-22): opening and closing an address, asking for an
account and looking at either are Domo's acts (``OUTWARD_ACTIONS``), his on his own; closing and
looking are never shown (``SILENT_OUTWARD_ACTIONS``), opening and asking are, so that the owner may
have himself asked first (spec 13.10). The owner and the adults have them too, a child asks for
an account of their own, sees theirs and removes theirs, a guest only sees theirs. Removing an
account is every member's (whoever gave it, or the owner: "their own" is the tools'), and Domo's
on his own for one that no longer serves (the owner, 2026-11-24); sharing a house account
(``account.share``) is the owner's alone (``OWNER_ONLY_ACTIONS``).
A port of the machine that hosts Domo (``net.open_host``)
is the owner's by default, and a right of the owner may give it to Domo. What a program sends
out (``program.out``) is everyone's by default, never shown, and judged by the rights of whom its
launch runs for.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final, Literal

from domo_core.policy.actions import FactValue
from domo_core.policy.roles import Role

# What kind of value a fact is. Every fact is stored as a simple value (``actions.FactValue``):
# ``person`` is the text ``person:<uuid>``, ``role`` the name of a role, ``date`` a day written
# ``YYYY-MM-DD``; a ``number`` is a whole number (``None`` when it does not apply).
FactType = Literal["text", "number", "boolean", "person", "role", "date"]

# What may be changed of someone's texts (``texts.for_someone``): their texts written again, the
# earlier version brought back, their language, the note Domo keeps on how to write to them.
TextChange = Literal["rewrite", "restore", "language", "note"]
TEXT_CHANGES: Final[tuple[TextChange, ...]] = ("rewrite", "restore", "language", "note")

# The action types the barrier knows, in the order the catalog lists them (an action of any other
# type is refused).
_KNOWN: Final[tuple[str, ...]] = (
    "household.list",
    "profile.rename",
    # A note is just a message, inside the household (the owner, 2026-10-31).
    "note.leave",
    # Finding again, and taking back, the notes one left oneself: undoing a message of one's own.
    "note.list_sent",
    "note.withdraw",
    # Keeping the notes one left for a guest beyond their departure (spec 5.10): the author's own
    # words, kept or not as they decide.
    "note.keep",
    # A note left for the person: Domo says he told it, and shows its exact words (spec 4.8).
    "note.told",
    "note.show",
    "household.cancel_invitation",
    "household.invite",
    # A new link for an invitation that waits (the old one stops working): its rights are those of
    # inviting (``RIGHTS_OF``).
    "household.invite_again",
    # The link of an invitation that waits, shown once to the one it waits for, in words (the
    # button of their card does the same): it changes nothing, and is never his own idea.
    "household.show_link",
    "household.change_role",
    "household.remove",
    # Leaving oneself: nothing is erased and they can be brought back, but it closes their account.
    "household.leave",
    "household.bring_back",
    "household.transfer_ownership",
    # Each member's own keys: give one again, add one, remove one, say who else may use it, limit
    # what it spends. The rules of the house on the keys are the owner's by default.
    "model.rekey",
    "key.add",
    "key.remove",
    "key.share",
    "spending.limit",
    "keys.house",
    # The admin key of one's own key, which reads its real spending, and what Domo costs.
    "key.admin",
    "spending.show",
    # Domo's own choices of models, and a step up for the rest of a request: nothing changes in
    # the household. The level of a model is the owner's word. Reading the catalog, the euro's
    # rate and the models of a key again changes nothing either.
    "models.choose",
    "models.step_up",
    "models.set_level",
    "models.refresh",
    "confirmation.remind",
    "confirmation.expire",
    "confirmation.require_explicit",
    "confirmation.amend",
    # Having Domo ask more for the person's next action changes nothing by itself.
    "confirmation.ask_before",
    # Domo saying that his next action is his own idea changes nothing by itself: that action is
    # judged with his own rights.
    "confirmation.own_idea",
    # What a person agreed about being asked again before what cannot be undone: changes nothing
    # in the household, and is never shown.
    "confirmation.agree_asking",
    "policy.show",
    "policy.update",
    # The name the whole house calls Domo by: the owner's alone, in a turn of their own.
    "house.name_domo",
    # The owner changes the rights in words (spec 5.6): a draft is written freely, and applying
    # it is judged like any action.
    "rights.propose",
    "rights.apply",
    # The owner's ordered list of who takes over: read freely, and a successor's death is only
    # told (nobody is asked to confirm a bereavement).
    "successors.show",
    "successors.set",
    "successors.note_death",
    # Putting a successor back in the running gives a place on the list again.
    "successors.undo_death",
    # Several of the person's tools in order, with one yes (spec 4), judged as a whole. Continuing
    # the rest of a chain that stopped runs under the yes already given to it (spec 4.4), each step
    # checked again in its turn.
    "chain.run",
    "chain.continue",
    # The texts people read (spec 4.7): each person's own, someone else's (nobody's by default),
    # the version of a language (the owner's by default), and Domo's choices on writing.
    "texts.find",
    "texts.rewrite",
    "texts.restore",
    "texts.language",
    "texts.note",
    "texts.for_someone",
    "texts.language_version",
    "writing.choose",
    # A device a person entrusts to Domo (spec 6.5): the owner and the adults entrust one,
    # everyone sees their own and takes their own back (the owner anyone's).
    "device.entrust",
    "device.list",
    "device.remove",
    # What Domo keeps, changes, forgets and looks up in his memory (0.9.0, spec 6.2): everyone's
    # turn may, his judgement decides; nothing is judged nor shown.
    "memory.keep",
    "memory.change",
    "memory.forget",
    "memory.search",
    # The plans Domo sets for himself (0.9.0, spec 6.9): everyone's turn may, never judged nor
    # shown; what he does once one is due is checked then.
    "plan.set",
    "plan.change",
    "plan.cancel",
    # The name each person calls Domo by (0.9.0, spec 6.7): theirs alone, never judged nor shown.
    "profile.name_domo",
    # The names each person uses for the others ("Sam" for Samuel, 0.9.0, spec 6.7): theirs
    # alone, never judged nor shown.
    "people.nickname_keep",
    "people.nickname_forget",
    # What reached someone of Domo's replies (0.9.0, spec 6.10): everyone's turn may, never
    # judged nor shown; about someone else, only the fact.
    "delivery.look",
    # What a person's word was worth (0.9.1, trust per person): everyone's turn may, never judged
    # nor shown; who may note what is the tools' own guard.
    "trust.note",
    "trust.look",
    "trust.correct",
    # What went wrong (Domo judges first, spec 9): his noting of a complaint about what he did, and
    # his marking that he asked what was wrong: everyone's turn may, never judged nor shown.
    "learning.note",
    "learning.asked",
    # The tools Domo makes for himself (0.10.0, spec 5.5 to 5.7): opening the build of one,
    # changing, removing, looking at and finding one, and giving one a time to launch at: everyone's
    # turn may, never judged nor shown. A tool is Domo's (2026-11-18), nobody's in particular.
    # The bounds of his tools are his settings (2026-11-21): for the house, and for one tool. He
    # keeps them up on his own (2026-11-21): going back to a version, checking one's examples.
    "tool.build",
    "tool.change",
    "tool.remove",
    "tool.look",
    "tool.find",
    "tool.launch_when",
    "tools.settings",
    "tool.set",
    "tool.back",
    "tool.check",
    # What a running program asks the core (the tools are Domo's own, 2026-11-18): beginning an
    # operation, reading and writing its base, running later, waking Domo, and a program run by
    # hand. Everyone's, never judged nor shown.
    "program.begin",
    "program.base.read",
    "program.base.write",
    "program.later",
    "program.wake",
    "program.run",
    # Going out (step 2, spec 13.3 to 13.5): an address Domo opens or closes for a tool, a port
    # of the machine that hosts him (a right apart, the owner's by default), his look at what is
    # open; an account he asks someone for, one removed, his look at them, and a house account the
    # owner opens to guests or closes to Domo alone. And what a program sends out through the
    # gatekeeper, judged with the rights of whom it runs for.
    "net.open",
    "net.close",
    "net.open_host",
    "net.look",
    "account.ask",
    "account.remove",
    "account.look",
    "account.share",
    "program.out",
    # The agreements that last (2026-11-18, spec 5.3): an operation of one of Domo's tools that
    # passes without a yes, within its bounds, for the person who keeps it (or the house, by the
    # owner); and withdrawing one. Everyone's, a guest's too, for themselves; judged and shown.
    "agreement.keep",
    "agreement.drop",
    # The voice (spec 6.2, 6.5, 6.9): agreeing, changing and removing the spoken phrase, stopping
    # the listening, and "that was not me". Everyone's, for themselves; only stopping is silent.
    "proof.phrase_agree",
    "proof.phrase_change",
    "proof.phrase_remove",
    "voice.stop_listening",
    "voice.unlearn",
    # Domo's own reading of how someone speaks (Task 23): a weak clue of his, never shown.
    "voice.manner_note",
    # Setting something for the whole house: a timed launch, a standing agreement (Task 27,
    # ruling R17). It changes nothing by itself: the tools read it (``HOUSE_SET``).
    "house.set",
    # Agreeing for a child (``CHILD_AGREE``): a right to read, no tool of its own.
    "child.agree",
    # The people of the house (the records, their links, their merges, a mistake of access), the
    # roles the assistant makes, the access he promises, and a new invitation link sent: in no
    # role (``PEOPLE_GESTURES``). Looking at them changes nothing (``PEOPLE_READ_ACTIONS``).
    "people.open",
    "people.complete",
    "people.look",
    "people.erase",
    "people.forget",
    "people.forget_me",
    "people.link",
    "people.unlink",
    "people.merge",
    "people.split",
    "people.mistake",
    "role.create",
    "role.change",
    "role.delete",
    "role.look",
    "access.promise",
    "access.promise_keep",
    "access.promise_drop",
    "access.promises_look",
    "household.send_link",
    # The owner's directives on the people of the house (``OWNER_DIRECTIVE_ACTIONS``).
    "people.directive_set",
    "people.directive_lift",
    "people.directives_look",
)
KNOWN_ACTIONS: Final[frozenset[str]] = frozenset(_KNOWN)
# What nobody may do by default, the owner neither, nor Domo (``DOMO_NOT_BY_DEFAULT``):
# changing someone else's texts, writing note or language (spec 4.7, socle 8). Only a right of the
# owner opens it.
_NOBODYS_ACTIONS: Final = frozenset({"texts.for_someone"})
# Each person's own texts, writing note and language: everyone's, a guest's too.
_OWN_TEXT_ACTIONS: Final = frozenset(
    {"texts.find", "texts.rewrite", "texts.restore", "texts.language", "texts.note"}
)
# The notes left for the person, told by Domo and their exact words shown: everyone's, a guest's
# too (a guest receives notes).
_RECEIVED_NOTE_ACTIONS: Final = frozenset({"note.told", "note.show"})
# Seeing one's own devices and taking one back (whose it is is the tool's to check): everyone's,
# a guest's too. Entrusting one (``device.entrust``) is the adults' and the owner's.
_OWN_DEVICE_ACTIONS: Final = frozenset({"device.list", "device.remove"})
# What Domo does with his memory, in anyone's turn (spec 6.1: everyone writes, his judgement
# decides what he keeps).
MEMORY_ACTIONS: Final = frozenset(
    {"memory.keep", "memory.change", "memory.forget", "memory.search"}
)
# The plans Domo sets for himself, in anyone's turn (spec 6.9: each person's own plans).
PLAN_ACTIONS: Final = frozenset({"plan.set", "plan.change", "plan.cancel"})
# The name the person calls Domo by, and the names they use for the others, theirs alone
# (spec 6.7).
NAME_ACTIONS: Final = frozenset(
    {"profile.name_domo", "people.nickname_keep", "people.nickname_forget"}
)
# Domo's look at what reached someone of his replies, in anyone's turn (spec 6.10).
DELIVERY_ACTIONS: Final = frozenset({"delivery.look"})
# The facts Domo notes about what a person's word was worth, his look at a score and his
# corrections, in anyone's turn (trust per person, spec 6.3 and 6.5).
TRUST_ACTIONS: Final = frozenset({"trust.note", "trust.look", "trust.correct"})
# What Domo notes of a complaint, and his marking that he asked what was wrong, in anyone's turn
# (spec 9.1 and 9.3): who may be asked, and in which turn, is the tools' own guard
# (``tools.learning_tools``).
LEARNING_ACTIONS: Final = frozenset({"learning.note", "learning.asked"})
# What a person agreed with Domo about being asked again before an action that cannot be undone,
# after their answer to one, theirs alone (Task 26): everyone's, a guest's too, and his.
ASKING_AGREED: Final = "confirmation.agree_asking"
# The tools Domo makes, in anyone's turn (0.10.0, spec 5.5): they are all his, any of them
# may be changed, removed, looked at or timed in anyone's turn, and their bounds are his settings
# (2026-11-21, spec 12.1: ``tools.settings`` for the house, ``tool.set`` for one tool), and he keeps
# them up (spec 12.3: ``tool.back`` to an earlier version, ``tool.check`` against its examples).
TOOL_ACTIONS: Final = frozenset(
    {
        "tool.build",
        "tool.change",
        "tool.remove",
        "tool.look",
        "tool.find",
        "tool.launch_when",
        "tools.settings",
        "tool.set",
        "tool.back",
        "tool.check",
    }
)
# What Domo does to look at his tools: the judge is not asked about it. Checking one runs its
# examples as dry runs, which keep nothing.
TOOL_READ_ACTIONS: Final = frozenset({"tool.look", "tool.find", "tool.check"})
# The types of what a running program asks the core (2026-11-18): in anyone's launch, a guest's
# too, judged by the barrier with the rights of whom it runs for (or Domo's, for the house),
# never by the judge, and never shown. A program never speaks to a person: waking Domo is its
# only path to one.
PROGRAM_BEGIN: Final = "program.begin"
PROGRAM_READ: Final = "program.base.read"
PROGRAM_WRITE: Final = "program.base.write"
PROGRAM_LATER: Final = "program.later"
PROGRAM_WAKE: Final = "program.wake"
PROGRAM_RUN: Final = "program.run"
PROGRAM_TYPES: Final = frozenset(
    {PROGRAM_BEGIN, PROGRAM_READ, PROGRAM_WRITE, PROGRAM_LATER, PROGRAM_WAKE, PROGRAM_RUN}
)
# What a running program's requests are judged on (``madetools.means``): there the owner's limit
# (with what was spent), his "always ask" and an approver are read, without a model
# (``policy.program``, spec 5.3, 5.8). A program run by hand (``program.run``) is Domo's act.
# What a program sends out through the gatekeeper is one of them (step 2, spec 13.10), but not one
# of ``PROGRAM_TYPES``, which every launch has whatever the rights say: going out is judged by the
# rights of whom the launch runs for, like any of their actions. It is everyone's by default, a
# guest's too, and never shown.
PROGRAM_OUT: Final = "program.out"
PROGRAM_REQUESTS: Final = (PROGRAM_TYPES - {PROGRAM_RUN}) | {PROGRAM_OUT}
# The code's types a program may do itself (``act``): only giving a tool a time to launch at.
# Every other type of the code speaks to a person, touches the socle or is Domo's own mind.
PROGRAM_ACTIONS: Final[frozenset[str]] = frozenset({"tool.launch_when"})
# The agreements that last: everyone keeps and withdraws their own (the tools' own guard says
# whose; one for the house is set by whom the rights allow ``HOUSE_SET``).
AGREEMENT_ACTIONS: Final = frozenset({"agreement.keep", "agreement.drop"})
# The voice (spec 6.2, 6.5, 6.9), in anyone's turn, a guest's too, for themselves: agreeing,
# changing and removing the spoken phrase (``proof.phrase_*``, the tools' own guard says who and
# when), stopping the listening (never shown) and "that was not me" (``voice.unlearn``).
VOICE_ACTIONS: Final = frozenset(
    {
        "proof.phrase_agree",
        "proof.phrase_change",
        "proof.phrase_remove",
        "voice.stop_listening",
        "voice.unlearn",
    }
)
# Domo's own reading of how someone speaks (Task 23), in anyone's turn: a weak clue of his that
# changes nothing anyone has nor how sure the code is; never judged nor shown.
MANNER_NOTE: Final = "voice.manner_note"
# Setting something for the whole house, a timed launch or a standing agreement (Task 27, ruling
# R17): the owner's by default, and a right may give it to anyone like any type. The tools read it
# (``tools.context.ToolContext.may_set_for_the_house``): asked by someone who may not, the call
# waits for the owner's yes. It changes nothing by itself and is never a tool of its own.
HOUSE_SET: Final = "house.set"
# Agreeing for a child (``confirmations.who_agrees``): the owner's and the adults' by default, and
# a right may give or deny it to anyone, or a role the assistant makes may hold it, like any type.
# It changes nothing by itself and is never a tool of its own.
CHILD_AGREE: Final = "child.agree"
# What the assistant reads of the people of the house, the roles he made and the access he
# promised: the first is everyone's (the tool limits a person to themselves), the others the
# owner's and his.
PEOPLE_READ_ACTIONS: Final[frozenset[str]] = frozenset(
    {"people.look", "role.look", "access.promises_look"}
)
# The owner's directives on the people of the house, which the code applies before any
# judgement: setting one, lifting one, looking at them. The owner's alone.
OWNER_DIRECTIVE_ACTIONS: Final[frozenset[str]] = frozenset(
    {"people.directive_set", "people.directive_lift", "people.directives_look"}
)
# What gives someone an access, or a way to one, and what makes someone new: the owner's
# directives on people (``people.directives``) are applied to these before any judgement. A call
# of the assistant's that the owner's words let through says which directive (the fact
# ``exception_of``).
GIVES_ACCESS: Final[frozenset[str]] = frozenset(
    {
        "household.invite",
        "household.invite_again",
        "household.change_role",
        "household.bring_back",
        "household.send_link",
        "access.promise_keep",
    }
)
MAKES_SOMEONE_NEW: Final[frozenset[str]] = frozenset({"people.open", "household.invite"})
# The fact that names the directive a call is an exception to: the assistant's, never what an
# owner's right is about (the rights writer leaves it out of the facts of an example).
EXCEPTION_FACT: Final = "exception_of"
# The fact that says the record that gets an access is the person whose turn it is: the judge is
# shown it beside "the idea was born of their words" (``people.records.give_access``), since saying
# who one is gives nothing by itself. On every type that gives an access.
SERVES_THE_SPEAKER_FACT: Final = "serves_the_speaker"
# The fact that says the access taken away is one the assistant gave (``household.remove``): his
# to take back when a stay ends or he was wrong, and his judge's to weigh. On an invitation
# cancelled or given a new link: the assistant made it.
GIVEN_BY_DOMO_FACT: Final = "given_by_domo"
# The fact that says the access given wakes one that slept at the record's next visit
# (``people.records.returns_now``: its stay was over and its access returns): the visit is the
# reason, and the judge weighs it. On the types that give an access again.
RETURNS_FACT: Final = "returns"
# The facts of an action that are the assistant's and his judge's to weigh, never what an owner's
# right is about: the rights writer leaves them out of the facts of an example.
JUDGED_FACTS: Final[frozenset[str]] = frozenset(
    {EXCEPTION_FACT, SERVES_THE_SPEAKER_FACT, GIVEN_BY_DOMO_FACT, RETURNS_FACT}
)
# Going out (step 2, spec 13.3 to 13.5). Domo's own acts on the addresses and the accounts, like
# his acts on his tools: opening and closing an address, his look at what is open, asking someone
# for an account and his look at the accounts. He has them on his own by default; looking changes
# nothing. Opening an address towards the internet "leaves the house"
# (``outward.literal``, computed at the call from the address).
OUTWARD_ACTIONS: Final = frozenset(
    {"net.open", "net.close", "net.look", "account.ask", "account.look"}
)
OUTWARD_READ_ACTIONS: Final = frozenset({"net.look", "account.look"})
# Of them, what is never shown: closing an address and looking (ruling R8). Opening an address and
# asking for an account are shown and judged: the owner may write "ask me first" on them (spec
# 13.10), which a rule on a type never shown could not say.
SILENT_OUTWARD_ACTIONS: Final = OUTWARD_READ_ACTIONS | {"net.close"}
# Opening a port of the machine that hosts Domo is a right apart (spec 13.10): the owner's by
# default, and not Domo's; but the owner may give it to him (2026-11-22), so it is not closed to
# him (``CLOSED_TO_DOMO``).
NET_OPEN_HOST: Final = "net.open_host"
# What an account handles beyond asking for one: removing it (whoever gave it, or the owner: the
# tools' own guard), and the owner opening a house account to guests or closing it to Domo alone
# (``account.share``, the owner's by default). Sharing is never Domo's; removing an account that
# no longer serves is his on his own (the owner, 2026-11-24), though he neither creates nor
# replaces one.
_ACCOUNT_SECRET_TYPES: Final = frozenset({"account.remove", "account.share"})
# What the owner and the adults have of going out: everything but a port of the machine that hosts
# Domo and sharing a house account. A child asks for an account of their own, sees it and
# removes it (spec 13.4: whoever gave it); a guest only sees theirs.
_ADULT_OUTWARD_ACTIONS: Final = (OUTWARD_ACTIONS | _ACCOUNT_SECRET_TYPES) - {"account.share"}
_CHILD_OUTWARD_ACTIONS: Final = frozenset({"account.ask", "account.look", "account.remove"})
_GUEST_OUTWARD_ACTIONS: Final = frozenset({"account.look"})
# What everyone has, whatever their role: their own texts, the notes left for them, their devices,
# Domo's memory, his plans, his look at what reached them, what he notes of a person's word,
# his own tools, what their programs ask and what they send out, and his saying that his next
# action is his own idea.
_EVERYONES_ACTIONS: Final = (
    _OWN_TEXT_ACTIONS
    | _RECEIVED_NOTE_ACTIONS
    | _OWN_DEVICE_ACTIONS
    | MEMORY_ACTIONS
    | PLAN_ACTIONS
    | NAME_ACTIONS
    | DELIVERY_ACTIONS
    | TRUST_ACTIONS
    | LEARNING_ACTIONS
    | TOOL_ACTIONS
    | PROGRAM_TYPES
    | {PROGRAM_OUT}
    | AGREEMENT_ACTIONS
    | VOICE_ACTIONS
    | {MANNER_NOTE}
    | {"confirmation.own_idea", ASKING_AGREED}
    # What the assistant knows of a person, and forgetting it, asked by that person (the tools
    # limit a person to themselves).
    | {"people.look", "people.forget_me"}
)

# The actions of every member of the household, whatever their role: reading who is in the
# household, renaming oneself, leaving a note and finding again, taking back or keeping the notes
# one left, managing the person's own pending requests, doing several of their tools in order (a
# chain, and the rest of their own chain that stopped), leaving the household themselves,
# Domo's choices of models with a step up for the rest of a request, their own keys, their own
# texts, and Domo's choices on writing. ("Own" and "in this conversation" are enforced by the
# tools themselves.) And what is everyone's: their own texts, and the notes left for them. And
# asking for an account of their own, and seeing theirs.
_MEMBER_ACTIONS = _EVERYONES_ACTIONS | frozenset(
    {
        "household.list",
        "profile.rename",
        "note.leave",
        "note.list_sent",
        "note.withdraw",
        "note.keep",
        "confirmation.remind",
        "confirmation.expire",
        "confirmation.require_explicit",
        "confirmation.amend",
        "confirmation.ask_before",
        "chain.run",
        "chain.continue",
        "household.leave",
        "household.show_link",
        "models.choose",
        "models.step_up",
        "model.rekey",
        "key.add",
        "key.remove",
        "key.share",
        "spending.limit",
        "key.admin",
        "spending.show",
        "models.refresh",
        "writing.choose",
        *_CHILD_OUTWARD_ACTIONS,
    }
)

# What an adult may do besides every member's: entrust a device to Domo, and agree for a child
# (``CHILD_AGREE``). And going out, but a port of the machine that hosts Domo and sharing a house
# account (``_ADULT_OUTWARD_ACTIONS``). Inviting someone is in no role (``PEOPLE_GESTURES``,
# ruling 14): the owner and the assistant invite, and the owner may open it to one person.
_ADULT_ACTIONS: Final = _ADULT_OUTWARD_ACTIONS | frozenset({"device.entrust", CHILD_AGREE})

_ALLOWED_ACTIONS: dict[Role, frozenset[str]] = {
    # The owner may do everything in the catalog, but what is nobody's by default.
    "owner": KNOWN_ACTIONS - _NOBODYS_ACTIONS,
    # Adults and children differ by who approves what they ask for (see ``barrier.decide``), and
    # by what only an adult may do (``_ADULT_ACTIONS``).
    "adult": _MEMBER_ACTIONS | _ADULT_ACTIONS,
    "child": _MEMBER_ACTIONS,
    # A guest may only rename themselves, leave early, have Domo take a stronger model for the
    # rest of a request, bring a key of their own (add it, give it again, remove it, limit it), ask
    # what they cost, change their own texts, writing note and language, hear the notes left for
    # them, and see their own accounts.
    "guest": _EVERYONES_ACTIONS
    | _GUEST_OUTWARD_ACTIONS
    | frozenset(
        {
            "profile.rename",
            "household.leave",
            "models.step_up",
            "key.add",
            "model.rekey",
            "key.remove",
            "spending.limit",
            "spending.show",
        }
    ),
}

# The facts each action type is described with (spec 5.4): their names and kinds. The tool of the
# action writes them (``DescribedAction.facts``); a limit can only be about these. An action with
# no fact has none (``{}``). ``days`` is the days of a guest's stay, ``None`` for anyone else and
# for a stay with no end (``no_end``). ``returns`` (``RETURNS_FACT``) is the judge's.
# ``is_self`` says that the person acted on is the one who asks. Leaving says the role the person
# who leaves has now (``current_role``): they act on themselves alone.
# ``owner_first`` says that Domo judged the owner should say yes before the invitee joins (the
# call's own word, false when it does not say).
_ON_A_RECORD: Final[dict[str, FactType]] = {
    "has_account": "boolean",
    "state": "text",
    "made_role": "text",
}
_FACTS: dict[str, dict[str, FactType]] = {
    "household.invite": {
        "role": "role",
        "days": "number",
        "no_end": "boolean",
        "display_name": "text",
        "owner_first": "boolean",
    },
    # The invitation's own role, days and name: a right on inviting reads them the same way. A new
    # link never sends the invitation to the owner again (``owner_first`` is always false).
    "household.invite_again": {
        "role": "role",
        "days": "number",
        "no_end": "boolean",
        "display_name": "text",
        "owner_first": "boolean",
        GIVEN_BY_DOMO_FACT: "boolean",
    },
    # A role, a removal and a return act on a record, with or without an account: ``made_role`` is
    # the name of the role made for the household the action gives (a removal: the one the record
    # carries), ``None`` for a built-in role alone; ``state`` where the record stands now
    # (``known``, ``has_access``, ``gone``...).
    "household.change_role": {
        "person": "person",
        "current_role": "role",
        "role": "role",
        "days": "number",
        "no_end": "boolean",
        RETURNS_FACT: "boolean",
        # Someone passing by who now lives here, and what it changes (the owner's answer of
        # 2026-11-27): an owner's rule may read them ("ask me before someone comes to live here").
        "comes_to_live": "boolean",
        "off_the_guests_key_rule": "boolean",
        "may_then_agree_for_a_child": "boolean",
        **_ON_A_RECORD,
    },
    # ``given_by_domo``: the access taken away is one the assistant gave (the last access given to
    # the record was his act), as when a stay ends or he was wrong; the judge weighs it.
    "household.remove": {
        "person": "person",
        "current_role": "role",
        "is_self": "boolean",
        GIVEN_BY_DOMO_FACT: "boolean",
        **_ON_A_RECORD,
    },
    "household.leave": {"current_role": "role"},
    # A promise kept gives the role and the stay it names: a limit of the owner's reads them as it
    # does for a role given.
    "access.promise_keep": {"role": "role", "days": "number"},
    "household.bring_back": {
        "person": "person",
        "role": "role",
        "days": "number",
        "no_end": "boolean",
        RETURNS_FACT: "boolean",
        **_ON_A_RECORD,
    },
    # ``from`` is the person who hands the role over, ``to`` the one who gets it; the roles are
    # the ones the database holds when the call is described.
    "household.transfer_ownership": {
        "from": "person",
        "to": "person",
        "requester_role": "role",
        "to_role": "role",
    },
    # ``given_by_domo``: the invitation is the assistant's, which whoever holds the right changes.
    "household.cancel_invitation": {"role": "role", GIVEN_BY_DOMO_FACT: "boolean"},
    # ``loses_access``: forgetting takes away the access of someone who lives here
    # (``people.forget.loses_access``): the judge weighs it, and a limit of the owner's may read it.
    "people.forget": {"loses_access": "boolean"},
    "people.forget_me": {"loses_access": "boolean"},
    # The two records a link is between: ``from`` is ``relation`` ``to``.
    "people.link": {"from": "person", "to": "person"},
    "people.unlink": {"from": "person", "to": "person"},
    # The two records a merge makes one (the record kept, the one absorbed), and splits again.
    "people.merge": {"kept": "person", "absorbed": "person"},
    "people.split": {"kept": "person", "absorbed": "person"},
    "note.leave": {"recipient": "person", "recipient_role": "role", "length": "number"},
    "note.withdraw": {"recipient": "person", "seen": "boolean"},
    # ``days`` is how many days from today the author keeps the note until.
    "note.keep": {"recipient": "person", "days": "number"},
    # ``count`` is how many successors the new list has; ``name`` is the name of the successor who
    # died (or whose death was noted by mistake), as the owner gave it.
    "successors.set": {"count": "number"},
    "successors.note_death": {"name": "text"},
    "successors.undo_death": {"name": "text"},
    # ``steps`` is how many steps the chain has, ``checked_in_turn`` how many of them are checked
    # in their turn (an earlier step changes whom they act on), ``waits`` how many of them wait for
    # someone's arrival (spec 4.3b).
    "chain.run": {"steps": "number", "checked_in_turn": "number", "waits": "number"},
    # ``own_key`` says that the choices are those of the key of the request, the person's own,
    # and not the household's.
    "models.choose": {"own_key": "boolean"},
    # The key a share or a limit is about, as whoever says yes reads it: its kind (``openai``...)
    # and the name of who gave it (``key_of``).
    "key.share": {"key_kind": "text", "key_of": "text"},
    "spending.limit": {"key_kind": "text", "key_of": "text"},
    "key.admin": {"key_kind": "text", "key_of": "text"},
    # Removing a key: whether it is the last key of the household that works (nobody could talk
    # with Domo after it), and whether it is the general key with no other key of its giver's to
    # take over (whoever uses the general key could not talk with Domo after it).
    "key.remove": {"last_key": "boolean", "general_key_without_heir": "boolean"},
    # Whose texts, writing note or language someone else changes, and their role: never what is
    # written (a note, an indication). Whether a change of one's own language moves the house's
    # too (the owner's, to a language the house does not read yet).
    "texts.for_someone": {"person": "person", "person_role": "role"},
    "texts.language": {"house_follows": "boolean"},
    # Whose device is taken back, and whether it is the one's who asks: never its name.
    "device.remove": {"person": "person", "is_self": "boolean"},
    # Whose spoken phrase is removed, their role, whether it is the one's who asks, and whether
    # the one who asks invited them (the owner, or an adult for a child: 2026-11-27).
    "proof.phrase_remove": {
        "person": "person",
        "person_role": "role",
        "is_self": "boolean",
        "invited_them": "boolean",
    },
    # The operation a program begins: the tool's name and the operation's, the amount it says it
    # spends, in cents (``None`` when it spends nothing), and its four risks.
    "program.begin": {
        "tool": "text",
        "operation": "text",
        "amount_cents": "number",
        "costs": "boolean",
        "irreversible": "boolean",
        "physical": "boolean",
        "outward": "boolean",
    },
    # A program Domo runs by hand (spec 5.4): the facts of a launch of a tool of his
    # (``MADE_FACTS``): a run that carries the yes of a stop says its operation, its amount in
    # cents, that it carries a yes, and whose yes it waits for; any other run has them empty.
    "program.run": {
        "operation": "text",
        "amount_cents": "number",
        "yes": "boolean",
        "approver": "text",
    },
    # An agreement that lasts: the tool and the operation, its bounds (an amount in cents, how
    # many times per day, week or month) and whether it is the house's.
    "agreement.keep": {
        "tool": "text",
        "operation": "text",
        "amount_cents_at_most": "number",
        "times": "number",
        "per": "text",
        "for_house": "boolean",
    },
    "agreement.drop": {"for_house": "boolean"},
    # Going out (step 2, spec 13.3, 13.10): an address by its host and port, the tool it is
    # opened for (``None``: a program run by hand), whether it is a port of the machine that hosts
    # Domo, its zone as written (``outward.literal.literal_zone``: ``house`` or ``internet``) and
    # whether opening it leaves the house (``outward.literal.open_risks``); whether the owner
    # closed it and on which day (``closed_by_owner``, ``closed_on``: what a reopening is weighed
    # against, ruling R24; a rule of his may read them). Never a path.
    "net.open": {
        "host": "text",
        "port": "number",
        "tool": "text",
        "on_host": "boolean",
        "zone": "text",
        "outward": "boolean",
        "closed_by_owner": "boolean",
        "closed_on": "date",
    },
    "net.open_host": {
        "host": "text",
        "port": "number",
        "tool": "text",
        "on_host": "boolean",
        "zone": "text",
        "outward": "boolean",
        "closed_by_owner": "boolean",
        "closed_on": "date",
    },
    "net.close": {"host": "text", "port": "number", "tool": "text"},
    "net.look": {"tool": "text"},
    # An account by its name, the kind of its place (``header``, ``basic``, ``login``), whether it
    # is the house's, who gives it, how many hosts it is bound to and whether one of them is on
    # the internet. Never a value, a header's template, a path or a form's link.
    # Whether the owner removed it last, on which day and at which instant (``removed_by_owner``,
    # ``removed_on``, ``removed_at``: what asking it again is weighed against, Task 27; a rule of
    # his may read the first two).
    "account.ask": {
        "name": "text",
        "place": "text",
        "for_house": "boolean",
        "giver": "person",
        "hosts": "number",
        "outward": "boolean",
        "removed_by_owner": "boolean",
        "removed_on": "date",
        "removed_at": "text",
    },
    "account.remove": {"name": "text", "for_house": "boolean", "of": "person"},
    # What a program sends out (spec 13.3, 13.6): the zone of its address as written
    # (``outward.literal.literal_zone``: ``house`` or ``internet``), whether it does more than read
    # (a method other than GET or HEAD) and whether it names an account. Never a host, a path, a
    # header, a body nor the account's name: they are the program's.
    "program.out": {"zone": "text", "writes": "boolean", "with_account": "boolean"},
    # A house account opened to guests or closed to them (``guests``), opened to Domo on his
    # own or closed to him (``domo``): ``None`` for what the call leaves as it is.
    "account.share": {"name": "text", "guests": "boolean", "domo": "boolean"},
}

# What no table can give to anyone else than the owner: changing the rules (the rights, and the
# settings of ``policy.update``), handing the owner's role over, the list of successors, and the
# level of a model (which decides who may judge a yes). The barrier checks it before the tables
# (``barrier.decide``).
OWNER_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "policy.update",
        "house.name_domo",
        "rights.propose",
        "rights.apply",
        "household.transfer_ownership",
        "successors.show",
        "successors.set",
        "successors.note_death",
        "successors.undo_death",
        "models.set_level",
        # Opening a house account to guests, or closing it to Domo alone (ruling P2): it moves
        # who may use what the house was given, and no right gives it to anyone else.
        "account.share",
        # The owner's directives on the people of the house.
        *OWNER_DIRECTIVE_ACTIONS,
    }
)

# What changes nothing (spec 4.1): reading, drafting, managing one's own requests, Domo's own
# choices of models and a step up (spec 4.6), what Domo costs (spec 4.8), reading the catalog
# again, finding one's own texts, Domo's choices on writing, a note left for the person told
# or shown (0.7.0 spec 4.8), one's own devices seen, Domo's memory and plans (0.9.0, his
# judgement), the name the person calls him by, his look at what reached someone, and what he
# notes of a person's word. The judge is not asked about them.
CHANGES_NOTHING: frozenset[str] = frozenset(
    {
        "household.list",
        "household.show_link",
        "note.list_sent",
        "policy.show",
        "successors.show",
        "rights.propose",
        "confirmation.remind",
        "confirmation.expire",
        "confirmation.require_explicit",
        "confirmation.ask_before",
        "confirmation.own_idea",
        ASKING_AGREED,
        HOUSE_SET,
        CHILD_AGREE,
        *PEOPLE_READ_ACTIONS,
        "people.directives_look",
        "models.choose",
        "models.step_up",
        "models.refresh",
        "spending.show",
        "texts.find",
        "writing.choose",
        "note.told",
        "note.show",
        "device.list",
        *MEMORY_ACTIONS,
        *PLAN_ACTIONS,
        *NAME_ACTIONS,
        *DELIVERY_ACTIONS,
        *TRUST_ACTIONS,
        *LEARNING_ACTIONS,
        *TOOL_READ_ACTIONS,
        *OUTWARD_READ_ACTIONS,
        MANNER_NOTE,
    }
)

# What is never shown (spec 6.2, 6.9, 6.7, 6.10): no card on any channel, live or stored, and no
# line in the history the model reads on later turns, whatever came of the call (done, refused,
# invalid, failed). The action is still stored with its reply, and the model reads its result in
# the turn.
# Of the gestures on people, those that give nothing to anyone: a record opened, completed or
# erased, a link made or undone, a merge split, a mistake of access taken back, a role deleted, a
# promise noted or dropped. The code's own guards hold on them (the owner's directives, the child
# mark, the ceiling of the roles; an end of stay is only ever moved earlier on a stay that runs,
# ``people.records.only_shortens``: a later one gives an access, the judged gestures' part); they
# are never shown, never judged, and an idea of the assistant still reaches them
# (``SILENT_YET_HIS_IDEA``).
_SILENT_PEOPLE_GESTURES: Final[frozenset[str]] = frozenset(
    {
        "people.open",
        "people.complete",
        "people.erase",
        "people.link",
        "people.unlink",
        "people.split",
        "people.mistake",
        # Not ``role.delete``: deleting a role people carry changes what they may do, so it is
        # judged and shown as a change of role is (the owner's answer of 2026-11-27).
        "access.promise",
        "access.promise_drop",
    }
)
SILENT_ACTIONS: Final[frozenset[str]] = (
    _SILENT_PEOPLE_GESTURES
    | PEOPLE_READ_ACTIONS
    | {"people.directives_look", CHILD_AGREE}
    | MEMORY_ACTIONS
    | PLAN_ACTIONS
    | NAME_ACTIONS
    | DELIVERY_ACTIONS
    | TRUST_ACTIONS
    | LEARNING_ACTIONS
    | TOOL_ACTIONS
    | PROGRAM_TYPES
    | SILENT_OUTWARD_ACTIONS
    | {PROGRAM_OUT, ASKING_AGREED, "house.name_domo"}
    | {"voice.stop_listening", MANNER_NOTE}
)


# The facts a judge must have weighed before the action runs: when one of them is true, a judge
# that did not answer asks a plain yes instead of letting it run at once (removing the last key,
# or the general key with nobody to take over: Domo warns that he could no longer answer; an
# account the owner removed, asked again: Task 27).
WARNED_FACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "key.remove": frozenset({"last_key", "general_key_without_heir"}),
        "account.ask": frozenset({"removed_by_owner"}),
    }
)


def warned_since(
    action_type: str,
    now: Mapping[str, FactValue],
    before: Mapping[str, FactValue] | None,
) -> bool:
    """Whether the facts of an action of ``action_type`` as it is described ``now`` hold one of
    its ``WARNED_FACTS`` that was not true ``before``, when the yes or the judge was given what
    it knew (``None``: nothing was known, as for a step checked in its turn). Such an action never
    runs on what was known: a plain yes is asked."""
    known = before or {}
    return any(
        now.get(fact) is True and known.get(fact) is not True
        for fact in WARNED_FACTS.get(action_type, ())
    )


# The silent types whose call an idea of Domo's still reaches (``act_on_my_own``): the call takes
# the mark and is judged and recorded as his: the gestures on people that give nothing to anyone,
# and the look at a record, which his own idea may ask of another's, in anyone's turn (to check for
# a duplicate before opening a record), and the look at the promises of access that wait (the day he
# recognises someone, in anyone's turn, to keep one).
SILENT_YET_HIS_IDEA: Final[frozenset[str]] = _SILENT_PEOPLE_GESTURES | {
    "people.look",
    "access.promises_look",
    # The look at the roles a review offers him on his own (``memory.review``).
    "role.look",
}

# The prefix of the action types of the tools Domo makes (``made.<name>``, the open catalog:
# ``policy.open_catalog``). No type of the code starts with it.
MADE_PREFIX: Final = "made."


def is_made(action_type: str) -> bool:
    """Whether ``action_type`` is the type of a tool Domo made (``MADE_PREFIX``), known or not."""
    return action_type.startswith(MADE_PREFIX)


def is_silent(action_type: str) -> bool:
    """Whether an action of this type is never shown (``SILENT_ACTIONS``), nor a tool Domo made
    (``is_made``: he tells nobody he uses a tool of his own unless they ask)."""
    return action_type in SILENT_ACTIONS or is_made(action_type)


def shown(action_type: str, status: str) -> bool:
    """Whether an action of this type that ended ``status`` is shown (a card, a line of the
    history): any that is not silent (``is_silent``), and a silent one only while it waits for a
    yes (a launch of a tool of Domo's that stopped for one is asked and shown like any request,
    2026-11-18)."""
    return not is_silent(action_type) or status == "waiting"


# The facts of every launch of a tool Domo made (``made.<name>``), declared on each of its kinds
# (``kind_facts``): a launch with a yes (``yes_for``) says the operation the yes is for, its amount
# in cents, that it carries a yes, and whose yes it waits for (``self``, ``adult``, ``owner``); any
# other launch has them empty (``None``, ``False``); and whether the launch is for the house
# (``for_house``: Domo on his own, or a stop of the house's), which the subject of a launch reads
# (``policy.subjects``). An owner's limit on ``made.<name>`` reads them.
MADE_FACTS: Final[Mapping[str, FactType]] = MappingProxyType(
    {
        "operation": "text",
        "amount_cents": "number",
        "yes": "boolean",
        "approver": "text",
        "for_house": "boolean",
    }
)
# The facts of the yes of a launch, or of a program run by hand (``program.run``), that carries
# none.
MADE_FACTS_WITHOUT_A_YES: Final[Mapping[str, FactValue]] = MappingProxyType(
    {"operation": None, "amount_cents": None, "yes": False, "approver": None}
)
# The facts of a launch that carries no yes, for a person: an example of the rights writer that
# leaves them out is about such a launch (``policy.rights_drafts``).
MADE_LAUNCH_WITHOUT_A_YES: Final[Mapping[str, FactValue]] = MappingProxyType(
    {**MADE_FACTS_WITHOUT_A_YES, "for_house": False}
)


# The action types whose rights are another type's (2026-11-16, the owner's trial of 0.9.0): who
# may do it, under which limit, with whose agreement and with which of the owner's rules on asking
# is read on that other type (``policy.rights.RightsTable.resolve``), so that whoever may invite
# someone with a role may give that invitation's link again. A right is never written on such a
# type itself (``policy.rights_writer``): it is written on the type it follows.
RIGHTS_OF: Mapping[str, str] = MappingProxyType({"household.invite_again": "household.invite"})


def rights_type(action_type: str) -> str:
    """The action type whose rights apply to ``action_type`` (``RIGHTS_OF``): itself, for any type
    that follows no other."""
    return RIGHTS_OF.get(action_type, action_type)


# What carries other actions: they are judged inside, step by step, not as such.
JUDGED_WITHIN: frozenset[str] = frozenset({"confirmation.amend", "chain.continue"})

# The types for which a child's request needs an adult's agreement first (spec 4.4): those that
# change the household, and a key of their own that they add, give again, share or limit, or whose
# admin key they give (0.6.0: removing it needs nobody's), and are not the owner's alone. A right
# can open each to a child, and the agreement of an adult then stays, unless that right says the
# child agrees for themselves (``approver`` ``self``).
ADULT_AGREES_FOR_A_CHILD: frozenset[str] = frozenset(
    {
        "household.cancel_invitation",
        "household.invite",
        "household.invite_again",
        "household.change_role",
        "household.remove",
        "household.leave",
        "household.bring_back",
        "model.rekey",
        "key.add",
        "key.share",
        "spending.limit",
        "key.admin",
    }
)


def an_adult_agrees_for(child: bool, action_type: str) -> bool:
    """Whether an adult's agreement stands for that of a person on ``action_type`` by default (a
    child, ``child``, on ``ADULT_AGREES_FOR_A_CHILD``): the single spot for this rule, read by
    the barrier (whose agreement a request needs) and by who answers for a thing of theirs
    (``policy.subjects.concerned_of``)."""
    return child and action_type in ADULT_AGREES_FOR_A_CHILD


def needs_the_judge(action_type: str) -> bool:
    """Whether the judge is asked about this type: not what changes nothing, not what carries
    other actions (those are judged inside), and not what is never shown (``is_silent``: nobody is
    told of it, so nobody could be asked about it; a tool Domo made, ``is_made``, has each
    request of its program judged as it runs, ``madetools.means``)."""
    return (
        action_type not in CHANGES_NOTHING
        and action_type not in JUDGED_WITHIN
        and not is_silent(action_type)
    )


ALLOWED_ACTIONS: Mapping[Role, frozenset[str]] = MappingProxyType(_ALLOWED_ACTIONS)
ACTION_FACTS: Mapping[str, Mapping[str, FactType]] = MappingProxyType(
    {
        action_type: MappingProxyType(
            {
                **_FACTS.get(action_type, {}),
                **(
                    {EXCEPTION_FACT: "text"}
                    if action_type in GIVES_ACCESS | MAKES_SOMEONE_NEW
                    else {}
                ),
                **({SERVES_THE_SPEAKER_FACT: "boolean"} if action_type in GIVES_ACCESS else {}),
            }
        )
        for action_type in dict.fromkeys((*_KNOWN, *_FACTS))
    }
)


def actions_allowed(role: Role) -> frozenset[str]:
    """The action types a role may use, by default."""
    return ALLOWED_ACTIONS[role]


@dataclass(frozen=True)
class Limit:
    """A limit on an action type (spec 5.5): a CEL ``expression`` that answers ``"allow"``,
    ``"refuse"``, ``"ask_owner"`` or ``"judge"`` (``policy.limits`` compiles and evaluates it);
    the ``judge_question`` that says, in words, what must be understood when it answers
    ``"judge"`` (``None`` otherwise); and the ``summary`` that says the limit in plain words (a
    right's limit has its right's summary). Domo may tell the person the summary of a limit that
    refuses them.

    The owner's rights carry limits (``policy.rights``), and so does the catalog
    (``DEFAULT_LIMITS``)."""

    expression: str
    judge_question: str | None
    summary: str


# Inviting (the owner's trial of 0.9.1): the owner's yes first when Domo judged the invitation
# deserves it (``owner_first``). The owner's own request goes on (``barrier.decide``: asking is
# his agreement). No length of stay is read (audit POL-5).
_INVITING = Limit(
    'action.facts.owner_first ? "ask_owner" : "allow"',
    None,
    "When the assistant judges that the owner should say yes before someone joins, the "
    "invitation waits for the owner's yes.",
)

# The catalog's limits, by action type (see the module docstring). Their summaries are in English,
# for the model, which says them in the person's language.
DEFAULT_LIMITS: Mapping[str, Limit] = MappingProxyType(
    {
        "household.invite": _INVITING,
        # Read through the type it follows (``RIGHTS_OF``): the same limit, said for both. A new
        # link never asks the owner first (its ``owner_first`` is always false).
        "household.invite_again": _INVITING,
    }
)


@dataclass(frozen=True)
class Risks:
    """The four risks of an action (spec 5.3, 2026-11-18): it ``costs`` money, it cannot be undone
    (``irreversible``), it acts on the physical world (``physical``), or it reaches outside the
    household (``outward``). They are data for whoever weighs the action (the reviewer of a tool,
    the judge, the owner's limits): the code refuses nothing to Domo because of one. Two sets of
    risks together are each risk of either (``|``).

    ``harmless``: its acting in the house was declared harmless (an operation of a tool Domo made,
    ``madetools.risks``, checked by the reviewer): done by mistake, it can hurt nobody, lock nobody
    out, expose nothing and cost nothing (a light, a speaker's volume); only with ``physical``
    (``ValueError`` otherwise). Not declared, acting in the house is taken as risky (the owner,
    2026-11-27: "the physical that carries risks is judged"). Together, it stays harmless only when
    every side that acts in the house was declared so."""

    costs: bool = False
    irreversible: bool = False
    physical: bool = False
    outward: bool = False
    harmless: bool = False

    def __post_init__(self) -> None:
        if self.harmless and not self.physical:
            raise ValueError("harmless is only said of acting in the house")

    def __or__(self, other: "Risks") -> "Risks":
        physical = self.physical or other.physical
        return Risks(
            costs=self.costs or other.costs,
            irreversible=self.irreversible or other.irreversible,
            physical=physical,
            outward=self.outward or other.outward,
            harmless=physical
            and (self.harmless or not self.physical)
            and (other.harmless or not other.physical),
        )

    @property
    def risky_physical(self) -> bool:
        """Whether it acts in the house without being declared harmless."""
        return self.physical and not self.harmless


# No risk at all.
NO_RISK: Final = Risks()

# What cannot be undone among the code's types: handing the owner's role over, removing a key,
# forgetting a memory, taking a note back and telling a successor's death; and removing an account
# (one given again is another account, spec 13.4); and forgetting a person.
_IRREVERSIBLE: Final = frozenset(
    {
        "people.forget",
        "people.forget_me",
        "household.transfer_ownership",
        "key.remove",
        "account.remove",
        "memory.forget",
        "note.withdraw",
        "successors.note_death",
        # The spoken phrase is gone for good (two new sayings agree another), and so is what
        # was learnt of a voice.
        "proof.phrase_remove",
        "voice.unlearn",
    }
)
# What costs among the code's types: a step up to a stronger model for the rest of a request.
_COSTS: Final = frozenset({"models.step_up"})

# The risks of each type of the code; an open kind carries its own (``risks_of``).
CODE_RISKS: Mapping[str, Risks] = MappingProxyType(
    {
        action_type: Risks(costs=action_type in _COSTS, irreversible=action_type in _IRREVERSIBLE)
        for action_type in _KNOWN
    }
)


@dataclass(frozen=True)
class OpenKind:
    """A kind of action Domo opened for a tool of his (spec 5.3, the table ``action_kinds``):
    its type (``made.<name>``), its four risks (in this delivery always ``False``: a tool only
    computes), the facts its action carries, the roles that have it by default, and what it does,
    in one sentence Domo wrote at its creation in the household's language (``description``:
    shown to people as it is, and to the writer of rights so that it can name it)."""

    action_type: str
    costs: bool
    irreversible: bool
    physical: bool
    outward: bool
    facts: Mapping[str, FactType]
    default_roles: frozenset[Role]
    description: str


# No open kind: the catalog of the code alone.
NO_KINDS: Final[Mapping[str, OpenKind]] = MappingProxyType({})

# The code's types whose action changes who may do what (Effect ``permissions``,
# ``policy.actions``): the tools that describe them say so. The catalog keeps no effect by type, so
# they are listed here, and a test checks the list against the tools' own descriptions.
PERMISSION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "household.invite",
        "household.invite_again",
        "household.cancel_invitation",
        "household.change_role",
        "household.remove",
        "household.leave",
        "household.bring_back",
        "household.transfer_ownership",
        "device.entrust",
        "device.remove",
        "key.share",
        "spending.limit",
        "keys.house",
        "successors.set",
        "successors.note_death",
        "successors.undo_death",
        "rights.propose",
        "rights.apply",
        "policy.update",
        "house.name_domo",
        "models.set_level",
        # The gestures on people that give an access or a way in, may move one, or change who
        # may do what, and the owner's directives.
        "household.send_link",
        "access.promise_keep",
        "people.merge",
        "role.create",
        "role.change",
        "role.delete",
        "people.directive_set",
        "people.directive_lift",
    }
)
# The types of the code that handle a secret: a key given, given again, removed, shared, its admin
# key, and the rules of the house on the keys; and an account removed or shared
# (``_ACCOUNT_SECRET_TYPES``).
SECRET_TYPES: Final = (
    frozenset({"model.rekey", "key.add", "key.remove", "key.share", "key.admin", "keys.house"})
    | _ACCOUNT_SECRET_TYPES
)
# The household's people he may invite, take back, remove and give another role on his own
# judgement (the owner, 2026-11-23); his own judge weighs each, and the owner takes any of it back
# by a rule. Leaving the household and handing the owner's role over stay out of it.
DOMO_HOUSEHOLD_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "household.invite",
        "household.invite_again",
        "household.cancel_invitation",
        "household.change_role",
        "household.remove",
        "household.bring_back",
    }
)
# Entrusting a device to someone and removing one he may also do on his own (the owner,
# 2026-11-24), as removing an account that no longer serves (``account.remove``); sharing a house
# account stays the owner's alone.
DOMO_DEVICE_ACTIONS: Final[frozenset[str]] = frozenset({"device.entrust", "device.remove"})
# The gestures on the people of the house and their roles (ruling 14): in no role. Only the owner
# and the assistant make them, and the owner may open one to a precise person by a right. A role
# the assistant makes never holds one (``ROLE_CEILING``).
PEOPLE_GESTURES: Final[frozenset[str]] = DOMO_HOUSEHOLD_ACTIONS | frozenset(
    {
        "household.send_link",
        "people.open",
        "people.complete",
        "people.erase",
        "people.forget",
        "people.link",
        "people.unlink",
        "people.merge",
        "people.split",
        "people.mistake",
        "role.create",
        "role.change",
        "role.delete",
        "access.promise",
        "access.promise_keep",
        "access.promise_drop",
    }
)
_HIS_OWN_AFTER_ALL: Final = PEOPLE_GESTURES | DOMO_DEVICE_ACTIONS | {"account.remove"}
# What Domo does not have by default (the controller's ruling of 2026-11-18, the socle): secrets,
# spending limits, who may do what, and what is nobody's, but what ``DOMO_HOUSEHOLD_ACTIONS`` and
# ``DOMO_DEVICE_ACTIONS`` hold and removing an account. What is nobody's, a right of the owner
# for Domo opens to him, as a right opens ``texts.for_someone`` to a person; the rest the rights
# writer never opens to him for now (``CLOSED_TO_DOMO``); the owner's own
# (``OWNER_ONLY_ACTIONS``) stay the owner's whatever any right says.
# Going out adds sharing an account (a secret), and a port of the machine that hosts him (the
# owner's by default).
DOMO_NOT_BY_DEFAULT: Final[frozenset[str]] = (
    SECRET_TYPES
    | {"spending.limit"}
    | PERMISSION_TYPES
    | _NOBODYS_ACTIONS
    | _ACCOUNT_SECRET_TYPES
    | {NET_OPEN_HOST}
) - _HIS_OWN_AFTER_ALL
# What no right the rights writer writes opens to Domo himself, for now (ruling R22, spec 5.1:
# the socle): secrets, keys and accounts, spending limits, and who may do what. Opened, they would
# act on the owner's keys and in his name. A port of the machine that hosts him is not among them:
# the owner may give him that right (2026-11-22, spec 13.10). The owner may still take any of them
# from him.
CLOSED_TO_DOMO: Final[frozenset[str]] = DOMO_NOT_BY_DEFAULT - _NOBODYS_ACTIONS - {NET_OPEN_HOST}

# What a role the assistant makes never holds (ruling 15, socle 7), whatever its accesses say: the
# owner's own, spending limits, the gestures on people, and what is closed to the assistant
# himself (he does not give what he does not have). Held in code at the writing of a role
# (``people.made_roles``) and at every reading (``policy.rights.RightsTable.resolve``).
ROLE_CEILING: Final[frozenset[str]] = (
    OWNER_ONLY_ACTIONS
    | {"spending.limit"}
    | PEOPLE_GESTURES
    # All he does not have by default: closed to him, nobody's (another's texts), and a port of
    # the machine that hosts him, which only the owner may give him.
    | DOMO_NOT_BY_DEFAULT
)
# What a role the assistant makes may still take away, though it is above its ceiling or what
# everyone has: leaving the household and "forget me" (the owner's answer of 2026-11-27, "Oui,
# Domo juge"). Never what it gives.
ROLE_MAY_DENY: Final[frozenset[str]] = frozenset({"household.leave", "people.forget_me"})

# What Domo may do on his own, for the house (the owner, 2026-11-18: he may do everything and
# judges himself whether it is right): every type of the code but the owner's own and what he does
# not have by default (``DOMO_NOT_BY_DEFAULT``). Every open kind joins it (``load_policy``).
DOMO_ACTIONS: Final[frozenset[str]] = KNOWN_ACTIONS - OWNER_ONLY_ACTIONS - DOMO_NOT_BY_DEFAULT

# What is sensitive (spec 6.12, the seam of the voice): what a speaker the code is not sure of
# never gets on a weak clue alone (``barrier.may_act_weakly``, which ``barrier.decide`` reads).
# The owner's rule it serves: ordinary things from an unsure speaker are done; for these the
# assistant judges first, and asks a proof only on a real doubt. Derived from the catalog's own
# sets, so that a type added to one of them is sensitive at once:
NOT_WEAKLY: Final[frozenset[str]] = (
    # the owner's own (rules, rights, succession, the level of models, a house account shared,
    # his directives) and who may do what (the household, devices entrusted, keys, limits);
    OWNER_ONLY_ACTIONS
    | PERMISSION_TYPES
    # the people, their roles, accesses and directives, and reading them;
    | PEOPLE_GESTURES
    | PEOPLE_READ_ACTIONS
    | OWNER_DIRECTIVE_ACTIONS
    | {"people.forget_me", "household.show_link", "household.leave"}
    # the rights to read: who agrees for a child, what is set for the whole house;
    | {CHILD_AGREE, HOUSE_SET}
    # keys, accounts and their secrets; opening an address, a port of the machine included;
    | SECRET_TYPES
    | OUTWARD_ACTIONS
    | {NET_OPEN_HOST}
    # spending, and whatever costs, leaves the house or cannot be undone; what is said in
    # someone's name;
    | {"spending.limit", "spending.show"}
    | {
        action_type
        for action_type, risks in CODE_RISKS.items()
        if risks.costs or risks.outward or risks.irreversible
    }
    | {"note.leave", "note.withdraw", "note.keep"}
    # the person's devices;
    | _OWN_DEVICE_ACTIONS
    # memory deleted, rewritten or searched, what is private to the person, what weighs on how
    # others are trusted, and another person's things;
    | {"memory.forget", "memory.change", "memory.search"}
    | TRUST_ACTIONS
    | DELIVERY_ACTIONS
    | _RECEIVED_NOTE_ACTIONS
    | {"note.list_sent", "texts.find"}
    | _NOBODYS_ACTIONS
    # a yes, an agreement that lasts, and what carries other actions (the assistant's own mark,
    # ``confirmation.own_idea``, is not a yes);
    | (
        {action_type for action_type in _KNOWN if action_type.startswith("confirmation.")}
        - {"confirmation.own_idea"}
    )
    | AGREEMENT_ACTIONS
    | {"chain.run", "chain.continue"}
    # a plan: making one too, since it runs later as the person, sure (the voice's choice, kept
    # at the merge: the stricter of the two); changing or dropping one;
    | {"plan.set", "plan.change", "plan.cancel"}
    # writing, changing, removing or timing a tool of the assistant's, and a program run by hand
    # (launching one follows the risks of its operation: ``barrier.may_act_weakly``).
    | TOOL_ACTIONS
    | PROGRAM_ACTIONS
    | {PROGRAM_RUN}
    # the spoken phrase (a proof) and what was learnt of a voice; the version of a text in
    # another language (the voice's choices, kept at the merge).
    | {"proof.phrase_agree", "proof.phrase_change", "proof.phrase_remove", "voice.unlearn"}
    | {"texts.language_version"}
)
# The ordinary things an unsure speaker may have done, written out so that a test compares them
# with what the catalog leaves (``KNOWN_ACTIONS - NOT_WEAKLY - PROGRAM_REQUESTS``: what a running
# program asks follows its risks). The person's own texts and writing, their name and the names
# they use, the assistant's choices of models, a memory kept, a plan or a reminder of their own,
# a complaint and its question, and the assistant's own mark.
WEAKLY_FINE: Final[frozenset[str]] = frozenset(
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
        "learning.note",
        "learning.asked",
        "confirmation.own_idea",
        # Of the voice: stopping to listen, and Domo's own note on how someone speaks.
        "voice.stop_listening",
        MANNER_NOTE,
    }
)

# What, in a turn Domo acts in on his own (a launch for the house woke him: Task 9), is about the
# person whose conversation it is, as their own: their profile and the name they call him by, the
# notes left for them, their texts, their devices seen, their nicknames and their spending. His
# memory, the trust he notes, his plans, his reminders, his look at what reached them, his chains
# and his choice of writing are his own: he keeps them on his own, and the owner takes any of
# it back by a rule. The conversation being theirs gives Domo none of the rest: in such a turn he
# has these only by a right the owner gives him
# (``on_his_own_tables``), as for ``DOMO_NOT_BY_DEFAULT``. A note he leaves there is his own (the
# owner, 2026-11-21), and so are the notes he finds, takes back and keeps there: his own, never
# theirs (``tools.notes``). So are the requests he drops or changes there (spec 12.5): his own
# that wait in that conversation; theirs only by a right the owner gives him
# (``ON_THEIR_REQUESTS``, ruling R30). Of the voice, the spoken phrase and the listening are
# theirs; what was learnt of a voice is his own recording (the owner, Task 27: ``voice.unlearn``
# is his, of anyone's voice, as his idea too; the tool's own guard says whose).
NOT_ON_HIS_OWN: Final[frozenset[str]] = frozenset(
    {
        "profile.rename",
        "profile.name_domo",
        "note.told",
        "note.show",
        "texts.find",
        "texts.rewrite",
        "texts.restore",
        "texts.language",
        "texts.note",
        "device.list",
        "people.nickname_keep",
        "people.nickname_forget",
        "spending.show",
        *(VOICE_ACTIONS - {"voice.unlearn"}),
    }
)


@dataclass(frozen=True)
class PolicyTables:
    """What the barrier decides from: the action types it knows (``known``), who may do which,
    the limit that applies to an action type when no right sets one (``DEFAULT_LIMITS`` unless a
    test says otherwise), the kinds of action opened for Domo's tools (``kinds``, by type:
    ``policy.open_catalog.load_policy`` lays them over the code's, in ``known``, ``allowed`` and
    ``domo`` too), and what Domo himself may do (``domo``, ``DOMO_ACTIONS`` unless said).

    It is the catalog's layer: the owner's rights (``policy.rights``) sit on top of it."""

    known: frozenset[str]
    allowed: Mapping[Role, frozenset[str]]
    limits: Mapping[str, Limit] = DEFAULT_LIMITS
    kinds: Mapping[str, OpenKind] = NO_KINDS
    domo: frozenset[str] = DOMO_ACTIONS


def adults_only(policy: PolicyTables) -> frozenset[str]:
    """What is reserved to adults today in ``policy`` (ruling 16): what the adults have and the
    children have not, open kinds included. A role never opens it to a child-marked person: only
    the owner's right for that person does (``policy.rights.RightsTable.resolve``)."""
    return policy.allowed.get("adult", frozenset()) - policy.allowed.get("child", frozenset())


def risks_of(action_type: str, policy: PolicyTables) -> Risks:
    """The four risks of ``action_type``: the code's (``CODE_RISKS``), else those of its open kind
    in ``policy``. ``KeyError`` for a type neither knows: its risks are never guessed as none."""
    risks = CODE_RISKS.get(action_type)
    if risks is not None:
        return risks
    kind = policy.kinds.get(action_type)
    if kind is None:
        raise KeyError(f"not an action type the catalog knows: {action_type}")
    return Risks(
        costs=kind.costs,
        irreversible=kind.irreversible,
        physical=kind.physical,
        outward=kind.outward,
    )


def declared_facts(action_type: str, policy: PolicyTables) -> Mapping[str, FactType] | None:
    """The facts an action of ``action_type`` carries: the code's (``ACTION_FACTS``), else those
    of its open kind in ``policy``; ``None`` for a type neither knows."""
    declared = ACTION_FACTS.get(action_type)
    if declared is not None:
        return declared
    kind = policy.kinds.get(action_type)
    return None if kind is None else kind_facts(kind)


def kind_facts(kind: "OpenKind") -> Mapping[str, FactType]:
    """The facts an action of the open ``kind`` carries: those of every launch of a tool Domo
    made (``MADE_FACTS``), and what the kind declares besides."""
    return MappingProxyType({**MADE_FACTS, **kind.facts})


DEFAULT_POLICY = PolicyTables(KNOWN_ACTIONS, ALLOWED_ACTIONS)


# What, in a turn Domo acts in on his own, he does by default to his own requests that wait
# there (spec 12.5), and to the person's only by a right the owner gives him, as for
# ``NOT_ON_HIS_OWN`` (ruling R30): dropping one, changing one (``on_his_own_requests``).
ON_THEIR_REQUESTS: Final[frozenset[str]] = frozenset({"confirmation.expire", "confirmation.amend"})


def on_his_own_requests(policy: PolicyTables) -> PolicyTables:
    """``policy`` as it reads, in a turn Domo acts in on his own, what he does to a request of
    the person whose conversation it is (``ON_THEIR_REQUESTS``): not his by default; a right the
    owner gives him opens it, as any of his rights."""
    return replace(policy, domo=policy.domo - ON_THEIR_REQUESTS)


def on_his_own_tables(policy: PolicyTables) -> PolicyTables:
    """``policy`` for a turn Domo acts in on his own: what is about the person whose
    conversation it is (``NOT_ON_HIS_OWN``) is not his by default there; a right the owner gives
    him opens it, as any of his rights."""
    return replace(policy, domo=policy.domo - NOT_ON_HIS_OWN)
