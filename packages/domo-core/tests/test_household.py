"""The household (task 9, decisions D1 and D3): the owner, through Domo, invites people, changes
a role, removes someone and cancels an invitation; the owner sees the household on its page; a
removed person, or a guest whose stay is over, loses access at the next request.

The four actions are the owner's only. Inviting, changing a role and removing someone are
``always_confirm`` (the owner says yes every time, by the button or "I confirm" until they choose
otherwise); cancelling an invitation is ``confirm``. A person is named by their display name:
exactly, whatever the case, among the people who are in the household now. A name that means
nobody is ``not_found``, one that means several people is ``ambiguous``: nothing is guessed.

A guest given the role of guest again, with days, has a new stay (spec 5.5). Leaving the household
oneself (``household_leave``, spec 5.9) is everyone's but the owner's, with their own yes, or an
adult's for a child; removing someone else stays the owner's, or a right's.

The chat pilots talk with Domo on a live server (see ``chat_support.py``).
"""

import html
import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from chat_support import GUEST_STAY, Card, an_actor, live_http_client, log_in_live, status_of
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.sessions import create_session, resolve_session
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Message
from domo_core.confirmations.describe import describe_for_people
from domo_core.household import service as household_service
from domo_core.household.models import Invitation
from domo_core.household.service import (
    GUEST_DAYS_DEFAULT,
    Removed,
    create_invitation,
    disable,
    resolve_person,
)
from domo_core.llm.types import ToolCall
from domo_core.people import records
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import (
    ACTION_FACTS,
    ALLOWED_ACTIONS,
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    KNOWN_ACTIONS,
    OWNER_ONLY_ACTIONS,
    PolicyTables,
    needs_the_judge,
)
from domo_core.policy.models import PolicyRight
from domo_core.policy.roles import ROLES, Actor
from domo_core.texts.view import CatalogTexts
from domo_core.tools.context import RefusesWhateverTheRights, ToolContext
from domo_core.tools.household_admin import guest_days_from, stay_days
from domo_core.tools.registry import TOOLS, InvalidArguments, tools_for
from domo_core.web.deps import get_policy
from fakes import FakeChatModel
from sqlalchemy import func, select, update
from web_support import in_language, in_words, reads_in

HOUSEHOLD_TOOLS = [
    "household_invite",
    "household_change_role",
    "household_remove",
    "household_cancel_invitation",
    "household_bring_back",
    "household_transfer_ownership",
]


def plain(fragment: str) -> str:
    """What a person reads in a page or a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


async def remove(owner_chat, name: str) -> Card:
    """The owner asks Domo to remove ``name`` and presses the confirm button."""
    return await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_remove", {"person": name})
    )


# --- the brief's tests ---------------------------------------------------------------------------


async def test_invite_rejects_owner_role_and_bad_names(owner_chat, count_invitations):  # RF 3
    for args in [
        '{"display_name":"X","role":"owner"}',
        '{"display_name":"","role":"adult"}',
        '{"display_name":"' + "x" * 61 + '","role":"adult"}',
        '{"display_name":"<img src=x>","role":"guest","guest_days":0}',
    ]:
        owner_chat.use_model(
            FakeChatModel(tool_script=[ToolCall("c", "household_invite", args), "ok"])
        )
        reply = await owner_chat.say("invite")
        assert reply.actions[0]["status"] == "invalid"
    assert await count_invitations() == 0


async def test_ambiguous_name_is_not_guessed(owner_chat, add_member):  # Review Focus 5
    await add_member("Alex", "adult")
    await add_member("alex", "child")
    owner_chat.use_model(
        FakeChatModel(
            tool_script=[ToolCall("c", "household_remove", '{"person":"ALEX"}'), "Lequel ?"]
        )
    )
    reply = await owner_chat.say("retire Alex")
    assert reply.actions[0]["status"] == "invalid" and reply.actions[0]["reason"] == "ambiguous"


async def test_removed_person_loses_access_immediately(
    owner_chat, live_adult_client, adult_member
):  # Review Focus 4
    await remove(owner_chat, "Alex")
    assert (await live_adult_client.get("/chat")).headers["location"] == "/login"


async def test_expired_guest_is_logged_out(live_guest_client, clock):
    clock.advance(timedelta(days=GUEST_DAYS_DEFAULT, seconds=1))
    assert (await live_guest_client.get("/chat")).status_code == 303


async def test_household_page_is_owner_only(live_adult_client):
    assert (await live_adult_client.get("/household")).status_code == 403


# --- who may do what -----------------------------------------------------------------------------


def test_the_pilots_guests_stay_less_than_a_default_invitation_and_no_stay_has_a_ceiling():
    assert GUEST_STAY < timedelta(days=GUEST_DAYS_DEFAULT) == timedelta(days=30)
    assert not hasattr(household_service, "GUEST_DAYS_CEILING")


# What an adult is offered of them: nothing (ruling 14: the gestures on people are in no role).
ADULTS_HOUSEHOLD_TOOLS: set[str] = set()


@pytest.mark.parametrize(
    ("role", "has_them"), [("owner", True), ("adult", False), ("child", False), ("guest", False)]
)
def test_only_the_owner_is_offered_the_household_tools(role, has_them):  # D1
    offered = {d.name for d in tools_for(an_actor(role), TOOLS, DEFAULT_POLICY)}
    assert (set(HOUSEHOLD_TOOLS) <= offered) is has_them
    adults = ADULTS_HOUSEHOLD_TOOLS if role == "adult" else set()
    assert offered & set(HOUSEHOLD_TOOLS) == adults or has_them
    assert ("invitation_link_again" in offered) is (role == "owner")


async def test_a_turn_tells_domo_what_exists_but_is_not_the_persons(
    adult_chat, owner_chat, chat_server, grant
):
    """The owner's trial of 0.9.1: Domo told an adult he lacked a tool to add someone. The
    context of a turn names what the rights in force keep from the person, never a silent tool, and
    nothing for the owner."""

    def kept_line() -> str | None:
        system = chat_server.calls[-1].system
        return next((line for line in system.splitlines() if line.startswith("Not theirs: ")), None)

    await adult_chat.say("hello")
    line = kept_line()
    assert line is not None
    assert "household_change_role" in line and "household_remove" in line
    # Inviting is in no role (ruling 14): kept from an adult too. A silent tool never is.
    assert "household_invite" in line and "memory_keep" not in line
    await grant("household.change_role", person=adult_chat.person, access="allow")
    await adult_chat.say("hello again")
    line = kept_line()
    assert line is not None and "household_change_role" not in line
    await owner_chat.say("hello")
    assert kept_line() is None


def test_the_invitation_says_domo_may_send_it_to_the_owner_first():
    """The owner's decision of 2026-11-17, "Domo judges": the call says when the owner should
    say yes first, in Domo's judgement, and nothing for a plain request."""
    tool = TOOLS["household_invite"].definition
    text = " ".join(tool.description.split())
    for words in (
        # Worded so that Domo never says "the owner" to the owner (trial fix 2, fix round 1).
        "With owner_first true, the owner must agree before they join",
        "in your judgement, the owner must agree first",
        "someone the household does not know, another adult, someone who will use the "
        "household's general key",
        "leave it out when the request is plain (the person's own child, say)",
        "An invitation made by whoever owns the household waits for nobody.",
    ):
        assert words in text, words
    assert tool.parameters["properties"]["owner_first"]["type"] == "boolean"
    assert "owner_first" not in tool.parameters["required"]


