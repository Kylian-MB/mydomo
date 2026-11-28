"""The open catalog (spec 5.3): the kinds of action Domo opens for his tools live in the
database, join the catalog of the code once per turn (``load_policy``) and are judged like the
rest: by the barrier, the owner's rights, the limits, the writer of rights and the cards.

The service is tried on the shared rolled-back ``session``; the rights on a kind go through a turn
and the page that confirms them, as the owner would."""

import dataclasses
import json
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

import pytest
from chat_support import confirmation_cards, last_audit
from domo_common.i18n import t
from domo_core.confirmations.describe import describe_example, describe_type
from domo_core.madetools.models import ActionKind
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, NeedsApproval, Refuse, decide
from domo_core.policy.catalog import (
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    MADE_FACTS,
    OpenKind,
    PolicyTables,
    declared_facts,
    is_silent,
    needs_the_judge,
)
from domo_core.policy.limits import InvalidLimit, check_limit
from domo_core.policy.open_catalog import KIND_ADDED, KindRefused, add_kind, load_policy
from domo_core.policy.rights import NO_RIGHTS, Right, RightsTable, load_rights
from domo_core.policy.rights_writer import (
    Draft,
    Example,
    check_draft,
    writer_context_json,
)
from domo_core.policy.roles import Actor, Role
from domo_core.texts.view import CatalogTexts
from test_rights_tools import apply, drafts, live, plain, propose, rights_of
from test_rights_writer import ALEX, SAM, change, context

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
SPLIT = "made.split"
DESCRIPTION = "Splits a bill between the people who shared it."


def kind(
    action_type: str = SPLIT,
    *,
    roles: frozenset[Role] = frozenset({"owner", "adult"}),
    facts: dict[str, str] | None = None,
    **risks: bool,
) -> OpenKind:
    return OpenKind(
        action_type=action_type,
        costs=risks.get("costs", False),
        irreversible=risks.get("irreversible", False),
        physical=risks.get("physical", False),
        outward=risks.get("outward", False),
        facts=MappingProxyType(dict(facts or {})),  # type: ignore[arg-type]
        default_roles=roles,
        description=DESCRIPTION,
    )


def split_action() -> DescribedAction:
    return DescribedAction("core", SPLIT, None, "read", "household")


def policy_with(*kinds: OpenKind) -> PolicyTables:
    """The code's catalog with ``kinds`` laid over it, as ``load_policy`` does."""
    allowed = {
        role: DEFAULT_POLICY.allowed[role]
        | {k.action_type for k in kinds if role in k.default_roles}
        for role in DEFAULT_POLICY.allowed
    }
    return PolicyTables(
        DEFAULT_POLICY.known | {k.action_type for k in kinds},
        MappingProxyType(allowed),
        kinds=MappingProxyType({k.action_type: k for k in kinds}),
    )


# --- the catalog of a turn -----------------------------------------------------------------------


async def test_with_no_kind_the_policy_of_a_turn_is_the_code_s(session):
    assert await load_policy(session) is DEFAULT_POLICY
    base = PolicyTables(frozenset({"household.list"}), MappingProxyType({}))
    assert await load_policy(session, base) is base


async def test_a_kind_is_known_and_allowed_to_its_default_roles(session):
    await add_kind(session, kind(), now=NOW)
    policy = await load_policy(session)
    assert SPLIT in policy.known and policy.known >= KNOWN_ACTIONS
    assert policy.kinds[SPLIT] == kind()
    assert SPLIT in policy.allowed["owner"] and SPLIT in policy.allowed["adult"]
    assert SPLIT not in policy.allowed["child"] and SPLIT not in policy.allowed["guest"]
    # The code's own tables are untouched.
    assert SPLIT not in DEFAULT_POLICY.known and DEFAULT_POLICY.kinds == {}
    for role in DEFAULT_POLICY.allowed:
        assert policy.allowed[role] - {SPLIT} == DEFAULT_POLICY.allowed[role]
    # Laid over a policy that has it already, it changes nothing.
    assert await load_policy(session, policy) is policy


async def test_the_barrier_judges_a_kind_like_any_type(session):
    await add_kind(session, kind(), now=NOW)
    policy = await load_policy(session)
    adult, child = Actor(uuid4(), "adult"), Actor(uuid4(), "child", child=True)
    assert isinstance(decide(adult, split_action(), policy), Allow)
    assert decide(child, split_action(), policy) == Refuse("not_allowed")
    # Unknown without the open catalog, as any type the code does not know.
    assert decide(adult, split_action(), DEFAULT_POLICY) == Refuse("not_allowed")


