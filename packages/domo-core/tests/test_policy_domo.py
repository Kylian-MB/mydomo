"""Every type has its four risks, programs have types of their own, and Domo is a subject of
rights (the owner, 2026-11-18): for what he does on his own, for the house, he has every type but
the owner's own and, by default, the socle's (secrets, spending limits, who may do what, what is
nobody's: the controller's ruling), and judges himself. The owner may open one of those to him or
take a right from him, and his own rules on what to ask apply to him; a right given to the adults
never reaches him.

The resolution is pure; the facts and the audit are tried on the shared rolled-back ``session``."""

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import domo_core
import pytest
from chat_support import last_audit
from domo_core.confirmations.judge import RightsSay, build_case
from domo_core.policy import barrier
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, Refuse, record_decision
from domo_core.policy.catalog import (
    ACTION_FACTS,
    CODE_RISKS,
    DEFAULT_POLICY,
    DOMO_NOT_BY_DEFAULT,
    KNOWN_ACTIONS,
    NO_RISK,
    NOT_ON_HIS_OWN,
    OWNER_ONLY_ACTIONS,
    PERMISSION_TYPES,
    PROGRAM_ACTIONS,
    PROGRAM_BEGIN,
    PROGRAM_LATER,
    PROGRAM_READ,
    PROGRAM_RUN,
    PROGRAM_WAKE,
    PROGRAM_WRITE,
    OpenKind,
    PolicyTables,
    Risks,
    is_silent,
    needs_the_judge,
    on_his_own_tables,
    risks_of,
)
from domo_core.policy.facts import (
    decision_facts,
    domo_judge_history,
    facts_of,
    made_up_facts,
)
from domo_core.policy.open_catalog import add_kind, load_policy
from domo_core.policy.rights import NO_RIGHTS, Right, RightsTable
from domo_core.policy.roles import (
    DOMO_SUBJECT_ID,
    Actor,
    actor_label,
    domo_actor,
)

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
NOTE = DescribedAction(
    "core",
    "note.leave",
    "person:x",
    "message",
    "household",
    {"recipient": "person:x", "recipient_role": "adult", "length": 3},
)
PROGRAM_TYPES = {
    PROGRAM_BEGIN,
    PROGRAM_READ,
    PROGRAM_WRITE,
    PROGRAM_LATER,
    PROGRAM_WAKE,
    PROGRAM_RUN,
}


def decide(actor: Actor, action: DescribedAction, rights: RightsTable = NO_RIGHTS, policy=None):
    """``barrier.decide`` with the facts of a moment made up from the action."""
    return barrier.decide(
        actor,
        action,
        DEFAULT_POLICY if policy is None else policy,
        rights,
        facts_of(actor, action, name="Someone", local_now=None),
    )


def right(
    action_type: str,
    *,
    access=None,
    person_id=None,
    role=None,
    ask=None,
    for_domo: bool = False,
) -> Right:
    return Right(
        uuid4(),
        person_id,
        role,
        action_type,
        access,
        None,
        None,
        ask,
        None,
        "a right",
        for_domo=for_domo,
    )


# --- the risks -----------------------------------------------------------------------------------


def test_every_known_type_has_its_risks():
    assert set(CODE_RISKS) == KNOWN_ACTIONS


def test_the_risks_of_the_code_are_these_and_no_other():
    irreversible = {
        "household.transfer_ownership",
        "key.remove",
        "memory.forget",
        "note.withdraw",
        "successors.note_death",
    }
    for action_type, risks in CODE_RISKS.items():
        expected = Risks(
            costs=action_type == "models.step_up", irreversible=action_type in irreversible
        )
        assert risks == expected, action_type
    assert Risks() == NO_RISK
    assert Risks(costs=True) | Risks(outward=True) == Risks(costs=True, outward=True)
    assert NO_RISK | NO_RISK == NO_RISK


def test_the_risks_of_a_kind_are_its_four_booleans():
    kind = OpenKind(
        "made.split",
        costs=True,
        irreversible=False,
        physical=True,
        outward=False,
        facts=MappingProxyType({}),
        default_roles=frozenset({"owner"}),
        description="Splits a bill.",
    )
    policy = PolicyTables(
        DEFAULT_POLICY.known | {"made.split"},
        DEFAULT_POLICY.allowed,
        kinds=MappingProxyType({"made.split": kind}),
    )
    assert risks_of("made.split", policy) == Risks(costs=True, physical=True)
    assert risks_of("key.remove", policy) == Risks(irreversible=True)


def test_the_risks_of_a_type_nobody_knows_are_never_guessed():
    for unknown in ("made.unknown", "household.delete_everything"):
        with pytest.raises(KeyError, match="not an action type the catalog knows"):
            risks_of(unknown, DEFAULT_POLICY)


