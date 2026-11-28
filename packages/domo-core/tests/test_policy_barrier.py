"""The policy barrier: a pure function that says what may happen to a described action (allowed,
refused, or whose accord it needs first), and the audit entry that records what it said (never the
arguments of the call). Whether a yes is asked is not the barrier's: the yes-judge decides it
(``test_judge_decides.py``).

The barrier reads the facts of the moment when a limit applies, and the catalog has a limit of its
own (a guest stays 30 days at most): the decisions here are made with facts made up from the
action (``decide`` below), for actions that limit lets through. The limits themselves are tried in
``test_policy_limits.py``."""

from dataclasses import replace
from uuid import uuid4

import pytest
from chat_support import last_audit
from domo_core.audit.models import AuditEntry
from domo_core.policy import barrier
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import NeverAsk
from domo_core.policy.barrier import (
    Allow,
    Decision,
    NeedsApproval,
    Refuse,
    approval_needed,
    record_decision,
)
from domo_core.policy.catalog import (
    ACTION_FACTS,
    ADULT_AGREES_FOR_A_CHILD,
    ALLOWED_ACTIONS,
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PolicyTables,
)
from domo_core.policy.facts import facts_of
from domo_core.policy.rights import NO_RIGHTS, Limit, Resolved, Right, RightsTable
from domo_core.policy.roles import ROLES, Actor
from sqlalchemy import func, select

# The value of each kind of fact in the actions of these tests: an adult, and no guest's stay.
_PLAIN = {
    "text": "x",
    "number": None,
    "boolean": False,
    "person": "person:x",
    "role": "adult",
    "date": "2030-01-01",
}


def described(action_type: str, target: str | None = None, effect: str = "read") -> DescribedAction:
    """An action of ``action_type`` with plain facts (``_PLAIN``)."""
    facts = {name: _PLAIN[kind] for name, kind in ACTION_FACTS.get(action_type, {}).items()}
    return DescribedAction("core", action_type, target, effect, "household", facts)  # type: ignore[arg-type]


def decide(
    actor: Actor,
    action: DescribedAction,
    policy: PolicyTables = DEFAULT_POLICY,
    rights: RightsTable = NO_RIGHTS,
) -> Decision:
    """``barrier.decide`` with the facts of a moment made up from the action (no time zone, nobody
    in the household, no history), as ``decide_now`` would give them."""
    return barrier.decide(
        actor, action, policy, rights, facts_of(actor, action, name="Someone", local_now=None)
    )


INVITE = described("household.invite", effect="permissions")
LEAVE = described("household.leave", "person:x", "permissions")
LIST = DescribedAction("core", "household.list", None, "read", "household")
NOTE = described("note.leave", "person:x", "message")
RENAME = DescribedAction("core", "profile.rename", None, "self_profile", "household")
TRANSFER = described("household.transfer_ownership", effect="permissions")
APPLY = described("rights.apply", effect="permissions")


@pytest.fixture
async def owner(add_person):
    return await add_person("Owner", "owner")


def right(
    *,
    action_type: str,
    person_id=None,
    role=None,
    access=None,
    approver=None,
    limit: Limit | None = None,
    ask=None,
    ask_when=None,
) -> Right:
    return Right(
        uuid4(),
        person_id,
        role,
        action_type,
        access,
        approver,
        limit,
        ask,
        ask_when,
        "a right",
    )


# --- the three answers --------------------------------------------------------------------------


def test_an_action_a_person_may_do_is_allowed_and_the_judge_decides_the_rest():
    assert decide(Actor(uuid4(), "owner"), INVITE) == Allow()
    assert decide(Actor(uuid4(), "adult"), NOTE) == Allow()
    assert decide(Actor(uuid4(), "child", child=True), LIST) == Allow()


def test_a_childs_leave_needs_an_adult_and_a_childs_note_does_not():
    child = Actor(uuid4(), "child", child=True)
    assert decide(child, LEAVE) == NeedsApproval("adult", judge_first=True)
    assert decide(child, NOTE) == Allow()
    assert decide(child, RENAME) == Allow()


