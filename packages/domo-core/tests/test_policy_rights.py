"""The owner's rights (spec 5.1 to 5.3, 5.8, 5.9 and 5.12): who may do what, per role and per
person, kept in ``policy_rights`` and read again at every decision.

A right is about an action type and a subject (a role, or a person). It may give the action or take
it away (``access``), say whose agreement is needed first, and carry a limit. Each of these is
resolved on its own: the person's right, else their role's, else the catalog. Nothing a right says
reaches the owner's own actions (changing the rules), and a type the catalog does not know is never
allowed.

The resolution is pure, and tried on tables of rights made here (``right``). What the table keeps,
and what is read from it, is tried on the shared rolled-back ``session``. The chat pilots talk with
Domo on a live server, with rights written by the ``grant`` fixture (see ``chat_support.py``).
"""

import html
import re
import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from chat_support import Member, live_http_client, log_in_live, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.store import ANSWER_BY_THE_OWNER
from domo_core.policy import catalog
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import NeverAsk
from domo_core.policy.barrier import Allow, NeedsApproval, Refuse, decide
from domo_core.policy.catalog import (
    ALLOWED_ACTIONS,
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PolicyTables,
)
from domo_core.policy.decision import decide_now
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import (
    NO_RIGHTS,
    Limit,
    Resolved,
    Right,
    RightsTable,
    load_rights,
    pause_personal_rights,
    pause_rights_and_audit,
    paused_rights,
    personal_right_summaries,
)
from domo_core.policy.roles import ROLES, Actor, as_role, domo_actor
from fakes import FakeChatModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
INVITE = DescribedAction(
    "core",
    "household.invite",
    None,
    "permissions",
    "household",
    {"role": "adult", "days": None, "display_name": "Dana", "owner_first": False},
)
RENAME = DescribedAction("core", "profile.rename", None, "self_profile", "household")
NEW_PASSWORD = "a-brand-new-passphrase"


def right(
    *,
    action_type: str,
    person_id: uuid.UUID | None = None,
    role: str | None = None,
    access: str | None = None,
    approver: str | None = None,
    limit: Limit | None = None,
    ask: str | None = None,
    ask_when: str | None = None,
    summary: str = "a right",
    for_domo: bool = False,
) -> Right:
    """A right of a table made for a test (``RightsTable``), as ``load_rights`` gives them."""
    return Right(
        uuid4(),
        person_id,
        role,  # type: ignore[arg-type]
        action_type,
        access,  # type: ignore[arg-type]
        approver,  # type: ignore[arg-type]
        limit,
        ask,  # type: ignore[arg-type]
        ask_when,
        summary,
        for_domo,
    )


def names(definitions) -> list[str]:
    return [definition.name for definition in definitions]


# --- how a right is resolved ---------------------------------------------------------------------


def test_a_person_right_beats_the_role_right_field_by_field():
    justine = Actor(uuid4(), "adult")
    rights = RightsTable(
        (
            right(role="adult", action_type="household.invite", access="allow", ask="yes"),
            right(person_id=justine.person_id, action_type="household.invite", approver="owner"),
        )
    )
    resolved = rights.resolve(justine, "household.invite", DEFAULT_POLICY)
    assert (resolved.allowed, resolved.approver, resolved.ask) == (True, "owner", "yes")


def test_a_person_right_wins_over_the_role_right_on_the_same_field():
    justine = Actor(uuid4(), "adult")
    rights = RightsTable(
        (
            right(
                role="adult",
                action_type="household.invite",
                access="allow",
                approver="adult",
            ),
            right(person_id=justine.person_id, action_type="household.invite", approver="self"),
        )
    )
    # Neither says a limit: the catalog's applies.
    stay = DEFAULT_LIMITS["household.invite"]
    assert rights.resolve(justine, "household.invite", DEFAULT_POLICY) == Resolved(
        True, "self", stay, None, None
    )
    # Another adult has the role's right only.
    assert rights.resolve(Actor(uuid4(), "adult"), "household.invite", DEFAULT_POLICY) == Resolved(
        True, "adult", stay, None, None
    )


def test_without_a_right_the_catalog_decides():
    for role in ROLES:
        actor = Actor(uuid4(), role)
        for action_type in KNOWN_ACTIONS:
            assert NO_RIGHTS.resolve(actor, action_type, DEFAULT_POLICY) == Resolved(
                action_type in ALLOWED_ACTIONS[role],
                None,
                DEFAULT_LIMITS.get(action_type),
                None,
                None,
            ), (role, action_type)


def test_the_catalog_under_the_rights_is_the_one_given():
    """Tests (and the web dependency ``get_policy``) hand in other tables: the rights sit on
    those."""
    stricter = PolicyTables(
        KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "guest": frozenset({"household.list"})}
    )
    guest = Actor(uuid4(), "guest")
    assert NO_RIGHTS.resolve(guest, "household.list", stricter) == Resolved(
        True, None, None, None, None
    )
    assert not NO_RIGHTS.resolve(guest, "profile.rename", stricter).allowed


