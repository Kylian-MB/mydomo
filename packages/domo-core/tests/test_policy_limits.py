"""The limits (spec 5.5, 5.7, 5.8 and 5.11): a CEL expression that the barrier evaluates on the
facts of the moment, and that answers ``allow``, ``refuse``, ``ask_owner`` or ``judge``.

A limit lets an action through, refuses it (``limit``, with the limit's summary, which Domo may
say), or sends it to the owner (with the question Domo could not answer, when the limit needs
understanding). Whatever goes wrong refuses (``limit_error``), is logged and audited: a limit is
applied by the code and fails closed. No limit of the catalog reads the length of a stay (audit
POL-5): a stay of any length, or with no end, goes on unless the owner wrote a limit on it himself.
The household's time zone is an owner's setting: without it, a limit cannot read the hour.

The barrier is pure, and tried on facts made here (``facts_of``). What the facts read from the
database is tried on the shared rolled-back ``session``; the chat pilots talk with Domo on a
live server, with rights written by the ``grant`` fixture (see ``chat_support.py``).
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from chat_support import live_http_client, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.chat.models import MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import decide_confirmation, requester_context
from domo_core.confirmations.store import approver_of
from domo_core.household.models import Invitation
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, NeedsApproval, Refuse, decide
from domo_core.policy.catalog import ACTION_FACTS, DEFAULT_LIMITS, DEFAULT_POLICY
from domo_core.policy.decision import decide_now
from domo_core.policy.facts import decision_facts, facts_of
from domo_core.policy.limits import (
    MAX_EXPRESSION_LENGTH,
    InvalidLimit,
    check_limit,
    compile_limit,
)
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import NO_RIGHTS, Limit, Right, RightsTable
from domo_core.policy.roles import Actor
from domo_core.policy.settings import (
    InvalidSetting,
    PolicySettings,
    validate_setting,
)
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import func, select, update

NOW = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)  # a Tuesday
PAUL_LIMIT = (
    'action.facts.role == "guest" && action.facts.days != null && action.facts.days <= 3 '
    '? "allow" : "refuse"'
)
PAUL_SUMMARY = "Paul invites guests, 3 days at most."
NO_NOTE_FOR_A_GUEST = "Adults leave no note for a guest."
# The catalog's limit of inviting (the owner's trial of 0.9.1): the owner's yes first when Domo
# judged so; no length of stay.
INVITING = (
    "When the assistant judges that the owner should say yes before someone joins, the "
    "invitation waits for the owner's yes."
)
NEW_PASSWORD = "a-brand-new-passphrase"


def invite(role: str, days: int | None, *, owner_first: bool = False) -> DescribedAction:
    """An invitation as ``household_invite`` describes it."""
    return DescribedAction(
        "core",
        "household.invite",
        None,
        "permissions",
        "household",
        {"role": role, "days": days, "display_name": "Dana", "owner_first": owner_first},
    )


def note_for(role: str) -> DescribedAction:
    """A note as ``note_leave`` describes it, for someone of ``role``."""
    return DescribedAction(
        "core",
        "note.leave",
        "person:x",
        "message",
        "household",
        {"recipient": "person:x", "recipient_role": role, "length": 9},
    )


def with_limit(
    actor: Actor,
    action_type: str,
    expression: str,
    *,
    judge_question: str | None = None,
    summary: str = PAUL_SUMMARY,
) -> RightsTable:
    """The rights of a test: ``actor`` may do ``action_type``, under this limit."""
    limit = Limit(expression, judge_question, summary)
    return RightsTable(
        (
            Right(
                uuid4(),
                actor.person_id,
                None,
                action_type,
                "allow",
                None,
                limit,
                None,
                None,
                summary,
            ),
        )
    )


def moment(actor: Actor, action: DescribedAction, **more) -> dict[str, object]:
    """The facts of a moment for a pure decision (no time zone)."""
    return facts_of(actor, action, name="Paul", local_now=None, **more)


# --- what the barrier does with a limit ----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "days", "expected"),
    [
        ("guest", 2, Allow()),
        ("guest", 5, Refuse("limit", limit=PAUL_SUMMARY)),
        ("adult", None, Refuse("limit", limit=PAUL_SUMMARY)),
    ],
)
def test_a_limit_lets_through_or_refuses(role, days, expected):
    paul = Actor(uuid4(), "adult")
    action = invite(role, days)
    rights = with_limit(paul, "household.invite", PAUL_LIMIT)
    assert decide(paul, action, DEFAULT_POLICY, rights, moment(paul, action)) == expected


def test_a_limit_that_asks_the_owner_needs_the_owners_agreement():
    paul = Actor(uuid4(), "adult")
    note = note_for("adult")
    rights = with_limit(paul, "note.leave", '"ask_owner"')
    assert decide(paul, note, DEFAULT_POLICY, rights, moment(paul, note)) == (
        NeedsApproval("owner")
    )
    action = invite("guest", 2)
    rights = with_limit(paul, "household.invite", '"ask_owner"')
    assert decide(paul, action, DEFAULT_POLICY, rights, moment(paul, action)) == (
        NeedsApproval("owner")
    )
    # The owner's own request never waits for the owner: asking is their agreement.
    owner = Actor(uuid4(), "owner")
    rights = with_limit(owner, "note.leave", '"ask_owner"')
    assert decide(owner, note, DEFAULT_POLICY, rights, moment(owner, note)) == Allow()


def test_a_judgement_goes_to_the_owner_with_the_question():
    """What needs understanding is the judge's first, with the question; when it doubts, it goes
    to the owner (spec 5.7, "Domo judges first" part 13)."""
    paul = Actor(uuid4(), "adult")
    action = invite("guest", 2)
    question = "Is Dana a friend of Paul?"
    rights = with_limit(paul, "household.invite", '"judge"', judge_question=question)
    assert decide(paul, action, DEFAULT_POLICY, rights, moment(paul, action)) == (
        NeedsApproval("owner", question=question, judge_first=True)
    )
    # Asking the owner straight away says the question too, when the right has one.
    rights = with_limit(paul, "household.invite", '"ask_owner"', judge_question=question)
    assert decide(paul, action, DEFAULT_POLICY, rights, moment(paul, action)) == (
        NeedsApproval("owner", question=question)
    )


@pytest.mark.parametrize(
    "expression",
    [
        'action.facts.age > 3 ? "allow" : "refuse"',  # a fact the action does not have
        '"maybe"',  # not one of the four answers
        "true",
        'action.facts.days > "3" ? "allow" : "refuse"',  # a type CEL does not compare
        'now.hour < 20 ? "allow" : "refuse"',  # no time zone: no hour
        "1 +",  # stored by hand: does not compile
        '[1, 2].exists(x, x > 1) ? "allow" : "refuse"',  # a loop
    ],
)
def test_a_limit_that_fails_refuses(expression):
    paul = Actor(uuid4(), "adult")
    action = invite("guest", 2)
    rights = with_limit(paul, "household.invite", expression)
    assert decide(paul, action, DEFAULT_POLICY, rights, moment(paul, action)) == Refuse(
        "limit_error"
    )


def test_a_limit_without_the_facts_of_the_moment_refuses():
    paul = Actor(uuid4(), "adult")
    rights = with_limit(paul, "household.invite", '"allow"')
    assert decide(paul, invite("guest", 2), DEFAULT_POLICY, rights) == Refuse("limit_error")
    assert decide(paul, invite("guest", 2), DEFAULT_POLICY, rights, None) == Refuse("limit_error")


def test_an_identity_that_cannot_be_trusted_is_never_sent_to_the_owner():
    claimed = Actor(uuid4(), "guest", trusted=False)
    rename = DescribedAction("core", "profile.rename", None, "self_profile", "household")
    rights = with_limit(claimed, "profile.rename", '"ask_owner"')
    assert decide(claimed, rename, DEFAULT_POLICY, rights, moment(claimed, rename)) == Refuse(
        "untrusted_identity"
    )
    rights = with_limit(claimed, "profile.rename", '"allow"')
    assert decide(claimed, rename, DEFAULT_POLICY, rights, moment(claimed, rename)) == Allow()


# --- writing a limit -----------------------------------------------------------------------------


def test_compile_limit_refuses_an_expression_too_long_or_not_cel():
    longest = '"allow"' + " " * (MAX_EXPRESSION_LENGTH - len('"allow"'))
    assert compile_limit(longest).expression == longest
    for expression in (
        longest + " ",
        "",
        "1 +",
        'action.facts.role == "guest" ?',
        # Nothing but the facts of the moment.
        'lists.range(3) == [] ? "allow" : "refuse"',
        'cel.bind(x, 1, x) == 1 ? "allow" : "refuse"',
        # Only the functions a limit needs.
        '"ab".repeat(3) == "" ? "allow" : "refuse"',
        # No loop: the facts hold no list, and a loop within a loop could keep the core busy for
        # minutes.
        '[1, 2].all(x, x > 0) ? "allow" : "refuse"',
        '[1, 2].map(actor, actor) == [] ? "allow" : "refuse"',
        '[1].filter(x, true) == [] ? "allow" : "refuse"',
        '[1].exists_one(x, true) ? "allow" : "refuse"',
        '[1]. // a comment\n map(x, x) == [] ? "allow" : "refuse"',
    ):
        with pytest.raises(InvalidLimit):
            compile_limit(expression)
    # Compiled once per expression.
    assert compile_limit(PAUL_LIMIT) is compile_limit(PAUL_LIMIT)


def test_check_limit_refuses_an_unknown_fact_and_the_hour_without_a_time_zone():
    check_limit(PAUL_LIMIT, "household.invite", time_zone_set=False)
    with pytest.raises(InvalidLimit):
        check_limit(
            'action.facts.age > 3 ? "allow" : "refuse"', "household.invite", time_zone_set=True
        )
    hour = 'now.hour >= 8 && now.hour < 20 ? "allow" : "refuse"'
    check_limit(hour, "household.invite", time_zone_set=True)
    with pytest.raises(InvalidLimit):
        check_limit(hour, "household.invite", time_zone_set=False)
    # Even where it would not be read on the facts tried.
    with pytest.raises(InvalidLimit):
        check_limit(
            'actor.role == "child" && now.hour > 20 ? "refuse" : "allow"',
            "note.leave",
            time_zone_set=False,
        )
    with pytest.raises(InvalidLimit):
        check_limit('"allow"', "x.unknown", time_zone_set=True)
    with pytest.raises(InvalidLimit):
        check_limit('"perhaps"', "note.leave", time_zone_set=True)
    with pytest.raises(InvalidLimit):
        check_limit("1 +", "note.leave", time_zone_set=True)
    # What the household and the history hold.
    check_limit(
        "household.people.guest + household.invitations.guest < 2 && history.week < 3 "
        '&& actor.name != "" ? "allow" : "ask_owner"',
        "household.invite",
        time_zone_set=False,
    )


def test_check_limit_refuses_a_number_read_without_a_guard_against_a_missing_value():
    # An adult's invitation has no stay (days is null): unguarded, every one of them would be
    # refused for a limit that cannot answer.
    for unguarded in (
        'action.facts.days <= 3 ? "allow" : "refuse"',
        'action.facts.role != "adult" && action.facts.days <= 3 ? "allow" : "refuse"',
    ):
        with pytest.raises(InvalidLimit, match=r"!= null"):
            check_limit(unguarded, "household.invite", time_zone_set=False)
    with pytest.raises(InvalidLimit, match=r"!= null"):
        check_limit(
            'action.facts.current_role == "guest" && action.facts.days > 3 ? "refuse" : "allow"',
            "household.change_role",
            time_zone_set=False,
        )
    # A guest's stay may have no end: read for a guest only, it is guarded all the same.
    with pytest.raises(InvalidLimit, match=r"!= null"):
        check_limit(
            'action.facts.role == "guest" && action.facts.days <= 3 ? "allow" : "refuse"',
            "household.invite",
            time_zone_set=False,
        )
    # Guarded, it is fine.
    check_limit(PAUL_LIMIT, "household.invite", time_zone_set=False)
    check_limit(
        'action.facts.days != null && action.facts.days <= 3 ? "allow" : "refuse"',
        "household.invite",
        time_zone_set=False,
    )
    # A note's length is a number too: a limit reads it the same way.
    with pytest.raises(InvalidLimit, match=r"!= null"):
        check_limit(
            'action.facts.length > 200 ? "refuse" : "allow"', "note.leave", time_zone_set=False
        )
    check_limit(
        'action.facts.length != null && action.facts.length > 200 ? "refuse" : "allow"',
        "note.leave",
        time_zone_set=False,
    )


def test_the_facts_of_every_action_type_can_be_read():
    for action_type, facts in ACTION_FACTS.items():
        for name in facts:
            check_limit(
                f'has(action.facts.{name}) ? "allow" : "refuse"', action_type, time_zone_set=False
            )


# --- the catalog's default limits (spec 5.11) ---------------------------------------------------


def test_the_catalog_s_default_limits_read_no_length_of_stay():
    assert {action_type: limit.summary for action_type, limit in DEFAULT_LIMITS.items()} == {
        # The owner's yes first when Domo judged so (0.9.1).
        "household.invite": INVITING,
        # Giving a link again follows the rights of inviting (``RIGHTS_OF``): the same limit (a
        # new link never asks the owner first).
        "household.invite_again": INVITING,
    }
    for action_type, limit in DEFAULT_LIMITS.items():
        assert limit.judge_question is None
        check_limit(limit.expression, action_type, time_zone_set=False)


def test_a_default_limit_applies_without_a_right_and_a_right_replaces_it():
    alex, owner = Actor(uuid4(), "adult"), Actor(uuid4(), "owner")
    # Alex has the right to invite, with no limit of his own: the catalog's applies to him as it
    # does to the owner.
    may_invite = RightsTable(
        (
            Right(
                uuid4(),
                alex.person_id,
                None,
                "household.invite",
                "allow",
                None,
                None,
                None,
                None,
                "Yes",
            ),
        )
    )
    # No stay is refused by the catalog: any length, or none (audit POL-5).
    for actor, rights in ((alex, may_invite), (owner, NO_RIGHTS)):
        for days in (30, 31, 400, None):
            action = invite("guest", days)
            assert decide(actor, action, DEFAULT_POLICY, rights, moment(actor, action)) == Allow()
    # A right of the owner's carries a limit on the stay of his own: it applies.
    summary = "Alex invites guests for 60 days at most."
    limited = with_limit(
        alex,
        "household.invite",
        'action.facts.days != null && action.facts.days > 60 ? "refuse" : "allow"',
        summary=summary,
    )
    long_stay = invite("guest", 61)
    assert decide(alex, long_stay, DEFAULT_POLICY, limited, moment(alex, long_stay)) == Refuse(
        "limit", limit=summary
    )
    adult = invite("adult", None)
    assert decide(owner, adult, facts=moment(owner, adult)) == Allow()
    for action_type, facts in (
        ("household.change_role", {"person": "person:x", "current_role": "adult"}),
        ("household.bring_back", {"person": "person:x"}),
    ):
        action = DescribedAction(
            "core",
            action_type,
            "person:x",
            "permissions",
            "household",
            {**facts, "role": "guest", "days": 400},
        )
        assert decide(owner, action, facts=moment(owner, action)) == Allow()


def _may_invite(actor: Actor) -> RightsTable:
    """The owner opens inviting to ``actor`` (it is in no role: ruling 14)."""
    right = Right(
        uuid4(), actor.person_id, None, "household.invite", "allow", None, None, None, None, "."
    )
    return RightsTable((right,))


def test_an_adult_invites_by_the_owner_s_right_and_domo_may_send_it_to_the_owner_first():
    """Inviting is in no role (ruling 14): an adult's own call is refused, unless the owner opened
    it to them. Then their invitation goes on (their own child, say), and waits for the owner when
    Domo judged it deserves it (``owner_first``). The owner's own goes on, whatever the stay. A
    child's and a guest's are refused, as before."""
    dana, owner = Actor(uuid4(), "adult"), Actor(uuid4(), "owner")
    child = invite("child", None)
    assert decide(dana, child, facts=moment(dana, child)) == Refuse("not_allowed")
    rights = _may_invite(dana)
    assert decide(dana, child, DEFAULT_POLICY, rights, moment(dana, child)) == Allow()
    stranger = invite("adult", None, owner_first=True)
    assert decide(dana, stranger, DEFAULT_POLICY, rights, moment(dana, stranger)) == (
        NeedsApproval("owner")
    )
    assert decide(owner, stranger, facts=moment(owner, stranger)) == Allow()
    long_stay = invite("guest", 400, owner_first=True)
    assert decide(dana, long_stay, DEFAULT_POLICY, rights, moment(dana, long_stay)) == (
        NeedsApproval("owner")
    )
    assert decide(owner, long_stay, DEFAULT_POLICY, rights, moment(owner, long_stay)) == Allow()
    for role in ("child", "guest"):
        someone = Actor(uuid4(), role)
        for action in (child, stranger):
            assert decide(someone, action, facts=moment(someone, action)) == Refuse("not_allowed")


def test_a_new_link_reads_the_limit_of_inviting_with_its_own_facts():
    """Giving a link again follows the rights of inviting (``RIGHTS_OF``): the limit read for it is
    inviting's, and it finds every fact there (a new link never asks the owner first)."""
    dana = Actor(uuid4(), "adult")
    again = DescribedAction(
        "core",
        "household.invite_again",
        "invitation:x",
        "permissions",
        "household",
        {"role": "adult", "days": None, "display_name": "Sam", "owner_first": False},
    )
    assert decide(dana, again, DEFAULT_POLICY, _may_invite(dana), moment(dana, again)) == Allow()
    check_limit(
        DEFAULT_LIMITS["household.invite"].expression,
        "household.invite_again",
        time_zone_set=False,
    )


