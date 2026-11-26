"""What a pending request would do, in one plain sentence for people (and for Domo), and how
long it has waited.

Each action type has its sentence in the catalogs, ``action.describe.<type>``; the arguments of
the call fill its placeholders (``{name}``...). Those arguments were written by someone (the
model, on a person's behalf): they are inserted as text, cut to a reasonable length, and the
templates escape them when they are shown. A type without a sentence, or a sentence whose
placeholders the arguments do not fill, gets the general one (``action.describe``); one of
Domo's own tools (a kind of the open catalog, ``policy.open_catalog``) is told by the
description Domo wrote for it, in the household's language, as it is. Each sentence
is made with the ``Texts`` of whoever reads it (``texts.view``; "in ``texts``" below), and what a
model reads with ``REFERENCE``, the English one.

A note (``note.leave``) is told with all of its words, as one line: whoever has to say yes to it
reads everything they approve, whatever the length (the other values are cut). The model of anyone
but the one who wrote it is never given those words in a request (D4: a note that is not left yet
reaches no model but its author's; once left, it reaches its recipient's Domo alone, as data,
``tools.notes``): ``description_for_a_model`` tells a request of someone else as "a note to Owner (5
characters)", and ``step_sentence_for_a_model`` a step of their chain the same way; so is the
request of the one who asked, to their own model, while it waits for someone else's yes (spec 5.2:
a waiting note reaches no model). Domo is told the steps of a chain that waits, numbered
(``steps_told_of``), so that he may give it back as a whole when the person changes it: the one
who asked with all their words (unless it waits for someone else's yes), anyone else (an
approver) with a note's words left out.

A change of the owner's rules (``policy.update``) is told by what it changes, not by its
arguments: "For invitations, a simple yes will be enough." (``describe_rule_change``). The key and
the value never reach the people as the code spells them (a time zone is told by its name, as in
``Europe/Paris``: it has no other). A change of the household (an invitation, a role, a removal,
someone brought back, a cancelled invitation, a guest's new stay) says the role in words and a
guest's stay in days (``describe_household_change``). Handing the owner's role over says who gets
it and that the one who asks becomes an adult. A change of the list of successors
(``successors.set``) is one plain sentence, and the card of a request that still waits also shows
the list as it is now and as it would be, each in one line of words (``successor_lines_of``): what
the owner reads is what will be done.

The keys are told whole, too (0.6.0): a share of a key (``key.share``) says which key (its kind,
who gave it and its id, from the facts of its described action), with whom, and whether they may
use it from now on or no longer (``describe_key_share``); a limit (``spending.limit``) says the
key, the amount, the currency and the period, the one person it is for and the threshold asked,
or that it is taken away (``_limit_sentence``); a change of the rules of the house on the keys
(``keys.house``) says each change in a sentence of its own (``describe_house_rules``); the admin
key of a key (``key.admin``) says which key; asking what Domo cost (``spending.show``) says for
whom and over which period (``describe_spending_show``). The names in them are what someone
wrote: they are cut and quoted as every name of a sentence.

A change of the owner's own language says that the house's, which visitors read, follows it
(the fact ``house_follows``). A change of someone else's texts (``texts.for_someone``, spec 4.7)
says what is changed (texts written again, an earlier version brought back, the language and which
one, or the note Domo keeps on how to write to them) and for whom, never the words of the note
nor of an indication (``describe_for_someone``): Domo's words about a person reach only the
writer.

A chain of actions (``chain.run``) says how many actions it does, in order; its card also shows
each step in a sentence of its own, in order, with what came of it (``step_lines_of``): a step
described when the chain was asked for is told as its own request would be, and a step checked in
its turn by the sentence of its type, with the words of its call, and a word that says so. A step
that changes the rights shows the rights and the examples of its draft, and one that changes the
list of successors, while the chain waits, the list now and as it would be: exactly as the card of
such a request alone, so that whoever says yes to the chain reads what each step will do. A step
taken up again after it failed, and that may have acted, says that it may already have been done.
A step that waits for someone's arrival says whom it waits for; a chain paused until then is told
by the name of the invitation it waits for, and one that waits for a yes again once they joined by
the name of the one who joined (``waiting_names_of``, spec 4.3b).

The model does not know what time it is, so a request's date comes with its age in plain words
(``waiting_since``), worked out from the injected clock.

A request that a limit sent to the owner says what Domo could not tell
(``what_domo_could_not_tell``: the right's question, kept as ``ask_reason``), for the owner to
read on its card.

A change of rights (``rights.apply``) is shown with its draft, read from the database
(``draft_lines_of``): each right's summary, as the owner approves it, and each example as a
sentence the code makes from its facts (``describe_example``: ``rights.example.<type>``, a person
told by their name; an example about a role nobody has yet names the role:
``rights.example.as_role.<role>``; one about Domo acting on his own names him,
``rights.example.as_domo``; an example of the owner's own actions is the owner asking)
with the outcome the code worked out (``rights.example.outcome.<outcome>``: Domo judges whether
to ask, it asks them first as a rule says, it waits for an adult's or the owner's agreement, or it
is refused), and, after the sentence, the moment it was worked out at
when a limit reads it or the writer stated it (``describe_moment``, Ruling R25: "On a Saturday at
22:00.", "Already done 3 times in the last 7 days.", "1 guest in the household."). Of that moment,
the card tells what the limits of the example's type name: the windows of the history, the roles
and the kinds of count of the household, the day of an hour (everything, when they name none).
The writer never writes those sentences: what the owner reads is what will be applied.
"""

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, cast

from domo_common.meanings import has_meaning
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.steps import StepButton, button_of, maybe_done
from domo_core.confirmations.store import action_type_of, effective_arguments
from domo_core.household.models import Invitation
from domo_core.household.service import GUEST_DAYS_DEFAULT
from domo_core.household.succession import (
    entries_from,
    list_successors,
    preview_successors,
    successors_in_words,
)
from domo_core.llm.spending import (
    CURRENCIES,
    LIMIT_ROLES,
    LIMIT_SPLITS,
    PERIODS,
    kept_amount,
)
from domo_core.llm.types import JsonObject
from domo_core.madetools.models import OFFERED_PREFIX
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.catalog import (
    ACTION_FACTS,
    NO_KINDS,
    PROGRAM_RUN,
    TEXT_CHANGES,
    OpenKind,
    is_made,
)
from domo_core.policy.models import RightsDraft
from domo_core.policy.open_catalog import open_kinds
from domo_core.policy.rights_drafts import Draft, Example, ImaginedNow, Moment, stored_draft
from domo_core.policy.roles import ROLES, Role
from domo_core.policy.settings import (
    LIFETIME_KEY,
    SETTING_DEFAULTS,
    TIME_ZONE_KEY,
    WRITER_SECONDS_KEY,
    InvalidSetting,
    validate_setting,
)
from domo_core.texts.view import REFERENCE, Texts
from domo_core.tools.context import InvalidArguments

