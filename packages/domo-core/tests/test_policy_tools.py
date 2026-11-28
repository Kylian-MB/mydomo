"""The owner's rules (task 8, decision D8): Domo records the owner's settings (``policy_update``:
how long a request may wait, the rights writer's time, the household's time zone) and says what is
set (``policy_show``: the settings and the rights).

How an action is confirmed is no setting any more: the yes-judge decides it, and the owner's rules
"always ask me before..." are rights. ``policy_update`` is judged like any other action.

The chat pilots talk with Domo on a live server (see ``chat_support.py``).
"""

import html
import json
import re
import uuid
from datetime import timedelta

import pytest
from chat_support import status_of
from domo_common.i18n import t
from domo_core.confirmations.describe import describe_for_people
from domo_core.confirmations.store import ANSWER_IN_WORDS
from domo_core.llm.types import ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.models import PolicyRight
from domo_core.policy.rights import pause_personal_rights
from domo_core.policy.roles import Actor
from domo_core.policy.settings import LIFETIME_KEY
from domo_core.texts.view import CatalogTexts
from domo_core.tools import policy_tools
from domo_core.tools.registry import TOOLS, InvalidArguments
from fakes import FakeChatModel
from web_support import audit_entries, in_language, reads_in

INVITE_STYLE = "confirmation_style.action.household.invite"


# --- the brief's tests ---------------------------------------------------------------------------


async def test_owner_sets_the_lifetime_then_changes_their_mind(owner_chat, current_settings):
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 24})
    )
    assert (await current_settings()).max_lifetime == timedelta(hours=24)
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": None})
    )
    assert (await current_settings()).max_lifetime == timedelta(hours=168)


async def test_a_rule_change_takes_what_the_judge_asks(owner_chat, current_settings):
    await owner_chat.say_with_tool(
        "policy_update", {"key": LIFETIME_KEY, "value": 24}, judge="explicit_yes: a big change"
    )
    owner_chat.use_model(FakeChatModel(["Dis « je confirme » ou utilise le bouton."]))
    await owner_chat.say("oui")
    assert (await current_settings()).max_lifetime == timedelta(hours=168)
    await owner_chat.say("je confirme")
    assert (await current_settings()).max_lifetime == timedelta(hours=24)


async def test_the_styles_of_before_are_no_settings(owner_chat, current_settings):
    for key in ("confirmation_style.confirm", INVITE_STYLE, "confirmation_style.action.chain.run"):
        reply = await owner_chat.say_with_tool("policy_update", {"key": key, "value": "natural"})
        assert reply.actions == [
            {"tool": "policy_update", "status": "invalid", "reason": "invalid"}
        ]
    assert (await current_settings()).owner_values == {}


async def test_only_the_owner_has_policy_tools(adult_chat):
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("salut")
    assert not {t.name for t in model.calls[0].tools} & {"policy_show", "policy_update"}


async def test_policy_show_tells_defaults_from_owner_choices(owner_chat, set_setting):
    await set_setting(LIFETIME_KEY, 24)
    reply = await owner_chat.say_with_tool("policy_show", {})
    assert (
        '"source": "owner"' in reply.model_result_text
        and '"source": "default"' in reply.model_result_text
    )


async def test_the_owner_is_no_longer_asked_how_to_confirm(owner_chat):
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    assert "Choices the owner has not made yet" not in model.calls[0].system
    assert "confirmation_style" not in model.calls[0].system


async def test_a_shorter_lifetime_applies_to_waiting_requests(adult_chat, set_setting, clock):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    clock.advance(timedelta(hours=2))
    await set_setting("confirmation_max_lifetime_hours", 1)
    r = await adult_chat.post(f"/confirmations/{reply.pending_confirmation_id}/approve")
    assert status_of(r) == "expired"


async def test_rule_changes_are_audited(owner_chat, last_audit_entry):
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool(
            "policy_update", {"key": "confirmation_max_lifetime_hours", "value": 48}
        )
    )
    entry = await last_audit_entry("policy.updated")
    assert entry.details == {"key": "confirmation_max_lifetime_hours", "old": None, "new": 48}


