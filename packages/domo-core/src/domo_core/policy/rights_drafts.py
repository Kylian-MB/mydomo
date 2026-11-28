"""The form of a draft of rights (spec 5.6): what the rights writer answers, and what is stored.

* the writer answers through one tool, ``WRITER_TOOL`` (``submit_rights``): a question for the
  owner, or the changes of rights (``RightChange``) and the examples that show them
  (``Example``). ``read_answer`` reads its call: a ``Question``, a ``Draft``, or the
  ``Problem``s of its parts (which the writer is given back once, ``policy.rights_writer``); an
  answer whose form cannot be read at all is ``UnreadableAnswer``;
* a draft that the code accepted is stored as it is (``draft_as_json``): its changes, its
  examples with the outcome the code worked out, the names of the people it speaks of, and the
  rights it was checked against (``rights_basis``); ``stored_draft`` reads it back, to apply it
  (``tools.rights_tools``) or to show it on a card (``confirmations.describe``).

A change is about a person (``person_id``), a role (``role``), Domo himself (``domo``: what he
does on his own for the house, ``RightChange.for_domo``), or none of them: a rule for everyone,
which says what to ask (``ask``) and nothing else. A right of Domo's is never a rule for everyone,
nor the other way round. Besides who may do what, a change says what
Domo must ask before the action (``ask``: ``yes``, ``explicit_yes``, ``proof``), or the owner's
"never ask me" (``ask``: ``judge``, with his conditions in his words, ``ask_when``; or ``never``).

An example is about a person of the household (``person_id``), about a role nobody in the
household has yet (``role``), or about Domo acting on his own (``who``: ``domo``,
``Example.for_domo``): exactly one of the three, which ``policy.rights_writer`` checks. Its
outcome (``ExampleOutcome``) is what will hold: ``refused``; someone else's agreement the rights
ask for (``asks_an_adult``, ``asks_the_owner``); what a rule of the owner's asks
(``asks_them``); else ``domo_judges``, nothing is imposed and Domo judges.

An example may also state the moment it imagines (``Moment``, Ruling R25): the hour (``now``:
weekday, hour, minute, and the date when it matters), how many times the person did the action
(``history``: over the last day, week and month) and the household (``household``: ``people``
and ``invitations``, counts by role). Each part given replaces the real one of the moment of the
proposal; a count by role given replaces that role's. A stored draft keeps, for each example, the
moment it was worked out at, as far as the owner reads it (``policy.rights_writer``): the parts
that a limit of its type reads, and those the writer stated, with the values used.

Nothing here decides anything, and nothing touches the database.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, NamedTuple, Protocol, cast, get_args

from domo_core.llm.types import JsonObject, ToolCall, ToolDefinition
from domo_core.policy.actions import FactValue
from domo_core.policy.asks import RuleAsk
from domo_core.policy.catalog import MADE_LAUNCH_WITHOUT_A_YES, is_made
from domo_core.policy.rights import Access, Approver, Right, RightsTable
from domo_core.policy.roles import DOMO_LABEL, ROLES, Role
from domo_core.text import is_plain_line

ExampleOutcome = Literal["domo_judges", "asks_them", "asks_an_adult", "asks_the_owner", "refused"]
EXAMPLE_OUTCOMES: Final[tuple[ExampleOutcome, ...]] = get_args(ExampleOutcome)

# The longest summary of a right (the ``policy_rights`` table says the same), and the longest
# question of the writer to the owner.
MAX_SUMMARY_LENGTH: Final = 300
MAX_QUESTION_LENGTH: Final = 300
# The longest arguments of the writer's call that are read, in characters of JSON.
_MAX_ARGUMENTS_LENGTH: Final = 64_000
# The most an example may imagine of a count (times done, people, invitations): a household's
# numbers are small.
MAX_IMAGINED_COUNT: Final = 10_000

_ACCESS: Final[tuple[Access, ...]] = get_args(Access)
_APPROVERS: Final[tuple[Approver, ...]] = get_args(Approver)
_RULE_ASKS: Final[tuple[RuleAsk, ...]] = get_args(RuleAsk)

# A person in a fact: ``person:<uuid>``.
PERSON_PREFIX: Final = "person:"

# What is said of a part that is wrong, in the code's words (never a value the writer gave).
NOT_READABLE: Final = "the writer answered in a form that cannot be read"
SUMMARY_RULE: Final = f"the summary is one line of 1 to {MAX_SUMMARY_LENGTH} characters"
EXAMPLE_TYPE_RULE: Final = "action_type is a type of the catalog that a right can be about"
PERSON_RULE: Final = "person_id is the id of a person of the household (people)"
# The roles a rule is written on are the built-in ones: a role the assistant made is not one (a
# rule for its people is written for each of them).
ROLE_RULE: Final = (
    f"role is one of {', '.join(ROLES)}: a role the assistant made is not a role a rule is "
    "written on (that role itself is changed instead, for whoever has it, now and later: ask "
    "about it)"
)
DOMO_RULE: Final = "domo is true or false"
WHO_RULE: Final = f'who is "{DOMO_LABEL}" or null'


@dataclass(frozen=True)
class RightChange:
    """One change of a draft: the right of a subject (``person_id``, ``role`` or Domo himself,
    ``for_domo``; none of them for a rule for everyone) for ``action_type`` becomes what the
    change says (each part ``None``: it says nothing of it), or is taken away (``remove``: it then
    says nothing else). A paused right is given back by writing it again. ``ask`` is what
    Domo must ask before the action, or the owner's "never ask me" (``judge``, with his
    conditions ``ask_when``, or ``never``).
    ``summary`` says it in plain words, in the owner's language."""

    person_id: uuid.UUID | None
    role: Role | None
    action_type: str
    remove: bool
    access: Access | None
    approver: Approver | None
    limit_expression: str | None
    judge_question: str | None
    ask: RuleAsk | None
    ask_when: str | None
    summary: str
    for_domo: bool = False


