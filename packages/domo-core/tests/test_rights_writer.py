"""The rights writer (spec 5.6): an isolated model turns the owner's words into a draft of rights,
and the code checks the draft on examples whose outcome it works out itself.

These tests need no database: the facts of the moment come from ``facts_of`` with a made-up
household (``WriterContext.facts_for``). The tests with Domo and the drafts in the database are
in ``test_rights_tools.py``.
"""

import asyncio
import dataclasses
import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from domo_common.i18n import t
from domo_core.confirmations.describe import describe_example, describe_rights_draft
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    ACTION_FACTS,
    CHANGES_NOTHING,
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    DOMO_DEVICE_ACTIONS,
    DOMO_HOUSEHOLD_ACTIONS,
    DOMO_NOT_BY_DEFAULT,
    JUDGED_FACTS,
    NOT_ON_HIS_OWN,
    OWNER_ONLY_ACTIONS,
    RIGHTS_OF,
)
from domo_core.policy.facts import facts_of
from domo_core.policy.rights import NO_RIGHTS, Limit, Right, RightsTable
from domo_core.policy.rights_drafts import draft_as_json, stored_draft
from domo_core.policy.rights_writer import (
    EXAMPLE_OUTCOMES,
    MADE_UP_PERSON,
    WRITER_FIRST_LINE,
    WRITER_TOOL,
    Draft,
    DraftRefused,
    Example,
    RightChange,
    WriterAsks,
    WriterContext,
    WriterTooSlow,
    check_draft,
    example_outcome,
    write_draft,
    writer_context_json,
    writer_prompt,
)
from domo_core.policy.roles import Actor, Role
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import RefusesWhateverTheRights
from domo_core.tools.registry import TOOLS
from fakes import FakeCall, FakeChatModel

OWNER = UUID("00000000-0000-0000-0000-00000000000a")
ALEX = UUID("00000000-0000-0000-0000-00000000000b")
SAM = UUID("00000000-0000-0000-0000-00000000000c")
PEOPLE: tuple[tuple[UUID, str, Role], ...] = (
    (OWNER, "Owner", "owner"),
    (ALEX, "Alex", "adult"),
    (SAM, "Sam", "child"),
)
NAMES = {person_id: name for person_id, name, _ in PEOPLE}
# Paul left the household: removed on the 30th of December (id, name, the role they had, the day).
PAUL = UUID("00000000-0000-0000-0000-00000000000d")
FORMER: tuple[tuple[UUID, str, Role, str], ...] = ((PAUL, "Paul", "adult", "2029-12-30"),)

# What the tools always refuse, whatever the rights: their own words (Ruling R19).
FIXED_REFUSALS = {
    tool.action_type: tool.fixed_refusal
    for tool in TOOLS.values()
    if isinstance(tool, RefusesWhateverTheRights)
}

# Alex may invite guests, three days at most (the spec's example, 5.5); a guest's stay may have no
# end, so the days are read only when they are there.
THREE_DAYS = (
    'action.facts.role == "guest" && action.facts.days != null && action.facts.days <= 3 '
    '? "allow" : "refuse"'
)
WORDS = "Alex peut inviter des invités, 3 jours au plus"
# The time each call to the writer has (the owner's setting, ``rights_writer.seconds``, is 60).
SECONDS = 60.0


def context(
    *,
    rights: RightsTable = NO_RIGHTS,
    paused: tuple[Right, ...] = (),
    time_zone_set: bool = False,
    local_now: datetime | None = None,
    former: tuple[tuple[UUID, str, Role, str], ...] = (),
) -> WriterContext:
    """The writer's context for the made-up household: the owner, Alex (adult) and Sam (child).
    ``local_now`` is the real moment in the household's time zone (none by default); ``former``
    are the people who left."""

    async def facts_for(actor: Actor, action: DescribedAction) -> Mapping[str, object]:
        # A made-up person of a role (an example for a role nobody has) has its role as name.
        return facts_of(
            actor,
            action,
            name=NAMES.get(actor.person_id, actor.role),
            local_now=local_now,
            people={"owner": 1, "adult": 1, "child": 1},
        )

    return WriterContext(
        persons=PEOPLE,
        rights=rights,
        paused=paused,
        policy=DEFAULT_POLICY,
        time_zone_set=time_zone_set,
        facts_for=facts_for,
        fixed_refusals=FIXED_REFUSALS,
        former=former,
    )


def change(**fields: Any) -> RightChange:
    values: dict[str, Any] = {
        "person_id": ALEX,
        "role": None,
        "action_type": "household.invite",
        "remove": False,
        "access": "allow",
        "approver": None,
        "limit_expression": THREE_DAYS,
        "judge_question": None,
        "ask": None,
        "ask_when": None,
        "summary": "Alex peut inviter des invités, 3 jours au plus.",
    }
    values.update(fields)
    return RightChange(**values)


def invite(
    role: str,
    days: int | None,
    expected: str,
    *,
    by: UUID | None = ALEX,
    as_role: Role | None = None,
    owner_first: bool = False,
) -> Example:
    return Example(
        person_id=by,
        action_type="household.invite",
        facts={
            "role": role,
            "days": days,
            "no_end": False,
            "display_name": "Dana",
            "owner_first": owner_first,
        },
        expected=expected,  # type: ignore[arg-type]
        role=as_role,
    )


# The draft of the spec's example, as the code expects it: three examples, one that passes, and
# one the default limit stops (it sends to the owner an invitation the assistant judged should wait
# for his yes), whose outcome shows whether it still holds.
GOOD_DRAFT = Draft(
    changes=(change(),),
    examples=(
        invite("guest", 2, "domo_judges"),
        invite("adult", None, "refused"),
        invite("guest", 2, "domo_judges", owner_first=True),
    ),
)


def as_arguments(draft: Draft) -> dict[str, Any]:
    """A draft as the writer sends it, the arguments of its ``submit_rights`` call."""

    def plain(value: Any) -> Any:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Mapping):
            return {k: plain(v) for k, v in value.items()}  # pyright: ignore
        return value

    return {
        "changes": [
            {
                # The writer says "domo" of Domo's own right.
                ("domo" if name == "for_domo" else name): plain(value)
                for name, value in dataclasses.asdict(c).items()
            }
            for c in draft.changes
        ],
        "examples": [
            {
                "person_id": None if e.person_id is None else str(e.person_id),
                "role": e.role,
                **({"who": "domo"} if e.for_domo else {}),
                "action_type": e.action_type,
                "facts": dict(e.facts),
                "expected": e.expected,
            }
            for e in draft.examples
        ],
    }


def submit(arguments: dict[str, Any], call_id: str = "w1") -> ToolCall:
    return ToolCall(call_id, "submit_rights", json.dumps(arguments))


# --- what the code checks ------------------------------------------------------------------------


async def test_a_good_draft_passes_the_checks():
    assert await check_draft(GOOD_DRAFT, context()) == []


@pytest.mark.parametrize("owner_action", ["rights.apply", "rights.propose", "policy.update"])
async def test_a_draft_that_gives_away_the_owners_actions_is_refused(owner_action):
    gives = "change 1: changing the rules is the owner's alone: no right gives it"
    for given in (
        change(action_type=owner_action, limit_expression=None),
        change(action_type=owner_action, access=None, approver="adult", limit_expression=None),
        change(action_type=owner_action, access=None),  # a limit
        change(action_type=owner_action, access="allow", limit_expression=None, ask="yes"),
    ):
        draft = Draft(
            changes=(given,),
            examples=(
                Example(OWNER, "household.list", {}, "domo_judges"),  # something else
            ),
        )
        assert gives in await check_draft(draft, context())
    # What to ask, alone, is a rule of the owner's for himself (F4): it is not refused as giving
    # his actions away (rights.propose changes nothing: nothing is asked for it).
    rule = change(
        person_id=OWNER, action_type=owner_action, access=None, limit_expression=None, ask="yes"
    )
    errors = await check_draft(
        Draft((rule,), (Example(OWNER, owner_action, {}, "asks_them"),)), context()
    )
    assert gives not in errors
    assert errors == (
        ["change 1: nothing is asked for an action that changes nothing"]
        if owner_action == "rights.propose"
        else []
    )
    # An example of the owner's own actions is about the owner, never someone else.
    with_example = Draft((rule,), (Example(ALEX, owner_action, {}, "domo_judges"),))
    errors = await check_draft(with_example, context())
    assert any(error.startswith("example 1: ") for error in errors)


SILENT_ONLY_ACCESS = (
    "change 1: an action that is never shown (the assistant's memory, his plans, the name he is "
    "called "
    "by, his look at what reached someone, what he notes of someone's word, the tools he makes for "
    "himself) takes access alone: no approver, no limit"
)


@pytest.mark.parametrize(
    "silent",
    [
        "memory.keep",
        "plan.set",
        "profile.name_domo",
        "people.nickname_keep",
        "people.nickname_forget",
        "delivery.look",
        "trust.note",
        "trust.look",
        "trust.correct",
    ],
)
async def test_a_rule_on_a_silent_action_says_access_alone(silent):
    # Final review I3: nobody would see a request for a yes on what is never shown.
    def errors_of(given: RightChange) -> Any:
        draft = Draft((given,), (Example(OWNER, "household.list", {}, "domo_judges"),))
        return check_draft(draft, context())

    # An ask: refused, as for anything that changes nothing.
    asks = await errors_of(change(action_type=silent, limit_expression=None, ask="yes"))
    assert "change 1: nothing is asked for an action that changes nothing" in asks
    # An approver: refused.
    approver = await errors_of(
        change(action_type=silent, access=None, approver="adult", limit_expression=None)
    )
    assert SILENT_ONLY_ACCESS in approver
    # A limit: refused.
    limited = await errors_of(change(action_type=silent, access=None, limit_expression='"allow"'))
    assert SILENT_ONLY_ACCESS in limited
    # Access alone (here, taking it away from Alex) is a rule the owner may write.
    access = await errors_of(change(action_type=silent, access="deny", limit_expression=None))
    assert SILENT_ONLY_ACCESS not in access
    # (The only thing said of it: no example of its type was given here.)
    assert [error for error in access if error.startswith("change 1")] == [
        "change 1: no example shows it: give one of its type"
    ]


@pytest.mark.parametrize("silent", ["tool.build", "tool.change", "tool.launch_when"])
async def test_a_rule_on_a_tool_of_domo_says_access_alone_though_it_changes_something(silent):
    # Never shown and never judged, whatever it changes: no yes is ever asked for it.
    def errors_of(given: RightChange) -> Any:
        draft = Draft((given,), (Example(OWNER, "household.list", {}, "domo_judges"),))
        return check_draft(draft, context())

    asks = await errors_of(change(action_type=silent, limit_expression=None, ask="yes"))
    assert SILENT_ONLY_ACCESS in asks
    approver = await errors_of(
        change(action_type=silent, access=None, approver="adult", limit_expression=None)
    )
    assert SILENT_ONLY_ACCESS in approver
    access = await errors_of(change(action_type=silent, access="deny", limit_expression=None))
    assert SILENT_ONLY_ACCESS not in access


async def test_a_limit_on_the_hour_without_a_time_zone_is_refused():
    evening = 'now.hour < 20 ? "allow" : "refuse"'
    draft = dataclasses.replace(
        GOOD_DRAFT, changes=(change(action_type="note.leave", limit_expression=evening),)
    )
    errors = await check_draft(draft, context())
    assert any(
        error.startswith("change 1: ") and "time zone must be set first" in error
        for error in errors
    )
    # Once the owner has set the time zone, the expression itself is fine.
    errors = await check_draft(draft, context(time_zone_set=True))
    assert not any("time zone" in error for error in errors)


async def test_a_limit_that_goes_through_a_list_is_sent_back_with_why():  # Ruling R11
    loop = '[1, 2].exists(x, x > 1) ? "allow" : "refuse"'
    errors = await check_draft(
        dataclasses.replace(GOOD_DRAFT, changes=(change(limit_expression=loop),)), context()
    )
    assert any(
        error.startswith("change 1: ") and "does not go through a list" in error for error in errors
    )


async def test_a_limit_that_replaces_a_default_one_shows_a_case_the_default_refuses():
    # Ruling R10: Alex's limit replaces the default one of inviting. Without an example the default
    # stops, the owner could not see whether it still holds.
    assert "household.invite" in DEFAULT_LIMITS
    without = dataclasses.replace(
        GOOD_DRAFT,
        examples=(
            invite("guest", 2, "domo_judges"),
            invite("adult", None, "refused"),
            invite("guest", 3, "domo_judges"),
        ),
    )
    errors = await check_draft(without, context())
    assert errors == [
        "change 1: its limit replaces the default limit of its action type: an example must "
        "show a case the default limit stops (refuses, or sends to the owner)"
    ]
    # A change without a limit keeps the default: no such example is needed.
    no_limit = Draft(
        changes=(change(limit_expression=None, summary="Alex may invite people."),),
        examples=(invite("guest", 2, "domo_judges"),),
    )
    assert await check_draft(no_limit, context()) == []


