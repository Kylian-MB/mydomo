"""Invitations (task 9, decision D3): the owner invites someone through Domo, the link is shown
once, to the owner only, and the person it was sent to joins the household with it.

The link is single-use and valid for seven days. Only the SHA-256 of its token is stored, and the
token itself never reaches the model, the history, a table or a log: it is in the answer to the
request that created the invitation (``ToolOutcome.card_once``) and nowhere else (R16, R35).

The tests of the page (``/invite/<token>``) use the ``app`` fixture (the shared rolled-back
session), with the test clock set to the real time (sessions are stamped with it). The tests of the
link as Domo shows it use the chat pilots on a live server (see ``chat_support.py``).
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_support import Card, dump, live_http_client, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.accounts.throttle import MAX_FAILED_ATTEMPTS, WINDOW
from domo_core.channels.page.cards import action_card
from domo_core.household.models import Invitation
from domo_core.household.service import (
    GUEST_DAYS_DEFAULT,
    INVITATION_TTL,
    InvitationUnavailable,
    UsernameTaken,
    WeakPassword,
    cancel_invitation,
    create_invitation,
    hash_token,
    name_in_use,
    reveal_link,
)
from domo_core.llm.types import ToolCall
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import Actor
from domo_core.proofs.service import means_of
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.web.deps import get_clock
from domo_core.web.templating import create_templates, render_fragment
from fakes import FakeChatModel
from sqlalchemy import func, select, text, update
from web_support import (
    audit_entries,
    house_reads,
    in_language,
    in_words,
    join_with_password,
    log_in,
)

PASSPHRASE = "a-long-passphrase"
_LINK = re.compile(r"/invite/([A-Za-z0-9_-]+)")


@pytest.fixture
def clock(clock):
    """The test clock, set to the real time: the sessions the page opens are stamped with it."""
    clock.advance(datetime.now(UTC) - clock())
    return clock


@pytest.fixture
def app(app, clock):
    """The ``app`` fixture, reading the time from the test clock."""
    app.dependency_overrides[get_clock] = lambda: clock
    return app


async def join(client, token: str, username: str = "alex", password: str = PASSPHRASE):
    return await client.post(f"/invite/{token}", data={"username": username, "password": password})


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


# --- the brief's tests ---------------------------------------------------------------------------


async def test_invitation_link_is_single_use_and_creates_the_person(
    client, owner_account, create_invitation_for_test, second_client, session
):
    owner_id = owner_account.person_id  # the refused second use rolls the shared session back
    inv, token = await create_invitation_for_test(display_name="Alex", role="adult")
    r = await client.post(f"/invite/{token}", data={"username": "alex", "password": PASSPHRASE})
    assert r.status_code == 303
    r2 = await second_client.post(
        f"/invite/{token}", data={"username": "alex2", "password": PASSPHRASE}
    )
    assert r2.status_code == 410
    person = (
        await session.execute(select(Person).join(Account).where(Account.username == "alex"))
    ).scalar_one()
    assert (person.display_name, person.role) == ("Alex", "adult")
    assert person.invited_by == owner_id and person.expires_at is None
    assert (
        await session.execute(select(Account).where(Account.username == "alex2"))
    ).first() is None


async def test_a_person_who_joins_has_the_password_as_a_means_of_proof(
    client, owner_account, create_invitation_for_test, session
):
    _, token = await create_invitation_for_test(display_name="Alex", role="adult")
    assert (await join(client, token)).status_code == 303
    person = (
        await session.execute(select(Person).join(Account).where(Account.username == "alex"))
    ).scalar_one()
    [mean] = await means_of(session, person, datetime.now(UTC))
    assert (mean.kind, mean.proposed_by) == ("password", "code")


async def test_invitation_token_never_reaches_the_model(owner_chat, caplog, audit_dump):
    caplog.set_level(logging.DEBUG)
    model = FakeChatModel(
        tool_script=[
            ToolCall("c1", "household_invite", '{"display_name":"Alex","role":"adult"}'),
            "Fait.",
        ]
    )
    owner_chat.use_model(model)
    card = await owner_chat.confirm_last(await owner_chat.say("invite Alex"))
    token = card.invitation_token
    assert token
    assert token not in json.dumps([dump(m) for call in model.calls for m in call.messages])
    assert token not in await audit_dump() and token not in caplog.text


# --- the link, shown once ------------------------------------------------------------------------


async def test_the_link_is_kept_nowhere_and_never_told_to_the_model(
    owner_chat, stored_text, audit_dump
):  # R35
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[
                ToolCall("c1", "household_invite", '{"display_name":"Alex","role":"guest"}'),
                "Fait.",
                "Avec plaisir.",
            ]
        )
    )
    card = await owner_chat.confirm_last(await owner_chat.say("invite Alex"))
    token = card.invitation_token
    assert card.status == "executed" and token
    # A later turn: the model is shown the history, with what came of the invitation.
    await owner_chat.say("merci")
    calls = owner_chat.server.calls
    later = json.dumps([dump(m) for m in calls[-1].messages], ensure_ascii=False)
    assert "[action household.invite: done]" in later
    everything_sent = json.dumps(
        [[call.system, [dump(m) for m in call.messages]] for call in calls], ensure_ascii=False
    )
    assert token not in everything_sent
    hashed = hashlib.sha256(token.encode()).hexdigest()
    kept = await stored_text()
    assert token not in kept and hashed not in kept
    assert "Alex" in await audit_dump()  # the audit says who was invited, never the link
    page = (await owner_chat.get("/chat")).text
    assert token not in page  # a reload does not show it again


async def test_the_owner_is_shown_the_link_as_a_link_with_its_end(owner_chat):  # R67
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Alex", "role": "adult"}
    )
    assert reply.actions == [{"tool": "household_invite", "status": "waiting"}]
    card = await owner_chat.confirm_last(reply)
    link = f"{owner_chat.server.base_url}/invite/{card.invitation_token}"
    assert f'<a class="action-link" href="{link}">' in card.html
    async with owner_chat.server.session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    expires = in_words(invitation.expires_at)
    assert t("action.card.done.household.invite", "en", name="Alex", expires=expires) in plain(
        card.html
    )
    assert t("action.card.once.household.invite", "en", name="Alex") in plain(card.html)


async def test_a_link_confirmed_in_words_is_shown_in_the_answer_only(owner_chat, stored_text):
    await owner_chat.say_with_tool("household_invite", {"display_name": "Alex", "role": "child"})
    answer = await owner_chat.say("oui")
    token = Card.read(answer.cards_html, answer.message_id).invitation_token
    assert token
    assert token not in answer.text and token not in await stored_text()
    assert token not in (await owner_chat.get("/chat")).text


async def test_the_link_shown_to_the_owner_lets_the_invitee_join(owner_chat):
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Alex", "role": "guest", "guest_days": 3}
        )
    )
    link = f"/invite/{card.invitation_token}"
    async with live_http_client(owner_chat.server.base_url) as invitee:
        form = await invitee.get(link)
        assert form.status_code == 200 and "Alex" in plain(form.text)
        r = await invitee.post(link, data={"username": "alex", "password": PASSPHRASE})
        assert r.status_code == 303 and r.headers["location"] == "/chat"
        # The session cookie is Secure: over plain http it is carried by hand, as log_in_live does.
        invitee.cookies.set(SESSION_COOKIE, r.cookies[SESSION_COOKIE])
        assert (await invitee.get("/chat")).status_code == 200
    async with owner_chat.server.session_factory() as reader:
        alex = (await reader.execute(select(Person).where(Person.display_name == "Alex"))).one()[0]
    assert alex.role == "guest" and alex.invited_by == owner_chat.person.id
    stay = alex.expires_at - alex.created_at
    assert timedelta(days=3) - timedelta(minutes=1) < stay <= timedelta(days=3)


# --- the link goes to the one who asked ----------------------------------------------------------


async def only_invitation(session_factory) -> Invitation:
    async with session_factory() as reader:
        return (await reader.execute(select(Invitation))).scalar_one()


async def test_an_invitation_someone_else_said_yes_to_shows_its_link_to_the_one_who_asked(
    owner_chat, adult_chat, grant, session_factory, stored_text, audit_dump, last_audit_entry
):
    """Alex may invite, and the owner says yes: Alex wants to send the link, so the yes makes the
    invitation without a token, and Alex's card has a button that makes the token and shows it,
    once, to Alex alone."""
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "adult"}
    )
    assert reply.actions == [{"tool": "household_invite", "status": "waiting"}]
    request_id = reply.pending_confirmation_id

    answer = await owner_chat.post(f"/confirmations/{request_id}/approve")
    assert status_of(answer) == "executed"
    # The owner sees what was done, not a link, and no button: it is not their link.
    assert _LINK.search(answer.text) is None and "/link" not in answer.text
    assert t("action.card.once.household.invite", "en", name="Dana") not in plain(answer.text)
    invitation = await only_invitation(session_factory)
    assert invitation.token_hash is None and invitation.created_by == adult_chat.person.id

    link_url = f"/invitations/{invitation.id}/link"
    button = f'hx-post="{link_url}"'
    assert button in (await adult_chat.get("/chat")).text
    assert button not in (await owner_chat.get("/chat")).text
    assert (await owner_chat.post(link_url)).status_code == 404

    shown = await adult_chat.post(link_url)
    assert shown.status_code == 200
    found = _LINK.search(shown.text)
    assert found is not None
    token = found.group(1)
    assert f'href="{adult_chat.server.base_url}/invite/{token}"' in shown.text
    assert t("action.card.once.household.invite", "en", name="Dana") in plain(shown.text)
    assert (await only_invitation(session_factory)).token_hash == hash_token(token)
    entry = await last_audit_entry("household.link_shown")
    assert entry.actor == f"person:{adult_chat.person.id}"
    assert entry.details == {"invitation_id": str(invitation.id)}

    # Shown once: not again, to Alex or to the owner (who never could).
    assert (await adult_chat.post(link_url)).status_code == 410
    assert (await owner_chat.post(link_url)).status_code == 404

    # The link is kept nowhere and Domo never reads it.
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("thanks")
    sent = json.dumps(
        [[call.system, [dump(m) for m in call.messages]] for call in model.calls],
        ensure_ascii=False,
    )
    assert token not in sent and token not in await stored_text()
    assert token not in await audit_dump() and token not in (await adult_chat.get("/chat")).text

    # And it works: Dana joins with it, once.
    async with live_http_client(adult_chat.server.base_url) as dana:
        joined = await dana.post(
            f"/invite/{token}", data={"username": "dana", "password": PASSPHRASE}
        )
        assert joined.status_code == 303
    assert (await adult_chat.post(link_url)).status_code == 410


async def test_an_adult_invites_their_child_at_once_and_someone_else_after_the_owners_yes(
    owner_chat, adult_chat, live_owner_client, session_factory
):
    """The owner's trial of 0.9.1: an adult asked Domo to add their child, and he said he had no
    tool for it. An adult invites with no right of the owner's: their child at once, and someone
    Domo judged the owner should agree to first (``owner_first``) after the owner's yes, who
    reads who asks, whom and with what role. The link is then the adult's."""
    child = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Paul", "role": "child"}, judge="do"
    )
    assert child.actions == [{"tool": "household_invite", "status": "done"}]
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Sam", "role": "adult", "owner_first": True}
    )
    assert reply.actions == [{"tool": "household_invite", "status": "waiting"}]
    request_id = reply.pending_confirmation_id
    owners = plain((await live_owner_client.get("/approvals")).text)
    assert str(request_id) in (await live_owner_client.get("/approvals")).text
    assert "Alex" in owners and "“Sam”" in owners
    assert t("household.role.adult", "en") in owners
    answer = await owner_chat.post(f"/confirmations/{request_id}/approve")
    assert status_of(answer) == "executed"
    assert _LINK.search(answer.text) is None
    async with session_factory() as reader:
        sam = (
            await reader.execute(select(Invitation).where(Invitation.display_name == "Sam"))
        ).scalar_one()
    # The link waits for the adult who asked, behind the button of their card.
    assert sam.token_hash is None and sam.created_by == adult_chat.person.id
    assert f'hx-post="/invitations/{sam.id}/link"' in (await adult_chat.get("/chat")).text