# --- the facts of the moment ---------------------------------------------------------------------


async def test_decision_facts_read_the_person_the_household_and_what_they_did(
    session, add_person, add_conversation, add_message
):
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "child")
    await add_person("Robin", "guest")
    session.add_all(
        [
            Person(display_name="Gone", role="guest", expires_at=NOW - timedelta(days=1)),
            Person(
                display_name="Left", role="adult", disabled_at=NOW - timedelta(days=1), state="gone"
            ),
        ]
    )
    for name, role, days, expires, cancelled in (
        ("Dana", "guest", 2, NOW + timedelta(days=1), None),
        ("Tom", "adult", None, NOW + timedelta(days=1), None),
        ("Old", "child", None, NOW - timedelta(days=1), None),  # expired
        ("No", "guest", 2, NOW + timedelta(days=1), NOW),  # cancelled
    ):
        session.add(
            Invitation(
                token_hash=uuid4().hex,
                display_name=name,
                role=role,
                guest_days=days,
                created_by=owner.id,
                created_at=NOW - timedelta(days=2),
                expires_at=expires,
                cancelled_at=cancelled,
            )
        )
    alex_reply = await add_message(await add_conversation(alex), role="assistant")
    sam_reply = await add_message(await add_conversation(sam), role="assistant")
    for message, action_type, status, ago in (
        (alex_reply, "note.leave", "done", timedelta(hours=1)),
        (alex_reply, "note.leave", "done", timedelta(days=3)),
        (alex_reply, "note.leave", "done", timedelta(days=20)),
        (alex_reply, "note.leave", "done", timedelta(days=40)),  # too long ago
        (alex_reply, "household.list", "done", timedelta(hours=1)),  # another type
        (alex_reply, "note.leave", "refused", timedelta(hours=1)),  # not done
        (sam_reply, "note.leave", "done", timedelta(hours=1)),  # someone else
    ):
        session.add(
            MessageAction(
                message_id=message.id,
                tool_name="x",
                action_type=action_type,
                status=status,
                summary={},
                created_at=NOW - ago,
            )
        )
    requests = {}
    for action_type, status in (
        ("note.leave", "executed"),  # done after a yes
        ("note.leave", "refused"),
        ("household.list", "executed"),
    ):
        requests[action_type, status] = Confirmation(
            requester_id=alex.id,
            approver_kind="self",
            conversation_id=alex_reply.conversation_id,
            tool_name="note_leave",
            arguments={},
            action={"action_type": action_type},
            status=status,
            created_at=NOW - timedelta(hours=3),
            decided_at=NOW - timedelta(hours=2),
        )
    session.add_all(requests.values())
    await session.flush()
    # A yes in words leaves a row under the reply that took it: the request is counted once.
    session.add(
        MessageAction(
            message_id=alex_reply.id,
            tool_name="note_leave",
            action_type="note.leave",
            status="done",
            confirmation_id=requests["note.leave", "executed"].id,
            summary={},
            created_at=NOW - timedelta(hours=2),
        )
    )
    await session.flush()

    action = note_for("child")
    facts = await decision_facts(
        session, Actor(alex.id, "adult"), action, NOW, ZoneInfo("Europe/Paris")
    )
    assert facts == {
        "actor": {"id": f"person:{alex.id}", "role": "adult", "name": "Alex"},
        "action": {"type": "note.leave", "target": "person:x", "facts": dict(action.facts)},
        "now": {"hour": 13, "minute": 30, "weekday": 2, "date": "2030-01-01"},
        "household": {
            "people": {"owner": 1, "adult": 1, "child": 1, "guest": 1},
            "invitations": {"owner": 0, "adult": 1, "child": 0, "guest": 1},
        },
        "history": {"day": 2, "week": 3, "month": 4},
        # What programs declared spending for her (Task 8): nothing yet.
        "spent": {"day": 0, "week": 0, "month": 0},
    }
    # Without a time zone, there is no hour to read.
    assert "now" not in await decision_facts(session, Actor(alex.id, "adult"), action, NOW, None)


