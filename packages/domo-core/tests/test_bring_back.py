"""Bringing a former member back (spec 6): the owner, through Domo, makes a link for someone who
left the household (removed, or a guest whose stay is over), and the person comes back with it.

They keep their username and everything they wrote (their conversations, their notes): only the
password is new, and so is the role (and the end of the stay, for a guest). Nothing of the old
access works before the link is used, and the link ends what was left of it: the passkeys, the
sessions, the requests that still waited for a yes.

The link is an invitation tied to the person (``invitations.person_id``): the same delay, the same
"shown once" rule, the same cancelling. The chat pilots talk with Domo on a live server (see
``chat_support.py``); the service is also tried directly, on the shared rolled-back ``session``.
"""

import hashlib
import html
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from chat_support import GUEST_STAY, Card, an_actor, dump, live_http_client, log_in_live, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.passkey_models import PasskeyCredential
from domo_core.accounts.passwords import hash_password
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.accounts.throttle import MAX_FAILED_ATTEMPTS, WINDOW
from domo_core.confirmations.describe import describe_for_people
from domo_core.household.models import Invitation
from domo_core.household.service import (
    GUEST_DAYS_DEFAULT,
    INVITATION_TTL,
    InvitationUnavailable,
    WeakPassword,
    accept_return,
    check_joining,
    check_return,
    create_return_invitation,
    disable,
    left_on,
    name_in_use,
    open_at,
    resolve_former_person,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY, needs_the_judge
from domo_core.policy.roles import Actor
from domo_core.proofs.service import agree, means_of
from domo_core.texts.view import CatalogTexts
from domo_core.tools import household_return
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS, tools_for
from fakes import is_judge_call
from sqlalchemy import func, select, update
from web_support import in_words

NEW_PASSWORD = "a-brand-new-passphrase"
OLD_PASSWORD = "correct horse battery staple"  # the one every pilot account was made with


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


async def remove(owner_chat, name: str) -> Card:
    """The owner asks Domo to remove ``name`` and presses the confirm button."""
    return await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_remove", {"person": name})
    )


async def gone_without_a_directive(owner_chat, person: Person) -> None:
    """``person`` left the household with no directive of the owner's on them (as when they leave,
    or the assistant removes them): what the owner removes is a withdrawal (spec 6.9), which only
    the owner gives back."""
    async with owner_chat.server.session_factory() as writer:
        await disable(
            writer, await writer.get_one(Person, person.id), owner_chat.clock(), by="domo"
        )
        await writer.commit()


async def bring_back(owner_chat, **arguments) -> Card:
    """The owner asks Domo to bring someone back and presses the confirm button; the card shows
    the link, once."""
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_bring_back", arguments)
    )
    assert card.status == "executed" and card.invitation_token, card.html
    return card


async def use_link(chat, card: Card, password: str = NEW_PASSWORD, **form):
    """Open the link of ``card`` in a browser of its own and send the password; the answer."""
    async with live_http_client(chat.server.base_url) as browser:
        return await browser.post(
            f"/invite/{card.invitation_token}", data={"password": password, **form}
        )


async def sign_in(chat, username: str, password: str):
    """Try to log in as ``username``, in a browser of its own; the answer."""
    async with live_http_client(chat.server.base_url) as browser:
        return await browser.post("/login", data={"username": username, "password": password})


async def account_of(chat, person: Person) -> Account:
    async with chat.server.session_factory() as reader:
        found = await reader.execute(select(Account).where(Account.person_id == person.id))
        return found.scalar_one()


def day_of(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


# --- the owner brings someone back -----------------------------------------------------------


async def test_a_guest_whose_stay_ended_comes_back_with_their_username_and_history(
    owner_chat, guest_chat, guest_member, clock, person_row
):
    await guest_chat.say("I was here before")  # a conversation of Robin's
    clock.advance(GUEST_STAY + timedelta(hours=1))
    assert (await guest_chat.get("/chat")).status_code == 303  # the stay is over

    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest", "guest_days": 2}
    )
    assert reply.actions == [{"tool": "household_bring_back", "status": "waiting"}]
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed" and card.invitation_token

    async with live_http_client(owner_chat.server.base_url) as browser:
        link = f"/invite/{card.invitation_token}"
        page = await browser.get(link)
        assert page.status_code == 200
        words = plain(page.text)
        assert t("invite.return.title", "en", name="Robin") in words
        assert t("invite.return.username", "en", username="robin") in words
        # It asks for a password and nothing else: the username does not change.
        assert re.search(r'<input[^>]*name="password"[^>]*type="password"', page.text)
        assert not re.search(r'<input[^>]*name="username"', page.text)
        answer = await browser.post(link, data={"password": NEW_PASSWORD})
        assert answer.status_code == 303 and answer.headers["location"] == "/chat"
        browser.cookies.set(SESSION_COOKIE, answer.cookies[SESSION_COOKIE])
        chat_page = await browser.get("/chat")
        # They are in, and what they said before is there.
        assert chat_page.status_code == 200 and "I was here before" in chat_page.text

    robin = await person_row(guest_member.person)
    assert (robin.role, robin.disabled_at) == ("guest", None)
    assert robin.expires_at == clock() + timedelta(days=2)
    async with live_http_client(owner_chat.server.base_url) as browser:
        await log_in_live(browser, "robin", NEW_PASSWORD)
        assert (await browser.get("/chat")).status_code == 200
    # The browser they had before the stay ended did not come back to life with them.
    assert (await guest_chat.get("/chat")).status_code == 303


async def test_a_removed_adult_comes_back_as_a_child_and_stays_for_good(
    owner_chat, adult_member, person_row
):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="child")
    assert (await use_link(owner_chat, card)).status_code == 303
    alex = await person_row(adult_member.person)
    assert (alex.role, alex.disabled_at, alex.expires_at) == ("child", None, None)
    assert (await sign_in(owner_chat, "alex", NEW_PASSWORD)).status_code == 303


async def test_nothing_from_before_lets_them_in_until_they_use_the_link(
    owner_chat, adult_chat, adult_member
):
    await remove(owner_chat, "Alex")
    assert (await sign_in(owner_chat, "alex", adult_member.password)).status_code == 400
    card = await bring_back(owner_chat, person="Alex", role="adult")
    # The link exists, and still the old password, the new one and the old browser get nobody in.
    assert (await sign_in(owner_chat, "alex", adult_member.password)).status_code == 400
    assert (await sign_in(owner_chat, "alex", NEW_PASSWORD)).status_code == 400
    assert (await adult_chat.get("/chat")).status_code == 303
    assert (await use_link(owner_chat, card)).status_code == 303
    assert (await sign_in(owner_chat, "alex", adult_member.password)).status_code == 400
    assert (await sign_in(owner_chat, "alex", NEW_PASSWORD)).status_code == 303


