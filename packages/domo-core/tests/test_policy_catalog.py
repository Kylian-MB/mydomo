"""The action catalog, the default roles and the described action.

The expected tables below are written out in full on purpose: they are the plan's decision D1
(which actions the barrier knows, and who may do them), so a change to the catalog has to change
them here too. Whether a yes is asked is the yes-judge's (``test_judge_decides.py``).
"""

import ast
from pathlib import Path
from uuid import uuid4

import domo_core
import pytest
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    ACTION_FACTS,
    ADULT_AGREES_FOR_A_CHILD,
    ALLOWED_ACTIONS,
    CHANGES_NOTHING,
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    GUEST_DAYS_RULE,
    JUDGED_WITHIN,
    KNOWN_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PolicyTables,
    actions_allowed,
    needs_the_judge,
)
from domo_core.policy.roles import ROLES, Actor, as_role
from domo_core.tools.registry import TOOLS

# What everyone has of the notes left for them, a guest too: Domo tells one, and shows its exact
# words (spec 4.8).
RECEIVED_NOTE_ACTIONS = {"note.told", "note.show"}
# What everyone has of their devices, a guest too: seeing their own and taking one back.
OWN_DEVICE_ACTIONS = {"device.list", "device.remove"}
# What Domo does with his memory, in everyone's turn, a guest's too (0.9.0, spec 6.2).
MEMORY_ACTIONS = {"memory.keep", "memory.change", "memory.forget", "memory.search"}
# The plans Domo sets for himself, in everyone's turn, a guest's too (0.9.0, spec 6.9).
PLAN_ACTIONS = {"plan.set", "plan.change", "plan.cancel"}
# The name a person calls Domo by, theirs alone, a guest's too (0.9.0, spec 6.7).
NAME_ACTIONS = {"profile.name_domo", "people.nickname_keep", "people.nickname_forget"}
# Domo's look at what reached someone of his replies, a guest's too (0.9.0, spec 6.10).
DELIVERY_ACTIONS = {"delivery.look"}
# What Domo notes of a person's word, his look at a score and his corrections, a guest's too
# (0.9.1, trust per person).
TRUST_ACTIONS = {"trust.note", "trust.look", "trust.correct"}
# What Domo does with the tools of his own, a guest's too (0.10.0, spec 5.5): they are all his
# (2026-11-18), nobody needs a right of their own to have one made for the house.
TOOL_ACTIONS = {
    "tool.build",
    "tool.change",
    "tool.remove",
    "tool.look",
    "tool.find",
    "tool.launch_when",
    # The bounds of his tools are his settings (2026-11-21, spec 12.1).
    "tools.settings",
    "tool.set",
    # He keeps them up on his own (2026-11-21, spec 12.3).
    "tool.back",
    "tool.check",
}
# What a running program asks the core, in anyone's launch, a guest's too (2026-11-18): never
# judged nor shown.
PROGRAM_TYPES = {
    "program.begin",
    "program.base.read",
    "program.base.write",
    "program.later",
    "program.wake",
    "program.run",
}
# The agreements that last, a guest's too, for themselves (2026-11-18): judged and shown.
AGREEMENT_ACTIONS = {"agreement.keep", "agreement.drop"}
# What the owner and the adults may do by default: entrust a device to Domo (a child or a guest
# only by a right), and invite someone, give the link of their invitation again or cancel it (the
# owner's trial of 0.9.1: "Domo judges").
INVITING_ACTIONS = {
    "household.invite",
    "household.invite_again",
    "household.cancel_invitation",
}
ADULT_ACTIONS = {"device.entrust", *INVITING_ACTIONS}
# What every member of the household (the owner, adults and children) may do.
MEMBER_ACTIONS = {
    "household.list",
    "profile.rename",
    "note.leave",
    "note.list_sent",
    "note.withdraw",
    "note.keep",
    # A note left for the person: told by Domo, its exact words shown (spec 4.8).
    *RECEIVED_NOTE_ACTIONS,
    "confirmation.remind",
    "confirmation.expire",
    "confirmation.require_explicit",
    "confirmation.amend",
    "confirmation.ask_before",
    "chain.run",
    "chain.continue",
    "household.leave",
    # Domo's own choices of models, and a step up for the rest of a request.
    "models.choose",
    "models.step_up",
    # Each member's own keys (0.6.0): add one, give it again, remove it, share it, limit it, give
    # its admin key; what Domo costs; the catalog, the rate and the key's models read again.
    "key.add",
    "model.rekey",
    "key.remove",
    "key.share",
    "spending.limit",
    "key.admin",
    "spending.show",
    "models.refresh",
    # Each member's own texts, writing note and language (spec 4.7), and Domo's choices on
    # writing.
    "texts.find",
    "texts.rewrite",
    "texts.restore",
    "texts.language",
    "texts.note",
    "writing.choose",
    # Seeing one's own devices and taking one back (spec 6.5).
    *OWN_DEVICE_ACTIONS,
    # Domo's memory (spec 6.2).
    *MEMORY_ACTIONS,
    *PLAN_ACTIONS,
    *NAME_ACTIONS,
    *DELIVERY_ACTIONS,
    *TRUST_ACTIONS,
    *TOOL_ACTIONS,
    *PROGRAM_TYPES,
    *AGREEMENT_ACTIONS,
}
# What everyone has of their own texts, a guest too: the texts they read, their writing note and
# their language (spec 4.7).
OWN_TEXT_ACTIONS = {"texts.find", "texts.rewrite", "texts.restore", "texts.language", "texts.note"}
# What only the owner may do by default (the foundation, ``catalog.OWNER_ONLY_ACTIONS``, is a part
# of it).
OWNER_ACTIONS = {
    "household.change_role",
    "household.remove",
    "household.bring_back",
    "household.transfer_ownership",
    "policy.show",
    "policy.update",
    "rights.propose",
    "rights.apply",
    "successors.show",
    "successors.set",
    "successors.note_death",
    "successors.undo_death",
    "models.set_level",
    # The rules of the house on the keys: the owner's by default, and they can be given.
    "keys.house",
    # The version of a language (the visitors' texts): the owner's by default, given like any
    # right.
    "texts.language_version",
}
# What nobody may do by default, the owner neither: changing someone else's texts, writing note or
# language (spec 4.7, socle 8). Only a right opens it.
NOBODYS_ACTIONS = {"texts.for_someone"}