@pytest.mark.parametrize(
    ("examples", "error"),
    [
        (
            (invite("guest", 2, "domo_judges"), invite("adult", None, "refused")),
            "the draft needs at least 3 examples when a change has a limit",
        ),
        (
            (invite("guest", 2, "domo_judges"), invite("guest", 1, "domo_judges"),
             invite("guest", 3, "domo_judges")),
            "the examples need one that passes (domo_judges or asks_...) and another that is "
            "stopped (refused or asks_the_owner)",
        ),
    ],
)  # fmt: skip
async def test_a_limit_needs_three_examples_one_that_passes_and_one_that_does_not(examples, error):
    draft = dataclasses.replace(GOOD_DRAFT, examples=examples)
    assert error in await check_draft(draft, context())


async def test_an_example_whose_outcome_the_code_does_not_find_is_an_error():
    wrong = dataclasses.replace(
        GOOD_DRAFT,
        examples=(
            invite("guest", 2, "asks_them"),  # nothing is imposed: Domo judges
            invite("adult", None, "refused"),
            invite("guest", 2, "domo_judges", owner_first=True),
        ),
    )
    assert await check_draft(wrong, context()) == [
        "example 1: expected asks_them, but the code finds domo_judges"
    ]


@pytest.mark.parametrize(
    ("draft", "error"),
    [
        (
            Draft((change(person_id=uuid4()),), GOOD_DRAFT.examples),
            "change 1: person_id is the id of a person of the household (people)",
        ),
        (
            Draft((change(role="adult"),), GOOD_DRAFT.examples),
            "change 1: the subject is a person_id, a role or domo, one at most (none of them: "
            "everyone)",
        ),
        (
            # A right for nobody in particular is a rule for everyone: what to ask, alone.
            Draft((change(person_id=None, role=None),), GOOD_DRAFT.examples),
            "change 1: a rule for everyone says what to ask and nothing else: ask alone",
        ),
        (
            Draft((change(action_type="household.delete"),), GOOD_DRAFT.examples),
            "change 1: action_type is a type of the catalog",
        ),
        (
            Draft((change(summary=""),), GOOD_DRAFT.examples),
            "change 1: the summary is one line of 1 to 300 characters",
        ),
        (
            Draft((change(summary="x" * 301),), GOOD_DRAFT.examples),
            "change 1: the summary is one line of 1 to 300 characters",
        ),
        (
            Draft((change(summary="two\nlines"),), GOOD_DRAFT.examples),
            "change 1: the summary is one line of 1 to 300 characters",
        ),
        (
            Draft(
                (change(limit_expression='action.facts.role == "guest" ? "judge" : "refuse"'),),
                GOOD_DRAFT.examples,
            ),
            'change 1: a limit that may answer "judge" needs a judge_question',
        ),
        (
            Draft(
                (change(limit_expression=None, judge_question="Is Dana a friend?"),),
                GOOD_DRAFT.examples,
            ),
            "change 1: a judge_question only goes with a limit",
        ),
        (
            Draft((change(remove=True),), GOOD_DRAFT.examples),
            "change 1: a change that removes a right says nothing else",
        ),
        (
            Draft((change(remove=True, access=None, limit_expression=None),), GOOD_DRAFT.examples),
            "change 1: there is no right of this subject for this action type to remove",
        ),
        (
            Draft((change(access=None, limit_expression=None),), GOOD_DRAFT.examples),
            "change 1: a right says at least one thing: access, approver, a limit or ask",
        ),
        (
            Draft((change(ask="yes", ask_when="seulement pour les invités"),), GOOD_DRAFT.examples),
            "change 1: ask_when goes with ask judge",
        ),
        (
            Draft((change(ask="judge", ask_when="x" * 301),), GOOD_DRAFT.examples),
            "change 1: ask_when is one line of 1 to 300 characters",
        ),
        (
            Draft((change(ask="judge", ask_when="two\nlines"),), GOOD_DRAFT.examples),
            "change 1: ask_when is one line of 1 to 300 characters",
        ),
        (
            Draft((change(ask="never"),), GOOD_DRAFT.examples),
            "change 1: never is only for the owner's own actions (owner_only): for anything else "
            "write judge",
        ),
        (
            Draft(
                (change(remove=True, access=None, limit_expression=None, ask="yes"),),
                GOOD_DRAFT.examples,
            ),
            "change 1: a change that removes a right says nothing else",
        ),
        (
            Draft((change(), change()), GOOD_DRAFT.examples),
            "change 2: the same subject and action type as change 1",
        ),
        (
            Draft(
                GOOD_DRAFT.changes,
                (*GOOD_DRAFT.examples, Example(uuid4(), "household.list", {}, "domo_judges")),
            ),
            "example 4: person_id is the id of a person of the household (people)",
        ),
        (
            Draft(
                GOOD_DRAFT.changes,
                (
                    *GOOD_DRAFT.examples,
                    Example(ALEX, "household.invite", {"role": "guest"}, "refused"),
                ),
            ),
            "example 4: facts are exactly the facts of its action type",
        ),
        (
            Draft(
                GOOD_DRAFT.changes,
                (
                    *GOOD_DRAFT.examples,
                    Example(
                        ALEX,
                        "household.invite",
                        {
                            "role": "captain",
                            "days": 2,
                            "no_end": False,
                            "display_name": "Dana",
                            "owner_first": False,
                        },
                        "refused",
                    ),
                ),
            ),
            "example 4: the fact role is not a role",
        ),
        (Draft((), GOOD_DRAFT.examples), "a draft changes at least one right"),
    ],
)
async def test_what_the_code_checks_in_a_draft(draft, error):
    assert error in await check_draft(draft, context())


async def test_a_right_can_be_taken_away_or_given_back():
    old = Right(
        uuid4(),
        ALEX,
        None,
        "household.invite",
        "allow",
        None,
        None,
        None,
        None,
        "Alex invites.",
    )
    # (Inviting is in no role, ruling 14: without the right, the catalog refuses it again.)
    removal = Draft(
        (
            change(
                remove=True, access=None, limit_expression=None, summary="Alex no longer invites."
            ),
        ),
        (invite("guest", 2, "refused"),),
    )
    assert await check_draft(removal, context(rights=RightsTable((old,)))) == []
    # A paused right may be taken away too, or given back by writing it again.
    assert await check_draft(removal, context(paused=(old,))) == []
    back = Draft(
        (change(limit_expression=None, summary="Alex invites."),),
        (invite("guest", 2, "domo_judges"),),
    )
    assert await check_draft(back, context(paused=(old,))) == []


# --- what an example comes to --------------------------------------------------------------------


def _right(action_type: str, **fields: Any) -> Right:
    values: dict[str, Any] = {
        "id": uuid4(),
        "person_id": None,
        "role": None,
        "action_type": action_type,
        "access": None,
        "approver": None,
        "limit": None,
        "ask": None,
        "ask_when": None,
        "summary": "a right",
    }
    values.update(fields)
    return Right(**values)


@pytest.mark.parametrize(
    ("example", "rights", "outcome"),
    [
        (Example(ALEX, "household.list", {}, "domo_judges"), (), "domo_judges"),
        # Inviting is in no role (ruling 14): an adult invites by the owner's right for them, and
        # Domo may send it to the owner first.
        (invite("guest", 2, "refused"), (), "refused"),
        (
            invite("guest", 2, "domo_judges", owner_first=True),
            (_right("household.invite", person_id=ALEX, access="allow"),),
            "asks_the_owner",
        ),
        (invite("guest", 2, "domo_judges", by=SAM), (), "refused"),  # a child does not
        (invite("guest", 2, "domo_judges", by=OWNER), (), "domo_judges"),
        (
            invite("guest", 2, "domo_judges", by=SAM),
            (_right("household.invite", role="child", access="allow"),),
            "asks_an_adult",
        ),
        (
            invite("guest", 2, "domo_judges", by=SAM),
            (_right("household.invite", role="child", access="allow", approver="owner"),),
            "asks_the_owner",
        ),
        (
            invite("guest", 2, "domo_judges", by=SAM),
            (
                _right(
                    "household.invite",
                    role="child",
                    access="allow",
                    limit=Limit('"ask_owner"', None, "The owner says yes."),
                ),
            ),
            "asks_the_owner",
        ),
        # An approver on a type the catalog leaves to the person asks for that agreement too.
        (
            Example(
                ALEX,
                "note.leave",
                {"recipient": f"person:{SAM}", "recipient_role": "child", "length": 5},
                "asks_an_adult",
            ),
            (_right("note.leave", role="adult", approver="adult"),),
            "asks_an_adult",
        ),
        # No default limit reads a stay (audit POL-5).
        (invite("guest", 400, "domo_judges", by=OWNER), (), "domo_judges"),
        # A limit that sends to the owner, for the owner himself: asking is his yes.
        (
            invite("guest", 2, "domo_judges", by=OWNER),
            (_right("household.invite", role="owner", limit=Limit('"ask_owner"', None, "Yes.")),),
            "domo_judges",
        ),
        # What a rule asks, whoever it is for.
        (
            invite("guest", 2, "asks_them", by=OWNER),
            (_right("household.invite", ask="yes"),),
            "asks_them",
        ),
        (
            invite("guest", 2, "asks_them", by=OWNER),
            (_right("household.invite", role="owner", ask="proof"),),
            "asks_them",
        ),
        # The guidance not to ask imposes nothing.
        (
            invite("guest", 2, "domo_judges", by=OWNER),
            (_right("household.invite", ask="judge"),),
            "domo_judges",
        ),
        # Applying rights asks nothing by default: Domo judges, with or without his guidance.
        (Example(OWNER, "rights.apply", {}, "domo_judges"), (), "domo_judges"),
        (
            Example(OWNER, "rights.apply", {}, "asks_them"),
            (_right("rights.apply", person_id=OWNER, ask="yes"),),
            "asks_them",
        ),
        (
            Example(OWNER, "rights.apply", {}, "domo_judges"),
            (_right("rights.apply", person_id=OWNER, ask="judge", ask_when="for guests"),),
            "domo_judges",
        ),
    ],
)
async def test_the_outcome_of_an_example_is_the_barriers(example, rights, outcome):
    empty = Draft((), ())
    assert await example_outcome(example, empty, context(rights=RightsTable(rights))) == outcome


async def test_the_writer_says_the_assistant_decides_when_he_knows():
    # Task 25: a limit that says "judge" is the assistant's when he knows the answer; the owner
    # reads the question only when he doubts. An example cannot show what he knows: it shows what
    # happens when he doubts.
    prompt = writer_prompt()
    assert "the assistant decides when he knows the answer" in prompt
    assert "for now it waits for the owner's yes" not in prompt
    assert "an example cannot show what he knows" in prompt
    limit = Limit('"judge"', "Is Sam a friend of Dana?", "a limit")
    judged = _right("household.invite", person_id=ALEX, access="allow", limit=limit)
    example = invite("guest", 2, "asks_the_owner")
    ctx = context(rights=RightsTable((judged,)))
    assert await example_outcome(example, Draft((), ()), ctx) == "asks_the_owner"


async def test_the_draft_is_laid_over_the_rights_in_force():
    # Alex's old right is replaced by the draft's for the same type: the outcome is the draft's.
    old = _right("household.invite", person_id=ALEX, access="allow", approver="owner")
    ctx = context(rights=RightsTable((old,)))
    example = invite("guest", 2, "domo_judges")
    assert await example_outcome(example, Draft((), ()), ctx) == "asks_the_owner"
    assert await example_outcome(example, GOOD_DRAFT, ctx) == "domo_judges"
    removal = Draft((change(remove=True, access=None, limit_expression=None),), ())
    # (Without a right, the catalog's: inviting is in no role, ruling 14.)
    assert await example_outcome(example, removal, ctx) == "refused"


# --- what the owner wants asked, or never asked (0.5.0) ------------------------------------------

REMOVE_RULE = "Demande-moi toujours un oui exprès avant de retirer quelqu'un."


def rule(**fields: Any) -> RightChange:
    """A change that only says what to ask: a rule for everyone, on removing someone, unless the
    ``fields`` say otherwise."""
    return change(
        **{
            "person_id": None,
            "action_type": "household.remove",
            "access": None,
            "limit_expression": None,
            "ask": "explicit_yes",
            "summary": REMOVE_RULE,
            **fields,
        }
    )


# What a role, a removal and a return say of the record they act on: a built-in role alone, an
# account, and where it stands.
ON_A_RECORD: dict[str, Any] = {"made_role": None, "has_account": True, "state": "has_access"}