# --- what the owner is asked, and what they see --------------------------------------------------


def _plain(fragment: str) -> str:
    """What a person reads in a fragment."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


async def test_the_card_says_the_change_in_plain_words(owner_chat):
    asked = await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 48})
    sentence = "A request may wait 2 days at most, then it expires."
    assert sentence in _plain(asked.cards_html)
    # Never the key as the code spells it.
    assert LIFETIME_KEY not in _plain(asked.cards_html)
    card = await owner_chat.confirm_last(asked)
    assert card.status == "executed"
    assert sentence in _plain(card.html)
    back = await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": None})
    assert "Back to the default: a request may wait 7 days at most, then it expires." in _plain(
        back.cards_html
    )


async def test_the_page_says_the_change_in_the_readers_language(owner_chat, session_factory):
    await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 24})
    async with session_factory() as writer:
        await reads_in(writer, owner_chat.person, "fr")
        await writer.commit()
    page = await owner_chat.get("/chat")
    # Each part of the sentence in the reader's version: the duration too.
    one_day = in_language("fr", "policy.duration.day", count="1")
    assert in_language("fr", "policy.change.lifetime", duration=one_day) in _plain(page.text)


def _rule_change(key: str) -> DescribedAction:
    return DescribedAction("core", "policy.update", key, "permissions", "household")


@pytest.mark.parametrize(
    ("key", "value", "lang", "sentence"),
    [
        (
            "confirmation_max_lifetime_hours",
            48,
            "en",
            "A request may wait 2 days at most, then it expires.",
        ),
        (
            "confirmation_max_lifetime_hours",
            1,
            "en",
            "A request may wait 1 hour at most, then it expires.",
        ),
        (
            "confirmation_max_lifetime_hours",
            30,
            "en",
            "A request may wait 30 hours at most, then it expires.",
        ),
        (
            "confirmation_max_lifetime_hours",
            24,
            "en",
            "A request may wait 1 day at most, then it expires.",
        ),
        (
            "confirmation_max_lifetime_hours",
            None,
            "en",
            "Back to the default: a request may wait 7 days at most, then it expires.",
        ),
        (
            "household.time_zone",
            "Europe/Paris",
            "en",
            "The household's time zone will be Europe/Paris.",
        ),
        ("household.time_zone", None, "en", "The household will no longer have a time zone."),
        (
            "rights_writer.seconds",
            120,
            "en",
            "The rights writer will have 120 seconds per call.",
        ),
        (
            "rights_writer.seconds",
            None,
            "en",
            "Back to the default: the rights writer will have 60 seconds per call.",
        ),
        # The voice's settings, and the tools' base, are told by what they do (the owner's first
        # real try, 2026-11-27: the judge read "an action the assistant was asked to do" with no
        # value, and refused his own choice).
        (
            "speech.hear_outside",
            "yes",
            "en",
            "What the microphones hear will go to a model outside the house: the voice listens "
            "freely, and the sound, who speaks and the words said near it leave the house.",
        ),
        ("speech.hear_outside", "no", "en", "What the microphones hear will stay at home."),
        ("speech.hear_outside", None, "en", "What the microphones hear will stay at home."),
        (
            "voice.after_a_gesture",
            "yes",
            "en",
            "The voice will hear only what is said while a gesture is made, such as a held key; "
            "what is heard stays at home unless it may go outside the house.",
        ),
        ("voice.after_a_gesture", "no", "en", "The voice will not wait for a gesture to hear."),
        ("voice.after_a_gesture", None, "en", "The voice will not wait for a gesture to hear."),
        (
            "voice.ordinary_when_unsure",
            "judge",
            "en",
            "An ordinary thing asked by a voice the assistant is not sure of will be weighed "
            "first, like a sensitive one.",
        ),
        (
            "voice.ordinary_when_unsure",
            None,
            "en",
            "An ordinary thing asked by a voice the assistant is not sure of will be done with "
            "the rights of whoever probably speaks.",
        ),
        (
            "tools.base_mb",
            2048,
            "en",
            "The base the assistant's tools keep their data in may grow to 2048 MB.",
        ),
        (
            "tools.base_mb",
            None,
            "en",
            "The base the assistant's tools keep their data in may grow to 1024 MB.",
        ),
        (
            "speech.hear_outside",
            "yes",
            "fr",
            "Ce que les micros entendent ira vers un modèle hors de la maison : la voix écoute "
            "librement, et le son, qui parle et les mots dits près d'elle sortent de la maison.",
        ),
    ],
)
def test_each_rule_change_reads_as_a_plain_sentence(key, value, lang, sentence):
    # How it is said in each reader's language is the writer's: its meaning is this sentence.
    arguments = {"key": key, "value": value}
    assert describe_for_people(_rule_change(key), arguments, CatalogTexts(lang)) == sentence


@pytest.mark.parametrize(
    "arguments",
    [
        {"key": LIFETIME_KEY},
        {"value": 24},
        {"key": LIFETIME_KEY, "value": 24, "why": "x"},
        {"key": 3, "value": 24},
        {"key": LIFETIME_KEY, "value": [24]},
        {"key": LIFETIME_KEY, "value": 1.5},
        # A style of before is no setting.
        {"key": "confirmation_style.confirm", "value": "natural"},
    ],
)
def test_a_rule_change_that_cannot_be_read_gets_the_general_sentence(arguments):
    action = _rule_change(LIFETIME_KEY)
    assert describe_for_people(action, arguments, CatalogTexts("en")) == t("action.describe", "en")


# --- the tools, for the barrier ------------------------------------------------------------------


OWNER = Actor(person_id=uuid.uuid4(), role="owner")


async def test_a_rule_change_is_described_as_a_change_of_permissions():
    tool = TOOLS["policy_update"]
    for key, value in [
        ("confirmation_max_lifetime_hours", 720),
        ("rights_writer.seconds", None),
        ("household.time_zone", "Europe/Paris"),
    ]:
        described = await tool.describe({"key": key, "value": value}, OWNER, None)
        assert described == DescribedAction(
            "core", "policy.update", key, "permissions", "household"
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"key": "confirmation_style.confirm"},
        {"value": "natural"},
        {"key": "confirmation_style.confirm", "value": "natural", "why": "x"},
        {"key": None, "value": "natural"},
        {"key": 3, "value": "natural"},
        {"key": "something.else", "value": "natural"},
        {"key": "something.else", "value": None},
        {"key": "confirmation_style.confirm", "value": "natural"},
        {"key": "confirmation_style.confirm", "value": None},
        {"key": INVITE_STYLE, "value": "explicit"},
        {"key": "confirmation_max_lifetime_hours", "value": 0},
        {"key": "confirmation_max_lifetime_hours", "value": 721},
        {"key": "confirmation_max_lifetime_hours", "value": "48"},
        {"key": "confirmation_max_lifetime_hours", "value": 4.5},
        {"key": "confirmation_max_lifetime_hours", "value": True},
        # The rules of the keys are keys_house's (a right of its own, which can be given).
        {"key": "keys.invitee_default", "value": "own"},
        {"key": "keys.upkeep", "value": False},
        {"key": "keys.upkeep", "value": None},
    ],
)
async def test_a_rule_the_settings_refuse_is_invalid(arguments):
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS["policy_update"].describe(arguments, OWNER, None)
    assert refused.value.code == "invalid"
    # A stored request is checked again when it runs, before anything is touched.
    with pytest.raises(InvalidArguments):
        await TOOLS["policy_update"].execute(arguments, None)


async def test_showing_the_rules_reads_them_and_takes_no_arguments():
    tool = TOOLS["policy_show"]
    described = await tool.describe({}, OWNER, None)
    assert described == DescribedAction("core", "policy.show", None, "read", "household")
    with pytest.raises(InvalidArguments):
        await tool.describe({"key": LIFETIME_KEY}, OWNER, None)


def test_policy_show_says_everything_it_shows():
    """It shows the rights (and the paused ones), the settings and the household's time zone."""
    description = TOOLS["policy_show"].definition.description
    for words in ("rights", "paused", "time zone", "settings", "left", "everyone", "ask_when"):
        assert words in description, words
    assert description.index("rights") < description.index("settings")


