"""The vocabulary of the yes: what a request asks for, who asked it, and the owner's "never ask me".

A request asks for one of three things, from the least demanding (``ASKS``): a ``yes`` (any clear
yes, in words or the button), an ``explicit_yes`` (a firm yes in the person's own words, read by
its meaning, or the button) and a ``proof`` (the person proves it is them). The most demanding
wins (``most_demanding``). Who asked it is noted beside it (``AskedBy``): a right of the owner
(``rule``), the yes-judge (``judge``), Domo himself (``domo``) or the approver's own wish
(``approver``). No type asks a yes by default (2026-11-15).

A right says more than a request does (``RuleAsk``): ``judge`` is "never ask me: leave it to the
judge", ``never`` is "not at all". Those two are never the ask of a request, so ``as_ask`` refuses
them. What is stored is never guessed: anything unknown is a ``ValueError``.
"""

from dataclasses import dataclass
from typing import Final, Literal

Ask = Literal["yes", "explicit_yes", "proof"]
# From the least demanding to the most.
ASKS: Final[tuple[Ask, ...]] = ("yes", "explicit_yes", "proof")

AskedBy = Literal["rule", "judge", "domo", "approver"]
ASKED_BY: Final[tuple[AskedBy, ...]] = ("rule", "judge", "domo", "approver")

# What a right says to ask: "judge" is "never ask me: leave it to the judge" (with the owner's
# conditions beside it), "never" is "not at all".
RuleAsk = Literal["judge", "never", "yes", "explicit_yes", "proof"]
_RULE_ASKS: Final[tuple[RuleAsk, ...]] = ("judge", "never", "yes", "explicit_yes", "proof")


@dataclass(frozen=True)
class Asked:
    """What a request asks for, who asked it, and why (one line, 200 characters at most).
    ``warned``: the yes-judge asked it only because of something the person must know first
    (its ``warn``), which ``why`` tells. ``before_doubt``: the judge asked it only because the
    code was not sure of who asks, and this is what the rules ask without that doubt; a later yes
    the code is sure of is held to it (``Confirmation.ask_before_doubt``, final fix wave, case 10).
    """

    ask: Ask
    by: AskedBy
    why: str | None = None
    warned: bool = False
    before_doubt: Ask | None = None


def most_demanding(*asks: Ask | None) -> Ask | None:
    """The most demanding of the asks given; ``None`` when every one is ``None``."""
    best: Ask | None = None
    for ask in asks:
        if ask is not None and (best is None or ASKS.index(ask) > ASKS.index(best)):
            best = ask
    return best


def most_demanding_asked(*candidates: Asked | None) -> Asked | None:
    """The most demanding of what ``candidates`` ask, with who asked it and why; on a tie, the
    first one given. ``None`` when none asks anything."""
    best: Asked | None = None
    for asked in candidates:
        if asked is not None and (best is None or ASKS.index(asked.ask) > ASKS.index(best.ask)):
            best = asked
    return best


def as_ask(value: object) -> Ask | None:
    """A stored ask; ``None`` stays ``None``, anything unknown is a ``ValueError``."""
    if value is None:
        return None
    for ask in ASKS:
        if value == ask:
            return ask
    raise ValueError(f"not an ask: {value!r}")


def as_asked_by(value: object) -> AskedBy | None:
    """The same for who asked."""
    if value is None:
        return None
    for by in ASKED_BY:
        if value == by:
            return by
    raise ValueError(f"not an asker: {value!r}")


def as_rule_ask(value: object) -> RuleAsk | None:
    """The same for what a right says to ask."""
    if value is None:
        return None
    for rule_ask in _RULE_ASKS:
        if value == rule_ask:
            return rule_ask
    raise ValueError(f"not a rule ask: {value!r}")


@dataclass(frozen=True)
class NeverAsk:
    """The owner's "never ask me for this" (a right with ``ask`` "judge" or "never"): guidance for
    the judge, with the conditions the owner said in words (``when``, None when he gave none);
    ``at_all`` for "never": no yes is asked at all, by the judge or by Domo, only a proof when
    the judge doubts it is the person (the owner's own "not at all" on one of his own actions:
    ``policy.rights``)."""

    when: str | None
    at_all: bool = False