async def test_two_former_members_with_one_name_are_told_apart_by_role_and_departure_day(
    owner_chat, add_member, clock
):
    adult = await add_member("Robin", "adult", username="robin.adult")
    await remove(owner_chat, "Robin")
    clock.advance(timedelta(days=1))
    guest = await add_member("Robin", "guest", username="robin.guest")
    clock.advance(GUEST_STAY + timedelta(days=1))

    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest"}
    )
    result = reply.model_results[-1]
    assert result["reason"] == "ambiguous"
    assert "a guest who left on" in result["detail"]
    assert "an adult who left on" in result["detail"]
    # Neither an identifier nor a username is told.
    for secret in ("robin.adult", "robin.guest", str(adult.id), str(guest.id)):
        assert secret not in result["detail"]
    assert reply.pending_confirmation_id is None

    # A day that fits nobody does not pick one at random.
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest", "left_on": "2001-01-01"}
    )
    assert reply.model_results[-1]["reason"] in ("ambiguous", "not_found")
    assert reply.pending_confirmation_id is None

    # The day the guest left is in the answer, and it picks the guest.
    second = day_of(guest.expires_at)
    assert f"a guest who left on {second}" in result["detail"]
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest", "left_on": second}
    )
    assert reply.pending_confirmation_id is not None
    card = await owner_chat.confirm_last(reply)
    assert card.invitation_token
    # It was the guest who was brought back, not the adult.
    assert (await use_link(owner_chat, card)).status_code == 303
    assert (await sign_in(owner_chat, "robin.guest", NEW_PASSWORD)).status_code == 303
    assert (await sign_in(owner_chat, "robin.adult", NEW_PASSWORD)).status_code == 400


async def test_someone_still_in_the_household_cannot_be_brought_back(owner_chat, adult_member):
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Alex", "role": "adult"}
    )
    result = reply.model_results[-1]
    assert result["reason"] == "not_found"
    assert "still in the household" in result["detail"]
    assert reply.pending_confirmation_id is None


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"person": "Nobody", "role": "adult"}, "not_found"),
        ({"person": "Owner", "role": "adult"}, "not_found"),  # the owner never left
        ({"person": "Robin", "role": ""}, "invalid"),
        ({"role": "adult"}, "invalid"),
        ({"person": "Robin", "role": "owner"}, "invalid"),
        ({"person": "Robin", "role": "adult", "guest_days": 3}, "invalid"),
        ({"person": "Robin", "role": "guest", "guest_days": 0}, "invalid"),
        ({"person": "Robin", "role": "guest", "guest_days": 5, "no_end": True}, "invalid"),
        ({"person": "Robin", "role": "guest", "left_on": "yesterday"}, "invalid"),
        ({"person": "Robin", "role": "guest", "left_on": "20261031"}, "invalid"),
        ({"person": "Robin", "role": "guest", "display_name": "  "}, "invalid"),
        ({"person": "Robin", "role": "guest", "why": "because"}, "invalid"),
    ],
)
async def test_bringing_back_checks_its_arguments_before_anything(
    owner_chat, guest_member, clock, arguments, reason
):
    clock.advance(GUEST_STAY + timedelta(hours=1))
    reply = await owner_chat.say_with_tool("household_bring_back", arguments)
    assert reply.actions == [
        {"tool": "household_bring_back", "status": "invalid", "reason": reason}
    ]


async def test_a_taken_name_needs_a_new_display_name(
    owner_chat, guest_member, add_member, clock, person_row
):
    clock.advance(GUEST_STAY + timedelta(hours=1))
    await add_member("Robin", "adult", username="robin.new")  # someone else has the name now
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest"}
    )
    result = reply.model_results[-1]
    assert result["reason"] == "invalid" and "name" in result["detail"]
    assert reply.pending_confirmation_id is None

    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest", "display_name": "Robin B"}
    )
    assert reply.pending_confirmation_id is not None
    card = await owner_chat.confirm_last(reply)
    page = await _open(owner_chat, card)
    assert t("invite.return.title", "en", name="Robin B") in plain(page.text)
    assert (await use_link(owner_chat, card)).status_code == 303
    robin = await person_row(guest_member.person)
    assert robin.display_name == "Robin B" and robin.disabled_at is None
    assert (await sign_in(owner_chat, "robin", NEW_PASSWORD)).status_code == 303


async def _open(chat, card: Card):
    async with live_http_client(chat.server.base_url) as browser:
        return await browser.get(f"/invite/{card.invitation_token}")


async def test_the_name_of_a_waiting_link_cannot_be_taken_by_someone_else(owner_chat, adult_member):
    await remove(owner_chat, "Alex")
    await bring_back(owner_chat, person="Alex", role="adult")
    # The link keeps the name for the person it waits for.
    taken = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "alex", "role": "adult"}
    )
    assert taken.model_results[-1]["reason"] == "invalid"


