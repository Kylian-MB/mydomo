"""The tools Domo may call: the registry, who sees which, and the first two tools (what they
describe to the barrier, and what they do). The tools that manage pending requests are tested in
``test_confirmation_tools.py``, the one that leaves a note in ``test_notes.py``."""

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.models import Person
from domo_core.household.models import Invitation
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PolicyTables,
)
from domo_core.policy.rights import Right, RightsTable
from domo_core.policy.roles import ROLES, Actor
from domo_core.tools.context import ToolContext, ToolOutcome
from domo_core.tools.registry import TOOLS, InternalTool, InvalidArguments, tools_for
from sqlalchemy import select

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def names(definitions) -> list[str]:
    return [definition.name for definition in definitions]


def context(person: Person, session_factory=None, *, now: datetime = NOW) -> ToolContext:
    """The context of a call made by ``person`` (what the tools of this file do not use is
    left empty)."""
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,
        clock=lambda: now,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


def someone(role: str = "adult", name: str = "Alex") -> Person:
    return Person(id=uuid.uuid4(), display_name=name, role=role)


# --- the registry -------------------------------------------------------------------------------


# The tools with which a person leaves a note, and finds again, takes back and keeps the notes they
# left.
NOTE_TOOLS = ["note_leave", "note_list_sent", "note_withdraw", "note_keep"]
# The tools of a note left for the person: Domo tells it, and shows its exact words (everyone's,
# a guest's too: spec 4.8).
RECEIVED_NOTE_TOOLS = ["note_told", "note_show"]
# The tools with which Domo manages the person's pending requests (task 7, decision D1).
REQUEST_TOOLS = [
    "confirmation_remind",
    "confirmation_expire",
    "confirmation_require_explicit",
    "confirmation_amend",
    "ask_before_acting",
]
# The tools with which the owner reads and changes their rules (task 8, decision D8).
POLICY_TOOLS = ["policy_show", "policy_update"]
# The tools with which the owner changes the rights in words (spec 5.6).
RIGHTS_TOOLS = ["rights_propose", "rights_apply"]
# The tools with which the owner changes who is in the household (task 9, decisions D1 and D3).
HOUSEHOLD_TOOLS = [
    "household_invite",
    "household_change_role",
    "household_remove",
    "household_cancel_invitation",
    "invitation_link_again",
    "household_bring_back",
    "household_transfer_ownership",
]
# The tools of each member's own keys: give one again (task 13), add one, remove one, say who
# else may use it, limit what it spends, give its admin key, and what Domo costs (0.6.0).
MODEL_TOOLS = [
    "model_rekey",
    "key_add",
    "key_remove",
    "key_share",
    "key_limit",
    "key_admin",
    "spending_show",
]
# What a guest has of them: no sharing, no admin key.
GUEST_KEY_TOOLS = ["model_rekey", "key_add", "key_remove", "key_limit", "spending_show"]
# The rules of the house on the keys: the owner's by default.
HOUSE_KEY_TOOLS = ["keys_house"]
# The tools of Domo's own choices of models: his choices, a step up for the rest of a request,
# the owner's word on the level of a model, and the catalog read again.
CHOICE_TOOLS = ["models_choose", "models_step_up", "models_set_level", "models_refresh"]
# What every member has of them (a guest has the step up only).
MEMBER_CHOICE_TOOLS = ["models_choose", "models_step_up", "models_refresh"]
# The tools of the texts people read (spec 4.7): each person's own (a guest's too), someone
# else's (nobody's by default), the version of a language (the owner's by default), and Domo's
# choices on writing (the members').
OWN_TEXT_TOOLS = ["texts_find", "texts_rewrite", "texts_restore", "texts_language", "texts_note"]
TEXT_TOOLS = [*OWN_TEXT_TOOLS, "texts_for_someone", "texts_rewrite_language", "writing_choose"]
# The tools with which the owner keeps the ordered list of who takes over.
SUCCESSOR_TOOLS = [
    "successors_show",
    "successors_set",
    "successors_note_death",
    "successors_undo_death",
]
# The tools with which Domo does several of its tools in order, with one yes, and continues the
# rest of such a chain that stopped.
CHAIN_TOOLS = ["do_in_order", "chain_continue"]
# The tools with which a person entrusts a device to Domo, sees theirs and takes one back
# (spec 6.5).
DEVICE_TOOLS = ["device_entrust", "device_list", "device_remove"]
# What a child and a guest have of them by default: seeing their own and taking one back.
OWN_DEVICE_TOOLS = ["device_list", "device_remove"]
# The tools with which Domo keeps, changes, forgets and looks up his memories (0.9.0, spec 6.2):
# everyone's, a guest's too.
MEMORY_TOOLS = ["memory_keep", "memory_change", "memory_forget", "memory_search"]
# The tools with which Domo sets, changes and cancels his plans (0.9.0, spec 6.9): everyone's,
# a guest's too.
PLAN_TOOLS = ["plan_set", "plan_change", "plan_cancel"]
# The tools with which a person gives Domo a name of their own, and keeps or forgets the names
# they use for the others (0.9.0, spec 6.7): everyone's, a guest's too.
NAME_TOOLS = ["domo_name", "nickname_keep", "nickname_forget"]
# The tool with which Domo looks at what reached someone of his replies (0.9.0, spec 6.10):
# everyone's, a guest's too.
DELIVERY_TOOLS = ["delivery_look"]
# The tools with which Domo notes what a person's word was worth, looks at a score and corrects
# a fact (0.9.1, trust per person): everyone's, a guest's too.
TRUST_TOOLS = ["trust_note", "trust_look", "trust_correct"]
# The tools with which Domo makes, changes, removes, looks at, finds and times the tools of his
# own (0.10.0): everyone's, a guest's too; the tools are all his (2026-11-18).
MADE_TOOLS = [
    "tool_build",
    "tool_change",
    "tool_remove",
    "tool_look",
    "tool_find",
    "tool_launch_when",
    # The bounds of his tools, his settings (2026-11-21, spec 12.1): for the house, for one tool.
    "tools_settings",
    "tool_set",
    # He keeps them up on his own (2026-11-21, spec 12.3): back to a version, its examples again.
    "tool_back",
    "tool_check",
    # A program run by hand (2026-11-18, spec 5.4): Domo's own act, in anyone's turn.
    "program_run",
    # The agreements that last (2026-11-18): everyone's, a guest's too, for themselves.
    "agreement_keep",
    "agreement_drop",
]
# Going out (step 2, spec 13): Domo opens and closes the addresses his programs reach and looks at
# them (his own acts, in anyone's turn, a guest's too); an account is asked for, removed, shared
# and looked at (the person's: asked by the owner, an adult or a child, removed by the owner or an
# adult, shared by the owner, looked at by everyone).
ADDRESS_TOOLS = ["address_open", "address_close", "addresses_look"]
ACCOUNT_TOOLS = ["account_ask", "account_remove", "account_share", "accounts_look"]
OUTWARD_TOOLS = [*ADDRESS_TOOLS, *ACCOUNT_TOOLS]
# What every person of the household has of their own: renaming themselves, and leaving the
# household themselves (spec 5.9; the owner is refused it by the tool, whatever the rights).
OWN_TOOLS = ["profile_rename", "household_leave"]