@dataclass(frozen=True)
class ImaginedNow:
    """The hour of an example, in the household's time zone: the day of the week (``weekday``, 1
    is Monday and 7 Sunday), the hour and the minute, and the day itself (``date``,
    ``YYYY-MM-DD``, a day of that weekday) when it matters. Without a date, it is the next day of
    that weekday (``policy.rights_writer``)."""

    weekday: int
    hour: int
    minute: int
    date: str | None = None


@dataclass(frozen=True)
class ImaginedHistory:
    """How many times the person did the action over the last 24 hours (``day``), 7 days
    (``week``) and 30 days (``month``)."""

    day: int
    week: int
    month: int


@dataclass(frozen=True)
class Moment:
    """The moment an example imagines (Ruling R25): its hour (``now``), its history
    (``history``) and its household (``people``, ``invitations``: counts by role). A part that is
    ``None`` is the real one of the moment of the proposal; a role missing from a count keeps its
    real count."""

    now: ImaginedNow | None = None
    history: ImaginedHistory | None = None
    people: Mapping[Role, int] | None = None
    invitations: Mapping[Role, int] | None = None

    @property
    def parts(self) -> frozenset[str]:
        """The roots of the facts of the moment it gives: ``now``, ``history``, ``household``."""
        given = {
            "now": self.now is not None,
            "history": self.history is not None,
            "household": self.people is not None or self.invitations is not None,
        }
        return frozenset(part for part, there in given.items() if there)


NO_MOMENT: Final = Moment()


@dataclass(frozen=True)
class Example:
    """A situation the draft is checked on, and shown to the owner with: someone asks for an
    action of ``action_type`` whose facts are ``facts`` (every fact its type declares), at the
    ``moment`` it imagines (the real one where it says nothing). Who asks is a person of the
    household (``person_id``), or, for a role nobody has yet, a made-up person of that ``role``,
    or Domo acting on his own (``for_domo``): exactly one of the three. The writer says what
    it expects (``expected``); the code works it out (``policy.rights_writer.example_outcome``)
    and the two must agree. Its sentence is the
    code's, from the facts and the moment (never the writer's)."""

    person_id: uuid.UUID | None
    action_type: str
    facts: Mapping[str, FactValue]
    expected: ExampleOutcome
    role: Role | None = None
    moment: Moment = NO_MOMENT
    for_domo: bool = False

    def __post_init__(self) -> None:
        if is_made(self.action_type):
            # The facts of a launch of one of Domo's tools that an example leaves out are those
            # of a launch without a yes, for a person (``catalog.MADE_FACTS``): it need not say
            # them.
            object.__setattr__(self, "facts", {**MADE_LAUNCH_WITHOUT_A_YES, **self.facts})