def test_a_deny_right_takes_away_a_default_right():
    guest = Actor(uuid4(), "guest")
    rights = RightsTable((right(role="guest", action_type="profile.rename", access="deny"),))
    assert not rights.resolve(guest, "profile.rename", DEFAULT_POLICY).allowed
    assert decide(guest, RENAME, DEFAULT_POLICY, rights) == Refuse("not_allowed")
    # The others keep it.
    assert rights.resolve(Actor(uuid4(), "adult"), "profile.rename", DEFAULT_POLICY).allowed


def test_a_person_may_keep_what_their_role_lost():
    robin = Actor(uuid4(), "guest")
    rights = RightsTable(
        (
            right(role="guest", action_type="profile.rename", access="deny"),
            right(person_id=robin.person_id, action_type="profile.rename", access="allow"),
        )
    )
    assert rights.resolve(robin, "profile.rename", DEFAULT_POLICY).allowed
    assert not rights.resolve(Actor(uuid4(), "guest"), "profile.rename", DEFAULT_POLICY).allowed


def test_the_right_of_someone_else_or_of_another_role_does_not_count():
    alex, jo = Actor(uuid4(), "adult"), Actor(uuid4(), "adult")
    rights = RightsTable(
        (
            right(person_id=alex.person_id, action_type="household.change_role", access="allow"),
            right(role="child", action_type="household.remove", access="allow"),
        )
    )
    assert rights.resolve(alex, "household.change_role", DEFAULT_POLICY).allowed
    assert not rights.resolve(jo, "household.change_role", DEFAULT_POLICY).allowed
    assert not rights.resolve(jo, "household.remove", DEFAULT_POLICY).allowed
    assert rights.resolve(Actor(uuid4(), "child"), "household.remove", DEFAULT_POLICY).allowed


def test_a_person_keeps_their_own_rights_whatever_their_role():  # spec 5.12
    alex = uuid4()
    rights = RightsTable((right(person_id=alex, action_type="household.invite", access="allow"),))
    for role in ROLES:
        assert rights.resolve(Actor(alex, role), "household.invite", DEFAULT_POLICY).allowed


@pytest.mark.parametrize("action_type", sorted(OWNER_ONLY_ACTIONS))
def test_owner_only_actions_ignore_every_right(action_type):
    tables = PolicyTables(
        KNOWN_ACTIONS, {**ALLOWED_ACTIONS, "owner": ALLOWED_ACTIONS["owner"] | {action_type}}
    )
    owner, adult = Actor(uuid4(), "owner"), Actor(uuid4(), "adult")
    everything = RightsTable(
        (
            right(role="adult", action_type=action_type, access="allow"),
            right(
                person_id=adult.person_id,
                action_type=action_type,
                access="allow",
                approver="self",
            ),
            right(role="owner", action_type=action_type, access="deny"),
            right(person_id=owner.person_id, action_type=action_type, approver="adult"),
        )
    )
    assert not everything.resolve(adult, action_type, tables).allowed
    assert everything.resolve(owner, action_type, tables) == Resolved(True, None, None, None, None)
    action = DescribedAction("core", action_type, None, "permissions", "household")
    assert decide(adult, action, tables, everything) == Refuse("not_allowed")
    assert decide(owner, action, tables, everything) == Allow()
    # Only what to ask is read on them (F4): the owner's own rule.
    asks = RightsTable(
        (
            *everything.rights[:2],
            right(person_id=owner.person_id, action_type=action_type, ask="explicit_yes"),
        )
    )
    assert asks.resolve(owner, action_type, tables) == Resolved(
        True, None, None, "explicit_yes", None
    )
    assert not asks.resolve(adult, action_type, tables).allowed


def test_an_action_the_catalog_does_not_know_is_never_allowed():
    owner = Actor(uuid4(), "owner")
    rights = RightsTable(
        (
            right(person_id=owner.person_id, action_type="x.unknown", access="allow"),
            right(role="owner", action_type="x.unknown", access="allow"),
        )
    )
    assert rights.resolve(owner, "x.unknown", DEFAULT_POLICY) == Resolved(
        False, None, None, None, None
    )
    unknown = DescribedAction("core", "x.unknown", None, "read", "household")
    assert decide(owner, unknown, DEFAULT_POLICY, rights) == Refuse("not_allowed")


