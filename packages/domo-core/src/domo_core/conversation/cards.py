"""A card in words: the sentence of an action's card, whoever reads it and wherever.

A card (``{"key": <catalog key>, "params": {...}, "dates": {...}}``) is what an action keeps of
itself (``message_actions``, ``confirmations``). ``card_sentence`` turns it into a sentence in the
reader's texts, with no page, no button and no address: the same sentence serves a page, a voice
or a message. What a page adds to it (buttons, links, forms) is ``channels.page.cards``. Which
actions of an answer have a card at all is ``shown_actions``, the same live and once stored, on
every channel.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Final, Protocol, cast

from domo_common.meanings import has_meaning

from domo_core.chat.models import MessageAction
from domo_core.policy.catalog import shown
from domo_core.texts import cards
from domo_core.texts.cards import HeadSource
from domo_core.texts.view import Texts

# Text a PostgreSQL ``text`` column cannot hold: the NUL character, and (once encoded) the lone
# surrogates that a broken stream can produce.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def storable(text: str) -> str:
    """``text`` without what the database and a UTF-8 stream cannot carry."""
    return _LONE_SURROGATE.sub("�", text.replace("\x00", ""))


class Attempt(Protocol):
    """An action as it comes in an answer (``chat.agent.ActionRecord``): the tool called, and
    what came of it."""

    @property
    def tool_name(self) -> str: ...

    @property
    def status(self) -> str: ...


# What makes good a failed attempt that came before it in the same answer (spec 6.7).
MAKES_GOOD: Final = frozenset({"done", "waiting"})


def _makes_good(failed: tuple[str, str], later: tuple[str, str]) -> bool:
    """``made_good``, of two actions given as ``(tool name, status)``."""
    (tool, status), (later_tool, later_status) = failed, later
    return status == "invalid" and later_status in MAKES_GOOD and later_tool == tool


def made_good(failed: Attempt, later: Attempt) -> bool:
    """Whether ``later``, an action of the same answer that came after ``failed``, makes it good:
    ``failed`` was refused for its arguments (``invalid``), and ``later`` is a call of the same
    tool that was done or waits for a yes."""
    return _makes_good((failed.tool_name, failed.status), (later.tool_name, later.status))


def shown_actions(actions: Sequence[MessageAction]) -> list[MessageAction]:
    """The actions of one answer, as stored, that the people see, in their order: not those that
    are never shown (``policy.catalog.shown``: the silent ones, but while one waits for a yes),
    nor a failed attempt that a later call of the same tool, in the same answer, made good
    (``made_good``, spec 6.7: the person sees what was done). A failed attempt alone is still
    shown. Every action stays stored all the same, and in the history the model reads (it learns
    from its own mistakes)."""
    taken = [(action.tool_name, action.status) for action in actions]
    return [
        action
        for at, action in enumerate(actions)
        if shown(action.action_type, action.status)
        and not any(_makes_good(taken[at], later) for later in taken[at + 1 :])
    ]


# The parameters of a card that are someone else's words: the judge's reason, a note's words
# (``note_show``). On the card a head is written from, each one is a hole the code fills.
_SOMEONE_ELSES_WORDS: Final = (cards.REASON, cards.WORDS)


def _texts_of(values: object) -> dict[str, str] | None:
    """``values`` if it is a mapping of names to texts, else ``None``."""
    if not isinstance(values, dict):
        return None
    found = cast(dict[object, object], values)
    if not all(isinstance(name, str) and isinstance(v, str) for name, v in found.items()):
        return None
    return cast(dict[str, str], found)


def date_in_words(stored: str, texts: Texts) -> str:
    """A date a card keeps (ISO 8601) as its reader reads it: a moment with its time
    (``2026-11-06T14:03:00+00:00``), a day alone (``2026-11-20``). ``ValueError`` when it does not
    read as either."""
    if "T" not in stored:
        return texts.date(date.fromisoformat(stored), time=False)
    moment = datetime.fromisoformat(stored)
    if moment.tzinfo is None:
        raise ValueError("a moment a card keeps has its offset")
    return texts.date(moment)


def _names_of(values: object) -> list[str] | None:
    """``values`` if it is a list of names, else ``None``."""
    if not isinstance(values, list):
        return None
    found = cast(list[object], values)
    return cast(list[str], found) if all(isinstance(name, str) for name in found) else None


def lists_of(card: Mapping[str, object]) -> dict[str, list[str]]:
    """The lists of names a card keeps (``"successors"``: ``{"before": [...], "after": [...]}``,
    the people of a list of successors in order), by the hole each one fills; none when they do
    not read."""
    lists = card.get("successors")
    if not isinstance(lists, dict):
        return {}
    found: dict[str, list[str]] = {}
    for name, value in cast(dict[object, object], lists).items():
        names = _names_of(value)
        if isinstance(name, str) and names is not None:
            found[name] = names
    return found


def _in_a_line(names: list[str], texts: Texts) -> str:
    """The names of a list, in order, joined by commas, or the word of ``texts`` for an empty
    one."""
    return ", ".join(names) or texts("successors.none")


def card_sentence(card: Mapping[str, object], texts: Texts, *, holed: bool = False) -> str | None:
    """The sentence of a card (``{"key": <catalog key>, "params": {<name>: <text>}, "dates":
    {<name>: <ISO 8601>}}``), or ``None`` when it cannot be read: a stored card outlives the
    catalog that wrote it. A date is kept as it is (``"dates"``) and put in words when the card is
    shown, by ``texts``; a card stored before had its dates in words among its ``"params"``, and
    still reads. The lists of names it keeps (``lists_of``) are said in ``texts`` too. With
    ``holed``, the judge's reason (``"reason"``), a note's words (``"words"``: ``note_show``) and
    each date are left as holes (``{reason}``, ``{words}``, ``{<name>}``: ``texts.cards``)."""
    key = card.get("key")
    params = _texts_of(card.get("params", {}))
    dates = _texts_of(card.get("dates", {}))
    if not isinstance(key, str) or not has_meaning(key) or params is None or dates is None:
        return None
    try:
        if holed:
            in_words = {name: cards.hole(name) for name in dates}
            params = {
                **params,
                **{name: cards.hole(name) for name in _SOMEONE_ELSES_WORDS if name in params},
            }
        else:
            in_words = {name: date_in_words(stored, texts) for name, stored in dates.items()}
        lists = {name: _in_a_line(names, texts) for name, names in lists_of(card).items()}
        return texts(key, **{**params, **lists, **in_words})
    except (KeyError, IndexError, ValueError):
        return None


def action_source(card: Mapping[str, object], texts: Texts) -> HeadSource:
    """What the card of an action holds for the writing of its head (``texts.cards``): the names
    it gives (``"name"``, and those of its lists), its values (its numbers among them, never
    someone else's words), and the words of its holes, the judge's reason and a note's words as
    they were given and each date in the words of ``texts`` (the reader's)."""
    params = _texts_of(card.get("params", {})) or {}
    dates = _texts_of(card.get("dates", {})) or {}
    names = [params[name] for name in ("name",) if name in params]
    names.extend(name for listed in lists_of(card).values() for name in listed)
    holes = {name: params[name] for name in _SOMEONE_ELSES_WORDS if name in params}
    for name, stored in dates.items():
        try:
            holes[name] = date_in_words(stored, texts)
        except ValueError:
            continue
    numbers = tuple(value for name, value in params.items() if name not in _SOMEONE_ELSES_WORDS)
    return HeadSource(names=tuple(names), numbers=numbers, holes=holes)
