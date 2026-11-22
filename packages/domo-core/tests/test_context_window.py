"""Choosing how much of the conversation is sent to the model."""

import pytest
from domo_core.chat.context_window import merge_consecutive_roles, select_history
from domo_core.llm.types import ChatMessage


def u(content: str) -> ChatMessage:
    return ChatMessage("user", content)


def a(content: str) -> ChatMessage:
    return ChatMessage("assistant", content)


def test_select_history_keeps_most_recent_within_budget():
    msgs = [u("a" * 10), a("b" * 10), u("c" * 10), a("d" * 10), u("e" * 10)]
    assert select_history(msgs, max_chars=35) == msgs[2:]


def test_select_history_drops_leading_assistant():
    msgs = [u("a" * 10), a("b" * 10), u("c" * 10)]
    assert select_history(msgs, max_chars=25) == [msgs[2]]


def test_select_history_truncates_single_oversized_latest_message():
    assert select_history([u("x" * 60 + "y" * 40)], max_chars=40) == [u("y" * 40)]


def test_select_history_keeps_everything_that_fits():
    msgs = [u("one"), a("two"), u("three")]
    assert select_history(msgs, max_chars=11) == msgs


def test_select_history_budget_is_inclusive():
    msgs = [u("a" * 10), a("b" * 10), u("c" * 10)]
    assert select_history(msgs, max_chars=30) == msgs
    assert select_history(msgs, max_chars=29) == [msgs[2]]


def test_select_history_truncated_latest_is_the_only_message_kept():
    msgs = [u("old question"), a("old answer"), u("z" * 100)]
    assert select_history(msgs, max_chars=10) == [u("z" * 10)]


def test_select_history_a_latest_message_of_exactly_the_budget_is_not_cut():
    msgs = [u("older"), u("abcdef")]
    assert select_history(msgs, max_chars=6) == [u("abcdef")]


def test_select_history_stops_at_the_first_message_that_does_not_fit():
    # A smaller message further back is not squeezed in around the one that did not fit.
    msgs = [u("a"), a("b" * 50), u("c" * 5)]
    assert select_history(msgs, max_chars=10) == [u("c" * 5)]


def test_select_history_drops_every_leading_assistant_message():
    msgs = [a("1"), a("2"), u("3"), a("4")]
    assert select_history(msgs, max_chars=100) == [u("3"), a("4")]


def test_select_history_with_only_assistant_messages_is_empty():
    assert select_history([a("1"), a("2")], max_chars=100) == []


def test_select_history_of_nothing_is_empty():
    assert select_history([], max_chars=100) == []


def test_select_history_is_chronological_and_leaves_its_input_alone():
    msgs = [u("1"), a("2"), u("3")]
    before = list(msgs)
    assert select_history(msgs, max_chars=100) == before
    assert msgs == before


@pytest.mark.parametrize("budget", [0, -1])
def test_select_history_needs_a_positive_budget(budget):
    with pytest.raises(ValueError):
        select_history([u("hello")], max_chars=budget)


# --- one side at a time ------------------------------------------------------------------------


def test_merge_consecutive_roles_joins_a_run_with_a_blank_line():
    msgs = [u("a"), u("b"), a("c"), u("d"), u("e"), u("f")]
    assert merge_consecutive_roles(msgs) == [u("a\n\nb"), a("c"), u("d\n\ne\n\nf")]


def test_merge_consecutive_roles_merges_assistant_runs_too():
    assert merge_consecutive_roles([u("a"), a("b"), a("c")]) == [u("a"), a("b\n\nc")]


def test_merge_consecutive_roles_leaves_an_alternating_conversation_alone():
    msgs = [u("a"), a("b"), u("c"), a("d")]
    assert merge_consecutive_roles(msgs) == msgs


def test_merge_consecutive_roles_of_nothing_is_nothing():
    assert merge_consecutive_roles([]) == []


def test_merge_consecutive_roles_does_not_change_its_input():
    msgs = [u("a"), u("b")]
    merge_consecutive_roles(msgs)
    assert msgs == [u("a"), u("b")]


def test_the_window_of_a_merged_conversation_keeps_alternating():
    merged = merge_consecutive_roles([u("q1"), u("q2"), a("r"), u("q3"), u("q4")])
    window = select_history(merged, max_chars=1000)
    assert [m.role for m in window] == ["user", "assistant", "user"]