async def test_a_self_approved_invitation_shows_its_link_at_once_as_before(
    owner_chat, adult_chat, grant, session_factory
):
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Dana", "role": "adult"}
        )
    )
    assert card.invitation_token and "/invitations/" not in card.html
    assert (await only_invitation(session_factory)).token_hash == hash_token(card.invitation_token)

    # Whoever asks and says yes themselves is shown the link at once, owner or not.
    await grant("household.invite", person=adult_chat.person, access="allow")
    card = await adult_chat.confirm_last(
        await adult_chat.say_with_tool("household_invite", {"display_name": "Eli", "role": "child"})
    )
    assert card.invitation_token and "/invitations/" not in card.html


def tool_context(person: Person, session_factory, *, approved_by=None) -> ToolContext:
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
        approved_by=approved_by,
    )


async def test_the_model_is_told_the_link_is_on_the_card_of_the_one_who_asked(
    session_factory, add_member
):
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult")
    outcome = await TOOLS["household_invite"].execute(
        {"display_name": "Dana", "role": "adult"},
        tool_context(alex, session_factory, approved_by=owner.id),
    )
    invitation = await only_invitation(session_factory)
    assert outcome.status == "done" and outcome.card_once is None
    assert outcome.model_result == {"invited": "Dana", "role": "adult", "link": "waits_for_them"}
    assert outcome.card["invitation"] == str(invitation.id)
    assert invitation.token_hash is None
    # Said yes to by oneself, or by nobody (the barrier let it through): as before.
    for name, approved_by in (("Eli", None), ("Fay", alex.id)):
        made = await TOOLS["household_invite"].execute(
            {"display_name": name, "role": "adult"},
            tool_context(alex, session_factory, approved_by=approved_by),
        )
        assert made.card_once is not None and "invitation" not in made.card
        assert made.model_result["link_shown_to_owner"] is True and "link" not in made.model_result