async def test_a_limit_on_the_hour_follows_the_households_time_zone(
    session_factory, add_member, grant, set_setting
):
    await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult")
    await grant("note.leave", person=alex, limit='now.hour < 12 ? "allow" : "refuse"')
    actor, note = Actor(alex.id, "adult"), note_for("adult")
    late = datetime(2030, 1, 1, 22, 0, tzinfo=UTC)  # 23:00 in Paris, 11:00 the next day in Auckland

    async def decided() -> object:
        async with session_factory() as reader:
            return await decide_now(reader, actor, note, DEFAULT_POLICY, late)

    assert await decided() == Refuse("limit_error")  # no time zone: never guessed
    await set_setting("household.time_zone", "Europe/Paris")
    assert await decided() == Refuse("limit", limit="test right")
    await set_setting("household.time_zone", "Pacific/Auckland")
    assert await decided() == Allow()


def test_the_time_zone_is_an_owner_setting():
    validate_setting("household.time_zone", "Europe/Paris")
    validate_setting("household.time_zone", None)
    for value in ("Mars/Olympus", "Europe", "", "Europe/Paris ", "../etc/passwd", 2, True):
        with pytest.raises(InvalidSetting):
            validate_setting("household.time_zone", value)
    assert PolicySettings({}).time_zone is None
    chosen = PolicySettings({"household.time_zone": "Europe/Paris"})
    assert chosen.time_zone == ZoneInfo("Europe/Paris")
    assert {"key": "household.time_zone", "value": "Europe/Paris", "source": "owner"} in (
        chosen.rows()
    )
    assert "household.time_zone" not in {row["key"] for row in PolicySettings({}).rows()}