@pytest.mark.parametrize(
    "role,expected",
    [
        # (The owner is refused by the tool itself: they hand their role over first.)
        ("owner", Allow()),
        ("adult", Allow()),
        ("child", NeedsApproval("adult", judge_first=True)),
        ("guest", Allow()),
    ],
)
def test_leaving_the_household_is_everyones_with_an_adult_for_a_child(role, expected):  # §5.9
    assert decide(Actor(uuid4(), role, child=role == "child"), LEAVE) == expected
    # Removing someone stays the owner's: nobody else may, without a right.
    remove = described("household.remove", "person:x", "permissions")
    if role != "owner":
        assert decide(Actor(uuid4(), role, child=role == "child"), remove) == Refuse("not_allowed")


def test_a_right_naming_an_approver_needs_their_accord():
    alex = Actor(uuid4(), "adult")
    rights = RightsTable(
        (
            right(
                person_id=alex.person_id,
                action_type="household.invite",
                access="allow",
                approver="owner",
            ),
            right(role="owner", action_type="household.invite", approver="owner"),
        )
    )
    assert decide(alex, INVITE, DEFAULT_POLICY, rights) == NeedsApproval("owner")
    # The owner's own request never waits for the owner: asking is their accord.
    assert decide(Actor(uuid4(), "owner"), INVITE, DEFAULT_POLICY, rights) == Allow()
    # An approver on a type the catalog leaves free asks for that accord too.
    notes = RightsTable((right(role="adult", action_type="note.leave", approver="adult"),))
    assert decide(alex, NOTE, DEFAULT_POLICY, notes) == NeedsApproval("adult")


def test_a_limit_that_sends_to_the_owner_needs_the_owners_accord_unless_the_owner_asks():
    for says in ("ask_owner", "judge"):
        question = "Is this a friend?" if says == "judge" else None
        limit = Limit(f'"{says}"', question, "a limit")
        rights = RightsTable(
            (
                right(role="adult", action_type="note.leave", limit=limit),
                right(role="owner", action_type="note.leave", limit=limit),
            )
        )
        # "judge" is the judge's first; "ask_owner" is the owner's own word, always his.
        assert decide(Actor(uuid4(), "adult"), NOTE, DEFAULT_POLICY, rights) == NeedsApproval(
            "owner", question, judge_first=says == "judge"
        )
        assert decide(Actor(uuid4(), "owner"), NOTE, DEFAULT_POLICY, rights) == Allow()


def test_a_judge_limit_on_what_needs_an_adult_anyway_goes_to_the_owner():
    # A child's leave waits for an adult by the catalog: a limit "judge" on it is not left to the
    # judge, whose do would spare the adult too. It goes to the owner, as before.
    limit = Limit('"judge"', "Is it the holidays?", "a limit")
    rights = RightsTable((right(role="child", action_type="household.leave", limit=limit),))
    assert decide(
        Actor(uuid4(), "child", child=True), LEAVE, DEFAULT_POLICY, rights
    ) == NeedsApproval("owner", "Is it the holidays?")


def test_an_approver_a_right_names_is_never_left_to_the_judge():
    # The catalog's adult for a child is the judge's first; an adult a right names is the
    # owner's word.
    named = RightsTable((right(role="child", action_type="household.leave", approver="adult"),))
    assert decide(
        Actor(uuid4(), "child", child=True), LEAVE, DEFAULT_POLICY, named
    ) == NeedsApproval("adult")
    assert decide(Actor(uuid4(), "child", child=True), LEAVE) == NeedsApproval(
        "adult", judge_first=True
    )


def test_the_owners_rule_travels_with_the_decision():
    adult = Actor(uuid4(), "adult")
    proof = RightsTable((right(role="adult", action_type="household.change_role", ask="proof"),))
    opened = RightsTable(
        (
            right(role="adult", action_type="household.invite", access="allow", ask="proof"),
            right(role="child", action_type="household.leave", ask="proof"),
        )
    )
    assert decide(adult, INVITE, DEFAULT_POLICY, opened) == Allow(rule="proof")
    assert decide(
        Actor(uuid4(), "child", child=True), LEAVE, DEFAULT_POLICY, opened
    ) == NeedsApproval("adult", None, "proof", judge_first=True)
    # A rule alone does not open the type: the adult may still not change a role (they invite
    # by default since 0.9.1).
    change = described("household.change_role", "person:x", "permissions")
    assert decide(adult, change, DEFAULT_POLICY, proof) == Refuse("not_allowed")
    owner = Actor(uuid4(), "owner")
    lifted = RightsTable(
        (
            right(
                person_id=owner.person_id,
                action_type="rights.apply",
                ask="judge",
                ask_when="only for guests",
            ),
            right(
                person_id=owner.person_id,
                action_type="household.transfer_ownership",
                ask="never",
            ),
        )
    )
    assert decide(owner, APPLY, DEFAULT_POLICY, lifted) == Allow(None, NeverAsk("only for guests"))
    assert decide(owner, TRANSFER, DEFAULT_POLICY, lifted) == Allow(
        None, NeverAsk(None, at_all=True)
    )