@dataclass(frozen=True)
class Draft:
    """What the writer proposes: the changes, and the examples."""

    changes: tuple[RightChange, ...]
    examples: tuple[Example, ...]


class Problem(NamedTuple):
    """What is wrong with a draft: ``said`` in the code's words, and ``detail``, what CEL said of
    a limit, for the writer only."""

    said: str
    detail: str | None = None

    def __str__(self) -> str:
        return self.said if self.detail is None else f"{self.said}: {self.detail}"


@dataclass(frozen=True)
class Question:
    """The writer's question for the owner: one plain line, ``MAX_QUESTION_LENGTH`` at most."""

    text: str


class UnreadableAnswer(Exception):
    """The writer's answer cannot be read at all. ``said`` says why, in the code's words."""

    def __init__(self, said: str) -> None:
        super().__init__(said)
        self.said = said


# --- the writer's tool ---------------------------------------------------------------------------


def _field(kind: str | list[str], description: str) -> JsonObject:
    return {"type": kind, "description": description}


_TEXT_OR_NULL: Final = ["string", "null"]

# A change and an example, as the writer gives them (the writer's text says more).
_CHANGE_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "person_id": _field(
            _TEXT_OR_NULL,
            "The id of the person (from people), or null (with role null and domo false: "
            "everyone).",
        ),
        "role": _field(_TEXT_OR_NULL, "owner, adult, child or guest, or null."),
        "domo": _field(
            "boolean",
            "true: the assistant's own right, for what he does on his own (person_id and role "
            "null).",
        ),
        "action_type": _field("string", "From the catalog."),
        "remove": _field("boolean", "true takes this subject's right away."),
        "access": _field(_TEXT_OR_NULL, "allow, deny or null."),
        "approver": _field(_TEXT_OR_NULL, "self, adult, owner or null."),
        "limit_expression": _field(_TEXT_OR_NULL, "A limit in CEL, or null."),
        "judge_question": _field(_TEXT_OR_NULL, 'The question when the limit may say "judge".'),
        "ask": _field(
            _TEXT_OR_NULL,
            "What to ask before it: yes, explicit_yes, proof; or judge, never; or null.",
        ),
        "ask_when": _field(_TEXT_OR_NULL, "With ask judge: the owner's conditions, in his words."),
        "summary": _field("string", "The right in one plain sentence, in the owner's language."),
    },
    "required": ["action_type", "summary"],
    "additionalProperties": False,
}
_EXAMPLE_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "person_id": _field(_TEXT_OR_NULL, "Who asks (from people), or null."),
        "role": _field(_TEXT_OR_NULL, "Or a role nobody has yet (roles_without_people)."),
        "who": _field(_TEXT_OR_NULL, f'Or "{DOMO_LABEL}": the assistant acting on his own.'),
        "action_type": _field("string", "From the catalog."),
        "facts": _field("object", "Every fact of the action type, with its value."),
        "expected": _field("string", f"One of: {', '.join(EXAMPLE_OUTCOMES)}."),
        "now": _field(
            "object",
            "The hour it imagines: weekday (1 Monday to 7 Sunday), hour, minute, and date "
            "(YYYY-MM-DD) if the limit reads it.",
        ),
        "history": _field(
            "object",
            "How many times the person did it: day, week and month (last 24 h, 7 days, 30 days).",
        ),
        "household": _field(
            "object",
            "people and/or invitations: counts by role (a role left out keeps its real count).",
        ),
    },
    "required": ["action_type", "facts", "expected"],
    "additionalProperties": False,
}
_CHANGE_FIELDS: Final = frozenset(_CHANGE_SCHEMA["properties"])