def test_the_tools_say_which_rules_exist_and_that_the_yes_is_judged():
    description = TOOLS["policy_update"].definition.description
    for word in (
        "confirmation_max_lifetime_hours",
        "rights_writer.seconds",
        "household.time_zone",
        # The tools' own base: the owner sets how big it may grow (Task 12).
        "tools.base_mb",
        "from 16 to 1048576",
        "from 30 to 240",
        "null",
        "your judge decides it",
        "rights_propose",
    ):
        assert word in description, word
    for gone in ("confirmation_style", "natural", "explicit"):
        assert gone not in description, gone


def test_the_voice_settings_are_told_with_their_values():
    description = TOOLS["policy_update"].definition.description
    for word in ("speech.hear_outside", "voice.after_a_gesture", "held key"):
        assert word in description, word
    # Found from the owner's plain words: how the voice listens, holding something to talk with
    # everything kept at home, or letting what is heard leave the house.
    for words in ("how the voice listens", "while holding it", "stays at home", "leave the house"):
        assert words in description, words


def test_the_owners_choice_is_his_and_what_it_brings_is_said_once():
    # The owner's first real try (2026-11-27): his own choice of a voice setting was refused
    # twice. The tool says it is theirs, and its result says what it brings, to be said once.
    description = TOOLS["policy_update"].definition.description
    for words in ("Their choice is theirs: never refuse it yourself", "say once", "(means)"):
        assert words in description, words
    outside = {"key": "speech.hear_outside", "value": "yes"}
    assert policy_tools.change_means(outside) == describe_for_people(
        _rule_change("speech.hear_outside"), outside, CatalogTexts("en")
    )
    assert "leave the house" in policy_tools.change_means(outside)
    assert policy_tools.change_means({"key": LIFETIME_KEY, "value": 24}) == (
        "A request may wait 1 day at most, then it expires."
    )