def test_someone_unsure_does_the_ordinary_and_the_judge_weighs_the_sensitive():  # F27, POL-4
    # Never refused out of hand: renaming oneself and the list are ordinary, done as anyone;
    # inviting and a note are sensitive, the judge weighs who asks (spec 6.5 of the voice).
    for role in ROLES:
        child = role == "child"
        unsure = Actor(uuid4(), role, standing="unsure", child=child)
        sure = Actor(unsure.person_id, role, child=child)
        assert decide(unsure, RENAME) == decide(sure, RENAME) == Allow()
        assert decide(unsure, LIST) == decide(sure, LIST)
        for action in (INVITE, NOTE):
            theirs = decide(sure, action)
            judged = decide(unsure, action)
            if isinstance(theirs, Refuse):
                assert judged == theirs
            else:
                assert judged == replace(theirs, judge_identity=True)


def test_a_right_that_opens_a_type_to_a_child_keeps_the_adult_unless_it_says_self():  # F1
    child = Actor(uuid4(), "child", child=True)
    opened = RightsTable(
        (right(person_id=child.person_id, action_type="household.invite", access="allow"),)
    )
    # (The catalog's adult, named by no right: the judge's first, task 25.)
    assert decide(child, INVITE, DEFAULT_POLICY, opened) == NeedsApproval("adult", judge_first=True)
    themselves = RightsTable(
        (
            right(
                person_id=child.person_id,
                action_type="household.invite",
                access="allow",
                approver="self",
            ),
        )
    )
    assert decide(child, INVITE, DEFAULT_POLICY, themselves) == Allow()


def test_approval_needed_is_the_one_rule_the_barrier_and_the_chain_share():
    nothing = Resolved(True, None, None, None, None)
    assert approval_needed(
        Actor(uuid4(), "child", child=True), nothing, "household.leave"
    ) == NeedsApproval("adult")
    assert approval_needed(Actor(uuid4(), "adult"), nothing, "note.leave") is None
    assert approval_needed(Actor(uuid4(), "child", child=True), nothing, "note.leave") is None
    to_the_owner = Resolved(True, "owner", None, "explicit_yes", None)
    assert approval_needed(Actor(uuid4(), "adult"), to_the_owner, "note.leave") == NeedsApproval(
        "owner", None, "explicit_yes"
    )
    assert approval_needed(Actor(uuid4(), "owner"), to_the_owner, "note.leave") is None
    by_themselves = Resolved(True, "self", None, None, NeverAsk("x"))
    assert (
        approval_needed(Actor(uuid4(), "child", child=True), by_themselves, "household.leave")
        is None
    )


def test_the_types_a_child_needs_an_adult_for_are_the_ones_that_were_not_free():
    for action_type in sorted(ADULT_AGREES_FOR_A_CHILD):
        resolved = Resolved(True, None, None, None, None)
        assert approval_needed(
            Actor(uuid4(), "child", child=True), resolved, action_type
        ) == NeedsApproval("adult")


def test_the_target_and_the_connector_do_not_change_the_decision():
    # The destination changes too (``external``), and that does not change the decision either:
    # a limit of 1b, where every action stays in the household. The privacy and provenance checks
    # that will look at where something goes (spec §10.2) come with plan 1e.
    elsewhere = DescribedAction(
        "example", "note.leave", "person:y", "message", "external", NOTE.facts
    )
    assert decide(Actor(uuid4(), "adult"), elsewhere) == decide(Actor(uuid4(), "adult"), NOTE)


def test_guest_cannot_list_household():
    assert decide(Actor(uuid4(), "guest"), LIST) == Refuse("not_allowed")


def test_guest_can_rename_themselves():
    assert decide(Actor(uuid4(), "guest"), RENAME) == Allow()


def test_unknown_action_type_is_refused():
    odd = DescribedAction("core", "household.delete_everything", None, "permissions", "household")
    assert decide(Actor(uuid4(), "owner"), odd) == Refuse("not_allowed")


