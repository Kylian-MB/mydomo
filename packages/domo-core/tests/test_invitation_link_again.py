"""An invitation's link given again (F1, the owner's trial of 0.9.0).

The owner lost the link of an invitation he had just made; Domo said he could not show it twice
and asked whether to cancel it and make another. The invitation is valid seven days: its link can
be given again, at once, to whoever may invite with its role. ``invitation_link_again`` replaces the
invitation's token (the old link stops working) and keeps everything else; the new link is shown
once, never to the model, never stored, and nothing is made when nobody could be shown it.
"""

import hashlib
import html
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import dump, live_http_client, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.household.models import Invitation
from domo_core.household.service import create_invitation
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, NeedsApproval, Refuse, decide
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.facts import facts_of
from domo_core.policy.rights import Right, RightsTable
from domo_core.policy.roles import Actor
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel
from sqlalchemy import select, update
from web_support import in_words

PASSPHRASE = "a-long-passphrase"


_LINK = re.compile(r"/invite/([A-Za-z0-9_-]+)")


def token_in(cards_html: str) -> str:
    """The token of the one link the cards of a reply show."""
    [token] = set(_LINK.findall(cards_html))
    return token


async def invited(owner_chat, name: str = "Dana", role: str = "guest", **more) -> str:
    """The owner invites ``name`` and is shown the link (the judge lets it through)."""
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": name, "role": role, **more}, judge="do"
    )
    assert reply.actions == [{"tool": "household_invite", "status": "done"}]
    return token_in(reply.cards_html)


async def the_invitation(session_factory) -> Invitation:
    async with session_factory() as reader:
        return (
            await reader.execute(select(Invitation).execution_options(populate_existing=True))
        ).scalar_one()


async def test_the_link_given_again_replaces_the_old_one_and_keeps_the_invitation(
    owner_chat, stored_text, audit_dump, caplog
):
    caplog.set_level(logging.DEBUG)
    first = await invited(owner_chat, guest_days=3)
    before = await the_invitation(owner_chat.server.session_factory)
    reply = await owner_chat.say_with_tool(
        "invitation_link_again",
        {"display_name": "Dana"},
        text="Renvoie-moi le lien de Dana, j'ai eu un souci",
        judge="do",
    )
    assert reply.actions == [{"tool": "invitation_link_again", "status": "done"}]
    token = token_in(reply.cards_html)
    assert token != first
    after = await the_invitation(owner_chat.server.session_factory)
    # The same invitation: its role, its stay, its word on the keys and its end are kept.
    assert after.id == before.id
    assert (after.role, after.guest_days, after.expires_at) == ("guest", 3, before.expires_at)
    assert (after.uses_inviter_key, after.created_by) == (
        before.uses_inviter_key,
        before.created_by,
    )
    assert after.token_hash == hashlib.sha256(token.encode()).hexdigest() != before.token_hash
    # The card says the new link, once, with its end; the model never reads it.
    once = t("action.card.once.household.invite_again", "en", name="Dana")
    assert once in " ".join(html.unescape(re.sub(r"<[^>]+>", " ", reply.cards_html)).split())
    assert reply.model_results[-1] == {
        "status": "done",
        "invitation": "Dana",
        "link_shown_to_them": True,
    }
    assert token not in reply.model_result_text
    sent = json.dumps(
        [[call.system, [dump(m) for m in call.messages]] for call in owner_chat.server.calls],
        ensure_ascii=False,
    )
    assert token not in sent
    # Never kept: not in what was said and done, the audit log or a log line.
    kept = await stored_text()
    assert token not in kept and after.token_hash not in kept
    assert token not in await audit_dump() and token not in caplog.text
    assert token not in (await owner_chat.get("/chat")).text
    async with live_http_client(owner_chat.server.base_url) as invitee:
        # The old link no longer opens the invitation; the new one does, and joins.
        assert (await invitee.get(f"/invite/{first}")).status_code == 410
        link = f"/invite/{token}"
        assert (await invitee.get(link)).status_code == 200
        joined = await invitee.post(link, data={"username": "dana", "password": PASSPHRASE})
        assert joined.status_code == 303
    async with owner_chat.server.session_factory() as reader:
        dana = (await reader.execute(select(Person).where(Person.display_name == "Dana"))).one()[0]
    assert dana.role == "guest"
    stay = dana.expires_at - dana.created_at
    assert timedelta(days=3) - timedelta(minutes=1) < stay <= timedelta(days=3)