async def test_a_link_nobody_would_see_waits_on_the_card_of_the_one_who_asked(
    session_factory, add_member
):
    """A turn no page reads (a wake of Domo after a button): the person's own yes, but the link
    would be shown to nobody, so it is left for them to show, as after someone else's yes."""
    alex = await add_member("Alex", "adult")
    ctx = tool_context(alex, session_factory, approved_by=alex.id)
    ctx.no_reader = True
    outcome = await TOOLS["household_invite"].execute(
        {"display_name": "Dana", "role": "adult"}, ctx
    )
    invitation = await only_invitation(session_factory)
    assert outcome.status == "done" and outcome.card_once is None
    assert outcome.model_result == {"invited": "Dana", "role": "adult", "link": "waits_for_them"}
    assert outcome.card["invitation"] == str(invitation.id)
    assert invitation.token_hash is None


async def test_only_the_one_who_asked_can_make_the_link_of_an_invitation_and_only_once(
    session, owner_account, clock
):
    owner_id = owner_account.person_id
    other = Person(display_name="Other", role="adult")
    session.add(other)
    await session.flush()
    invitation, token = await create_invitation(
        session,
        display_name="Dana",
        role="adult",
        guest_days=None,
        created_by=owner_id,
        now=clock(),
        with_token=False,
    )
    assert token is None and invitation.token_hash is None
    # A tokenless invitation waits like any: it holds its name, and nobody can join with it.
    assert await name_in_use(session, "dana", now=clock())
    with pytest.raises(InvitationUnavailable):
        await reveal_link(session, invitation.id, other.id, clock())
    revealed = await reveal_link(session, invitation.id, owner_id, clock())
    assert invitation.token_hash == hash_token(revealed)
    with pytest.raises(InvitationUnavailable):
        await reveal_link(session, invitation.id, owner_id, clock())

    # An invitation made with its token already (as before) has nothing left to show.
    made, _ = await create_invitation(
        session,
        display_name="Eli",
        role="adult",
        guest_days=None,
        created_by=owner_id,
        now=clock(),
    )
    with pytest.raises(InvitationUnavailable):
        await reveal_link(session, made.id, owner_id, clock())