async def test_the_history_can_leave_out_the_request_a_yes_is_carrying_out(
    session, add_person, add_conversation, add_message
):  # Ruling R23
    alex = await add_person("Alex", "adult")
    reply = await add_message(await add_conversation(alex), role="assistant")
    actor, action = Actor(alex.id, "adult"), note_for("child")

    def request(hours: int) -> Confirmation:
        return Confirmation(
            requester_id=alex.id,
            approver_kind="self",
            conversation_id=reply.conversation_id,
            tool_name="note_leave",
            arguments={},
            action={"action_type": "note.leave"},
            status="executed",
            created_at=NOW - timedelta(hours=hours + 1),
            decided_at=NOW - timedelta(hours=hours),
        )

    carried, earlier = request(2), request(5)
    session.add_all([carried, earlier])
    await session.flush()
    session.add_all(
        [
            # The row a yes in words leaves under its reply: the request's, not counted twice.
            MessageAction(
                message_id=reply.id,
                tool_name="note_leave",
                action_type="note.leave",
                status="done",
                confirmation_id=carried.id,
                summary={},
                created_at=NOW - timedelta(hours=2),
            ),
            # A note the barrier let through at once.
            MessageAction(
                message_id=reply.id,
                tool_name="note_leave",
                action_type="note.leave",
                status="done",
                summary={},
                created_at=NOW - timedelta(hours=1),
            ),
        ]
    )
    await session.flush()

    async def history(not_counting=None) -> dict[str, int]:
        facts = await decision_facts(session, actor, action, NOW, None, not_counting=not_counting)
        return facts["history"]  # type: ignore[return-value]

    three = {"day": 3, "week": 3, "month": 3}
    assert await history() == three
    # The request being carried out is counted zero times (not once less than its two rows) ...
    assert await history(carried.id) == {"day": 2, "week": 2, "month": 2}
    # ... and the others of the same person still count, whichever is left out.
    assert await history(earlier.id) == {"day": 2, "week": 2, "month": 2}
    assert await history(uuid4()) == three