# --- every role against every action of the catalog ---------------------------------------


@pytest.mark.parametrize("action_type", sorted(KNOWN_ACTIONS))
@pytest.mark.parametrize("role", ROLES)
def test_decision_for_every_role_and_action(role, action_type):
    action = described(action_type)
    if action_type not in ALLOWED_ACTIONS[role]:
        expected: Decision = Refuse("not_allowed")
    elif role == "child" and action_type in ADULT_AGREES_FOR_A_CHILD:
        expected = NeedsApproval("adult", judge_first=True)
    else:
        expected = Allow()
    assert decide(Actor(uuid4(), role, child=role == "child"), action) == expected


# --- who asks, when the code is not sure of them ------------------------------------------


def test_nobody_known_is_refused_before_the_action_is_looked_up():
    odd = DescribedAction("core", "household.delete_everything", None, "permissions", "household")
    assert decide(Actor(uuid4(), "owner", standing="unknown"), odd) == Refuse("untrusted_identity")
    # Someone unsure is not refused out of hand: an unknown type is refused as for anyone.
    assert decide(Actor(uuid4(), "owner", standing="unsure"), odd) == Refuse("not_allowed")


def test_what_someone_unsure_may_do_comes_from_the_tables():
    # What a guest may do comes from the table of the roles: the unsure owner keeps the owner's.
    no_guest = PolicyTables(KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "guest": frozenset()})
    assert decide(Actor(uuid4(), "owner", standing="unsure"), RENAME, no_guest) == Allow()
    assert decide(Actor(uuid4(), "guest", standing="unsure"), RENAME, no_guest) == Refuse(
        "not_allowed"
    )


def test_someone_unsure_acts_with_the_probable_persons_rights_and_the_judge_weighs_the_rest():
    """The rights of the person who probably speaks apply as they are (POL-4): their own rights
    open what they open, an agreement a right asks is still asked, a deny still refuses; what is
    sensitive is marked for the judge, never refused out of hand."""
    claimed = Actor(uuid4(), "adult", standing="unsure")
    rights = RightsTable(
        (
            right(person_id=claimed.person_id, action_type="household.invite", access="allow"),
            right(role="guest", action_type="household.list", access="allow"),
            right(role="adult", action_type="profile.rename", approver="owner"),
        )
    )
    assert decide(claimed, INVITE, DEFAULT_POLICY, rights) == Allow(judge_identity=True)
    assert decide(claimed, LIST, DEFAULT_POLICY, rights) == Allow()
    renamed = decide(claimed, RENAME, DEFAULT_POLICY, rights)
    assert isinstance(renamed, NeedsApproval) and renamed.approver == "owner"
    assert not renamed.judge_identity
    denied = RightsTable(
        (right(person_id=claimed.person_id, action_type="profile.rename", access="deny"),)
    )
    assert decide(claimed, RENAME, DEFAULT_POLICY, denied) == Refuse("not_allowed")


# --- the foundation: changing the rules is the owner's ----------------------------------------


def test_the_rules_are_changed_by_the_owner_alone():
    assert (
        frozenset(
            {
                "policy.update",
                # The name the whole house calls Domo by.
                "house.name_domo",
                "rights.propose",
                "rights.apply",
                "household.transfer_ownership",
                "successors.show",
                "successors.set",
                "successors.note_death",
                "successors.undo_death",
                # The level of a model decides which models may judge a yes.
                "models.set_level",
                # Who a house account serves besides the household (ruling P2).
                "account.share",
                # The owner's directives on the people of the house.
                "people.directive_set",
                "people.directive_lift",
                "people.directives_look",
            }
        )
        == OWNER_ONLY_ACTIONS
    )


@pytest.mark.parametrize("action_type", sorted(OWNER_ONLY_ACTIONS))
@pytest.mark.parametrize("role", ["adult", "child", "guest"])
def test_changing_the_rules_is_the_owners_whatever_the_tables_say(action_type, role):
    everyone = PolicyTables(KNOWN_ACTIONS, {r: frozenset({action_type}) for r in ROLES})
    action = DescribedAction("core", action_type, None, "permissions", "household")
    assert decide(Actor(uuid4(), role, child=role == "child"), action, everyone) == Refuse(
        "not_allowed"
    )
    assert decide(Actor(uuid4(), role, child=role == "child"), action) == Refuse("not_allowed")