async def test_two_requests_for_the_link_cannot_both_get_it(
    session_factory, add_owner_in_database, clock
):
    """The button is pressed twice at once: the second waits for the invitation's row, and once
    the first commits it finds the link made already."""
    owner = await add_owner_in_database()
    async with session_factory() as writer:
        invitation, _ = await create_invitation(
            writer,
            display_name="Dana",
            role="adult",
            guest_days=None,
            created_by=owner.person_id,
            now=clock(),
            with_token=False,
        )
        await writer.commit()
        invitation_id = invitation.id
    async with session_factory() as first, session_factory() as second:
        token = await reveal_link(first, invitation_id, owner.person_id, clock())
        later = asyncio.create_task(reveal_link(second, invitation_id, owner.person_id, clock()))
        await _until_someone_waits_for_a_row(session_factory)
        assert not later.done()
        await first.commit()
        with pytest.raises(InvitationUnavailable):
            await later
    assert (await only_invitation(session_factory)).token_hash == hash_token(token)


async def test_the_link_of_a_closed_invitation_cannot_be_made(session, owner_account, clock):
    owner_id = owner_account.person_id
    for how in ("cancelled", "expired", "unknown"):
        invitation, _ = await create_invitation(
            session,
            display_name=f"Dana {how}",
            role="adult",
            guest_days=None,
            created_by=owner_id,
            now=clock(),
            with_token=False,
        )
        target = invitation.id
        if how == "cancelled":
            await cancel_invitation(session, invitation, clock())
        elif how == "expired":
            invitation.expires_at = clock() - timedelta(seconds=1)
            await session.flush()
        else:
            target = uuid.uuid4()
        with pytest.raises(InvitationUnavailable):
            await reveal_link(session, target, owner_id, clock())
        assert invitation.token_hash is None