def test_catalog_matches_the_plan_table():
    assert KNOWN_ACTIONS == MEMBER_ACTIONS | ADULT_ACTIONS | OWNER_ACTIONS | NOBODYS_ACTIONS
    assert len(KNOWN_ACTIONS) == 86


def test_handing_the_owners_role_over_is_the_owners_alone_and_judged():
    action = "household.transfer_ownership"
    assert needs_the_judge(action)
    assert action in OWNER_ONLY_ACTIONS
    assert dict(ACTION_FACTS[action]) == {
        "from": "person",
        "to": "person",
        "requester_role": "role",
        "to_role": "role",
    }
    for role in ("adult", "child", "guest"):
        assert action not in ALLOWED_ACTIONS[role]


def test_the_owners_list_of_successors_is_the_owners_alone_and_its_change_judged():
    for action in (
        "successors.show",
        "successors.set",
        "successors.note_death",
        "successors.undo_death",
    ):
        assert action in OWNER_ONLY_ACTIONS
        for role in ("adult", "child", "guest"):
            assert action not in ALLOWED_ACTIONS[role]
    # Reading the list changes nothing; changing it, telling a death and putting a successor back
    # are judged like any action.
    assert "successors.show" in CHANGES_NOTHING
    for action in ("successors.set", "successors.note_death", "successors.undo_death"):
        assert needs_the_judge(action)


def test_the_chain_is_for_the_members_not_the_guest():
    # A chain is judged as a whole; the rest of a chain that stopped is judged inside.
    assert needs_the_judge("chain.run")
    assert "chain.continue" in JUDGED_WITHIN
    for action in ("chain.run", "chain.continue"):
        for role in ("owner", "adult", "child"):
            assert action in ALLOWED_ACTIONS[role]
        assert action not in ALLOWED_ACTIONS["guest"]
        assert action not in OWNER_ONLY_ACTIONS
    assert dict(ACTION_FACTS["chain.continue"]) == {}
    assert dict(ACTION_FACTS["chain.run"]) == {
        "steps": "number",
        "checked_in_turn": "number",
        "waits": "number",
    }