@pytest.mark.parametrize("action_type", sorted(OWNER_ONLY_ACTIONS))
def test_the_owner_is_not_stopped_by_the_foundation(action_type):
    tables = PolicyTables(KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "owner": frozenset({action_type})})
    action = DescribedAction("core", action_type, None, "permissions", "household")
    assert decide(Actor(uuid4(), "owner"), action, tables) == Allow()


@pytest.mark.parametrize("action_type", sorted(OWNER_ONLY_ACTIONS))
@pytest.mark.parametrize("role", ROLES)
def test_nobody_known_is_refused_first_and_the_owners_own_stay_his(action_type, role):
    everyone = PolicyTables(KNOWN_ACTIONS, {r: frozenset({action_type}) for r in ROLES})
    action = DescribedAction("core", action_type, None, "permissions", "household")
    child = role == "child"
    assert decide(
        Actor(uuid4(), role, standing="unknown", child=child), action, everyone
    ) == Refuse("untrusted_identity")
    unsure = decide(Actor(uuid4(), role, standing="unsure", child=child), action, everyone)
    if role == "owner":
        # The owner's own are sensitive: the judge weighs who asks.
        assert isinstance(unsure, Allow | NeedsApproval) and unsure.judge_identity
    else:
        assert unsure == Refuse("not_allowed")


@pytest.mark.parametrize("role", ["adult", "child", "guest"])
def test_the_transfer_is_the_owners_whatever_the_tables_say(role):
    """Nothing the owner wrote, and no table, gives the owner's role away: asked by anyone else
    than the owner it is refused, rights and approvers or not."""
    anyone = Actor(uuid4(), role, child=role == "child")
    for_all = PolicyTables(
        KNOWN_ACTIONS, {r: frozenset({"household.transfer_ownership"}) for r in ROLES}
    )
    rights = RightsTable(
        (
            right(
                person_id=anyone.person_id,
                action_type="household.transfer_ownership",
                access="allow",
                approver="self",
            ),
            right(role=role, action_type="household.transfer_ownership", access="allow"),
        )
    )
    for tables in (DEFAULT_POLICY, for_all):
        assert decide(anyone, TRANSFER, tables) == Refuse("not_allowed")
        assert decide(anyone, TRANSFER, tables, rights) == Refuse("not_allowed")
    # The owner may: what is asked of them is the judge's, and the yes they decided by default.
    owner = Actor(uuid4(), "owner")
    assert decide(owner, TRANSFER) == Allow()
    deny = RightsTable(
        (right(role="owner", action_type="household.transfer_ownership", access="deny"),)
    )
    assert decide(owner, TRANSFER, DEFAULT_POLICY, deny) == Allow()


def test_the_default_policy_keeps_the_settings_to_the_owner():
    update = DescribedAction("core", "policy.update", None, "permissions", "household")
    assert decide(Actor(uuid4(), "owner"), update) == Allow()
    for role in ("adult", "child", "guest"):
        assert decide(Actor(uuid4(), role, child=role == "child"), update) == Refuse("not_allowed")


# --- other tables ---------------------------------------------------------------------------


def test_the_tables_are_a_parameter():
    wider = PolicyTables(KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "guest": frozenset({"household.list"})})
    assert decide(Actor(uuid4(), "guest"), LIST, wider) == Allow()
    # The default policy is untouched by another one.
    assert decide(Actor(uuid4(), "guest"), LIST) == Refuse("not_allowed")


def test_a_table_can_forbid_an_action():
    other = PolicyTables(KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "adult": frozenset({"note.leave"})})
    assert decide(Actor(uuid4(), "adult"), NOTE, other) == Allow()
    assert decide(Actor(uuid4(), "adult"), LIST, other) == Refuse("not_allowed")


def test_an_action_that_a_table_allows_but_does_not_know_is_refused():
    odd = PolicyTables(frozenset(), {"owner": frozenset({"x.read"})})
    action = DescribedAction("core", "x.read", None, "read", "household")
    assert decide(Actor(uuid4(), "owner"), action, odd) == Refuse("not_allowed")


def test_a_role_missing_from_a_table_may_do_nothing():
    odd = PolicyTables(KNOWN_ACTIONS, {"owner": KNOWN_ACTIONS})
    assert decide(Actor(uuid4(), "adult"), LIST, odd) == Refuse("not_allowed")