def test_the_buttons_of_a_stored_card_are_for_the_one_who_asked_and_only_for_what_it_names():
    invitation = str(uuid.uuid4())
    card = {
        "key": "action.card.done.household.invite.on_request",
        "params": {"name": "Dana", "expires": "2030-01-01 12:00 UTC"},
        "invitation": invitation,
        "rekey": "p-0123abcd",
        "rights": {"key": "action.card.rights.kept", "items": ["<b>Alex</b> invites", 7]},
    }
    request = uuid.uuid4()
    mine = action_card(
        tool_name="household_invite",
        status="done",
        card=card,
        texts=CatalogTexts("en"),
        for_requester=True,
        confirmation_id=request,
    )
    assert mine.invitation == invitation
    # The key's button names the request it is the card of.
    assert mine.rekey_url == f"/chat/key/p-0123abcd/form?confirmation={request}"
    # With no request behind it (an action that ran at once, its form left on the card: a turn
    # Domo started, or a key tool run from the API, which opens no form), the card would be a
    # dead end without its button: since task 8 it opens the form for whoever asked, the route
    # asking the barrier again (``own_rekey_button_of``). It used to have none.
    no_request = action_card(
        tool_name="t", status="done", card=card, texts=CatalogTexts("en"), for_requester=True
    )
    assert no_request.rekey_url == "/chat/key/p-0123abcd/form"
    # Still for the one who asked alone, and only for what the card names.
    not_theirs = action_card(
        tool_name="t", status="done", card=card, texts=CatalogTexts("en"), for_requester=False
    )
    assert not_theirs.rekey_url is None and not_theirs.admin_url is None
    names_nothing = action_card(
        tool_name="t",
        status="done",
        card={"key": "action.card.done", "params": {}},
        texts=CatalogTexts("en"),
        for_requester=True,
    )
    assert names_nothing.rekey_url is None and names_nothing.admin_url is None
    assert mine.rights_title == t("action.card.rights.kept", "en")
    assert mine.rights == ("<b>Alex</b> invites",)  # what is not a summary is left out
    # The one who said yes sees what was done, not the buttons.
    theirs = action_card(
        tool_name="household_invite",
        status="done",
        card=card,
        texts=CatalogTexts("en"),
        for_requester=False,
        confirmation_id=request,
    )
    assert theirs.invitation is None and theirs.rekey_url is None and theirs.rights == mine.rights
    # A stored card is read with care: only what the tools put there makes a button.
    for broken in ("../x", f"{invitation}/../x", '" onclick="x', "", 7, None):
        odd = {**card, "invitation": broken, "rekey": broken, "rights": broken}
        shown = action_card(
            tool_name="household_invite",
            status="done",
            card=odd,
            texts=CatalogTexts("en"),
            for_requester=True,
            confirmation_id=request,
        )
        assert (shown.invitation, shown.rekey_url, shown.rights_title, shown.rights) == (
            None,
            None,
            None,
            (),
        )
    unknown = {**card, "rights": {"key": "no.such.key", "items": ["x"]}}
    unread = action_card(
        tool_name="t", status="done", card=unknown, texts=CatalogTexts("en"), for_requester=True
    )
    assert unread.rights == ()

    # And what people wrote in a summary is text on the page, never markup.
    fragment = render_fragment(
        create_templates(), "fragments/action_card.html", CatalogTexts("en"), action=mine
    )
    assert "<b>Alex</b>" not in fragment and "&lt;b&gt;Alex&lt;/b&gt; invites" in fragment
    assert f'hx-post="/invitations/{invitation}/link"' in fragment
    assert f'hx-post="/chat/key/p-0123abcd/form?confirmation={request}"' in fragment
    seen = render_fragment(
        create_templates(), "fragments/action_card.html", CatalogTexts("en"), action=theirs
    )
    assert "hx-post" not in seen and "&lt;b&gt;Alex&lt;/b&gt; invites" in seen


async def test_the_link_route_answers_404_for_what_is_not_the_persons_invitation(adult_chat):
    for url in (
        f"/invitations/{uuid.uuid4()}/link",
        "/invitations/not-an-id/link",
        "/invitations/00000000-0000-0000-0000-00000000000G/link",
    ):
        assert (await adult_chat.post(url)).status_code == 404, url
    async with live_http_client(adult_chat.server.base_url) as nobody:
        denied = await nobody.post(f"/invitations/{uuid.uuid4()}/link")
        assert denied.status_code in (303, 401)
    # From another site: the Origin check of every request that changes something.
    other_site = await adult_chat.client.post(
        f"/invitations/{uuid.uuid4()}/link", headers={"origin": "https://example.com"}
    )
    assert other_site.status_code == 403


async def test_a_cancelled_invitation_answers_410_to_the_one_who_made_it(
    owner_chat, adult_chat, grant, session_factory
):
    await grant("household.invite", person=adult_chat.person, access="allow", approver="owner")
    reply = await adult_chat.say_with_tool(
        "household_invite", {"display_name": "Dana", "role": "adult"}
    )
    await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    invitation = await only_invitation(session_factory)
    async with session_factory() as writer:
        await writer.execute(
            update(Invitation)
            .where(Invitation.id == invitation.id)
            .values(cancelled_at=datetime.now(UTC))
        )
        await writer.commit()
    assert (await adult_chat.post(f"/invitations/{invitation.id}/link")).status_code == 410
    assert (await only_invitation(session_factory)).token_hash is None


# --- the service ---------------------------------------------------------------------------------