def test_a_limit_comes_with_the_right_that_carries_it():
    three_days = Limit('action.facts.days <= 3 ? "allow" : "refuse"', None, "three days at most")
    friends = Limit('"judge"', "Is the guest a friend of Justine?", "her friends only")
    justine, jo = Actor(uuid4(), "adult"), Actor(uuid4(), "adult")
    rights = RightsTable(
        (
            right(role="adult", action_type="household.invite", access="allow", limit=three_days),
            right(person_id=justine.person_id, action_type="household.invite", limit=friends),
        )
    )
    assert rights.resolve(justine, "household.invite", DEFAULT_POLICY).limit == friends
    assert rights.resolve(jo, "household.invite", DEFAULT_POLICY).limit == three_days
    # Without a right that says one, the catalog's limit applies (spec 5.11); none for the others.
    assert (
        NO_RIGHTS.resolve(jo, "household.invite", DEFAULT_POLICY).limit
        == (DEFAULT_LIMITS["household.invite"])
    )
    assert NO_RIGHTS.resolve(jo, "household.list", DEFAULT_POLICY).limit is None
    # Tables of a test may have none.
    bare = PolicyTables(KNOWN_ACTIONS, ALLOWED_ACTIONS, limits={})
    assert NO_RIGHTS.resolve(jo, "note.leave", bare).limit is None


def test_the_limit_is_the_catalogs():
    """The base module defines it (the catalog's default limits use it); the rights re-export it."""
    assert Limit is catalog.Limit


# --- what the table keeps ------------------------------------------------------------------------


def _row(by: uuid.UUID, **fields) -> PolicyRight:
    fields.setdefault("summary", "a right")
    fields.setdefault("owner_words", "")
    return PolicyRight(created_by=by, **fields)


async def test_the_rights_in_force_are_read_from_the_database(session, add_person):
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    session.add_all(
        [
            _row(
                owner.id,
                person_id=alex.id,
                action_type="household.invite",
                access="allow",
                approver="owner",
                limit_expression='action.facts.role == "guest" ? "allow" : "refuse"',
                summary="Alex may invite guests",
            ),
            _row(owner.id, role="guest", action_type="profile.rename", access="deny"),
            # Taken away, and paused: neither counts.
            _row(owner.id, role="child", action_type="note.leave", ask="yes", revoked_at=NOW),
            _row(
                owner.id,
                person_id=alex.id,
                action_type="household.remove",
                access="allow",
                paused_at=NOW,
            ),
        ]
    )
    await session.flush()
    rights = await load_rights(session)
    assert sorted(r.action_type for r in rights.rights) == ["household.invite", "profile.rename"]
    alex_invites = next(r for r in rights.rights if r.person_id == alex.id)
    assert alex_invites == Right(
        alex_invites.id,
        alex.id,
        None,
        "household.invite",
        "allow",
        "owner",
        Limit('action.facts.role == "guest" ? "allow" : "refuse"', None, "Alex may invite guests"),
        None,
        None,
        "Alex may invite guests",
    )
    guests = next(r for r in rights.rights if r.role == "guest")
    assert (guests.person_id, guests.access, guests.limit) == (None, "deny", None)
    assert [r.action_type for r in await paused_rights(session)] == ["household.remove"]


async def test_the_barrier_reads_the_rights_of_the_moment(session, add_person):
    owner = await add_person("Owner", "owner")
    # A child (an adult invites by default since 0.9.1).
    sam = await add_person("Sam", "child")
    actor = Actor(sam.id, "child")
    assert await decide_now(session, actor, INVITE, DEFAULT_POLICY, NOW) == Refuse("not_allowed")
    session.add(
        _row(
            owner.id,
            person_id=sam.id,
            action_type="household.invite",
            access="allow",
            approver="owner",
        )
    )
    await session.flush()
    assert await decide_now(session, actor, INVITE, DEFAULT_POLICY, NOW) == NeedsApproval("owner")


async def test_a_right_without_a_subject_carries_only_a_rule(session, add_person):
    """A right for nobody in particular is a rule for everyone: it says what to ask, and
    nothing else; and a right is never for a person and a role at once."""
    owner = await add_person("Owner", "owner")

    async def add(action_type: str, **fields) -> None:
        async with session.begin_nested():
            session.add(_row(owner.id, action_type=action_type, **fields))

    await add("household.invite", ask="yes")
    # (Each on a type of its own: one right per subject and type is the index's business.)
    for action_type, more in (
        ("household.remove", {"access": "allow"}),
        ("household.change_role", {"approver": "owner"}),
        ("note.leave", {"limit_expression": '"allow"'}),
    ):
        with pytest.raises(IntegrityError):
            await add(action_type, ask="proof", **more)
    with pytest.raises(IntegrityError):
        await add("household.list", person_id=owner.id, role="adult", access="deny")
    rows = await session.execute(select(PolicyRight.person_id, PolicyRight.role, PolicyRight.ask))
    assert rows.all() == [(None, None, "yes")]