# The longest value of an argument that goes into a sentence, in characters.
MAX_DESCRIBED_VALUE_LENGTH: Final = 200
# The longest text of a note that goes into its sentence: a note is at most that long, so it is
# never cut (an approver must read all of what they approve).
MAX_DESCRIBED_NOTE_LENGTH: Final = 1000


def _shown(value: str, limit: int = MAX_DESCRIBED_VALUE_LENGTH) -> str:
    """``value`` on one line, cut to ``limit`` characters."""
    one_line = " ".join(value.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def describe_for_people(action: DescribedAction, arguments: JsonObject, texts: Texts) -> str:
    """One simple sentence that says what the request would do, in ``texts`` (with what its facts
    tell: a guest who stays a guest has a new stay)."""
    return describe_type(action.action_type, arguments, texts, action.facts)


def describe_type(
    action_type: str,
    arguments: JsonObject,
    texts: Texts,
    facts: Mapping[str, FactValue] | None = None,
    *,
    kinds: Mapping[str, OpenKind] = NO_KINDS,
) -> str:
    """One simple sentence that says what a call of ``action_type`` with ``arguments`` would do,
    in ``texts`` (the sentence of its type, with the words of its call, and what the ``facts`` of
    its described action tell, if it has them), or the general sentence (``action.describe``)
    when the words do not fill it. A step checked in its turn has no described action: it is
    told this way, on a card and to the yes-judge (``judge.planned_step_told``). A kind of the
    open catalog (``kinds``: one of Domo's own tools) is told by its description, as Domo
    wrote it in the household's language."""
    carries_a_yes = is_made(action_type) or action_type == PROGRAM_RUN
    if carries_a_yes and facts is not None and facts.get("yes") is True:
        # A launch of one of Domo's tools, or of a program he runs by hand, that carries a yes:
        # what it costs, when it costs; what it does is the operation's own words, told as the
        # reason of the request (``ask_why``).
        amount = facts.get("amount_cents")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            return texts("action.describe.made.yes.amount", amount=euros(amount, texts))
        return texts("action.describe.made.yes")
    kind = kinds.get(action_type)
    if kind is not None:
        return kind.description
    try:
        if action_type == "policy.update":
            return describe_rule_change(arguments, texts)
        if action_type == "note.leave":
            return describe_note(arguments, texts)
        if action_type in _HOUSEHOLD_CHANGES:
            return describe_household_change(action_type, arguments, texts, facts)
        if action_type == _CHAIN_RUN:
            return describe_chain(arguments, texts)
        if action_type == "key.share":
            return describe_key_share(arguments, texts, facts)
        if action_type == "spending.limit":
            the_key = _the_key(arguments, facts, texts)
            return _limit_sentence(
                the_key, arguments, texts, amount="amount", currency="currency", period="period"
            )
        if action_type == "keys.house":
            return describe_house_rules(arguments, texts)
        if action_type == "key.admin":
            return texts("action.describe.key.admin", key=_the_key(arguments, facts, texts))
        if action_type == "spending.show":
            return describe_spending_show(arguments, texts)
        if action_type == "texts.for_someone":
            return describe_for_someone(arguments, texts)
        if action_type == "tool.launch_when" and arguments.get("cancel") is not None:
            # Cancelling a trigger (for the house, it waits for the owner: ruling R74).
            tool = _shown(_text_of(arguments, "name"))
            return texts("action.describe.tool.launch_when.cancel", tool=tool)
        if action_type in _SET_FOR_THE_HOUSE and _for_the_house(arguments, facts):
            # What someone asks to set for the whole house waits for the owner (ruling R17,
            # Task 27): his card says so, and what it sets.
            return describe_for_the_house(action_type, arguments, texts)
        if action_type == "texts.language" and facts is not None and facts.get("house_follows"):
            # The owner's own language: the house's, which visitors read, follows it.
            language = _shown(_text_of(arguments, "language"))
            return texts("action.describe.texts.language.house", language=language)
        params = {
            name: _shown(value) for name, value in arguments.items() if isinstance(value, str)
        }
        return texts(f"action.describe.{action_type}", **params)
    except (KeyError, IndexError, ValueError, InvalidSetting):
        return texts("action.describe")


# What may be set for the whole house (``catalog.HOUSE_SET``): a timed launch, a standing
# agreement kept or withdrawn.
_SET_FOR_THE_HOUSE: Final = frozenset({"tool.launch_when", "agreement.keep", "agreement.drop"})
_AGREEMENT_PERIODS: Final = ("day", "week", "month")


def _for_the_house(arguments: JsonObject, facts: Mapping[str, FactValue] | None) -> bool:
    """Whether the call sets something for the whole house: its facts say so (an agreement), or
    its arguments do (a trigger, ``for: "house"``)."""
    return (facts is not None and facts.get("for_house") is True) or arguments.get("for") == "house"


def describe_for_the_house(action_type: str, arguments: JsonObject, texts: Texts) -> str:
    """What a call sets for the whole house, in one sentence that says so: a trigger, its tool
    and when; an agreement kept, its tool, its operation, its amount and how often; one
    withdrawn. ``ValueError`` (or ``KeyError``) for what does not read: the general sentence."""
    if action_type == "agreement.drop":
        return texts("action.describe.agreement.drop.house")
    if action_type == "agreement.keep":
        amount, times, per = (
            arguments.get("amount_cents_at_most"),
            arguments.get("times"),
            arguments.get("per"),
        )
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or not isinstance(times, int)
            or isinstance(times, bool)
            or per not in _AGREEMENT_PERIODS
        ):
            raise ValueError("an agreement has its amount, its times and its period")
        said = texts(
            "action.describe.agreement.keep.house",
            tool=_shown(_text_of(arguments, "tool").removeprefix(OFFERED_PREFIX)),
            operation=_shown(_text_of(arguments, "operation")),
            amount=euros(amount, texts),
            times=str(times),
            per=texts(f"action.describe.agreement.per.{per}"),
        )
        days = _weekdays(arguments.get("weekdays"), texts)
        return (
            said
            if days is None
            else f"{said} {texts('action.describe.agreement.weekdays', days=days)}"
        )
    return _with_what_it_is_given(_launch_for_the_house(arguments, texts), arguments, texts)


def _launch_for_the_house(arguments: JsonObject, texts: Texts) -> str:
    """When a trigger for the house launches its tool: on an arrival or a return, by a rule that
    repeats (shown as it is written, as data), or at a moment; the general sentence for a moment
    that does not read here."""
    tool = _shown(_text_of(arguments, "name"))
    on = arguments.get("on")
    if on == "arrival" or on == "return":
        return texts(f"action.describe.tool.launch_when.house.{on}", tool=tool)
    repeat = arguments.get("repeat")
    if isinstance(repeat, str) and repeat.strip():
        return texts(
            "action.describe.tool.launch_when.house.repeat",
            tool=tool,
            repeat=_shown(repeat, _SHOWN_RULE_CHARS),
        )
    when = arguments.get("when")
    if isinstance(when, str):
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            return texts(
                "action.describe.tool.launch_when.house.time", tool=tool, when=texts.date(moment)
            )
    return texts("action.describe.tool.launch_when.house", tool=tool)


