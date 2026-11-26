"""The trust percentage the code draws from a person's facts (spec 6.2).

The score is a weighted mean: the start (a lever of Domo's, ``Choices.trust_start``) counts as
``START_WEIGHT`` facts, each fact pulls towards 100 (``held``) or towards 0 (``mistaken``,
``lied``) with its own weight, and a ``reported`` fact weighs ``REPORTED_SHARE`` of an established
one::

    score = (start * START_WEIGHT + sum(weight * value)) / (START_WEIGHT + sum(weight))

Why each property of the spec holds (the numbers are from a start of 70):

* without a fact both sums are 0: the score is the start;
* it is a mean of the start and of values of 0 or 100, so it stays between 0 and 100;
* the first fact moves the score by ``start * weight / (START_WEIGHT + weight)``: a lie
  (weight 1) by 70 / 7 = 10, a mistake (weight 0.2) by 14 / 6.2, about 2.3: more than three times
  less;
* two established lies give 420 / 8 = 52.5, three give 420 / 9, about 46.7: it takes three to
  fall under 50 (true for any ``START_WEIGHT`` from 5 up to, not including, 7.5 lies);
* ten established ``held`` give (420 + 10 * 0.5 * 100) / 11, about 83.6: at most 95 (a word that
  held is the ordinary case, a lie the exception that tells something: a truth weighs half a
  lie). That bound holds from any start up to 91; from a start of 92, ten ``held`` give 96;
* ten reported facts weigh 10 * 0.2 = 2 established facts of the same kind, fewer than three;
  ten reported lies alone leave 420 / 8 = 52.5, above 50, and rounded 53 like two established
  lies (the mean is rounded to ``DECIMALS`` places before it is rounded half up, so that the
  error of numbers with a decimal point never moves a score that falls on .5);
* the order of the facts is lost in the sums;
* two starts are ``START_WEIGHT / (START_WEIGHT + sum(weight))`` of their gap apart: the whole gap
  without a fact, less with each fact.

The score is computed, never kept; it is data for Domo's judgement and the yes-judge's, never a
right, a rule or a limit.
"""

import math
from collections.abc import Sequence
from typing import Final

from domo_core.trust.models import Established, Outcome

# How many established lies the start weighs: the first facts move the score little.
START_WEIGHT: Final = 6.0
# How much each outcome weighs, in established lies.
HELD_WEIGHT: Final = 0.5
MISTAKEN_WEIGHT: Final = 0.2
LIED_WEIGHT: Final = 1.0
# What a reported fact weighs, as a share of the same fact established: what someone reports is
# a word to be judged too.
REPORTED_SHARE: Final = 0.2
# The value a fact pulls towards: a word that held, or one that did not.
TRUE_VALUE: Final = 100.0
FALSE_VALUE: Final = 0.0
LOWEST: Final = 0
HIGHEST: Final = 100
# The places the mean is rounded to first: far below a point, far above the float's error.
DECIMALS: Final = 6

_OUTCOME_WEIGHTS: Final[dict[Outcome, float]] = {
    "held": HELD_WEIGHT,
    "mistaken": MISTAKEN_WEIGHT,
    "lied": LIED_WEIGHT,
}


def _weight(outcome: Outcome, established: Established) -> float:
    share = REPORTED_SHARE if established == "reported" else 1.0
    return _OUTCOME_WEIGHTS[outcome] * share


def score(facts: Sequence[tuple[Outcome, Established]], *, start: int) -> int:
    """The trust percentage of someone whose facts are ``facts`` (their outcome and how each is
    established, in any order), from ``start`` (0 to 100, else ``ValueError``): a whole number
    from 0 to 100, rounded half up."""
    if not LOWEST <= start <= HIGHEST:
        raise ValueError(f"A start of trust is {LOWEST} to {HIGHEST}.")
    weighted = start * START_WEIGHT
    total = START_WEIGHT
    for outcome, established in facts:
        weight = _weight(outcome, established)
        weighted += weight * (TRUE_VALUE if outcome == "held" else FALSE_VALUE)
        total += weight
    mean = round(weighted / total, DECIMALS)
    return min(HIGHEST, max(LOWEST, math.floor(mean + 0.5)))