async def test_only_the_owner_chooses_how_the_voice_listens(adult_chat, current_settings):
    reply = await adult_chat.say_with_tool(
        "policy_update", {"key": "voice.after_a_gesture", "value": "yes"}
    )
    assert reply.actions == [
        {"tool": "policy_update", "status": "refused", "reason": "not_allowed"}
    ]
    assert (await current_settings()).owner_values == {}


# --- through the chat ----------------------------------------------------------------------------


async def test_a_rule_change_is_judged_like_any_action(owner_chat, current_settings):
    reply = await owner_chat.say_with_tool(
        "policy_update", {"key": "confirmation_max_lifetime_hours", "value": 24}
    )
    assert reply.actions == [{"tool": "policy_update", "status": "waiting"}]
    assert json.loads(reply.model_result_text) == {
        "status": "waiting_for_confirmation",
        "by": "you",
        "asked_by": "judge",
        "answer_with": ANSWER_IN_WORDS,
    }
    assert (await current_settings()).max_lifetime == timedelta(hours=168)
    done = await owner_chat.say_with_tool(
        "policy_update", {"key": "confirmation_max_lifetime_hours", "value": 48}, judge="do"
    )
    assert done.actions == [{"tool": "policy_update", "status": "done"}]
    assert (await current_settings()).max_lifetime == timedelta(hours=48)


async def test_a_rule_change_the_settings_refuse_is_invalid_and_waits_for_nothing(
    owner_chat, current_settings
):
    reply = await owner_chat.say_with_tool(
        "policy_update", {"key": "confirmation_style.action.household.list", "value": "natural"}
    )
    assert reply.actions == [{"tool": "policy_update", "status": "invalid", "reason": "invalid"}]
    assert reply.pending_confirmation_id is None
    assert (await current_settings()).owner_values == {}


