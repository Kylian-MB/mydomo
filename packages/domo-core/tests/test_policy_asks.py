"""The vocabulary of the yes: what a request asks for, who asked, and the owner's "never ask me"."""

import pytest
from domo_core.policy.asks import (
    ASKED_BY,
    ASKS,
    NeverAsk,
    as_ask,
    as_asked_by,
    as_rule_ask,
    most_demanding,
)


def test_the_asks_are_ranked_from_the_least_demanding():
    assert most_demanding() is None and most_demanding(None, None) is None
    assert most_demanding("yes", None) == "yes"
    assert most_demanding("yes", "proof", "explicit_yes") == "proof"
    assert ASKS == ("yes", "explicit_yes", "proof")
    # No "always": nothing is asked by default, and no stored request holds it (0040).
    assert ASKED_BY == ("rule", "judge", "domo", "approver")


def test_a_stored_ask_is_never_guessed():
    assert as_ask(None) is None and as_ask("explicit_yes") == "explicit_yes"
    with pytest.raises(ValueError):
        as_ask("confirm")
    # A request never asks "judge" or "never": only a right says it.
    with pytest.raises(ValueError):
        as_ask("judge")
    assert as_rule_ask(None) is None
    assert as_rule_ask("judge") == "judge" and as_rule_ask("never") == "never"
    assert as_rule_ask("proof") == "proof"
    with pytest.raises(ValueError):
        as_rule_ask("maybe")
    assert as_asked_by(None) is None and as_asked_by("rule") == "rule"
    with pytest.raises(ValueError):
        as_asked_by("owner")


def test_the_owners_never_ask_says_whether_it_is_not_at_all():
    # Nothing is asked by default any more (2026-11-15), so a "never ask me" lifts nothing: it
    # is what the judge is told, and "not at all" what keeps a yes from being asked.
    assert NeverAsk(None) == NeverAsk(None, at_all=False)
    assert NeverAsk("only for guests").when == "only for guests"
    assert NeverAsk(None, at_all=True).at_all