def on_a_record(action_type: str, facts: dict[str, Any]) -> dict[str, Any]:
    """``facts`` with what an action of ``action_type`` says of the record it acts on, if any (one
    who left, for a return), and whether a stay it gives has no end (``no_end``, false unless
    ``facts`` say), for a type that says it."""
    if "no_end" in ACTION_FACTS.get(action_type, {}):
        facts = {"no_end": False, **facts}
    if action_type not in ("household.change_role", "household.remove", "household.bring_back"):
        return facts
    gone = {"state": "gone"} if action_type == "household.bring_back" else {}
    # Whether someone passing by now lives here, and what it changes (answer g, declared).
    living = (
        dict.fromkeys(
            ("comes_to_live", "off_the_guests_key_rule", "may_then_agree_for_a_child"), False
        )
        if action_type == "household.change_role"
        else {}
    )
    return {**living, **facts, **ON_A_RECORD, **gone}


def removes(person: UUID, expected: str, *, by: UUID = OWNER) -> Example:
    role = next(role for pid, _, role in PEOPLE if pid == person)
    facts = on_a_record(
        "household.remove", {"person": f"person:{person}", "current_role": role, "is_self": False}
    )
    return Example(by, "household.remove", facts, expected)  # type: ignore[arg-type]


def leaves(person: UUID, expected: str) -> Example:
    role = next(role for pid, _, role in PEOPLE if pid == person)
    return Example(person, "household.leave", {"current_role": role}, expected)  # type: ignore[arg-type]


def applies(expected: str, *, by: UUID = OWNER) -> Example:
    return Example(by, "rights.apply", {}, expected)  # type: ignore[arg-type]


def hands_over(expected: str) -> Example:
    facts = {
        "from": f"person:{OWNER}",
        "to": f"person:{ALEX}",
        "requester_role": "owner",
        "to_role": "adult",
    }
    return Example(OWNER, "household.transfer_ownership", facts, expected)  # type: ignore[arg-type]


def sets_successors(expected: str, *, by: UUID = OWNER) -> Example:
    return Example(by, "successors.set", {"count": 2}, expected)  # type: ignore[arg-type]


async def test_the_writer_writes_a_rule_when_the_owner_says_what_to_ask():
    # "Demande-moi toujours un oui exprès avant de retirer quelqu'un": a rule for everyone.
    given = {
        "changes": [
            {"action_type": "household.remove", "ask": "explicit_yes", "summary": REMOVE_RULE}
        ],
        "examples": [
            {
                "person_id": str(OWNER),
                "action_type": "household.remove",
                "facts": on_a_record(
                    "household.remove",
                    {"person": f"person:{ALEX}", "current_role": "adult", "is_self": False},
                ),
                "expected": "asks_them",
            }
        ],
    }
    writer = Writer(submit(given))
    draft = await write_draft(writer, REMOVE_RULE, context(), seconds=SECONDS)
    assert draft == Draft((rule(),), (removes(ALEX, "asks_them"),))
    # It is stored with what to ask, for nobody in particular, and no level.
    [stored] = draft_as_json(draft, context())["changes"]
    assert {k: stored[k] for k in ("person_id", "role", "ask", "ask_when")} == {
        "person_id": None,
        "role": None,
        "ask": "explicit_yes",
        "ask_when": None,
    }
    assert "level" not in stored
    # Without the rule, nothing is imposed: Domo judges.
    assert await example_outcome(removes(ALEX, "x"), Draft((), ()), context()) == "domo_judges"


async def test_an_example_says_domo_judges_when_nothing_is_asked():
    alex_invites = change(limit_expression=None, summary="Alex invite.")
    assert await example_outcome(
        invite("guest", 2, "x"), Draft((alex_invites,), ()), context()
    ) == ("domo_judges")
    asked = dataclasses.replace(alex_invites, ask="yes")
    assert await example_outcome(invite("guest", 2, "x"), Draft((asked,), ()), context()) == (
        "asks_them"
    )
    # Someone else's agreement the rights ask for stays: a child's leave, or a child a right
    # lets invite without naming who says yes (F1).
    assert await example_outcome(leaves(SAM, "x"), Draft((), ()), context()) == "asks_an_adult"
    sam_invites = change(person_id=SAM, limit_expression=None, summary="Sam invite.")
    assert await example_outcome(
        invite("guest", 2, "x", by=SAM), Draft((sam_invites,), ()), context()
    ) == ("asks_an_adult")
    # Applying rights asks nothing by default (2026-11-15): Domo judges, as with his guidance.
    assert await example_outcome(applies("x"), Draft((), ()), context()) == "domo_judges"
    lifted = rule(person_id=OWNER, action_type="rights.apply", ask="judge", summary="Juge.")
    assert await example_outcome(applies("x"), Draft((lifted,), ()), context()) == ("domo_judges")
    assert await check_draft(Draft((lifted,), (applies("domo_judges"),)), context()) == []


async def test_the_owner_says_not_to_ask_him_for_one_of_his_own_actions_as_he_likes():
    conditions = "sauf si ça donne plus de droits à un enfant"
    given = {
        "changes": [
            {
                "action_type": "rights.apply",
                "person_id": str(OWNER),
                "ask": "judge",
                "ask_when": conditions,
                "summary": "Applique mes droits sans me demander, sauf pour un enfant.",
            }
        ],
        "examples": [
            {
                "person_id": str(OWNER),
                "action_type": "rights.apply",
                "facts": {},
                "expected": "domo_judges",
            }
        ],
    }
    draft = await write_draft(Writer(submit(given)), conditions, context(), seconds=SECONDS)
    [lift] = draft.changes
    assert (lift.person_id, lift.role, lift.ask, lift.ask_when) == (
        OWNER,
        None,
        "judge",
        conditions,
    )
    # "Not at all" on handing his role over: no yes, by the judge or by Domo.
    never = rule(
        person_id=OWNER,
        action_type="household.transfer_ownership",
        ask="never",
        summary="Ne me demande plus jamais pour donner mon rôle.",
    )
    assert await check_draft(Draft((never,), (hands_over("domo_judges"),)), context()) == []
    assert await check_draft(Draft((never,), (hands_over("asks_them"),)), context()) == [
        "example 1: expected asks_them, but the code finds domo_judges"
    ]

    async def sent_back(change: RightChange, example: Example) -> list[str]:
        return await check_draft(Draft((change,), (example,)), context())

    # He takes it back the same way: a change that takes that right away (F25). Nothing is
    # asked by default then either: Domo judges, without his guidance.
    in_force = _right("rights.apply", person_id=OWNER, ask="judge", ask_when=conditions)
    back = rule(
        person_id=OWNER, action_type="rights.apply", ask=None, remove=True, summary="Redemande."
    )
    lifted = context(rights=RightsTable((in_force,)))
    assert await check_draft(Draft((back,), (applies("domo_judges"),)), lifted) == []
    assert await example_outcome(applies("x"), Draft((), ()), lifted) == "domo_judges"
    # Conditions go with "judge" only.
    with_yes = rule(person_id=OWNER, action_type="rights.apply", ask="yes", ask_when=conditions)
    assert "change 1: ask_when goes with ask judge" in await sent_back(with_yes, applies("x"))
    # His own yes, for himself: never for a role, nor for everyone (it would pass to the next
    # owner, who never decided it).
    himself = (
        "change 1: on his own actions, only the owner says not to ask him, for himself "
        "(his person_id)"
    )
    for subject in ({"role": "adult"}, {"role": "owner"}, {}):
        lift_for = rule(
            action_type="successors.set", ask="never", summary="Plus jamais.", **subject
        )
        assert himself in await sent_back(lift_for, sets_successors("domo_judges")), subject
        judged_for = dataclasses.replace(lift_for, ask="judge")
        assert himself in await sent_back(judged_for, sets_successors("domo_judges")), subject
    # "Not at all" is only for his own actions: elsewhere, the guidance is "judge".
    elsewhere = rule(person_id=OWNER, action_type="household.invite", ask="never")
    assert (
        "change 1: never is only for the owner's own actions (owner_only): for anything else "
        "write judge" in await sent_back(elsewhere, invite("guest", 2, "domo_judges", by=OWNER))
    )
    # Giving his actions away is refused as before.
    given_away = rule(person_id=OWNER, action_type="rights.apply", ask=None, access="allow")
    assert "change 1: changing the rules is the owner's alone: no right gives it" in (
        await sent_back(given_away, applies("asks_them"))
    )
    # And an example of his actions is the owner's own.
    about_alex = rule(person_id=OWNER, action_type="successors.set", ask="judge", summary="Juge.")
    errors = await sent_back(about_alex, sets_successors("refused", by=ALEX))
    assert errors == [
        "example 1: an example of the owner's own actions is the owner asking (his person_id)"
    ]


async def test_the_owners_own_actions_take_a_rule_that_asks_for_him_his_role_or_everyone():
    for subject in ({"role": "owner"}, {}, {"person_id": OWNER}):
        asks = rule(
            action_type="rights.apply", ask="explicit_yes", summary="Oui exprès.", **subject
        )
        assert await check_draft(Draft((asks,), (applies("asks_them"),)), context()) == [], subject
    # Nobody else does them: a rule for someone else is no rule.
    for_alex = rule(person_id=ALEX, action_type="rights.apply", ask="explicit_yes")
    assert await check_draft(Draft((for_alex,), (applies("asks_them"),)), context()) == [
        "change 1: a rule on the owner's own actions is for the owner: his person_id, the role "
        "owner, or everyone"
    ]


async def test_a_rule_on_what_changes_nothing_is_sent_back():
    reading = rule(action_type="household.list", ask="yes", summary="Demande avant de lister.")
    example = Example(ALEX, "household.list", {}, "domo_judges")
    assert await check_draft(Draft((reading,), (example,)), context()) == [
        "change 1: nothing is asked for an action that changes nothing"
    ]


@pytest.mark.parametrize("action_type", ["chain.continue", "confirmation.amend"])
@pytest.mark.parametrize("ask", ["yes", "explicit_yes", "proof", "judge", "never"])
async def test_a_rule_on_what_carries_other_actions_is_sent_back(action_type, ask):
    """Review I3: taking a chain up again, or changing a request, is judged inside, by what it
    carries; a rule on it would never be read. The owner's rule goes on those actions."""
    reading = rule(action_type=action_type, ask=ask, summary="Demande avant.")
    example = Example(ALEX, action_type, {}, "asks_them")
    assert await check_draft(Draft((reading,), (example,)), context()) == [
        "change 1: nothing is asked for an action that carries others (taking a chain up again, "
        "changing a request): write the rule on the actions it carries"
    ]


async def test_a_rule_for_everyone_with_more_than_ask_is_sent_back():
    more = "change 1: a rule for everyone says what to ask and nothing else: ask alone"
    for extra in (
        {"access": "allow"},
        {"approver": "owner"},
        {"limit_expression": '"allow"'},
        {"ask": None, "access": "deny"},
    ):
        draft = Draft((rule(**extra),), (removes(ALEX, "asks_them"),))
        assert more in await check_draft(draft, context()), extra
    nothing = Draft((rule(ask=None),), (removes(ALEX, "domo_judges"),))
    assert more in await check_draft(nothing, context())
    # Taking it away again is a removal, without ask (F25).
    in_force = _right("household.remove", ask="explicit_yes", summary=REMOVE_RULE)
    back = Draft(
        (rule(ask=None, remove=True, summary="Plus de oui exprès pour retirer."),),
        (removes(ALEX, "domo_judges"),),
    )
    assert await check_draft(back, context(rights=RightsTable((in_force,)))) == []
    assert await example_outcome(
        removes(ALEX, "x"), Draft((), ()), context(rights=RightsTable((in_force,)))
    ) == ("asks_them")


def test_the_writers_text_speaks_of_asks_not_levels():
    prompt = writer_prompt()
    for needed in (
        "`ask`",
        "`ask_when`",
        '"never"',
        '"judge"',
        '"explicit_yes"',
        '"proof"',
        "Nothing is asked for them by default",
        "owner_only",
        "never choose one yourself",
        "domo_judges",
        "asks_them",
        "everyone",
        # (Task 25: a limit "judge" is the assistant's when he knows, the owner's when he doubts.)
        "the assistant decides when he knows the answer",
    ):
        assert needed in prompt, needed
    for gone in ("always_confirm", '"free"', "`level`"):
        assert gone not in prompt, gone


# --- the sentence of an example ------------------------------------------------------------------