async def test_only_the_hash_of_the_token_is_stored(session, owner_account, clock):
    invitation, token = await create_invitation(
        session,
        display_name="Alex",
        role="adult",
        guest_days=None,
        created_by=owner_account.person_id,
        now=clock(),
    )
    assert len(token) >= 43  # 256 bits, url-safe base64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert invitation.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert invitation.expires_at == clock() + INVITATION_TTL == clock() + timedelta(days=7)
    assert invitation.created_at == clock()
    assert (invitation.role, invitation.guest_days, invitation.created_by) == (
        "adult",
        None,
        owner_account.person_id,
    )


async def test_an_invitation_says_whether_the_invitee_uses_the_inviters_key(
    session, owner_account, clock
):
    told, _ = await create_invitation(
        session,
        display_name="Alex",
        role="adult",
        guest_days=None,
        created_by=owner_account.person_id,
        now=clock(),
        uses_inviter_key=False,
    )
    untold, _ = await create_invitation(
        session,
        display_name="Sam",
        role="child",
        guest_days=None,
        created_by=owner_account.person_id,
        now=clock(),
    )
    # Not said: the household's default decides when they join.
    assert (told.uses_inviter_key, untold.uses_inviter_key) == (False, None)


@pytest.mark.parametrize("value", ["yes", 1, 0, [True]])
async def test_uses_my_key_is_true_or_false(tool_context, value):
    actor = Actor(tool_context.person.id, "owner")
    arguments = {"display_name": "Robin", "role": "guest", "uses_my_key": value}
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["household_invite"].describe(arguments, actor, tool_context)
    assert raised.value.code == "invalid"
    for told in (True, False):
        arguments["uses_my_key"] = told
        await TOOLS["household_invite"].describe(arguments, actor, tool_context)


async def test_a_guest_is_invited_for_two_days_unless_told_otherwise(
    create_invitation_for_test, owner_account
):
    guest, _ = await create_invitation_for_test(display_name="Robin", role="guest")
    assert guest.guest_days == GUEST_DAYS_DEFAULT == 2
    longer, _ = await create_invitation_for_test(
        display_name="Charlie", role="guest", guest_days=30
    )
    assert longer.guest_days == 30


@pytest.mark.parametrize(("role", "guest_days"), [("adult", 3), ("guest", 0), ("guest", 366)])
async def test_the_service_refuses_days_that_make_no_sense(
    create_invitation_for_test, owner_account, role, guest_days
):
    with pytest.raises(ValueError):
        await create_invitation_for_test(role=role, guest_days=guest_days)


async def test_accepting_creates_the_person_and_the_account_and_closes_the_invitation(
    session, owner_account, create_invitation_for_test, clock
):
    invitation, token = await create_invitation_for_test(
        display_name="Robin", role="guest", guest_days=5
    )
    person = await join_with_password(
        session, token=token, username="  Robin ", password=PASSPHRASE, now=clock()
    )
    assert (person.display_name, person.role) == ("Robin", "guest")
    assert person.expires_at == clock() + timedelta(days=5)
    assert person.invited_by == owner_account.person_id
    account = (await session.execute(select(Account).where(Account.person_id == person.id))).one()[
        0
    ]
    assert account.username == "robin" and PASSPHRASE not in account.password_hash
    await session.refresh(invitation)
    assert invitation.accepted_at == clock() and invitation.accepted_person_id == person.id
    with pytest.raises(InvitationUnavailable):
        await join_with_password(
            session, token=token, username="other", password=PASSPHRASE, now=clock()
        )


async def test_accepting_refuses_a_taken_username_or_a_short_password(
    session, owner_account, create_invitation_for_test, clock
):
    _, token = await create_invitation_for_test()
    with pytest.raises(UsernameTaken):
        await join_with_password(
            session, token=token, username="OWNER", password=PASSPHRASE, now=clock()
        )
    with pytest.raises(WeakPassword):
        await join_with_password(
            session, token=token, username="alex", password="short", now=clock()
        )


async def test_an_expired_or_cancelled_invitation_cannot_be_accepted(
    session, owner_account, create_invitation_for_test, clock
):
    expired, expired_token = await create_invitation_for_test(display_name="Alex")
    cancelled, cancelled_token = await create_invitation_for_test(display_name="Sam")
    await cancel_invitation(session, cancelled, clock())
    later = clock() + INVITATION_TTL
    for token in (expired_token, cancelled_token, "not-a-token"):
        with pytest.raises(InvitationUnavailable):
            await join_with_password(
                session, token=token, username="x", password=PASSPHRASE, now=later
            )
    assert (await session.execute(select(func.count()).select_from(Account))).scalar_one() == 1


# --- the page ------------------------------------------------------------------------------------