def test_no_type_asks_a_yes_by_default():
    """The owner's decision of 2026-11-15: what a person asks for and Domo understood is done.
    The catalog holds no yes of its own any more (it had one for the hand-over of the owner's
    role, the list of successors, a death undone and applying rights): asking is the judge's,
    Domo's, or a rule the owner set himself."""
    from domo_core.policy import catalog

    for gone in ("ALWAYS_ASKED", "ALWAYS_EXPLICIT_ACTIONS", "floor_of"):
        assert not hasattr(catalog, gone), gone
    # What they asked a yes for is judged like any action (drafting rights changes nothing).
    for action in (
        "household.transfer_ownership",
        "successors.set",
        "successors.undo_death",
        "rights.apply",
    ):
        assert needs_the_judge(action) and action in OWNER_ONLY_ACTIONS


def test_the_roles_are_the_four_household_roles():
    assert ROLES == ("owner", "adult", "child", "guest")


def test_a_stored_role_becomes_a_role():
    assert [as_role(value) for value in ROLES] == list(ROLES)


@pytest.mark.parametrize("value", ["member", "Owner", "", "admin"])
def test_a_value_that_is_not_a_role_is_refused(value):
    with pytest.raises(ValueError, match="role"):
        as_role(value)


def test_who_may_use_what_matches_the_plan_table():
    assert {
        "owner": MEMBER_ACTIONS | ADULT_ACTIONS | OWNER_ACTIONS,
        "adult": MEMBER_ACTIONS | ADULT_ACTIONS,
        "child": MEMBER_ACTIONS,
        # A guest may rename themselves, leave early (spec 5.9), have Domo take a stronger
        # model for the rest of a request, add, give again, remove or limit a key of their own,
        # ask what they cost, change their own texts, writing note and language, and hear the
        # notes left for them.
        "guest": {
            "profile.rename",
            "household.leave",
            "models.step_up",
            "key.add",
            "model.rekey",
            "key.remove",
            "spending.limit",
            "spending.show",
            *OWN_TEXT_ACTIONS,
            *RECEIVED_NOTE_ACTIONS,
            *OWN_DEVICE_ACTIONS,
            *MEMORY_ACTIONS,
            *PLAN_ACTIONS,
            *NAME_ACTIONS,
            *DELIVERY_ACTIONS,
            *TRUST_ACTIONS,
            *TOOL_ACTIONS,
            *PROGRAM_TYPES,
            *AGREEMENT_ACTIONS,
        },
    } == ALLOWED_ACTIONS


def test_every_role_has_an_entry_and_every_entry_is_a_frozenset():
    assert set(ALLOWED_ACTIONS) == set(ROLES)
    assert all(isinstance(actions, frozenset) for actions in ALLOWED_ACTIONS.values())


def test_no_role_may_use_an_action_the_catalog_does_not_know():
    for role in ROLES:
        assert ALLOWED_ACTIONS[role] <= KNOWN_ACTIONS


def test_the_owner_may_use_every_action_of_the_catalog_but_someone_elses_texts():
    assert ALLOWED_ACTIONS["owner"] == KNOWN_ACTIONS - NOBODYS_ACTIONS


def test_actions_allowed_gives_the_set_of_the_role():
    for role in ROLES:
        assert actions_allowed(role) == ALLOWED_ACTIONS[role]
    assert "household.change_role" not in actions_allowed("adult")