WRITER_TOOL: Final = ToolDefinition(
    name="submit_rights",
    description=(
        "Give your answer, once: either a question for the owner (question alone), or the "
        "changes of rights and the examples that show them (changes and examples)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                **_field("string", "What you need the owner to tell you, in their language."),
                "maxLength": MAX_QUESTION_LENGTH,
            },
            "changes": {"type": "array", "items": _CHANGE_SCHEMA},
            "examples": {"type": "array", "items": _EXAMPLE_SCHEMA},
        },
        "additionalProperties": False,
    },
)


# --- reading what the writer gives ---------------------------------------------------------------


def as_person_id(value: object) -> uuid.UUID | None:
    """``value`` as the id of a person, if it is the text of one."""
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _one_of[T: str](value: object, accepted: tuple[T, ...]) -> T | None:
    for candidate in accepted:
        if candidate == value:
            return candidate
    return None


def _read_change(n: int, item: Mapping[str, object]) -> RightChange | list[Problem]:
    """Change number ``n`` as the writer (or a stored draft) gives it, or what is wrong with its
    form. A part that is left out says nothing (``null``)."""
    where = f"change {n}"
    problems: list[Problem] = []
    if set(item) - _CHANGE_FIELDS:
        problems.append(Problem(f"{where}: it has fields a change does not have"))
    raw_person = item.get("person_id")
    person_id = as_person_id(raw_person)
    if raw_person is not None and person_id is None:
        problems.append(Problem(f"{where}: {PERSON_RULE}"))
    raw_role = item.get("role")
    role: Role | None = _one_of(raw_role, ROLES)
    if raw_role is not None and role is None:
        problems.append(Problem(f"{where}: {ROLE_RULE}"))
    for_domo = item.get("domo")
    if for_domo is None:
        for_domo = False
    if not isinstance(for_domo, bool):
        problems.append(Problem(f"{where}: {DOMO_RULE}"))
    action_type = item.get("action_type")
    if not isinstance(action_type, str):
        problems.append(Problem(f"{where}: action_type is a type of the catalog"))
    remove = item.get("remove")
    if remove is None:
        remove = False
    if not isinstance(remove, bool):
        problems.append(Problem(f"{where}: remove is true or false"))
    parts: dict[str, str | None] = {}
    for name, accepted in (("access", _ACCESS), ("approver", _APPROVERS)):
        raw = item.get(name)
        parts[name] = _one_of(raw, accepted)
        if raw is not None and parts[name] is None:
            problems.append(Problem(f"{where}: {name} is {', '.join(accepted)} or null"))
    raw_ask = item.get("ask")
    ask: RuleAsk | None = _one_of(raw_ask, _RULE_ASKS)
    if raw_ask is not None and ask is None:
        problems.append(Problem(f"{where}: ask is {', '.join(_RULE_ASKS)} or null"))
    for name in ("limit_expression", "judge_question", "ask_when"):
        raw = item.get(name)
        if raw is not None and not isinstance(raw, str):
            problems.append(Problem(f"{where}: {name} is a text or null"))
    summary = item.get("summary")
    if not isinstance(summary, str):
        problems.append(Problem(f"{where}: {SUMMARY_RULE}"))
    if problems:
        return problems
    limit_expression = item.get("limit_expression")
    judge_question = item.get("judge_question")
    return RightChange(
        person_id=person_id,
        role=role,
        action_type=cast(str, action_type),
        remove=cast(bool, remove),
        access=cast(Access | None, parts["access"]),
        approver=cast(Approver | None, parts["approver"]),
        limit_expression=cast(str | None, limit_expression),
        judge_question=cast(str | None, judge_question),
        ask=ask,
        ask_when=cast(str | None, item.get("ask_when")),
        summary=cast(str, summary),
        for_domo=cast(bool, for_domo),
    )