async def test_a_new_return_link_replaces_the_one_that_waits_with_one_yes(
    owner_chat, adult_member, count_invitations, session_factory, last_audit_entry
):  # spec 5.8: "Refais le lien pour Alex, il a perdu l'autre"
    await remove(owner_chat, "Alex")
    first = await bring_back(owner_chat, person="Alex", role="adult")

    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Alex", "role": "child"}
    )
    # Not refused for a link that waits: it waits for the one yes, like any link.
    assert reply.actions == [{"tool": "household_bring_back", "status": "waiting"}]
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed" and card.invitation_token
    assert card.invitation_token != first.invitation_token

    async with session_factory() as reader:
        old, new = (
            await reader.execute(select(Invitation).order_by(Invitation.created_at))
        ).scalars()
        open_now = (
            await reader.execute(
                select(func.count()).select_from(Invitation).where(open_at(datetime.now(UTC)))
            )
        ).scalar_one()
    # The old one is cancelled in the same operation: one link is open, and it is the new one.
    assert old.cancelled_at is not None and old.accepted_at is None
    assert (new.cancelled_at, new.accepted_at) == (None, None)
    assert (new.role, new.person_id) == ("child", adult_member.person.id)
    assert open_now == 1 and await count_invitations() == 2
    # The card says the old link no longer works.
    words = plain(card.html)
    assert (
        t(
            "action.card.done.household.bring_back.replaced",
            "en",
            name="Alex",
            expires=in_words(new.expires_at),
        )
        in words
    )
    assert "The old link no longer works." in words
    # The old link is dead, the new one brings Alex back.
    assert (await _open(owner_chat, first)).status_code == 410
    assert (await use_link(owner_chat, first)).status_code == 410
    assert (await _open(owner_chat, card)).status_code == 200
    # Cancelled, then offered: identifiers, names and roles only, never a link.
    cancelled = await last_audit_entry("household.invitation_cancelled")
    offered = await last_audit_entry("household.bring_back_offered")
    assert cancelled.actor == f"person:{owner_chat.person.id}"
    assert cancelled.details == {"invitation_id": str(old.id), "name": "Alex", "role": "adult"}
    assert cancelled.id < offered.id
    assert offered.details["invitation_id"] == str(new.id)
    assert (await use_link(owner_chat, card)).status_code == 303


async def test_replacing_a_return_link_under_another_name_works_too(
    owner_chat, adult_member, session_factory
):
    await remove(owner_chat, "Alex")
    await bring_back(owner_chat, person="Alex", role="adult", display_name="Alex B")
    # The link that waits holds "Alex B": that name is the person's own, not a name taken.
    for name in ("Alex B", "alex b", "Alexandre", None):
        arguments = {"person": "Alex", "role": "adult"}
        if name is not None:
            arguments["display_name"] = name
        card = await bring_back(owner_chat, **arguments)
        assert card.status == "executed"
    async with session_factory() as reader:
        links = list(
            (await reader.execute(select(Invitation).order_by(Invitation.created_at))).scalars()
        )
    assert len(links) == 5
    assert [link.cancelled_at is None for link in links] == [False, False, False, False, True]


async def test_the_name_of_the_new_link_is_still_checked_against_everybody_else(
    owner_chat, adult_member, count_invitations, session_factory
):
    await remove(owner_chat, "Alex")
    await bring_back(owner_chat, person="Alex", role="adult")
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Dana", "role": "child"}
        )
    )
    before = await count_invitations()
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Alex", "role": "adult", "display_name": "dana"}
    )
    assert reply.actions == [
        {"tool": "household_bring_back", "status": "invalid", "reason": "invalid"}
    ]
    assert "name" in reply.model_results[-1]["detail"]
    # Asked for a yes of nobody, and nothing was cancelled: the link that waits still waits.
    assert reply.pending_confirmation_id is None
    async with session_factory() as reader:
        waiting = (
            await reader.execute(
                select(func.count()).select_from(Invitation).where(open_at(datetime.now(UTC)))
            )
        ).scalar_one()
    assert waiting == 2 and await count_invitations() == before


async def test_a_name_taken_when_the_yes_comes_leaves_the_link_that_waited(
    owner_chat, adult_member, session_factory
):
    await remove(owner_chat, "Alex")
    await bring_back(owner_chat, person="Alex", role="adult")
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Alex", "role": "adult", "display_name": "Dana"}
    )
    assert reply.pending_confirmation_id is not None
    # Meanwhile, somebody is invited as Dana.
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Dana", "role": "child"}
        )
    )
    card = await owner_chat.confirm_last(reply)
    assert card.status != "executed" and card.invitation_token is None
    async with session_factory() as reader:
        mine = (
            await reader.execute(
                select(Invitation).where(Invitation.person_id == adult_member.person.id)
            )
        ).scalars()
        # Nothing was cancelled: the whole operation was undone.
        assert [link.cancelled_at for link in mine] == [None]


async def test_a_link_that_ended_as_the_yes_came_is_not_said_to_be_replaced(
    owner_chat, adult_member, monkeypatch
):
    await remove(owner_chat, "Alex")
    await bring_back(owner_chat, person="Alex", role="adult")

    async def ended(session, invitation, now):
        raise InvitationUnavailable  # used or cancelled between the read and the lock

    monkeypatch.setattr(household_return, "cancel_invitation", ended)
    card = await bring_back(owner_chat, person="Alex", role="adult")
    # A new link was made all the same, and the card does not claim an old link was replaced.
    assert "The old link no longer works." not in plain(card.html)


async def test_the_model_is_told_the_old_link_was_replaced(
    owner_chat, child_member, session_factory
):
    await remove(owner_chat, "Sam")
    ctx = ToolContext(
        session_factory=session_factory,
        actor=Actor(owner_chat.person.id, "owner"),
        person=owner_chat.person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        approved_by=owner_chat.person.id,
    )
    first = await TOOLS["household_bring_back"].execute({"person": "Sam", "role": "child"}, ctx)
    assert "replaced_link" not in first.model_result
    assert first.card["key"] == "action.card.done.household.bring_back"
    again = await TOOLS["household_bring_back"].execute({"person": "Sam", "role": "child"}, ctx)
    assert again.model_result["replaced_link"] is True
    assert again.card["key"] == "action.card.done.household.bring_back.replaced"
    # Nobody to see the link: it waits on the card, and the card says the old one is replaced.
    ctx.no_reader = True
    third = await TOOLS["household_bring_back"].execute({"person": "Sam", "role": "child"}, ctx)
    assert third.model_result["replaced_link"] is True
    assert third.model_result["link"] == "waits_for_them"
    assert third.card["key"] == "action.card.done.household.bring_back.on_request.replaced"
    assert third.card_once is None


async def test_a_name_is_in_use_unless_it_is_the_excluded_invitations(
    session, add_person, clock
):  # C15: the link that waits for a person carries their own name
    owner = await add_person("Owner", "owner")
    gone = await add_person("Gone", "adult")
    await disable(session, gone, clock(), by=gone.id)
    await session.refresh(gone)
    link, _ = await create_return_invitation(
        session,
        person=gone,
        role="adult",
        guest_days=None,
        display_name="Gone B",
        created_by=owner.id,
        now=clock(),
    )
    assert await name_in_use(session, "gone b", now=clock())
    assert not await name_in_use(session, "gone b", now=clock(), excluding_invitation=link.id)
    # Only that invitation does not count: nothing else, and not a person with that name.
    assert await name_in_use(session, "Owner", now=clock(), excluding_invitation=link.id)
    assert not await name_in_use(session, "Gone", now=clock(), excluding_invitation=link.id)
    other, _ = await create_return_invitation(
        session,
        person=gone,
        role="adult",
        guest_days=None,
        display_name="Someone",
        created_by=owner.id,
        now=clock(),
    )
    assert await name_in_use(session, "someone", now=clock(), excluding_invitation=link.id)
    assert not await name_in_use(session, "someone", now=clock(), excluding_invitation=other.id)