def test_an_owner_s_right_on_a_kind_is_read_as_any_right():
    policy = policy_with(kind())
    alex, dana = Actor(uuid4(), "adult"), Actor(uuid4(), "adult")
    denied = Right(uuid4(), alex.person_id, None, SPLIT, "deny", None, None, None, None, "No.")
    rights = RightsTable((denied,))
    assert decide(alex, split_action(), policy, rights) == Refuse("not_allowed")
    assert isinstance(decide(dana, split_action(), policy, rights), Allow)
    # And one that opens it to a child: reserved to adults (ruling 16), an adult still agrees
    # first, unless the owner's right says the child agrees for themselves.
    sam = Actor(uuid4(), "child", child=True)
    opened = Right(uuid4(), sam.person_id, None, SPLIT, "allow", None, None, None, None, "Yes.")
    asked = decide(sam, split_action(), policy, RightsTable((opened,)))
    assert isinstance(asked, NeedsApproval) and asked.approver == "adult"
    alone = Right(uuid4(), sam.person_id, None, SPLIT, "allow", "self", None, None, None, "Yes.")
    assert isinstance(decide(sam, split_action(), policy, RightsTable((alone,))), Allow)
    assert decide(sam, split_action(), policy, NO_RIGHTS) == Refuse("not_allowed")


# --- opening a kind ------------------------------------------------------------------------------


async def test_opening_a_kind_writes_the_audit_entry_without_its_words(session):
    await add_kind(session, kind(), now=NOW)
    row = await session.get(ActionKind, SPLIT)
    assert row is not None
    assert (row.description, row.default_roles, row.facts) == (DESCRIPTION, ["owner", "adult"], {})
    assert row.created_at == NOW
    audited = await last_audit(session, KIND_ADDED)
    assert audited.actor == "system"
    assert audited.details == {
        "action_type": SPLIT,
        "roles": ["owner", "adult"],
        "description_length": len(DESCRIPTION),
    }
    assert DESCRIPTION not in json.dumps(audited.details)


@pytest.mark.parametrize(
    ("opened", "why"),
    [
        (kind("household.invite"), "this is the name of an action type of the code"),
        (kind("split"), "the action type of a tool starts with made."),
    ],
)
async def test_a_kind_that_may_not_be_opened_is_refused(session, opened, why):
    with pytest.raises(KindRefused, match=why.replace(".", r"\.")):
        await add_kind(session, opened, now=NOW)
    assert await session.get(ActionKind, opened.action_type) is None


async def test_a_kind_carries_the_risks_of_its_tools_operations(session):
    # A tool may cost, be irreversible, act physically or go out: its kind says so (the union of
    # its operations' risks), and each request of its program is judged as it runs.
    await add_kind(session, kind(costs=True, irreversible=True, outward=True), now=NOW)
    row = await session.get(ActionKind, SPLIT)
    assert row is not None
    assert (row.costs, row.irreversible, row.physical, row.outward) == (True, True, False, True)
    policy = await load_policy(session)
    assert policy.kinds[SPLIT].costs and not policy.kinds[SPLIT].physical


@pytest.mark.parametrize(
    "roles",
    [
        # A new tool's kind: every role, guests too (a tool is Domo's).
        {"owner", "adult", "child", "guest"},
        {"owner", "adult", "child"},
        {"guest"},
        set(),
    ],
)
async def test_a_kind_is_opened_to_its_roles_whoever_asked_for_it(session, roles):
    # No role bounds it: a tool is not the tool of the one whose turn asked for it.
    await add_kind(session, kind(roles=frozenset(roles)), now=NOW)
    policy = await load_policy(session)
    for role in ("owner", "adult", "child", "guest"):
        assert (SPLIT in policy.allowed[role]) == (role in roles), role
    assert SPLIT in policy.domo


async def test_a_row_that_does_not_read_back_is_left_out(session):
    session.add(ActionKind(action_type="made.odd", default_roles=["captain"], description="odd"))
    session.add(ActionKind(action_type="made.odder", facts={"x": "colour"}, description="odd"))
    session.add(ActionKind(action_type="note.leave", default_roles=["guest"], description="odd"))
    await session.flush()
    assert await load_policy(session) is DEFAULT_POLICY


# --- never shown, never judged -------------------------------------------------------------------


def test_a_tool_s_action_is_silent_and_not_judged_by_its_prefix():
    for action_type in ("made.x", SPLIT):
        assert is_silent(action_type)
        assert not needs_the_judge(action_type)
    assert not any(t.startswith("made.") for t in KNOWN_ACTIONS)


# --- the limits ----------------------------------------------------------------------------------