# --- the owner's rights (spec 5.8) -------------------------------------------------------------


def test_with_no_right_the_barrier_decides_as_before():
    for role in ROLES:
        for action_type in KNOWN_ACTIONS:
            action = DescribedAction("core", action_type, None, "read", "household")
            actor = Actor(uuid4(), role, child=role == "child")
            assert decide(actor, action, DEFAULT_POLICY, NO_RIGHTS) == decide(actor, action)


def test_a_right_may_take_an_action_away():
    adult = Actor(uuid4(), "adult")
    rights = RightsTable(
        (right(person_id=adult.person_id, action_type="note.leave", access="deny"),)
    )
    assert decide(adult, NOTE, DEFAULT_POLICY, rights) == Refuse("not_allowed")


def test_no_right_reaches_the_rules_of_the_owner():
    adult = Actor(uuid4(), "adult")
    update = DescribedAction("core", "policy.update", None, "permissions", "household")
    rights = RightsTable(
        (
            right(role="adult", action_type="policy.update", access="allow"),
            right(person_id=adult.person_id, action_type="policy.update", access="allow"),
        )
    )
    assert decide(adult, update, DEFAULT_POLICY, rights) == Refuse("not_allowed")


# --- the audit of a decision ---------------------------------------------------------------


async def test_decision_audit_has_no_arguments(session, owner):
    await record_decision(
        session, Actor(owner.id, "owner"), INVITE, Allow(), tool_name="household_invite"
    )
    entry = await last_audit(session, "policy.decision")
    assert set(entry.details) == {"tool", "action_type", "target", "decision", "reason", "facts"}


async def test_the_decision_audit_holds_the_facts(session, owner):
    action = DescribedAction(
        "core",
        "household.invite",
        None,
        "permissions",
        "household",
        {"role": "guest", "days": 2, "display_name": "Sam", "none": None, "yes": True},
    )
    await record_decision(
        session, Actor(owner.id, "owner"), action, Allow(), tool_name="household_invite"
    )
    entry = await last_audit(session, "policy.decision")
    assert entry.details["facts"] == {
        "role": "guest",
        "days": 2,
        "display_name": "Sam",
        "none": None,
        "yes": True,
    }
    assert set(entry.details) == {"tool", "action_type", "target", "decision", "reason", "facts"}


async def test_a_decision_is_audited_with_who_must_agree(session, owner):
    await record_decision(
        session,
        Actor(owner.id, "child", child=True),
        LEAVE,
        NeedsApproval("adult"),
        tool_name="household_leave",
    )
    entry = await last_audit(session, "policy.decision")
    assert entry.actor == f"person:{owner.id}"
    assert entry.details == {
        "tool": "household_leave",
        "action_type": "household.leave",
        "target": "person:x",
        "decision": "needs_approval",
        "reason": "adult",
        "facts": dict(LEAVE.facts),
    }


async def test_an_allowed_action_is_audited_without_a_reason(session, owner):
    await record_decision(
        session, Actor(owner.id, "owner"), LIST, Allow(), tool_name="household_list"
    )
    entry = await last_audit(session, "policy.decision")
    assert entry.details == {
        "tool": "household_list",
        "action_type": "household.list",
        "target": None,
        "decision": "allow",
        "reason": None,
        "facts": {},
    }


async def test_a_refusal_is_audited_with_its_reason(session, owner):
    await record_decision(
        session,
        Actor(owner.id, "adult", standing="unknown"),
        INVITE,
        Refuse("untrusted_identity"),
        tool_name="household_invite",
    )
    entry = await last_audit(session, "policy.decision")
    assert entry.details["decision"] == "refuse"
    assert entry.details["reason"] == "untrusted_identity"


async def test_the_decision_made_by_decide_is_the_one_audited(session, owner):
    actor = Actor(owner.id, "child", child=True)
    decision = decide(actor, LEAVE)
    await record_decision(session, actor, LEAVE, decision, tool_name="household_leave")
    entry = await last_audit(session, "policy.decision")
    assert (entry.details["decision"], entry.details["reason"]) == ("needs_approval", "adult")


async def test_recording_a_decision_adds_exactly_one_entry(session, owner):
    before = (await session.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
    await record_decision(
        session, Actor(owner.id, "owner"), LIST, Allow(), tool_name="household_list"
    )
    after = (await session.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
    assert after == before + 1
