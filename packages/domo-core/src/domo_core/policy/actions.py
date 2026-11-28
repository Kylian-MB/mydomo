"""The described action: what the policy barrier reasons about (spec 10.1).

The barrier does not look at a tool or at its arguments. It looks at a description of what the
call would do: which connector, which type of action, on what, with what kind of effect, and
whether the result leaves the household. The description is written by the code of the tool,
never by the model.

It also carries the *facts* of the call (spec 5.4): a few typed values a limit can read (the role
someone is given, for how many days, how long a note is). They are written by the code of the tool
too, and they are never a free text: a note is described by its length, not by its words. They go
where the rest of the action goes, into a stored request and into the audit log.

Some situations a tool always refuses, whatever the rights (spec 5.2): nobody changes their own
role, nobody acts on the owner, the owner's role is never given, the owner never leaves. Each such
tool says them in one pure function of the action's facts (``FixedRefusal``), which it uses
itself and which the rights writer uses to keep its examples true (``policy.rights_writer``).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast, get_args

from domo_core.policy.roles import Actor, Role

# What the action touches. ``read`` changes nothing; ``self_profile`` is the person's own profile;
# ``own_requests`` is the person's own pending requests; ``message`` reaches another person;
# ``permissions`` changes who may do what (people, roles, rules); ``secrets`` handles a secret;
# ``program`` is what a running program asks the core (``catalog.PROGRAM_TYPES``).
Effect = Literal[
    "read", "self_profile", "own_requests", "message", "permissions", "secrets", "program"
]

# Where the effect ends up: inside the household, or outside of it.
Destination = Literal["household", "external"]

# A fact is a simple value: a short text, a whole number, a yes or no, or nothing.
FactValue = str | int | bool | None

# The longest name of a fact and the longest text a fact may hold. A fact is never a content (a
# note's words, say): the caps keep it so.
MAX_FACT_NAME_LENGTH = 64
MAX_FACT_TEXT_LENGTH = 200

# The facts of an action that has none.
EMPTY_FACTS: Mapping[str, FactValue] = MappingProxyType({})

# Who is in the household now, as the facts of an action name people: their role, by
# ``person:<id>``.
Present = Mapping[str, Role]

# A situation a tool always refuses, whatever the rights: given who asks, the facts of the action
# and who is in the household, why (in plain words, never a value of the call), or ``None``.
FixedRefusal = Callable[[Actor, Mapping[str, FactValue], Present], str | None]

_EFFECTS: tuple[str, ...] = get_args(Effect)
_DESTINATIONS: tuple[str, ...] = get_args(Destination)
_FIELDS = ("connector", "action_type", "target", "effect", "destination")
# The field a stored action has only when it is Domo's own act too (``also_domo``).
_ALSO_DOMO = "also_domo"
# The field a stored action has only when the judge weighs whether it is really theirs
# (``whose_judged``).
_WHOSE_JUDGED = "whose_judged"
# The field a stored action has only when only the judge's verdict lets it go (``verdict_needed``).
_VERDICT_NEEDED = "verdict_needed"


def _checked_facts(facts: object) -> dict[str, FactValue]:
    """A copy of ``facts`` if it is what an action may carry, else ``ValueError``: a mapping from
    names (``MAX_FACT_NAME_LENGTH`` characters at most) to simple values (a text of
    ``MAX_FACT_TEXT_LENGTH`` characters at most, a whole number, a boolean or ``None``)."""
    if not isinstance(facts, Mapping):
        raise ValueError("the facts of an action are a JSON object")
    checked: dict[str, FactValue] = {}
    for name, value in cast(Mapping[object, object], facts).items():
        if not isinstance(name, str) or len(name) > MAX_FACT_NAME_LENGTH:
            raise ValueError(f"the name of a fact is a text of {MAX_FACT_NAME_LENGTH} at most")
        if isinstance(value, str):
            if len(value) > MAX_FACT_TEXT_LENGTH:
                raise ValueError(f"a fact of text has {MAX_FACT_TEXT_LENGTH} characters at most")
        elif value is not None and not isinstance(value, (bool, int)):
            raise ValueError("a fact is a text, a whole number, a boolean or null")
        checked[name] = value
    return checked


@dataclass(frozen=True)
class DescribedAction:
    """``{connector, action type, target, effect, destination, facts}``.

    ``connector`` is ``"core"`` for the core's own tools. ``action_type`` is a name of the catalog
    (``household.invite``). ``target`` says on what the action acts (``None`` when it acts on
    nothing in particular). ``facts`` are the typed values of the call that a limit may read
    (``policy.catalog.ACTION_FACTS`` declares them for each action type): none unless the tool
    gives some. It holds names, identifiers and numbers, never the arguments of the call: it is
    written to the audit log. The facts are checked when the action is made (``ValueError``: a
    request with a fact that does not read back could never be replayed) and cannot be changed
    afterwards.

    ``also_domo`` says that the action, asked in a person's turn, is Domo's own act too (a
    note in his own name, ``tools.notes``, ruling R29): the barrier judges it for the person, as
    any of their actions, and for Domo, and keeps the stricter (``policy.decision.decide_now``).
    Stored with the action (``as_json`` writes it only when set), so that a request that waits is
    judged both ways again when its yes comes.

    ``whose_judged`` says that the tool asks the judge whether the call is really the person's own
    wish (words of another member of the household were read in the turn): the barrier marks it
    ``judge_identity``, as for a speaker the code is not sure of (final fix wave, case 1). Stored
    the same way.

    ``verdict_needed`` says that only the judge's own verdict lets it go: a judge that does not
    answer leaves it undone and asks nobody anything, no proof of whoever asks standing in for it
    (someone else's spoken phrase removed, the owner 2026-11-27: "the assistant judges it";
    ``conversation.execution.not_weighed``). Stored the same way.
    """

    connector: str
    action_type: str
    target: str | None
    effect: Effect
    destination: Destination
    # Left out of the hash (a mapping of facts is not hashable), kept in the comparison.
    facts: Mapping[str, FactValue] = field(default=EMPTY_FACTS, hash=False)
    also_domo: bool = False
    whose_judged: bool = False
    verdict_needed: bool = False

    def __post_init__(self) -> None:
        if self.facts is not EMPTY_FACTS:
            object.__setattr__(self, "facts", MappingProxyType(_checked_facts(self.facts)))

    def as_json(self) -> dict[str, object]:
        """The action as it is stored (in a confirmation) and audited."""
        return {
            "connector": self.connector,
            "action_type": self.action_type,
            "target": self.target,
            "effect": self.effect,
            "destination": self.destination,
            "facts": dict(self.facts),
            **({_ALSO_DOMO: True} if self.also_domo else {}),
            **({_WHOSE_JUDGED: True} if self.whose_judged else {}),
            **({_VERDICT_NEEDED: True} if self.verdict_needed else {}),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "DescribedAction":
        """The inverse of ``as_json``, to replay a stored action.

        ``ValueError`` unless ``data`` is exactly what ``as_json`` writes: the six fields, no
        other, each of the right kind. A request stored before the facts existed has the first five
        only: it reads back with no facts. A stored action that does not read back is never
        guessed.
        """
        if not isinstance(data, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("a described action is a JSON object")
        optional = {_ALSO_DOMO, _WHOSE_JUDGED, _VERDICT_NEEDED}
        if set(data) - optional not in (set(_FIELDS), {*_FIELDS, "facts"}):
            raise ValueError(
                f"a described action has exactly the fields {', '.join(_FIELDS)} and facts"
            )
        also_domo = data.get(_ALSO_DOMO, False)
        if also_domo is not True and also_domo is not False:
            raise ValueError("also_domo is true or false")
        whose_judged = data.get(_WHOSE_JUDGED, False)
        if whose_judged is not True and whose_judged is not False:
            raise ValueError("whose_judged is true or false")
        verdict_needed = data.get(_VERDICT_NEEDED, False)
        if verdict_needed is not True and verdict_needed is not False:
            raise ValueError("verdict_needed is true or false")
        connector = data["connector"]
        action_type = data["action_type"]
        target = data["target"]
        effect = data["effect"]
        destination = data["destination"]
        if not isinstance(connector, str) or not isinstance(action_type, str):
            raise ValueError("the connector and the action type of an action are text")
        if target is not None and not isinstance(target, str):
            raise ValueError("the target of an action is text or null")
        if effect not in _EFFECTS:
            raise ValueError(f"unknown effect {effect!r}")
        if destination not in _DESTINATIONS:
            raise ValueError(f"unknown destination {destination!r}")
        facts = _checked_facts(data["facts"]) if "facts" in data else EMPTY_FACTS
        return cls(
            connector,
            action_type,
            target,
            cast(Effect, effect),
            cast(Destination, destination),
            facts,
            also_domo,
            whose_judged,
            verdict_needed,
        )