def test_the_registry_knows_its_tools_by_the_name_the_model_uses():
    assert list(TOOLS) == [
        "household_list",
        *OWN_TOOLS,
        *NOTE_TOOLS,
        *RECEIVED_NOTE_TOOLS,
        *REQUEST_TOOLS,
        *POLICY_TOOLS,
        *RIGHTS_TOOLS,
        *HOUSEHOLD_TOOLS,
        *MODEL_TOOLS,
        *HOUSE_KEY_TOOLS,
        *CHOICE_TOOLS,
        *TEXT_TOOLS,
        *SUCCESSOR_TOOLS,
        *DEVICE_TOOLS,
        *MEMORY_TOOLS,
        *PLAN_TOOLS,
        *NAME_TOOLS,
        *DELIVERY_TOOLS,
        *TRUST_TOOLS,
        *MADE_TOOLS,
        *OUTWARD_TOOLS,
        *CHAIN_TOOLS,
    ]
    for name, tool in TOOLS.items():
        assert tool.definition.name == name
        assert isinstance(tool, InternalTool)


def test_every_tool_is_an_action_of_the_catalog():
    assert {tool.action_type for tool in TOOLS.values()} == {
        "household.list",
        "profile.rename",
        "household.leave",
        "note.leave",
        "note.list_sent",
        "note.withdraw",
        "note.keep",
        "note.told",
        "note.show",
        "confirmation.remind",
        "confirmation.expire",
        "confirmation.require_explicit",
        "confirmation.amend",
        "confirmation.ask_before",
        "policy.show",
        "policy.update",
        "rights.propose",
        "rights.apply",
        "household.invite",
        "household.change_role",
        "household.remove",
        "household.cancel_invitation",
        "household.invite_again",
        "household.bring_back",
        "household.transfer_ownership",
        "model.rekey",
        "key.add",
        "key.remove",
        "key.share",
        "spending.limit",
        "key.admin",
        "spending.show",
        "keys.house",
        "models.choose",
        "models.step_up",
        "models.set_level",
        "models.refresh",
        "texts.find",
        "texts.rewrite",
        "texts.restore",
        "texts.language",
        "texts.note",
        "texts.for_someone",
        "texts.language_version",
        "writing.choose",
        "successors.show",
        "successors.set",
        "successors.note_death",
        "successors.undo_death",
        "device.entrust",
        "device.list",
        "device.remove",
        "memory.keep",
        "memory.change",
        "memory.forget",
        "memory.search",
        "plan.set",
        "plan.change",
        "plan.cancel",
        "profile.name_domo",
        "people.nickname_keep",
        "people.nickname_forget",
        "delivery.look",
        "trust.note",
        "trust.look",
        "trust.correct",
        "tool.build",
        "tool.change",
        "tool.remove",
        "tool.look",
        "tool.find",
        "tool.launch_when",
        "tools.settings",
        "tool.set",
        "tool.back",
        "tool.check",
        "program.run",
        "agreement.keep",
        "agreement.drop",
        "net.open",
        "net.close",
        "net.look",
        "account.ask",
        "account.remove",
        "account.share",
        "account.look",
        "chain.run",
        "chain.continue",
    }
    for tool in TOOLS.values():
        assert tool.action_type in KNOWN_ACTIONS