# --- Domo's rights -----------------------------------------------------------------------------

# What Domo does not have by default (the controller's ruling of 2026-11-18): secrets, spending
# limits, who may do what, and what is nobody's.
SECRETS = {"model.rekey", "key.add", "key.remove", "key.share", "key.admin", "keys.house"}
PERMISSIONS = {
    "household.invite",
    "household.invite_again",
    "household.cancel_invitation",
    "household.change_role",
    "household.remove",
    "household.leave",
    "household.bring_back",
    "household.transfer_ownership",
    "device.entrust",
    "device.remove",
    "key.share",
    "spending.limit",
    "keys.house",
    "successors.set",
    "successors.note_death",
    "successors.undo_death",
    "rights.propose",
    "rights.apply",
    "policy.update",
    "models.set_level",
}


def test_domo_starts_with_every_right_but_the_owners_own_and_the_socle_s():
    assert DEFAULT_POLICY.domo == KNOWN_ACTIONS - OWNER_ONLY_ACTIONS - DOMO_NOT_BY_DEFAULT
    assert SECRETS | {"spending.limit"} | PERMISSIONS | {"texts.for_someone"} == DOMO_NOT_BY_DEFAULT
    assert PERMISSION_TYPES == PERMISSIONS
    assert "models.step_up" in DEFAULT_POLICY.domo and "policy.update" not in DEFAULT_POLICY.domo


