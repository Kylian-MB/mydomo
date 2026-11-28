"""The owner's directives on people are applied in code before any judgement: the assistant never
gives back an access the owner withdrew, nor accepts someone the owner refused, whatever right the
owner gave him; the owner alone sets and lifts them."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.passwords import hash_password
from domo_core.accounts.sessions import is_active
from domo_core.confirmations import service as service_module
from domo_core.confirmations.store import create_confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.devices.models import Device
from domo_core.household.models import Invitation
from domo_core.household.service import (
    InvitationUnavailable,
    accept_invitation,
    accept_return,
    check_joining,
    check_return,
    create_invitation,
    create_return_invitation,
    disable,
    nobody_new_since,
    tell_the_owner_nobody_new_came,
)
from domo_core.llm.types import ToolCall
from domo_core.people.directives import (
    GIVES_ACCESS,
    MAKES_SOMEONE_NEW,
    directives_now,
    end_expired,
    lift,
    set_directive,
    under_directive,
)
from domo_core.people.models import AccessPromise, OwnerDirective, PersonAct
from domo_core.people.reach import can_answer
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.barrier import Allow, Refuse
from domo_core.policy.catalog import ACTION_FACTS, DEFAULT_POLICY
from domo_core.policy.decision import ChainBasis, decide_in_chain, decide_now
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import Actor, domo_actor
from sqlalchemy import select
from test_concerned_person import _people, _requests, _turn, _yes
from test_own_idea import LIVE

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
KNOWN = {"state": "known", "origin": "domo", "origin_reason": "Alex said Sam comes on Saturday"}
GIVING = (
    "household.bring_back",
    "household.change_role",
    "household.invite",
    "access.promise_keep",
)


def act(action_type: str, target: str | None = None, **facts: str | int | bool | None):
    """An action as its tool describes it, with the facts a default limit reads."""
    declared = ACTION_FACTS[action_type]
    given: dict[str, str | int | bool | None] = {
        "person": target,
        "role": "adult",
        "current_role": "adult",
        "days": None,
        "display_name": "Sam",
        "owner_first": False,
    }
    wanted = {name: given[name] for name in declared if name in given}
    return DescribedAction(
        "core", action_type, target, "permissions", "household", {**wanted, **facts}
    )


async def _decide(session, actor: Actor, action: DescribedAction, now: datetime = NOW):
    return await decide_now(session, actor, action, DEFAULT_POLICY, now)


def _directive_refusal(decision: object) -> bool:
    return isinstance(decision, Refuse) and decision.reason == "owner_directive"


async def _owner_gives_the_assistant(session, owner: Person, action_types) -> None:
    session.add_all(
        PolicyRight(
            for_domo=True,
            action_type=action_type,
            access="allow",
            summary="The assistant may.",
            owner_words="you may",
            created_by=owner.id,
        )
        for action_type in action_types
    )
    await session.flush()


async def test_the_assistant_never_gives_back_an_access_the_owner_withdrew(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    for action_type in GIVING:
        assert not isinstance(
            await _decide(session, domo_actor(), act(action_type, f"person:{sam.id}")), Refuse
        ), action_type
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await _owner_gives_the_assistant(session, alex, GIVING)
    for action_type in GIVING:
        decision = await _decide(session, domo_actor(), act(action_type, f"person:{sam.id}"))
        assert _directive_refusal(decision), action_type
    assert (await session.get(Person, sam.id)).state == "withdrawn"


async def test_the_assistant_never_accepts_someone_the_owner_refused(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    tom = await add_person("Tom", "guest", **KNOWN)  # refused before it ever had access
    for who in (sam, tom):
        await set_directive(
            session,
            kind="refuse",
            person=who,
            description="Sam from the club",
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )
    await _owner_gives_the_assistant(session, alex, GIVING)
    for who in (sam, tom):
        for action_type in GIVING:
            decision = await _decide(session, domo_actor(), act(action_type, f"person:{who.id}"))
            assert _directive_refusal(decision), (who.display_name, action_type)
        # A merge only ever leaves the stricter state: it is never refused for a directive.
        other = tom if who is sam else sam
        merging = act(
            "people.merge",
            f"person:{who.id}",
            kept=f"person:{who.id}",
            absorbed=f"person:{other.id}",
        )
        assert not _directive_refusal(await _decide(session, domo_actor(), merging))
        assert (await session.get(Person, who.id)).state == "refused"


async def test_another_adult_cannot_bring_them_back_either(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest")
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    session.add(
        PolicyRight(
            person_id=dana.id,
            action_type="household.bring_back",
            access="allow",
            summary="Dana may.",
            owner_words="she may",
            created_by=alex.id,
        )
    )
    await session.flush()
    decision = await _decide(
        session, actor_of(dana), act("household.bring_back", f"person:{sam.id}")
    )
    assert _directive_refusal(decision)


async def test_the_owners_own_request_goes_on(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    for action in (
        act("household.bring_back", f"person:{sam.id}"),
        act("household.invite", None),
        act("people.open"),
    ):
        decision = await _decide(session, actor_of(alex), action)
        assert isinstance(decision, Allow), action.action_type


async def test_no_new_people_refuses_a_new_record_and_a_first_access_and_leaves_the_others_alone(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    known = await add_person("Tom", "guest", **KNOWN)
    old = await add_person("Dana", "adult", first_access_at=NOW - timedelta(days=30))
    await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description="for the party",
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    assert _directive_refusal(await _decide(session, domo_actor(), act("people.open")))
    assert _directive_refusal(await _decide(session, domo_actor(), act("household.invite")))
    first = await _decide(session, domo_actor(), act("household.change_role", f"person:{known.id}"))
    assert _directive_refusal(first)
    later = await _decide(session, domo_actor(), act("household.change_role", f"person:{old.id}"))
    assert not isinstance(later, Refuse)


async def test_no_keeping_applies_the_same_way(session, add_person):
    alex = await add_person("Alex", "owner")
    await set_directive(
        session,
        kind="no_keeping",
        person=None,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    assert _directive_refusal(await _decide(session, domo_actor(), act("people.open")))


async def test_a_directive_limited_in_time_ends_by_itself(session, add_person):
    alex = await add_person("Alex", "owner")
    await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description=None,
        except_words=None,
        until=NOW + timedelta(days=1),
        owner=alex,
        now=NOW,
    )
    assert _directive_refusal(await _decide(session, domo_actor(), act("people.open")))
    later = NOW + timedelta(days=2)
    assert not isinstance(await _decide(session, domo_actor(), act("people.open"), later), Refuse)
    assert await directives_now(session, later) == []
    assert len(await directives_now(session, NOW)) == 1


async def test_an_exception_in_words_lets_through_what_the_assistant_decides_for_it(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    directive = await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description=None,
        except_words="except the family",
        until=None,
        owner=alex,
        now=NOW,
    )
    excepted = act("people.open", None, exception_of=str(directive.id))
    assert not isinstance(await _decide(session, domo_actor(), excepted), Refuse)
    # A person as the actor never uses it.
    assert _directive_refusal(await _decide(session, actor_of(dana), excepted))
    # Another id is no exception.
    other = act("people.open", None, exception_of=str(alex.id))
    assert _directive_refusal(await _decide(session, domo_actor(), other))
    # Without words on the directive, naming it changes nothing.
    directive.except_words = None
    await session.flush()
    assert _directive_refusal(await _decide(session, domo_actor(), excepted))


async def test_an_exception_never_lifts_a_withdrawal(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    directive = await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words="except me",
        until=None,
        owner=alex,
        now=NOW,
    )
    action = act("household.bring_back", f"person:{sam.id}", exception_of=str(directive.id))
    assert _directive_refusal(await _decide(session, domo_actor(), action))


async def test_only_the_owner_sets_or_lifts(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest")
    with pytest.raises(ValueError):
        await set_directive(
            session,
            kind="withdraw",
            person=sam,
            description=None,
            except_words=None,
            until=None,
            owner=dana,
            now=NOW,
        )
    with pytest.raises(ValueError):
        await set_directive(
            session,
            kind="withdraw",
            person=alex,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )
    directive = await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    with pytest.raises(ValueError):
        await lift(session, directive, owner=dana, now=NOW)
    assert await under_directive(session, sam.id, NOW) is not None


async def test_lifting_makes_someone_gone_not_back(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    tom = await add_person("Tom", "guest", **KNOWN)
    withdrawn = await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    refused = await set_directive(
        session,
        kind="refuse",
        person=tom,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await lift(session, withdrawn, owner=alex, now=NOW)
    await lift(session, refused, owner=alex, now=NOW)
    assert (await session.get(Person, sam.id)).state == "gone"
    assert (await session.get(Person, sam.id)).disabled_at is not None
    assert (await session.get(Person, tom.id)).state == "known"
    assert await under_directive(session, sam.id, NOW) is None
    assert await directives_now(session, NOW) == []
    assert not isinstance(
        await _decide(session, domo_actor(), act("household.bring_back", f"person:{sam.id}")),
        Refuse,
    )


async def test_withdrawing_cuts_sessions_devices_and_pauses_their_rights(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "adult")
    account = await add_account(sam, "sam")
    session.add(
        WebSession(account_id=account.id, token_hash="a" * 64, expires_at=NOW + timedelta(days=1))
    )
    device = Device(person_id=sam.id, name="Tablet", secret_hash="h-sam")
    right = PolicyRight(
        person_id=sam.id,
        action_type="note.leave",
        access="allow",
        summary="Sam may.",
        owner_words="he may",
        created_by=alex.id,
    )
    promise = AccessPromise(
        description="Sam from the club",
        role="guest",
        standing="visiting",
        asked_by="domo",
        is_order=False,
        reason="Alex said so",
        kept_at=NOW,
        kept_person_id=sam.id,
    )
    session.add_all([device, right, promise])
    await session.flush()
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    assert (await session.scalars(select(WebSession))).all() == []
    await session.refresh(device)
    assert device.removed_at is not None
    await session.refresh(right)
    assert right.paused_at is not None
    await session.refresh(promise)
    # A promise ends once: the one kept for Sam stays as it is, only his access is taken back.
    assert promise.kept_at is not None and promise.dropped_at is None
    refreshed = await session.get(Person, sam.id, populate_existing=True)
    assert refreshed.state == "withdrawn" and refreshed.disabled_at is not None
    assert await session.scalar(select(Account.id).where(Account.person_id == sam.id))


def test_the_lists_and_the_exception_fact_are_in_the_catalog():
    assert "household.send_link" in GIVES_ACCESS and "access.promise_keep" in GIVES_ACCESS
    assert frozenset({"people.open", "household.invite"}) == MAKES_SOMEONE_NEW
    for action_type in GIVES_ACCESS | MAKES_SOMEONE_NEW:
        assert ACTION_FACTS[action_type]["exception_of"] == "text", action_type


async def test_the_decision_of_a_directive_is_recorded_as_any_refusal(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    decision = await _decide(session, domo_actor(), act("household.bring_back", f"person:{sam.id}"))
    assert decision == Refuse("owner_directive")
    stored = await session.scalars(select(OwnerDirective))
    assert len(stored.all()) == 1


def test_the_model_is_told_in_words_it_can_say_never_the_word_of_the_reason():
    from domo_core.conversation.execution import OWNER_DIRECTIVE_DETAIL, _refused

    record = _refused("household_bring_back", "household.bring_back", "owner_directive")
    assert record.model_result["detail"] == OWNER_DIRECTIVE_DETAIL
    assert record.card["key"] == "action.card.refused.owner_directive"


# --- fix round 1 -------------------------------------------------------------------------------


async def _a_return_link(session, person, owner, now=NOW):
    return await create_return_invitation(
        session,
        person=person,
        role="adult",
        guest_days=None,
        display_name=person.display_name,
        created_by=owner.id,
        now=now,
    )


async def _away(session, add_person, add_account, name="Sam"):
    sam = await add_person(name, "adult")
    await add_account(sam, name.lower(), hash_password("old password"))
    await disable(session, sam, NOW - timedelta(days=3), by=sam.id)
    await session.refresh(sam)
    return sam


async def test_a_withdrawal_cancels_the_open_links_and_refuses_the_pending_requests(
    session, add_person, add_account, add_conversation
):
    alex = await add_person("Alex", "owner")
    sam = await _away(session, add_person, add_account)
    link, token = await _a_return_link(session, sam, alex)
    returning = await check_return(session, token=token, password="a new password", now=NOW)
    dana = await add_person("Dana", "adult")
    waiting = await create_confirmation(
        session,
        requester=actor_of(sam),
        tool_name="profile_rename",
        arguments={"name": "Sam"},
        action=act("profile.rename", f"person:{sam.id}"),
        approver="adult",
        asked=Asked("yes", "judge"),
        conversation_id=(await add_conversation(dana)).id,
        message_id=None,
        now=NOW,
    )
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await session.refresh(link)
    await session.refresh(waiting)
    assert link.cancelled_at == NOW
    assert waiting.status == "refused"
    with pytest.raises(InvitationUnavailable):
        await accept_return(session, returning, hash_password("a new password"), now=NOW)
    assert (await session.get(Person, sam.id, populate_existing=True)).state == "withdrawn"


async def test_a_return_link_made_after_the_directive_still_brings_nobody_back(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    sam = await _away(session, add_person, add_account)
    await set_directive(
        session,
        kind="refuse",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    # A link the check lets through (made, say, by a path that forgot the directive).
    sam.state, sam.disabled_at = "gone", NOW - timedelta(days=3)
    link, token = await _a_return_link(session, sam, alex)
    returning = await check_return(session, token=token, password="a new password", now=NOW)
    with pytest.raises(InvitationUnavailable):
        await accept_return(session, returning, hash_password("a new password"), now=NOW)
    await session.refresh(link)
    assert link.accepted_at is None
    assert not is_active(await session.get(Person, sam.id, populate_existing=True), NOW)


async def test_an_exception_must_name_every_directive_that_blocks(session, add_person):
    alex = await add_person("Alex", "owner")
    first = await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description=None,
        except_words="except the family",
        until=None,
        owner=alex,
        now=NOW,
    )
    second = await set_directive(
        session,
        kind="no_keeping",
        person=None,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    only_first = act("people.open", None, exception_of=str(first.id))
    assert _directive_refusal(await _decide(session, domo_actor(), only_first))
    both = act("people.open", None, exception_of=f"{first.id},{second.id}")
    assert _directive_refusal(await _decide(session, domo_actor(), both))  # no words on the second
    second.except_words = "except the family too"
    await session.flush()
    assert not isinstance(await _decide(session, domo_actor(), both), Refuse)
    assert _directive_refusal(await _decide(session, domo_actor(), only_first))


async def test_a_chain_step_on_a_withdrawn_person_is_refused(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest")
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    _, decision = await decide_in_chain(
        session,
        actor_of(dana),
        act("household.bring_back", f"person:{sam.id}"),
        DEFAULT_POLICY,
        NOW,
        basis=ChainBasis("adult", frozenset()),
        done=[],
    )
    assert _directive_refusal(decision)


async def test_the_assistants_own_act_asked_by_a_person_is_refused_on_both_sides(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await add_person("Sam", "guest")
    await set_directive(
        session,
        kind="refuse",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    action = replace(act("household.change_role", f"person:{sam.id}"), also_domo=True)
    for who in (actor_of(dana), actor_of(alex)):
        assert _directive_refusal(await _decide(session, who, action)), who.role


async def test_a_request_that_waited_is_refused_at_the_yes_once_the_owner_wants_nobody_new(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=dana.id,
                action_type="household.invite",
                access="allow",
                approver="owner",
                summary="Dana may invite.",
                owner_words="she may",
                created_by=alex.id,
            )
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, dana)
    asked = ToolCall(
        "c",
        "household_invite",
        json.dumps({"display_name": "Tom", "role": "guest", "guest_days": 1}),
    )
    record = await handle_tool_call(asked, ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    async with session_factory() as writer:
        owner = await writer.get(Person, alex.id)
        await set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description=None,
            except_words=None,
            until=None,
            owner=owner,
            now=LIVE,
        )
        await writer.commit()
    decided: list[object] = []
    real = service_module.decide_now

    async def seen(*args, **kwargs):
        decision = await real(*args, **kwargs)
        decided.append(decision)
        return decision

    monkeypatch.setattr(service_module, "decide_now", seen)
    outcome, _ = await _yes(ctx, request, alex)
    assert outcome == "refused"
    assert Refuse("owner_directive") in decided
    async with session_factory() as reader:
        assert (await reader.scalars(select(Invitation))).all() == []


async def test_a_directive_that_ends_in_time_is_lifted_like_a_lift_by_the_upkeep(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest")
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=NOW + timedelta(days=1),
        owner=alex,
        now=NOW,
    )
    later = NOW + timedelta(days=2)
    # Before the upkeep runs, every reader already sees it ended.
    assert await under_directive(session, sam.id, later) is None
    back = act("household.bring_back", f"person:{sam.id}")
    assert not isinstance(await _decide(session, domo_actor(), back, later), Refuse)
    assert await end_expired(session, NOW + timedelta(hours=1)) == 0
    assert await end_expired(session, later) == 1
    assert (await session.get(Person, sam.id, populate_existing=True)).state == "gone"
    assert await end_expired(session, later) == 0


async def test_lifting_one_of_two_directives_keeps_the_access_cut(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "adult")
    withdrawn = await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    refused = await set_directive(
        session,
        kind="refuse",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    await lift(session, refused, owner=alex, now=NOW)
    sam = await session.get(Person, sam.id, populate_existing=True)
    assert sam.state == "withdrawn" and not is_active(sam, NOW)
    await lift(session, withdrawn, owner=alex, now=NOW)
    assert (await session.get(Person, sam.id, populate_existing=True)).state == "gone"


async def test_withdrawing_someone_already_gone_keeps_the_date_they_left(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    sam = await _away(session, add_person, add_account)
    left = sam.disabled_at
    await set_directive(
        session,
        kind="withdraw",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    assert (await session.get(Person, sam.id, populate_existing=True)).disabled_at == left


async def test_a_refused_person_has_no_access_and_nobody_can_ask_them(
    session, add_person, add_account
):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "adult")
    await add_account(sam, "sam")
    await set_directive(
        session,
        kind="refuse",
        person=sam,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    sam = await session.get(Person, sam.id, populate_existing=True)
    assert not is_active(sam, NOW)
    assert not await can_answer(session, sam.id, NOW)


async def test_no_new_people_also_stops_a_link_that_already_waited(session, add_person):
    """Dana invites Tom on Monday; Alex says "no new people" on Tuesday; Tom's link no longer
    lets him in on Wednesday, and Alex is told once that it stopped someone. A link Alex makes
    after his own word is his order, and works."""
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    monday = NOW - timedelta(days=1)
    invitation, token = await create_invitation(
        session, display_name="Tom", role="guest", guest_days=3, created_by=dana.id, now=monday
    )
    assert not await nobody_new_since(session, invitation, NOW)
    await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW,
    )
    later = NOW + timedelta(hours=1)
    assert await nobody_new_since(session, invitation, later)
    with pytest.raises(InvitationUnavailable):
        await check_joining(
            session, token=token, username="tom", password="a long password 1234", now=later
        )
    assert await tell_the_owner_nobody_new_came(session, invitation, later) == alex.id
    assert await tell_the_owner_nobody_new_came(session, invitation, later) is None
    [stopped] = (
        await session.scalars(select(PersonAct).where(PersonAct.act == "joining_stopped"))
    ).all()
    assert (stopped.owed_to_owner, stopped.invitation_id) == (True, invitation.id)
    his, his_token = await create_invitation(
        session, display_name="Sam", role="guest", guest_days=3, created_by=alex.id, now=later
    )
    assert not await nobody_new_since(session, his, later)
    joining = await check_joining(
        session, token=his_token, username="sam", password="a long password 1234", now=later
    )
    assert joining.invitation_id == his.id


async def test_no_new_people_said_between_the_check_and_the_joining_stops_it(session, add_person):
    """Tom's form was checked, then Alex says "nobody new" before Tom's account is made: the
    joining itself is stopped too (accept_invitation)."""
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    invitation, token = await create_invitation(
        session, display_name="Tom", role="guest", guest_days=3, created_by=dana.id, now=NOW
    )
    joining = await check_joining(
        session, token=token, username="tom", password="a long password 1234", now=NOW
    )
    await set_directive(
        session,
        kind="no_new_people",
        person=None,
        description=None,
        except_words=None,
        until=None,
        owner=alex,
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(InvitationUnavailable):
        await accept_invitation(session, joining, "hash", now=NOW + timedelta(minutes=2))
    del invitation