@pytest.mark.parametrize(
    "wrong",
    [
        {"role": "member"},
        {"access": "maybe"},
        {"approver": "child"},
        {"ask": "maybe"},
        {"ask": "judge", "ask_when": "x" * 301},
        {"ask": "yes", "ask_when": "only for guests"},  # conditions go with "judge" only
        {"ask_when": "only for guests"},
        {"action_type": "x" * 101},
        {"limit_expression": "x" * 2001},
        {"judge_question": "x" * 301},
        {"summary": ""},
        {"summary": "x" * 301},
        {"owner_words": "x" * 1001},
    ],
)
async def test_the_table_keeps_out_what_a_right_cannot_say(session, add_person, wrong):
    owner = await add_person("Owner", "owner")
    fields = {"role": "adult", "action_type": "household.list", "limit_expression": '"allow"'}
    session.add(_row(owner.id, **{**fields, **wrong}))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_longest_values_a_right_may_hold_fit(session, add_person):
    owner = await add_person("Owner", "owner")
    session.add(
        _row(
            owner.id,
            role="adult",
            action_type="x" * 100,
            limit_expression="x" * 2000,
            judge_question="x" * 300,
            ask="judge",
            ask_when="x" * 300,
            summary="x" * 300,
            owner_words="x" * 1000,
        )
    )
    await session.flush()


async def test_one_right_is_in_force_per_subject_and_action_type(session, add_person):
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")

    async def add(**fields) -> PolicyRight:
        row = _row(owner.id, action_type="household.invite", access="allow", **fields)
        async with session.begin_nested():
            session.add(row)
        return row

    first = await add(person_id=alex.id)
    with pytest.raises(IntegrityError):
        await add(person_id=alex.id)
    # A paused right still holds its place (giving it back is a change like any other).
    first.paused_at = NOW
    await session.flush()
    with pytest.raises(IntegrityError):
        await add(person_id=alex.id)
    # Taken away (or replaced), it makes room.
    first.revoked_at = NOW
    await session.flush()
    await add(person_id=alex.id)

    await add(role="adult")
    with pytest.raises(IntegrityError):
        await add(role="adult")
    await add(role="child")
    # Domo's own right holds its own place, apart from a rule for everyone on the same type.
    await add(for_domo=True)
    with pytest.raises(IntegrityError):
        await add(for_domo=True)
    async with session.begin_nested():
        session.add(_row(owner.id, action_type="household.invite", ask="yes"))


async def test_a_right_of_domo_has_no_other_subject_and_may_say_anything(
    session, add_person
):  # 2026-11-18: Domo is a subject of rights of his own
    owner = await add_person("Owner", "owner")

    async def add(action_type: str, **fields) -> None:
        async with session.begin_nested():
            session.add(_row(owner.id, action_type=action_type, for_domo=True, **fields))

    # Not a rule for everyone: it says whether he may, under which limit, with whose agreement.
    await add("note.leave", access="deny", approver="owner", limit_expression='"allow"')
    for action_type, more in (
        ("household.list", {"person_id": owner.id}),
        ("household.invite", {"role": "adult"}),
    ):
        with pytest.raises(IntegrityError):
            await add(action_type, ask="yes", **more)
    rights = await load_rights(session)
    [his] = rights.rights
    assert (his.for_domo, his.person_id, his.role, his.access) == (True, None, None, "deny")
    # A right written without saying is nobody's but its subject's.
    session.add(_row(owner.id, role="adult", action_type="household.list", access="deny"))
    await session.flush()
    assert not next(r for r in (await load_rights(session)).rights if r.role == "adult").for_domo


def test_domo_s_right_is_his_and_no_rule_for_everyone():
    domo = domo_actor()
    dana = Actor(uuid4(), "adult")
    rights = RightsTable(
        (
            right(action_type="household.invite", for_domo=True, access="deny", ask="proof"),
            right(action_type="household.invite", role="adult", access="allow", ask="yes"),
        )
    )
    mine = rights.resolve(domo, "household.invite", DEFAULT_POLICY)
    assert (mine.allowed, mine.ask) == (False, "proof")
    # Dana reads her role's right, never Domo's.
    hers = rights.resolve(dana, "household.invite", DEFAULT_POLICY)
    assert (hers.allowed, hers.ask) == (True, "yes")
    # Without a right of his, the catalog's own for Domo: what no role has, he has.
    assert NO_RIGHTS.resolve(domo, "texts.language_version", DEFAULT_POLICY).allowed
    assert not NO_RIGHTS.resolve(dana, "texts.language_version", DEFAULT_POLICY).allowed
    # And not, by default, what the socle holds (a secret).
    assert not NO_RIGHTS.resolve(domo, "keys.house", DEFAULT_POLICY).allowed
    # And a catalog that says otherwise.
    narrow = PolicyTables(KNOWN_ACTIONS, ALLOWED_ACTIONS, domo=frozenset())
    assert not NO_RIGHTS.resolve(domo, "household.list", narrow).allowed
    assert NO_RIGHTS.resolve(dana, "household.list", narrow).allowed