def _types_described_as_permissions() -> set[str]:
    """The action types the core's tools describe with the effect ``permissions``, read from their
    source: the type is a literal, ``self.action_type`` (a class attribute) or a module constant."""
    found: set[str] = set()
    for path in sorted((Path(domo_core.__file__).parent / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        # Each class with its own ``action_type``, and each function outside a class.
        scopes: list[ast.AST] = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        scopes += [n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        for cls in scopes:
            own = next(
                (
                    node.value.value
                    for node in getattr(cls, "body", [])
                    if isinstance(node, ast.Assign | ast.AnnAssign)
                    and isinstance(node.value, ast.Constant)
                    and any(
                        isinstance(target, ast.Name) and target.id == "action_type"
                        for target in (
                            node.targets if isinstance(node, ast.Assign) else [node.target]
                        )
                    )
                ),
                None,
            )
            for call in ast.walk(cls):
                if not isinstance(call, ast.Call):
                    continue
                keywords = {k.arg: k.value for k in call.keywords}
                effect, action_type = keywords.get("effect"), keywords.get("action_type")
                if not (isinstance(effect, ast.Constant) and effect.value == "permissions"):
                    continue
                match action_type:
                    case ast.Constant(value=str(value)):
                        found.add(value)
                    case ast.Attribute(attr="action_type") if own is not None:
                        found.add(own)
                    case ast.Name(id=name) if name in constants:
                        found.add(constants[name])
                    case _:
                        raise AssertionError(f"{path.name}: a type the test cannot read")
    return found


def test_the_types_of_permissions_are_those_the_tools_describe_so():
    assert _types_described_as_permissions() == PERMISSION_TYPES


def test_what_domo_lacks_by_default_is_refused_to_him_until_the_owner_opens_it():
    domo = domo_actor()
    for action_type in DOMO_NOT_BY_DEFAULT:
        facts = {name: None for name in ACTION_FACTS[action_type]}
        action = DescribedAction("core", action_type, None, "permissions", "household", facts)
        assert decide(domo, action) == Refuse("not_allowed"), action_type
    add_key = DescribedAction("core", "key.add", None, "secrets", "household")
    opened = RightsTable((right("key.add", access="allow", for_domo=True),))
    assert isinstance(decide(domo, add_key, opened), Allow)
    # The owner's own stay his, whatever a right for Domo says.
    update = DescribedAction("core", "policy.update", None, "permissions", "household")
    given = RightsTable((right("policy.update", access="allow", for_domo=True),))
    assert decide(domo, update, given) == Refuse("not_allowed")


def test_domo_is_a_subject_of_his_own():
    domo = domo_actor()
    assert (domo.person_id, domo.role, domo.trusted, domo.subject) == (
        DOMO_SUBJECT_ID,
        "adult",
        True,
        "domo",
    )
    assert actor_label(domo) == "domo"
    dana = Actor(uuid4(), "adult")
    assert dana.subject == "person"
    assert actor_label(dana) == f"person:{dana.person_id}"


def test_domo_may_do_what_no_role_has_but_never_the_owners_own():
    domo = domo_actor()
    # The adults lack some of his (``texts.language_version``, ``policy.show``): Domo does not.
    assert not DEFAULT_POLICY.allowed["adult"] >= DEFAULT_POLICY.domo
    for action_type in DEFAULT_POLICY.domo:
        assert NO_RIGHTS.resolve(domo, action_type, DEFAULT_POLICY).allowed, action_type
    for action_type in KNOWN_ACTIONS - DEFAULT_POLICY.domo:
        assert not NO_RIGHTS.resolve(domo, action_type, DEFAULT_POLICY).allowed, action_type
    for action_type in OWNER_ONLY_ACTIONS:
        assert not NO_RIGHTS.resolve(domo, action_type, DEFAULT_POLICY).allowed
        action = DescribedAction("core", action_type, None, "permissions", "household")
        assert decide(domo, action) == Refuse("not_allowed"), action_type


def test_an_owners_deny_for_domo_refuses_domo_and_nobody_else():
    rights = RightsTable((right("note.leave", access="deny", for_domo=True),))
    assert decide(domo_actor(), NOTE, rights) == Refuse("not_allowed")
    dana = Actor(uuid4(), "adult")
    assert isinstance(decide(dana, NOTE, rights), Allow)
    # Not a rule for everyone either: it asks nothing of anyone else.
    assert rights.resolve(dana, "note.leave", DEFAULT_POLICY).ask is None


def test_a_right_of_the_adults_does_not_reach_domo():
    rights = RightsTable((right("note.leave", access="deny", role="adult"),))
    assert isinstance(decide(domo_actor(), NOTE, rights), Allow)
    assert decide(Actor(uuid4(), "adult"), NOTE, rights) == Refuse("not_allowed")


def test_the_owners_rules_on_asking_apply_to_domo():
    everyone = RightsTable((right("note.leave", ask="explicit_yes"),))
    assert decide(domo_actor(), NOTE, everyone) == Allow("explicit_yes")
    his_own = RightsTable((right("note.leave", ask="proof", for_domo=True),))
    assert decide(domo_actor(), NOTE, his_own) == Allow("proof")
    # The adults' rule is theirs.
    adults = RightsTable((right("note.leave", ask="yes", role="adult"),))
    assert decide(domo_actor(), NOTE, adults) == Allow()


def test_a_right_for_a_person_with_domo_s_id_is_not_his():
    rights = RightsTable((right("note.leave", access="deny", person_id=DOMO_SUBJECT_ID),))
    assert isinstance(decide(domo_actor(), NOTE, rights), Allow)


async def test_every_open_kind_is_domo_s(session):
    kind = OpenKind(
        "made.split",
        costs=False,
        irreversible=False,
        physical=False,
        outward=False,
        facts=MappingProxyType({}),
        default_roles=frozenset({"owner"}),
        description="Splits a bill.",
    )
    await add_kind(session, kind, now=NOW)
    policy = await load_policy(session)
    assert policy.domo == DEFAULT_POLICY.domo | {"made.split"}
    split = DescribedAction("core", "made.split", None, "read", "household")
    assert isinstance(decide(domo_actor(), split, policy=policy), Allow)


# --- the program types ---------------------------------------------------------------------------


def test_program_types_are_everyones_silent_and_unjudged():
    assert PROGRAM_TYPES <= KNOWN_ACTIONS
    guest = Actor(uuid4(), "guest")
    for action_type in PROGRAM_TYPES:
        facts = {name: None for name in ACTION_FACTS[action_type]}
        action = DescribedAction("program", action_type, None, "program", "household", facts)
        assert isinstance(decide(guest, action), Allow), action_type
        assert isinstance(decide(domo_actor(), action), Allow), action_type
        assert is_silent(action_type)
        assert not needs_the_judge(action_type)
        assert action_type not in OWNER_ONLY_ACTIONS
    assert dict(ACTION_FACTS[PROGRAM_BEGIN]) == {
        "tool": "text",
        "operation": "text",
        "amount_cents": "number",
        "costs": "boolean",
        "irreversible": "boolean",
        "physical": "boolean",
        "outward": "boolean",
    }


def test_no_program_action_speaks_to_a_person_or_touches_the_socle():
    assert {"tool.launch_when"} == PROGRAM_ACTIONS
    assert PROGRAM_ACTIONS <= KNOWN_ACTIONS
    assert not PROGRAM_ACTIONS & (
        OWNER_ONLY_ACTIONS
        | {
            t
            for t in KNOWN_ACTIONS
            if t.split(".")[0]
            in {
                "household",
                "note",
                "key",
                "keys",
                "spending",
                "models",
                "model",
                "successors",
                "policy",
                "rights",
                "trust",
                "confirmation",
                "texts",
                "device",
                "memory",
                "profile",
                "people",
                "chain",
            }
        }
    )


# --- the facts and the audit ---------------------------------------------------------------------


async def test_facts_of_domo_read_back(session):
    domo = domo_actor()
    # Nobody has his id: a read of a person would fail.
    facts = await decision_facts(session, domo, NOTE, NOW, None)
    assert facts["actor"] == {"id": "domo", "role": "adult", "name": "Domo"}
    assert facts["history"] == {"day": 0, "week": 0, "month": 0}


async def test_domo_s_history_is_what_he_was_allowed_of_that_type(session, add_person):
    domo = domo_actor()
    dana = await add_person("Dana", "adult")
    tool = "note_leave"
    await record_decision(session, domo, NOTE, Allow(), tool_name=tool)
    await record_decision(session, domo, NOTE, Allow(), tool_name=tool)
    await record_decision(session, domo, NOTE, Refuse("not_allowed"), tool_name=tool)
    other = DescribedAction("core", "household.list", None, "read", "household")
    await record_decision(session, domo, other, Allow(), tool_name="household_list")
    # Dana's own decisions are hers.
    await record_decision(session, Actor(dana.id, "adult"), NOTE, Allow(), tool_name=tool)
    now = datetime.now(UTC)
    facts = await decision_facts(session, domo, NOTE, now, None)
    assert facts["history"] == {"day": 2, "week": 2, "month": 2}
    more = await decision_facts(session, domo, NOTE, now, None, unrecorded=["note.leave"])
    assert more["history"] == {"day": 3, "week": 3, "month": 3}


async def test_a_decision_for_domo_is_audited_as_domo(session):
    await record_decision(session, domo_actor(), NOTE, Allow(), tool_name="note_leave")
    entry = await last_audit(session, "policy.decision")
    assert entry.actor == "domo"
    assert entry.details["decision"] == "allow"


# --- Task 9: Domo's own history, judge and examples --------------------------------------------


async def test_a_request_of_domo_counts_once_however_many_yeses_it_went_through(session):
    domo = domo_actor()
    launch = DescribedAction("core", "made.order", None, "self_profile", "household")
    with_yes = DescribedAction(
        "core",
        "made.order",
        None,
        "self_profile",
        "household",
        MappingProxyType({"operation": "order", "amount_cents": 3250, "yes": True}),
    )
    # One launch that stopped twice, and the two calls that carried his yes.
    await record_decision(session, domo, launch, Allow(), tool_name="t_order")
    await record_decision(session, domo, with_yes, Allow(), tool_name="t_order")
    await record_decision(session, domo, with_yes, Allow(), tool_name="t_order")
    now = datetime.now(UTC)
    facts = await decision_facts(session, domo, launch, now, None)
    assert facts["history"] == {"day": 1, "week": 1, "month": 1}
    judged = await domo_judge_history(session, "made.order", now)
    assert (judged.day, judged.ever, judged.refused_or_dropped) == (1, 1, 0)
    assert judged.last_at is not None


async def test_the_judge_s_case_of_domo_on_his_own_is_his(session):
    case = await build_case(
        session,
        actor=domo_actor(),
        action=NOTE,
        says="Leave a note.",
        rights=RightsSay(allowed=True, needs=None, question=None),
        conversation_id=uuid4(),
        now=NOW,
        zone=None,
    )
    # His name, nobody's words nor requests, his own history: never the owner's.
    assert case.person == {"name": "Domo", "role": "adult"}
    assert (case.words, case.pending, case.asked_them, case.speaker) == ((), (), None, None)
    assert case.history.ever == 0


async def test_an_example_about_domo_names_him(session):
    facts = await made_up_facts(session, domo_actor(), NOTE, NOW, None)
    assert facts["actor"] == {"id": "domo", "role": "adult", "name": "Domo"}
    assert facts["spent"] == {"day": 0, "week": 0, "month": 0}


def test_what_is_the_persons_own_is_not_domo_s_on_his_own():
    alone = on_his_own_tables(DEFAULT_POLICY)
    for action_type in NOT_ON_HIS_OWN:
        assert action_type in KNOWN_ACTIONS, action_type
        assert action_type in DEFAULT_POLICY.domo or action_type in DOMO_NOT_BY_DEFAULT
        assert not NO_RIGHTS.resolve(domo_actor(), action_type, alone).allowed, action_type
    # A right the owner gives him opens one, as any right of his.
    opened = RightsTable((right("memory.search", access="allow", for_domo=True),))
    assert opened.resolve(domo_actor(), "memory.search", alone).allowed
    # What is the house's or his own stays his.
    # A note he leaves on his own is his own (the owner, 2026-11-21), never in someone's name.
    assert "note.leave" not in NOT_ON_HIS_OWN
    for action_type in ("agreement.keep", "tool.build", "household.list"):
        assert NO_RIGHTS.resolve(domo_actor(), action_type, alone).allowed, action_type