async def test_a_name_that_means_someone_else_by_the_yes_brings_nobody_back(
    owner_chat, guest_member, add_member, clock, chat_server, count_invitations
):  # Review Focus 5
    clock.advance(GUEST_STAY + timedelta(hours=1))
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest"}
    )
    assert reply.pending_confirmation_id is not None
    # Before the owner's yes, the Robin of the request is called otherwise, and another Robin left.
    other = await add_member("Robin", "adult", username="robin.other")
    async with chat_server.session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == guest_member.person.id).values(display_name="Robin Z")
        )
        await writer.execute(
            update(Person).where(Person.id == other.id).values(disabled_at=clock(), state="gone")
        )
        await writer.commit()
    card = await owner_chat.confirm_last(reply)
    # Described again at the yes, the call is about another person: it is not carried out.
    assert card.status == "refused" and card.invitation_token is None
    assert await count_invitations() == 0


# --- the link ------------------------------------------------------------------------------------


async def test_a_return_link_works_once_and_can_be_cancelled(
    owner_chat, adult_member, last_audit_entry
):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    assert (await use_link(owner_chat, card)).status_code == 303
    assert (await use_link(owner_chat, card, "another-passphrase-1")).status_code == 410
    assert (await _open(owner_chat, card)).status_code == 410
    assert (await sign_in(owner_chat, "alex", "another-passphrase-1")).status_code == 400

    # A second time away, and a link that is cancelled before anyone uses it.
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    reply = await owner_chat.say_with_tool("household_cancel_invitation", {"display_name": "Alex"})
    assert reply.actions == [{"tool": "household_cancel_invitation", "status": "waiting"}]
    answer = await owner_chat.say("oui")
    assert answer.actions == [{"tool": "household_cancel_invitation", "status": "done"}]
    assert (await _open(owner_chat, card)).status_code == 410
    assert (await use_link(owner_chat, card, "yet-another-passphrase")).status_code == 410
    assert (await sign_in(owner_chat, "alex", "yet-another-passphrase")).status_code == 400
    assert (await last_audit_entry("household.invitation_cancelled")).details["name"] == "Alex"


async def test_a_return_link_ends_after_seven_days(owner_chat, adult_member, clock):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    clock.advance(INVITATION_TTL)
    assert (await _open(owner_chat, card)).status_code == 410
    assert (await use_link(owner_chat, card)).status_code == 410


async def test_a_return_link_for_someone_who_is_back_already_cannot_be_used(
    owner_chat, adult_member, chat_server, person_row
):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    # Meanwhile they are put back some other way (here: straight in the database).
    async with chat_server.session_factory() as writer:
        await writer.execute(
            update(Person)
            .where(Person.id == adult_member.person.id)
            .values(disabled_at=None, state="has_access")
        )
        await writer.commit()
    assert (await _open(owner_chat, card)).status_code == 410
    assert (await use_link(owner_chat, card)).status_code == 410
    assert (await person_row(adult_member.person)).role == "adult"


async def test_a_return_link_ignores_a_username_and_is_held_back_like_a_login(
    owner_chat, adult_member, clock
):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    for _ in range(MAX_FAILED_ATTEMPTS):
        short = await use_link(owner_chat, card, "short")
        assert short.status_code == 400
        assert t("invite.error.password", "en", min="8") in plain(short.text)
        assert not re.search(r'<input[^>]*name="username"', short.text)
    throttled = await use_link(owner_chat, card)
    assert throttled.status_code == 429 and "retry-after" in throttled.headers
    clock.advance(WINDOW + timedelta(seconds=1))
    # A username in the form changes nothing: the account is the person's own.
    answer = await use_link(owner_chat, card, username="somebody-else")
    assert answer.status_code == 303
    assert (await sign_in(owner_chat, "alex", NEW_PASSWORD)).status_code == 303
    assert (await sign_in(owner_chat, "somebody-else", NEW_PASSWORD)).status_code == 400


async def test_a_return_link_is_not_a_join_link_and_the_other_way_round(
    owner_chat, adult_member, guest_member, clock, chat_server
):
    await remove(owner_chat, "Alex")
    returning = await bring_back(owner_chat, person="Alex", role="adult")
    joining = await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_invite", {"display_name": "Sam", "role": "child"})
    )
    assert joining.invitation_token
    async with chat_server.session_factory() as session:
        with pytest.raises(InvitationUnavailable):
            await check_joining(
                session,
                token=returning.invitation_token,
                username="alex2",
                password=NEW_PASSWORD,
                now=clock(),
            )
        with pytest.raises(InvitationUnavailable):
            await check_return(
                session, token=joining.invitation_token, password=NEW_PASSWORD, now=clock()
            )
    # The pages tell them apart: one asks for a username, the other shows it.
    join_page = await _open(owner_chat, joining)
    assert re.search(r'<input[^>]*name="username"', join_page.text)
    assert t("invite.title", "en") in plain(join_page.text)
    return_page = await _open(owner_chat, returning)
    assert not re.search(r'<input[^>]*name="username"', return_page.text)
    # A join link still makes a new person, with the username that was typed.
    async with live_http_client(chat_server.base_url) as browser:
        answer = await browser.post(
            f"/invite/{joining.invitation_token}",
            data={"username": "sam", "password": NEW_PASSWORD},
        )
    assert answer.status_code == 303
    async with chat_server.session_factory() as reader:
        names = (await reader.execute(select(Person.display_name))).scalars().all()
    assert sorted(names) == ["Alex", "Owner", "Robin", "Sam"]


# --- what coming back ends -------------------------------------------------------------------