async def test_the_card_of_the_link_given_again_says_its_end(owner_chat):
    await invited(owner_chat, name="Robin", role="adult")
    reply = await owner_chat.say_with_tool(
        "invitation_link_again", {"display_name": "Robin"}, judge="do"
    )
    invitation = await the_invitation(owner_chat.server.session_factory)
    expires = in_words(invitation.expires_at)
    page = (await owner_chat.get("/chat")).text
    done = t("action.card.done.household.invite_again", "en", name="Robin", expires=expires)
    assert done in " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
    assert reply.actions == [{"tool": "invitation_link_again", "status": "done"}]


async def test_the_link_of_an_invitation_used_or_expired_cannot_be_given_again(owner_chat):
    first = await invited(owner_chat, name="Sky", role="adult")
    async with live_http_client(owner_chat.server.base_url) as invitee:
        link = f"/invite/{first}"
        assert (
            await invitee.post(link, data={"username": "sky", "password": PASSPHRASE})
        ).status_code == 303
    used = await owner_chat.say_with_tool(
        "invitation_link_again", {"display_name": "Sky"}, judge="do"
    )
    await invited(owner_chat, name="Lou", role="adult")
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(
            update(Invitation)
            .where(Invitation.display_name == "Lou")
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await writer.commit()
    expired = await owner_chat.say_with_tool(
        "invitation_link_again", {"display_name": "Lou"}, judge="do"
    )
    for reply in (used, expired):
        assert reply.actions == [
            {"tool": "invitation_link_again", "status": "invalid", "reason": "not_found"}
        ]
        # The detail tells Domo what to do: invite them again.
        assert "invite them again" in reply.model_result_text


async def waiting_for(session_factory, owner: Person, name: str) -> Invitation:
    """An invitation of ``owner`` for ``name``, an adult, that waits (committed)."""
    return (await waiting_with_link(session_factory, owner, name))[0]


async def waiting_with_link(session_factory, by: Person, name: str) -> tuple[Invitation, str]:
    """An invitation ``by`` made for ``name``, an adult, that waits (committed), and its token."""
    async with session_factory() as writer:
        invitation, token = await create_invitation(
            writer,
            display_name=name,
            role="adult",
            guest_days=None,
            created_by=by.id,
            now=datetime.now(UTC),
        )
        await writer.commit()
    return invitation, token


def _context(person: Person, session_factory, **more) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        **more,
    )


async def test_when_nobody_could_be_shown_the_link_it_waits_behind_the_button(
    session_factory, add_member
):
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult")
    # Alex's own invitation (0.9.1: an adult gives the link again only of an invitation they made).
    invitation = await waiting_for(session_factory, alex, "Dana")
    for ctx in (
        _context(alex, session_factory, approved_by=owner.id),
        _context(alex, session_factory, no_reader=True),
        _context(alex, session_factory, in_a_chain=True),
    ):
        outcome = await TOOLS["invitation_link_again"].execute({"display_name": "Dana"}, ctx)
        assert outcome.status == "done" and outcome.card_once is None
        assert outcome.model_result == {"invitation": "Dana", "link": "waits_for_them"}
        assert outcome.card["key"] == "action.card.done.household.invite_again.on_request"
        assert outcome.card["invitation"] == str(invitation.id)
        async with session_factory() as reader:
            row = (
                await reader.execute(select(Invitation).execution_options(populate_existing=True))
            ).scalar_one()
        # The old link is dead; the new one waits for Alex, who asked.
        assert row.token_hash is None and row.link_for_id == alex.id
        assert row.created_by == alex.id and row.expires_at == invitation.expires_at