async def test_the_owners_no_changes_nothing(owner_chat, current_settings, chat_server):
    reply = await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 24})
    r = await owner_chat.post(f"/confirmations/{reply.pending_confirmation_id}/refuse")
    assert status_of(r) == "refused"
    assert (await current_settings()).owner_values == {}
    async with chat_server.session_factory() as reader:
        assert "policy.updated" not in [e.action for e in await audit_entries(reader)]


async def test_a_member_who_calls_a_policy_tool_anyway_is_refused(adult_chat, current_settings):
    for tool, args in [
        ("policy_update", {"key": LIFETIME_KEY, "value": 24}),
        ("policy_show", {}),
    ]:
        reply = await adult_chat.say_with_tool(tool, args)
        assert reply.actions == [{"tool": tool, "status": "refused", "reason": "not_allowed"}]
        assert reply.pending_confirmation_id is None
    assert (await current_settings()).owner_values == {}


async def test_policy_show_changes_nothing_and_gives_every_rule(owner_chat, set_setting):
    await set_setting("confirmation_max_lifetime_hours", 24)
    reply = await owner_chat.say_with_tool("policy_show", {})
    assert reply.actions == [{"tool": "policy_show", "status": "done"}]
    assert json.loads(reply.model_result_text) == {
        "status": "done",
        "settings": [
            {"key": "confirmation_max_lifetime_hours", "value": 24, "source": "owner"},
            {"key": "rights_writer.seconds", "value": 60, "source": "default"},
        ],
        "time_zone": None,
        "rights": [],
    }


async def test_policy_show_lists_the_rights_and_the_paused_ones(
    owner_chat, adult_member, grant, set_setting, session_factory, live_clock
):
    alex = adult_member.person
    await grant("note.leave", role="child", ask="yes", summary="Children's notes wait.")
    live_clock.advance(timedelta(seconds=1))  # oldest first
    await grant("household.invite", person=alex, access="allow", summary="Alex invites guests.")
    async with session_factory() as writer:
        await pause_personal_rights(writer, alex.id, live_clock())
        await writer.commit()
    live_clock.advance(timedelta(seconds=1))
    await grant("profile.rename", person=alex, access="deny", summary="Alex keeps his name.")
    live_clock.advance(timedelta(seconds=1))
    await grant("household.remove", ask="explicit_yes", summary="Ask me before removing anyone.")
    live_clock.advance(timedelta(seconds=1))
    await grant(
        "rights.apply",
        person=owner_chat.person,
        ask="judge",
        ask_when="only for guests",
        summary="Apply my rights without asking me, for guests.",
    )
    await set_setting("household.time_zone", "Europe/Paris")
    reply = await owner_chat.say_with_tool("policy_show", {})
    result = reply.model_results[-1]
    assert result["time_zone"] == "Europe/Paris"
    assert result["rights"] == [
        {
            "for": "child",
            "action": "note.leave",
            "ask": "yes",
            "ask_when": None,
            "summary": "Children's notes wait.",
            "paused": False,
        },
        {
            "for": "Alex",
            "action": "profile.rename",
            "ask": None,
            "ask_when": None,
            "summary": "Alex keeps his name.",
            "paused": False,
        },
        # A rule for everyone: what to ask before removing someone, whoever asks.
        {
            "for": "everyone",
            "action": "household.remove",
            "ask": "explicit_yes",
            "ask_when": None,
            "summary": "Ask me before removing anyone.",
            "paused": False,
        },
        # The owner lifted his own yes, with his conditions.
        {
            "for": "Owner",
            "action": "rights.apply",
            "ask": "judge",
            "ask_when": "only for guests",
            "summary": "Apply my rights without asking me, for guests.",
            "paused": False,
        },
        {
            "for": "Alex",
            "action": "household.invite",
            "ask": None,
            "ask_when": None,
            "summary": "Alex invites guests.",
            "paused": True,
        },
    ]