async def test_coming_back_removes_old_passkeys_and_refuses_old_requests(
    owner_chat, guest_chat, guest_member, clock, chat_server, confirmation_status
):
    # A guest's rename waits for a yes (the judge asks one), and a stay that ends leaves it
    # waiting.
    waiting = await guest_chat.say_with_tool("profile_rename", {"name": "Robin B"})
    assert await confirmation_status(waiting.pending_confirmation_id) == "pending"
    account = await account_of(guest_chat, guest_member.person)
    async with chat_server.session_factory() as writer:
        writer.add(
            PasskeyCredential(
                account_id=account.id,
                credential_id=b"old-credential",
                public_key=b"old-key",
                sign_count=3,
                transports=["internal"],
                label="the old phone",
            )
        )
        await writer.commit()
    clock.advance(GUEST_STAY + timedelta(hours=1))

    card = await bring_back(owner_chat, person="Robin", role="guest")
    # Until the link is used, nothing is touched.
    assert await confirmation_status(waiting.pending_confirmation_id) == "pending"
    assert (await use_link(owner_chat, card)).status_code == 303

    assert await confirmation_status(waiting.pending_confirmation_id) == "refused"
    async with chat_server.session_factory() as reader:
        passkeys = await reader.execute(
            select(func.count())
            .select_from(PasskeyCredential)
            .where(PasskeyCredential.account_id == account.id)
        )
        sessions = await reader.execute(
            select(func.count()).select_from(WebSession).where(WebSession.account_id == account.id)
        )
    # The only session left is the one the link just opened; the old browser's is gone.
    assert passkeys.scalar_one() == 0 and sessions.scalar_one() == 1
    assert (await guest_chat.get("/chat")).status_code == 303


async def test_coming_back_takes_the_passkey_means_away_and_keeps_the_password(
    owner_chat, guest_chat, guest_member, clock, chat_server
):
    account = await account_of(guest_chat, guest_member.person)
    async with chat_server.session_factory() as writer:
        writer.add(
            PasskeyCredential(
                account_id=account.id,
                credential_id=b"old-credential",
                public_key=b"old-key",
                sign_count=3,
                transports=["internal"],
                label="the old phone",
            )
        )
        await agree(writer, guest_member.person.id, "passkey", proposed_by="person", now=clock())
        await writer.commit()
    clock.advance(GUEST_STAY + timedelta(hours=1))

    card = await bring_back(owner_chat, person="Robin", role="guest")
    assert (await use_link(owner_chat, card)).status_code == 303

    async with chat_server.session_factory() as reader:
        kept = await means_of(reader, guest_member.person, clock())
    assert [mean.kind for mean in kept] == ["password"]


async def test_the_old_password_is_replaced_not_kept_next_to_the_new_one(owner_chat, adult_member):
    await remove(owner_chat, "Alex")
    before = (await account_of(owner_chat, adult_member.person)).password_hash
    card = await bring_back(owner_chat, person="Alex", role="adult")
    assert (await account_of(owner_chat, adult_member.person)).password_hash == before
    assert (await use_link(owner_chat, card)).status_code == 303
    after = (await account_of(owner_chat, adult_member.person)).password_hash
    assert after != before and NEW_PASSWORD not in after


# --- audit, and what the model is told ---------------------------------------------------------


async def test_the_link_is_kept_nowhere_and_never_told_to_the_model(
    owner_chat, add_member, clock, stored_text, audit_dump
):
    robin = await add_member("Robin", "guest", username="rb.4471")
    clock.advance(GUEST_STAY + timedelta(hours=1))
    card = await bring_back(owner_chat, person="Robin", role="guest", guest_days=3)
    token = card.invitation_token
    await owner_chat.say("thanks")
    calls = owner_chat.server.calls
    later = json.dumps([dump(m) for m in calls[-1].messages], ensure_ascii=False)
    assert "[action household.bring_back: done]" in later
    everything_sent = json.dumps(
        [[call.system, [dump(m) for m in call.messages]] for call in calls], ensure_ascii=False
    )
    kept = await stored_text()
    hashed = hashlib.sha256(token.encode()).hexdigest()
    for text in (everything_sent, kept, await audit_dump(), (await owner_chat.get("/chat")).text):
        assert token not in text and hashed not in text
    # The username is on the page of the link and nowhere else.
    assert "rb.4471" not in everything_sent and "rb.4471" not in kept
    # Domo's own calls name Robin, never by an id; the yes-judge reads the action as the barrier
    # sees it (its target and facts: spec 3), in a call of its own that acts on nothing.
    told_domo = json.dumps(
        [
            [call.system, [dump(m) for m in call.messages]]
            for call in calls
            if not is_judge_call(call.system)
        ],
        ensure_ascii=False,
    )
    assert str(robin.id) not in told_domo


async def test_the_card_shows_the_link_as_a_link_with_its_end(owner_chat, add_member, clock):
    await add_member("Robin", "guest", username="robin")
    clock.advance(GUEST_STAY + timedelta(hours=1))
    card = await bring_back(owner_chat, person="Robin", role="guest", guest_days=3)
    async with owner_chat.server.session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    expires = in_words(invitation.expires_at)
    words = plain(card.html)
    assert t("action.card.done.household.bring_back", "en", name="Robin", expires=expires) in words
    assert t("action.card.once.household.bring_back", "en", name="Robin") in words
    link = f"{owner_chat.server.base_url}/invite/{card.invitation_token}"
    assert f'<a class="action-link" href="{link}">' in card.html


async def test_domo_is_told_it_is_done_and_the_link_was_shown(
    owner_chat, add_member, clock, chat_server
):
    # When the judge asks no yes, the model reads the result of the call.
    await add_member("Robin", "guest", username="robin")
    clock.advance(GUEST_STAY + timedelta(hours=1))
    reply = await owner_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest", "guest_days": 3}, judge="do"
    )
    assert reply.actions == [{"tool": "household_bring_back", "status": "done"}]
    assert reply.model_result_text == (
        '{"status": "done", "brought_back": "Robin", "role": "guest", "guest_days": 3, '
        '"link_shown_to_owner": true}'
    )
    token = re.search(r"/invite/([A-Za-z0-9_-]+)", reply.cards_html)
    assert token and token.group(1) not in reply.model_result_text