# --- failing closed ----------------------------------------------------------------------------


async def test_a_limit_error_is_logged_and_audited(
    caplog, adult_chat, add_member, grant, last_audit_entry, count_notes
):
    await add_member("Sam", "child", username="sam")
    await grant("note.leave", role="adult", limit='action.facts.age > 3 ? "allow" : "refuse"')
    with caplog.at_level(logging.ERROR, logger="domo_core.policy.decision"):
        reply = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Hello"})
    assert reply.actions == [{"tool": "note_leave", "status": "refused", "reason": "limit_error"}]
    assert reply.model_results[-1]["reason"] == "limit_error"
    assert await count_notes() == 0
    logged = [r for r in caplog.records if r.name == "domo_core.policy.decision"]
    assert logged and all(r.levelno == logging.ERROR for r in logged)
    # The log says which action, never the facts.
    assert all("person:" not in r.getMessage() and "Sam" not in r.getMessage() for r in logged)
    decided = await last_audit_entry("policy.decision")
    assert (decided.details["decision"], decided.details["reason"]) == ("refuse", "limit_error")


async def test_a_yes_refused_for_a_limit_that_could_not_be_checked_tells_domo_to_say_so(
    adult_chat, add_member, grant, session_factory, chat_server, clock
):  # Ruling R12
    """The replay of a yes refuses with the same words for Domo as a call that was refused
    at once: the check could not be made, and the person may ask the owner."""
    await add_member("Sam", "child", username="sam")
    right = await grant("note.leave", role="adult", ask="yes")
    waiting = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Hello"})
    assert waiting.actions == [{"tool": "note_leave", "status": "waiting"}]
    async with session_factory() as writer:
        await writer.execute(
            update(PolicyRight)
            .where(PolicyRight.id == right.id)
            .values(limit_expression='action.facts.age > 3 ? "allow" : "refuse"')
        )
        await writer.commit()

    at_once = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Again"})
    direct = at_once.model_results[-1]
    assert direct["reason"] == "limit_error" and "ask the owner" in direct["detail"]

    base = ToolContext(
        session_factory=session_factory,
        actor=Actor(adult_chat.person.id, "adult"),
        person=adult_chat.person,
        conversation_id=uuid4(),
        origin=chat_server.base_url,
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )
    outcome, record = await decide_confirmation(
        session_factory=session_factory,
        confirmation_id=waiting.pending_confirmation_id,
        decider=adult_chat.person,
        approve=True,
        via="button",
        reply_message_id=None,
        ctx_factory=requester_context(base),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        now=clock(),
    )
    assert outcome == "refused" and record is not None
    assert record.model_result == {
        "status": "refused",
        "reason": "limit_error",
        "detail": direct["detail"],
    }


