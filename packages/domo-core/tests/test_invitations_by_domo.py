"""An invitation the assistant makes, for a record that exists (Task 17, S-K, WEB-3, Review Focus
5): the inviter of an act of his is the assistant, never the person of the turn; the account a
link of a record opens joins that record; a link that waits for someone is had in words too; and
nothing around a link is a sentence fixed in code."""

import ast
import asyncio
import inspect
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from domo_common.i18n import t
from domo_common.meanings import group_of, written_groups
from domo_core.accounts.models import Account, Person
from domo_core.audit.models import AuditEntry
from domo_core.channels.api import render as api_render
from domo_core.channels.page.cards import action_card
from domo_core.chat.models import MessageAction
from domo_core.confirmations.chain import _open_invitation
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.conversation.visits import what_is_new
from domo_core.household import routes as household_routes
from domo_core.household.models import Invitation
from domo_core.household.service import (
    InvitationUnavailable,
    accept_invitation,
    check_joining,
    create_invitation,
    reveal_link,
)
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.models import ProviderUser
from domo_core.llm.newcomers import newcomers_told, untold_newcomers
from domo_core.llm.router import usable_keys
from domo_core.llm.types import ToolCall
from domo_core.memory.models import Memory
from domo_core.migrate import alembic_config
from domo_core.people import directives, merge
from domo_core.people.acts import owed_rows
from domo_core.people.forget import forget
from domo_core.people.models import PersonAct, PersonLink
from domo_core.policy.models import PolicyRight
from domo_core.policy.settings import INVITEE_GENERAL_KEY, update_policy_setting
from domo_core.texts.view import REFERENCE, CatalogTexts, RecueilTexts
from domo_core.web.deps import get_clock
from domo_core.web.templating import create_templates, render_fragment
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_concerned_person import DOUBT, _judge, _people, _requests, _turn, _yes
from test_newcomers import add_call
from test_own_idea import LIVE, mark
from test_spending import add_key

REASON = "Sam says his friend Tom comes tonight"
PASSPHRASE = "a-long-passphrase"
_LINK = re.compile(r"/invite/([A-Za-z0-9_-]+)")
_TEMPLATES = Path(household_routes.__file__).parents[1] / "web" / "templates"