async def test_pausing_a_persons_rights_leaves_the_others_alone(session, add_person):  # spec 5.12
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    jo = await add_person("Jo", "adult")
    earlier = NOW - timedelta(days=3)
    session.add_all(
        [
            _row(owner.id, person_id=alex.id, action_type="household.invite", summary="invite"),
            _row(owner.id, person_id=alex.id, action_type="household.list", summary="list"),
            _row(owner.id, person_id=alex.id, action_type="note.leave", revoked_at=earlier),
            _row(owner.id, person_id=alex.id, action_type="profile.rename", paused_at=earlier),
            _row(owner.id, person_id=jo.id, action_type="household.invite", summary="Jo's"),
            _row(owner.id, role="adult", action_type="household.invite", summary="adults'"),
        ]
    )
    await session.flush()
    assert await personal_right_summaries(session, alex.id) == ["invite", "list"]

    assert await pause_personal_rights(session, alex.id, NOW) == 2

    paused = await session.execute(
        select(PolicyRight.action_type, PolicyRight.paused_at)
        .where(PolicyRight.person_id == alex.id)
        .order_by(PolicyRight.action_type)
    )
    assert paused.all() == [
        ("household.invite", NOW),
        ("household.list", NOW),
        ("note.leave", None),  # taken away already: it stays so
        ("profile.rename", earlier),  # paused before: when stays
    ]
    assert await personal_right_summaries(session, alex.id) == []
    assert await personal_right_summaries(session, jo.id) == ["Jo's"]
    assert {r.summary for r in (await load_rights(session)).rights} == {"Jo's", "adults'"}
    assert await pause_personal_rights(session, alex.id, NOW) == 0


async def test_the_summaries_can_include_the_rights_that_are_paused(session, add_person):
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    earlier = NOW - timedelta(days=3)
    session.add_all(
        [
            _row(owner.id, person_id=alex.id, action_type="household.invite", summary="in force"),
            _row(
                owner.id,
                person_id=alex.id,
                action_type="note.leave",
                summary="paused",
                paused_at=earlier,
            ),
            _row(
                owner.id,
                person_id=alex.id,
                action_type="profile.rename",
                summary="taken away",
                paused_at=earlier,
                revoked_at=earlier,
            ),
        ]
    )
    await session.flush()
    assert await personal_right_summaries(session, alex.id) == ["in force"]
    assert set(await personal_right_summaries(session, alex.id, with_paused=True)) == {
        "in force",
        "paused",
    }


async def test_pausing_the_rights_of_a_person_audits_how_many_and_only_when_there_were_some(
    session, add_person
):  # F26: the one block that accept_return, disable and the upkeep share
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    jo = await add_person("Jo", "adult")
    session.add_all(
        [
            _row(owner.id, person_id=alex.id, action_type="household.invite"),
            _row(owner.id, person_id=alex.id, action_type="note.leave"),
            _row(owner.id, person_id=jo.id, action_type="household.invite"),
        ]
    )
    await session.flush()

    async def audited() -> list[tuple[str, dict]]:
        found = await session.execute(
            select(AuditEntry.actor, AuditEntry.details)
            .where(AuditEntry.action == "rights.paused")
            .order_by(AuditEntry.id)
        )
        return [(actor, details) for actor, details in found.all()]

    assert await pause_rights_and_audit(session, alex.id, NOW) == 2
    # Identifiers and a number only; the person themselves are the actor unless it is said.
    assert await audited() == [(f"person:{alex.id}", {"person_id": str(alex.id), "count": 2})]
    assert await pause_rights_and_audit(session, alex.id, NOW) == 0
    assert len(await audited()) == 1
    assert await pause_rights_and_audit(session, jo.id, NOW, actor="system") == 1
    assert (await audited())[1] == ("system", {"person_id": str(jo.id), "count": 1})


# --- what the owner wants asked, or never asked (0.5.0) ------------------------------------------


def _actor(member: Member) -> Actor:
    return Actor(member.person.id, as_role(member.person.role))


async def _resolved(session_factory, actor: Actor, action_type: str) -> Resolved:
    """What applies to ``actor`` for ``action_type`` with the rights the database holds now."""
    async with session_factory() as reader:
        return (await load_rights(reader)).resolve(actor, action_type, DEFAULT_POLICY)