def test_a_limit_that_could_not_be_checked_says_the_person_may_ask_the_owner():  # Ruling R12
    for key in ("confirmation.outcome.limit_error", "action.card.refused.limit_error"):
        assert "ask the owner" in t(key, "en"), key


async def test_when_the_rights_cannot_be_read_nothing_runs(
    adult_chat, monkeypatch, display_name_of
):  # Ruling R9
    async def unreadable(session):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr("domo_core.policy.decision.load_rights", unreadable)
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    assert reply.actions == [{"tool": "profile_rename", "status": "failed"}]
    assert await display_name_of(adult_chat.person) == "Alex"


def test_an_approver_kind_nobody_knows_is_never_read_as_an_adult():  # Ruling R9
    with pytest.raises(ValueError):
        approver_of(Confirmation(approver_kind="nobody"))
    for kind in ("self", "adult", "owner"):
        assert approver_of(Confirmation(approver_kind=kind)) == kind


# --- with Domo ---------------------------------------------------------------------------------


async def test_a_note_for_a_guest_is_left_and_the_owner_can_still_limit_it(
    adult_chat, guest_member, grant, count_notes
):
    # No default limit: a guest who is there receives a note (see ``test_notes_upkeep.py``).
    reply = await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Robin", "text": "Bienvenue"}, judge="do"
    )
    assert reply.actions == [{"tool": "note_leave", "status": "done"}]
    assert await count_notes() == 1
    # The owner may write a limit of their own, which reads who the note is for.
    await grant(
        "note.leave",
        role="adult",
        limit='action.facts.recipient_role == "guest" ? "refuse" : "allow"',
        summary=NO_NOTE_FOR_A_GUEST,
    )
    reply = await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Robin", "text": "Bienvenue"}
    )
    assert reply.model_results[-1] == {
        "status": "refused",
        "reason": "limit",
        "limit": NO_NOTE_FOR_A_GUEST,
    }
    assert reply.actions == [{"tool": "note_leave", "status": "refused", "reason": "limit"}]
    assert t("action.card.refused.limit", "en") in reply.cards_html
    assert await count_notes() == 1