def _simple_facts(value: object) -> dict[str, FactValue] | None:
    """``value`` as facts (names to simple values), or ``None`` when it is not that."""
    if not isinstance(value, Mapping):
        return None
    facts: dict[str, FactValue] = {}
    for name, fact in cast(Mapping[object, object], value).items():
        if not isinstance(name, str):
            return None
        if fact is not None and not isinstance(fact, str | int):
            return None
        facts[name] = fact
    return facts


def _whole(value: object, lowest: int, highest: int) -> int | None:
    """``value`` if it is a whole number from ``lowest`` to ``highest``."""
    if isinstance(value, bool) or not isinstance(value, int) or not lowest <= value <= highest:
        return None
    return value


def is_day(value: object) -> bool:
    """Whether ``value`` is a day written ``YYYY-MM-DD``."""
    if not isinstance(value, str) or len(value) != len("YYYY-MM-DD"):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


_NOW_PARTS: Final = ("weekday", "hour", "minute")
_HISTORY_PARTS: Final = ("day", "week", "month")
_HOUSEHOLD_PARTS: Final = ("people", "invitations")
_MOMENT_PARTS: Final = frozenset({"now", "history", "household"})


def _read_now(where: str, value: object) -> ImaginedNow | list[Problem]:
    """The hour an example imagines (``now``), or what is wrong with it."""
    shape = Problem(f"{where}: now is weekday, hour, minute and, if it matters, date")
    if not isinstance(value, Mapping):
        return [shape]
    given = cast(Mapping[str, object], value)
    if not set(_NOW_PARTS) <= set(given) or set(given) - {*_NOW_PARTS, "date"}:
        return [shape]
    problems: list[Problem] = []
    weekday = _whole(given["weekday"], 1, 7)
    if weekday is None:
        problems.append(
            Problem(f"{where}: now.weekday is a whole number from 1 (Monday) to 7 (Sunday)")
        )
    hour = _whole(given["hour"], 0, 23)
    if hour is None:
        problems.append(Problem(f"{where}: now.hour is a whole number from 0 to 23"))
    minute = _whole(given["minute"], 0, 59)
    if minute is None:
        problems.append(Problem(f"{where}: now.minute is a whole number from 0 to 59"))
    day = given.get("date")
    if day is not None and (
        not is_day(day)
        or (weekday is not None and date.fromisoformat(cast(str, day)).isoweekday() != weekday)
    ):
        problems.append(
            Problem(f"{where}: now.date is a day (YYYY-MM-DD) that falls on its weekday")
        )
    if problems or weekday is None or hour is None or minute is None:
        return problems
    return ImaginedNow(weekday, hour, minute, cast(str | None, day))


def _read_history(where: str, value: object) -> ImaginedHistory | list[Problem]:
    """The history an example imagines, or what is wrong with it."""
    if not isinstance(value, Mapping) or set(cast(Mapping[str, object], value)) != set(
        _HISTORY_PARTS
    ):
        return [Problem(f"{where}: history is day, week and month")]
    given = cast(Mapping[str, object], value)
    counts: dict[str, int] = {}
    problems: list[Problem] = []
    for name in _HISTORY_PARTS:
        count = _whole(given[name], 0, MAX_IMAGINED_COUNT)
        if count is None:
            problems.append(
                Problem(f"{where}: history.{name} is a whole number from 0 to {MAX_IMAGINED_COUNT}")
            )
        else:
            counts[name] = count
    if problems:
        return problems
    if not counts["day"] <= counts["week"] <= counts["month"]:
        return [
            Problem(
                f"{where}: history counts the last day within the week within the month: "
                "day <= week <= month"
            )
        ]
    return ImaginedHistory(counts["day"], counts["week"], counts["month"])


def _counts_by_role(value: object) -> dict[Role, int] | None:
    """``value`` as counts by role, if it is that."""
    if not isinstance(value, Mapping):
        return None
    counts: dict[Role, int] = {}
    for name, count in cast(Mapping[object, object], value).items():
        role: Role | None = _one_of(name, ROLES)
        whole = _whole(count, 0, MAX_IMAGINED_COUNT)
        if role is None or whole is None:
            return None
        counts[role] = whole
    return counts