def test_every_tool_declares_an_object_of_arguments_and_says_what_it_does():
    for tool in TOOLS.values():
        parameters = tool.definition.parameters
        assert parameters["type"] == "object" and parameters["additionalProperties"] is False
        assert tool.definition.description


def _keywords_of(schema: object) -> set[str]:
    """Every keyword a JSON schema uses, however deep."""
    if isinstance(schema, dict):
        found = set(schema)
        for value in schema.values():
            found |= _keywords_of(value)
        return found
    if isinstance(schema, list):
        return set().union(*(_keywords_of(item) for item in schema))
    return set()


def test_no_tool_schema_uses_an_exclusive_bound():
    """Every provider is given the schemas as they are (the Google adapter among them): a bound
    is ``minimum`` or ``maximum``, the keywords every one of them reads."""
    for name, tool in TOOLS.items():
        used = _keywords_of(tool.definition.parameters)
        assert not used & {"exclusiveMinimum", "exclusiveMaximum"}, name
    amount = TOOLS["key_limit"].definition.parameters["properties"]["amount"]
    assert amount["minimum"] == 0.0001
    house = TOOLS["keys_house"].definition.parameters["properties"]["limit_amount"]
    assert house["minimum"] == 0.0001


def test_no_tool_description_promises_a_yes_or_a_setting_of_style():
    for name, tool in TOOLS.items():
        text = tool.definition.description.lower()
        for phrase in (
            "it waits for a yes",
            "every time",
            "explicit_step",
            "confirmation_style",
            "whatever the settings",
        ):
            assert phrase not in text, (name, phrase)


def _descriptions_of(schema: object) -> list[str]:
    """Every description a JSON schema holds, however deep."""
    if isinstance(schema, dict):
        found = [schema["description"]] if isinstance(schema.get("description"), str) else []
        return found + [text for value in schema.values() for text in _descriptions_of(value)]
    if isinstance(schema, list):
        return [text for item in schema for text in _descriptions_of(item)]
    return []