async def test_no_stay_is_refused_by_default_and_the_owner_can_limit_it(
    owner_chat, grant, count_invitations, session_factory
):
    longer = {"display_name": "Dana", "role": "guest", "guest_days": 400}
    card = await owner_chat.confirm_last(await owner_chat.say_with_tool("household_invite", longer))
    assert card.status == "executed"
    async with session_factory() as reader:
        assert (await reader.execute(select(Invitation.guest_days))).scalar_one() == 400
    # The owner's own limit on the stay applies to his own request too.
    summary = "Guests stay 60 days at most."
    await grant(
        "household.invite",
        role="owner",
        limit='action.facts.days != null && action.facts.days > 60 ? "refuse" : "allow"',
        summary=summary,
    )
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Tom", "role": "guest", "guest_days": 61}
    )
    assert reply.model_results[-1] == {"status": "refused", "reason": "limit", "limit": summary}
    assert await count_invitations() == 1


async def test_the_owner_sees_what_domo_could_not_tell(
    adult_chat, live_owner_client, grant, count_invitations
):
    question = "Is Dana a friend of Alex?"
    await grant(
        "household.invite",
        person=adult_chat.person,
        access="allow",
        limit='"judge"',
        judge_question=question,
        summary="Alex invites her friends only.",
    )
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "guest"}
    )
    assert reply.model_results[-1]["by"] == "the owner"
    zone = (await live_owner_client.get("/approvals")).text
    assert t("confirmation.ask_reason", "en", question=question) in zone
    answer = await live_owner_client.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(answer) == "executed"
    assert await count_invitations() == 1


