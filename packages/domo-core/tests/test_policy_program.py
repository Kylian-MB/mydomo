"""When a program's request needs a yes (Task 8, spec 5.3, 5.8): pure, from the barrier's
decision, the request's risks, whether it is for Domo (the house), whether a standing agreement
covers it and whether a yes was given for it (``policy.program.judge_request``)."""

import pytest
from domo_core.policy.barrier import Allow, NeedsApproval, Refuse
from domo_core.policy.catalog import NO_RISK, Risks
from domo_core.policy.program import Go, NeedsYes, No, answers, judge_request, judge_under_yes


def test_costs_without_an_agreement_needs_a_yes():
    assert judge_request(
        Allow(),
        risks=Risks(costs=True),
        for_domo=False,
        covered=False,
    ) == NeedsYes("costs", "self", "yes")


def test_costs_covered_by_an_agreement_goes():
    assert (
        judge_request(
            Allow(),
            risks=Risks(costs=True),
            for_domo=False,
            covered=True,
        )
        == Go()
    )


@pytest.mark.parametrize(
    "risks",
    [
        Risks(physical=True),
        Risks(outward=True),
        Risks(irreversible=True),
        Risks(irreversible=True, physical=True, outward=True),
        NO_RISK,
    ],
)
@pytest.mark.parametrize("for_domo", [False, True])
def test_physical_outward_irreversible_ask_nothing_alone(risks: Risks, for_domo: bool):
    assert judge_request(Allow(), risks=risks, for_domo=for_domo, covered=False) == Go()


def test_an_owners_rule_asks_its_own_kind():
    assert judge_request(
        Allow(rule="explicit_yes"),
        risks=NO_RISK,
        for_domo=False,
        covered=True,
    ) == NeedsYes("owner_rule", "self", "explicit_yes")


def test_an_adults_agreement_for_a_child():
    assert judge_request(
        NeedsApproval("adult"),
        risks=NO_RISK,
        for_domo=False,
        covered=False,
    ) == NeedsYes("approval", "adult", "yes")


def test_for_the_house_a_cost_without_an_agreement_stops_for_domo_to_judge():
    assert judge_request(
        Allow(),
        risks=Risks(costs=True),
        for_domo=True,
        covered=False,
    ) == NeedsYes("costs", "self", "yes")
    covered = judge_request(
        Allow(),
        risks=Risks(costs=True),
        for_domo=True,
        covered=True,
    )
    assert covered == Go()


def test_an_owners_rule_on_domo_waits_for_the_owners_yes():
    # Carried question (1): the owner's "always ask" on a right of Domo's is the owner's to
    # answer; Domo never says yes to the owner's own rule.
    assert judge_request(
        Allow(rule="yes"),
        risks=NO_RISK,
        for_domo=True,
        covered=False,
    ) == NeedsYes("owner_rule", "owner", "yes")


@pytest.mark.parametrize("approver", ["adult", "owner"])
def test_an_adult_or_the_owner_who_agrees_is_always_a_person(approver):
    # Carried question (2): Domo is never the adult nor the owner who agrees.
    assert judge_request(
        NeedsApproval(approver),
        risks=NO_RISK,
        for_domo=True,
        covered=True,
    ) == NeedsYes("approval", approver, "yes")


def test_a_refusal_wins_over_everything_and_a_full_yes_over_the_rest():
    full = NeedsYes("approval", "owner", "proof")
    for yes in ((), (full,)):
        assert judge_under_yes(
            Refuse("not_allowed"), risks=Risks(costs=True), for_domo=False, covered=True, yes=yes
        ) == No("not_allowed")
    assert (
        judge_under_yes(
            NeedsApproval("owner", rule="proof"),
            risks=Risks(costs=True),
            for_domo=False,
            covered=False,
            yes=(full,),
        )
        == Go()
    )


def test_the_order_of_judgement():
    # Approval before the owner's rule, the owner's rule before the cost: the first gives the
    # stop its why, and the stop asks the strongest approver and ask of all (fix round 2, N1).
    assert judge_request(
        NeedsApproval("adult", rule="explicit_yes"),
        risks=Risks(costs=True),
        for_domo=False,
        covered=False,
    ) == NeedsYes("approval", "adult", "explicit_yes")
    assert judge_request(
        Allow(rule="proof"),
        risks=Risks(costs=True),
        for_domo=False,
        covered=False,
    ) == NeedsYes("owner_rule", "self", "proof")


# --- a cost asked again (fix round 4, M2) --------------------------------------------------------


def test_a_cost_asked_again_is_answered_only_by_the_yeses_given_for_it():
    approval = NeedsYes("approval", "adult", "yes")
    cost = NeedsYes("costs", "self", "yes")

    def judged(*for_cost: NeedsYes) -> object:
        return judge_under_yes(
            NeedsApproval("adult"),
            risks=Risks(costs=True),
            for_domo=False,
            covered=False,
            yes=(approval, *for_cost),
            for_cost=for_cost,
        )

    # The adult's approval of another request still answers the approval, never the cost.
    assert judged() == cost
    assert judged(cost) == Go()


# --- under the yes of a stop (fix round 1, C1) ---------------------------------------------------


def test_a_yes_lifts_only_what_asks_no_more_than_its_stop():
    cost = NeedsYes("costs", "self", "yes")
    # A requester's own yes on a cost never stands for an adult's approval nor the owner's proof.
    assert judge_under_yes(
        NeedsApproval("adult"),
        risks=Risks(costs=True),
        for_domo=False,
        covered=False,
        yes=(cost,),
    ) == NeedsYes("approval", "adult", "yes")
    assert judge_under_yes(
        Allow(rule="proof"), risks=NO_RISK, for_domo=False, covered=False, yes=(cost,)
    ) == NeedsYes("owner_rule", "self", "proof")
    assert (
        judge_under_yes(
            Allow(), risks=Risks(costs=True), for_domo=False, covered=False, yes=(cost,)
        )
        == Go()
    )
    # A proof lifts a plain yes; an adult's approval lifts the requester's own.
    proof = NeedsYes("owner_rule", "self", "proof")
    assert (
        judge_under_yes(
            Allow(rule="yes"), risks=NO_RISK, for_domo=False, covered=True, yes=(proof,)
        )
        == Go()
    )
    adult = NeedsYes("approval", "adult", "yes")
    assert answers(adult, cost) and not answers(cost, adult)
    assert not answers(adult, NeedsYes("approval", "owner", "yes"))
    # A refusal is never lifted, whatever the yes.
    owner_proof = NeedsYes("approval", "owner", "proof")
    assert judge_under_yes(
        Refuse("limit"), risks=NO_RISK, for_domo=False, covered=True, yes=(owner_proof,)
    ) == No("limit")