async def test_bringing_back_is_audited_with_identifiers_never_the_link(
    owner_chat, add_member, clock, last_audit_entry, audit_dump
):
    robin = await add_member("Robin", "guest", username="robin")
    clock.advance(GUEST_STAY + timedelta(hours=1))
    card = await bring_back(owner_chat, person="Robin", role="guest", guest_days=3)
    async with owner_chat.server.session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert invitation.person_id == robin.id
    offered = await last_audit_entry("household.bring_back_offered")
    assert offered.actor == f"person:{owner_chat.person.id}"
    assert offered.details == {
        "invitation_id": str(invitation.id),
        "person_id": str(robin.id),
        "role": "guest",
        "guest_days": 3,
    }
    assert (await use_link(owner_chat, card)).status_code == 303
    back = await last_audit_entry("household.brought_back")
    assert back.actor == f"person:{robin.id}"
    assert back.details == {"person_id": str(robin.id), "role": "guest"}
    assert card.invitation_token not in await audit_dump()


async def test_an_adult_brought_back_is_audited_without_days(
    owner_chat, adult_member, last_audit_entry
):
    await remove(owner_chat, "Alex")
    await bring_back(owner_chat, person="Alex", role="adult")
    offered = await last_audit_entry("household.bring_back_offered")
    assert set(offered.details) == {"invitation_id", "person_id", "role"}


async def test_a_return_link_someone_else_said_yes_to_shows_its_link_to_the_one_who_asked(
    owner_chat, adult_chat, child_member, grant, session_factory, stored_text, audit_dump
):
    await grant("household.bring_back", person=adult_chat.person, access="allow", approver="owner")
    await grant(
        "household.invite", person=child_member.person, access="allow", summary="Sam may invite"
    )
    await gone_without_a_directive(owner_chat, child_member.person)
    reply = await adult_chat.say_with_tool(
        "household_bring_back", {"person": "Sam", "role": "child"}
    )
    assert reply.actions == [{"tool": "household_bring_back", "status": "waiting"}]

    answer = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(answer) == "executed"
    # The owner sees what was done: no link, no button (it is Alex's link), and the rights Sam
    # had, which will be paused (Ruling R26).
    assert "/invite/" not in answer.text and "/link" not in answer.text
    assert "Sam may invite" in plain(answer.text)
    async with session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert invitation.person_id == child_member.person.id and invitation.token_hash is None
    assert invitation.created_by == adult_chat.person.id

    link_url = f"/invitations/{invitation.id}/link"
    page = (await adult_chat.get("/chat")).text
    assert f'hx-post="{link_url}"' in page
    assert (await owner_chat.post(link_url)).status_code == 404

    shown = await adult_chat.post(link_url)
    assert shown.status_code == 200
    token = re.search(r"/invite/([A-Za-z0-9_-]+)", shown.text).group(1)
    assert t("action.card.once.household.bring_back", "en", name="Sam") in plain(shown.text)
    assert (await adult_chat.post(link_url)).status_code == 410
    assert token not in await stored_text() and token not in await audit_dump()

    # The link brings Sam back, who chooses a new password.
    async with live_http_client(adult_chat.server.base_url) as browser:
        back = await browser.post(f"/invite/{token}", data={"password": NEW_PASSWORD})
        assert back.status_code == 303
    assert (await sign_in(owner_chat, "sam", NEW_PASSWORD)).status_code == 303


async def test_the_model_is_told_the_return_link_is_on_the_card_of_the_one_who_asked(
    owner_chat, adult_chat, child_member, session_factory
):
    await gone_without_a_directive(owner_chat, child_member.person)
    ctx = ToolContext(
        session_factory=session_factory,
        actor=Actor(adult_chat.person.id, "adult"),
        person=adult_chat.person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        approved_by=owner_chat.person.id,
    )
    outcome = await TOOLS["household_bring_back"].execute({"person": "Sam", "role": "child"}, ctx)
    async with session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert outcome.card_once is None and invitation.token_hash is None
    assert outcome.model_result == {
        "brought_back": "Sam",
        "role": "child",
        "link": "waits_for_them",
    }
    assert outcome.card["invitation"] == str(invitation.id)


async def test_a_return_link_nobody_would_see_waits_on_the_card_of_the_one_who_asked(
    owner_chat, child_member, session_factory
):
    """A turn no page reads (a wake of Domo after a button): the owner's own yes, but nobody
    would see the link, so it is left for them to show."""
    await remove(owner_chat, "Sam")
    ctx = ToolContext(
        session_factory=session_factory,
        actor=Actor(owner_chat.person.id, "owner"),
        person=owner_chat.person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        approved_by=owner_chat.person.id,
    )
    ctx.no_reader = True
    outcome = await TOOLS["household_bring_back"].execute({"person": "Sam", "role": "child"}, ctx)
    async with session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert outcome.card_once is None and invitation.token_hash is None
    assert outcome.model_result["link"] == "waits_for_them"
    assert "link_shown_to_owner" not in outcome.model_result
    assert outcome.card["invitation"] == str(invitation.id)


async def test_the_card_of_a_bring_back_shows_the_rights_that_wait_for_the_owner(
    owner_chat, adult_member, grant
):  # spec 5.12, ruling R8: paused when Alex left, they are still the owner's to give back
    await grant(
        "household.invite",
        person=adult_member.person,
        access="allow",
        summary="Alex may invite people",
    )
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    words = plain(card.html)
    assert t("action.card.rights.paused", "en") in words and "Alex may invite people" in words
    assert "Alex may invite people" in plain((await owner_chat.get("/chat")).text)


async def test_the_owner_who_says_yes_to_someone_elses_bring_back_reads_the_paused_rights(
    owner_chat, adult_chat, child_member, grant
):  # spec 5.12, Ruling R26: whoever asked
    await grant("household.bring_back", person=adult_chat.person, access="allow", approver="owner")
    await grant(
        "household.invite", person=child_member.person, access="allow", summary="Sam may invite"
    )
    await gone_without_a_directive(owner_chat, child_member.person)
    reply = await adult_chat.say_with_tool(
        "household_bring_back", {"person": "Sam", "role": "child"}
    )
    answer = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(answer) == "executed"
    words = plain(answer.text)
    assert t("action.card.rights.paused", "en") in words and "Sam may invite" in words
    # Domo is told them only when the owner asked (the model of whoever else asked is not).
    assert "Sam may invite" not in json.dumps(reply.model_results)


async def test_the_card_of_a_bring_back_says_nothing_of_rights_nobody_gave(
    owner_chat, adult_member
):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    assert t("action.card.rights.paused", "en") not in plain(card.html)