def test_a_limit_on_a_kind_reads_its_facts_and_those_of_every_tool():
    # Every kind carries the facts of a launch's yes (``MADE_FACTS``, Task 8): a limit may read
    # them, guarded, since a launch without a yes has none.
    without = MappingProxyType({SPLIT: kind()})
    check_limit('"allow"', SPLIT, time_zone_set=False, kinds=without)
    over = "action.facts.amount_cents != null && action.facts.amount_cents > 3000"
    check_limit(f'{over} ? "refuse" : "allow"', SPLIT, time_zone_set=False, kinds=without)
    with pytest.raises(InvalidLimit, match="null"):
        check_limit(
            'action.facts.amount_cents > 3000 ? "refuse" : "allow"',
            SPLIT,
            time_zone_set=False,
            kinds=without,
        )
    # Unknown as before without the open kinds (and no KeyError).
    with pytest.raises(InvalidLimit, match="an action type of the catalog"):
        check_limit('"allow"', SPLIT, time_zone_set=False)
    counted = MappingProxyType({SPLIT: kind(facts={"people": "number"})})
    guarded = 'action.facts.people != null && action.facts.people > 6 ? "refuse" : "allow"'
    check_limit(guarded, SPLIT, time_zone_set=False, kinds=counted)
    with pytest.raises(InvalidLimit, match="null"):
        check_limit(
            'action.facts.people > 6 ? "refuse" : "allow"',
            SPLIT,
            time_zone_set=False,
            kinds=counted,
        )
    assert declared_facts(SPLIT, policy_with(kind(facts={"people": "number"}))) == {
        **MADE_FACTS,
        "people": "number",
    }
    assert declared_facts(SPLIT, DEFAULT_POLICY) is None


# --- the words of people -------------------------------------------------------------------------


def test_a_kind_is_told_by_its_description():
    for language in ("en", "fr"):
        texts = CatalogTexts(language)
        kinds = policy_with(kind()).kinds
        assert describe_type(SPLIT, {}, texts, kinds=kinds) == DESCRIPTION
        # Without the kind, the general sentence, as for any type without its own.
        assert describe_type(SPLIT, {}, texts) == t("action.describe", language)
        assert describe_example(SPLIT, {}, who="Alex", names={}, texts=texts, kinds=kinds) == t(
            "rights.example.made", language, who="Alex", action=DESCRIPTION
        )


# --- the writer of rights ------------------------------------------------------------------------


def writer_context():
    return dataclasses.replace(context(), policy=policy_with(kind()))


def test_the_writer_is_told_each_kind_with_its_description():
    catalog = writer_context_json(writer_context())["catalog"]
    assert isinstance(catalog, list)
    [entry] = [e for e in catalog if isinstance(e, dict) and e["action_type"] == SPLIT]
    assert entry == {
        "action_type": SPLIT,
        "allowed_roles": ["owner", "adult"],
        "facts": dict(MADE_FACTS),
        "description": DESCRIPTION,
    }
    # The code's types have none.
    assert not any(
        "description" in e for e in catalog if isinstance(e, dict) and e["action_type"] != SPLIT
    )


TOOLS_ASK_A_YES = (
    "change 1: on what a running program asks (program.) and on the assistant's own tools (made.), "
    "ask is "
    "yes, explicit_yes or proof"
)
TOOLS_NO_JUDGE = (
    "change 1: on what a running program asks (program.) and on the assistant's own tools (made.), "
    "nothing goes to a judge: no judge_question, and a limit gives allow, refuse or ask_owner"
)


async def test_a_right_on_a_kind_takes_the_owners_rules_but_no_judge():
    def denies(**fields) -> Draft:
        said = {"action_type": SPLIT, "limit_expression": None, "access": "deny", **fields}
        return Draft((change(**said),), (Example(ALEX, SPLIT, {}, "refused"),))

    def says(expected: str, **fields) -> Draft:
        said = {"action_type": SPLIT, "limit_expression": None, "access": None, **fields}
        return Draft((change(**said),), (Example(ALEX, SPLIT, {}, expected),))

    assert await check_draft(denies(), writer_context()) == []
    # Opened to a child, who does not have it by default: an adult still agrees first (ruling 16).
    opens = Draft(
        (change(person_id=SAM, action_type=SPLIT, limit_expression=None),),
        (Example(SAM, SPLIT, {}, "asks_an_adult"),),
    )
    assert await check_draft(opens, writer_context()) == []
    # The owner's "always ask" and an approver (final review I2, spec 5.8).
    for allowed in (
        says("asks_them", ask="yes"),
        says("asks_an_adult", approver="adult"),
        says("asks_the_owner", approver="owner"),
    ):
        assert await check_draft(allowed, writer_context()) == []
    # Nothing that goes to a judge.
    assert TOOLS_ASK_A_YES in await check_draft(denies(access=None, ask="judge"), writer_context())
    judged = denies(access=None, limit_expression='"judge"', judge_question="Is it worth it?")
    assert TOOLS_NO_JUDGE in await check_draft(judged, writer_context())
    # An example of a kind carries its facts, none here.
    wrong = Draft(denies().changes, (Example(ALEX, SPLIT, {"people": 3}, "refused"),))
    assert "example 1: facts are exactly the facts of its action type" in await check_draft(
        wrong, writer_context()
    )
    # Unknown to a writer without the open catalog.
    assert "change 1: action_type is a type of the catalog" in await check_draft(
        denies(), context()
    )