async def test_a_child_with_the_right_gets_the_new_link_after_an_adults_yes(
    owner_chat, adult_chat, child_chat, grant, session_factory, stored_text, audit_dump
):
    """IMPORTANT 1 of the review: a child with the right to invite needs an adult's yes. After
    it, the old link is dead and the child shows the new one, once, with the button of their card:
    no dead end of "ask again". (Since 0.9.1, an invitation the child made themselves: anyone but
    the owner gives the link again only of their own.)"""
    await grant("household.invite", person=child_chat.person, access="allow")
    _, first = await waiting_with_link(session_factory, child_chat.person, "Dana")
    asked = await child_chat.say_with_tool("invitation_link_again", {"display_name": "Dana"})
    assert asked.actions == [{"tool": "invitation_link_again", "status": "waiting"}]
    answer = await adult_chat.post(f"/confirmations/{asked.pending_confirmation_id}/approve")
    assert status_of(answer) == "executed"
    # The adult sees what was done, never a link.
    assert _LINK.search(answer.text) is None
    async with live_http_client(owner_chat.server.base_url) as invitee:
        assert (await invitee.get(f"/invite/{first}")).status_code == 410
    async with session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    link_url = f"/invitations/{invitation.id}/link"
    assert f'hx-post="{link_url}"' in (await child_chat.get("/chat")).text
    # Not the adult's, nor the owner's: the child's, who asked.
    assert (await adult_chat.post(link_url)).status_code == 404
    assert (await owner_chat.post(link_url)).status_code == 404
    shown = await child_chat.post(link_url)
    assert shown.status_code == 200
    token = token_in(shown.text)
    assert (await child_chat.post(link_url)).status_code == 410
    # Domo never reads it, it is kept nowhere, and it works.
    model = child_chat.use_model(FakeChatModel(["ok"]))
    await child_chat.say("thanks")
    sent = json.dumps(
        [
            [call.system, [dump(m) for m in call.messages]]
            for call in [*owner_chat.server.calls, *model.calls]
        ],
        ensure_ascii=False,
    )
    assert token not in sent and token not in await stored_text()
    assert token not in await audit_dump()
    async with live_http_client(owner_chat.server.base_url) as invitee:
        joined = await invitee.post(
            f"/invite/{token}", data={"username": "dana", "password": PASSPHRASE}
        )
        assert joined.status_code == 303


@pytest.mark.parametrize("tool", ["invitation_link_again", "household_cancel_invitation"])
async def test_an_adult_changes_only_an_invitation_they_made(tool, session_factory, add_member):
    """The owner's trial of 0.9.1: an adult invites, gives the link of their invitation again and
    cancels it; never the owner's invitation, nor another adult's. The owner, any."""
    owner = await add_member("Owner", "owner")
    dana = await add_member("Dana", "adult")
    await waiting_for(session_factory, owner, "Sam")
    await waiting_for(session_factory, dana, "Paul")
    await waiting_for(session_factory, dana, "Lou")
    sam = {"display_name": "Sam"}
    for call in (TOOLS[tool].describe, None):
        with pytest.raises(InvalidArguments) as refused:
            if call is None:
                await TOOLS[tool].execute(sam, _context(dana, session_factory))
            else:
                await call(sam, Actor(dana.id, "adult"), _context(dana, session_factory))
        assert refused.value.code == "not_possible"
        assert "someone else" in str(refused.value) and "Sam" not in str(refused.value)
    paul = {"display_name": "Paul"}
    action = await TOOLS[tool].describe(
        paul, Actor(dana.id, "adult"), _context(dana, session_factory)
    )
    assert action.target is not None
    assert (await TOOLS[tool].execute(paul, _context(dana, session_factory))).status == "done"
    # The owner changes anyone's: the adult's, and their own.
    for name in ("Lou", "Sam"):
        outcome = await TOOLS[tool].execute(
            {"display_name": name}, _context(owner, session_factory)
        )
        assert outcome.status == "done"