def test_the_example_sentence_comes_from_the_facts_not_from_the_writer():
    # The writer gives facts, the moment it imagines and an outcome, never a sentence.
    assert {f.name for f in dataclasses.fields(Example)} == {
        "person_id",
        "role",
        "action_type",
        "facts",
        "expected",
        "moment",
        "for_domo",
    }
    facts = {
        "role": "guest",
        "days": 2,
        "no_end": False,
        "display_name": "Dana",
        "owner_first": False,
    }
    for lang in ("en", "fr"):
        assert describe_example(
            "household.invite", facts, who="Alex", names={}, texts=CatalogTexts(lang)
        ) == t(
            "rights.example.household.invite",
            lang,
            who="Alex",
            role=t("household.role.guest", lang),
            days=t("policy.duration.days", lang, count="2"),
            no_end=t("rights.example.no", lang),
            display_name="Dana",
            owner_first=t("rights.example.no", lang),
        )
    # A person is told by their name, or as someone the code does not know.
    note = {"recipient": f"person:{SAM}", "seen": False}
    sentence = describe_example(
        "note.withdraw", note, who="Alex", names={str(SAM): "Sam"}, texts=CatalogTexts("en")
    )
    assert "Sam" in sentence and str(SAM) not in sentence
    unknown = describe_example(
        "note.withdraw", note, who="Alex", names={}, texts=CatalogTexts("en")
    )
    assert t("rights.example.someone", "en") in unknown and str(SAM) not in unknown


def test_every_action_type_has_an_example_sentence():
    for action_type in DEFAULT_POLICY.known:
        key = f"rights.example.{action_type}"
        # Every fact of the type has its place in the sentence, and the person who asks too.
        # (The exception of a directive is the assistant's call, never what a right is about.)
        facts = ACTION_FACTS[action_type]
        names = {
            "who": "W",
            **{name: f"<{name}>" for name in facts if name not in JUDGED_FACTS},
        }
        sentence = t(key, "en", **names)
        assert all(value in sentence for value in names.values()), key
    for outcome in EXAMPLE_OUTCOMES:
        assert t(f"rights.example.outcome.{outcome}", "en")
    assert EXAMPLE_OUTCOMES == (
        "domo_judges",
        "asks_them",
        "asks_an_adult",
        "asks_the_owner",
        "refused",
    )
    assert t("rights.example.outcome.domo_judges", "en") == "The assistant judges whether to ask."
    assert t("rights.example.outcome.asks_them", "en") == "Asks them first, as the rule says."


def test_facts_that_do_not_fit_the_sentence_get_the_general_one():
    sentence = describe_example(
        "household.invite", {"role": "captain"}, who="Alex", names={}, texts=CatalogTexts("en")
    )
    assert sentence == t("rights.example.generic", "en", who="Alex")


def test_the_card_of_a_draft_says_each_right_and_each_example_in_the_readers_language():
    content = draft_as_json(GOOD_DRAFT, context())
    for lang in ("en", "fr"):
        lines = describe_rights_draft(content, CatalogTexts(lang))
        assert lines is not None
        assert lines.summaries == ("Alex peut inviter des invités, 3 jours au plus.",)
        assert [example.outcome for example in lines.examples] == [
            t("rights.example.outcome.domo_judges", lang),
            t("rights.example.outcome.refused", lang),
            t("rights.example.outcome.domo_judges", lang),
        ]
        assert lines.examples[0].situation == describe_example(
            "household.invite",
            GOOD_DRAFT.examples[0].facts,
            who="Alex",
            names={},
            texts=CatalogTexts(lang),
        )
    assert describe_rights_draft({"changes": "nope"}, CatalogTexts("en")) is None


# --- the writer ----------------------------------------------------------------------------------


class Writer:
    """A writer model: each call takes the next answer (a ``ToolCall``, a text, or an exception
    to raise), and records what it received."""

    def __init__(self, *answers: ToolCall | str | Exception) -> None:
        self._answers = list(answers)
        self.calls: list[FakeCall] = []

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        self.calls.append(
            FakeCall(system, list(messages), tuple(tools), max_output_tokens, tool_choice)
        )
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        yield ChatChunk(answer) if isinstance(answer, str) else answer
        yield ChatUsage(3, 2)


async def test_the_writer_is_one_isolated_call_with_the_words_and_the_context():
    writer = Writer(submit(as_arguments(GOOD_DRAFT)))
    draft = await write_draft(writer, WORDS, context(), seconds=SECONDS)
    assert draft == GOOD_DRAFT
    [call] = writer.calls
    assert call.system == writer_prompt()
    assert call.system.startswith(WRITER_FIRST_LINE)
    assert call.tools == (WRITER_TOOL,)
    [message] = call.messages
    assert isinstance(message, ChatMessage) and message.role == "user"
    # The owner's words come first, then the context, as JSON.
    assert message.content.index(WORDS) < message.content.index("{")
    shown = json.loads(message.content[message.content.index("{") :])
    assert {"id": str(ALEX), "name": "Alex", "role": "adult"} in shown["people"]
    invite_type = next(a for a in shown["catalog"] if a["action_type"] == "household.invite")
    assert invite_type["facts"] == {
        "role": "role",
        "days": "number",
        "no_end": "boolean",
        "display_name": "text",
        "owner_first": "boolean",
    }
    # Ruling R10: the default limits are listed, with what they say.
    assert invite_type["default_limit"] == {
        "expression": DEFAULT_LIMITS["household.invite"].expression,
        "summary": DEFAULT_LIMITS["household.invite"].summary,
    }
    # What says how a yes is asked for: whether a child needs an adult's agreement, and whether
    # the type changes nothing (no rule asks anything for it). No level any more.
    assert "level" not in invite_type and "changes_nothing" not in invite_type
    assert invite_type["adult_agrees_for_a_child"] is True
    by_type = {a["action_type"]: a for a in shown["catalog"]}
    assert by_type["household.list"]["changes_nothing"] is True
    assert "adult_agrees_for_a_child" not in by_type["note.leave"]
    # The owner's own actions are there, marked (F4); none asks a yes by default any more.
    assert {a for a, entry in by_type.items() if entry.get("owner_only")} == OWNER_ONLY_ACTIONS
    assert "owner_only" not in invite_type
    assert all("default_ask" not in entry for entry in shown["catalog"])
    assert shown["time_zone_set"] is False
    # Ruling R15: the roles nobody has, for which an example may name the role.
    assert shown["roles_without_people"] == ["guest"]


async def test_the_writer_is_told_the_rights_in_force_and_those_that_are_paused():
    live = _right("note.leave", role="child", ask="yes", summary="Sam's notes wait.")
    everyone = _right(
        "household.remove", ask="judge", ask_when="only guests", summary="Removing, judged."
    )
    paused = _right("household.invite", person_id=ALEX, access="allow", summary="Alex invites.")
    writer = Writer(submit(as_arguments(GOOD_DRAFT)))
    await write_draft(
        writer,
        WORDS,
        context(rights=RightsTable((live, everyone)), paused=(paused,)),
        seconds=SECONDS,
    )
    content = writer.calls[0].messages[0].content  # type: ignore[union-attr]
    shown = json.loads(content[content.index("{") :])
    assert [r["summary"] for r in shown["rights"]] == ["Sam's notes wait.", "Removing, judged."]
    assert (shown["rights"][0]["role"], shown["rights"][0]["ask"]) == ("child", "yes")
    # A rule for everyone: no person, no role; what to ask, and the owner's conditions.
    assert {k: shown["rights"][1][k] for k in ("person_id", "role", "ask", "ask_when")} == {
        "person_id": None,
        "role": None,
        "ask": "judge",
        "ask_when": "only guests",
    }
    assert all("level" not in r for r in shown["rights"])
    assert [r["summary"] for r in shown["paused_rights"]] == ["Alex invites."]
    assert shown["paused_rights"][0]["person_id"] == str(ALEX)


# --- the people who left (spec 5.7) -------------------------------------------------------------


def pauls_right(**fields: Any) -> Right:
    """A right of Paul's, paused when they left."""
    return _right(
        "household.invite", person_id=PAUL, access="allow", summary="Paul invites.", **fields
    )


def paul_loses_the_right() -> Draft:
    """The draft of "Paul left, take away the right to invite": one change that removes, and an
    example about somebody who is here (the owner asks, and invites a guest for two days)."""
    return Draft(
        changes=(
            change(
                person_id=PAUL,
                remove=True,
                access=None,
                limit_expression=None,
                summary="Paul n'invite plus.",
            ),
        ),
        examples=(invite("guest", 2, "domo_judges", by=OWNER),),
    )


def test_the_writer_is_told_who_left_and_may_only_take_their_rights_away():
    paused = pauls_right()
    shown = writer_context_json(context(paused=(paused,), former=FORMER))
    # Marked as having left, with the role they had and the day, apart from the people here.
    assert shown["former_people"] == [
        {"id": str(PAUL), "name": "Paul", "role": "adult", "left_on": "2029-12-30"}
    ]
    assert str(PAUL) not in {person["id"] for person in shown["people"]}
    # Their paused right is told by their name, not as the right of nobody.
    assert shown["paused_rights"][0]["name"] == "Paul"
    assert writer_context_json(context())["former_people"] == []
    # The context knows them by id (to store a draft with their name), still not as present.
    assert context(former=FORMER).person(PAUL) == ("Paul", "adult")
    assert context().person(PAUL) is None


async def test_a_right_of_someone_who_left_is_taken_away_and_nothing_else():
    here = context(paused=(pauls_right(),), former=FORMER)
    assert await check_draft(paul_loses_the_right(), here) == []
    # Only taken away: giving, changing or giving back a right is for the people who are here.
    for given in (
        change(person_id=PAUL, limit_expression=None),
        change(person_id=PAUL, limit_expression=None, access=None, ask="yes"),
    ):
        draft = Draft(changes=(given,), examples=(invite("guest", 2, "domo_judges", by=OWNER),))
        assert await check_draft(draft, here) == [
            "change 1: a right for someone who left the household is only taken away"
        ]
    # A right they never had cannot be taken away; somebody nobody knows is nobody.
    assert await check_draft(paul_loses_the_right(), context(former=FORMER)) == [
        "change 1: there is no right of this subject for this action type to remove"
    ]
    nobody = Draft(
        changes=(change(person_id=uuid4(), remove=True, access=None, limit_expression=None),),
        examples=paul_loses_the_right().examples,
    )
    unknown = await check_draft(nobody, here)
    assert unknown[0].startswith("change 1: person_id is the id of a person of the household")


async def test_an_example_about_someone_who_left_is_refused():
    here = context(paused=(pauls_right(),), former=FORMER)
    draft = Draft(
        changes=paul_loses_the_right().changes,
        examples=(invite("guest", 2, "domo_judges", by=PAUL),),
    )
    [problem] = await check_draft(draft, here)
    assert problem.startswith("example 1: person_id is the id of a person of the household")


async def test_the_draft_of_someone_who_left_is_stored_with_their_name():
    here = context(paused=(pauls_right(),), former=FORMER)
    stored = draft_as_json(paul_loses_the_right(), here)
    assert stored["names"] == {str(PAUL): "Paul", str(OWNER): "Owner"}


async def test_the_writer_is_sent_back_once_for_a_right_given_to_someone_who_left():
    wrong = as_arguments(Draft(changes=(change(person_id=PAUL),), examples=GOOD_DRAFT.examples))
    right = as_arguments(paul_loses_the_right())
    writer = Writer(submit(wrong, "w1"), submit(right, "w2"))
    draft = await write_draft(
        writer, "Paul est parti", context(paused=(pauls_right(),), former=FORMER), seconds=SECONDS
    )
    assert draft == paul_loses_the_right()
    answer = writer.calls[1].messages[-1]
    assert isinstance(answer, ToolResult)
    assert "only taken away" in answer.content


def test_the_writers_text_speaks_of_the_people_who_left():
    prompt = writer_prompt()
    for needed in ("former_people", "paused", "remove", "no example"):
        assert needed in prompt, needed


def test_the_writers_text_says_what_it_must_know():
    prompt = writer_prompt()
    assert prompt.splitlines()[0] == WRITER_FIRST_LINE
    for needed in (
        '"allow"',
        '"refuse"',
        '"ask_owner"',
        '"judge"',
        "judge_question",
        "household.time_zone",
        "replaces",  # a right's limit replaces the default one (Ruling R10)
        "exists_one",  # no loops (Ruling R11)
        "language of the owner",
        "question",
    ):
        assert needed in prompt, needed


async def test_a_draft_whose_examples_disagree_is_sent_back_once_then_refused():
    wrong = as_arguments(GOOD_DRAFT)
    wrong["examples"][0]["expected"] = "asks_them"
    writer = Writer(submit(wrong, "w1"), submit(wrong, "w2"))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, WORDS, context(), seconds=SECONDS)
    assert refused.value.errors == ["example 1: expected asks_them, but the code finds domo_judges"]
    first, second = writer.calls
    # The second call is the same conversation, with the errors as the answer to the first call.
    assert second.messages[0] == first.messages[0]
    answer = second.messages[-1]
    assert isinstance(answer, ToolResult) and answer.call_id == "w1"
    assert "example 1: expected asks_them, but the code finds domo_judges" in answer.content