def test_household_tools_no_longer_say_the_owner_asks():
    """The tools are those of whoever has the right (the owner by default): they do not tell the
    model that the owner is the one who asks, or the only one who says yes or sees a link."""
    for name in HOUSEHOLD_TOOLS:
        description = TOOLS[name].definition.description
        assert "owner asks" not in description, name
        assert "the owner's yes" not in description, name
        assert "the owner is shown" not in description, name
    for name in ("household_invite", "household_change_role", "household_bring_back"):
        assert "what the person you are talking with wants" in TOOLS[name].definition.description
    assert "what the person you are talking with wants" in (
        TOOLS["household_cancel_invitation"].definition.description
    )


async def test_an_adult_who_calls_a_household_tool_anyway_is_refused(adult_chat, child_member):
    reply = await adult_chat.say_with_tool("household_remove", {"person": "Sam"})
    assert reply.actions == [
        {"tool": "household_remove", "status": "refused", "reason": "not_allowed"}
    ]
    async with adult_chat.server.session_factory() as reader:
        sam = (
            await reader.execute(select(Person).where(Person.id == child_member.person.id))
        ).one()[0]
    assert sam.disabled_at is None


@pytest.mark.parametrize(
    ("tool", "floor"),
    [
        ("household_invite", None),
        ("household_change_role", None),
        ("household_remove", None),
        ("household_cancel_invitation", None),
        ("household_bring_back", None),
        ("household_transfer_ownership", None),
    ],
)
def test_each_household_tool_does_its_action_of_the_catalog(tool, floor):  # D1
    action_type = TOOLS[tool].action_type
    assert action_type == tool.replace("household_", "household.")
    # The judge is asked about each, and none asks a yes by default (2026-11-15): handing the
    # owner's role over neither.
    assert needs_the_judge(action_type) and floor is None


# --- inviting ------------------------------------------------------------------------------------


async def test_an_invitation_waits_for_the_owners_yes_then_is_made_and_audited(
    owner_chat, count_invitations, last_audit_entry
):
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Robin", "role": "guest", "guest_days": 5}
    )
    assert reply.actions == [{"tool": "household_invite", "status": "waiting"}]
    assert await count_invitations() == 0  # nothing happened yet
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    async with owner_chat.server.session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert (invitation.display_name, invitation.role, invitation.guest_days) == (
        "Robin",
        "guest",
        5,
    )
    assert invitation.created_by == owner_chat.person.id
    entry = await last_audit_entry("household.invited")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {
        "name": "Robin",
        "role": "guest",
        "guest_days": 5,
        "invitation_id": str(invitation.id),
    }


async def test_domo_is_told_the_invitation_is_made_and_the_link_was_shown(owner_chat, chat_server):
    # When the judge asks no yes, the model reads the result of the call itself.
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "Alex", "role": "adult"}, judge="do"
    )
    assert reply.actions == [{"tool": "household_invite", "status": "done"}]
    assert reply.model_result_text == (
        '{"status": "done", "invited": "Alex", "role": "adult", "link_shown_to_owner": true}'
    )
    token = re.search(r"/invite/([A-Za-z0-9_-]+)", reply.cards_html)
    assert token and token.group(1) not in reply.model_result_text


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"display_name": "Alex"},
        {"display_name": "Alex", "role": "owner"},
        {"display_name": "Alex", "role": "Adult"},
        {"display_name": "Alex", "role": "adult", "guest_days": 2},
        {"display_name": "Alex", "role": "guest", "guest_days": 0},
        {"display_name": "Alex", "role": "guest", "guest_days": 5, "no_end": True},  # both
        {"display_name": "Alex", "role": "guest", "guest_days": "2"},
        {"display_name": "Alex", "role": "guest", "guest_days": 2.0},
        {"display_name": "Alex", "role": "guest", "guest_days": True},
        {"display_name": "Alex", "role": "adult", "link": "x"},
        {"display_name": 7, "role": "adult"},
        {"display_name": "  ", "role": "adult"},
        {"display_name": "a\nb", "role": "adult"},
        {"display_name": "Alex\N{RIGHT-TO-LEFT OVERRIDE}B", "role": "adult"},
        {"display_name": "Alex", "role": "adult", "owner_first": "yes"},
        {"display_name": "Alex", "role": "adult", "owner_first": 1},
    ],
)
async def test_invite_arguments_are_checked_before_anything(arguments, session_factory):
    owner = Person(id=uuid.uuid4(), display_name="Owner", role="owner")
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["household_invite"].describe(
            arguments, actor(owner), context(owner, session_factory)
        )
    assert raised.value.code == "invalid"


async def test_a_name_over_sixty_characters_is_too_long(session_factory):
    owner = Person(id=uuid.uuid4(), display_name="Owner", role="owner")
    with pytest.raises(InvalidArguments) as raised:
        await TOOLS["household_invite"].describe(
            {"display_name": "x" * 61, "role": "adult"},
            actor(owner),
            context(owner, session_factory),
        )
    assert raised.value.code == "too_long"


async def test_an_invitation_is_described_as_a_change_of_the_household(session_factory):
    owner = await committed_person(session_factory, "Owner", "owner")
    action = await TOOLS["household_invite"].describe(
        {"display_name": "Alex", "role": "guest"}, actor(owner), context(owner, session_factory)
    )
    assert action == DescribedAction(
        "core",
        "household.invite",
        None,
        "permissions",
        "household",
        {
            "role": "guest",
            "days": GUEST_DAYS_DEFAULT,
            "no_end": False,
            "display_name": "Alex",
            "owner_first": False,
            "exception_of": None,
            "serves_the_speaker": False,
        },
    )
    # Domo judged the owner should say yes first: a fact of the action, which the catalog's
    # limit of inviting reads.
    sent = await TOOLS["household_invite"].describe(
        {"display_name": "Sam", "role": "adult", "owner_first": True},
        actor(owner),
        context(owner, session_factory),
    )
    assert sent.facts["owner_first"] is True


async def test_a_name_already_in_the_household_is_not_given_twice(owner_chat, add_member):
    await add_member("Alex", "adult")
    reply = await owner_chat.say_with_tool(
        "household_invite", {"display_name": " ALEX ", "role": "child"}
    )
    assert reply.actions == [{"tool": "household_invite", "status": "invalid", "reason": "invalid"}]
    assert "already" in reply.model_result_text
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_invite", {"display_name": "Sam", "role": "child"})
    )
    again = await owner_chat.say_with_tool(
        "household_invite", {"display_name": "sam", "role": "adult"}
    )
    assert again.actions[0]["status"] == "invalid"


# --- changing a role -----------------------------------------------------------------------------


async def test_the_owner_changes_a_role(owner_chat, adult_member, person_row, last_audit_entry):
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "alex", "role": "child"}
    )
    assert reply.actions == [{"tool": "household_change_role", "status": "waiting"}]
    assert (await person_row(adult_member.person)).role == "adult"
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    assert (await person_row(adult_member.person)).role == "child"
    assert t("action.card.done.household.change_role.child", "en", name="Alex") in plain(card.html)
    entry = await last_audit_entry("household.role_changed")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {
        "person_id": str(adult_member.person.id),
        "name": "Alex",
        "old_role": "adult",
        "new_role": "child",
    }