def test_no_tool_description_names_a_place():  # spec 6.8, ruling R4
    """A tool says what it gives back; the context says how Domo is heard here
    (``prompts.abilities_line``). Only the tools whose subject is the texts of the pages name
    them: they stay on the page (spec 9)."""
    for name, tool in TOOLS.items():
        if name.startswith("texts_") or name == "writing_choose":
            continue
        texts = [tool.definition.description, *_descriptions_of(tool.definition.parameters)]
        for text in texts:
            for word in ("card", "button", "page"):
                assert word not in text.lower(), (name, word)


def test_the_tools_of_a_key_never_say_the_form_opens_here():  # spec 6.8
    """A key's safe form opens only where the channel has a safe screen: the tools that ask for
    one say it is where the context says a key is typed, and that Domo says where, never that a
    form opens (on the API, a real model said "I open a secure form" and named no place)."""
    for name in ("key_add", "model_rekey", "key_admin"):
        text = " ".join(TOOLS[name].definition.description.split())
        assert "in a safe form, where the context says a key is typed (say where)" in text, name
        for phrase in ("open a safe form", "form opens", "opens where"):
            assert phrase not in text.lower(), (name, phrase)


def test_the_model_and_spending_tools_tell_how_to_judge_save_and_say_what_is_estimated():
    choose = " ".join(TOOLS["models_choose"].definition.description.split())
    for phrase in ("say no", "propose something else", "cheaper or local models", "less history"):
        assert phrase in choose, phrase
    assert "shorter answers" in choose
    show = " ".join(TOOLS["spending_show"].definition.description.split())
    assert "An estimate is not the real bill" in show and "say which" in show


def test_the_owners_own_tools_never_say_the_owner():  # F9, spec 5.7.3
    for name, tool in TOOLS.items():
        if tool.action_type in OWNER_ONLY_ACTIONS:
            assert "the owner" not in tool.definition.description, name