async def test_a_draft_sent_back_can_be_corrected():
    wrong = as_arguments(GOOD_DRAFT)
    wrong["examples"][0]["expected"] = "asks_them"
    writer = Writer(submit(wrong, "w1"), submit(as_arguments(GOOD_DRAFT), "w2"))
    assert await write_draft(writer, WORDS, context(), seconds=SECONDS) == GOOD_DRAFT
    assert len(writer.calls) == 2


async def test_a_limit_error_goes_back_to_the_writer_with_the_reason_and_not_to_domo():
    broken = as_arguments(GOOD_DRAFT)
    broken["changes"][0]["limit_expression"] = 'action.facts.role == "guest ? "allow"'
    writer = Writer(submit(broken, "w1"), submit(broken, "w2"))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, WORDS, context(), seconds=SECONDS)
    # The writer reads what CEL said; Domo only reads the code's words.
    answer = writer.calls[1].messages[-1]
    assert isinstance(answer, ToolResult) and "not CEL" in answer.content
    assert refused.value.errors == ["change 1: the limit cannot be used"]


async def test_the_writer_asks_when_the_owners_words_are_not_enough():
    writer = Writer(submit({"question": "Which Alex do you mean?"}))
    with pytest.raises(WriterAsks) as asks:
        await write_draft(writer, "Alex peut inviter", context(), seconds=SECONDS)
    assert asks.value.question == "Which Alex do you mean?"
    assert len(writer.calls) == 1


@pytest.mark.parametrize(
    "answer",
    [
        "I would rather not.",  # no call
        ModelCallError("unreachable"),
        ToolCall("w1", "household_invite", "{}"),  # another tool
        ToolCall("w1", "submit_rights", "[1, 2]"),  # not an object
        ToolCall("w1", "submit_rights", "{not json"),
        submit({"question": "Which?", **as_arguments(GOOD_DRAFT)}),  # both
        submit({}),  # neither
        submit({"changes": "everything", "examples": []}),
        submit({"question": ""}),
        submit({"question": "x" * 301}),
    ],
)
async def test_a_writer_that_answers_badly_is_refused_at_once(answer):
    writer = Writer(answer, submit(as_arguments(GOOD_DRAFT)))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, WORDS, context(), seconds=SECONDS)
    assert refused.value.errors
    assert len(writer.calls) == 1


async def test_two_calls_at_once_are_refused():
    class TwoCalls(Writer):
        async def stream(self, **kwargs: Any) -> AsyncGenerator[Any]:  # type: ignore[override]
            yield submit(as_arguments(GOOD_DRAFT), "w1")
            yield submit(as_arguments(GOOD_DRAFT), "w2")

    with pytest.raises(DraftRefused):
        await write_draft(TwoCalls(), WORDS, context(), seconds=SECONDS)


async def test_a_model_that_says_ok_writes_nothing():
    with pytest.raises(DraftRefused):
        await write_draft(FakeChatModel(["ok"]), WORDS, context(), seconds=SECONDS)


# --- a role nobody has yet (Ruling R15) ----------------------------------------------------------

# Guests may invite guests, three days at most: written while the household has no guest.
GUESTS_DRAFT = Draft(
    changes=(
        change(person_id=None, role="guest", summary="Les invités invitent, 3 jours au plus."),
    ),
    examples=(
        invite("guest", 2, "domo_judges", by=None, as_role="guest"),
        invite("adult", None, "refused", by=None, as_role="guest"),
        invite("guest", 40, "refused", by=None, as_role="guest"),
        invite("guest", 2, "domo_judges", by=None, as_role="guest", owner_first=True),
    ),
)


async def test_a_rule_for_guests_is_shown_while_there_is_no_guest():
    ctx = context()
    assert "guest" in ctx.roles_without_people()
    assert await check_draft(GUESTS_DRAFT, ctx) == []
    # Both outcomes are worked out, for a made-up guest.
    passes, stopped, longer, first = GUESTS_DRAFT.examples
    assert await example_outcome(passes, GUESTS_DRAFT, ctx) == "domo_judges"
    assert await example_outcome(stopped, GUESTS_DRAFT, ctx) == "refused"
    assert await example_outcome(longer, GUESTS_DRAFT, ctx) == "refused"
    # The right's limit replaces the default one of inviting: the owner sees it no longer holds.
    assert await example_outcome(first, GUESTS_DRAFT, ctx) == "domo_judges"
    # Without the draft, a guest may not invite at all.
    assert await example_outcome(passes, Draft((), ()), ctx) == "refused"
    # The writer gives it the same way, and the card names the role.
    writer = Writer(submit(as_arguments(GUESTS_DRAFT)))
    assert await write_draft(
        writer, "Les invités peuvent inviter, 3 jours au plus", ctx, seconds=SECONDS
    ) == (GUESTS_DRAFT)
    content = draft_as_json(GUESTS_DRAFT, ctx)
    assert content["examples"][0]["person_id"] is None
    assert content["examples"][0]["role"] == "guest"
    for lang in ("en", "fr"):
        lines = describe_rights_draft(content, CatalogTexts(lang))
        assert lines is not None
        assert lines.examples[0].situation == describe_example(
            "household.invite",
            passes.facts,
            who=t("rights.example.as_role.guest", lang),
            names={},
            texts=CatalogTexts(lang),
        )


async def test_a_made_up_person_has_no_rights_of_its_own():
    seen: list[Actor] = []
    ctx = context()

    async def facts_for(actor: Actor, action: DescribedAction) -> Mapping[str, object]:
        seen.append(actor)
        return await ctx.facts_for(actor, action)

    example = invite("guest", 2, "domo_judges", by=None, as_role="guest")
    await example_outcome(example, GUESTS_DRAFT, dataclasses.replace(ctx, facts_for=facts_for))
    assert seen == [Actor(MADE_UP_PERSON, "guest", visiting=True)]
    assert MADE_UP_PERSON not in NAMES


@pytest.mark.parametrize(
    ("example", "error"),
    [
        (
            invite("guest", 2, "domo_judges", by=ALEX, as_role="guest"),
            "example 4: who asks is a person_id, a role or who domo, exactly one",
        ),
        (
            invite("guest", 2, "domo_judges", by=None, as_role=None),
            "example 4: who asks is a person_id, a role or who domo, exactly one",
        ),
        (
            invite("guest", 2, "refused", by=None, as_role="adult"),  # Alex is an adult
            "example 4: a role is only for a role nobody in the household has "
            "(roles_without_people): give a person_id",
        ),
    ],
)
async def test_an_example_names_a_person_or_a_role_nobody_has(example, error):
    draft = dataclasses.replace(GOOD_DRAFT, examples=(*GOOD_DRAFT.examples, example))
    assert error in await check_draft(draft, context())


# --- the writer's time (Ruling R16) --------------------------------------------------------------


async def test_a_writer_that_never_answers_refuses_the_draft():
    class Silent(Writer):
        async def stream(self, **kwargs: Any) -> AsyncGenerator[Any]:  # type: ignore[override]
            await asyncio.sleep(3600)
            yield ChatChunk("never")

    with pytest.raises(DraftRefused) as refused:
        await write_draft(Silent(), WORDS, context(), seconds=0.05)
    assert refused.value.errors == ["the writer took too long"]


async def test_each_writer_call_has_the_time_of_the_setting():
    class Slow(Writer):
        """Takes ``delays[n]`` seconds to answer its call number ``n``."""

        def __init__(self, delays: Sequence[float], *answers: ToolCall | str | Exception) -> None:
            super().__init__(*answers)
            self.delays = list(delays)

        async def stream(self, **kwargs: Any) -> AsyncGenerator[Any]:  # type: ignore[override]
            await asyncio.sleep(self.delays[len(self.calls)])
            async for item in super().stream(**kwargs):
                yield item

    good = as_arguments(GOOD_DRAFT)
    wrong = as_arguments(GOOD_DRAFT)
    wrong["examples"][0]["expected"] = "asks_them"
    # A first call that takes longer than the time it has: the writer is too slow, which is a
    # refusal of the draft all the same, with the usual words.
    with pytest.raises(WriterTooSlow) as too_slow:
        await write_draft(Slow([0.5], submit(good)), WORDS, context(), seconds=0.05)
    assert isinstance(too_slow.value, DraftRefused)
    assert too_slow.value.errors == ["the writer took too long"]
    # Two calls of 0.03 s each (the first one wrong, so that it is sent back) take 0.06 s in all,
    # more than the 0.05 s of one call: each call has its own time (Ruling R16).
    quick = Slow([0.03, 0.03], submit(wrong, "w1"), submit(good, "w2"))
    assert await write_draft(quick, WORDS, context(), seconds=0.05) == GOOD_DRAFT
    assert len(quick.calls) == 2
    # The second call is cut short when it alone is too slow.
    late = Slow([0.01, 0.5], submit(wrong, "w1"), submit(good, "w2"))
    with pytest.raises(WriterTooSlow):
        await write_draft(late, WORDS, context(), seconds=0.05)
    assert len(late.calls) == 1


# --- a limit does not read the target (Ruling R18) ---------------------------------------------

NOTE_DRAFT_EXAMPLES = (
    Example(ALEX, "note.leave", {"recipient": f"person:{SAM}", "recipient_role": "child",
                                 "length": 5}, "domo_judges"),
    Example(ALEX, "note.leave", {"recipient": f"person:{OWNER}", "recipient_role": "owner",
                                 "length": 5}, "domo_judges"),
    Example(ALEX, "note.leave", {"recipient": f"person:{uuid4()}", "recipient_role": "guest",
                                 "length": 5}, "refused"),
)  # fmt: skip


def a_note_limit(expression: str) -> Draft:
    return Draft(
        (
            change(
                person_id=None,
                role="adult",
                action_type="note.leave",
                access=None,
                limit_expression=expression,
                summary="Les adultes ne laissent pas de mot à Sam.",
            ),
        ),
        NOTE_DRAFT_EXAMPLES,
    )


async def test_a_limit_that_reads_the_target_is_sent_back_then_refused():
    # Every example has no target: the card would say "Domo judges" for a note the limit refuses.
    reads_the_target = a_note_limit(
        f'action.target == "person:{SAM}" || action.facts.recipient_role == "guest" '
        '? "refuse" : "allow"'
    )
    writer = Writer(
        submit(as_arguments(reads_the_target), "w1"), submit(as_arguments(reads_the_target), "w2")
    )
    with pytest.raises(DraftRefused) as refused:
        await write_draft(
            writer, "Les adultes ne laissent pas de mot à Sam", context(), seconds=SECONDS
        )
    assert refused.value.errors == ["change 1: the limit cannot be used"]
    answer = writer.calls[1].messages[-1]
    assert isinstance(answer, ToolResult)
    assert "use the facts that name people instead (person, recipient)" in answer.content
    # The facts that name people say the same, and are shown.
    by_the_facts = a_note_limit(
        f'action.facts.recipient == "person:{SAM}" || action.facts.recipient_role == "guest" '
        '? "refuse" : "allow"'
    )
    examples = (
        *NOTE_DRAFT_EXAMPLES[1:],
        dataclasses.replace(NOTE_DRAFT_EXAMPLES[0], expected="refused"),
    )
    assert await check_draft(dataclasses.replace(by_the_facts, examples=examples), context()) == []


async def test_the_word_target_is_refused_even_inside_a_text():
    in_a_text = a_note_limit(
        'action.facts.recipient_role == "target" || action.facts.recipient_role == "guest" '
        '? "refuse" : "allow"'
    )
    errors = await check_draft(in_a_text, context())
    assert any(
        error.startswith("change 1: the limit cannot be used: a limit may not read action.target")
        for error in errors
    )


# --- what the tools always refuse (Ruling R19) ---------------------------------------------------

ALWAYS_REFUSED = "this situation is always refused, whatever the rights: choose another"


def a_change_of_role(by: UUID, person: UUID, current_role: str, role: str) -> Example:
    facts = on_a_record("household.change_role", {"person": f"person:{person}",
             "current_role": current_role, "role": role, "days": None})  # fmt: skip
    return Example(by, "household.change_role", facts, "domo_judges")


async def test_an_example_of_a_self_role_change_is_sent_back_then_refused():
    draft = Draft(
        (
            change(
                action_type="household.change_role",
                limit_expression=None,
                summary="Alex peut changer le rôle des autres.",
            ),
        ),
        (a_change_of_role(ALEX, ALEX, "adult", "child"),),
    )
    writer = Writer(submit(as_arguments(draft), "w1"), submit(as_arguments(draft), "w2"))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, "Alex peut changer les rôles", context(), seconds=SECONDS)
    assert f"example 1: {ALWAYS_REFUSED}" in refused.value.errors
    # Someone else's role is a situation that may happen: it is worked out.
    fine = dataclasses.replace(draft, examples=(a_change_of_role(ALEX, SAM, "child", "adult"),))
    assert await check_draft(fine, context()) == []