async def test_a_guest_made_an_adult_stays_for_good(owner_chat, guest_member, person_row, clock):
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_change_role", {"person": "Robin", "role": "adult"}
        )
    )
    robin = await person_row(guest_member.person)
    assert (robin.role, robin.expires_at) == ("adult", None)
    clock.advance(GUEST_STAY + timedelta(days=1))
    async with live_http_client(owner_chat.server.base_url) as browser:
        await log_in_live(browser, "robin", guest_member.password)
        assert (await browser.get("/chat")).status_code == 200


async def test_someone_with_an_account_made_a_guest_stays_thirty_days_then_loses_access(
    owner_chat, child_member, live_child_client, person_row, last_audit_entry, clock
):  # R78
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Sam", "role": "guest"}
    )
    assert t(
        "action.describe.household.change_role.guest", "en", name="Sam", duration="30 days"
    ) in plain(reply.cards_html)
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    sam = await person_row(child_member.person)
    assert (sam.role, sam.expires_at) == ("guest", clock() + timedelta(days=GUEST_DAYS_DEFAULT))
    until = in_words(sam.expires_at)
    assert t(
        "action.card.done.household.change_role.guest", "en", name="Sam", until=until
    ) in plain(card.html)
    assert (await last_audit_entry("household.role_changed")).details == {
        "person_id": str(child_member.person.id),
        "name": "Sam",
        "old_role": "child",
        "new_role": "guest",
        "guest_days": GUEST_DAYS_DEFAULT,
    }
    assert (await live_child_client.get("/chat")).status_code == 200
    clock.advance(timedelta(days=GUEST_DAYS_DEFAULT, seconds=1))
    assert (await live_child_client.get("/chat")).headers["location"] == "/login"


async def test_someone_made_a_guest_stays_the_days_the_owner_said(
    owner_chat, adult_member, person_row, clock
):  # R78
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Alex", "role": "guest", "guest_days": 5}
    )
    await owner_chat.confirm_last(reply)
    alex = await person_row(adult_member.person)
    assert (alex.role, alex.expires_at) == ("guest", clock() + timedelta(days=5))
    page = (await owner_chat.get("/household")).text
    until = in_words(alex.expires_at)
    assert re.search(
        rf'data-person-id="{alex.id}"[^>]*data-person-role="guest"[^>]*data-person-state="has_access"',
        page,
    )
    assert t("household.state.guest", "en", date=until) in plain(page)


async def test_a_guests_stay_is_extended_or_shortened_as_a_change_of_role(
    owner_chat, guest_member, person_row, last_audit_entry, clock
):  # spec §5.5, §8.9
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Robin", "role": "guest", "guest_days": 5}
    )
    assert reply.actions == [{"tool": "household_change_role", "status": "waiting"}]
    # Whoever says yes reads a change of stay, not a change of role.
    assert t(
        "action.describe.household.change_role.stay", "en", name="Robin", duration="5 days"
    ) in plain(reply.cards_html)
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    robin = await person_row(guest_member.person)
    # The new end is counted from now: longer than the stay they had.
    assert (robin.role, robin.expires_at) == ("guest", clock() + timedelta(days=5))
    until = in_words(robin.expires_at)
    assert t(
        "action.card.done.household.change_role.stay", "en", name="Robin", until=until
    ) in plain(card.html)
    entry = await last_audit_entry("household.stay_changed")
    assert entry.actor == f"person:{owner_chat.person.id}"
    assert entry.details == {
        "person_id": str(guest_member.person.id),
        "name": "Robin",
        "guest_days": 5,
    }
    # Shorter: "Robin leaves tomorrow".
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_change_role", {"person": "Robin", "role": "guest", "guest_days": 1}
        )
    )
    assert card.status == "executed"
    robin = await person_row(guest_member.person)
    assert (robin.role, robin.expires_at) == ("guest", clock() + timedelta(days=1))
    # Without a stay there is nothing to change: Domo is told to give one.
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Robin", "role": "guest"}
    )
    assert reply.actions == [
        {"tool": "household_change_role", "status": "invalid", "reason": "invalid"}
    ]
    assert "give guest_days" in reply.model_result_text
    assert (await person_row(guest_member.person)).expires_at == robin.expires_at


async def test_a_changed_stay_of_any_length_is_not_refused(
    owner_chat, guest_member, person_row, clock
):  # audit POL-5
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_change_role", {"person": "Robin", "role": "guest", "guest_days": 400}
        )
    )
    assert card.status == "executed"
    assert (await person_row(guest_member.person)).expires_at == clock() + timedelta(days=400)
    # Or no end at all: a card of its own, with no day to say.
    card = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_change_role", {"person": "Robin", "role": "guest", "no_end": True}
        )
    )
    assert card.status == "executed"
    assert (await person_row(guest_member.person)).expires_at is None
    assert t("action.card.done.household.change_role.stay.no_end", "en", name="Robin") in plain(
        card.html
    )


async def test_domo_is_told_a_guests_stay_changed(guest_member, tool_context):
    done = await TOOLS["household_change_role"].execute(
        {"person": "Robin", "role": "guest", "guest_days": 3}, tool_context
    )
    assert done.model_result == {
        "person": "Robin",
        "role": "guest",
        "guest_days": 3,
        "stay_changed": True,
    }
    assert done.card["key"] == "action.card.done.household.change_role.stay"


async def test_the_card_of_a_guests_stay_keeps_its_end_as_iso_8601(
    guest_member, tool_context, person_row
):
    # Its date is put in words when the card is shown, in the reader's language (R15).
    done = await TOOLS["household_change_role"].execute(
        {"person": "Robin", "role": "guest", "guest_days": 3}, tool_context
    )
    robin = await person_row(guest_member.person)
    assert set(done.card["dates"]) == {"until"}
    assert datetime.fromisoformat(done.card["dates"]["until"]) == robin.expires_at
    assert "until" not in done.card["params"]


def test_the_sentence_of_a_change_of_stay_reads_the_role_the_person_has():
    arguments = {"person": "Robin", "role": "guest", "guest_days": 4}
    facts = {"person": "person:x", "current_role": "guest", "role": "guest", "days": 4}
    stay = DescribedAction("core", "household.change_role", None, "permissions", "household", facts)
    for lang, duration in (("en", "4 days"),):
        assert describe_for_people(stay, arguments, CatalogTexts(lang)) == t(
            "action.describe.household.change_role.stay", lang, name="Robin", duration=duration
        )
    # Someone who is not a guest yet is made one: the sentence of a change of role.
    made = DescribedAction(
        "core",
        "household.change_role",
        None,
        "permissions",
        "household",
        {**facts, "current_role": "adult"},
    )
    assert describe_for_people(made, arguments, CatalogTexts("en")) == t(
        "action.describe.household.change_role.guest", "en", name="Robin", duration="4 days"
    )


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"person": "Owner", "role": "adult"}, "invalid"),  # the owner's role never changes
        ({"person": "Alex", "role": "owner"}, "invalid"),  # nobody becomes the owner
        ({"person": "Alex", "role": "adult"}, "invalid"),  # already so
        ({"person": "Alex"}, "invalid"),
        ({"person": "Alex", "role": "child", "why": "x"}, "invalid"),
        ({"person": 3, "role": "child"}, "invalid"),
        ({"person": "Nobody", "role": "child"}, "not_found"),
        ({"person": "", "role": "child"}, "invalid"),
        # Days from 1, as for an invitation (the household's rule of 30 days is a limit:
        # test_policy_limits.py); for someone who is not a guest, an end of their access
        # (the owner's answer of 2026-11-27: test_people_stays.py).
        ({"person": "Alex", "role": "guest", "guest_days": 0}, "invalid"),
        ({"person": "Alex", "role": "guest", "guest_days": 5, "no_end": True}, "invalid"),
        ({"person": "Alex", "role": "guest", "guest_days": "3"}, "invalid"),
        ({"person": "Alex", "role": "guest", "guest_days": True}, "invalid"),
    ],
)
async def test_changing_a_role_checks_who_and_what(owner_chat, adult_member, arguments, code):
    reply = await owner_chat.say_with_tool("household_change_role", arguments)
    assert reply.actions == [{"tool": "household_change_role", "status": "invalid", "reason": code}]