async def test_the_page_says_who_is_invited_and_asks_for_a_username_and_a_password(
    client, owner_account, create_invitation_for_test, session
):
    _, token = await create_invitation_for_test(display_name="<b>Alex</b>", role="child")
    r = await client.get(f"/invite/{token}")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "&lt;b&gt;Alex&lt;/b&gt;" in r.text and "<b>Alex</b>" not in r.text
    assert t("household.role.child", "en") in plain(r.text)
    assert re.search(rf'<form[^>]*method="post"[^>]*action="/invite/{token}"', r.text)
    assert re.search(r'<input[^>]*name="username"', r.text)
    assert re.search(r'<input[^>]*name="password"[^>]*type="password"', r.text)
    assert 'autocomplete="new-password"' in r.text
    await house_reads(session, "fr")
    french = await client.get(f"/invite/{token}", headers={"accept-language": "fr"})
    assert in_language("fr", "invite.submit") in french.text
    # A language without a version nor default phrases reads English, not the house's.
    german = await client.get(f"/invite/{token}", headers={"accept-language": "de"})
    assert '<html lang="en">' in german.text
    assert t("invite.submit", "en") in plain(german.text)


@pytest.mark.parametrize("token", ["not-a-token", "x" * 500, "%E2%80%AEtoken", "a%00b"])
async def test_an_unknown_link_says_the_invitation_cannot_be_used(client, owner_account, token):
    r = await client.get(f"/invite/{token}")
    assert r.status_code == 410
    assert t("invite.unavailable.title", "en") in r.text
    assert not re.search(r'<input[^>]*name="password"', r.text)


async def test_a_used_an_expired_and_a_cancelled_link_look_the_same(
    client, owner_account, create_invitation_for_test, session, clock
):
    _, used = await create_invitation_for_test(display_name="Alex")
    assert (await join(client, used)).status_code == 303
    cancelled_invitation, cancelled = await create_invitation_for_test(display_name="Sam")
    await cancel_invitation(session, cancelled_invitation, clock())
    _, expired = await create_invitation_for_test(display_name="Robin")
    clock.advance(INVITATION_TTL)
    pages = [await client.get(f"/invite/{token}") for token in (used, cancelled, expired, "nope")]
    assert {page.status_code for page in pages} == {410}
    assert len({plain(page.text) for page in pages}) == 1  # nothing tells one from the other
    for token in (cancelled, expired):
        assert (await join(client, token, "robin")).status_code == 410


async def test_joining_logs_the_person_in_and_sends_them_to_the_chat(
    client, owner_account, create_invitation_for_test
):
    _, token = await create_invitation_for_test(display_name="Alex", role="adult")
    r = await join(client, token)
    assert r.status_code == 303 and r.headers["location"] == "/chat"
    cookie = r.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    me = (await client.get("/whoami")).json()
    assert (me["name"], me["role"]) == ("Alex", "adult")


async def test_joining_is_audited_with_the_name_and_the_role_never_the_token(
    client, owner_account, create_invitation_for_test, session
):
    invitation, token = await create_invitation_for_test(display_name="Alex", role="child")
    await join(client, token)
    joined = [e for e in await audit_entries(session) if e.action == "household.joined"]
    assert len(joined) == 1
    person_id = (
        await session.execute(select(Person.id).where(Person.display_name == "Alex"))
    ).scalar_one()
    assert joined[0].actor == f"person:{person_id}"
    assert joined[0].details == {
        "name": "Alex",
        "role": "child",
        "invitation_id": str(invitation.id),
    }
    assert token not in json.dumps([list(e) for e in await audit_entries(session)], default=str)


async def test_a_taken_username_or_a_short_password_asks_again_and_the_link_still_works(
    client, owner_account, create_invitation_for_test
):
    _, token = await create_invitation_for_test(display_name="Alex")
    taken = await join(client, token, username="Owner")
    assert taken.status_code == 400
    assert t("invite.error.username_taken", "en") in plain(taken.text)
    short = await join(client, token, password="short")
    assert short.status_code == 400
    assert t("invite.error.password", "en", min="8") in plain(short.text)
    assert "short" not in short.text  # the password is never echoed
    assert (await join(client, token)).status_code == 303


@pytest.mark.parametrize("username", ["", "  ", "x" * 65, "a\x01b", "a\N{RIGHT-TO-LEFT OVERRIDE}b"])
async def test_a_username_that_cannot_be_used_asks_again(
    client, owner_account, create_invitation_for_test, username
):
    _, token = await create_invitation_for_test(display_name="Alex")
    r = await join(client, token, username=username)
    assert r.status_code == 400
    assert t("invite.error.username", "en", max="64") in plain(r.text)