async def test_a_rule_for_everyone_counts_for_each_person_and_the_most_demanding_wins(
    session_factory, owner_member, adult_member, grant
):
    owner, alex = _actor(owner_member), _actor(adult_member)
    assert (await _resolved(session_factory, alex, "household.invite")).ask is None
    await grant("household.invite", ask="yes", summary="Ask before any invitation.")
    await grant("household.invite", role="adult", ask="proof", summary="Adults prove it.")
    assert (await _resolved(session_factory, alex, "household.invite")).ask == "proof"
    assert (await _resolved(session_factory, owner, "household.invite")).ask == "yes"
    # The person's own rule counts with the others: the most demanding of the three.
    await grant("household.invite", person=owner_member.person, ask="explicit_yes")
    assert (await _resolved(session_factory, owner, "household.invite")).ask == "explicit_yes"
    # A rule says what to ask, nothing else: Alex still may not change a role (an adult invites
    # by default since 0.9.1).
    await grant("household.change_role", ask="yes", summary="Ask before any change of role.")
    resolved = await _resolved(session_factory, alex, "household.change_role")
    assert (resolved.allowed, resolved.never_ask) == (False, None)
    async with session_factory() as reader:
        rights = (await load_rights(reader)).rights
    [everyone] = [
        r
        for r in rights
        if r.role is None and r.person_id is None and r.action_type == "household.invite"
    ]
    assert (everyone.ask, everyone.ask_when, everyone.access) == ("yes", None, None)


async def test_a_rule_for_everyone_is_never_paused(
    session_factory, owner_member, adult_member, grant, live_clock
):  # Review Focus 4
    owner, alex = _actor(owner_member), _actor(adult_member)
    await grant("note.leave", ask="yes", summary="Ask before any note.")
    await grant("household.invite", person=adult_member.person, access="allow")
    async with session_factory() as writer:
        # Alex leaves: only the right given to Alex himself is paused.
        assert await pause_rights_and_audit(writer, alex.person_id, live_clock()) == 1
        await writer.commit()
    async with session_factory() as reader:
        assert [r.action_type for r in await paused_rights(reader)] == ["household.invite"]
    # The rule for everyone stays in force, for the others and for Alex alike.
    assert (await _resolved(session_factory, owner, "note.leave")).ask == "yes"
    assert (await _resolved(session_factory, alex, "note.leave")).ask == "yes"


async def test_a_never_ask_rule_resolves_with_its_conditions_and_a_rule_that_asks_wins(
    session_factory, owner_member, adult_member, grant
):
    owner, alex = _actor(owner_member), _actor(adult_member)
    # The owner says not to ask him before applying rights, with his conditions in his words.
    await grant("rights.apply", person=owner_member.person, ask="judge", ask_when="only for guests")
    applying = await _resolved(session_factory, owner, "rights.apply")
    assert (applying.allowed, applying.ask) == (True, None)
    assert applying.never_ask == NeverAsk("only for guests")
    # "Not at all" on handing his role over: no yes ever, at most a proof.
    await grant("household.transfer_ownership", person=owner_member.person, ask="never")
    handing = await _resolved(session_factory, owner, "household.transfer_ownership")
    assert (handing.ask, handing.never_ask) == (None, NeverAsk(None, at_all=True))
    # A rule that asks always wins over one that lifts.
    await grant("household.invite", ask="judge", summary="Never ask for an invitation.")
    await grant("household.invite", role="adult", ask="yes", summary="Adults say yes first.")
    alex_invites = await _resolved(session_factory, alex, "household.invite")
    assert (alex_invites.ask, alex_invites.never_ask) == ("yes", None)
    assert (await _resolved(session_factory, owner, "household.invite")).never_ask == NeverAsk(None)
    # "Not at all" outside the owner's own actions is only guidance for the judge
    # (F5): written by hand (the writer refuses it), it reads as "judge".
    await grant("household.invite", person=owner_member.person, ask="never")
    assert (await _resolved(session_factory, owner, "household.invite")).never_ask == NeverAsk(
        None, at_all=False
    )


@pytest.mark.parametrize("ask", ["never", "judge"])
@pytest.mark.parametrize("subject", ["role", "everyone"])
@pytest.mark.parametrize("action_type", sorted(catalog.OWNER_ONLY_ACTIONS))
def test_only_the_persons_own_right_says_never_ask_me_on_the_owners_own_actions(
    action_type, subject, ask
):  # F4: a "never ask me" must never pass to a later owner
    owner = Actor(uuid4(), "owner")
    lift = right(
        action_type=action_type,
        role="owner" if subject == "role" else None,
        ask=ask,
        summary="Never ask the owner.",
    )
    resolved = RightsTable((lift,)).resolve(owner, action_type, DEFAULT_POLICY)
    assert (resolved.ask, resolved.never_ask) == (None, None)
    # The same one, given to the person himself, holds.
    mine = right(action_type=action_type, person_id=owner.person_id, ask=ask)
    resolved = RightsTable((lift, mine)).resolve(owner, action_type, DEFAULT_POLICY)
    assert resolved.never_ask == NeverAsk(None, at_all=ask == "never")


def test_a_roles_or_everyones_never_ask_me_counts_outside_the_owners_own_actions():
    # Outside the owner's own actions, "never ask me" is guidance for the judge, whoever it is
    # for: a role's or everyone's counts as before.
    alex = Actor(uuid4(), "adult")
    for lift in (
        right(action_type="note.leave", role="adult", ask="judge", ask_when="only to Sam"),
        right(action_type="note.leave", ask="judge", ask_when="only to Sam"),
    ):
        resolved = RightsTable((lift,)).resolve(alex, "note.leave", DEFAULT_POLICY)
        assert resolved.never_ask == NeverAsk("only to Sam")