def test_an_adult_invites_and_a_child_or_a_guest_does_not():
    """The owner's trial of 0.9.1: an adult asked Domo to add their child, and could not.
    Inviting, giving the link of one's invitation again and cancelling it are the adults' too; a
    child's and a guest's are as before (a right opens them)."""
    for action in INVITING_ACTIONS:
        assert action in ALLOWED_ACTIONS["owner"] and action in ALLOWED_ACTIONS["adult"]
        assert action not in ALLOWED_ACTIONS["child"] and action not in ALLOWED_ACTIONS["guest"]
        assert action not in OWNER_ONLY_ACTIONS and needs_the_judge(action)
    # Whether Domo sends the invitation to the owner first is one of its facts, and a new link
    # carries it too (always false), so that a limit on inviting reads both the same way.
    for action in ("household.invite", "household.invite_again"):
        assert dict(ACTION_FACTS[action]) == {
            "role": "role",
            "days": "number",
            "display_name": "text",
            "owner_first": "boolean",
        }


def test_the_default_policy_is_the_catalog():
    assert PolicyTables(KNOWN_ACTIONS, ALLOWED_ACTIONS) == DEFAULT_POLICY
    assert DEFAULT_POLICY.known is KNOWN_ACTIONS
    assert DEFAULT_POLICY.allowed is ALLOWED_ACTIONS
    # No kind of the open catalog in the code's own tables, and none can be added by accident.
    assert DEFAULT_POLICY.kinds == {}
    with pytest.raises(TypeError):
        DEFAULT_POLICY.kinds["made.x"] = None  # type: ignore[index]


def test_the_tables_cannot_be_changed_by_accident():
    with pytest.raises(AttributeError):
        KNOWN_ACTIONS.add("household.delete_everything")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        ALLOWED_ACTIONS["guest"] = frozenset({"household.invite"})


def test_an_actor_is_trusted_unless_said_otherwise():
    person_id = uuid4()
    assert Actor(person_id, "adult").trusted is True
    assert Actor(person_id, "adult", trusted=False).trusted is False


# --- the described action ------------------------------------------------------------------

ACTION = DescribedAction("core", "note.leave", "person:x", "message", "household")


def test_a_described_action_is_written_as_json():
    assert ACTION.as_json() == {
        "connector": "core",
        "action_type": "note.leave",
        "target": "person:x",
        "effect": "message",
        "destination": "household",
        "facts": {},
    }
    assert (
        DescribedAction("core", "household.list", None, "read", "household").as_json()["target"]
        is None
    )


@pytest.mark.parametrize(
    "action",
    [
        ACTION,
        DescribedAction("core", "household.list", None, "read", "household"),
        DescribedAction("example", "mail.send", "x@example.com", "message", "external"),
        DescribedAction("core", "profile.rename", None, "self_profile", "household"),
        DescribedAction("core", "confirmation.expire", None, "own_requests", "household"),
        DescribedAction("core", "policy.update", None, "permissions", "household"),
        DescribedAction("core", "model.rekey", None, "secrets", "household"),
        DescribedAction("program", "program.base.read", None, "program", "household"),
    ],
)
def test_from_json_is_the_inverse_of_as_json(action):
    assert DescribedAction.from_json(action.as_json()) == action


def test_from_json_accepts_what_a_database_gives_back():
    # JSONB comes back as a plain dict, whatever the order of the keys.
    stored = {
        "destination": "household",
        "effect": "message",
        "target": None,
        "action_type": "note.leave",
        "connector": "core",
    }
    assert DescribedAction.from_json(stored) == DescribedAction(
        "core", "note.leave", None, "message", "household"
    )


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"connector": "core"},
        # A key is missing.
        {"connector": "core", "action_type": "a", "effect": "read", "destination": "household"},
        # A key too many: a stored action is exactly what ``as_json`` wrote.
        {**ACTION.as_json(), "arguments": "x"},
        # Values that are not what the field says.
        {**ACTION.as_json(), "effect": "everything"},
        {**ACTION.as_json(), "destination": "mars"},
        {**ACTION.as_json(), "connector": 3},
        {**ACTION.as_json(), "action_type": None},
        {**ACTION.as_json(), "target": 7},
    ],
)
def test_from_json_refuses_anything_else(broken):
    with pytest.raises(ValueError):
        DescribedAction.from_json(broken)