@pytest.mark.parametrize("tool", ["invitation_link_again", "household_cancel_invitation"])
async def test_a_yes_never_lets_an_adult_change_the_owners_invitation(
    tool, session_factory, add_member
):
    """Review of trial fix 1: an adult's request on the owner's invitation, stored and run after
    someone said yes to it (the owner himself), is still refused: only an invitation they made is
    theirs to change, whoever said yes. Nothing changes."""
    owner = await add_member("Owner", "owner")
    dana = await add_member("Dana", "adult")
    invitation = await waiting_for(session_factory, owner, "Sam")
    stored = DescribedAction(
        "core",
        TOOLS[tool].action_type,
        f"invitation:{invitation.id}",
        "permissions",
        "household",
        {},
    )
    ctx = _context(dana, session_factory, approved_by=owner.id, described_action=stored)
    assert ctx.someone_else_said_yes
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS[tool].execute({"display_name": "Sam"}, ctx)
    assert refused.value.code == "not_possible"
    assert "someone else" in str(refused.value)
    row = await the_invitation(session_factory)
    assert (row.token_hash, row.cancelled_at) == (invitation.token_hash, None)
    assert row.created_by == owner.id and row.link_for_id is None


async def test_a_return_link_is_made_again_by_bringing_the_person_back(session_factory, add_member):
    owner = await add_member("Owner", "owner")
    gone = await add_member("Paul", "adult")
    invitation = await waiting_for(session_factory, owner, "Dana")
    async with session_factory() as writer:
        await writer.execute(
            update(Invitation).where(Invitation.id == invitation.id).values(person_id=gone.id)
        )
        await writer.commit()
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS["invitation_link_again"].execute(
            {"display_name": "Dana"}, _context(owner, session_factory)
        )
    assert refused.value.code == "invalid" and "bringing the person back" in str(refused.value)


def _right(action_type: str, *, person_id=None, role=None, access=None) -> Right:
    return Right(uuid.uuid4(), person_id, role, action_type, access, None, None, None, None, "r")


def test_whoever_may_invite_with_a_role_may_give_its_link_again():
    alex = uuid.uuid4()
    adult = Actor(alex, "adult")
    child = Actor(uuid.uuid4(), "child")
    owner = Actor(uuid.uuid4(), "owner")

    def again(role: str, days: int | None) -> DescribedAction:
        return DescribedAction(
            connector="core",
            action_type="household.invite_again",
            target=f"invitation:{uuid.uuid4()}",
            effect="permissions",
            destination="household",
            facts={"role": role, "days": days, "display_name": "Dana", "owner_first": False},
        )

    def moment(actor: Actor, action: DescribedAction) -> dict[str, object]:
        return facts_of(actor, action, name="Alex", local_now=None)

    adult_again = again("adult", None)
    # The owner and the adults by default (0.9.1); a right on inviting moves it, never one of its
    # own.
    assert decide(owner, adult_again, facts=moment(owner, adult_again)) == Allow()
    assert decide(adult, adult_again, facts=moment(adult, adult_again)) == Allow()
    rights = RightsTable((_right("household.invite", person_id=alex, access="deny"),))
    assert decide(adult, adult_again, DEFAULT_POLICY, rights, moment(adult, adult_again)) == Refuse(
        "not_allowed"
    )
    rights = RightsTable((_right("household.invite", person_id=alex, access="allow"),))
    assert decide(adult, adult_again, DEFAULT_POLICY, rights, moment(adult, adult_again)) == Allow()
    # The limits of inviting apply: a guest stays the household's rule at most.
    long_stay = again("guest", 31)
    refused = decide(adult, long_stay, DEFAULT_POLICY, rights, moment(adult, long_stay))
    assert isinstance(refused, Refuse) and refused.reason == "limit"
    # A child asks an adult, as for inviting.
    child_rights = RightsTable((_right("household.invite", role="child", access="allow"),))
    child_again = again("adult", None)
    assert decide(
        child, child_again, DEFAULT_POLICY, child_rights, moment(child, child_again)
    ) == NeedsApproval("adult", judge_first=True)