async def test_an_approver_on_a_type_the_catalog_leaves_free_asks_for_their_agreement(
    session_factory, adult_member, grant
):  # F3: the level went with the yes-judge; who must agree stays a right
    alex = _actor(adult_member)
    await grant("note.leave", person=adult_member.person, approver="owner", ask="yes")
    note = DescribedAction(
        "core",
        "note.leave",
        None,
        "message",
        "household",
        {"recipient": f"person:{alex.person_id}", "recipient_role": "adult", "length": 3},
    )
    async with session_factory() as reader:
        decided = await decide_now(reader, alex, note, DEFAULT_POLICY, NOW)
    assert decided == NeedsApproval("owner", None, "yes")


# --- with Domo ---------------------------------------------------------------------------------


async def test_a_right_given_to_a_person_lets_them_do_what_their_role_may_not(
    adult_chat, grant, child_member, person_row
):
    # (An adult invites by default since 0.9.1: changing a role is still the owner's.)
    making_sam_an_adult = {"person": "Sam", "role": "adult"}
    refused = await adult_chat.say_with_tool("household_change_role", making_sam_an_adult)
    assert refused.actions == [
        {"tool": "household_change_role", "status": "refused", "reason": "not_allowed"}
    ]

    await grant("household.change_role", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool("household_change_role", making_sam_an_adult)
    assert reply.actions == [{"tool": "household_change_role", "status": "waiting"}]
    [result] = reply.model_results
    assert (result["status"], result["by"]) == ("waiting_for_confirmation", "you")
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    assert (await person_row(child_member.person)).role == "adult"


async def test_the_owner_answers_a_request_whose_yes_is_theirs(
    adult_chat,
    live_owner_client,
    live_adult_client,
    grant,
    add_member,
    chat_server,
    count_invitations,
    last_audit_entry,
):
    jo = Member(await add_member("Jo", "adult", username="jo"), "jo")
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "adult"}
    )
    [result] = reply.model_results
    assert (result["by"], result["answer_with"]) == ("the owner", ANSWER_BY_THE_OWNER)
    assert t("action.card.waiting.owner", "en") in html.unescape(reply.cards_html)
    card_id = reply.pending_confirmation_id
    assert card_id is not None

    # It waits in the owner's zone, and in no adult's: not the requester's, not another adult's.
    assert f'data-confirmation-id="{card_id}"' in (await live_owner_client.get("/approvals")).text
    assert "data-confirmation-id" not in (await live_adult_client.get("/approvals")).text
    async with live_http_client(chat_server.base_url) as jo_browser:
        await log_in_live(jo_browser, jo.username, jo.password)
        assert "data-confirmation-id" not in (await jo_browser.get("/approvals")).text
        answer = await jo_browser.post(f"/confirmations/{card_id}/approve")
        assert status_of(answer) == "not_yours"
    assert status_of(await adult_chat.post(f"/confirmations/{card_id}/approve")) == "not_yours"
    assert await count_invitations() == 0

    answer = await live_owner_client.post(f"/confirmations/{card_id}/approve")
    assert status_of(answer) == "executed"
    assert await count_invitations() == 1
    decided = await last_audit_entry("confirmation.decided")
    assert decided.details["approver_kind"] == "owner"
    assert decided.actor == f"person:{(await _owner_id(chat_server))}"


async def _owner_id(chat_server) -> uuid.UUID:
    async with chat_server.session_factory() as reader:
        return (await reader.execute(select(Person.id).where(Person.role == "owner"))).scalar_one()


async def test_a_person_sees_the_tools_of_their_own_rights(adult_chat, grant):
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("hello")
    before = names(model.calls[-1].tools)
    # (An adult invites by default since 0.9.1: changing a role is still the owner's.)
    assert "household_change_role" not in before and "note_leave" in before

    await grant("household.change_role", person=adult_chat.person, access="allow")
    await grant("note.leave", role="adult", access="deny")
    await adult_chat.say("hello again")
    after = names(model.calls[-1].tools)
    assert "household_change_role" in after and "note_leave" not in after


async def test_a_right_taken_away_applies_to_the_requests_that_wait(
    adult_chat, grant, session_factory, child_member, person_row
):
    # (An adult invites by default since 0.9.1: changing a role is still the owner's.)
    given = await grant("household.change_role", person=adult_chat.person, access="allow")
    reply = await adult_chat.say_with_tool(
        "household_change_role", {"person": "Sam", "role": "adult"}
    )
    assert reply.actions == [{"tool": "household_change_role", "status": "waiting"}]
    async with session_factory() as writer:
        await writer.execute(
            update(PolicyRight)
            .where(PolicyRight.id == given.id)
            .values(revoked_at=adult_chat.clock())
        )
        await writer.commit()
    card = await adult_chat.confirm_last(reply)
    assert card.status == "refused"
    assert (await person_row(child_member.person)).role == "child"