async def test_an_example_giving_the_owner_role_is_sent_back():
    draft = dataclasses.replace(
        GOOD_DRAFT,
        examples=(*GOOD_DRAFT.examples, invite("owner", None, "domo_judges", by=OWNER)),
    )
    assert f"example 4: {ALWAYS_REFUSED}" in await check_draft(draft, context())


@pytest.mark.parametrize(
    ("action_type", "facts", "refused"),
    [
        ("household.change_role", {"person": f"person:{ALEX}", "current_role": "adult",
                                   "role": "child", "days": None}, True),  # oneself
        ("household.change_role", {"person": f"person:{OWNER}", "current_role": "owner",
                                   "role": "adult", "days": None}, True),  # the owner
        ("household.change_role", {"person": f"person:{SAM}", "current_role": "child",
                                   "role": "owner", "days": None}, True),  # the owner's role
        ("household.change_role", {"person": f"person:{SAM}", "current_role": "child",
                                   "role": "adult", "days": None}, False),
        ("household.change_role", {"person": f"person:{SAM}", "current_role": "adult",
                                   "role": "adult", "days": None}, True),  # already so
        ("household.change_role", {"person": f"person:{SAM}", "current_role": "guest",
                                   "role": "guest", "days": 40}, False),  # a new stay (spec 5.5)
        ("household.change_role", {"person": f"person:{SAM}", "current_role": "guest",
                                   "role": "guest", "days": None}, True),  # no stay to change
        ("household.remove", {"person": f"person:{OWNER}", "current_role": "owner",
                              "is_self": False}, True),  # the owner
        ("household.remove", {"person": f"person:{ALEX}", "current_role": "adult",
                              "is_self": True}, False),  # oneself: allowed (spec 5.2)
        ("household.leave", {"current_role": "owner"}, True),  # the owner never leaves
        ("household.leave", {"current_role": "adult"}, False),  # anyone else (spec 5.9)
        ("household.leave", {"current_role": "guest"}, False),
        ("household.bring_back", {"person": f"person:{OWNER}", "role": "adult",
                                  "days": None}, True),  # the owner never left
        ("household.bring_back", {"person": f"person:{SAM}", "role": "adult",
                                  "days": None}, True),  # still in the household
        ("household.bring_back", {"person": f"person:{uuid4()}", "role": "owner",
                                  "days": None}, True),  # the owner's role
        ("household.bring_back", {"person": f"person:{uuid4()}", "role": "adult",
                                  "days": None}, False),  # someone who left
        ("household.invite", {"role": "owner", "days": None, "display_name": "Dana",
                              "owner_first": False}, True),
        ("note.leave", {"recipient": f"person:{ALEX}", "recipient_role": "adult",
                        "length": 3}, False),  # a note to oneself (spec 5.6)
        ("note.leave", {"recipient": f"person:{SAM}", "recipient_role": "child",
                        "length": 3}, False),
    ],
)  # fmt: skip
async def test_what_the_tools_always_refuse_is_never_an_example(action_type, facts, refused):
    example = Example(ALEX, action_type, on_a_record(action_type, facts), "refused")
    draft = Draft((change(),), (*GOOD_DRAFT.examples, example))
    found = f"example 4: {ALWAYS_REFUSED}" in await check_draft(draft, context())
    assert found is refused


# --- the moment an example imagines (Ruling R25) -------------------------------------------------

# Sam may rename himself only between 8:00 and 20:00, and Alex fewer than 3 times a week.
DAY_HOURS = 'now.hour >= 8 && now.hour < 20 ? "allow" : "refuse"'
WEEKLY = 'history.week < 3 ? "allow" : "refuse"'
# The real moment of the proposal, in the household's time zone: a Tuesday, at 22:00.
TUESDAY_NIGHT = datetime(2030, 1, 1, 22, 0)


def renames(person: UUID, expected: str, **moment: Any) -> dict[str, Any]:
    """An example of ``person`` renaming themselves, as the writer gives it, with the parts of the
    moment it imagines (``now``, ``history``, ``household``)."""
    return {
        "person_id": str(person),
        "action_type": "profile.rename",
        "facts": {},
        "expected": expected,
        **moment,
    }


def a_rename_limit(person: UUID, expression: str, *examples: dict[str, Any]) -> dict[str, Any]:
    return {
        "changes": [
            {
                "person_id": str(person),
                "action_type": "profile.rename",
                "limit_expression": expression,
                "summary": "Une limite pour se renommer.",
            }
        ],
        "examples": list(examples),
    }


async def test_an_hour_limit_proposed_at_night_shows_the_hours_its_examples_imagine():
    ctx = context(time_zone_set=True, local_now=TUESDAY_NIGHT)
    answer = a_rename_limit(
        SAM,
        DAY_HOURS,
        renames(SAM, "domo_judges", now={"weekday": 6, "hour": 10, "minute": 0}),
        renames(SAM, "refused", now={"weekday": 6, "hour": 22, "minute": 30}),
        renames(SAM, "refused"),  # the real moment: Tuesday night
    )
    # Proposed at 22:00, the first example still passes: the code works it out at the hour it
    # states, not at the real one.
    writer = Writer(submit(answer, "w1"), submit(answer, "w2"))
    draft = await write_draft(
        writer, "Sam se renomme seulement entre 8 h et 20 h", ctx, seconds=SECONDS
    )
    assert len(writer.calls) == 1
    assert [e.expected for e in draft.examples] == ["domo_judges", "refused", "refused"]
    assert await example_outcome(draft.examples[0], draft, ctx) == "domo_judges"
    # The card shows the hour of each example, the real one too.
    lines = describe_rights_draft(draft_as_json(draft, ctx), CatalogTexts("en"))
    assert lines is not None
    assert [e.moment for e in lines.examples] == [
        "On a Saturday at 10:00.",
        "On a Saturday at 22:30.",
        "On a Tuesday at 22:00.",
    ]


async def test_a_history_limit_shows_the_counts_its_examples_imagine():
    ctx = context()
    answer = a_rename_limit(
        ALEX,
        WEEKLY,
        renames(ALEX, "domo_judges", history={"day": 0, "week": 2, "month": 4}),
        renames(ALEX, "refused", history={"day": 1, "week": 3, "month": 3}),
        renames(ALEX, "domo_judges"),  # the real history: nothing yet
    )
    draft = await write_draft(
        Writer(submit(answer), submit(answer)), "moins de 3 fois", ctx, seconds=SECONDS
    )
    lines = describe_rights_draft(draft_as_json(draft, ctx), CatalogTexts("en"))
    assert lines is not None
    # Only the count the limit reads (the last 7 days).
    assert [e.moment for e in lines.examples] == [
        "Already done 2 times in the last 7 days.",
        "Already done 3 times in the last 7 days.",
        "Not done yet in the last 7 days.",
    ]


async def test_a_household_limit_shows_the_household_its_examples_imagine():
    guests = 'household.people.guest < 2 ? "allow" : "refuse"'
    answer = a_rename_limit(
        ALEX,
        guests,
        renames(ALEX, "domo_judges", household={"people": {"guest": 1}}),
        renames(ALEX, "refused", household={"people": {"guest": 2}}),
        renames(ALEX, "domo_judges"),  # the real household: no guest
    )
    ctx = context()
    draft = await write_draft(
        Writer(submit(answer), submit(answer)), "moins de 2 invités", ctx, seconds=SECONDS
    )
    lines = describe_rights_draft(draft_as_json(draft, ctx), CatalogTexts("en"))
    assert lines is not None
    assert [e.moment for e in lines.examples] == [
        "1 guest in the household.",
        "2 guests in the household.",
        "No guest in the household.",
    ]


async def test_the_stored_draft_holds_the_moment_each_example_was_checked_with():
    ctx = context(time_zone_set=True, local_now=TUESDAY_NIGHT)
    answer = a_rename_limit(
        SAM,
        DAY_HOURS,
        renames(SAM, "domo_judges", now={"weekday": 6, "hour": 10, "minute": 0}),
        renames(SAM, "refused", now={"weekday": 6, "hour": 22, "minute": 0, "date": "2030-01-12"}),
        renames(SAM, "refused"),
    )
    draft = await write_draft(
        Writer(submit(answer), submit(answer)), "entre 8 h et 20 h", ctx, seconds=SECONDS
    )
    content = draft_as_json(draft, ctx)
    # The date of a day the writer did not date is the next one of that weekday.
    assert [e["now"] for e in content["examples"]] == [
        {"weekday": 6, "hour": 10, "minute": 0, "date": "2030-01-05"},
        {"weekday": 6, "hour": 22, "minute": 0, "date": "2030-01-12"},
        {"weekday": 2, "hour": 22, "minute": 0, "date": "2030-01-01"},
    ]
    # Read back, it is what the card shows.
    lines = describe_rights_draft(content, CatalogTexts("en"))
    assert lines is not None and lines.examples[2].moment == "On a Tuesday at 22:00."
    # An example of a limit that reads nothing of the moment holds none.
    assert all(
        set(e) == {"person_id", "role", "action_type", "facts", "outcome"}
        for e in draft_as_json(GOOD_DRAFT, ctx)["examples"]
    )


@pytest.mark.parametrize(
    ("moment", "error"),
    [
        ({"now": {"weekday": 6, "hour": 24, "minute": 0}},
         "example 1: now.hour is a whole number from 0 to 23"),
        ({"now": {"weekday": 0, "hour": 10, "minute": 0}},
         "example 1: now.weekday is a whole number from 1 (Monday) to 7 (Sunday)"),
        ({"now": {"weekday": 6, "hour": 10, "minute": 60}},
         "example 1: now.minute is a whole number from 0 to 59"),
        ({"now": {"weekday": 6, "hour": 10, "minute": 0, "date": "2030-01-06"}},
         "example 1: now.date is a day (YYYY-MM-DD) that falls on its weekday"),
        ({"now": {"weekday": 6, "hour": 10}},
         "example 1: now is weekday, hour, minute and, if it matters, date"),
        ({"history": {"day": -1, "week": 0, "month": 0}},
         "example 1: history.day is a whole number from 0 to 10000"),
        ({"history": {"day": 2, "week": 1, "month": 3}},
         "example 1: history counts the last day within the week within the month: "
         "day <= week <= month"),
        ({"history": {"week": 1}}, "example 1: history is day, week and month"),
        ({"household": {"people": {"captain": 1}}},
         "example 1: household.people counts by role (owner, adult, child, guest), each a whole "
         "number from 0 to 10000"),
        ({"household": {"pets": {"guest": 1}}},
         "example 1: household is people, invitations or both"),
    ],
)  # fmt: skip
async def test_a_moment_out_of_range_is_sent_back_to_the_writer(moment, error):
    ctx = context(time_zone_set=True, local_now=TUESDAY_NIGHT)
    wrong = a_rename_limit(
        SAM,
        DAY_HOURS,
        renames(SAM, "domo_judges", **moment),
        renames(SAM, "refused", now={"weekday": 6, "hour": 22, "minute": 0}),
        renames(SAM, "refused"),
    )
    writer = Writer(submit(wrong, "w1"), submit(wrong, "w2"))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, "entre 8 h et 20 h", ctx, seconds=SECONDS)
    assert error in refused.value.errors
    answer = writer.calls[1].messages[-1]
    assert isinstance(answer, ToolResult) and error in answer.content


async def test_an_hour_is_imagined_only_once_the_time_zone_is_set():
    answer = a_rename_limit(
        ALEX,
        WEEKLY,
        renames(ALEX, "domo_judges", now={"weekday": 6, "hour": 10, "minute": 0}),
        renames(ALEX, "refused", history={"day": 1, "week": 3, "month": 3}),
        renames(ALEX, "domo_judges"),
    )
    writer = Writer(submit(answer, "w1"), submit(answer, "w2"))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, "moins de 3 fois", context(), seconds=SECONDS)
    assert refused.value.errors == [
        "example 1: an example states now only when the household's time zone is set "
        "(time_zone_set)"
    ]


def test_the_writers_text_names_no_length_of_stay():  # M6, audit POL-5
    prompt = writer_prompt()
    assert prompt.splitlines()[0] == WRITER_FIRST_LINE
    assert "days at most" not in prompt and "365" not in prompt
    assert "no length of stay is limited" in prompt
    assert "{" not in prompt and "}" not in prompt


def test_the_writer_is_told_it_may_state_the_moment():
    prompt = writer_prompt()
    for needed in (
        "`now`",
        "`history`",
        "`household`",
        "`weekday`",
        "whenever a limit of its type reads",
    ):
        assert needed in prompt, needed


# --- Domo's own rights (the owner, 2026-11-18) -------------------------------------------------