# --- removing someone ----------------------------------------------------------------------------


async def test_removing_closes_the_account_and_keeps_the_data(
    owner_chat, adult_chat, person_row, last_audit_entry, chat_server
):
    await adult_chat.say("bonjour")  # something Alex wrote
    card = await remove(owner_chat, "Alex")
    assert card.status == "executed"
    assert t("action.card.done.household.remove", "en", name="Alex") in plain(card.html)
    alex = await person_row(adult_chat.person)
    assert alex.disabled_at is not None and alex.display_name == "Alex"
    async with chat_server.session_factory() as reader:
        sessions = (
            await reader.execute(
                select(func.count())
                .select_from(WebSession)
                .where(WebSession.account_id.in_(select_account_id(adult_chat.person.id)))
            )
        ).scalar_one()
        kept = (
            await reader.execute(
                select(func.count()).select_from(Message).where(Message.content == "bonjour")
            )
        ).scalar_one()
    assert sessions == 0 and kept == 1
    entry = await last_audit_entry("household.removed")
    assert entry.details == {
        "person_id": str(adult_chat.person.id),
        "name": "Alex",
        "role": "adult",
        "requests_refused": 0,
    }


async def test_removing_someone_pauses_their_personal_rights(
    owner_chat, adult_member, grant, session_factory, last_audit_entry
):  # spec 5.7
    mine = await grant(
        "household.invite", person=adult_member.person, access="allow", summary="Alex invites"
    )
    adults = await grant("household.invite", role="adult", access="allow", summary="Adults invite")
    card = await remove(owner_chat, "Alex")
    assert card.status == "executed"
    # The card says which rights wait for the owner, the person's own only.
    words = plain(card.html)
    assert t("action.card.rights.paused", "en") in words and "Alex invites" in words
    assert "Adults invite" not in words
    async with session_factory() as reader:
        rows = {r.id: r for r in (await reader.execute(select(PolicyRight))).scalars()}
    assert rows[mine.id].paused_at is not None and rows[mine.id].revoked_at is None
    # The right of their role is not theirs: it stays.
    assert rows[adults.id].paused_at is None
    paused = await last_audit_entry("rights.paused")
    assert paused.details == {"person_id": str(adult_member.person.id), "count": 1}
    # The one who removed them did it: the same actor as the removal's own entry (written after).
    removal = await last_audit_entry("household.removed")
    assert paused.actor == removal.actor == f"person:{owner_chat.person.id}"
    assert removal.id > paused.id


async def test_removing_someone_without_a_right_of_their_own_pauses_and_audits_nothing(
    owner_chat, adult_member, audit_actions
):
    card = await remove(owner_chat, "Alex")
    assert t("action.card.rights.paused", "en") not in plain(card.html)
    assert "rights.paused" not in await audit_actions()


async def test_domo_is_told_how_many_rights_the_removal_paused(
    owner_member, add_member, grant, tool_context
):
    alex = await add_member("Alex", "adult")
    await add_member("Jo", "adult", username="jo")
    await grant("household.invite", person=alex, access="allow", summary="Alex invites")
    none = await TOOLS["household_remove"].execute({"person": "Jo"}, tool_context)
    # The owner's own removal is his withdrawal (spec 6.9): Domo is told so.
    owners = {"withdrawn_by_the_owner": True}
    assert none.model_result == {"removed": "Jo", "paused_rights": 0, **owners}
    assert "rights" not in none.card
    some = await TOOLS["household_remove"].execute({"person": "Alex"}, tool_context)
    assert some.model_result == {"removed": "Alex", "paused_rights": 1, **owners}
    assert some.card["rights"] == {"key": "action.card.rights.paused", "items": ["Alex invites"]}


async def test_a_removed_person_cannot_log_in_again(owner_chat, adult_member):
    await remove(owner_chat, "Alex")
    async with live_http_client(owner_chat.server.base_url) as browser:
        r = await browser.post(
            "/login", data={"username": "alex", "password": adult_member.password}
        )
        assert r.status_code == 400
        assert t("login.error.invalid", "en") in plain(r.text)


async def test_a_removed_persons_requests_can_no_longer_be_approved(
    owner_chat,
    adult_chat,
    child_chat,
    an_adult_agrees_to_a_childs_rename,
    confirmation_status,
    display_name_of,
    last_audit_entry,
):  # Review Focus 4 (R39)
    own = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    childs = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    await remove(owner_chat, "Sam")
    assert await confirmation_status(childs.pending_confirmation_id) == "refused"
    assert (await last_audit_entry("household.removed")).details["requests_refused"] == 1
    # The adult who would have approved Sam's request finds it closed, and nothing runs.
    r = await adult_chat.post(f"/confirmations/{childs.pending_confirmation_id}/approve")
    assert status_of(r) == "already_decided"
    assert await display_name_of(child_chat.person) == "Sam"
    # Alex's own request is refused when Alex is removed; Alex's open tab can no longer answer.
    await remove(owner_chat, "Alex")
    assert await confirmation_status(own.pending_confirmation_id) == "refused"
    r = await adult_chat.post(f"/confirmations/{own.pending_confirmation_id}/approve")
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"
    assert await display_name_of(adult_chat.person) == "Alex"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"person": "Owner"}, "invalid"),  # the owner is never removed, not even by themselves
        ({"person": "Nobody"}, "not_found"),
        # A stay that is over is no refusal for the owner: his removal is a withdrawal (final
        # review B M4).
        ({}, "invalid"),
        ({"person": "Alex", "now": True}, "invalid"),
    ],
)
async def test_removing_checks_who(owner_chat, adult_member, guest_member, clock, arguments, code):
    clock.advance(GUEST_STAY + timedelta(seconds=1))
    reply = await owner_chat.say_with_tool("household_remove", arguments)
    assert reply.actions == [{"tool": "household_remove", "status": "invalid", "reason": code}]


def may_also(chat, *action_types: str, role: str) -> None:
    """The pilots' server under a policy in which ``role`` may also do ``action_types`` (as when
    the owner gives that right)."""
    policy = PolicyTables(
        KNOWN_ACTIONS,
        {**DEFAULT_POLICY.allowed, role: DEFAULT_POLICY.allowed[role] | set(action_types)},  # type: ignore[index]
    )
    chat.app.dependency_overrides[get_policy] = lambda: policy


