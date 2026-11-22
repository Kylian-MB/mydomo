"""The levers of Domo's memory among his choices (``llm.choices``, spec 6.4 and 6.6, tasks 5
and 6).

How many characters of his core and of his recall a turn gives him: a whole number from 0
(nothing) to ``MAX_MEMORY_CHARS``, the start when nothing is chosen, a key's choice on top of the
household's. How many minutes without a message before he goes back over a conversation: 0
(never) to ``MAX_REVIEW_AFTER_MINUTES``, 30 when nothing is chosen, the household's alone. Where
the trust of someone Domo does not know starts (trust per person, task 1): 0 to 100, 70 when
nothing is chosen, the household's alone.
"""

import pytest
from domo_core.llm.choices import (
    MAX_MEMORY_CHARS,
    MAX_REVIEW_AFTER_MINUTES,
    NO_CHOICES,
    START_MEMORY_CORE_CHARS,
    START_MEMORY_RECALL_CHARS,
    START_REVIEW_AFTER_MINUTES,
    START_TRUST_PERCENT,
    Choices,
    choices_as_json,
    choices_from_json,
    merged,
)


def test_the_sizes_start_at_2000_and_3000():
    assert (START_MEMORY_CORE_CHARS, START_MEMORY_RECALL_CHARS) == (2000, 3000)
    assert (NO_CHOICES.memory_core_chars, NO_CHOICES.memory_recall_chars) == (None, None)
    assert (NO_CHOICES.memory_core_size, NO_CHOICES.memory_recall_size) == (2000, 3000)


def test_zero_is_a_size_kept_as_it_is():
    chosen = choices_from_json({"memory_core_chars": 0, "memory_recall_chars": 0})
    assert (chosen.memory_core_size, chosen.memory_recall_size) == (0, 0)
    assert choices_as_json(chosen) == {
        "tasks": {},
        "memory_core_chars": 0,
        "memory_recall_chars": 0,
    }
    assert choices_from_json(choices_as_json(chosen)) == chosen


@pytest.mark.parametrize("value", [-1, MAX_MEMORY_CHARS + 1, True, "2000", 1.5, None])
def test_a_size_out_of_its_bounds_is_dropped(value):
    chosen = choices_from_json({"memory_core_chars": value, "memory_recall_chars": value})
    assert (chosen.memory_core_chars, chosen.memory_recall_chars) == (None, None)
    assert choices_as_json(chosen) == {"tasks": {}}


def test_a_keys_size_sits_on_the_household_s():
    household = Choices(memory_core_chars=500, memory_recall_chars=800)
    key = Choices(memory_core_chars=0)
    both = merged(household, key)
    assert (both.memory_core_size, both.memory_recall_size) == (0, 800)
    assert merged(household, Choices()).memory_core_size == 500


def test_a_review_waits_thirty_minutes_until_domo_chooses():
    assert START_REVIEW_AFTER_MINUTES == 30
    assert NO_CHOICES.review_after_minutes is None
    assert NO_CHOICES.review_minutes == 30


def test_zero_minutes_is_a_choice_kept_as_it_is():
    chosen = choices_from_json({"review_after_minutes": 0})
    assert (chosen.review_after_minutes, chosen.review_minutes) == (0, 0)
    assert choices_as_json(chosen) == {"tasks": {}, "review_after_minutes": 0}
    assert choices_from_json(choices_as_json(chosen)) == chosen


@pytest.mark.parametrize("value", [-1, MAX_REVIEW_AFTER_MINUTES + 1, True, "30", 1.5, None])
def test_minutes_out_of_their_bounds_are_dropped(value):
    chosen = choices_from_json({"review_after_minutes": value})
    assert chosen.review_after_minutes is None
    assert choices_as_json(chosen) == {"tasks": {}}


def test_when_to_review_is_the_households_alone():
    household = Choices(review_after_minutes=90)
    # A key's row that says otherwise has no say in it.
    assert merged(household, Choices(review_after_minutes=0)).review_minutes == 90
    assert merged(Choices(), Choices(review_after_minutes=0)).review_minutes == 30


def test_trust_starts_at_70_until_domo_chooses():
    assert START_TRUST_PERCENT == 70
    assert NO_CHOICES.trust_start_percent is None
    assert NO_CHOICES.trust_start == 70


@pytest.mark.parametrize("value", [0, 55, 100])
def test_a_trust_start_is_read_and_written_as_it_is(value):
    chosen = choices_from_json({"trust_start_percent": value})
    assert (chosen.trust_start_percent, chosen.trust_start) == (value, value)
    assert choices_as_json(chosen) == {"tasks": {}, "trust_start_percent": value}
    assert choices_from_json(choices_as_json(chosen)) == chosen


@pytest.mark.parametrize("value", [-1, 101, True, "70", 70.0, None])
def test_a_trust_start_out_of_its_bounds_is_dropped(value):
    chosen = choices_from_json({"trust_start_percent": value})
    assert (chosen.trust_start_percent, chosen.trust_start) == (None, 70)
    assert choices_as_json(chosen) == {"tasks": {}}


def test_the_trust_start_is_the_households_alone():
    household = Choices(trust_start_percent=60)
    # How far Domo believes someone he does not know is not a key's to say.
    assert merged(household, Choices(trust_start_percent=90)).trust_start == 60
    assert merged(Choices(), Choices(trust_start_percent=90)).trust_start == 70
    assert merged(household, None).trust_start == 60