# --- end to end: a turn, the card, the yes ------------------------------------------------------


def denies_alex_the_split(alex) -> dict:
    return {
        "changes": [
            {
                "person_id": str(alex),
                "role": None,
                "action_type": SPLIT,
                "remove": False,
                "access": "deny",
                "approver": None,
                "limit_expression": None,
                "judge_question": None,
                "summary": "Alex ne partage plus les additions.",
            }
        ],
        "examples": [
            {"person_id": str(alex), "action_type": SPLIT, "facts": {}, "expected": "refused"}
        ],
    }


async def test_a_right_on_a_kind_is_drafted_shown_and_applied(
    owner_chat, adult_member, session_factory
):
    async with session_factory() as writer:
        await add_kind(writer, kind(), now=NOW)
        await writer.commit()
    alex = adult_member.person
    words = "Alex ne partage plus les additions"
    reply, model = await propose(owner_chat, words, denies_alex_the_split(alex.id))
    assert reply.actions == [{"tool": "rights_propose", "status": "done"}]
    # The writer was told the kind, with its description (the turn's catalog).
    [call] = model.writer_calls
    assert SPLIT in call.messages[0].content and DESCRIPTION in call.messages[0].content
    [result] = model.results
    assert result["examples"] == [
        {
            "situation": t("rights.example.made", "en", who="Alex", action=DESCRIPTION),
            "outcome": "refused",
        }
    ]
    [draft] = await drafts(session_factory)

    waiting = await apply(owner_chat, draft.id)
    assert waiting.actions == [{"tool": "rights_apply", "status": "waiting"}]
    # The card of the change shows the right and its example in words.
    [card] = confirmation_cards(waiting.cards_html)
    shown = plain(card)
    assert "Alex ne partage plus les additions." in shown
    assert t("rights.example.made", "en", who="Alex", action=DESCRIPTION) in shown
    assert t("rights.example.outcome.refused", "en") in shown

    answer = await owner_chat.confirm_last(waiting)
    assert answer.status == "executed"
    [right] = live(await rights_of(session_factory, SPLIT))
    assert (right.person_id, right.access) == (alex.id, "deny")
    async with session_factory() as reader:
        policy = await load_policy(reader)
        rights = await load_rights(reader)
    actor = Actor(alex.id, "adult")
    assert decide(actor, split_action(), policy, rights) == Refuse("not_allowed")
    assert isinstance(
        decide(Actor(owner_chat.person.id, "owner"), split_action(), policy, rights), Allow
    )


# --- carried from the review of Task 5 ------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["made.", "made.Split", "made.1split", "made.split-it", "made.a.b"]
)
async def test_a_kind_is_named_after_a_tool(session, name):
    with pytest.raises(KindRefused, match="after made. comes the name of the tool"):
        await add_kind(session, kind(name), now=NOW)
    assert await session.get(ActionKind, name) is None
    await add_kind(session, kind("made." + "a" * 40), now=NOW)
    with pytest.raises(KindRefused, match="after made. comes the name of the tool"):
        await add_kind(session, kind("made." + "a" * 41), now=NOW)


async def test_the_turn_a_button_wakes_lays_the_kinds_over_the_code_s_catalog(
    adult_chat, session_factory, monkeypatch
):
    # Review of Task 5: the woken turn was given the policy the request loaded, kinds and all, and
    # would keep a kind as it was then (``load_policy`` leaves a type its base knows).
    from domo_core.channels import turns

    given: list[PolicyTables] = []
    real = turns.turn_deps_of

    def turn_deps_of(*args, **kwargs):
        given.append(kwargs["policy"])
        return real(*args, **kwargs)

    monkeypatch.setattr(turns, "turn_deps_of", turn_deps_of)
    reply = await adult_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": "x"})
    async with session_factory() as writer:
        writer.add(ActionKind(action_type=SPLIT, default_roles=["adult"], description=DESCRIPTION))
        await writer.commit()
    assert (await adult_chat.confirm_last(reply)).status == "executed"
    assert given, "no turn was woken"
    assert all(SPLIT not in policy.known for policy in given)
    assert given[-1] is DEFAULT_POLICY