def _read_household(
    where: str, value: object
) -> tuple[dict[Role, int] | None, dict[Role, int] | None] | list[Problem]:
    """The household an example imagines (its people, its invitations), or what is wrong with
    it."""
    shape = Problem(f"{where}: household is people, invitations or both")
    if not isinstance(value, Mapping):
        return [shape]
    given = cast(Mapping[str, object], value)
    if not given or set(given) - set(_HOUSEHOLD_PARTS):
        return [shape]
    read: dict[str, dict[Role, int]] = {}
    problems: list[Problem] = []
    for section in _HOUSEHOLD_PARTS:
        if section not in given:
            continue
        counts = _counts_by_role(given[section])
        if counts is None:
            problems.append(
                Problem(
                    f"{where}: household.{section} counts by role ({', '.join(ROLES)}), each a "
                    f"whole number from 0 to {MAX_IMAGINED_COUNT}"
                )
            )
        else:
            read[section] = counts
    if problems:
        return problems
    return read.get("people"), read.get("invitations")


def _read_moment(where: str, item: Mapping[str, object]) -> Moment | list[Problem]:
    """The moment example ``where`` imagines (``now``, ``history``, ``household``: each one
    optional), or what is wrong with it."""
    problems: list[Problem] = []
    now = history = None
    people = invitations = None
    if item.get("now") is not None:
        read_now = _read_now(where, item["now"])
        if isinstance(read_now, list):
            problems.extend(read_now)
        else:
            now = read_now
    if item.get("history") is not None:
        read_history = _read_history(where, item["history"])
        if isinstance(read_history, list):
            problems.extend(read_history)
        else:
            history = read_history
    if item.get("household") is not None:
        read_household = _read_household(where, item["household"])
        if isinstance(read_household, list):
            problems.extend(read_household)
        else:
            people, invitations = read_household
    if problems:
        return problems
    return Moment(now, history, people, invitations)


def _read_example(n: int, item: Mapping[str, object], outcome_key: str) -> Example | list[Problem]:
    """Example number ``n`` (its outcome under ``outcome_key``), or what is wrong with its form.
    Who asks is ``person_id``, ``role`` or ``who`` (Domo); which one, and that it is exactly one,
    is checked with the household (``policy.rights_writer``). The moment it imagines is
    optional."""
    where = f"example {n}"
    problems: list[Problem] = []
    given = set(item)
    required = {"action_type", "facts", outcome_key}
    if not required <= given or given - {*required, "person_id", "role", "who", *_MOMENT_PARTS}:
        problems.append(
            Problem(
                f"{where}: an example is person_id, role or who, action_type, facts and "
                f"{outcome_key}, and may say now, history and household"
            )
        )
    raw_person = item.get("person_id")
    person_id = as_person_id(raw_person)
    if raw_person is not None and person_id is None:
        problems.append(Problem(f"{where}: {PERSON_RULE}"))
    raw_role = item.get("role")
    role: Role | None = _one_of(raw_role, ROLES)
    if raw_role is not None and role is None:
        problems.append(Problem(f"{where}: {ROLE_RULE}"))
    raw_who = item.get("who")
    if raw_who is not None and raw_who != DOMO_LABEL:
        problems.append(Problem(f"{where}: {WHO_RULE}"))
    action_type = item.get("action_type")
    if not isinstance(action_type, str):
        problems.append(Problem(f"{where}: {EXAMPLE_TYPE_RULE}"))
    facts = _simple_facts(item.get("facts"))
    if facts is None:
        problems.append(Problem(f"{where}: facts is an object of simple values"))
    expected: ExampleOutcome | None = _one_of(item.get(outcome_key), EXAMPLE_OUTCOMES)
    if expected is None:
        problems.append(Problem(f"{where}: {outcome_key} is {', '.join(EXAMPLE_OUTCOMES)}"))
    moment = _read_moment(where, item)
    if isinstance(moment, list):
        problems.extend(moment)
    if problems or facts is None or expected is None or isinstance(moment, list):
        return problems
    return Example(
        person_id, cast(str, action_type), facts, expected, role, moment, raw_who == DOMO_LABEL
    )