# How much of what a trigger gives its tool a card shows: so many arguments, each name and value
# cut, the rest said to be there; and how much of a rule that repeats.
_SHOWN_ARGUMENTS: Final = 4
_SHOWN_ARGUMENT_CHARS: Final = 40
_SHOWN_RULE_CHARS: Final = 80


def _with_what_it_is_given(said: str, arguments: JsonObject, texts: Texts) -> str:
    """``said``, followed by what the trigger gives its tool when it launches, as data: each
    name and its value as written (a text in quotes), cut, ``_SHOWN_ARGUMENTS`` at most."""
    given = arguments.get("arguments")
    if not isinstance(given, dict) or not given:
        return said
    items = list(cast(JsonObject, given).items())
    shown = [
        f"{_shown(str(name), _SHOWN_ARGUMENT_CHARS)}: "
        f"{_shown(json.dumps(value, ensure_ascii=False), _SHOWN_ARGUMENT_CHARS)}"
        for name, value in items[:_SHOWN_ARGUMENTS]
    ]
    if len(items) > _SHOWN_ARGUMENTS:
        shown.append("…")
    return f"{said} {texts('action.describe.tool.launch_when.given', given='; '.join(shown))}"


def _weekdays(found: object, texts: Texts) -> str | None:
    """The days of the week an agreement holds on (1 Monday to 7 Sunday), in the reader's words;
    ``None`` when it holds every day."""
    if not isinstance(found, list) or not found:
        return None
    values = cast(list[object], found)
    days = [
        day
        for day in values
        if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 7
    ]
    if len(days) != len(values):
        raise ValueError("weekdays are 1 to 7")
    return ", ".join(texts(f"rights.example.weekday.{day}") for day in sorted(set(days)))