async def test_the_rights_paused_when_someone_left_are_not_paused_again_when_they_come_back(
    owner_chat, adult_member, grant, last_audit_entry, audit_actions
):  # spec 5.7
    await grant("household.invite", person=adult_member.person, access="allow", summary="Invites")
    await grant("note.leave", person=adult_member.person, ask="yes", summary="Notes wait")
    await remove(owner_chat, "Alex")
    paused = await last_audit_entry("rights.paused")  # at the removal, not at the return
    assert paused.details == {"person_id": str(adult_member.person.id), "count": 2}
    card = await bring_back(owner_chat, person="Alex", role="adult")
    assert (await audit_actions()).count("rights.paused") == 1  # nothing changes before the link
    assert (await use_link(owner_chat, card)).status_code == 303
    assert (await audit_actions()).count("rights.paused") == 1  # and nothing more at the return
    assert (await last_audit_entry("household.brought_back")).id > paused.id


async def test_coming_back_pauses_and_audits_the_rights_still_in_force(
    owner_chat, guest_member, grant, clock, last_audit_entry, audit_actions
):  # a stay over, and the upkeep has not made its pass yet
    await grant("note.leave", person=guest_member.person, ask="yes", summary="Notes wait")
    clock.advance(GUEST_STAY + timedelta(hours=1))
    card = await bring_back(owner_chat, person="Robin", role="guest", guest_days=2)
    words = plain(card.html)
    assert t("action.card.rights.paused", "en") in words and "Notes wait" in words
    assert "rights.paused" not in await audit_actions()
    assert (await use_link(owner_chat, card)).status_code == 303
    paused = await last_audit_entry("rights.paused")
    assert paused.actor == f"person:{guest_member.person.id}"
    assert paused.details == {"person_id": str(guest_member.person.id), "count": 1}
    assert (await last_audit_entry("household.brought_back")).id > paused.id


async def test_coming_back_without_a_right_of_their_own_audits_nothing_about_rights(
    owner_chat, adult_member, audit_actions
):
    await remove(owner_chat, "Alex")
    card = await bring_back(owner_chat, person="Alex", role="adult")
    assert (await use_link(owner_chat, card)).status_code == 303
    assert "household.brought_back" in await audit_actions()
    assert "rights.paused" not in await audit_actions()


# --- who may, and how it reads -----------------------------------------------------------------


def test_bring_back_is_the_owners_and_judged_every_time():
    assert needs_the_judge("household.bring_back")
    assert "household.bring_back" in DEFAULT_POLICY.allowed["owner"]
    assert all(
        "household.bring_back" not in DEFAULT_POLICY.allowed[role]
        for role in ("adult", "child", "guest")
    )


@pytest.mark.parametrize(
    ("role", "has_it"), [("owner", True), ("adult", False), ("child", False), ("guest", False)]
)
def test_only_the_owner_is_offered_the_tool(role, has_it):
    offered = {d.name for d in tools_for(an_actor(role), TOOLS, DEFAULT_POLICY)}
    assert ("household_bring_back" in offered) is has_it


def test_the_tool_does_its_action_of_the_catalog():
    tool = TOOLS["household_bring_back"]
    assert tool.action_type == "household.bring_back"
    # The role is the one they had when the call does not give one.
    assert tool.definition.parameters["required"] == ["person"]
    assert set(tool.definition.parameters["properties"]) == {
        "person",
        "role",
        "guest_days",
        "no_end",
        "display_name",
        "left_on",
        "reason",
        "exception_of",
    }


async def test_an_adult_who_calls_the_tool_anyway_is_refused(adult_chat, guest_member, clock):
    clock.advance(GUEST_STAY + timedelta(hours=1))
    reply = await adult_chat.say_with_tool(
        "household_bring_back", {"person": "Robin", "role": "guest"}
    )
    assert reply.actions == [
        {"tool": "household_bring_back", "status": "refused", "reason": "not_allowed"}
    ]


@pytest.mark.parametrize(
    ("arguments", "lang", "expected"),
    [
        (
            {"person": "Alex", "role": "child"},
            "en",
            ("action.describe.household.bring_back", {"name": "Alex", "role": "child"}),
        ),
        (
            {"person": "Robin", "role": "guest", "guest_days": 3},
            "en",
            (
                "action.describe.household.bring_back.guest",
                {"name": "Robin", "duration": "3 days"},
            ),
        ),
        (
            # Without the described facts, the stay is the record's (no end without an account).
            {"person": "Robin", "role": "guest"},
            "en",
            ("action.describe.household.bring_back", {"name": "Robin", "role": "guest"}),
        ),
        (
            {"person": "Robin", "role": "guest", "no_end": True},
            "en",
            ("action.describe.household.bring_back.guest.no_end", {"name": "Robin"}),
        ),
    ],
)
def test_the_request_reads_as_a_plain_sentence(arguments, lang, expected):
    key, params = expected
    if "role" in params:
        params = {**params, "role": t(f"household.role.{params['role']}", lang)}
    action = DescribedAction("core", "household.bring_back", "person:x", "permissions", "household")
    sentence = describe_for_people(action, arguments, CatalogTexts(lang))
    assert sentence == t(key, lang, **params)
    assert "household." not in sentence and "guest_days" not in sentence


def test_a_new_display_name_is_part_of_the_sentence():
    action = DescribedAction("core", "household.bring_back", "person:x", "permissions", "household")
    arguments = {"person": "Robin", "role": "adult", "display_name": "Robin B"}
    sentence = describe_for_people(action, arguments, CatalogTexts("en"))
    assert "Robin B" in sentence and sentence != t("action.describe", "en")


# --- the service ---------------------------------------------------------------------------------


def test_left_on_is_the_day_of_the_removal_or_else_of_the_end_of_the_stay():
    removed = Person(
        display_name="A",
        role="adult",
        disabled_at=datetime(2030, 1, 3, 23, 59, tzinfo=UTC),
        state="gone",
    )
    assert left_on(removed) == date(2030, 1, 3)
    over = Person(display_name="B", role="guest", expires_at=datetime(2030, 1, 2, 0, 0, tzinfo=UTC))
    assert left_on(over) == date(2030, 1, 2)
    # The day is the UTC one, whatever the zone the moment is written in.
    paris = timezone(timedelta(hours=2))
    late = Person(
        display_name="C",
        role="adult",
        disabled_at=datetime(2030, 1, 3, 1, 0, tzinfo=paris),
        state="gone",
    )
    assert left_on(late) == date(2030, 1, 2)
    # Removed after the stay was over: the removal is when they left the household for good.
    both = Person(
        display_name="D",
        role="guest",
        expires_at=datetime(2030, 1, 2, tzinfo=UTC),
        disabled_at=datetime(2030, 1, 5, tzinfo=UTC),
        state="gone",
    )
    assert left_on(both) == date(2030, 1, 5)
    with pytest.raises(ValueError):
        left_on(Person(display_name="E", role="adult"))