async def test_a_member_who_may_remove_people_may_remove_themselves(
    adult_chat, child_member, owner_member, person_row, last_audit_entry
):
    may_also(adult_chat, "household.remove", role="adult")
    reply = await adult_chat.say_with_tool("household_remove", {"person": "Alex"})
    assert reply.actions == [{"tool": "household_remove", "status": "waiting"}]
    assert (await person_row(adult_chat.person)).disabled_at is None  # not before the yes
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    alex = await person_row(adult_chat.person)
    assert alex.disabled_at is not None and alex.display_name == "Alex"
    entry = await last_audit_entry("household.removed")
    assert entry.actor == f"person:{adult_chat.person.id}"
    assert entry.details == {
        "person_id": str(adult_chat.person.id),
        "name": "Alex",
        "role": "adult",
        "requests_refused": 0,
    }
    # The decision says it was themselves they removed, and the others are untouched.
    assert (await last_audit_entry("policy.decision")).details["facts"] == {
        "person": f"person:{adult_chat.person.id}",
        "current_role": "adult",
        "is_self": True,
        "given_by_domo": False,
        "made_role": None,
        "has_account": True,
        "state": "has_access",
    }
    assert (await person_row(child_member.person)).disabled_at is None
    assert (await person_row(owner_member.person)).disabled_at is None
    # They are out: their browser is not let in any more.
    assert (await adult_chat.get("/chat")).status_code in (303, 401)


async def test_a_member_who_may_remove_people_removes_another_without_leaving(
    adult_chat, child_member, person_row, last_audit_entry
):
    may_also(adult_chat, "household.remove", role="adult")
    card = await adult_chat.confirm_last(
        await adult_chat.say_with_tool("household_remove", {"person": "Sam"})
    )
    assert card.status == "executed"
    assert (await person_row(child_member.person)).disabled_at is not None
    assert (await person_row(adult_chat.person)).disabled_at is None
    facts = (await last_audit_entry("policy.decision")).details["facts"]
    assert facts == {
        "person": f"person:{child_member.person.id}",
        "current_role": "child",
        "is_self": False,
        "given_by_domo": False,
        "made_role": None,
        "has_account": True,
        "state": "has_access",
    }


async def test_nobody_removes_the_owner_even_with_the_right_to_remove(
    adult_chat, owner_member, person_row
):
    may_also(adult_chat, "household.remove", role="adult")
    reply = await adult_chat.say_with_tool("household_remove", {"person": "Owner"})
    assert reply.actions == [{"tool": "household_remove", "status": "invalid", "reason": "invalid"}]
    assert (await person_row(owner_member.person)).disabled_at is None


async def test_an_adult_who_may_change_roles_does_not_change_their_own(adult_chat, person_row):
    may_also(adult_chat, "household.change_role", role="adult")
    reply = await adult_chat.say_with_tool(
        "household_change_role", {"person": "Alex", "role": "child"}
    )
    assert reply.actions == [
        {"tool": "household_change_role", "status": "invalid", "reason": "invalid"}
    ]
    assert "own role" in reply.model_result_text
    assert (await person_row(adult_chat.person)).role == "adult"


async def test_nobody_changes_their_own_role(child_chat, person_row):
    may_also(child_chat, "household.change_role", role="child")
    reply = await child_chat.say_with_tool(
        "household_change_role", {"person": "Sam", "role": "adult"}
    )
    assert reply.actions == [
        {"tool": "household_change_role", "status": "invalid", "reason": "invalid"}
    ]
    assert "own role" in reply.model_result_text
    assert (await person_row(child_chat.person)).role == "child"


async def test_a_member_who_may_change_roles_still_cannot_touch_the_owners(
    adult_chat, owner_member, person_row
):
    may_also(adult_chat, "household.change_role", role="adult")
    reply = await adult_chat.say_with_tool(
        "household_change_role", {"person": "Owner", "role": "guest"}
    )
    assert reply.actions == [
        {"tool": "household_change_role", "status": "invalid", "reason": "invalid"}
    ]
    assert (await person_row(owner_member.person)).role == "owner"


# --- leaving the household -----------------------------------------------------------------------


async def test_an_adult_a_child_or_a_guest_may_leave_the_household_but_not_remove_another(
    adult_chat,
    child_chat,
    guest_chat,
    live_owner_client,
    person_row,
    confirmation_status,
    last_audit_entry,
):  # spec §5.9, §8.9
    # Removing someone else stays the owner's, or a right's: Alex may not remove Sam (F27: before
    # anyone leaves).
    reply = await adult_chat.say_with_tool("household_remove", {"person": "Sam"})
    assert reply.actions == [
        {"tool": "household_remove", "status": "refused", "reason": "not_allowed"}
    ]
    assert (await person_row(child_chat.person)).disabled_at is None
    # Sam, a child, leaves: an adult says yes (the owner, here, with the button).
    reply = await child_chat.say_with_tool("household_leave", {})
    assert reply.actions == [{"tool": "household_leave", "status": "waiting"}]
    assert json.loads(reply.model_result_text)["by"] == "an adult"
    assert t("action.describe.household.leave", "en") in plain(reply.cards_html)
    assert (await person_row(child_chat.person)).disabled_at is None  # not before the yes
    answer = await live_owner_client.post(
        f"/confirmations/{reply.pending_confirmation_id}/approve", headers={"hx-request": "true"}
    )
    assert status_of(answer) == "executed"
    assert t("action.card.done.household.leave", "en", name="Sam") in plain(answer.text)
    sam = await person_row(child_chat.person)
    assert sam.disabled_at is not None and sam.display_name == "Sam"  # nothing is erased
    entry = await last_audit_entry("household.left")
    assert entry.actor == f"person:{child_chat.person.id}"
    assert entry.details == {
        "person_id": str(child_chat.person.id),
        "name": "Sam",
        "role": "child",
        "requests_refused": 0,
        "rights_paused": 0,
    }
    # Robin, a guest, leaves early: their own yes, in words.
    reply = await guest_chat.say_with_tool("household_leave", {})
    assert reply.actions == [{"tool": "household_leave", "status": "waiting"}]
    asked = reply.pending_confirmation_id
    answer = await guest_chat.say("je confirme")
    assert answer.actions == [{"tool": "household_leave", "status": "done"}]
    assert await confirmation_status(asked) == "executed"
    assert (await person_row(guest_chat.person)).disabled_at is not None
    assert (await last_audit_entry("household.left")).details["role"] == "guest"
    # Alex, an adult, leaves too: their own yes, with the button.
    reply = await adult_chat.say_with_tool("household_leave", {})
    assert reply.actions == [{"tool": "household_leave", "status": "waiting"}]
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    alex = await person_row(adult_chat.person)
    assert alex.disabled_at is not None and alex.display_name == "Alex"
    assert (await adult_chat.get("/chat")).status_code in (303, 401)


