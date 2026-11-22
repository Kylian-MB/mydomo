"""The trust percentage the code draws from what a person's word has been worth (spec 6.2, task 1).

``trust.score`` is pure: each property the spec asks of it is one test here.
"""

import itertools

import pytest
from domo_core.trust.models import Established, Outcome
from domo_core.trust.score import score

START = 70

ESTABLISHED: tuple[Established, ...] = ("confirmed", "seen")
OUTCOMES: tuple[Outcome, ...] = ("held", "mistaken", "lied")


def test_without_a_fact_the_score_is_the_start():
    for start in (0, 50, 70, 100):
        assert score([], start=start) == start


@pytest.mark.parametrize("start", [0, 1, 50, 70, 99, 100])
@pytest.mark.parametrize("outcome", OUTCOMES)
@pytest.mark.parametrize("established", ["confirmed", "seen", "reported"])
def test_the_score_stays_between_0_and_100(start, outcome, established):
    for count in (1, 3, 10, 500):
        assert 0 <= score([(outcome, established)] * count, start=start) <= 100
    mixed = [(o, e) for o in OUTCOMES for e in ("confirmed", "seen", "reported")] * 50
    assert 0 <= score(mixed, start=start) <= 100


def test_a_start_out_of_its_bounds_is_refused():
    for start in (-1, 101):
        with pytest.raises(ValueError):
            score([], start=start)


@pytest.mark.parametrize("established", ESTABLISHED)
def test_an_established_lie_lowers_at_least_three_times_a_mistake(established):
    after_a_lie = score([("lied", established)], start=START)
    after_a_mistake = score([("mistaken", established)], start=START)
    assert after_a_mistake < START
    assert START - after_a_lie >= 3 * (START - after_a_mistake)


@pytest.mark.parametrize("established", ESTABLISHED)
def test_it_takes_three_established_lies_to_fall_under_50(established):
    assert score([("lied", established)] * 2, start=START) >= 50
    assert score([("lied", established)] * 3, start=START) < 50
    # The two ways of establishing a fact weigh the same.
    assert score([("lied", "confirmed"), ("lied", "seen")], start=START) >= 50


@pytest.mark.parametrize("established", ESTABLISHED)
def test_ten_established_held_give_at_most_95(established):
    after = score([("held", established)] * 10, start=START)
    assert START < after <= 95


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_ten_reported_facts_weigh_no_more_than_three_established(outcome):
    reported = score([(outcome, "reported")] * 10, start=START)
    for established in ESTABLISHED:
        three = score([(outcome, established)] * 3, start=START)
        assert abs(reported - START) <= abs(three - START)
    # Someone who only says ill of another cannot bring them under 50 alone.
    assert score([("lied", "reported")] * 10, start=START) >= 50


def test_a_mean_that_falls_on_half_is_rounded_up_whatever_the_float_error():
    # Ten reported lies and two established lies both give 420 / 8 = 52.5 (the docstring): the
    # sum of ten weights of 0.2 is not exactly 2 in floats, and must not round down.
    assert score([("lied", "reported")] * 10, start=START) == 53
    for established in ESTABLISHED:
        assert score([("lied", established)] * 2, start=START) == 53


def test_ten_held_stay_at_most_95_up_to_a_start_of_91_only():
    # The docstring's bound, and where it stops holding.
    assert score([("held", "seen")] * 10, start=91) == 95
    assert score([("held", "seen")] * 10, start=92) == 96


def test_held_raises_mistaken_and_lied_lower():
    assert score([("held", "confirmed")], start=START) > START
    assert score([("mistaken", "confirmed")], start=START) < START
    assert score([("lied", "confirmed")], start=START) < START
    assert score([("held", "reported")], start=START) >= START


def test_the_order_of_the_facts_does_not_change_the_score():
    facts: list[tuple[Outcome, Established]] = [
        ("held", "confirmed"),
        ("lied", "seen"),
        ("mistaken", "reported"),
        ("held", "seen"),
        ("lied", "reported"),
    ]
    scores = {score(list(order), start=START) for order in itertools.permutations(facts)}
    assert len(scores) == 1


def test_two_starts_differ_without_a_fact_and_come_closer_as_facts_add_up():
    assert score([], start=50) != score([], start=80)
    for outcome in OUTCOMES:
        gaps = [
            score([(outcome, "confirmed")] * count, start=80)
            - score([(outcome, "confirmed")] * count, start=50)
            for count in (0, 3, 10, 50)
        ]
        assert gaps[0] == 30
        assert gaps == sorted(gaps, reverse=True), outcome
        assert gaps[-1] < gaps[0], outcome