async def test_a_former_member_is_found_by_how_their_name_reads(
    session, owner_account, add_person, clock
):
    removed = await add_person("Zoé  Q", "adult")
    removed.disabled_at = clock() - timedelta(days=3)
    removed.state = "gone"
    gone = await add_person("Yan", "guest")
    gone.expires_at = clock() - timedelta(hours=1)
    here = await add_person("Here", "guest")
    here.expires_at = clock() + timedelta(days=1)
    await session.flush()
    assert (await resolve_former_person(session, " zoé q", now=clock())).id == removed.id
    assert (await resolve_former_person(session, "YAN", now=clock())).id == gone.id
    for name, detail in (("Here", "still in the household"), ("Owner", "still in the household")):
        with pytest.raises(InvalidArguments) as refused:
            await resolve_former_person(session, name, now=clock())
        assert refused.value.code == "not_found" and detail in (refused.value.detail or "")
    with pytest.raises(InvalidArguments) as refused:
        await resolve_former_person(session, "Nobody", now=clock())
    assert refused.value.code == "not_found"
    # The stay of someone who is still here has not ended: they are in the household at that time.
    assert (
        await resolve_former_person(session, "Here", now=clock() + timedelta(days=2))
    ).id == here.id


async def a_return_link(session, person, owner_id, clock, *, role="adult", days=None, name=None):
    """The return link of ``person``, made with the service (the invitation, and its token)."""
    return await create_return_invitation(
        session,
        person=person,
        role=role,
        guest_days=days,
        display_name=name or person.display_name,
        created_by=owner_id,
        now=clock(),
    )


async def test_a_return_link_is_made_only_for_someone_who_left(
    session, owner_account, add_person, clock
):
    owner_id = owner_account.person_id
    here = await add_person("Here", "adult")
    gone = await add_person("Gone", "adult")
    await disable(session, gone, clock(), by=gone.id)
    await session.refresh(gone)
    owner = await session.get(Person, owner_id)
    for not_away in (here, owner):
        with pytest.raises(ValueError):
            await a_return_link(session, not_away, owner_id, clock)
    # As for any invitation: a role of the household, and days for a guest only, from 1 with no
    # ceiling (audit POL-5), 30 when nothing is said.
    for role, days in (("owner", None), ("adult", 2), ("guest", 0)):
        with pytest.raises(ValueError):
            await a_return_link(session, gone, owner_id, clock, role=role, days=days)
    invitation, token = await a_return_link(session, gone, owner_id, clock, role="guest")
    assert invitation.person_id == gone.id and invitation.guest_days == GUEST_DAYS_DEFAULT == 30
    assert invitation.expires_at == clock() + INVITATION_TTL
    assert invitation.token_hash == hashlib.sha256(token.encode()).hexdigest()


async def test_accepting_a_return_checks_again_that_the_person_is_still_away(
    session, owner_account, add_person, add_account, clock
):
    gone = await add_person("Gone", "adult")
    await add_account(gone, "gone", hash_password(OLD_PASSWORD))
    await disable(session, gone, clock(), by=gone.id)
    await session.refresh(gone)
    invitation, token = await a_return_link(session, gone, owner_account.person_id, clock)
    returning = await check_return(session, token=token, password=NEW_PASSWORD, now=clock())
    assert (returning.username, returning.display_name) == ("gone", "Gone")
    assert (returning.invitation_id, returning.person_id) == (invitation.id, gone.id)
    with pytest.raises(WeakPassword):
        await check_return(session, token=token, password="short", now=clock())
    # Back already, some other way, between the check and the answer.
    gone.disabled_at = None
    gone.state = "has_access"
    await session.flush()
    with pytest.raises(InvitationUnavailable):
        await accept_return(session, returning, hash_password(NEW_PASSWORD), now=clock())
    gone.disabled_at = clock()
    gone.state = "gone"
    await session.flush()
    person = await accept_return(session, returning, hash_password(NEW_PASSWORD), now=clock())
    assert person.id == gone.id and person.disabled_at is None
    await session.refresh(invitation)
    assert invitation.accepted_at == clock() and invitation.accepted_person_id == gone.id
    # The link is used up.
    with pytest.raises(InvitationUnavailable):
        await accept_return(session, returning, hash_password(NEW_PASSWORD), now=clock())
    with pytest.raises(InvitationUnavailable):
        await check_return(session, token=token, password=NEW_PASSWORD, now=clock())


async def test_a_join_link_is_refused_by_the_return_check_and_a_return_link_by_the_join_check(
    session, owner_account, add_person, add_account, create_invitation_for_test, clock
):
    _, joining = await create_invitation_for_test(display_name="Sam", role="child")
    gone = await add_person("Gone", "adult")
    await add_account(gone, "gone", hash_password(OLD_PASSWORD))
    await disable(session, gone, clock(), by=gone.id)
    await session.refresh(gone)
    _, returning = await a_return_link(session, gone, owner_account.person_id, clock)
    with pytest.raises(InvitationUnavailable):
        await check_return(session, token=joining, password=NEW_PASSWORD, now=clock())
    with pytest.raises(InvitationUnavailable):
        await check_joining(
            session, token=returning, username="gone2", password=NEW_PASSWORD, now=clock()
        )


async def test_the_card_of_a_return_link_keeps_its_end_as_iso_8601(
    owner_chat, child_member, session_factory
):
    # Its date is put in words when the card is shown, in the reader's language (R15).
    await remove(owner_chat, "Sam")
    ctx = ToolContext(
        session_factory=session_factory,
        actor=Actor(owner_chat.person.id, "owner"),
        person=owner_chat.person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
        approved_by=owner_chat.person.id,
    )
    outcome = await TOOLS["household_bring_back"].execute({"person": "Sam", "role": "child"}, ctx)
    async with session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert set(outcome.card["dates"]) == {"expires"}
    assert datetime.fromisoformat(outcome.card["dates"]["expires"]) == invitation.expires_at
    assert "expires" not in outcome.card["params"]