async def test_a_right_still_lets_someone_remove_others(
    adult_chat, child_member, grant, person_row
):  # F16: household.remove is unchanged
    await grant(
        "household.remove", person=adult_chat.person, access="allow", summary="Alex removes"
    )
    reply = await adult_chat.say_with_tool("household_remove", {"person": "Sam"})
    assert reply.actions == [{"tool": "household_remove", "status": "waiting"}]
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    assert (await person_row(child_member.person)).disabled_at is not None
    assert (await person_row(adult_chat.person)).disabled_at is None


async def test_the_owner_still_removes_others_and_never_leaves(
    owner_chat, adult_member, person_row
):  # Review Focus 5
    reply = await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    assert reply.actions == [{"tool": "household_remove", "status": "waiting"}]
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    assert (await person_row(adult_member.person)).disabled_at is not None
    reply = await owner_chat.say_with_tool("household_leave", {})
    assert reply.actions == [{"tool": "household_leave", "status": "invalid", "reason": "invalid"}]
    assert "never leaves" in reply.model_result_text
    assert "hand" in reply.model_result_text  # they hand their role over first
    assert (await person_row(owner_chat.person)).disabled_at is None


async def test_leaving_takes_no_arguments(adult_chat, child_member, person_row):
    reply = await adult_chat.say_with_tool("household_leave", {"person": "Sam"})
    assert reply.actions == [{"tool": "household_leave", "status": "invalid", "reason": "invalid"}]
    assert (await person_row(child_member.person)).disabled_at is None
    assert (await person_row(adult_chat.person)).disabled_at is None


async def test_leaving_pauses_the_persons_own_rights(
    adult_member, grant, tool_context, session_factory, last_audit_entry
):  # spec §5.7
    alex = adult_member.person
    mine = await grant("household.invite", person=alex, access="allow", summary="Alex invites")
    ctx = replace(tool_context, actor=Actor(alex.id, "adult"), person=alex)
    done = await TOOLS["household_leave"].execute({}, ctx)
    assert done.model_result == {"left": "Alex", "paused_rights": 1}
    assert done.card == {"key": "action.card.done.household.leave", "params": {"name": "Alex"}}
    async with session_factory() as reader:
        right = await reader.get_one(PolicyRight, mine.id)
    assert right.paused_at is not None and right.revoked_at is None
    paused = await last_audit_entry("rights.paused")
    assert paused.actor == f"person:{alex.id}"  # they left themselves
    assert (await last_audit_entry("household.left")).details["rights_paused"] == 1


def test_household_leave_is_every_members_and_never_the_owners():  # F16
    tool = TOOLS["household_leave"]
    assert tool.action_type == "household.leave"
    assert needs_the_judge("household.leave")
    for role in ROLES:
        assert "household.leave" in ALLOWED_ACTIONS[role]
        assert "household_leave" in {
            d.name for d in tools_for(an_actor(role), TOOLS, DEFAULT_POLICY)
        }
    # No limit, nothing explicit: the barrier's default approver (an adult, for a child).
    assert "household.leave" not in DEFAULT_LIMITS
    assert "household.leave" not in OWNER_ONLY_ACTIONS
    assert dict(ACTION_FACTS["household.leave"]) == {"current_role": "role"}
    assert tool.definition.parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert "household_remove" in tool.definition.description
    # The owner never leaves: a refusal of the tool, whatever the rights.
    assert isinstance(tool, RefusesWhateverTheRights)
    refused = tool.fixed_refusal(Actor(uuid.uuid4(), "owner"), {"current_role": "owner"}, {})
    assert refused == "the owner never leaves: they hand their role over first"
    for role in ("adult", "child", "guest"):
        assert tool.fixed_refusal(Actor(uuid.uuid4(), role), {"current_role": role}, {}) is None
    # Removing someone stays the owner's, unless a right says otherwise.
    for role in ("adult", "child", "guest"):
        assert "household.remove" not in ALLOWED_ACTIONS[role]
    assert "household.remove" not in DEFAULT_LIMITS


async def test_a_name_that_means_someone_else_by_the_yes_acts_on_nobody(
    owner_chat, adult_member, child_member, person_row, chat_server
):  # Review Focus 5
    reply = await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    # Before the owner's yes, Alex takes another name and Sam takes Alex's.
    async with chat_server.session_factory() as writer:
        await writer.execute(
            update(Person)
            .where(Person.id == adult_member.person.id)
            .values(display_name="Alexandre")
        )
        await writer.execute(
            update(Person).where(Person.id == child_member.person.id).values(display_name="Alex")
        )
        await writer.commit()
    card = await owner_chat.confirm_last(reply)
    # The call, described again at the yes, no longer acts on the one the yes was given to.
    assert card.status == "refused"
    assert t("confirmation.outcome.no_longer_possible", "en") in plain(card.html)
    assert (await person_row(child_member.person)).disabled_at is None
    assert (await person_row(adult_member.person)).disabled_at is None


# --- cancelling an invitation --------------------------------------------------------------------


async def test_the_owner_cancels_an_invitation_and_its_link_stops_working(
    owner_chat, last_audit_entry
):
    invited = await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Alex", "role": "adult"}
        )
    )
    reply = await owner_chat.say_with_tool("household_cancel_invitation", {"display_name": "ALEX"})
    assert reply.actions == [{"tool": "household_cancel_invitation", "status": "waiting"}]
    answer = await owner_chat.say("oui")  # level confirm: a plain yes is enough by default
    assert answer.actions == [{"tool": "household_cancel_invitation", "status": "done"}]
    async with live_http_client(owner_chat.server.base_url) as invitee:
        r = await invitee.get(f"/invite/{invited.invitation_token}")
        assert r.status_code == 410
    async with owner_chat.server.session_factory() as reader:
        invitation = (await reader.execute(select(Invitation))).scalar_one()
    assert invitation.cancelled_at is not None
    entry = await last_audit_entry("household.invitation_cancelled")
    assert entry.details == {"invitation_id": str(invitation.id), "name": "Alex", "role": "adult"}


@pytest.mark.parametrize(
    ("arguments", "code"), [({"display_name": "Sam"}, "not_found"), ({}, "invalid")]
)
async def test_cancelling_names_an_invitation_that_waits(owner_chat, arguments, code):
    reply = await owner_chat.say_with_tool("household_cancel_invitation", arguments)
    assert reply.actions == [
        {"tool": "household_cancel_invitation", "status": "invalid", "reason": code}
    ]