DOMO_NOTE = {"recipient": f"person:{ALEX}", "recipient_role": "adult", "length": 5}
ALEX_NOTE = {"recipient": f"person:{SAM}", "recipient_role": "child", "length": 5}
NO_NOTES = "L'assistant ne laisse plus de mots de lui-même."
NEVER_DOMO_S = (
    "the owner's own actions are never the assistant's: no right of his is written on them"
)


def domo_change(**fields: Any) -> RightChange:
    """A change of Domo's own right: he leaves no note on his own, unless ``fields`` say
    otherwise."""
    return change(
        **{
            "person_id": None,
            "for_domo": True,
            "action_type": "note.leave",
            "access": "deny",
            "limit_expression": None,
            "summary": NO_NOTES,
            **fields,
        }
    )


def domo_leaves_a_note(expected: str) -> Example:
    return Example(None, "note.leave", DOMO_NOTE, expected, for_domo=True)  # type: ignore[arg-type]


def alex_leaves_a_note(expected: str) -> Example:
    return Example(ALEX, "note.leave", ALEX_NOTE, expected)  # type: ignore[arg-type]


# The writer's answer to "leave no more notes of your own": Domo's right, an example of him
# acting on his own, and one of Alex, whom it does not touch.
DOMO_DRAFT: dict[str, Any] = {
    "changes": [{"domo": True, "action_type": "note.leave", "access": "deny", "summary": NO_NOTES}],
    "examples": [
        {"who": "domo", "action_type": "note.leave", "facts": DOMO_NOTE, "expected": "refused"},
        {
            "person_id": str(ALEX),
            "action_type": "note.leave",
            "facts": ALEX_NOTE,
            "expected": "domo_judges",
        },
    ],
}


async def test_the_writer_writes_a_right_of_domo_s_own():
    writer = Writer(submit(DOMO_DRAFT))
    draft = await write_draft(
        writer, "Ne laisse plus de mots de toi-même", context(), seconds=SECONDS
    )
    [domo] = draft.changes
    assert (domo.for_domo, domo.person_id, domo.role) == (True, None, None)
    assert (domo.access, domo.summary) == ("deny", NO_NOTES)
    first, second = draft.examples
    assert (first.for_domo, first.person_id, first.role) == (True, None, None)
    assert first.expected == "refused"
    # Taking it from Domo takes it from nobody else.
    assert (second.for_domo, second.person_id, second.expected) == (False, ALEX, "domo_judges")
    # Stored as it is, and read back the same.
    stored: Any = draft_as_json(draft, context())
    assert stored["changes"][0]["domo"] is True
    assert stored["examples"][0]["who"] == "domo"
    assert "who" not in stored["examples"][1]
    assert stored_draft(stored).draft == draft
    # A change of a person's says it is not Domo's.
    good: Any = draft_as_json(GOOD_DRAFT, context())
    assert good["changes"][0]["domo"] is False


async def test_giving_domo_his_notes_shows_what_then_happens():  # ledger line n4
    # The owner gives Domo note.leave: the example shows him judging, which is now true, as a
    # note he leaves on his own is his own (the run itself: test_notes_of_domo.py).
    gives = Draft(
        (domo_change(access="allow", summary="L'assistant laisse des mots."),),
        (domo_leaves_a_note("domo_judges"),),
    )
    assert await check_draft(gives, context()) == []
    assert "note.leave" not in NOT_ON_HIS_OWN


async def test_domo_s_right_is_his_alone_and_never_a_rule_for_everyone():
    draft = Draft(
        (domo_change(),), (domo_leaves_a_note("refused"), alex_leaves_a_note("domo_judges"))
    )
    assert await check_draft(draft, context()) == []
    # Written for everyone, the same change is a rule for everyone: it says what to ask, nothing
    # else.
    for_everyone = Draft((domo_change(for_domo=False),), (alex_leaves_a_note("domo_judges"),))
    assert await check_draft(for_everyone, context()) == [
        "change 1: a rule for everyone says what to ask and nothing else: ask alone"
    ]
    # And a rule for everyone is not Domo's right: his stays as it is.
    his = _right("note.leave", for_domo=True, access="deny", summary=NO_NOTES)
    asks = Draft(
        (domo_change(for_domo=False, access=None, ask="yes", summary="Ask first."),),
        (alex_leaves_a_note("asks_them"),),
    )
    in_force = context(rights=RightsTable((his,)))
    assert await check_draft(asks, in_force) == []
    assert await example_outcome(domo_leaves_a_note("refused"), asks, in_force) == "refused"
    # Nor does Domo's right reach anyone else.
    assert await example_outcome(alex_leaves_a_note("domo_judges"), draft, context()) == (
        "domo_judges"
    )


async def test_domo_s_right_and_the_rule_for_everyone_are_taken_away_each_on_its_own():
    his = _right("note.leave", for_domo=True, access="deny", summary=NO_NOTES)
    everyone = _right("note.leave", ask="yes", summary="Ask first.")
    nothing_to_remove = "change 1: there is no right of this subject for this action type to remove"
    takes_his = Draft(
        (domo_change(remove=True, access=None),), (domo_leaves_a_note("domo_judges"),)
    )
    takes_everyones = Draft(
        (domo_change(for_domo=False, remove=True, access=None),),
        (alex_leaves_a_note("domo_judges"),),
    )
    # Each only finds its own.
    assert await check_draft(takes_his, context(rights=RightsTable((his,)))) == []
    assert await check_draft(takes_his, context(rights=RightsTable((everyone,)))) == [
        nothing_to_remove
    ]
    assert await check_draft(takes_everyones, context(rights=RightsTable((everyone,)))) == []
    assert await check_draft(takes_everyones, context(rights=RightsTable((his,)))) == [
        nothing_to_remove
    ]
    # Taking the rule for everyone away leaves Domo's right in force.
    both = context(rights=RightsTable((his, everyone)))
    outcome = await example_outcome(domo_leaves_a_note("refused"), takes_everyones, both)
    assert outcome == "refused"


@pytest.mark.parametrize(
    "subject", [{"person_id": ALEX}, {"role": "adult"}], ids=["with a person", "with a role"]
)
async def test_a_right_of_domo_s_is_about_him_alone(subject):
    draft = Draft((domo_change(**subject),), (domo_leaves_a_note("refused"),))
    assert await check_draft(draft, context()) == [
        "change 1: the subject is a person_id, a role or domo, one at most (none of them: everyone)"
    ]


@pytest.mark.parametrize("owner_action", sorted(OWNER_ONLY_ACTIONS))
async def test_a_draft_about_domo_on_the_owners_own_actions_is_refused(owner_action):
    facts = dict.fromkeys(ACTION_FACTS.get(owner_action, {}))
    draft = Draft(
        (domo_change(action_type=owner_action, access="allow"),),
        (Example(None, owner_action, facts, "domo_judges", for_domo=True),),
    )
    errors = await check_draft(draft, context())
    assert f"change 1: {NEVER_DOMO_S}" in errors
    assert (
        "example 1: an example of the owner's own actions is the owner asking (his person_id)"
        in errors
    )
    # Not even a rule on what to ask, nor taking one away.
    for only in (
        domo_change(action_type=owner_action, access=None, ask="yes"),
        domo_change(action_type=owner_action, access=None, remove=True),
    ):
        assert f"change 1: {NEVER_DOMO_S}" in await check_draft(
            Draft((only,), draft.examples), context()
        )


async def test_the_owner_opens_to_domo_what_he_lacks_by_default():
    # What is nobody's by default is opened to him as to anyone.
    assert "texts.for_someone" not in DEFAULT_POLICY.domo
    facts = {"person": f"person:{ALEX}", "person_role": "adult"}
    rewrites = Example(None, "texts.for_someone", facts, "domo_judges", for_domo=True)
    assert await example_outcome(rewrites, Draft((), ()), context()) == "refused"
    opens = Draft(
        (
            domo_change(
                action_type="texts.for_someone",
                access="allow",
                summary="The assistant rewrites texts.",
            ),
        ),
        (rewrites,),
    )
    assert await check_draft(opens, context()) == []


# The facts of opening an address and of asking for an account, as an example says them.
OPENING = {
    "host": "192.168.1.20",
    "port": 8123,
    "tool": "lights",
    "on_host": True,
    "zone": "house",
    "outward": False,
    "closed_by_owner": True,
    "closed_on": "2030-01-01",
}
GOING_OUT_FACTS = {
    "net.open": {**OPENING, "on_host": False},
    "net.open_host": OPENING,
    "account.ask": {
        "name": "lights",
        "place": "header",
        "for_house": True,
        "giver": f"person:{ALEX}",
        "hosts": 1,
        "outward": False,
        "removed_by_owner": True,
        "removed_on": "2030-01-01",
        "removed_at": "2030-01-01T09:00:00+00:00",
    },
}


async def test_the_owner_gives_domo_the_ports_of_the_machine_that_hosts_him():
    # The owner, 2026-11-22 (spec 13.10): not Domo's by default, but the owner may give it.
    assert "net.open_host" not in DEFAULT_POLICY.domo
    opens_one = Example(None, "net.open_host", OPENING, "domo_judges", for_domo=True)
    assert await example_outcome(opens_one, Draft((), ()), context()) == "refused"
    given = Draft(
        (
            domo_change(
                action_type="net.open_host",
                access="allow",
                summary="The assistant opens the ports of his own machine.",
            ),
        ),
        (opens_one,),
    )
    assert await check_draft(given, context()) == []


@pytest.mark.parametrize("action_type", ["net.open", "net.open_host", "account.ask"])
async def test_the_owner_has_himself_asked_before_an_address_is_opened_or_an_account_asked(
    action_type,
):
    # Spec 13.10, ruling R8: opening an address (towards the internet, or a port of the machine
    # that hosts Domo) and asking for an account are shown, so a rule may ask the owner first,
    # for Domo, for a role and for a person.
    for_domo = Draft(
        (
            domo_change(
                action_type=action_type,
                access="allow",
                approver="owner",
                summary="L'assistant me demande d'abord.",
            ),
        ),
        (
            Example(
                None, action_type, GOING_OUT_FACTS[action_type], "asks_the_owner", for_domo=True
            ),
        ),
    )
    assert await check_draft(for_domo, context()) == []
    for subject in ({"person_id": None, "role": "adult"}, {"person_id": ALEX}):
        given = change(
            **subject,
            action_type=action_type,
            access="allow",
            approver="owner",
            limit_expression=None,
            summary="On me demande d'abord.",
        )
        errors = await check_draft(Draft((given,), ()), context())
        assert SILENT_ONLY_ACCESS not in errors, (action_type, subject)
        assert "change 1: nothing is asked for an action that changes nothing" not in errors
    asks = domo_change(action_type=action_type, access=None, ask="yes", summary="Demande-moi.")
    assert SILENT_ONLY_ACCESS not in await check_draft(Draft((asks,), ()), context())


@pytest.mark.parametrize("action_type", ["net.look", "net.close", "account.look"])
async def test_closing_an_address_and_looking_take_access_alone(action_type):
    for given in (
        domo_change(action_type=action_type, access=None, approver="owner"),
        change(action_type=action_type, access=None, approver="owner", limit_expression=None),
    ):
        assert SILENT_ONLY_ACCESS in await check_draft(Draft((given,), ()), context())


CLOSED_TO_DOMO = (
    "change 1: secrets, keys, sharing an account, spending limits and who may do what (rights, "
    "rules, leaving, handing the owner's role over) are not opened to the assistant himself for "
    "now: only taken from him"
)
# Opened to Domo by a right of the owner (``test_the_owner_opens_to_domo_what_he_lacks_by_
# default``, ``test_the_owner_gives_domo_the_ports_of_the_machine_that_hosts_him``).
OPENED_TO_DOMO_BY_A_RIGHT = {"texts.for_someone", "net.open_host"}
SOCLE_CLOSED_TO_DOMO = sorted(
    DOMO_NOT_BY_DEFAULT - OWNER_ONLY_ACTIONS - set(RIGHTS_OF) - OPENED_TO_DOMO_BY_A_RIGHT
)


def test_sharing_an_account_is_closed_to_domo_and_removing_one_is_his():
    # The owner, 2026-11-24: he removes on his own an account that no longer serves.
    assert "account.remove" not in SOCLE_CLOSED_TO_DOMO
    assert "account.remove" not in DOMO_NOT_BY_DEFAULT
    # Sharing a house account is the owner's alone: no right writes it for anyone.
    assert "account.share" in OWNER_ONLY_ACTIONS
    assert {"household.leave", "keys.house", "spending.limit"} <= set(SOCLE_CLOSED_TO_DOMO)