async def test_who_says_yes_is_read_again_when_the_yes_comes(
    adult_chat, grant, count_invitations, confirmation_status
):
    """A request that waited for the person's own yes waits for the owner's once a right says so:
    the person's own yes no longer does, and the request waits for the owner (Ruling R27)."""
    await grant("household.invite", role="adult", access="allow")
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "guest"}
    )
    await grant("household.invite", person=adult_chat.person, approver="owner")
    card = await adult_chat.confirm_last(reply)
    assert card.status == "approver_changed"
    assert await confirmation_status(reply.pending_confirmation_id) == "pending"
    assert await count_invitations() == 0


async def test_the_rights_of_someone_who_left_stay_paused_when_they_come_back(
    owner_chat, adult_member, grant, chat_server, session_factory
):  # spec 5.7 and 5.12
    await grant(
        "household.change_role",
        person=adult_member.person,
        access="allow",
        summary="Alex may change roles",
    )
    await grant("note.leave", role="adult", ask="yes", summary="adults' notes wait")
    removed = await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    )
    assert removed.status == "executed"

    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Alex", "role": "adult"}, judge="do"
    )
    # The owner is told the rights Alex had, which were paused when Alex left.
    assert reply.model_results[-1]["paused_on_return"] == ["Alex may change roles"]
    async with session_factory() as reader:
        assert await personal_right_summaries(reader, adult_member.person.id) == []
        assert [r.summary for r in await paused_rights(reader)] == ["Alex may change roles"]
    token = re.search(r"/invite/([A-Za-z0-9_-]+)", reply.cards_html)
    assert token is not None

    async with live_http_client(chat_server.base_url) as browser:
        answer = await browser.post(f"/invite/{token.group(1)}", data={"password": NEW_PASSWORD})
        assert answer.status_code == 303

    async with session_factory() as reader:
        rights = await load_rights(reader)
        paused = await paused_rights(reader)
        assert await personal_right_summaries(reader, adult_member.person.id) == []
    assert [r.summary for r in paused] == ["Alex may change roles"]
    alex = Actor(adult_member.person.id, "adult")
    assert not rights.resolve(alex, "household.change_role", DEFAULT_POLICY).allowed
    # A role's right is not the person's: it stays.
    assert rights.resolve(alex, "note.leave", DEFAULT_POLICY).ask == "yes"


async def test_the_owner_is_told_the_rights_kept_after_a_role_change(
    owner_chat, adult_member, add_member, grant, chat_server, session_factory
):
    await grant(
        "household.invite",
        person=adult_member.person,
        access="allow",
        summary="Alex may invite people",
    )
    await grant("note.leave", role="adult", ask="yes", summary="adults' notes wait")
    await add_member("Jo", "adult")
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Alex", "role": "child"}, judge="do"
    )
    assert reply.actions == [{"tool": "household_change_role", "status": "done"}]
    assert reply.model_results[-1]["their_own_rights"] == ["Alex may invite people"]
    # Alex keeps them.
    async with session_factory() as reader:
        rights = await load_rights(reader)
    assert rights.resolve(
        Actor(adult_member.person.id, "child"), "household.invite", DEFAULT_POLICY
    ).allowed
    # Someone who has none: nothing is said of them.
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Jo", "role": "child"}, judge="do"
    )
    assert reply.actions == [{"tool": "household_change_role", "status": "done"}]
    assert "their_own_rights" not in reply.model_results[-1]


async def test_only_the_owner_is_told_the_rights_of_the_person(
    adult_chat, add_member, grant, chat_server
):
    jo = await add_member("Jo", "adult")
    await grant("household.change_role", person=adult_chat.person, access="allow")
    await grant("household.invite", person=jo, access="allow", summary="Jo may invite people")
    reply = await adult_chat.say_with_tool(
        "household_change_role", {"person": "Jo", "role": "child"}, judge="do"
    )
    assert reply.actions == [{"tool": "household_change_role", "status": "done"}]
    assert "their_own_rights" not in reply.model_results[-1]


async def test_a_right_in_the_table_cannot_give_away_the_owners_actions(
    adult_chat, grant, current_settings
):
    await grant("policy.update", person=adult_chat.person, access="allow", approver="self")
    await grant("policy.update", role="adult", access="allow")
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("hello")
    assert "policy_update" not in names(model.calls[-1].tools)

    reply = await adult_chat.say_with_tool(
        "policy_update", {"key": "confirmation_max_lifetime_hours", "value": 24}
    )
    assert reply.actions == [
        {"tool": "policy_update", "status": "refused", "reason": "not_allowed"}
    ]
    assert dict((await current_settings()).owner_values) == {}