def test_from_json_refuses_what_is_not_an_object():
    with pytest.raises(ValueError):
        DescribedAction.from_json(["core"])
    with pytest.raises(ValueError):
        DescribedAction.from_json(None)


# --- the guest stay rule is written once ---------------------------------------------------------


def test_the_guest_stay_rule_is_one_constant_used_by_the_limit_and_the_tools():
    limit = DEFAULT_LIMITS["household.change_role"]
    assert GUEST_DAYS_RULE == 30
    assert f"action.facts.days > {GUEST_DAYS_RULE} " in limit.expression
    assert f"{GUEST_DAYS_RULE} days" in limit.summary
    assert DEFAULT_LIMITS["household.bring_back"] is limit
    # Inviting holds the same stay, and asks the owner when Domo judged so (0.9.1); a new link
    # follows inviting (``RIGHTS_OF``).
    inviting = DEFAULT_LIMITS["household.invite"]
    assert f"action.facts.days > {GUEST_DAYS_RULE} " in inviting.expression
    assert inviting.summary.startswith(limit.summary)
    assert DEFAULT_LIMITS["household.invite_again"] is inviting
    for name in ("household_invite", "household_change_role", "household_bring_back"):
        assert f"{GUEST_DAYS_RULE} days at most" in TOOLS[name].definition.description, name


@pytest.mark.parametrize(
    "module", ["policy/catalog.py", "tools/household_admin.py", "tools/household_return.py"]
)
def test_no_other_30_is_written_by_hand_for_the_guest_stay_rule(module):
    """The numeral is only in ``GUEST_DAYS_RULE``'s own line: not in an expression, a summary or
    a description (docstrings and comments, which only talk about the rule, are left out)."""
    source = (Path(domo_core.__file__).parent / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and id(node) not in docstrings
        and (node.value == 30 or (isinstance(node.value, str) and "30" in node.value))
    ]
    allowed = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "GUEST_DAYS_RULE"
    ]
    assert found == allowed


# --- the marks of the yes-judge (0.5.0) ----------------------------------------------------------


def test_what_changes_nothing_passes_without_the_judge():
    assert {
        "household.list",
        "note.list_sent",
        "policy.show",
        "successors.show",
        "rights.propose",
        "confirmation.remind",
        "confirmation.expire",
        "confirmation.require_explicit",
        "confirmation.ask_before",
        "models.choose",
        "models.step_up",
        "models.refresh",
        "spending.show",
        "texts.find",
        "writing.choose",
        "note.told",
        "note.show",
        "device.list",
        # Domo's memory is his judgement (spec 6.2).
        *MEMORY_ACTIONS,
        *PLAN_ACTIONS,
        *NAME_ACTIONS,
        *DELIVERY_ACTIONS,
        *TRUST_ACTIONS,
        # Looking at one's tools and finding one change nothing; making, changing, removing or
        # timing one does, though nobody is told (``SILENT_ACTIONS``).
        "tool.look",
        "tool.find",
        # Checking one runs its examples as dry runs, which keep nothing.
        "tool.check",
    } == CHANGES_NOTHING
    assert {"confirmation.amend", "chain.continue"} == JUDGED_WITHIN
    assert needs_the_judge("household.invite") is True
    assert needs_the_judge("confirmation.amend") is False
    assert needs_the_judge("policy.show") is False
    for action_type in CHANGES_NOTHING | JUDGED_WITHIN:
        assert action_type in KNOWN_ACTIONS


def test_domo_memory_and_plans_are_never_shown_whatever_came_of_them():  # spec 6.2, 6.9
    from domo_core.policy.catalog import SILENT_ACTIONS, is_silent

    assert SILENT_ACTIONS == (
        MEMORY_ACTIONS
        | PLAN_ACTIONS
        | NAME_ACTIONS
        | DELIVERY_ACTIONS
        | TRUST_ACTIONS
        | TOOL_ACTIONS
        | PROGRAM_TYPES
    )
    assert all(is_silent(action_type) for action_type in SILENT_ACTIONS)
    for action_type in KNOWN_ACTIONS - SILENT_ACTIONS:
        assert not is_silent(action_type), action_type
    assert not is_silent("")