def _objects(value: object) -> list[Mapping[str, object]] | None:
    """``value`` as a list of JSON objects, or ``None``."""
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if not all(isinstance(item, Mapping) for item in items):
        return None
    return [cast(Mapping[str, object], item) for item in items]


def _read_draft(
    changes: list[Mapping[str, object]],
    examples: list[Mapping[str, object]],
    outcome_key: str,
) -> Draft | list[Problem]:
    """A draft from its changes and examples, or what is wrong with their form."""
    problems: list[Problem] = []
    read_changes: list[RightChange] = []
    read_examples: list[Example] = []
    for n, item in enumerate(changes, start=1):
        change = _read_change(n, item)
        if isinstance(change, list):
            problems.extend(change)
        else:
            read_changes.append(change)
    for n, item in enumerate(examples, start=1):
        example = _read_example(n, item, outcome_key)
        if isinstance(example, list):
            problems.extend(example)
        else:
            read_examples.append(example)
    if problems:
        return problems
    return Draft(tuple(read_changes), tuple(read_examples))


def read_answer(call: ToolCall) -> Question | Draft | list[Problem]:
    """What the writer's call gives: its question, a draft, or what is wrong with the parts of
    a draft. ``UnreadableAnswer`` for a call whose form cannot be read at all (another tool,
    arguments that are not a JSON object, a question with anything else, neither form, a
    question that cannot be shown)."""
    if call.name != WRITER_TOOL.name or len(call.arguments) > _MAX_ARGUMENTS_LENGTH:
        raise UnreadableAnswer(NOT_READABLE)
    try:
        arguments: object = json.loads(call.arguments)
    except (ValueError, RecursionError):
        raise UnreadableAnswer(NOT_READABLE) from None
    if not isinstance(arguments, dict):
        raise UnreadableAnswer(NOT_READABLE)
    given = cast(dict[str, object], arguments)
    if "question" in given:
        question = given["question"]
        if set(given) != {"question"} or not isinstance(question, str):
            raise UnreadableAnswer(NOT_READABLE)
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_LENGTH or not is_plain_line(question):
            raise UnreadableAnswer("the writer's question cannot be shown")
        return Question(question)
    changes = _objects(given.get("changes"))
    examples = _objects(given.get("examples"))
    if set(given) != {"changes", "examples"} or changes is None or examples is None:
        raise UnreadableAnswer(NOT_READABLE)
    return _read_draft(changes, examples, "expected")


# --- the stored draft ----------------------------------------------------------------------------


def rights_basis(rights: RightsTable, paused: tuple[Right, ...]) -> list[str]:
    """The rights a draft was checked against: the ids of those in force and of the paused ones.
    A draft is applied only while they are the same (``tools.rights_tools``): its examples were
    worked out on them."""
    return sorted([str(r.id) for r in rights.rights] + [f"{r.id}:paused" for r in paused])


class DraftSource(Protocol):
    """What a draft is stored with (``policy.rights_writer.WriterContext`` is one): the people
    of the household, and the rights it was checked against."""

    @property
    def rights(self) -> RightsTable: ...

    @property
    def paused(self) -> tuple[Right, ...]: ...

    def person(self, person_id: uuid.UUID) -> tuple[str, Role] | None: ...


def _change_json(change: RightChange) -> JsonObject:
    return {
        "person_id": None if change.person_id is None else str(change.person_id),
        "role": change.role,
        "domo": change.for_domo,
        "action_type": change.action_type,
        "remove": change.remove,
        "access": change.access,
        "approver": change.approver,
        "limit_expression": change.limit_expression,
        "judge_question": change.judge_question,
        "ask": change.ask,
        "ask_when": change.ask_when,
        "summary": change.summary,
    }