def _call(tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(uuid.uuid4().hex[:8], tool, json.dumps(arguments))


def invite_tom(**more: Any) -> ToolCall:
    return _call("household_invite", display_name="Tom", role="guest", guest_days=1, **more)


async def _invitation(session_factory: Any, name: str = "Tom") -> Invitation:
    async with session_factory() as reader:
        return (
            await reader.execute(
                select(Invitation)
                .where(Invitation.display_name == name)
                .order_by(Invitation.created_at.desc())
                .limit(1)
            )
        ).scalar_one()


async def _person(session_factory: Any, person_id: uuid.UUID) -> Person:
    async with session_factory() as reader:
        found = await reader.get(Person, person_id)
        assert found is not None
        return found


async def _join(session_factory: Any, token: str, username: str = "tom") -> Person:
    """Someone joins with the link of ``token`` (the page's two steps, a hash of no password)."""
    async with session_factory() as session:
        joining = await check_joining(
            session, token=token, username=username, password=PASSPHRASE, now=LIVE
        )
        person = await accept_invitation(session, joining, "hash", now=LIVE)
        await session.commit()
        return person


async def _token_for(session_factory: Any, invitation: Invitation, holder: Person) -> str:
    """The link of an invitation that waits, made for the one it waits for."""
    async with session_factory() as session:
        token = await reveal_link(session, invitation.id, holder.id, LIVE)
        await session.commit()
        return token


def _token_of(record: Any) -> str:
    assert record.card_once is not None, record.model_result
    found = _LINK.search(record.card_once["link"])
    assert found is not None
    return found.group(1)


async def _guest_invites_tom(session_factory: Any, monkeypatch: Any) -> dict[str, Person]:
    """S-K's guest case: Sam, a guest without the right to invite, says his friend Tom comes
    tonight; the assistant marks his idea and invites Tom with his reason."""
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite_tom(reason=REASON), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert record.card_once is None
    assert record.model_result["link"] == "waits_for_the_owner"
    return people


# --- the inviter of an act of his is the assistant (S-K) -----------------------------------------


async def test_an_invitation_born_of_a_guest_s_words_has_the_assistant_as_inviter(
    session_factory, monkeypatch
):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    alex, sam = people["Alex"], people["Sam"]
    invitation = await _invitation(session_factory)
    assert invitation.created_by is None and invitation.by_domo is True
    assert invitation.link_for_id == alex.id and invitation.token_hash is None
    assert invitation.reason == REASON and invitation.kind == "new"
    async with session_factory() as reader:
        row = (
            await reader.execute(
                text("SELECT * FROM invitations WHERE id = :id"), {"id": invitation.id}
            )
        ).one()
    assert sam.id not in tuple(row)
    async with session_factory() as reader:
        [stored] = await reader.scalars(
            select(MessageAction).where(MessageAction.tool_name == "household_invite")
        )
    assert stored.by_domo and stored.idea_reason == REASON


async def test_the_guest_can_neither_cancel_it_nor_give_it_again(session_factory, monkeypatch):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    before = await _invitation(session_factory)
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    for tool in ("household_cancel_invitation", "invitation_link_again", "invitation_link_show"):
        record = await handle_tool_call(_call(tool, display_name="Tom"), ctx=ctx, reply_id=reply_id)
        assert record.status in ("refused", "invalid"), (tool, record.model_result)
        assert record.card_once is None
    after = await _invitation(session_factory)
    assert after.cancelled_at is None and after.token_hash == before.token_hash
    assert after.link_for_id == people["Alex"].id
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    cancelled = await handle_tool_call(
        _call("household_cancel_invitation", display_name="Tom"), ctx=ctx, reply_id=reply_id
    )
    assert cancelled.status == "done", cancelled.model_result
    assert (await _invitation(session_factory)).cancelled_at is not None


async def test_who_holds_the_cancel_right_changes_his_invitation_and_he_judges(
    session_factory, monkeypatch
):
    """The owner's answer of 2026-11-27 ("Oui, Domo juge"): an invitation the assistant made is
    changed by whoever holds the right, Dana here, whom Alex opened inviting and cancelling to;
    the judge is shown that it is the assistant's (``given_by_domo``)."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"), inviters=("Dana",)
    )
    dana = people["Dana"]
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=dana.id,
                action_type="household.cancel_invitation",
                access="allow",
                summary="Dana may cancel an invitation.",
                owner_words="she may cancel",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    judged = _judge(monkeypatch)
    sams, sams_reply, _ = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(REASON), ctx=sams, reply_id=sams_reply)
    made = await handle_tool_call(invite_tom(reason=REASON), ctx=sams, reply_id=sams_reply)
    assert made.status == "done", made.model_result
    before = await _invitation(session_factory)
    ctx, reply_id, _ = await _turn(session_factory, dana)
    for tool in ("invitation_link_again", "household_cancel_invitation"):
        record = await handle_tool_call(_call(tool, display_name="Tom"), ctx=ctx, reply_id=reply_id)
        assert record.status == "done", (tool, record.model_result)
        assert judged.cases[-1].action["facts"]["given_by_domo"] is True
    after = await _invitation(session_factory)
    assert after.cancelled_at is not None and after.token_hash != before.token_hash


async def test_an_invitation_someone_made_stays_theirs_to_change(session_factory, monkeypatch):
    """``theirs_to_change`` (the owner's trial of 0.9.1), untouched by his answer of 2026-11-27:
    an invitation Dana made is not Sam's to cancel, though he holds the right."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "adult"), inviters=("Dana",)
    )
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=people["Sam"].id,
                action_type="household.cancel_invitation",
                access="allow",
                summary="Sam may cancel an invitation.",
                owner_words="he may cancel",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    _judge(monkeypatch)
    danas, danas_reply, _ = await _turn(session_factory, people["Dana"])
    made = await handle_tool_call(invite_tom(), ctx=danas, reply_id=danas_reply)
    assert made.status == "done", made.model_result
    ctx, reply_id, _ = await _turn(session_factory, people["Sam"])
    record = await handle_tool_call(
        _call("household_cancel_invitation", display_name="Tom"), ctx=ctx, reply_id=reply_id
    )
    assert record.model_result["reason"] == "not_possible", record.model_result
    assert (await _invitation(session_factory)).cancelled_at is None


async def test_the_arrival_is_not_announced_to_the_guest_as_his_guest(session_factory, monkeypatch):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    alex, sam = people["Alex"], people["Sam"]
    general = await add_key(session_factory, alex.id, general=True)
    await add_key(session_factory, sam.id)
    news_before = (await _person(session_factory, sam.id)).news_at
    tom = await _join(
        session_factory, await _token_for(session_factory, await _invitation(session_factory), alex)
    )
    await add_call(session_factory, general.id, tom, LIVE)
    async with session_factory() as session:
        sams = await what_is_new(session, await session.get(Person, sam.id), since=None)  # type: ignore[arg-type]
        alexs = await what_is_new(session, await session.get(Person, alex.id), since=None)  # type: ignore[arg-type]
        told = await newcomers_told(
            session, giver_id=alex.id, now=LIVE, zone=None, catalog=shipped_catalog(), mark=False
        )
    assert sams["newcomers"] == 0 and alexs["newcomers"] == 1
    assert (await _person(session_factory, sam.id)).news_at == news_before
    # Someone the assistant invited: nobody is their host.
    assert [(n.person_name, n.host_name) for n in told] == [("Tom", None)]


async def test_key_use_does_not_follow_the_guest(session_factory, monkeypatch):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    alex, sam = people["Alex"], people["Sam"]
    general = await add_key(session_factory, alex.id, general=True)
    sams_key = await add_key(session_factory, sam.id)
    tom = await _join(
        session_factory, await _token_for(session_factory, await _invitation(session_factory), alex)
    )
    assert tom.invited_by is None
    async with session_factory() as session:
        lines = list(
            await session.scalars(select(ProviderUser).where(ProviderUser.person_id == tom.id))
        )
        keys = [key.id for key in await usable_keys(session, await session.get(Person, tom.id))]  # type: ignore[arg-type]
    assert lines == []
    assert keys == [general.id] and sams_key.id not in keys


async def test_a_chain_takes_the_assistant_s_new_link_for_the_one_that_ended(
    session_factory, monkeypatch
):
    """The owner's answer of 2026-11-27 ("Domo juge et continue"): a link broke, the assistant
    invited again; the chain goes on under the yes it had when his invitation replaces the one
    that ended, for the same person and the same role. Anything else of his stays as before: never
    on a first reach of a wait, nor for another role."""
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex = people["Alex"]
    yes_at = LIVE - timedelta(minutes=5)
    chain = Confirmation(requester_id=alex.id, decided_at=yes_at)
    async with session_factory() as session:
        before, _ = await create_invitation(
            session,
            display_name="Paul",
            role="child",
            guest_days=None,
            created_by=None,
            now=yes_at - timedelta(minutes=1),
            link_for=alex.id,
            reason="Alex said Paul's friend comes",
        )
        ended, _ = await create_invitation(
            session,
            display_name="Tom",
            role="guest",
            guest_days=1,
            created_by=alex.id,
            now=yes_at - timedelta(minutes=2),
        )
        ended.cancelled_at = LIVE - timedelta(minutes=1)
        await session.flush()
        after, _ = await create_invitation(
            session,
            display_name="Tom",
            role="guest",
            guest_days=1,
            created_by=None,
            now=LIVE,
            link_for=alex.id,
            reason=REASON,
        )
        # The one he made again for the link that ended, same person and role: taken.
        found = await _open_invitation(
            session, chain, "Tom", LIVE, made_by_the_requester_only=True, replaces=ended
        )
        assert found is not None and found.id == after.id
        # Never on a first reach of the wait (nothing ended there), as before.
        for only_theirs in (False, True):
            assert (
                await _open_invitation(
                    session, chain, "Tom", LIVE, made_by_the_requester_only=only_theirs
                )
                is None
            )
        # Never for another role: it is not the same request.
        ended.role, ended.guest_days = "adult", None
        await session.flush()
        assert (
            await _open_invitation(
                session, chain, "Tom", LIVE, made_by_the_requester_only=True, replaces=ended
            )
            is None
        )
        # One that already waited when the yes was given is, as for anyone's.
        taken = await _open_invitation(
            session, chain, "Paul", LIVE, made_by_the_requester_only=False
        )
        assert taken is not None and taken.id == before.id


async def test_the_same_holds_when_the_request_waited_for_the_owner_s_yes(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex, sam = people["Alex"], people["Sam"]
    _judge(monkeypatch, DOUBT)
    ctx, reply_id, _ = await _turn(session_factory, sam)
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    record = await handle_tool_call(invite_tom(reason=REASON), ctx=ctx, reply_id=reply_id)
    assert record.status == "waiting", record.model_result
    [request] = await _requests(session_factory)
    assert request.by_domo and request.concerned_id == alex.id
    _judge(monkeypatch)
    await _yes(ctx, request, alex)
    invitation = await _invitation(session_factory)
    assert invitation.created_by is None and invitation.by_domo
    assert invitation.link_for_id == alex.id and invitation.token_hash is None
    # The owner agreed to it himself: he is not told it again.
    [act] = await _invited_acts(session_factory)
    assert (act.owed_to_owner, act.told_owner_at) == (True, LIVE)


async def test_someone_who_invites_by_their_own_request_stays_the_inviter(
    session_factory, monkeypatch
):
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"), inviters=("Dana",)
    )
    dana = people["Dana"]
    _judge(monkeypatch)
    # Cancelling is a gesture of its own, which the owner opens to her too.
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=dana.id,
                action_type="household.cancel_invitation",
                access="allow",
                summary="Dana may cancel an invitation.",
                owner_words="she may cancel",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    danas_key = await add_key(session_factory, dana.id)
    ctx, reply_id, _ = await _turn(session_factory, dana)
    made = await handle_tool_call(invite_tom(), ctx=ctx, reply_id=reply_id)
    assert made.status == "done", made.model_result
    invitation = await _invitation(session_factory)
    assert invitation.created_by == dana.id and invitation.by_domo is False
    again = await handle_tool_call(
        _call("invitation_link_again", display_name="Tom"), ctx=ctx, reply_id=reply_id
    )
    assert again.status == "done", again.model_result
    other = await handle_tool_call(
        _call("household_invite", display_name="Paul", role="child"), ctx=ctx, reply_id=reply_id
    )
    assert other.status == "done", other.model_result
    cancelled = await handle_tool_call(
        _call("household_cancel_invitation", display_name="Paul"), ctx=ctx, reply_id=reply_id
    )
    assert cancelled.status == "done", cancelled.model_result
    tom = await _join(session_factory, _token_of(again))
    assert tom.invited_by == dana.id
    await add_call(session_factory, danas_key.id, tom, LIVE)
    async with session_factory() as session:
        told = await newcomers_told(
            session, giver_id=dana.id, now=LIVE, zone=None, catalog=shipped_catalog(), mark=False
        )
        followed = await session.scalar(
            select(ProviderUser.allowed).where(
                ProviderUser.person_id == tom.id, ProviderUser.provider_id == danas_key.id
            )
        )
    assert [(n.person_name, n.host_name) for n in told] == [("Tom", "Dana")]
    assert followed is True


async def test_the_migration_gives_the_assistant_the_invitations_he_made_in_between(
    scratch_database_url,
):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0059")
    alex, sam, dana, tom = (str(uuid.uuid4()) for _ in range(4))
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) VALUES "
                    f"('{alex}', 'Alex', 'owner'), ('{sam}', 'Sam', 'guest'), "
                    f"('{dana}', 'Dana', 'adult')"
                )
            )
            # Tom joined with the invitation the assistant made from Sam's words.
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role, invited_by) VALUES "
                    f"('{tom}', 'Tom', 'guest', '{sam}')"
                )
            )
            conversation = (
                await connection.execute(
                    text(f"INSERT INTO conversations (person_id) VALUES ('{sam}') RETURNING id")
                )
            ).scalar_one()
            message = (
                await connection.execute(
                    text(
                        "INSERT INTO messages (conversation_id, role, status, provenance) "
                        f"VALUES ('{conversation}', 'assistant', 'complete', 'model') RETURNING id"
                    )
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO message_actions (message_id, tool_name, action_type, status, "
                    "summary, by_domo, subject_key, idea_reason) VALUES "
                    f"('{message}', 'household_invite', 'household.invite', 'done', '{{}}', "
                    f"true, 'invitee:tom', :reason), "
                    # He gave again the link of the invitation Dana made herself: still hers.
                    f"('{message}', 'invitation_link_again', 'household.invite_again', 'done', "
                    f"'{{}}', true, 'invitee:paul', :reason)"
                ),
                {"reason": REASON},
            )
            await connection.execute(
                text(
                    "INSERT INTO invitations (display_name, role, guest_days, created_by, "
                    "expires_at, accepted_at, accepted_person_id) VALUES "
                    f"('Tom', 'guest', 1, '{sam}', now() + interval '7 days', now(), '{tom}'), "
                    f"('Paul', 'child', NULL, '{dana}', now() + interval '7 days', NULL, NULL)"
                )
            )
        await asyncio.to_thread(command.upgrade, config, "0060")
        async with engine.connect() as connection:
            rows = {
                row[0]: row[1:]
                for row in await connection.execute(
                    text(
                        "SELECT display_name, created_by::text, by_domo, link_for_id::text, "
                        "reason, kind FROM invitations"
                    )
                )
            }
        assert rows["Tom"] == (None, True, None, REASON, "new")
        assert rows["Paul"] == (dana, False, None, None, "new")
        async with engine.connect() as connection:
            hosts = [
                (
                    await connection.execute(
                        text(f"SELECT invited_by::text FROM persons WHERE id = '{tom}'")
                    )
                ).scalar_one()
            ]
        await asyncio.to_thread(command.downgrade, config, "0059")
        async with engine.connect() as connection:
            hosts.append(
                (
                    await connection.execute(
                        text(f"SELECT invited_by::text FROM persons WHERE id = '{tom}'")
                    )
                ).scalar_one()
            )
        async with engine.connect() as connection:
            back = {
                row[0]: row[1:]
                for row in await connection.execute(
                    text(
                        "SELECT display_name, created_by::text, cancelled_at IS NOT NULL "
                        "FROM invitations"
                    )
                )
            }
    finally:
        await engine.dispose()
    # Accepted, Tom's invitation is not cancelled; its maker, and Tom's inviter, is the owner again.
    assert back == {"Tom": (alex, False), "Paul": (dana, False)}
    assert hosts == [None, alex]


# --- with no tool to send: the link goes to the one who asked (WEB-3, part a) --------------------


async def test_without_a_tool_the_link_goes_once_to_the_one_who_asked(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"))
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await handle_tool_call(invite_tom(), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    token = _token_of(record)
    assert token not in json.dumps(record.model_result) and token not in json.dumps(record.card)
    async with session_factory() as reader:
        stored = list(await reader.scalars(select(MessageAction.summary)))
        audit = list(await reader.scalars(select(AuditEntry.details)))
    assert token not in json.dumps(stored) and token not in json.dumps(audit)


async def test_an_invitation_nobody_asked_for_waits_on_the_owner_s_card(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Sam", "guest"))
    alex, sam = people["Alex"], people["Sam"]
    _judge(monkeypatch)
    # A turn of his own, in the owner's conversation.
    his, reply_id, _ = await _turn(session_factory, alex, on_his_own=True, reply_message_id=None)
    own = await handle_tool_call(
        invite_tom(reason="the guest room is ready"), ctx=his, reply_id=reply_id
    )
    # A review of Sam's conversation: nobody reads it.
    review, review_reply, _ = await _turn(
        session_factory, sam, no_reader=True, reply_message_id=None
    )
    await handle_tool_call(mark("Sam spoke of Paul"), ctx=review, reply_id=review_reply)
    reviewed = await handle_tool_call(
        _call("household_invite", display_name="Paul", role="guest", reason="Sam spoke of Paul"),
        ctx=review,
        reply_id=review_reply,
    )
    for record, name in ((own, "Tom"), (reviewed, "Paul")):
        assert record.status == "done", record.model_result
        assert record.card_once is None
        invitation = await _invitation(session_factory, name)
        assert invitation.token_hash is None and invitation.link_for_id == alex.id
    # The owner asks for it in words: given once, a second call does not.
    ctx, reply_id, _ = await _turn(session_factory, alex)
    shown = await handle_tool_call(
        _call("invitation_link_show", display_name="Tom"), ctx=ctx, reply_id=reply_id
    )
    assert shown.status == "done", shown.model_result
    assert _token_of(shown) not in json.dumps(shown.model_result)
    again = await handle_tool_call(
        _call("invitation_link_show", display_name="Tom"), ctx=ctx, reply_id=reply_id
    )
    assert again.card_once is None and again.model_result["reason"] == "not_found"


# --- a link that waits can be had in words (WEB-3, part b) ---------------------------------------


async def test_the_link_that_waits_is_given_in_words_on_the_api_channel(
    session_factory, monkeypatch
):
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "adult"), inviters=("Dana",)
    )
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, dana)
    asked = await handle_tool_call(invite_tom(owner_first=True), ctx=ctx, reply_id=reply_id)
    assert asked.status == "waiting", asked.model_result
    [request] = await _requests(session_factory)
    await _yes(ctx, request, alex)
    assert (await _invitation(session_factory)).link_for_id is None
    show = _call("invitation_link_show", display_name="Tom")
    # As his own idea, or in a turn nobody reads: not here.
    his_turn, his_reply, _ = await _turn(
        session_factory, dana, on_his_own=True, reply_message_id=None
    )
    his = await handle_tool_call(show, ctx=his_turn, reply_id=his_reply)
    assert his.card_once is None and his.model_result["reason"] == "not_possible"
    unread, unread_reply, _ = await _turn(
        session_factory, dana, no_reader=True, reply_message_id=None
    )
    nobody = await handle_tool_call(show, ctx=unread, reply_id=unread_reply)
    assert nobody.card_once is None and nobody.model_result["reason"] == "not_possible"
    # Anyone else but the owner: there is nothing for them.
    sams, sams_reply, _ = await _turn(session_factory, people["Sam"])
    theirs = await handle_tool_call(show, ctx=sams, reply_id=sams_reply)
    assert theirs.card_once is None and theirs.model_result["reason"] == "not_found"
    # The one it waits for: once, as the API channel gives it.
    ctx, reply_id, _ = await _turn(session_factory, dana, origin="https://house.example")
    shown = await handle_tool_call(show, ctx=ctx, reply_id=reply_id)
    assert shown.status == "done", shown.model_result
    once = api_render.once_of(shown.card_once, REFERENCE)
    assert once is not None and once["kind"] == "link" and _token_of(shown) in once["value"]


async def test_the_owner_is_shown_a_link_that_waits_for_someone_else(session_factory, monkeypatch):
    """The owner's answer of 2026-11-27 ("Il te le montre"): the owner asks to see the link that
    waits for Dana; it is shown to him once, as hers would be (``card_once``: from the code to his
    page, never to the model), and the assistant is told whom it waited for."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"), inviters=("Dana",)
    )
    alex, dana = people["Alex"], people["Dana"]
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, dana)
    asked = await handle_tool_call(invite_tom(owner_first=True), ctx=ctx, reply_id=reply_id)
    [request] = await _requests(session_factory)
    await _yes(ctx, request, alex)
    assert asked.status == "waiting"
    show = _call("invitation_link_show", display_name="Tom")
    # A guest asking for it gets nothing: it is not theirs.
    sams, sams_reply, _ = await _turn(session_factory, people["Sam"])
    guests = await handle_tool_call(show, ctx=sams, reply_id=sams_reply)
    assert guests.card_once is None and guests.model_result["reason"] == "not_found"
    owners, owners_reply, told = await _turn(session_factory, alex)
    shown = await handle_tool_call(show, ctx=owners, reply_id=owners_reply)
    assert shown.status == "done", shown.model_result
    token = _token_of(shown)
    assert token not in json.dumps(shown.model_result) and token not in json.dumps(shown.card)
    assert shown.model_result["waited_for"] == "Dana"
    # Dana has news of it.
    assert told == [dana.id]
    assert (await _person(session_factory, dana.id)).news_at == LIVE
    # One link, shown once: it no longer waits for Dana.
    ctx, reply_id, _ = await _turn(session_factory, dana)
    hers = await handle_tool_call(show, ctx=ctx, reply_id=reply_id)
    assert hers.card_once is None and hers.model_result["reason"] == "not_found"
    # Joining with the link the owner was shown works.
    tom = await _join(session_factory, token)
    assert tom.display_name == "Tom"


def test_the_on_request_cards_are_said_in_words():
    expires = datetime(2030, 1, 5, 12, tzinfo=UTC).isoformat()
    for action in ("invite", "invite_again"):
        key = f"action.card.done.household.{action}.on_request"
        assert key in api_render.CARDS_IN_WORDS
        assert group_of(f"{key}.in_words") in written_groups()
        for language in ("en", "fr"):
            said = t(f"{key}.in_words", language, name="Tom", expires="x")
            assert "Tom" in said
            for word in ("button", "bouton"):
                assert word not in said.lower()
        card = {"key": key, "params": {"name": "Tom"}, "dates": {"expires": expires}}
        in_words = api_render.sentence_of_card(card, REFERENCE)
        assert in_words is not None and "Tom" in in_words
        assert in_words.startswith(REFERENCE(f"{key}.in_words", name="Tom", expires="")[:20])
        page = REFERENCE(key, name="Tom", expires="soon")
        assert api_render.in_words(f"Done. {page}", REFERENCE) == (
            f"Done. {REFERENCE(f'{key}.in_words', name='Tom', expires='soon')}"
        )


def _shown_text(template: str) -> str:
    """What a template shows a person outside what the code fills: its text, without the
    template's own syntax and the markup."""
    without = re.sub(r"\{#.*?#\}", " ", template, flags=re.S)
    without = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", without, flags=re.S)
    return re.sub(r"<[^>]+>", " ", without)


def test_no_fixed_sentence_around_the_link():
    for name in ("fragments/action_card.html", "fragments/invitation_link.html"):
        shown = _shown_text((_TEMPLATES / name).read_text(encoding="utf-8"))
        assert re.search(r"[A-Za-z]", shown) is None, (name, shown.strip())
    # The route says nothing in its own words: every text it shows comes from the reader's texts.
    tree = ast.parse(inspect.getsource(household_routes.show_link).lstrip())
    function = tree.body[0]
    assert isinstance(function, ast.AsyncFunctionDef)
    body = function.body[1:]  # its docstring is for the code
    literals = [
        node.value
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert all(" " not in literal for literal in literals), literals
    # A text written for the reader is what the page shows.
    written = RecueilTexts(
        language="en",
        zone=ZoneInfo("UTC"),
        found={"invite.link.show": "Give me the address"},
        personal=frozenset({"invite.link.show"}),
    )
    card = {
        "key": "action.card.done.household.invite.on_request",
        "params": {"name": "Tom"},
        "dates": {"expires": LIVE.isoformat()},
        "invitation": str(uuid.uuid4()),
    }
    shown = action_card(
        tool_name="household_invite", status="done", card=card, texts=written, for_requester=True
    )
    fragment = render_fragment(
        create_templates(), "fragments/action_card.html", written, action=shown
    )
    assert "Give me the address" in fragment
    assert t("invite.link.show", "en") not in fragment
    plain = render_fragment(
        create_templates(), "fragments/action_card.html", CatalogTexts("fr"), action=shown
    )
    assert t("invite.link.show", "fr") in plain


async def test_the_assistant_is_the_author_of_an_invitation(session_factory, monkeypatch):
    await _guest_invites_tom(session_factory, monkeypatch)
    invitation = await _invitation(session_factory)
    assert invitation.created_by is None and invitation.by_domo
    async with session_factory() as reader:
        [line] = await reader.scalars(
            select(AuditEntry).where(AuditEntry.action == "household.invited")
        )
    assert line.actor == "domo" and line.details["invitation_id"] == str(invitation.id)
    assert REASON not in json.dumps(line.details)


# --- the link of a record ------------------------------------------------------------------------


async def _sam_known(session_factory: Any, **values: Any) -> Person:
    """Sam, a record the assistant knows, with no account."""
    columns: dict[str, Any] = {
        "role": "guest",
        "state": "known",
        "standing": "lives_here",
        "origin": "domo",
        "origin_reason": "Alex said Sam moves in",
    } | values
    async with session_factory() as writer:
        sam = Person(display_name="Sam", **columns)
        writer.add(sam)
        await writer.commit()
    return sam


async def test_opening_the_link_attaches_the_account_to_the_record_that_existed(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    _judge(monkeypatch)
    sam = await _sam_known(session_factory)
    async with session_factory() as writer:
        memory = Memory(
            subject="Sam",
            content="Sam likes tea",
            kind="preference",
            origin="others_words",
            about_id=sam.id,
            said_by_id=alex.id,
        )
        writer.add(memory)
        writer.add(
            PersonLink(
                from_id=sam.id,
                to_id=alex.id,
                relation="partner of",
                said_by_id=alex.id,
                confirmed_at=LIVE,
            )
        )
        await writer.commit()
    async with session_factory() as reader:
        count = await reader.scalar(select(func.count()).select_from(Person))
    ctx, reply_id, _ = await _turn(session_factory, alex)
    record = await handle_tool_call(
        _call("household_invite", person="Sam", role="adult", username="sam"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done", record.model_result
    invitation = await _invitation(session_factory, "Sam")
    assert (invitation.kind, invitation.person_id, invitation.proposed_username) == (
        "record",
        sam.id,
        "sam",
    )
    joined = await _join(session_factory, _token_of(record), username="sam")
    assert joined.id == sam.id
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(Person)) == count
        account = await reader.scalar(select(Account).where(Account.username == "sam"))
        kept = await reader.get(Memory, memory.id)
        links = list(await reader.scalars(select(PersonLink).where(PersonLink.from_id == sam.id)))
    assert account is not None and account.person_id == sam.id
    assert kept is not None and kept.about_id == sam.id
    assert [link.relation for link in links] == ["partner of"]
    now = await _person(session_factory, sam.id)
    assert (now.state, now.role, now.origin) == ("has_access", "adult", "domo")


# --- the page ------------------------------------------------------------------------------------


@pytest.fixture
def clock(clock):
    """The test clock, set to the real time: the sessions the page opens are stamped with it."""
    clock.advance(datetime.now(UTC) - clock())
    return clock


@pytest.fixture
def app(app, clock):
    app.dependency_overrides[get_clock] = lambda: clock
    return app


async def test_the_page_of_a_record_with_an_account_says_so_at_once(
    client, owner_account, session, clock
):
    sam = Person(
        display_name="Sam",
        role="guest",
        state="known",
        standing="visiting",
        origin="domo",
        origin_reason="Alex said Sam comes",
    )
    session.add(sam)
    await session.flush()
    _, token = await create_invitation(
        session,
        display_name="Sam",
        role="guest",
        guest_days=2,
        created_by=owner_account.person_id,
        now=clock(),
        record=sam,
    )
    session.add(Account(person_id=sam.id, username="sam-before", password_hash="hash"))
    await session.commit()
    page = await client.get(f"/invite/{token}")
    assert page.status_code == 410
    assert t("invite.unavailable.title", "en") in page.text
    assert 'name="username"' not in page.text


async def test_a_record_with_access_is_invited_with_the_role_it_has(session_factory, monkeypatch):
    """What the page states is what they get: a record that has access keeps its role and its stay,
    whatever role the call named; joining gives it an account only."""
    people = await _people(session_factory, ("Alex", "owner"))
    _judge(monkeypatch)
    sam = await _sam_known(session_factory, role="adult", state="has_access")
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    record = await handle_tool_call(
        _call("household_invite", person="Sam", role="guest", guest_days=3),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert record.status == "done", record.model_result
    assert record.model_result["role"] == "adult"
    invitation = await _invitation(session_factory, "Sam")
    assert (invitation.role, invitation.guest_days) == ("adult", None)
    joined = await _join(session_factory, _token_of(record), username="sam")
    now = await _person(session_factory, joined.id)
    assert (now.id, now.role, now.expires_at) == (sam.id, "adult", None)


async def test_the_page_proposes_the_username(client, owner_account, session, clock):
    invitation, token = await create_invitation(
        session,
        display_name="Tom",
        role="adult",
        guest_days=None,
        created_by=owner_account.person_id,
        now=clock(),
        proposed_username="tom.d",
    )
    await session.commit()
    page = await client.get(f"/invite/{token}")
    assert page.status_code == 200
    assert 'value="tom.d"' in page.text
    assert t("invite.username_proposed", "en") in page.text
    joined = await client.post(
        f"/invite/{token}", data={"username": "tommy", "password": PASSPHRASE}
    )
    assert joined.status_code == 303
    account = (
        await session.execute(select(Account).where(Account.username == "tommy"))
    ).scalar_one()
    assert account.person_id == invitation.accepted_person_id


@pytest.mark.parametrize("case", ["merged", "forgotten", "account", "refused", "withdrawn"])
async def test_a_link_for_a_record_that_was_merged_forgotten_or_refused_is_unusable(
    client, owner_account, session, clock, case
):
    owner = await session.get(Person, owner_account.person_id)
    assert owner is not None
    sam = Person(
        display_name="Sam",
        role="guest",
        state="known",
        standing="visiting",
        origin="domo",
        origin_reason="Alex said Sam comes",
    )
    session.add(sam)
    await session.flush()
    _, token = await create_invitation(
        session,
        display_name="Sam",
        role="guest",
        guest_days=2,
        created_by=owner.id,
        now=clock(),
        record=sam,
    )
    await session.commit()
    match case:
        case "merged":
            other = Person(
                display_name="Dana",
                role="guest",
                state="known",
                standing="visiting",
                origin="domo",
                origin_reason="Alex said Dana comes",
            )
            session.add(other)
            await session.flush()
            await merge.merge(
                session,
                kept=other,
                absorbed=sam,
                by="domo",
                reason="the same man",
                turn_of=None,
                now=clock(),
            )
        case "forgotten":
            await forget(session, sam, by=f"person:{owner.id}", turn_of=owner.id, now=clock())
        case "account":
            session.add(Account(person_id=sam.id, username="sam-before", password_hash="hash"))
        case "refused" | "withdrawn":
            await directives.set_directive(
                session,
                kind="refuse" if case == "refused" else "withdraw",
                person=sam,
                description=None,
                except_words=None,
                until=None,
                owner=owner,
                now=clock(),
            )
    await session.commit()
    answer = await client.post(f"/invite/{token}", data={"username": "sam", "password": PASSPHRASE})
    assert answer.status_code == 410
    assert t("invite.unavailable.title", "en") in answer.text
    assert (
        await session.execute(select(Account).where(Account.username == "sam"))
    ).scalar_one_or_none() is None


async def test_a_record_under_a_directive_set_after_the_check_is_refused_at_the_join(
    session_factory, monkeypatch
):
    """``accept_invitation`` checks the record again: a refusal that came between the check of
    the link and the account is in force."""
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    sam = await _sam_known(session_factory)
    async with session_factory() as session:
        _, token = await create_invitation(
            session,
            display_name="Sam",
            role="guest",
            guest_days=2,
            created_by=alex.id,
            now=LIVE,
            record=await session.get(Person, sam.id),
        )
        await session.commit()
    async with session_factory() as session:
        joining = await check_joining(
            session, token=token, username="sam", password=PASSPHRASE, now=LIVE
        )
        await directives.set_directive(
            session,
            kind="refuse",
            person=await session.get(Person, sam.id),
            description=None,
            except_words=None,
            until=None,
            owner=await session.get(Person, alex.id),  # type: ignore[arg-type]
            now=LIVE,
        )
        with pytest.raises(InvitationUnavailable):
            await accept_invitation(session, joining, "hash", now=LIVE)


async def _invited_acts(session_factory: Any) -> list[PersonAct]:
    async with session_factory() as reader:
        return list(await reader.scalars(select(PersonAct).where(PersonAct.act == "invited")))


async def test_the_assistant_invites_a_visitor_on_his_own_and_the_owner_is_told(
    session_factory, monkeypatch
):
    """The owner's answer of 2026-11-27 ("Oui, tu es prévenu"): the assistant invites on his own
    idea a record that does not live here; the owner is told: in his own turn as it happens, in
    anyone else's afterwards (an act owed to him, his news)."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), inviters=("Dana",)
    )
    alex = people["Alex"]
    _judge(monkeypatch)
    await _sam_known(session_factory, standing="visiting")
    ctx, reply_id, notified = await _turn(session_factory, alex)
    await handle_tool_call(mark("Sam would like it"), ctx=ctx, reply_id=reply_id)
    his = await handle_tool_call(
        _call("household_invite", person="Sam", role="guest", reason="Sam would like it"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert his.status == "done", his.model_result
    [act] = await _invited_acts(session_factory)
    assert act.owed_to_owner is True and act.told_owner_at == LIVE
    assert notified == []
    # In Dana's turn, of a visitor Tom: the owner is owed the telling, and his news is kept.
    async with session_factory() as writer:
        writer.add(
            Person(
                display_name="Tom",
                role="guest",
                state="known",
                standing="visiting",
                origin="domo",
                origin_reason="Dana spoke of Tom",
            )
        )
        await writer.commit()
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark("Dana's friend Tom comes"), ctx=ctx, reply_id=reply_id)
    danas = await handle_tool_call(
        _call("household_invite", person="Tom", role="guest", reason="Dana's friend Tom comes"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert danas.status == "done", danas.model_result
    toms = [a for a in await _invited_acts(session_factory) if a.id != act.id]
    assert [(a.owed_to_owner, a.told_owner_at) for a in toms] == [(True, None)]
    assert notified == [alex.id]
    assert (await _person(session_factory, alex.id)).news_at == LIVE


async def test_what_the_owner_sees_in_his_own_turn_is_not_told_again(session_factory, monkeypatch):
    """Every invitation of the assistant's own idea is owed to the owner, someone who lives here
    too; in the owner's own turn that he sees, it is told as it happens, once."""
    people = await _people(session_factory, ("Alex", "owner"))
    _judge(monkeypatch)
    await _sam_known(session_factory)
    ctx, reply_id, notified = await _turn(session_factory, people["Alex"])
    await handle_tool_call(mark("Sam moves in"), ctx=ctx, reply_id=reply_id)
    his = await handle_tool_call(
        _call("household_invite", person="Sam", role="adult", reason="Sam moves in"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert his.status == "done", his.model_result
    [act] = await _invited_acts(session_factory)
    assert (act.owed_to_owner, act.told_owner_at) == (True, LIVE) and notified == []


async def test_the_owner_is_told_of_someone_new_he_invites_by_name(session_factory, monkeypatch):
    """The reviewer's case: in Dana's turn the assistant invites Tom, a passing friend, by name; the
    link is Dana's, and the owner is told, once, by the invitation's name and role."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), inviters=("Dana",)
    )
    alex = people["Alex"]
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark("Dana's friend Tom comes"), ctx=ctx, reply_id=reply_id)
    his = await handle_tool_call(
        invite_tom(reason="Dana's friend Tom comes"), ctx=ctx, reply_id=reply_id
    )
    assert his.status == "done", his.model_result
    [act] = await _invited_acts(session_factory)
    invitation = await _invitation(session_factory)
    assert (act.person_id, act.invitation_id) == (None, invitation.id)
    assert (act.owed_to_owner, act.told_owner_at) == (True, None)
    assert notified == [alex.id]
    async with session_factory() as reader:
        [row] = await owed_rows(reader)
    assert (row["act"], row["person"], row["role"]) == ("invited", "Tom", "guest")


async def test_without_an_inviter_the_house_key_pays(session_factory, monkeypatch):
    people = await _guest_invites_tom(session_factory, monkeypatch)
    general = await add_key(session_factory, people["Alex"].id, general=True)
    async with session_factory() as session:
        # The household's rule that invitees who join never use the general key: his own
        # invitee uses it as he does.
        await update_policy_setting(
            session, key=INVITEE_GENERAL_KEY, value="never", by=people["Alex"].id, now=LIVE
        )
        await session.commit()
    tom = await _join(
        session_factory,
        await _token_for(session_factory, await _invitation(session_factory), people["Alex"]),
    )
    async with session_factory() as session:
        lines = await session.scalar(
            select(func.count()).select_from(ProviderUser).where(ProviderUser.person_id == tom.id)
        )
        keys = [key.id for key in await usable_keys(session, await session.get(Person, tom.id))]  # type: ignore[arg-type]
        untold = await untold_newcomers(session, giver_id=people["Sam"].id)
    assert lines == 0 and keys == [general.id] and untold == 0


async def test_upgrade_then_downgrade(scratch_database_url):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0060")
    alex, sam = str(uuid.uuid4()), str(uuid.uuid4())
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role, state, origin, origin_reason) "
                    f"VALUES ('{alex}', 'Alex', 'owner', 'has_access', 'setup', NULL), "
                    f"('{sam}', 'Sam', 'guest', 'known', 'domo', 'he comes')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO invitations (display_name, role, guest_days, created_by, by_domo, "
                    "reason, kind, person_id, link_for_id, expires_at) VALUES "
                    f"('Sam', 'guest', 2, '{alex}', false, NULL, 'record', '{sam}', NULL, "
                    "now() + interval '7 days'), "
                    f"('Tom', 'guest', 1, NULL, true, 'he comes', 'new', NULL, '{alex}', "
                    "now() + interval '7 days'), "
                    f"('Paul', 'child', NULL, '{alex}', false, NULL, 'new', NULL, NULL, "
                    "now() + interval '7 days')"
                )
            )
        await asyncio.to_thread(command.downgrade, config, "0059")
        async with engine.connect() as connection:
            rows = {
                row[0]: row[1:]
                for row in await connection.execute(
                    text(
                        "SELECT display_name, created_by::text, cancelled_at IS NOT NULL, "
                        "person_id IS NULL FROM invitations"
                    )
                )
            }
        await asyncio.to_thread(command.upgrade, config, "0060")
    finally:
        await engine.dispose()
    # The link of a record has no person any more: before 0060 that would be a return link.
    assert rows == {
        "Sam": (alex, True, True),
        "Tom": (alex, True, True),
        "Paul": (alex, False, True),
    }


async def test_going_down_with_no_owner_gives_the_one_its_link_was_for(scratch_database_url):
    config = alembic_config(scratch_database_url)
    await asyncio.to_thread(command.upgrade, config, "0060")
    dana = str(uuid.uuid4())
    engine = create_async_engine(scratch_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO persons (id, display_name, role) VALUES "
                    f"('{dana}', 'Dana', 'adult')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO invitations (display_name, role, guest_days, created_by, by_domo, "
                    "reason, kind, link_for_id, expires_at) VALUES "
                    f"('Tom', 'guest', 1, NULL, true, 'he comes', 'new', '{dana}', "
                    "now() + interval '7 days')"
                )
            )
        await asyncio.to_thread(command.downgrade, config, "0059")
        async with engine.connect() as connection:
            maker = (
                await connection.execute(text("SELECT created_by::text FROM invitations"))
            ).scalar_one()
        await asyncio.to_thread(command.upgrade, config, "0060")
    finally:
        await engine.dispose()
    assert maker == dana


# --- the owner is told of a link of his own idea (final review B-I1, B-I2) ----------------------


async def _owed_invitations(session_factory: Any) -> list[PersonAct]:
    async with session_factory() as reader:
        found = await reader.scalars(
            select(PersonAct).where(PersonAct.act == "invited", PersonAct.owed_to_owner.is_(True))
        )
        return list(found)


async def test_a_new_link_of_his_own_idea_is_told_to_the_owner(session_factory, monkeypatch):
    """The message to Tom bounced: the assistant gives the link again on his own idea. The new
    link waits for Alex, and Alex is told of it, as of the invitation itself."""
    people = await _guest_invites_tom(session_factory, monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    again = await handle_tool_call(
        _call("invitation_link_again", display_name="Tom", reason="the message bounced"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert again.status == "done", again.model_result
    assert again.model_result["link"] == "waits_for_the_owner"
    invitation = await _invitation(session_factory)
    acts = await _owed_invitations(session_factory)
    assert [act.invitation_id for act in acts] == [invitation.id, invitation.id]
    assert notified == [people["Alex"].id]


async def test_a_bring_back_of_his_own_idea_is_told_to_the_owner(session_factory, monkeypatch):
    """In Dana's turn the assistant brings Sam (removed, with an account) back on his own idea:
    the link waits for Alex, and Alex is told of it."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "adult"))
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="household.bring_back",
                access="allow",
                summary="The assistant may.",
                owner_words="you may",
                created_by=people["Alex"].id,
            )
        )
        sam = await writer.get(Person, people["Sam"].id)
        assert sam is not None
        sam.state, sam.disabled_at = "gone", LIVE - timedelta(days=30)
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    back = await handle_tool_call(
        _call("household_bring_back", person="Sam", reason="Dana says Sam is back in town"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert back.status == "done", back.model_result
    assert back.model_result["link"] == "waits_for_the_owner"
    [act] = await _owed_invitations(session_factory)
    assert act.person_id == people["Sam"].id and act.invitation_id is not None
    assert notified == [people["Alex"].id]


async def test_the_owner_s_exception_lets_his_judged_invitation_through(
    session_factory, monkeypatch
):
    """Alex: "nobody new, except the family". Dana: "my sister Sam comes, invite her". The
    assistant judges she is family, names the rule's exception, and the invitation is made and
    traced; without the exception, it is the owner's rule that answers."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    async with session_factory() as writer:
        alex = await writer.get(Person, people["Alex"].id)
        assert alex is not None
        rule = await directives.set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description=None,
            except_words="except the family",
            until=None,
            owner=alex,
            now=LIVE - timedelta(hours=1),
        )
        rule_id = rule.id
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    sam = _call(
        "household_invite", display_name="Sam", role="guest", guest_days=2, reason="Dana's sister"
    )
    refused = await handle_tool_call(sam, ctx=ctx, reply_id=reply_id)
    assert refused.model_result["reason"] == "owner_directive", refused.model_result
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    excepted = _call(
        "household_invite",
        display_name="Sam",
        role="guest",
        guest_days=2,
        reason="Dana's sister: family",
        exception_of=str(rule_id),
    )
    done = await handle_tool_call(excepted, ctx=ctx, reply_id=reply_id)
    assert done.status == "done", done.model_result
    async with session_factory() as reader:
        [used] = await reader.scalars(select(PersonAct).where(PersonAct.act == "exception_used"))
    assert used.directive_id == rule_id and used.by == "domo"
    # A person's own call does not name an exception: that is the assistant's judgement.
    theirs = await handle_tool_call(
        _call("household_invite", display_name="Tom", role="guest", exception_of=str(rule_id)),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert theirs.status == "invalid", theirs.model_result


async def test_his_own_invitation_waits_for_the_owner_when_her_right_has_his_limit(
    session_factory, monkeypatch
):
    """Alex wrote "Dana may invite guests; anyone else, judge it". Dana: "it would be good if Tom
    had an adult access". The assistant invites Tom as an adult on his own idea, judged: Dana's
    right does not plainly cover it, so the link waits for Alex, never shown to Dana."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=people["Dana"].id,
                action_type="household.invite",
                access="allow",
                limit_expression='action.facts.role == "guest" ? "allow" : "judge"',
                judge_question="Is this someone the household would want?",
                summary="Dana may invite guests; anyone else is judged.",
                owner_words="guests; anyone else, judge it",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    made = await handle_tool_call(
        _call("household_invite", display_name="Tom", role="adult", reason="Tom helps here"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert made.status == "done", made.model_result
    assert made.card_once is None
    assert made.model_result["link"] == "waits_for_the_owner"
    assert (await _invitation(session_factory)).link_for_id == people["Alex"].id


async def test_a_chain_goes_on_after_a_re_invitation_by_whoever_may_invite_never_for_a_namesake(
    session_factory,
):
    """Alex: "invite Tom, and make him an adult when he arrives". The link dies; Dana, who may
    invite, invites Tom (his record) again with the same role: the chain goes on. An invitation
    the assistant made for another Tom, someone new whose link is not Alex's, never takes its
    place."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    alex, dana = people["Alex"], people["Dana"]
    yes_at = LIVE - timedelta(minutes=5)
    chain = Confirmation(requester_id=alex.id, decided_at=yes_at)
    async with session_factory() as session:
        tom = Person(
            display_name="Tom",
            role="guest",
            state="known",
            origin="domo",
            origin_reason="Alex spoke of him",
            standing="visiting",
        )
        session.add(tom)
        await session.flush()
        ended, _ = await create_invitation(
            session,
            display_name="Tom",
            role="guest",
            guest_days=1,
            created_by=alex.id,
            now=yes_at - timedelta(minutes=2),
            record=tom,
        )
        ended.cancelled_at = LIVE - timedelta(minutes=1)
        await session.flush()
        again, _ = await create_invitation(
            session,
            display_name="Tom",
            role="guest",
            guest_days=1,
            created_by=dana.id,
            now=LIVE,
            record=tom,
        )
        found = await _open_invitation(
            session, chain, "Tom", LIVE, made_by_the_requester_only=True, replaces=ended
        )
        assert found is not None and found.id == again.id
        # Someone new: Alex's ended link, and the assistant's link for another Tom, for Dana.
        again.cancelled_at = LIVE
        new_ended, _ = await create_invitation(
            session,
            display_name="Sam",
            role="guest",
            guest_days=1,
            created_by=alex.id,
            now=yes_at - timedelta(minutes=2),
        )
        new_ended.cancelled_at = LIVE - timedelta(minutes=1)
        await session.flush()
        await create_invitation(
            session,
            display_name="Sam",
            role="guest",
            guest_days=1,
            created_by=None,
            now=LIVE,
            link_for=dana.id,
            reason="Dana's friend Sam comes",
        )
        assert (
            await _open_invitation(
                session, chain, "Sam", LIVE, made_by_the_requester_only=True, replaces=new_ended
            )
            is None
        )


async def _no_new_people(session_factory: Any, alex: Person, words: str | None = None) -> uuid.UUID:
    async with session_factory() as writer:
        owner = await writer.get(Person, alex.id)
        assert owner is not None
        rule = await directives.set_directive(
            writer,
            kind="no_new_people",
            person=None,
            description=None,
            except_words=words,
            until=None,
            owner=owner,
            now=LIVE - timedelta(minutes=30),
        )
        await writer.commit()
        return rule.id


async def _opens(session_factory: Any, token: str) -> bool:
    async with session_factory() as session:
        try:
            await check_joining(session, token=token, username="tom", password=PASSPHRASE, now=LIVE)
        except InvitationUnavailable:
            return False
        return True


@pytest.mark.parametrize("who", ["owner", "his_exception"])
async def test_a_link_given_again_after_no_new_people_opens(session_factory, monkeypatch, who):
    """Dana invited Tom before Alex said "nobody new"; the waiting link stays stopped. Alex gives
    it again himself, or the assistant does on the owner's exception: that link opens (final
    re-review R1)."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), inviters=("Dana",)
    )
    _judge(monkeypatch)
    async with session_factory() as writer:
        made, old = await create_invitation(
            writer,
            display_name="Tom",
            role="guest",
            guest_days=1,
            created_by=people["Dana"].id,
            now=LIVE - timedelta(hours=1),
        )
        await writer.commit()
    rule = await _no_new_people(
        session_factory, people["Alex"], "except the family" if who != "owner" else None
    )
    assert not await _opens(session_factory, old)
    if who == "owner":
        ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
        again = await handle_tool_call(
            _call("invitation_link_again", display_name="Tom"), ctx=ctx, reply_id=reply_id
        )
    else:
        ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
        await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
        again = await handle_tool_call(
            _call(
                "invitation_link_again",
                display_name="Tom",
                reason="Tom is Dana's brother: family",
                exception_of=str(rule),
            ),
            ctx=ctx,
            reply_id=reply_id,
        )
    assert again.status == "done", again.model_result
    assert await _opens(session_factory, _token_of(again))
    del made


@pytest.mark.parametrize(("days", "role", "to_dana"), [(2, "guest", True), (5, "guest", False)])
async def test_his_invitation_inside_the_owner_s_limit_on_her_right_goes_to_her(
    session_factory, monkeypatch, days, role, to_dana
):
    """Alex wrote "Dana may invite guests, three days at most". The assistant invites Tom for
    two days from Dana's wish: inside the limit, the link goes to Dana; for five days, it waits
    for Alex (ruling 21)."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                person_id=people["Dana"].id,
                action_type="household.invite",
                access="allow",
                limit_expression=(
                    'action.facts.role == "guest" && action.facts.days != null '
                    '&& action.facts.days <= 3 ? "allow" : "refuse"'
                ),
                summary="Dana may invite guests, three days at most.",
                owner_words="guests, three days at most",
                created_by=people["Alex"].id,
            )
        )
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    made = await handle_tool_call(
        _call("household_invite", display_name="Tom", role=role, guest_days=days, reason=REASON),
        ctx=ctx,
        reply_id=reply_id,
    )
    if not to_dana:
        # The person's side of his idea reads her limit too: outside it, nothing is made.
        assert made.status != "done" or made.model_result.get("link") == "waits_for_the_owner"
        return
    assert made.status == "done", made.model_result
    assert made.card_once is not None
    assert (await _invitation(session_factory)).link_for_id == people["Dana"].id


# --- the tests the re-review asked for ---------------------------------------------------------


async def test_a_link_shown_in_a_chain_waits_behind_the_card_for_its_holder(session_factory):
    """B M3: "give Tom's invitation again and show me the link", a chain: the step showing the
    link is not refused; it waits behind the button of the chain's card, for Dana."""
    people = await _people(
        session_factory, ("Alex", "owner"), ("Dana", "adult"), inviters=("Dana",)
    )
    async with session_factory() as writer:
        await create_invitation(
            writer,
            display_name="Tom",
            role="guest",
            guest_days=1,
            created_by=people["Dana"].id,
            now=LIVE,
            with_token=False,
        )
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], in_a_chain=True)
    shown = await handle_tool_call(
        _call("invitation_link_show", display_name="Tom"), ctx=ctx, reply_id=reply_id
    )
    assert shown.status == "done", shown.model_result
    assert shown.model_result["link"] == "waits_for_them"
    assert shown.card_once is None and shown.card["invitation"]


async def test_a_bring_back_at_once_of_his_own_idea_is_told_to_the_owner(
    session_factory, monkeypatch
):
    """B-I1, the path with no account: Sam's access is given again at once on the assistant's
    own idea, and Alex is told."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="household.bring_back",
                access="allow",
                summary="The assistant may.",
                owner_words="you may",
                created_by=people["Alex"].id,
            )
        )
        writer.add(
            Person(
                display_name="Sam",
                role="guest",
                state="gone",
                disabled_at=LIVE - timedelta(days=3),
                first_access_at=LIVE - timedelta(days=10),
                origin="domo",
                origin_reason="Sam came last month",
            )
        )
        await writer.commit()
    _judge(monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    back = await handle_tool_call(
        _call("household_bring_back", person="Sam", reason="Sam is back today"),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert back.status == "done", back.model_result
    assert back.model_result["access_given"] is True
    assert notified == [people["Alex"].id]


async def test_cancelling_his_own_invitation_tells_the_owner_only_when_he_judges_it(
    session_factory, monkeypatch
):
    """Answer d beyond removal: cancelling his own invitation, told with tell_owner only."""
    people = await _guest_invites_tom(session_factory, monkeypatch)
    ctx, reply_id, notified = await _turn(session_factory, people["Sam"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    done = await handle_tool_call(
        _call(
            "household_cancel_invitation",
            display_name="Tom",
            reason="Tom will not come",
            tell_owner=True,
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert done.status == "done", done.model_result
    assert notified == [people["Alex"].id]
    async with session_factory() as reader:
        [act] = await reader.scalars(
            select(PersonAct).where(PersonAct.act == "invitation_cancelled")
        )
    assert act.owed_to_owner is True


async def test_a_record_opened_on_the_owner_s_exception_is_traced(session_factory, monkeypatch):
    """B-I4 beyond the invitation: "nobody new, except the family"; the assistant opens Dana's
    sister's record naming the exception."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    rule = await _no_new_people(session_factory, people["Alex"], "except the family")
    _judge(monkeypatch)
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    opened = await handle_tool_call(
        _call(
            "record_open",
            name="Sam",
            standing="visiting",
            ground="will_come",
            reason="Dana's sister: family",
            exception_of=str(rule),
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert opened.status == "done", opened.model_result
    async with session_factory() as reader:
        [used] = await reader.scalars(select(PersonAct).where(PersonAct.act == "exception_used"))
    assert used.directive_id == rule