async def test_accepting_an_invitation_is_throttled_like_a_login(
    client, owner_account, create_invitation_for_test, clock
):
    _, token = await create_invitation_for_test()
    for _ in range(MAX_FAILED_ATTEMPTS):
        assert (await join(client, token, password="short")).status_code == 400
    throttled = await join(client, token)
    assert throttled.status_code == 429 and "retry-after" in throttled.headers
    assert t("invite.error.throttled", "en", minutes="15") in plain(throttled.text)
    clock.advance(WINDOW + timedelta(seconds=1))
    assert (await join(client, token)).status_code == 303


async def test_joining_in_a_browser_that_has_a_session_replaces_it(
    client, owner_account, create_invitation_for_test, session
):
    await log_in(client)
    owner_token = client.cookies[SESSION_COOKIE]
    _, token = await create_invitation_for_test(display_name="Alex")
    assert (await join(client, token)).status_code == 303
    assert (await client.get("/whoami")).json()["name"] == "Alex"
    hashed = hashlib.sha256(owner_token.encode()).hexdigest()
    gone = (
        await session.execute(select(WebSession).where(WebSession.token_hash == hashed))
    ).first()
    assert gone is None


async def test_a_cross_site_post_cannot_use_an_invitation(
    client_factory, app, owner_account, create_invitation_for_test
):
    _, token = await create_invitation_for_test()
    async with client_factory(app, origin="https://evil.example.net") as evil:
        assert (await join(evil, token)).status_code == 403
    async with client_factory(app) as browser:
        assert (await join(browser, token)).status_code == 303


async def test_two_people_cannot_both_use_one_invitation(
    session_factory, add_owner_in_database, clock
):
    """Two tabs (or two people) use one link at once. The first acceptance is made but not yet
    committed when the second starts: the second passes the first, unlocked check (the
    invitation still looks open), hashes its password, then waits for the invitation's row. Once
    the first commits, the locked check sees the invitation used, and the second gets nothing.
    """
    owner = await add_owner_in_database()
    async with session_factory() as writer:
        _, token = await create_invitation(
            writer,
            display_name="Alex",
            role="adult",
            guest_days=None,
            created_by=owner.person_id,
            now=clock(),
        )
        await writer.commit()
    async with session_factory() as first, session_factory() as second:
        await join_with_password(
            first, token=token, username="alex", password=PASSPHRASE, now=clock()
        )
        later = asyncio.create_task(
            join_with_password(
                second, token=token, username="sam", password=PASSPHRASE, now=clock()
            )
        )
        await _until_someone_waits_for_a_row(session_factory)
        assert not later.done()
        await first.commit()
        with pytest.raises(InvitationUnavailable):
            await later
    async with session_factory() as reader:
        usernames = (await reader.execute(select(Account.username))).scalars().all()
        adults = (
            await reader.execute(
                select(func.count()).select_from(Person).where(Person.role == "adult")
            )
        ).scalar_one()
    assert sorted(usernames) == ["alex", "owner"] and adults == 1


async def _until_someone_waits_for_a_row(session_factory) -> None:
    """Wait until a connection of the test database waits for a lock (a row that another
    transaction holds)."""
    async with asyncio.timeout(10):
        while True:
            async with session_factory() as watcher:
                waiting = (
                    await watcher.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        )
                    )
                ).scalar_one()
            if waiting:
                return
            await asyncio.sleep(0.02)


async def test_the_card_of_an_invitation_keeps_its_end_as_iso_8601(session_factory, add_member):
    # Its date is put in words when the card is shown, in the reader's language (R15).
    owner = await add_member("Owner", "owner")
    outcome = await TOOLS["household_invite"].execute(
        {"display_name": "Dana", "role": "adult"}, tool_context(owner, session_factory)
    )
    invitation = await only_invitation(session_factory)
    assert set(outcome.card["dates"]) == {"expires"}
    assert datetime.fromisoformat(outcome.card["dates"]["expires"]) == invitation.expires_at
    assert "expires" not in outcome.card["params"]


def test_the_invitation_says_the_choices_a_request_makes_plain():  # spec 5.5, scenario 8
    """In the owner's trial of 0.7.0, "create Dana, my partner" brought three questions (role, how
    long, whose key) whose answers were plain: the tool says what to choose then, without asking."""
    definition = TOOLS["household_invite"].definition
    told = " ".join(definition.description.split())
    assert "When the request makes the choices plain, make them without asking" in told
    assert (
        "someone close to the person who invites (their partner, say) is an adult, with no end "
        "date, on that person's keys (uses_my_key true)"
    ) in told
    assert "Ask only what would change the invitation and you cannot guess" in told
    assert "a request that waits shows your choices" in told
    key = " ".join(definition.parameters["properties"]["uses_my_key"]["description"].split())
    assert "Only if the person says, or the request makes it plain." in key