# --- what the requests say -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action_type", "arguments", "lang", "expected"),
    [
        (
            "household.invite",
            {"display_name": "Alex", "role": "child"},
            "en",
            ("action.describe.household.invite", {"name": "Alex", "role": "child"}),
        ),
        (
            "household.invite",
            {"display_name": "Tom", "role": "guest", "guest_days": 3},
            "en",
            ("action.describe.household.invite.guest", {"name": "Tom", "duration": "3 days"}),
        ),
        (
            "household.invite",
            {"display_name": "Tom", "role": "guest"},
            "en",
            ("action.describe.household.invite.guest", {"name": "Tom", "duration": "30 days"}),
        ),
        (
            "household.invite",
            {"display_name": "Tom", "role": "guest", "no_end": True},
            "en",
            ("action.describe.household.invite.guest.no_end", {"name": "Tom"}),
        ),
        (
            "household.change_role",
            {"person": "Alex", "role": "child"},
            "en",
            ("action.describe.household.change_role", {"name": "Alex", "role": "child"}),
        ),
        (
            "household.change_role",
            # Without the described facts, the stay is the record's (no end without an account).
            {"person": "Alex", "role": "guest"},
            "en",
            ("action.describe.household.change_role", {"name": "Alex", "role": "guest"}),
        ),
        (
            "household.change_role",
            {"person": "Alex", "role": "guest", "guest_days": 1},
            "en",
            ("action.describe.household.change_role.guest", {"name": "Alex", "duration": "1 day"}),
        ),
        (
            "household.remove",
            {"person": "Alex"},
            "en",
            ("action.describe.household.remove", {"name": "Alex"}),
        ),
        (
            "household.cancel_invitation",
            {"display_name": "Alex"},
            "en",
            ("action.describe.household.cancel_invitation", {"name": "Alex"}),
        ),
    ],
)
def test_each_household_request_reads_as_a_plain_sentence(action_type, arguments, lang, expected):
    key, params = expected
    if "role" in params:
        params = {**params, "role": t(f"household.role.{params['role']}", lang)}
    action = DescribedAction("core", action_type, None, "permissions", "household")
    sentence = describe_for_people(action, arguments, CatalogTexts(lang))
    assert sentence == t(key, lang, **params)
    assert "household." not in sentence and "guest_days" not in sentence


# --- what Domo is told of the household --------------------------------------------------------


def people_of(reply) -> dict[str, dict]:
    """The people ``household_list`` told Domo, by name."""
    return {person["name"]: person for person in reply.model_results[-1]["people"]}


async def test_household_list_tells_the_last_visit_to_those_who_may_remove_people(
    owner_chat, adult_chat, guest_member, grant, clock
):
    clock.advance(timedelta(days=1))
    await adult_chat.get("/chat")  # Alex's session is seen now, a day after it was made
    seen = clock().astimezone(UTC).date().isoformat()

    told = people_of(await owner_chat.say_with_tool("household_list", {}))
    assert told["Alex"]["last_seen"] == seen and told["Owner"]["last_seen"] == seen
    # Robin has never come: nothing to say, which is not the same as nothing to know.
    assert told["Robin"]["last_seen"] is None

    # Those who may not remove anyone are not told when others last came.
    plain_list = people_of(await adult_chat.say_with_tool("household_list", {}))
    assert all("last_seen" not in person for person in plain_list.values())

    # The right to remove someone is the right to be told.
    await grant("household.remove", person=adult_chat.person, access="allow")
    allowed = people_of(await adult_chat.say_with_tool("household_list", {}))
    assert allowed["Alex"]["last_seen"] == seen and allowed["Robin"]["last_seen"] is None


async def test_the_last_visit_is_the_most_recent_of_the_sessions_in_utc(
    owner_chat, adult_member, session_factory, clock
):
    visits = [
        datetime(2030, 1, 1, 23, 30, tzinfo=UTC),
        datetime(2030, 1, 3, 23, 30, tzinfo=timezone(timedelta(hours=-5))),  # the 4th, in UTC
        datetime(2030, 1, 2, 12, 0, tzinfo=UTC),
    ]
    async with session_factory() as writer:
        account_id = (
            await writer.execute(
                select(Account.id).where(Account.person_id == adult_member.person.id)
            )
        ).scalar_one()
        await writer.execute(
            update(WebSession)
            .where(WebSession.account_id == account_id)
            .values(last_seen_at=visits[0])
        )
        for number, visit in enumerate(visits[1:]):
            writer.add(
                WebSession(
                    account_id=account_id,
                    token_hash=f"other-{number}",
                    expires_at=clock() + timedelta(days=1),
                    last_seen_at=visit,
                )
            )
        await writer.commit()
    told = people_of(await owner_chat.say_with_tool("household_list", {}))
    assert told["Alex"]["last_seen"] == "2030-01-04"


async def test_the_card_of_a_role_change_shows_the_rights_the_person_keeps(
    owner_chat, adult_member, add_member, grant
):  # spec 5.12, ruling R8
    await grant(
        "household.invite",
        person=adult_member.person,
        access="allow",
        summary="Alex may invite people",
    )
    await add_member("Jo", "adult")
    reply = await owner_chat.say_with_tool(
        "household_change_role", {"person": "Alex", "role": "child"}
    )
    card = await owner_chat.confirm_last(reply)
    assert card.status == "executed"
    words = plain(card.html)
    assert t("action.card.rights.kept", "en") in words and "Alex may invite people" in words
    # The stored card says the same, on a page loaded later.
    assert "Alex may invite people" in plain((await owner_chat.get("/chat")).text)
    # Someone who has no right of their own: nothing more on the card.
    other = await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_change_role", {"person": "Jo", "role": "child"})
    )
    assert t("action.card.rights.kept", "en") not in plain(other.html)


async def test_the_card_of_a_role_change_by_someone_else_shows_the_rights_kept_too(
    adult_chat, add_member, grant
):  # Ruling R26: whoever asked
    jo = await add_member("Jo", "adult")
    await grant("household.change_role", person=adult_chat.person, access="allow")
    await grant("household.invite", person=jo, access="allow", summary="Jo may invite people")
    reply = await adult_chat.say_with_tool(
        "household_change_role", {"person": "Jo", "role": "child"}
    )
    card = await adult_chat.confirm_last(reply)
    assert card.status == "executed"
    words = plain(card.html)
    assert t("action.card.rights.kept", "en") in words and "Jo may invite people" in words
    assert "Jo may invite people" in plain((await adult_chat.get("/chat")).text)


async def test_the_owner_who_says_yes_to_someone_elses_role_change_reads_the_rights_kept(
    owner_chat, adult_chat, add_member, grant
):  # spec 5.12, Ruling R26
    jo = await add_member("Jo", "adult")
    await grant("household.change_role", person=adult_chat.person, access="allow", approver="owner")
    await grant("household.invite", person=jo, access="allow", summary="Jo may invite people")
    reply = await adult_chat.say_with_tool(
        "household_change_role", {"person": "Jo", "role": "child"}
    )
    answer = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(answer) == "executed"
    words = plain(answer.text)
    assert t("action.card.rights.kept", "en") in words and "Jo may invite people" in words


# --- the household page --------------------------------------------------------------------------


async def test_the_household_page_shows_people_roles_states_and_waiting_invitations(
    owner_chat, adult_member, child_member, guest_member, add_member
):
    gone = await add_member("Charlie", "adult", username="charlie")
    await remove(owner_chat, "Charlie")
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "household_invite", {"display_name": "Dana", "role": "child"}
        )
    )
    r = await owner_chat.get("/household")
    assert r.status_code == 200
    rows = re.findall(r'<li class="household-person"[^>]*data-person-role="([a-z]+)"[^>]*>', r.text)
    assert sorted(rows) == ["adult", "adult", "child", "guest", "owner"]
    text = plain(r.text)
    for name in ("Owner", "Alex", "Sam", "Robin", "Charlie", "Dana"):
        assert name in text
    assert t("household.role.guest", "en") in text
    assert re.search(
        rf'data-person-id="{gone.id}"[^>]*data-person-state="withdrawn"', r.text
    ) or re.search(rf'data-person-state="withdrawn"[^>]*data-person-id="{gone.id}"', r.text)
    assert re.search(r'<li class="household-invitation"[^>]*data-invitation-role="child"', r.text)
    assert "/invite/" not in r.text  # the link is never shown again