def euros(cents: int, texts: Texts) -> str:
    """An amount in whole cents of euros, as a card says it in the reader's language
    (``money.euros``: ``32.50 €``, ``32,50 €``)."""
    return texts("money.euros", units=str(cents // 100), cents=f"{cents % 100:02d}")


def describe_chain(arguments: JsonObject, texts: Texts) -> str:
    """What a ``do_in_order`` call (``{"steps": [...]}``, flattened as its request stores it) does,
    in one sentence: how many actions, in order, or the one action of a chain of one step (its card
    shows each one, ``step_lines_of``). ``ValueError`` when its steps are not a list: the caller
    then says the general sentence."""
    steps: object = arguments.get("steps")
    if not isinstance(steps, list):
        raise ValueError("the steps of a chain are a list")
    count = len(cast(list[object], steps))
    if count == 1:
        return texts("action.describe.chain.one")
    return texts("action.describe.chain", count=str(count))


def describe_note(arguments: JsonObject, texts: Texts) -> str:
    """What a ``note_leave`` call (``{"recipient", "text"}``) does, in one sentence: for whom, and
    all of the words on one line. ``ValueError`` when an argument is missing or not a text, and
    ``KeyError`` when the catalog lacks a word for it: the caller then says the general sentence."""
    recipient = _shown(_text_of(arguments, "recipient"))
    text = _shown(_text_of(arguments, "text"), MAX_DESCRIBED_NOTE_LENGTH)
    return texts("action.describe.note.leave", recipient=recipient, text=text)


def _duration(hours: int, texts: Texts) -> str:
    """``hours`` in plain words: in days when it is a whole number of days."""
    days, rest = divmod(hours, 24)
    if rest == 0:
        return texts(
            "policy.duration.day" if days == 1 else "policy.duration.days", count=str(days)
        )
    return texts(
        "policy.duration.hour" if hours == 1 else "policy.duration.hours", count=str(hours)
    )


def describe_rule_change(arguments: JsonObject, texts: Texts) -> str:
    """What a ``policy_update`` call (``{"key", "value"}``) changes, in one plain sentence.

    ``ValueError`` when the arguments are not exactly a key and a value, ``InvalidSetting`` when
    the settings do not accept them, ``KeyError`` when the catalog lacks a word for them: the
    caller then says the general sentence."""
    if set(arguments) != {"key", "value"}:
        raise ValueError("a change of the rules is a key and a value")
    key: object = arguments["key"]
    value: object = arguments["value"]
    if not isinstance(key, str) or not (value is None or isinstance(value, str | int)):
        raise ValueError("a change of the rules is a key and a value")
    validate_setting(key, value)
    if key == TIME_ZONE_KEY:
        if value is None:
            return texts("policy.change.time_zone.default")
        return texts("policy.change.time_zone", zone=str(value))
    if key == LIFETIME_KEY:
        if value is None:
            default = SETTING_DEFAULTS[LIFETIME_KEY]
            return texts("policy.change.lifetime.default", duration=_duration(int(default), texts))
        return texts("policy.change.lifetime", duration=_duration(int(value), texts))
    if key == WRITER_SECONDS_KEY:
        if value is None:
            default = SETTING_DEFAULTS[WRITER_SECONDS_KEY]
            return texts("policy.change.writer_seconds.default", seconds=str(default))
        return texts("policy.change.writer_seconds", seconds=str(value))
    raise ValueError("a change of the rules is one of the settings")


# The changes of the household that have a sentence of their own (``describe_household_change``).
_HOUSEHOLD_CHANGES: Final = frozenset(
    {
        "household.invite",
        "household.change_role",
        "household.remove",
        "household.bring_back",
        "household.transfer_ownership",
        "household.cancel_invitation",
    }
)


# The action type of a chain of actions, told by its number of steps (its card shows each one).
_CHAIN_RUN: Final = "chain.run"


def _text_of(arguments: JsonObject, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} is a text")
    return value


def describe_for_someone(arguments: JsonObject, texts: Texts) -> str:
    """What a ``texts_for_someone`` call (``{"person", "what", ...}``) does, in one sentence: what
    is changed and for whom, and the language for a change of language; never the words of a note
    or of an indication. ``ValueError`` when ``what`` is none of the four or a text is missing:
    the caller then says the general sentence."""
    what = _text_of(arguments, "what")
    if what not in TEXT_CHANGES:
        raise ValueError("what is changed of someone's texts")
    person = _shown(_text_of(arguments, "person"))
    if what == "language":
        language = _shown(_text_of(arguments, "language"))
        return texts("action.describe.texts.for_someone.language", person=person, language=language)
    return texts(f"action.describe.texts.for_someone.{what}", person=person)


# The id of a key, as the code makes it (``llm.models.new_provider_id``).
_KEY_ID: Final = re.compile(r"p-[0-9a-f]{8}")
_SHARED_WITH: Final = frozenset({"person", "household", "my_invitees"})


def _the_key(arguments: JsonObject, facts: Mapping[str, FactValue] | None, texts: Texts) -> str:
    """The key a share or a limit is about, in words: its kind (``_kind_in_words``), who gave it
    and its id, from the facts of its described action (``key_kind``, ``key_of``); by its id alone
    without them (a step checked in its turn). ``ValueError`` when the call names no key id."""
    key_id = arguments.get("key")
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise ValueError("a key is named by its id")
    kind = None if facts is None else facts.get("key_kind")
    giver = None if facts is None else facts.get("key_of")
    if isinstance(kind, str) and isinstance(giver, str):
        return texts(
            "action.describe.key.named",
            kind=_kind_in_words(kind, texts),
            giver=_shown(giver),
            id=key_id,
        )
    return texts("action.describe.key.id", id=key_id)


def _kind_in_words(kind: str, texts: Texts) -> str:
    """A key's kind as people read it (``chat.key.new.kind.<kind>``, as the form of a new key
    names it: "OpenAI", "Other server (OpenAI-compatible)"), or its code for a kind the catalog
    has no word for."""
    key = f"chat.key.new.kind.{kind}"
    return texts(key) if has_meaning(key) else _shown(kind)


def _amount_in_words(value: object, currency: str, texts: Texts) -> str:
    """An amount as the call gives it (a number), as the limit keeps it (``kept_amount``), in
    ``currency``, as people read an amount (``texts.amount``: ``$0.50``, ``0,50 €``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("an amount is a number")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("an amount is above 0")
    return texts.amount(kept_amount(amount), currency)


def _limit_sentence(
    the_key: str,
    arguments: JsonObject,
    texts: Texts,
    *,
    amount: str,
    currency: str,
    period: str,
    remove: str = "remove",
    role: str = "role",
    split: str = "split",
) -> str:
    """A firm limit set or taken away on ``the_key`` (already in words), as a call gives it under
    its own names (``amount``, ``currency``, ``period``, ``remove``, ``role``, ``split`` and
    ``warn_at_percent``): the amount, the currency and the period (a month by default), the one
    person it is for if the call names one, or the role (each of them, or all of them together),
    and the threshold of whoever sets it. ``ValueError`` for what does not read."""
    person = arguments.get("person")
    who = None if person is None else _shown(_text_of(arguments, "person"))
    removing = arguments.get(remove, False)
    if not isinstance(removing, bool):
        raise ValueError(f"{remove} is true or false")
    for_role: object = arguments.get(role)
    way: object = arguments.get(split)
    if for_role is None and way is not None:
        raise ValueError("a split goes with its role")
    if for_role is not None:
        if who is not None or for_role not in LIMIT_ROLES or way not in LIMIT_SPLITS:
            raise ValueError("a limit for a role has its split, and no person")
        if removing:
            return texts(
                f"action.describe.limit.remove.role.{way}",
                key=the_key,
                role=texts(f"action.describe.limit.role.name.{for_role}"),
                roles=texts(f"action.describe.limit.role.names.{for_role}"),
            )
    if removing:
        if who is None:
            return texts("action.describe.limit.remove", key=the_key)
        return texts("action.describe.limit.remove.for", key=the_key, person=who)
    currency_of: object = arguments.get(currency)
    period_of: object = arguments.get(period) or "month"
    if currency_of not in CURRENCIES or period_of not in PERIODS:
        raise ValueError("a limit has a currency and a period of the catalog")
    if for_role is None:
        sentence_key = "action.describe.limit" if who is None else "action.describe.limit.for"
    else:
        sentence_key = f"action.describe.limit.role.{way}"
    sentence = texts(
        sentence_key,
        key=the_key,
        person=who or "",
        role=texts(f"action.describe.limit.role.name.{for_role}") if for_role else "",
        roles=texts(f"action.describe.limit.role.names.{for_role}") if for_role else "",
        amount=_amount_in_words(arguments.get(amount), str(currency_of), texts),
        period=texts(f"action.describe.limit.period.{period_of}"),
    )
    warn: object = arguments.get("warn_at_percent")
    if warn is None:
        return sentence
    if isinstance(warn, bool) or not isinstance(warn, int):
        raise ValueError("a threshold is a whole number")
    return f"{sentence} {texts('action.describe.limit.warn', percent=str(warn))}"


def describe_key_share(
    arguments: JsonObject, texts: Texts, facts: Mapping[str, FactValue] | None = None
) -> str:
    """What a ``key_share`` call does, in one plain sentence: which key (``_the_key``), with whom
    (a person by their name, the whole household, or the people its giver invited) and whether
    they may use it from now on, or no longer. ``ValueError`` for what does not read."""
    the_key = _the_key(arguments, facts, texts)
    allowed = arguments.get("allowed", True)
    with_: object = arguments.get("with")
    if not isinstance(allowed, bool) or with_ not in _SHARED_WITH:
        raise ValueError("a share says with whom, and whether they may use the key")
    way = "allow" if allowed else "deny"
    if with_ == "person":
        who = _shown(_text_of(arguments, "person"))
        return texts(f"action.describe.key.share.person.{way}", key=the_key, person=who)
    return texts(f"action.describe.key.share.{with_}.{way}", key=the_key)


def describe_spending_show(arguments: JsonObject, texts: Texts) -> str:
    """What a ``spending_show`` call asks, in one plain sentence: what Domo cost over the day,
    the week or the month (the month by default), for the person who asks or for the one person it
    names. ``ValueError`` for what does not read."""
    period: object = arguments.get("period") or "month"
    if period not in PERIODS:
        raise ValueError("a period of the catalog")
    if arguments.get("person") is None:
        return texts(f"action.describe.spending.show.{period}")
    who = _shown(_text_of(arguments, "person"))
    return texts(f"action.describe.spending.show.for.{period}", person=who)


def describe_house_rules(arguments: JsonObject, texts: Texts) -> str:
    """What a ``keys_house`` call changes: one sentence for the rules of the house on the keys,
    then one per change, in the order of the tool: who may use the general key (``person``,
    ``on_general_key``), what an invitee uses (``invitee_default``), whether invitees use the
    general key when their host does (``invitee_general_key``), whether the general key asks
    someone without a key for one (``upkeep_on_general_key``), and the limit of the general key
    (``_limit_sentence``). ``ValueError`` for a call that changes nothing, or does not read."""
    changes: list[str] = []
    person = arguments.get("person")
    who = None if person is None else _shown(_text_of(arguments, "person"))
    on_general: object = arguments.get("on_general_key")
    if on_general is not None:
        if who is None or on_general not in ("allow", "deny"):
            raise ValueError("on_general_key is allow or deny, for a person")
        changes.append(texts(f"action.describe.keys.house.{on_general}", person=who))
    invitee: object = arguments.get("invitee_default")
    if invitee is not None:
        if invitee not in ("inviter", "own"):
            raise ValueError("invitee_default is inviter or own")
        changes.append(texts(f"action.describe.keys.house.invitee.{invitee}"))
    invitee_general: object = arguments.get("invitee_general_key")
    if invitee_general is not None:
        if invitee_general not in ("follow", "never"):
            raise ValueError("invitee_general_key is follow or never")
        changes.append(texts(f"action.describe.keys.house.invitee_general.{invitee_general}"))
    upkeep = arguments.get("upkeep_on_general_key")
    if upkeep is not None:
        if not isinstance(upkeep, bool):
            raise ValueError("upkeep_on_general_key is true or false")
        changes.append(texts(f"action.describe.keys.house.upkeep.{'on' if upkeep else 'off'}"))
    if arguments.get("limit_amount") is not None or arguments.get("remove_limit") is True:
        changes.append(
            _limit_sentence(
                texts("action.describe.key.general"),
                arguments,
                texts,
                amount="limit_amount",
                currency="limit_currency",
                period="limit_period",
                remove="remove_limit",
                role="limit_role",
                split="limit_split",
            )
        )
    if not changes:
        raise ValueError("a change of the rules of the keys changes something")
    return " ".join([texts("action.describe.keys.house"), *changes])


def _stay(arguments: JsonObject, texts: Texts) -> str:
    """A guest's stay in words: the call's ``guest_days``, or the default."""
    days: object = arguments.get("guest_days")
    if days is None:
        days = GUEST_DAYS_DEFAULT
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("guest_days is a whole number")
    return _duration(days * 24, texts)


def describe_household_change(
    action_type: str,
    arguments: JsonObject,
    texts: Texts,
    facts: Mapping[str, FactValue] | None = None,
) -> str:
    """What a change of the household does, in one plain sentence: who, and the role in words
    (``household.role.<role>``), or for a guest (invited, or made one) how long they may stay; a
    guest who stays a guest (``facts``, the described action's: ``current_role``) is given a new
    stay, from now (spec 5.5).

    ``ValueError`` when an argument is not of its kind, ``KeyError`` when the catalog lacks a
    word for it: the caller then says the general sentence."""
    if action_type in ("household.invite", "household.change_role", "household.bring_back"):
        key = action_type.replace("household.", "action.describe.household.")
        name = _shown(_text_of(arguments, "display_name" if key.endswith("invite") else "person"))
        role = _text_of(arguments, "role")
        stays = action_type == "household.change_role" and role == "guest"
        if stays and facts is not None and facts.get("current_role") == "guest":
            return texts(f"{key}.stay", name=name, duration=_stay(arguments, texts))
        if role == "guest":
            # A guest's stay, whether invited, made a guest or brought back (R78).
            sentence = texts(f"{key}.guest", name=name, duration=_stay(arguments, texts))
        else:
            sentence = texts(key, name=name, role=texts(f"household.role.{role}"))
        told: object = arguments.get("uses_my_key")
        if action_type == "household.invite" and isinstance(told, bool):
            # Whose AI keys they use: whoever says yes reads it.
            keys = "inviter_key" if told else "own_key"
            return f"{sentence} {texts(f'action.describe.household.invite.{keys}')}"
        if action_type == "household.bring_back" and arguments.get("display_name") is not None:
            # They come back under another name: whoever says yes reads it.
            new_name = _shown(_text_of(arguments, "display_name"))
            renamed = texts("action.describe.household.bring_back.renamed", name=new_name)
            return f"{sentence} {renamed}"
        return sentence
    if action_type == "household.remove":
        return texts("action.describe.household.remove", name=_shown(_text_of(arguments, "person")))
    if action_type == "household.transfer_ownership":
        return texts(
            "action.describe.household.transfer_ownership",
            name=_shown(_text_of(arguments, "person")),
        )
    return texts(
        "action.describe.household.cancel_invitation",
        name=_shown(_text_of(arguments, "display_name")),
    )


# The action type of a note, whose words are someone else's.
_NOTE_LEAVE: Final = "note.leave"


def note_words_of(action_type: str, arguments: JsonObject) -> str | None:
    """The words of a note (``note.leave``, ``{"recipient", "text"}``) as its sentence shows them:
    all of them, on one line (``describe_note``); ``None`` for a call that is no note's, or whose
    words do not read."""
    text = arguments.get("text")
    if action_type != _NOTE_LEAVE or not isinstance(text, str):
        return None
    return _shown(text, MAX_DESCRIBED_NOTE_LENGTH)


def _described(
    c: Confirmation, arguments: JsonObject, texts: Texts, words: str | None = None
) -> str:
    """What ``c`` does with ``arguments``, in ``texts``; ``words``, when given, stands for the
    words of a note (a hole of the head of a card, ``texts.cards``)."""
    try:
        action = DescribedAction.from_json(c.action)
    except ValueError:
        return texts("action.describe")
    if words is not None and note_words_of(action.action_type, arguments) is not None:
        arguments = {**arguments, "text": words}
    return describe_for_people(action, arguments, texts)


def request_description(c: Confirmation, texts: Texts, *, words: str | None = None) -> str:
    """``describe_for_people`` for a stored request: what it does, with the arguments a change
    gave it, if any (``effective_arguments``: what a yes runs). A stored action that does not
    read back gets the general sentence. ``words``, when given, stands for the words of a note."""
    return _described(c, effective_arguments(c), texts, words)


def note_for_a_model(arguments: JsonObject) -> str | None:
    """A note (``{"recipient", "text"}``) as a model other than its author's is told of it, and
    their own while it waits for someone else's yes (spec 5.2): for whom, and how long it is, never
    its words (D4). ``None`` when the arguments do not read as a note."""
    recipient, text = arguments.get("recipient"), arguments.get("text")
    if not isinstance(recipient, str) or not isinstance(text, str):
        return None
    return f"a note to {_shown(recipient)} ({_counted(len(text), 'character')})"


def description_for_a_model(c: Confirmation) -> str:
    """What the request does, as the model of someone other than the one who asked is told
    (an approver's, spec 5.1), and the one who asked's own while it waits for someone else's yes
    (spec 5.2): ``request_description`` in English, but a note (``note.leave``) is told by whom it
    is for and its length, never its words (D4: the words of someone else, and a waiting note,
    reach no model). A note whose arguments do not read back gets the general sentence."""
    arguments = effective_arguments(c)
    if action_type_of(c) != "note.leave":
        return _described(c, arguments, REFERENCE)
    note = note_for_a_model(arguments)
    return REFERENCE("action.describe") if note is None else note


def description_before_the_change(
    c: Confirmation, replaced: Confirmation | None, texts: Texts, *, words: str | None = None
) -> str | None:
    """What the request was before the person asked for a change, if they did: its own
    ``arguments`` when a change ran with others, or the request it ``replaced`` (a change made it
    a new request). ``None`` for a request nobody changed. ``words``, when given, stands for the
    words of a note then (``words_before_the_change``)."""
    if c.amended_arguments is not None:
        return _described(c, dict(c.arguments), texts, words)
    if replaced is not None:
        return request_description(replaced, texts, words=words)
    return None


def words_before_the_change(c: Confirmation, replaced: Confirmation | None) -> str | None:
    """The words of the note the request was before a change, as ``description_before_the_change``
    shows them; ``None`` when it was no note, or nobody changed it."""
    if c.amended_arguments is not None:
        return note_words_of(action_type_of(c), dict(c.arguments))
    if replaced is not None:
        return note_words_of(action_type_of(replaced), effective_arguments(replaced))
    return None


def domo_question_of(c: Confirmation) -> str | None:
    """What Domo could not tell when a limit sent the request to the owner (its ``ask_reason``,
    the question of the rights), as its card shows it; ``None`` when there is nothing of the
    kind."""
    return _shown(c.ask_reason, 300) if c.ask_reason else None


def what_domo_could_not_tell(
    c: Confirmation, texts: Texts, *, question: str | None = None
) -> str | None:
    """The sentence that says what Domo could not tell when a limit sent the request to the
    owner (``domo_question_of``), or ``None`` when there is nothing of the kind. ``question``,
    when given, stands for the question itself (a hole of the head of a card)."""
    shown = domo_question_of(c)
    if shown is None:
        return None
    return texts("confirmation.ask_reason", question=shown if question is None else question)


# --- a change of rights ---------------------------------------------------------------------------

# The action type whose request is shown with its draft, and the prefix of its target.
_RIGHTS_APPLY: Final = "rights.apply"
_DRAFT_TARGET: Final = "draft:"
_PERSON_PREFIX: Final = "person:"


def _fact_words(
    name: str, kind: str, value: FactValue, names: Mapping[str, str], texts: Texts
) -> str:
    """One fact of an example in words: a role by its name, a person by their name (or as someone
    the code does not know), a guest's stay in days, a yes or no."""
    if value is None:
        return texts("rights.example.no_end" if name == "days" else "rights.example.not_set")
    if kind == "role":
        return texts(f"household.role.{value}")
    if kind == "person":
        person = str(value).removeprefix(_PERSON_PREFIX)
        return names.get(person) or texts("rights.example.someone")
    if kind == "boolean":
        return texts("rights.example.yes" if value else "rights.example.no")
    if name == "days" and isinstance(value, int):
        return _duration(value * 24, texts)
    return _shown(str(value))


def describe_example(
    action_type: str,
    facts: Mapping[str, FactValue],
    *,
    who: str,
    names: Mapping[str, str],
    texts: Texts,
    kinds: Mapping[str, OpenKind] = NO_KINDS,
) -> str:
    """The situation of an example, in one sentence of ``texts``: ``who`` (the name of the person
    who asks) and the action of ``action_type`` with its ``facts`` (``rights.example.<type>``). A
    person in the facts is told by their name (``names``, by id), or as someone. Facts that do not
    fit the sentence get the general one (``rights.example.generic``). A kind of the open catalog
    (``kinds``) is told by its description (``rights.example.made``)."""
    kind = kinds.get(action_type)
    if kind is not None:
        # Its description whole, on one line: Domo wrote it, 300 characters at most.
        action = " ".join(kind.description.split())
        return texts("rights.example.made", who=_shown(who), action=action)
    declared = ACTION_FACTS.get(action_type, {})
    try:
        words = {
            name: _fact_words(name, declared[name], value, names, texts)
            for name, value in facts.items()
        }
        return texts(f"rights.example.{action_type}", who=_shown(who), **words)
    except (KeyError, IndexError, ValueError):
        return texts("rights.example.generic", who=_shown(who))


def example_situation(
    example: Example,
    names: Mapping[str, str],
    texts: Texts,
    kinds: Mapping[str, OpenKind] = NO_KINDS,
) -> str:
    """The situation of ``example`` in one sentence of ``texts`` (``describe_example``, with the
    open ``kinds``): who asks is told by their name (``names``, by id), or by their role for an
    example about a role nobody has yet ("A guest"), or as Domo when he acts on his own."""
    if example.for_domo:
        who = texts("rights.example.as_domo")
    elif example.role is not None:
        who = texts(f"rights.example.as_role.{example.role}")
    else:
        who = names.get(str(example.person_id)) or texts("rights.example.someone")
    return describe_example(
        example.action_type, example.facts, who=who, names=names, texts=texts, kinds=kinds
    )


# A name in an expression: what a limit names of the moment is told of it.
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WINDOWS: Final = ("day", "week", "month")
_COUNTS: Final = ("people", "invitations")


def _counted_words(key: str, count: int, texts: Texts) -> str:
    """``count`` with the words of ``key`` (``<key>.none``, ``.one`` or ``.many``)."""
    form = "none" if count == 0 else "one" if count == 1 else "many"
    return texts(f"{key}.{form}", count=str(count))


def _listed(items: list[str], texts: Texts) -> str:
    """``items`` as one list in words ("1 adult, 2 children and no guest")."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {texts('rights.example.and')} {items[-1]}"


def _hour_words(now: ImaginedNow, dated: bool, texts: Texts) -> str:
    """The hour of an example ("on a Saturday at 22:00"), with its day when ``dated``."""
    time = texts(
        "rights.example.time.whole" if now.minute == 0 else "rights.example.time",
        hour=str(now.hour),
        minute=f"{now.minute:02d}",
    )
    weekday = texts(f"rights.example.weekday.{now.weekday}")
    if dated and now.date is not None:
        day = date.fromisoformat(now.date)
        when = texts(
            "rights.example.date",
            day=str(day.day),
            month=texts(f"rights.example.month.{day.month}"),
            year=str(day.year),
        )
        return texts("rights.example.moment.now.dated", weekday=weekday, date=when, time=time)
    return texts("rights.example.moment.now", weekday=weekday, time=time)


def _household_words(counts: Mapping[Role, int], roles: list[Role], texts: Texts) -> str:
    return _listed(
        [
            _counted_words(f"rights.example.count.{role}", counts.get(role, 0), texts)
            for role in roles
        ],
        texts,
    )


def describe_moment(moment: Moment, limits: Iterable[str], texts: Texts) -> str | None:
    """The moment of an example in one sentence of ``texts``, or ``None`` when it holds none: its
    hour, its history and its household, as far as ``limits`` (the expressions of the limits of
    its type) name them (see the module docstring)."""
    named = {name for expression in limits for name in _NAME.findall(expression)}
    parts: list[str] = []
    if moment.now is not None:
        parts.append(_hour_words(moment.now, "date" in named, texts))
    if moment.history is not None:
        done = {
            "day": moment.history.day,
            "week": moment.history.week,
            "month": moment.history.month,
        }
        windows = [w for w in _WINDOWS if w in named] or list(_WINDOWS)
        parts.extend(
            _counted_words(f"rights.example.moment.history.{w}", done[w], texts) for w in windows
        )
    if moment.people is not None or moment.invitations is not None:
        roles: list[Role] = [role for role in ROLES if role in named] or list(ROLES)
        kinds = [kind for kind in _COUNTS if kind in named] or list(_COUNTS)
        for kind, counts in (("people", moment.people), ("invitations", moment.invitations)):
            if kind in kinds and counts is not None:
                words = _household_words(counts, roles, texts)
                parts.append(texts(f"rights.example.moment.{kind}", counts=words))
    if not parts:
        return None
    sentence = "; ".join(parts)
    return texts("rights.example.moment", parts=sentence[:1].upper() + sentence[1:])


def example_moment(example: Example, draft: Draft, texts: Texts) -> str | None:
    """The moment ``example`` of ``draft`` was worked out at, in one sentence of ``texts``
    (``describe_moment``, with the limits of the draft's changes of its type), or ``None``."""
    limits = [
        change.limit_expression
        for change in draft.changes
        if not change.remove
        and change.limit_expression is not None
        and change.action_type == example.action_type
    ]
    return describe_moment(example.moment, limits, texts)


@dataclass(frozen=True)
class ExampleLine:
    """An example as a card shows it: the situation, what happens, and the moment it was worked
    out at, if the owner reads one (``example_moment``)."""

    situation: str
    outcome: str
    moment: str | None = None


@dataclass(frozen=True)
class DraftLines:
    """A draft of rights as a card shows it: the summary of each right, and each example."""

    summaries: tuple[str, ...]
    examples: tuple[ExampleLine, ...]


def describe_rights_draft(
    content: object, texts: Texts, kinds: Mapping[str, OpenKind] = NO_KINDS
) -> DraftLines | None:
    """The stored ``content`` of a draft (``rights_drafts.draft_as_json``) in ``texts``, with the
    open ``kinds`` its examples may be about, or ``None`` when it does not read back."""
    try:
        stored = stored_draft(content)
    except ValueError:
        return None
    names = stored.names
    return DraftLines(
        summaries=tuple(change.summary for change in stored.draft.changes),
        examples=tuple(
            ExampleLine(
                situation=example_situation(example, names, texts, kinds),
                outcome=texts(f"rights.example.outcome.{example.expected}"),
                moment=example_moment(example, stored.draft, texts),
            )
            for example in stored.draft.examples
        ),
    )


def _draft_in(action: Mapping[str, object] | None) -> uuid.UUID | None:
    """The draft a stored change of rights (``rights.apply``) applies, from its action's target;
    ``None`` for any other action."""
    if action is None:
        return None
    target = action.get("target")
    if action.get("action_type") != _RIGHTS_APPLY or not isinstance(target, str):
        return None
    try:
        return uuid.UUID(target.removeprefix(_DRAFT_TARGET))
    except ValueError:
        return None


def draft_of(c: Confirmation) -> uuid.UUID | None:
    """The draft a change of rights (``rights.apply``) applies, from its stored action's target;
    ``None`` for any other request."""
    return _draft_in(c.action)


async def _drafts_by_id(
    session: AsyncSession, draft_ids: Iterable[uuid.UUID], texts: Texts
) -> dict[uuid.UUID, DraftLines]:
    """These drafts, by draft id, in ``texts`` (those that do not read back are left out), with
    the kinds of the open catalog as they are now (``open_kinds``)."""
    wanted = set(draft_ids)
    if not wanted:
        return {}
    result = await session.execute(
        select(RightsDraft.id, RightsDraft.content).where(RightsDraft.id.in_(wanted))
    )
    kinds = await open_kinds(session)
    lines: dict[uuid.UUID, DraftLines] = {}
    for draft_id, content in result.all():
        shown = describe_rights_draft(content, texts, kinds)
        if shown is not None:
            lines[draft_id] = shown
    return lines


async def draft_lines_of(
    session: AsyncSession, requests: Iterable[Confirmation], texts: Texts
) -> dict[uuid.UUID, DraftLines]:
    """The drafts of the changes of rights among ``requests``, by request id, in ``texts``."""
    wanted = {c.id: draft for c in requests if (draft := draft_of(c)) is not None}
    drafts = await _drafts_by_id(session, wanted.values(), texts)
    return {
        request_id: drafts[draft_id]
        for request_id, draft_id in wanted.items()
        if draft_id in drafts
    }


# --- a change of the list of successors -----------------------------------------------------------

# The action type whose request is shown with the list before and after.
_SUCCESSORS_SET: Final = "successors.set"


@dataclass(frozen=True)
class SuccessorLines:
    """A change of the list of successors as a card shows it: the list as it is now (``before``)
    and as it would be (``after``), each in one line of words."""

    before: str
    after: str


async def successor_lines_of(
    session: AsyncSession, requests: Iterable[Confirmation], texts: Texts, now: datetime
) -> dict[uuid.UUID, SuccessorLines]:
    """The lists of the changes of successors among ``requests`` that still wait for a yes, by
    request id, in ``texts``: the list now (read at ``now``) and the list the request would make
    (``preview_successors``, with the people found again by their name). A request whose list can
    no longer be read (someone on it left, say) gets none: its card still says what it would do.
    Reads only."""
    wanted = [
        c
        for c in requests
        if c.status == "pending" and c.action.get("action_type") == _SUCCESSORS_SET
    ]
    if not wanted:
        return {}
    before = successors_in_words(await list_successors(session, now), texts)
    lines: dict[uuid.UUID, SuccessorLines] = {}
    for c in wanted:
        shown = await _successor_change(session, effective_arguments(c), before, texts, now)
        if shown is not None:
            lines[c.id] = shown
    return lines


async def _successor_change(
    session: AsyncSession, arguments: JsonObject, before: str, texts: Texts, now: datetime
) -> SuccessorLines | None:
    """A change of the list of successors made by a call with ``arguments`` (``successors_set``):
    ``before``, the list now in words, and the list it would make; ``None`` when that list can no
    longer be read."""
    try:
        entries = await entries_from(session, arguments.get("successors"), now)
        after = await preview_successors(session, entries, now)
    except (InvalidArguments, ValueError):
        return None
    return SuccessorLines(before=before, after=successors_in_words(after, texts))


# --- the steps of a chain -------------------------------------------------------------------------


@dataclass(frozen=True)
class StepLine:
    """A step of a chain as its card shows it: its ``position`` (from 0), what it does in one
    ``sentence``, whether it is ``checked_in_turn`` (it could not be described when the chain was
    asked for), and its ``state`` (``planned``, ``done``, ``failed`` or ``stopped``). ``draft`` is,
    for a change of rights, the rights and the examples of its draft, and ``successors``, for a
    change of the list of successors that is still to be done in a chain that waits, the list now
    and as it would be (for the owner's eyes only: ``confirmations.routes``). ``action_type`` is
    its type. ``maybe_done``: a step still to run, taken from a step
    that failed and may have acted (``steps.maybe_done``), which may already have been done: its
    card says so (spec 4.4). ``button``: what a step done left for the one who asked behind a
    button (``steps.button_of``, from the card on its row: an invitation, the provider of a key),
    which the card of the chain shows them, in any state of the request (spec 4.5)."""

    position: int
    sentence: str
    checked_in_turn: bool
    state: str
    action_type: str
    draft: DraftLines | None = None
    successors: SuccessorLines | None = None
    maybe_done: bool = False
    button: StepButton | None = None


def step_sentence(step: ConfirmationStep, texts: Texts) -> str:
    """What a step does, in one sentence of ``texts``: as its own request would be told, or, for
    a step checked in its turn, the sentence of its type with the words of its call, and a word
    that says it is checked in its turn; and, for a step that waits for someone's arrival, whom it
    waits for."""
    arguments: JsonObject = dict(step.arguments)
    if step.action is None:
        sentence = describe_type(step.action_type, arguments, texts)
        sentence = f"{sentence} {texts('confirmation.step.checked_in_turn')}"
    else:
        try:
            action = DescribedAction.from_json(step.action)
        except ValueError:
            return texts("action.describe")
        sentence = describe_for_people(action, arguments, texts)
    if step.after_arrival_of is None:
        return sentence
    waits = texts("confirmation.step.after_arrival_of", name=_shown(step.after_arrival_of))
    return f"{sentence} {waits}"


def step_sentence_for_a_model(step: ConfirmationStep) -> str:
    """``step_sentence`` in English, as the model of someone other than the one who asked is told
    of the step (an approver's, spec 5.1): a note is told by whom it is for and its length, never
    its words (``description_for_a_model``)."""
    if step.action_type != "note.leave":
        return step_sentence(step, REFERENCE)
    sentence = note_for_a_model(dict(step.arguments)) or REFERENCE("action.describe")
    if step.after_arrival_of is None:
        return sentence
    waits = REFERENCE("confirmation.step.after_arrival_of", name=_shown(step.after_arrival_of))
    return f"{sentence} {waits}"


def note_told_as(step: ConfirmationStep) -> frozenset[str]:
    """The sentences a model may give back for the words of the note step ``step``, which it was
    told without its words (``step_sentence_for_a_model``): the note as told, with and without
    whom it waits for; none for a step that is no note."""
    note = note_for_a_model(dict(step.arguments)) if step.action_type == "note.leave" else None
    return frozenset() if note is None else frozenset({note, step_sentence_for_a_model(step)})


async def steps_told_of(
    session: AsyncSession, requests: Iterable[Confirmation], *, someone_else: bool
) -> dict[uuid.UUID, tuple[str, ...]]:
    """The steps of the chains among ``requests``, by request id, as Domo is told of them, so
    that he may give a chain back as a whole when the person changes it (``confirmation_amend``):
    one English sentence each, in order, numbered from 1, ``(done)`` before a step already done.
    With ``someone_else``, they are told to the model of someone other than the one who asked (an
    approver's, spec 5.1), or to the one who asked's for their chain that waits for someone else's
    yes (spec 5.2): a note without its words (``step_sentence_for_a_model``, D4: a waiting note
    reaches no model); else to the one who asked, with their own words. A request that is no
    chain has none. Reads only."""
    chains = {c.id for c in requests if c.chain_basis is not None}
    if not chains:
        return {}
    result = await session.execute(
        select(ConfirmationStep)
        .where(ConfirmationStep.confirmation_id.in_(chains))
        .order_by(ConfirmationStep.confirmation_id, ConfirmationStep.position)
    )
    told: dict[uuid.UUID, list[str]] = {}
    for step in result.scalars():
        sentence = (
            step_sentence_for_a_model(step) if someone_else else step_sentence(step, REFERENCE)
        )
        done = "(done) " if step.status == "done" else ""
        told.setdefault(step.confirmation_id, []).append(f"{step.position + 1}. {done}{sentence}")
    return {request_id: tuple(steps) for request_id, steps in told.items()}


async def step_lines_of(
    session: AsyncSession, requests: Iterable[Confirmation], texts: Texts, now: datetime
) -> dict[uuid.UUID, tuple[StepLine, ...]]:
    """The steps of the chains among ``requests``, by request id, in order, in ``texts`` (a
    request that is no chain has none), each with the draft of a change of rights and, for a
    change of the list of successors still to be done in a chain that waits, the list now (read at
    ``now``) and as it would be. Reads only."""
    chains = {c.id: c for c in requests if c.chain_basis is not None}
    if not chains:
        return {}
    result = await session.execute(
        select(ConfirmationStep)
        .where(ConfirmationStep.confirmation_id.in_(set(chains)))
        .order_by(ConfirmationStep.confirmation_id, ConfirmationStep.position)
        .execution_options(populate_existing=True)
    )
    steps = list(result.scalars())
    drafts = await _drafts_by_id(
        session, (d for step in steps if (d := _draft_in(step.action)) is not None), texts
    )
    before: str | None = None
    lines: dict[uuid.UUID, list[StepLine]] = {}
    for step in steps:
        draft = _draft_in(step.action)
        successors: SuccessorLines | None = None
        if (
            step.action_type == _SUCCESSORS_SET
            and step.status == "planned"
            and chains[step.confirmation_id].status == "pending"
        ):
            if before is None:
                before = successors_in_words(await list_successors(session, now), texts)
            successors = await _successor_change(session, dict(step.arguments), before, texts, now)
        lines.setdefault(step.confirmation_id, []).append(
            StepLine(
                position=step.position,
                sentence=step_sentence(step, texts),
                checked_in_turn=step.action is None,
                state=step.status,
                action_type=step.action_type,
                draft=None if draft is None else drafts.get(draft),
                successors=successors,
                maybe_done=maybe_done(step),
                button=(
                    button_of(step.position, step.result)
                    if step.status == "done" and step.result is not None
                    else None
                ),
            )
        )
    return {request_id: tuple(found) for request_id, found in lines.items()}


async def waiting_names_of(
    session: AsyncSession, requests: Iterable[Confirmation]
) -> dict[uuid.UUID, tuple[str, bool]]:
    """For each of ``requests`` that waits, or waited, for someone's arrival
    (``waiting_for_invitation_id``), the name of that invitation and whether it was accepted, by
    request id: whom a paused chain waits for, or who joined for a chain that went on (spec 4.3b).
    Reads only."""
    wanted = {
        c.id: c.waiting_for_invitation_id
        for c in requests
        if c.waiting_for_invitation_id is not None
    }
    if not wanted:
        return {}
    result = await session.execute(
        select(Invitation.id, Invitation.display_name, Invitation.accepted_at).where(
            Invitation.id.in_(set(wanted.values()))
        )
    )
    found = {
        invitation_id: (name, accepted_at is not None)
        for invitation_id, name, accepted_at in result.all()
    }
    return {
        request_id: found[invitation_id]
        for request_id, invitation_id in wanted.items()
        if invitation_id in found
    }


def _counted(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def waiting_since(created_at: datetime, now: datetime) -> str:
    """When the request was made and how long it has waited, for Domo: for instance
    ``2026-10-30T10:00Z (waiting 3 days)``. The age is in whole minutes, hours or days, rounded
    down."""
    seconds = int((now - created_at).total_seconds())
    if seconds < 60:
        age = "less than a minute"
    elif seconds < 3600:
        age = _counted(seconds // 60, "minute")
    elif seconds < 86400:
        age = _counted(seconds // 3600, "hour")
    else:
        age = _counted(seconds // 86400, "day")
    return f"{created_at.astimezone(UTC):%Y-%m-%dT%H:%M}Z (waiting {age})"