def test_a_tool_domo_made_is_never_shown_nor_judged():  # spec 5.3, the rulings of the plan
    from domo_core.policy.catalog import MADE_PREFIX, is_made, is_silent

    assert MADE_PREFIX == "made."
    for action_type in ("made.split", "made.x"):
        assert is_made(action_type) and is_silent(action_type)
        assert not needs_the_judge(action_type)
    # No type of the code takes the prefix.
    assert not any(is_made(action_type) for action_type in KNOWN_ACTIONS)
    assert not is_made("household.made.x")


def test_a_silent_action_is_shown_only_while_it_waits_for_a_yes():  # Task 8
    from domo_core.policy.catalog import shown

    for status in ("done", "failed", "refused", "invalid"):
        assert not shown("made.order", status) and not shown("memory.keep", status)
    assert shown("made.order", "waiting")
    assert shown("note.leave", "done") and shown("agreement.keep", "done")


def test_a_child_needs_an_adult_for_the_same_types_as_before_and_their_keys():
    assert {
        "household.cancel_invitation",
        "household.invite",
        # A new link for an invitation is inviting (2026-11-16).
        "household.invite_again",
        "household.change_role",
        "household.remove",
        "household.leave",
        "household.bring_back",
        "model.rekey",
        # A child adds, shares or limits a key of their own, or gives its admin key, with an
        # adult's agreement (0.6.0).
        "key.add",
        "key.share",
        "spending.limit",
        "key.admin",
    } == ADULT_AGREES_FOR_A_CHILD
    assert ADULT_AGREES_FOR_A_CHILD.isdisjoint(OWNER_ONLY_ACTIONS)
    assert ADULT_AGREES_FOR_A_CHILD <= KNOWN_ACTIONS


# --- the choices of models (0.6.0) ---------------------------------------------------------------


def test_domo_s_choices_change_nothing_and_the_level_of_a_model_is_the_owners():
    # Domo's choices and a step up change nothing in the household: no judge, and members (a
    # guest, for the step up) have them.
    for action in ("models.choose", "models.step_up"):
        assert not needs_the_judge(action)
        assert action not in OWNER_ONLY_ACTIONS
        for role in ("owner", "adult", "child"):
            assert action in ALLOWED_ACTIONS[role]
    assert "models.choose" not in ALLOWED_ACTIONS["guest"]
    assert "models.step_up" in ALLOWED_ACTIONS["guest"]
    # The level of a model is the owner's word, judged like any change.
    assert "models.set_level" in OWNER_ONLY_ACTIONS
    assert needs_the_judge("models.set_level")
    # A limit may tell the choices of a person's own key from the household's.
    assert dict(ACTION_FACTS["models.choose"]) == {"own_key": "boolean"}
    assert dict(ACTION_FACTS["models.step_up"]) == {}
    assert dict(ACTION_FACTS["models.set_level"]) == {}
    assert not {"models.choose", "models.step_up", "models.set_level"} & ADULT_AGREES_FOR_A_CHILD


def test_each_member_has_their_own_keys():
    # Adding, giving again, removing and sharing a key are judged, and nobody's alone.
    for action in ("key.add", "model.rekey", "key.remove", "key.share"):
        assert needs_the_judge(action)
        assert action not in OWNER_ONLY_ACTIONS
        for role in ("owner", "adult", "child"):
            assert action in ALLOWED_ACTIONS[role]
    for action in ("key.add", "model.rekey", "key.remove"):
        assert dict(ACTION_FACTS[action]) == {}
    # A shared key is told by its kind and its giver, for whoever says yes to it.
    assert dict(ACTION_FACTS["key.share"]) == {"key_kind": "text", "key_of": "text"}
    # A guest brings a key of their own, and takes it away, but shares none.
    assert {"key.add", "model.rekey", "key.remove"} <= ALLOWED_ACTIONS["guest"]
    assert "key.share" not in ALLOWED_ACTIONS["guest"]
    # Removing one's own key needs nobody's agreement.
    assert "key.remove" not in ADULT_AGREES_FOR_A_CHILD