@pytest.mark.parametrize(
    "module",
    [
        "domo_core.web.app",
        "domo_core.channels.page.routes",
        "domo_core.channels.turns",
        "domo_core.conversation.starts",
        "domo_core.conversation.prepare",
        "domo_core.conversation.turn",
        "domo_core.conversation.service",
        "domo_core.channels.page.render",
        "domo_core.conversation.execution",
        "domo_core.conversation.cards",
        "domo_core.channels.page.cards",
        "domo_core.confirmations.service",
        "domo_core.confirmations.routes",
        "domo_core.tools.registry",
        "domo_core.tools.confirmation_tools",
        "domo_core.tools.policy_tools",
        "domo_core.tools.household_admin",
        "domo_core.tools.household_return",
        "domo_core.tools.notes",
        "domo_core.tools.memory_tools",
        "domo_core.tools.plan_tools",
        "domo_core.tools.model_key",
        "domo_core.tools.key_tools",
        "domo_core.tools.keys_house",
        "domo_core.llm.key_flow",
        "domo_core.tools.model_tools",
        "domo_core.tools.text_tools",
        "domo_core.tools.spending_tools",
        "domo_core.llm.real_spend",
        "domo_core.llm.refresh",
        "domo_core.llm.lessons",
        "domo_core.tools.successors",
        "domo_core.tools.chain",
        "domo_core.confirmations.chain",
        "domo_core.confirmations.steps",
        "domo_core.confirmations.describe",
        "domo_core.channels.page.key_routes",
        "domo_core.policy.key_forms",
        "domo_core.household.service",
        "domo_core.household.succession",
        "domo_core.household.routes",
        "domo_core.household.upkeep",
        "domo_core.notes.upkeep",
    ],
)
def test_each_entry_point_can_be_imported_first(module):
    """The registry holds the tools that manage requests, which run through the confirmation
    service, which runs calls through ``conversation.execution``: whichever module a process imports
    first, the imports must not meet a module that is only half loaded."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_registry_cannot_be_changed():
    with pytest.raises(TypeError):
        TOOLS["rm_rf"] = TOOLS["household_list"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            "owner",
            [
                "household_list",
                *OWN_TOOLS,
                *NOTE_TOOLS,
                *RECEIVED_NOTE_TOOLS,
                *REQUEST_TOOLS,
                *POLICY_TOOLS,
                *RIGHTS_TOOLS,
                *HOUSEHOLD_TOOLS,
                *MODEL_TOOLS,
                *HOUSE_KEY_TOOLS,
                *CHOICE_TOOLS,
                *OWN_TEXT_TOOLS,
                "texts_rewrite_language",
                "writing_choose",
                *SUCCESSOR_TOOLS,
                *DEVICE_TOOLS,
                *MEMORY_TOOLS,
                *PLAN_TOOLS,
                *NAME_TOOLS,
                *DELIVERY_TOOLS,
                *TRUST_TOOLS,
                *MADE_TOOLS,
                *OUTWARD_TOOLS,
                *CHAIN_TOOLS,
            ],
        ),
        (
            "adult",
            [
                "household_list",
                *OWN_TOOLS,
                *NOTE_TOOLS,
                *RECEIVED_NOTE_TOOLS,
                *REQUEST_TOOLS,
                # Inviting, and cancelling or giving again the link of their own invitation
                # (0.9.1).
                "household_invite",
                "household_cancel_invitation",
                "invitation_link_again",
                *MODEL_TOOLS,
                *MEMBER_CHOICE_TOOLS,
                *OWN_TEXT_TOOLS,
                "writing_choose",
                *DEVICE_TOOLS,
                *MEMORY_TOOLS,
                *PLAN_TOOLS,
                *NAME_TOOLS,
                *DELIVERY_TOOLS,
                *TRUST_TOOLS,
                *MADE_TOOLS,
                *ADDRESS_TOOLS,
                "account_ask",
                "account_remove",
                "accounts_look",
                *CHAIN_TOOLS,
            ],
        ),
        (
            "child",
            [
                "household_list",
                *OWN_TOOLS,
                *NOTE_TOOLS,
                *RECEIVED_NOTE_TOOLS,
                *REQUEST_TOOLS,
                *MODEL_TOOLS,
                *MEMBER_CHOICE_TOOLS,
                *OWN_TEXT_TOOLS,
                "writing_choose",
                *OWN_DEVICE_TOOLS,
                *MEMORY_TOOLS,
                *PLAN_TOOLS,
                *NAME_TOOLS,
                *DELIVERY_TOOLS,
                *TRUST_TOOLS,
                *MADE_TOOLS,
                *ADDRESS_TOOLS,
                "account_ask",
                "account_remove",
                "accounts_look",
                *CHAIN_TOOLS,
            ],
        ),
        (
            "guest",
            [
                *OWN_TOOLS,
                *RECEIVED_NOTE_TOOLS,
                *GUEST_KEY_TOOLS,
                "models_step_up",
                *OWN_TEXT_TOOLS,
                *OWN_DEVICE_TOOLS,
                *MEMORY_TOOLS,
                *PLAN_TOOLS,
                *NAME_TOOLS,
                *DELIVERY_TOOLS,
                *TRUST_TOOLS,
                *MADE_TOOLS,
                *ADDRESS_TOOLS,
                "accounts_look",
            ],
        ),
    ],
)
def test_each_role_sees_the_tools_its_actions_allow(role, expected):  # D1
    assert names(tools_for(role, TOOLS, DEFAULT_POLICY)) == expected


def test_the_tools_follow_the_policy_they_are_given():
    # Making his tools is Domo's own act: offered by his own rights (Task 9), none here.
    nobody_lists = PolicyTables(
        known=DEFAULT_POLICY.known,
        allowed={role: frozenset({"profile.rename"}) for role in ROLES},
        domo=frozenset(),
    )
    assert names(tools_for("owner", TOOLS, nobody_lists)) == ["profile_rename"]
    his_own = PolicyTables(known=DEFAULT_POLICY.known, allowed={}, domo=frozenset({"tool.build"}))
    assert names(tools_for("guest", TOOLS, his_own)) == ["tool_build"]
    guests_list = PolicyTables(
        known=DEFAULT_POLICY.known,
        allowed={"guest": frozenset({"household.list"})},
        domo=frozenset(),
    )
    assert names(tools_for("guest", TOOLS, guests_list)) == ["household_list"]
    assert tools_for("adult", TOOLS, guests_list) == []


def test_a_tool_whose_action_the_catalog_does_not_know_is_never_offered():
    unknown = PolicyTables(
        known=frozenset({"profile.rename"}),
        allowed={"owner": frozenset({"household.list", "profile.rename"})},
    )
    assert names(tools_for("owner", TOOLS, unknown)) == ["profile_rename"]


def a_right(action_type: str, *, person_id=None, role=None, access=None, ask=None) -> Right:
    return Right(
        uuid.uuid4(), person_id, role, action_type, access, None, None, ask, None, "a right"
    )


def test_a_person_is_shown_the_tools_of_their_own_rights():  # spec 5.9
    # A child (an adult invites by default since 0.9.1).
    sam, jo = uuid.uuid4(), uuid.uuid4()
    rights = RightsTable((a_right("household.invite", person_id=sam, access="allow"),))
    shown = names(tools_for("child", TOOLS, DEFAULT_POLICY, person_id=sam, rights=rights))
    assert shown == [
        "household_list",
        *OWN_TOOLS,
        *NOTE_TOOLS,
        *RECEIVED_NOTE_TOOLS,
        *REQUEST_TOOLS,
    ] + [
        "household_invite",
        # The right to invite gives the link again too (F1: ``catalog.RIGHTS_OF``).
        "invitation_link_again",
        *MODEL_TOOLS,
        *MEMBER_CHOICE_TOOLS,
        *OWN_TEXT_TOOLS,
        "writing_choose",
        *OWN_DEVICE_TOOLS,
        *MEMORY_TOOLS,
        *PLAN_TOOLS,
        *NAME_TOOLS,
        *DELIVERY_TOOLS,
        *TRUST_TOOLS,
        *MADE_TOOLS,
        *ADDRESS_TOOLS,
        "account_ask",
        "account_remove",
        "accounts_look",
        *CHAIN_TOOLS,
    ]
    assert "household_invite" not in names(
        tools_for("child", TOOLS, DEFAULT_POLICY, person_id=jo, rights=rights)
    )
    # Without a person, only the rights of the role count.
    assert "household_invite" not in names(tools_for("child", TOOLS, DEFAULT_POLICY, rights=rights))


def test_a_right_of_a_role_shows_or_hides_a_tool_to_the_whole_role():
    rights = RightsTable(
        (
            a_right("profile.rename", role="guest", access="deny"),
            a_right("household.remove", role="child", access="allow"),
        )
    )
    assert names(
        tools_for("guest", TOOLS, DEFAULT_POLICY, person_id=uuid.uuid4(), rights=rights)
    ) == [
        "household_leave",
        *RECEIVED_NOTE_TOOLS,
        *GUEST_KEY_TOOLS,
        "models_step_up",
        *OWN_TEXT_TOOLS,
        *OWN_DEVICE_TOOLS,
        *MEMORY_TOOLS,
        *PLAN_TOOLS,
        *NAME_TOOLS,
        *DELIVERY_TOOLS,
        *TRUST_TOOLS,
        *MADE_TOOLS,
        *ADDRESS_TOOLS,
        "accounts_look",
    ]
    assert "household_remove" in names(
        tools_for("child", TOOLS, DEFAULT_POLICY, person_id=uuid.uuid4(), rights=rights)
    )
    assert "household_remove" not in names(
        tools_for("adult", TOOLS, DEFAULT_POLICY, person_id=uuid.uuid4(), rights=rights)
    )


def test_a_tool_shown_with_a_rule_is_still_shown():
    rights = RightsTable((a_right("note.leave", role="adult", ask="explicit_yes"),))
    assert "note_leave" in names(
        tools_for("adult", TOOLS, DEFAULT_POLICY, person_id=uuid.uuid4(), rights=rights)
    )


def test_no_right_shows_the_owners_tools_to_anyone_else():
    adult = uuid.uuid4()
    rights = RightsTable(
        (
            a_right("policy.update", role="adult", access="allow"),
            a_right("policy.update", person_id=adult, access="allow"),
        )
    )
    shown = names(tools_for("adult", TOOLS, DEFAULT_POLICY, person_id=adult, rights=rights))
    assert "policy_update" not in shown
    # Not even when the tables say so: changing the rules is the owner's.
    everyone = PolicyTables(
        known=DEFAULT_POLICY.known,
        allowed={role: DEFAULT_POLICY.allowed["owner"] for role in ROLES},
    )
    assert "policy_update" not in names(tools_for("adult", TOOLS, everyone, person_id=adult))
    assert "policy_update" in names(tools_for("owner", TOOLS, everyone, person_id=adult))


def test_someone_elses_texts_are_shown_only_to_whoever_has_the_right():  # spec 4.7, socle 8
    for role in ROLES:
        assert "texts_for_someone" not in names(tools_for(role, TOOLS, DEFAULT_POLICY))
    alex, owner = uuid.uuid4(), uuid.uuid4()
    rights = RightsTable((a_right("texts.for_someone", person_id=alex, access="allow"),))
    assert "texts_for_someone" in names(
        tools_for("adult", TOOLS, DEFAULT_POLICY, person_id=alex, rights=rights)
    )
    assert "texts_for_someone" not in names(
        tools_for("owner", TOOLS, DEFAULT_POLICY, person_id=owner, rights=rights)
    )


def test_invalid_arguments_carry_a_code_and_maybe_a_detail():
    error = InvalidArguments("too_long", "the name has more than 60 characters")
    assert (error.code, error.detail) == ("too_long", "the name has more than 60 characters")
    assert InvalidArguments("ambiguous").detail is None


# --- household_list -----------------------------------------------------------------------------


async def test_household_list_is_a_read_of_the_household():
    adult = someone()
    action = await TOOLS["household_list"].describe({}, Actor(adult.id, "adult"), context(adult))
    assert action == DescribedAction("core", "household.list", None, "read", "household")


async def test_household_list_takes_no_arguments():
    adult = someone()
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["household_list"].describe(
            {"all": True}, Actor(adult.id, "adult"), context(adult)
        )
    assert raised.value.code == "invalid"


async def test_household_list_tells_who_is_here_and_who_is_invited(session_factory):
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        writer.add(owner)
        await writer.flush()
        for person in (
            Person(display_name="Alex", role="adult"),
            Person(display_name="Robin", role="guest", expires_at=NOW + timedelta(days=2)),
            # Not in the household any more: removed, or a stay that is over.
            Person(display_name="Gone", role="adult", disabled_at=NOW - timedelta(days=1)),
            Person(display_name="Left", role="guest", expires_at=NOW - timedelta(seconds=1)),
        ):
            writer.add(person)
            await writer.flush()  # one at a time: the list is in the order people arrived
        invitation = {"created_by": owner.id, "expires_at": NOW + timedelta(days=7)}
        writer.add_all(
            [
                Invitation(token_hash="a", display_name="Sam", role="child", **invitation),
                # Not waiting any more: answered, cancelled, or too old.
                Invitation(
                    token_hash="b",
                    display_name="Accepted",
                    role="adult",
                    accepted_at=NOW,
                    **invitation,
                ),
                Invitation(
                    token_hash="c",
                    display_name="Cancelled",
                    role="adult",
                    cancelled_at=NOW,
                    **invitation,
                ),
                Invitation(
                    token_hash="d",
                    display_name="Late",
                    role="adult",
                    created_by=owner.id,
                    expires_at=NOW,
                ),
            ]
        )
        await writer.commit()

    outcome = await TOOLS["household_list"].execute({}, context(owner, session_factory))
    assert outcome == ToolOutcome(
        status="done",
        model_result={
            "people": [
                # The owner may remove people, so is told when each last came (nobody has a
                # session in this test).
                {"name": "Owner", "role": "owner", "status": "active", "last_seen": None},
                {"name": "Alex", "role": "adult", "status": "active", "last_seen": None},
                {
                    "name": "Robin",
                    "role": "guest",
                    "status": "guest_until:2030-01-03",
                    "last_seen": None,
                },
                {"name": "Sam", "role": "child", "status": "invited"},
            ]
        },
        card={"key": "action.card.done.household.list", "params": {}},
    )


# --- profile_rename -----------------------------------------------------------------------------


async def test_renaming_is_about_the_person_who_speaks(session_factory):
    adult = someone()
    action = await TOOLS["profile_rename"].describe(
        {"name": "Alex B"}, Actor(adult.id, "adult"), context(adult, session_factory)
    )
    assert action == DescribedAction(
        "core", "profile.rename", f"person:{adult.id}", "self_profile", "household"
    )
    # The new name is an argument of the call, never part of what the audit log gets.
    assert "Alex B" not in str(action.as_json())


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({}, "invalid"),
        ({"name": None}, "invalid"),
        ({"name": 3}, "invalid"),
        ({"name": ""}, "invalid"),
        ({"name": "   "}, "invalid"),
        ({"name": "x" * 61}, "too_long"),
        ({"name": "a\x00b"}, "invalid"),
        ({"name": "a\nb"}, "invalid"),
        ({"name": "a\u2028b"}, "invalid"),
        ({"name": "a\ud800b"}, "invalid"),
        # Bidirectional controls: they can make a name shown to others look like another.
        ({"name": "Alex\u202eB"}, "invalid"),
        ({"name": "\u202aAlex"}, "invalid"),
        ({"name": "Alex\u2066B\u2069"}, "invalid"),
        ({"name": "Alex\u200f"}, "invalid"),
        ({"name": "\u200eAlex"}, "invalid"),
        ({"name": "Alex\u061c"}, "invalid"),
        ({"name": "Alex", "role": "owner"}, "invalid"),
    ],
)
async def test_a_name_must_be_one_to_sixty_plain_characters(arguments, code):
    adult = someone()
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["profile_rename"].describe(arguments, Actor(adult.id, "adult"), context(adult))
    assert raised.value.code == code
    with pytest.raises(InvalidArguments):
        await TOOLS["profile_rename"].execute(arguments, context(adult))


@pytest.mark.parametrize(
    "name",
    [
        "x" * 60,
        "Élise",
        "<b>Al</b>",
        "Ana-María 🌻",
        # Emoji joined by a zero-width joiner (another format character) stay allowed.
        "Sam 👨\u200d👩\u200d👧",
        "Alex 🏳\ufe0f\u200d🌈",
    ],
)
async def test_names_of_up_to_sixty_characters_are_fine(name, session_factory):
    adult = someone()
    await TOOLS["profile_rename"].describe(
        {"name": name}, Actor(adult.id, "adult"), context(adult, session_factory)
    )


async def test_renaming_changes_the_name_of_the_person_who_speaks_only(session_factory):
    async with session_factory() as writer:
        alex, sam = (
            Person(display_name="Alex", role="adult"),
            Person(display_name="Sam", role="child"),
        )
        writer.add(alex)
        await writer.flush()
        writer.add(sam)
        await writer.commit()

    outcome = await TOOLS["profile_rename"].execute(
        {"name": "  Alex B  "}, context(alex, session_factory)
    )
    assert outcome == ToolOutcome(
        status="done",
        model_result={"name": "Alex B"},
        card={"key": "action.card.done.profile.rename", "params": {"name": "Alex B"}},
    )
    async with session_factory() as reader:
        result = await reader.execute(select(Person.display_name).order_by(Person.created_at))
        assert result.scalars().all() == ["Alex B", "Sam"]


def test_domo_s_own_tools_say_what_holds_them():  # Task 12, fix round 1 (F1, F2, M2)
    """What the code holds that Domo needs to know as he calls them: who a tool is closed to
    cannot have it changed, removed or a trigger cancelled (R11); a build slowed down after
    refused rounds (not_before); a version that changes an operation's risks takes back its
    agreements (last_build); a stop's yes_for call is the ask itself (R20); a note of his own
    says so (2026-11-21); he can say who told him something."""

    def said(name: str) -> str:
        return " ".join(TOOLS[name].definition.description.split())

    for name in ("tool_change", "tool_remove"):
        assert "Someone the tool is closed to cannot have it" in said(name), name
    cancel = TOOLS["tool_launch_when"].definition.parameters["properties"]["cancel"]
    assert "Someone the tool is closed to cannot cancel it." in cancel["description"]
    assert "not_before" in said("tool_change") and "not_before" in said("tool_look")
    assert "takes back its agreements" in said("tool_change") and "last_build" in said("tool_look")
    run = said("program_run")
    assert "call this again at once with yes_for alone" in run and "that call is the ask" in run
    assert "your yes_for runs it at once" in run and "once they agree" not in run
    assert '"from": "me"' in said("note_leave")
    assert "you can say who told you" in said("memory_keep")