async def test_a_limit_is_checked_again_when_the_yes_comes(
    adult_chat, guest_member, grant, chat_server, count_invitations, last_audit_entry
):  # Review Focus 1
    """Alex may invite guests while the household has fewer than two. Two invitations wait; the
    first one is accepted and its guest joins; the yes to the second is refused."""
    await grant(
        "household.invite",
        person=adult_chat.person,
        access="allow",
        limit='action.facts.role == "guest" && household.people.guest < 2 ? "allow" : "refuse"',
        summary="Alex invites guests while there are fewer than two.",
    )
    first = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "guest"}
    )
    second = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Tom", "role": "guest"}
    )
    assert first.actions == second.actions == [{"tool": "household_invite", "status": "waiting"}]

    card = await adult_chat.confirm_last(first)
    assert card.status == "executed" and card.invitation_token is not None
    async with live_http_client(chat_server.base_url) as dana:
        joined = await dana.post(
            f"/invite/{card.invitation_token}",
            data={"username": "dana", "password": NEW_PASSWORD},
        )
        assert joined.status_code == 303

    card = await adult_chat.confirm_last(second)
    assert card.status == "refused"
    assert t("confirmation.outcome.limit", "en") in card.html
    assert await count_invitations() == 1
    decided = await last_audit_entry("policy.decision")
    assert (decided.details["decision"], decided.details["reason"]) == ("refuse", "limit")


# --- a request is decided again with the facts of the yes (Ruling R6) ----------------------------


async def test_the_facts_of_a_waiting_request_are_read_again_at_the_yes(
    adult_chat, add_member, grant, set_role, count_notes
):
    sam = await add_member("Sam", "child", username="sam")
    await grant(
        "note.leave",
        role="adult",
        approver="self",
        limit='action.facts.recipient_role == "guest" ? "refuse" : "allow"',
        summary=NO_NOTE_FOR_A_GUEST,
    )
    reply = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Hello"})
    assert reply.actions == [{"tool": "note_leave", "status": "waiting"}]
    # Sam is a guest now: the request was described for a child, the yes comes for a guest.
    await set_role(sam, "guest")
    card = await adult_chat.confirm_last(reply)
    assert card.status == "refused"
    assert t("confirmation.outcome.limit", "en") in card.html
    assert await count_notes() == 0


async def test_a_yes_to_a_call_that_no_longer_describes_the_same_action_is_refused(
    adult_chat, add_member, grant, session_factory, count_notes
):
    sam = await add_member("Sam", "child", username="sam")
    await grant("note.leave", role="adult", approver="self")
    gone = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Hello"})
    other = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Again"})
    # Sam leaves; then someone else has the name.
    async with session_factory() as writer:
        found = await writer.execute(select(Person).where(Person.id == sam.id))
        left = found.scalar_one()
        left.disabled_at = adult_chat.clock()
        left.state = "gone"
        await writer.commit()
    card = await adult_chat.confirm_last(gone)
    assert card.status == "refused"
    assert t("confirmation.outcome.no_longer_possible", "en") in card.html
    await add_member("Sam", "adult")
    card = await adult_chat.confirm_last(other)
    assert card.status == "refused"
    assert t("confirmation.outcome.no_longer_possible", "en") in card.html
    assert await count_notes() == 0


# --- a rule raised since applies to the requests that wait (Ruling R7, F21) ----------------------


async def test_a_waiting_request_follows_a_rule_raised_since(adult_chat, grant, display_name_of):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    assert reply.actions == [{"tool": "profile_rename", "status": "waiting"}]
    # A plain yes was asked; the owner then asks the button or "I confirm" for it.
    await grant("profile.rename", role="adult", ask="explicit_yes")
    model = adult_chat.use_model(FakeChatModel(["Say “I confirm”."]))
    await adult_chat.say("oui")
    assert len(model.calls) == 1 and await display_name_of(adult_chat.person) == "Alex"
    await adult_chat.say("je confirme")
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_the_history_counts_what_was_done_after_a_yes_too(owner_chat, grant, session_factory):
    await grant(
        "household.invite",
        role="owner",
        limit='history.day < 1 ? "allow" : "refuse"',
        summary="One invitation a day.",
    )
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Dana", "role": "adult"}
        )
    )
    assert card.status == "executed"
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Tom", "role": "adult"}
    )
    assert reply.model_results[-1] == {
        "status": "refused",
        "reason": "limit",
        "limit": "One invitation a day.",
    }
    async with session_factory() as reader:
        assert (
            await reader.execute(select(func.count()).select_from(Invitation))
        ).scalar_one() == 1