@pytest.mark.parametrize("socle", SOCLE_CLOSED_TO_DOMO)
async def test_secrets_spending_limits_and_permissions_are_never_opened_to_domo(socle):
    # Ruling R22: spec 5.1 keeps them in the socle; opened, they would act on the owner's keys.
    facts = dict.fromkeys(ACTION_FACTS.get(socle, {}))
    example = Example(None, socle, facts, "domo_judges", for_domo=True)
    for opening in (
        domo_change(action_type=socle, access="allow"),
        domo_change(action_type=socle, access=None, approver="owner"),
        domo_change(action_type=socle, access=None, limit_expression='"allow"'),
    ):
        assert CLOSED_TO_DOMO in await check_draft(Draft((opening,), (example,)), context())
    # Taking it from him (he lacks it already), or a person's right on it, is still written.
    denied = Draft(
        (domo_change(action_type=socle, access="deny"),),
        (dataclasses.replace(example, expected="refused"),),
    )
    assert CLOSED_TO_DOMO not in await check_draft(denied, context())
    for_alex = Draft(
        (change(action_type=socle, access="deny", limit_expression=None),),
        (Example(ALEX, socle, facts, "refused"),),
    )
    assert CLOSED_TO_DOMO not in await check_draft(for_alex, context())


HIS_OWN_BY_DEFAULT = [
    *sorted((DOMO_HOUSEHOLD_ACTIONS | DOMO_DEVICE_ACTIONS) - set(RIGHTS_OF)),
    "account.remove",
    "memory.keep",
    "memory.forget",
    "trust.note",
    "plan.set",
    "confirmation.remind",
    "chain.run",
    "chain.continue",
    "writing.choose",
    "delivery.look",
]
_FACT_OF_KIND: dict[str, object] = {
    "person": f"person:{ALEX}",
    "role": "adult",
    "boolean": False,
    "number": None,
    "text": "a text",
    "date": "2026-01-02",
}


def his_example(action_type: str, expected: str) -> Example:
    """The assistant acting on his own on ``action_type``, with every fact of a plausible kind."""
    facts = {
        name: _FACT_OF_KIND[kind]
        for name, kind in ACTION_FACTS.get(action_type, {}).items()
        if name not in JUDGED_FACTS
    }
    # Sam (a child) is the one the people types are about; Paul, who left, is brought back.
    if "person" in facts:
        facts["person"] = f"person:{PAUL if action_type == 'household.bring_back' else SAM}"
    if "current_role" in facts:
        facts["current_role"] = "child"
    if action_type == "device.remove":
        facts["is_self"] = True  # (the tool's own guard refuses another's device to a non-owner)
    return Example(None, action_type, facts, expected, for_domo=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("held", HIS_OWN_BY_DEFAULT)
async def test_the_owner_takes_from_the_assistant_what_he_keeps_on_his_own_and_gives_it_back(held):
    # Each is written whole, with no problem at all: a deny, an "ask me first" (the barrier asks
    # it before any judgement, even on what changes nothing) and a yes given back after a deny.
    assert held not in SOCLE_CLOSED_TO_DOMO
    for rule, outcome in (
        (domo_change(action_type=held, access="deny"), "refused"),
        (
            domo_change(action_type=held, access=None, ask="yes", summary="Demande-moi."),
            "asks_them",
        ),
        (
            domo_change(action_type=held, access="allow", summary="Il peut de nouveau."),
            "domo_judges",
        ),
    ):
        draft = Draft((rule,), (his_example(held, outcome),))
        assert await check_draft(draft, context(former=FORMER)) == [], (held, rule)


async def test_the_owner_s_ask_me_first_for_him_is_written_but_never_for_a_person():
    # For a person, nothing is asked on what changes nothing: as before.
    assert "plan.set" in CHANGES_NOTHING
    for_alex = change(
        action_type="plan.set", access=None, limit_expression=None, ask="yes", summary="Demande."
    )
    assert any(
        "nothing is asked" in problem
        for problem in await check_draft(Draft((for_alex,), ()), context())
    )


async def test_the_owner_never_leaves_to_a_judge_what_is_never_judged_for_him():
    # Memory is never judged: "ask: judge" for him there would do nothing (it is accepted for a
    # type his judge weighs, such as inviting).
    never_judged = domo_change(
        action_type="memory.keep", access=None, ask="judge", ask_when="always", summary="Juge."
    )
    problems = await check_draft(Draft((never_judged,), ()), context())
    assert any("never weighs" in problem for problem in problems), problems
    judged = domo_change(
        action_type="household.remove", access=None, ask="judge", ask_when="x", summary="Juge."
    )
    problems = await check_draft(Draft((judged,), ()), context())
    assert not any("never weighs" in problem for problem in problems), problems


@pytest.mark.parametrize("socle", ["key.add", "household.leave", "spending.limit", "account.share"])
async def test_what_stays_closed_to_him_is_still_refused_for_an_opening(socle):
    opening = domo_change(action_type=socle, access="allow", summary="Il peut.")
    problems = await check_draft(Draft((opening,), ()), context())
    assert problems != []
    ask = domo_change(action_type=socle, access=None, ask="yes", summary="Demande-moi.")
    assert await check_draft(Draft((ask,), ()), context()) != []


# --- what a tool does and spends (final review I2, spec 5.3, 5.8) ------------------------------


def begins(
    amount: int | None, expected: str, *, by: UUID | None = SAM, for_domo: bool = False
) -> Example:
    """An example of a program that begins ``order`` of the tool ``pizza`` for ``by`` (or for
    the house: ``for_domo``), saying it costs ``amount`` cents."""
    facts = {
        "tool": "pizza",
        "operation": "order",
        "amount_cents": amount,
        "costs": amount is not None,
        "irreversible": False,
        "physical": False,
        "outward": True,
    }
    return Example(
        None if for_domo else by,
        "program.begin",
        facts,
        expected,  # type: ignore[arg-type]
        for_domo=for_domo,
    )


async def test_the_owner_puts_a_spending_limit_and_an_adult_on_what_a_tool_spends_for_a_child():
    over_20 = (
        "action.facts.amount_cents != null && spent.week + action.facts.amount_cents > 2000 ? "
        '"refuse" : "allow"'
    )
    draft = Draft(
        (
            change(
                person_id=SAM,
                action_type="program.begin",
                access=None,
                approver="adult",
                limit_expression=over_20,
                summary="Sam dépense 20 € par semaine au plus, avec l'accord d'un adulte.",
            ),
        ),
        (begins(3000, "refused"), begins(1500, "asks_an_adult"), begins(None, "asks_an_adult")),
    )
    assert await check_draft(draft, context()) == []


async def test_the_owner_says_to_always_ask_before_a_tool_spends():
    for_everyone = change(
        person_id=None,
        action_type="program.begin",
        access=None,
        limit_expression=None,
        ask="explicit_yes",
        summary="Toujours demander avant qu'un outil dépense.",
    )
    draft = Draft((for_everyone,), (begins(1500, "asks_them", by=ALEX),))
    assert await check_draft(draft, context()) == []
    # And the same on a write of the base, or on the launch of one of Domo's tools.
    for action_type in ("program.base.write", "program.wake", "program.later"):
        rule = dataclasses.replace(for_everyone, action_type=action_type)
        example = Example(ALEX, action_type, {}, "asks_them")
        assert await check_draft(Draft((rule,), (example,)), context()) == []


async def test_the_owner_limits_what_domo_spends_for_the_house():
    over_100 = (
        "action.facts.amount_cents != null && spent.month + action.facts.amount_cents > 10000 ? "
        '"ask_owner" : "allow"'
    )
    draft = Draft(
        (
            domo_change(
                action_type="program.begin",
                access=None,
                limit_expression=over_100,
                summary="L'assistant dépense 100 € par mois au plus pour la maison sans me "
                "demander.",
            ),
        ),
        (
            begins(20000, "asks_the_owner", for_domo=True),
            begins(5000, "domo_judges", for_domo=True),
            begins(None, "domo_judges", for_domo=True),
        ),
    )
    assert await check_draft(draft, context()) == []


TOOLS_ASK_A_YES = (
    "change 1: on what a running program asks (program.) and on the assistant's own tools (made.), "
    "ask is "
    "yes, explicit_yes or proof"
)
TOOLS_NO_JUDGE = (
    "change 1: on what a running program asks (program.) and on the assistant's own tools (made.), "
    "nothing goes to a judge: no judge_question, and a limit gives allow, refuse or ask_owner"
)


async def test_on_what_a_program_asks_nothing_goes_to_a_judge():
    example = begins(1500, "domo_judges")
    leaves_it = change(
        person_id=SAM, action_type="program.begin", access=None, limit_expression=None, ask="judge"
    )
    assert TOOLS_ASK_A_YES in await check_draft(Draft((leaves_it,), (example,)), context())
    judged = change(
        person_id=SAM,
        action_type="program.begin",
        access=None,
        limit_expression='"judge"',
        judge_question="Is it worth it?",
    )
    assert TOOLS_NO_JUDGE in await check_draft(Draft((judged,), (example,)), context())
    # A program run by hand is Domo's act: its right says access alone, as before.
    by_hand = change(
        person_id=None,
        for_domo=True,
        action_type="program.run",
        access=None,
        approver="owner",
        limit_expression=None,
    )
    assert SILENT_ONLY_ACCESS in await check_draft(Draft((by_hand,), (example,)), context())


def test_the_writers_text_tells_what_a_tool_spends():
    prompt = writer_prompt()
    for needed in ("`program.begin`", "`spent`", "`amount_cents`", "`made.`"):
        assert needed in prompt, needed


@pytest.mark.parametrize(
    ("who", "error"),
    [
        ({"who": "Domo"}, 'example 1: who is "domo" or null'),
        (
            {"who": "domo", "person_id": str(ALEX)},
            "example 1: who asks is a person_id, a role or who domo, exactly one",
        ),
        (
            {"who": "domo", "role": "guest"},
            "example 1: who asks is a person_id, a role or who domo, exactly one",
        ),
    ],
)
async def test_an_example_names_domo_alone(who, error):
    answer = {
        "changes": DOMO_DRAFT["changes"],
        "examples": [
            {"action_type": "note.leave", "facts": DOMO_NOTE, "expected": "refused", **who}
        ],
    }
    writer = Writer(submit(answer, "w1"), submit(answer, "w2"))
    with pytest.raises(DraftRefused) as refused:
        await write_draft(writer, "pas de mots", context(), seconds=SECONDS)
    assert error in refused.value.errors


def test_the_writer_is_told_domo_s_rights_and_what_he_has_by_default():
    his = _right("note.leave", for_domo=True, access="deny", summary=NO_NOTES)
    everyone = _right("household.remove", ask="yes", summary="Ask first.")
    shown = writer_context_json(context(rights=RightsTable((his, everyone))))
    rights: Any = shown["rights"]
    assert [(r["summary"], r["domo"]) for r in rights] == [
        (NO_NOTES, True),
        ("Ask first.", False),
    ]
    catalog: Any = shown["catalog"]
    by_type = {entry["action_type"]: entry for entry in catalog}
    assert {a for a, entry in by_type.items() if entry.get("domo_by_default")} == set(
        DEFAULT_POLICY.domo
    )
    assert "domo_by_default" not in by_type["rights.apply"]
    assert "domo_by_default" not in by_type["key.add"]


def test_the_writers_text_says_domo_is_a_subject_of_his_own():
    prompt = writer_prompt()
    for needed in (
        "The assistant himself is a subject of rights (`domo: true`",
        "what he does on his own, for the house",
        "every right except what is the owner's alone",
        "judges himself what is right to do",
        "The owner may take a right from him",
        '`"who": "domo"`',
        "`domo_by_default`",
    ):
        assert needed in prompt, needed


def test_an_example_of_domo_names_him():
    draft = Draft((domo_change(),), (domo_leaves_a_note("refused"),))
    for lang in ("en", "fr"):
        lines = describe_rights_draft(draft_as_json(draft, context()), CatalogTexts(lang))
        assert lines is not None
        assert lines.examples[0].situation.startswith(t("rights.example.as_domo", lang))


def _leaves(by: UUID | None, role: Role | None) -> Example:
    return Example(by, "household.leave", {"current_role": "child"}, "asks_an_adult", role=role)


async def test_a_made_up_child_asking_to_leave_is_shown_as_asking_an_adult():
    # The child mark is kept in what the owner is shown: it predicts the barrier's decision.
    outcome = await example_outcome(_leaves(None, "child"), Draft((), ()), context())
    assert outcome == "asks_an_adult"


async def test_a_real_child_marked_person_asking_to_leave_is_shown_as_asking_an_adult():
    tom = UUID("00000000-0000-0000-0000-0000000000e1")
    ctx = dataclasses.replace(
        context(),
        persons=(*PEOPLE, (tom, "Tom", "adult")),
        child_ids=frozenset({tom}),
    )
    assert await example_outcome(_leaves(tom, None), Draft((), ()), ctx) == "asks_an_adult"