async def test_policy_show_says_which_rights_are_of_people_who_left(
    owner_chat, adult_member, child_member, grant
):  # spec 5.7: their rights are paused, and cannot be given back while they are away
    await grant("household.invite", person=adult_member.person, access="allow", summary="Alex.")
    await grant("note.leave", person=child_member.person, ask="yes", summary="Sam.")
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("household_remove", {"person": "Alex"})
    )
    result = (await owner_chat.say_with_tool("policy_show", {})).model_results[-1]
    sam, alex = result["rights"]  # (in force first, then the paused ones)
    assert (alex["for"], alex["paused"], alex["left_the_household"]) == ("Alex", True, True)
    # Said only of someone who left.
    assert (sam["for"], sam["paused"]) == ("Sam", False)
    assert "left_the_household" not in sam


async def test_a_rule_change_is_audited_after_the_owners_decision(owner_chat, chat_server):
    owner = owner_chat.person
    reply = await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 24})
    await owner_chat.confirm_last(reply)
    await owner_chat.confirm_last(
        await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 48})
    )
    async with chat_server.session_factory() as reader:
        entries = await audit_entries(reader)
    decisions = [
        e.details
        for e in entries
        if e.action == "policy.decision" and e.details["action_type"] == "policy.update"
    ]
    # R56: the barrier's words; the target is the key, never the value.
    assert decisions[0] == {
        "tool": "policy_update",
        "action_type": "policy.update",
        "target": LIFETIME_KEY,
        "decision": "allow",
        "reason": None,
        "facts": {},
    }
    updated = [e for e in entries if e.action == "policy.updated"]
    assert [e.details for e in updated] == [
        {"key": LIFETIME_KEY, "old": None, "new": 24},
        {"key": LIFETIME_KEY, "old": 24, "new": 48},
    ]
    assert {e.actor for e in updated} == {f"person:{owner.id}"}
    # The change is written when it runs: after the owner's yes, before the decision's record.
    actions = [e.action for e in entries]
    first_update = actions.index("policy.updated")
    assert actions[first_update + 1] == "confirmation.decided"


async def test_a_yes_with_a_change_to_a_rule_runs_the_changed_rule(owner_chat, current_settings):
    rid = str(
        (
            await owner_chat.say_with_tool("policy_update", {"key": LIFETIME_KEY, "value": 24})
        ).pending_confirmation_id
    )
    change = ToolCall(
        "c1",
        "confirmation_amend",
        json.dumps({"request_id": rid, "arguments": {"value": 48}, "ask_again": False}),
    )
    owner_chat.use_model(FakeChatModel(tool_script=["change", change, "C'est noté."], judge="do"))
    await owner_chat.say("oui, mais deux jours")
    assert (await current_settings()).max_lifetime == timedelta(hours=48)


async def test_policy_show_lists_domo_s_rights_under_his_own_heading(
    owner_chat, grant, session_factory, live_clock
):  # the owner, 2026-11-18: Domo is a subject of rights of his own
    await grant("note.leave", ask="yes", summary="Ask me before any note.")
    live_clock.advance(timedelta(seconds=1))  # oldest first
    async with session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="note.leave",
                access="deny",
                summary="The assistant leaves no note of his own.",
                owner_words="",
                created_by=owner_chat.person.id,
                created_at=live_clock(),
            )
        )
        await writer.commit()
    result = (await owner_chat.say_with_tool("policy_show", {})).model_results[-1]
    everyone, domo = result["rights"]
    # A rule for everyone is never shown as Domo's, nor Domo's as a rule for everyone.
    assert (everyone["for"], everyone["summary"]) == ("everyone", "Ask me before any note.")
    assert (domo["for"], domo["summary"]) == (
        t("rights.subject.domo", "en"),
        "The assistant leaves no note of his own.",
    )
    assert domo["paused"] is False and "left_the_household" not in domo


def test_the_heading_of_domo_s_rights_says_it_is_him():
    assert t("rights.subject.domo", "en") == "Me (the assistant)"
    assert t("rights.subject.domo", "fr") == "Moi (l'assistant)"