async def test_a_name_with_html_is_shown_escaped_on_the_household_page(
    live_owner_client, add_member
):  # Review Focus 3 (R39)
    await add_member("<img src=x onerror=alert(1)>", "adult")
    r = await live_owner_client.get("/household")
    assert "<img src=x" not in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text


async def test_only_the_owner_has_a_household_link(live_owner_client, live_adult_client):
    assert '<a href="/household">' in (await live_owner_client.get("/chat")).text
    assert '<a href="/household">' not in (await live_adult_client.get("/chat")).text


async def test_the_household_page_speaks_the_language_of_its_reader(
    live_owner_client, owner_member, session_factory
):
    async with session_factory() as writer:
        await reads_in(writer, owner_member.person, "fr")
        await writer.commit()
    r = await live_owner_client.get("/household")
    assert '<html lang="fr">' in r.text
    assert in_language("fr", "household.title") in html.unescape(r.text)


# --- the service ---------------------------------------------------------------------------------


async def test_a_name_means_one_person_in_the_household_now(session, add_person, clock):
    alex = await add_person("Alex", "adult")
    await add_person("Gone", "adult")
    await session.execute(
        update(Person)
        .where(Person.display_name == "Gone")
        .values(disabled_at=clock(), state="gone")
    )
    over = await add_person("Over", "guest")
    over.expires_at = clock() - timedelta(seconds=1)
    await session.flush()
    assert (await resolve_person(session, "  aLEX ", now=clock())).id == alex.id
    # A piece from inside a name means nobody ("Ale", the beginning of "Alex", now means Alex:
    # spec 6.7, ``test_resolve_person.py``).
    for name in ("Gone", "Over", "Alexandre", "lex", ""):
        with pytest.raises(InvalidArguments) as raised:
            await resolve_person(session, name, now=clock())
        assert raised.value.code == "not_found"
    await add_person("ALEX", "child")
    with pytest.raises(InvalidArguments) as raised:
        await resolve_person(session, "alex", now=clock())
    assert raised.value.code == "ambiguous"


async def test_disabling_records_when_and_revokes_every_session(
    session, add_person, add_account, clock
):
    alex = await add_person("Alex", "adult")
    account = await add_account(alex, "alex")
    first = await create_session(session, account.id, ttl=timedelta(days=1))
    await create_session(session, account.id, ttl=timedelta(days=1))
    removed = await disable(session, alex, clock(), by=alex.id)
    assert removed == Removed(requests_refused=0, rights_paused=0)
    disabled_at = select(Person.disabled_at).where(Person.id == alex.id)
    assert (await session.execute(disabled_at)).scalar_one() == clock()
    assert await resolve_session(session, first, datetime.now(UTC)) is None
    left = (
        await session.execute(
            select(func.count()).select_from(WebSession).where(WebSession.account_id == account.id)
        )
    ).scalar_one()
    assert left == 0


async def test_disabling_pauses_the_personal_rights_and_counts_them(session, add_person, clock):
    owner = await add_person("Owner", "owner")
    alex = await add_person("Alex", "adult")
    jo = await add_person("Jo", "adult")
    session.add_all(
        PolicyRight(
            person_id=person_id,
            role=role,
            action_type=action_type,
            access="allow",
            summary="a right",
            owner_words="",
            created_by=owner.id,
            created_at=clock(),
        )
        for person_id, role, action_type in (
            (alex.id, None, "household.invite"),
            (alex.id, None, "note.leave"),
            (jo.id, None, "household.invite"),
            (None, "adult", "household.invite"),
        )
    )
    await session.flush()
    removed = await disable(session, alex, clock(), by=owner.id)
    assert removed == Removed(requests_refused=0, rights_paused=2)
    paused = await session.execute(
        select(PolicyRight.person_id, PolicyRight.role).where(PolicyRight.paused_at.is_not(None))
    )
    assert paused.all() == [(alex.id, None), (alex.id, None)]
    audited = select(AuditEntry.actor).where(AuditEntry.action == "rights.paused")
    # The one who removed them is the actor.
    assert (await session.execute(audited)).scalars().all() == [f"person:{owner.id}"]
    # Removed a second time, there is nothing left to pause, and nothing more is audited.
    assert await disable(session, alex, clock(), by=owner.id) == Removed(0, 0)
    assert len((await session.execute(audited)).scalars().all()) == 1


async def _give(session, person: Person, role: str, now, days: int | None = None) -> None:
    """``person`` given ``role`` on their record (``people.records.give_access``), as a change of
    role does."""
    await records.give_access(
        session,
        person,
        role=role,  # type: ignore[arg-type]
        made_role=None,
        standing=None,
        guest_days=stay_days(role, days),  # type: ignore[arg-type]
        by="domo",
        by_owner=False,
        reason="a test",
        turn_of=None,
        now=now,
    )


async def test_setting_a_role_ends_a_guests_time_limit(session, add_person, clock):
    tom = await add_person("Tom", "guest")
    tom.expires_at = clock() + timedelta(days=1)
    await session.flush()
    await _give(session, tom, "child", clock())
    await session.refresh(tom)
    assert (tom.role, tom.expires_at, tom.is_child) == ("child", None, True)


async def test_setting_the_guest_role_gives_an_end_of_stay(session, add_person, clock):  # R78
    alex = await add_person("Alex", "adult")
    await _give(session, alex, "guest", clock())
    await session.refresh(alex)
    end = clock() + timedelta(days=GUEST_DAYS_DEFAULT)
    assert (alex.role, alex.expires_at, alex.last_review_due) == ("guest", end, True)
    sam = await add_person("Sam", "child")
    await _give(session, sam, "guest", clock(), 400)
    await session.refresh(sam)
    assert sam.expires_at == clock() + timedelta(days=400)
    # The days a call may give: from 1, with no ceiling, only for a guest, and never with no_end.
    assert guest_days_from({"guest_days": 400}, "guest") == 400
    for role, days in (("guest", 0), ("guest", -1), ("adult", 2)):
        with pytest.raises(InvalidArguments):
            guest_days_from({"guest_days": days}, role)  # type: ignore[arg-type]
    with pytest.raises(InvalidArguments):
        guest_days_from({"guest_days": 5, "no_end": True}, "guest")


async def test_the_service_never_makes_an_owner_by_invitation(session, add_person, clock):
    owner = await add_person("Owner", "owner")
    with pytest.raises(ValueError):
        await create_invitation(
            session,
            display_name="X",
            role="owner",  # type: ignore[arg-type]
            guest_days=None,
            created_by=owner.id,
            now=clock(),
        )


# --- helpers of this file ------------------------------------------------------------------------


def actor(person: Person) -> Actor:
    return actor_of(person)


def context(person: Person, session_factory) -> ToolContext:
    """The context of a call made by ``person`` (what the tools do not use is left empty)."""
    return ToolContext(
        session_factory=session_factory,
        actor=actor(person),
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


async def committed_person(session_factory, name: str, role: str) -> Person:
    async with session_factory() as writer:
        person = Person(display_name=name, role=role)
        writer.add(person)
        await writer.commit()
        return person


def select_account_id(person_id: uuid.UUID):
    return select(Account.id).where(Account.person_id == person_id)