def test_the_house_rules_on_the_keys_are_the_owners_and_can_be_given():
    # The owner's by default, judged like any change, and not the owner's alone: a right gives it.
    action = "keys.house"
    assert action in ALLOWED_ACTIONS["owner"]
    for role in ("adult", "child", "guest"):
        assert action not in ALLOWED_ACTIONS[role]
    assert action not in OWNER_ONLY_ACTIONS
    assert needs_the_judge(action)
    assert dict(ACTION_FACTS[action]) == {}
    assert action not in ADULT_AGREES_FOR_A_CHILD


def test_each_member_limits_their_own_keys():
    # Every member's, a guest's too, judged; a child's waits for an adult's agreement.
    action = "spending.limit"
    for role in ("owner", "adult", "child", "guest"):
        assert action in ALLOWED_ACTIONS[role]
    assert action not in OWNER_ONLY_ACTIONS
    assert needs_the_judge(action)
    # The key is told by its kind and its giver, for whoever says yes to it.
    assert dict(ACTION_FACTS[action]) == {"key_kind": "text", "key_of": "text"}
    assert action in ADULT_AGREES_FOR_A_CHILD


def test_what_domo_costs_is_everyone_s_and_the_admin_key_a_member_s():
    # Asking what one costs, and reading the catalog again, change nothing: no judge.
    for action in ("spending.show", "models.refresh"):
        assert not needs_the_judge(action)
        assert dict(ACTION_FACTS[action]) == {}
        for role in ("owner", "adult", "child"):
            assert action in ALLOWED_ACTIONS[role]
    assert "spending.show" in ALLOWED_ACTIONS["guest"]
    assert "models.refresh" not in ALLOWED_ACTIONS["guest"]
    # The admin key of one's own key is judged, a member's (an adult agrees for a child), and
    # whoever says yes reads which key it is.
    assert needs_the_judge("key.admin")
    assert "key.admin" not in ALLOWED_ACTIONS["guest"]
    assert "key.admin" in ADULT_AGREES_FOR_A_CHILD
    assert dict(ACTION_FACTS["key.admin"]) == {"key_kind": "text", "key_of": "text"}


# --- the texts people read (0.7.0) --------------------------------------------------------------


def test_each_person_changes_their_own_texts_and_nobody_someone_elses():
    # Spec 4.7: each person their own texts, writing note and language, a child and a guest too,
    # judged, and with nobody's agreement; finding them changes nothing.
    for action in OWN_TEXT_ACTIONS:
        for role in ROLES:
            assert action in ALLOWED_ACTIONS[role]
        assert action not in OWNER_ONLY_ACTIONS
        assert action not in ADULT_AGREES_FOR_A_CHILD
        assert needs_the_judge(action) is (action != "texts.find")
    for action in OWN_TEXT_ACTIONS - {"texts.language"}:
        assert dict(ACTION_FACTS[action]) == {}
    # The owner's own language moves the house's: who judges or says yes reads it.
    assert dict(ACTION_FACTS["texts.language"]) == {"house_follows": "boolean"}
    # Someone else's: nobody by default, the owner neither (socle 8); a right opens it, and a limit
    # reads whom it is about.
    action = "texts.for_someone"
    for role in ROLES:
        assert action not in ALLOWED_ACTIONS[role]
    assert action not in OWNER_ONLY_ACTIONS and needs_the_judge(action)
    assert dict(ACTION_FACTS[action]) == {"person": "person", "person_role": "role"}
    # The version of a language: the owner's by default, and it can be given.
    action = "texts.language_version"
    assert action in ALLOWED_ACTIONS["owner"] and action not in OWNER_ONLY_ACTIONS
    for role in ("adult", "child", "guest"):
        assert action not in ALLOWED_ACTIONS[role]
    assert needs_the_judge(action)
    # Domo's choices on writing change nothing, and are the members' (not a guest's).
    action = "writing.choose"
    assert not needs_the_judge(action)
    for role in ("owner", "adult", "child"):
        assert action in ALLOWED_ACTIONS[role]
    assert action not in ALLOWED_ACTIONS["guest"]