def _people_of(draft: Draft) -> set[uuid.UUID]:
    """The people a draft speaks of: the subjects of its changes, who asks in its examples, and
    the people its facts name."""
    people = {c.person_id for c in draft.changes if c.person_id is not None}
    for example in draft.examples:
        if example.person_id is not None:
            people.add(example.person_id)
        for value in example.facts.values():
            if isinstance(value, str) and value.startswith(PERSON_PREFIX):
                person_id = as_person_id(value.removeprefix(PERSON_PREFIX))
                if person_id is not None:
                    people.add(person_id)
    return people


def moment_json(moment: Moment) -> JsonObject:
    """The parts of ``moment`` it gives, as an example holds them (``now``, ``history``,
    ``household``): nothing for a part it does not give."""
    shown: JsonObject = {}
    if moment.now is not None:
        now: JsonObject = {
            "weekday": moment.now.weekday,
            "hour": moment.now.hour,
            "minute": moment.now.minute,
        }
        if moment.now.date is not None:
            now["date"] = moment.now.date
        shown["now"] = now
    if moment.history is not None:
        history = moment.history
        shown["history"] = {"day": history.day, "week": history.week, "month": history.month}
    household: JsonObject = {}
    if moment.people is not None:
        household["people"] = dict(moment.people)
    if moment.invitations is not None:
        household["invitations"] = dict(moment.invitations)
    if household:
        shown["household"] = household
    return shown


def draft_as_json(draft: Draft, source: DraftSource) -> JsonObject:
    """A draft that ``check_draft`` accepted, as it is stored: its changes; its examples, each
    with the outcome the code worked out (``outcome``: the writer's expected one, which is the
    same) and the moment it was worked out at, as far as the owner reads it (``moment_json``:
    what ``policy.rights_writer.write_draft`` gives); the names of the people it speaks of, as
    they are now (``names``, by id), for the sentences of its examples; and the rights it was
    checked against (``based_on``, ``rights_basis``)."""
    names: dict[str, str] = {}
    for person_id in sorted(_people_of(draft)):
        known = source.person(person_id)
        if known is not None:
            names[str(person_id)] = known[0]
    return {
        "changes": [_change_json(change) for change in draft.changes],
        "examples": [
            {
                "person_id": None if example.person_id is None else str(example.person_id),
                "role": example.role,
                **({"who": DOMO_LABEL} if example.for_domo else {}),
                "action_type": example.action_type,
                "facts": dict(example.facts),
                "outcome": example.expected,
                **moment_json(example.moment),
            }
            for example in draft.examples
        ],
        "names": names,
        "based_on": rights_basis(source.rights, source.paused),
    }


@dataclass(frozen=True)
class StoredDraft:
    """A draft as stored (``draft_as_json``): the draft (each example's ``expected`` is the
    outcome the code worked out), the names of the people it speaks of, by id, and the rights it
    was checked against."""

    draft: Draft
    names: Mapping[str, str]
    based_on: tuple[str, ...]


def stored_draft(content: object) -> StoredDraft:
    """The stored ``content`` of a draft read back. ``ValueError`` unless it is what
    ``draft_as_json`` writes: a stored draft that does not read back is never guessed."""
    if not isinstance(content, Mapping):
        raise ValueError("a stored draft is a JSON object")
    stored = cast(Mapping[str, object], content)
    changes = _objects(stored.get("changes"))
    examples = _objects(stored.get("examples"))
    names: object = stored.get("names")
    based_on: object = stored.get("based_on")
    if changes is None or examples is None:
        raise ValueError("a stored draft has changes and examples")
    if not isinstance(names, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in cast(Mapping[object, object], names).items()
    ):
        raise ValueError("the names of a stored draft are texts by id")
    if not isinstance(based_on, list) or not all(
        isinstance(item, str) for item in cast(list[object], based_on)
    ):
        raise ValueError("a stored draft says which rights it was checked against")
    draft = _read_draft(changes, examples, "outcome")
    if isinstance(draft, list):
        raise ValueError("a stored draft does not read back")
    return StoredDraft(
        draft,
        dict(cast(Mapping[str, str], names)),
        tuple(cast(list[str], based_on)),
    )
