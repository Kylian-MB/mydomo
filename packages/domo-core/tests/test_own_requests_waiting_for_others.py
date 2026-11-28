"""A person's own requests that wait for someone else's yes (spec 5.2, C6).

A child's request waits for an adult, and a right of the owner may send a request of anyone to
the owner. The one who asked sees it in their own Domo's context, marked with whose yes it waits
for (``answered_by``), and described without the words of a note (D4: a waiting note reaches no
model). They may drop it (``confirmation_expire``), change it (``confirmation_amend``: with no
answer of theirs to read, nothing ever runs, it is always a new request for the same approver), or
nudge the approver (``confirmation_remind``): the request then comes first in the approver's zone
"To approve", and the approver's Domo is told. The words of the one who asked never answer such
a request.

The chat pilots talk with Domo on a live server (see ``chat_support.py``); what the model is
shown is read from ``chat_server.calls``.
"""

import json
import re
import uuid
from datetime import timedelta
from typing import Any

from chat_support import dump
from domo_core.confirmations import service
from domo_core.confirmations.describe import note_told_as
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.replan import notes_as_told
from domo_core.confirmations.store import (
    ANSWER_AS_APPROVER,
    ANSWER_BY_AN_ADULT,
    ANSWER_BY_THE_OWNER,
    ANSWER_IN_WORDS,
    awaiting_approval,
    create_confirmation,
    pending_for_others,
)
from domo_core.llm.types import ToolCall
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.rights import Approver
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings
from fakes import FakeChatModel
from sqlalchemy import update
from test_approver_in_words import (
    OTHERS_MARK,
    PENDING_MARK,
    block_of,
    calls_for,
    changed_by,
    plain,
    stored,
    tom_for,
)
from test_chain_plan import chain
from test_confirmation_tools import amend, ask_before
from test_judge_decides import judge_cases

NOTE = "Sam C rather, ask Sam: open to it"


def pending_of(chat_server: Any, name: str) -> list[dict[str, Any]]:
    """The pending requests the last turn of ``name`` was told of (none: an empty list)."""
    system = calls_for(chat_server, name)[-1].system
    return block_of(system, PENDING_MARK) if PENDING_MARK in system else []


def approval_items(zone: str) -> list[str]:
    """The items of the zone "To approve", in order, each as its HTML (its opening tag first)."""
    return re.split(r'(?=<li class="approval")', zone)[1:]


# --- seeing one's request, and dropping it -------------------------------------------------------


async def test_a_child_sees_their_request_that_waits_for_an_adult_and_drops_it(
    child_chat,
    guest_chat,
    an_adult_agrees_to_a_childs_rename,
    chat_server,
    confirmation_status,
    last_audit_entry,
):  # spec 8.8
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    # Nobody else is told of it as theirs (an adult sees it among the requests of others).
    await guest_chat.say("salut")
    assert pending_of(chat_server, "Robin") == []
    await child_chat.say("et ma demande ?")
    [told] = pending_of(chat_server, "Sam")
    assert (told["request_id"], told["answered_by"], told["answer_with"]) == (
        str(rid),
        "an adult",
        ANSWER_BY_AN_ADULT,
    )
    assert "Sam B" in told["description"]
    assert (told["nudged"], told["approver_note"]) == (False, None)
    dropped = await child_chat.say_with_tool("confirmation_expire", {"request_id": str(rid)})
    assert dropped.actions[0] == {"tool": "confirmation_expire", "status": "done"}
    assert await confirmation_status(rid) == "expired"
    entry = await last_audit_entry("confirmation.expired")
    assert entry.actor == f"person:{child_chat.person.id}"
    assert (entry.details["confirmation_id"], entry.details["expired_by"]) == (str(rid), "domo")
    # Once dropped, it is no longer told.
    await child_chat.say("et maintenant ?")
    assert pending_of(chat_server, "Sam") == []


async def test_a_childs_waiting_note_is_listed_to_them_without_its_words(
    child_chat, owner_member, grant, chat_server, confirmation_status
):  # F2
    words = "Zq7xW"
    await grant("note.leave", role="child", approver="adult")
    reply = await child_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": words})
    rid = reply.pending_confirmation_id
    assert await confirmation_status(rid) == "pending"
    await child_chat.say("et mon mot ?")
    [told] = pending_of(chat_server, "Sam")
    assert (told["request_id"], told["answered_by"]) == (str(rid), "an adult")
    assert told["description"] == "a note to Owner (5 characters)"
    made = calls_for(chat_server, "Sam")[-1]
    assert words not in made.system + json.dumps([dump(m) for m in made.messages])


async def test_a_waiting_note_given_back_as_it_was_told_keeps_its_words(
    child_chat, owner_member, adult_member, grant, session_factory
):
    """Sam's Domo gives the note back as he was told of it, to another recipient: the words
    stay Sam's; new words would be Sam's new words."""
    await grant("note.leave", role="child", approver="adult")
    reply = await child_chat.say_with_tool("note_leave", {"recipient": "Owner", "text": "Zq7xW"})
    rid = reply.pending_confirmation_id
    told = "a note to Owner (5 characters)"
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {
            "request_id": str(rid),
            "arguments": {"recipient": "Alex", "text": told},
            "ask_again": False,
        },
        text="plutôt pour Alex",
    )
    assert changed.model_results[0]["outcome"] == "asked_again"
    new = await stored(session_factory, changed.model_results[0]["request_id"])
    assert (new.approver_kind, new.arguments) == ("adult", {"recipient": "Alex", "text": "Zq7xW"})
    renewed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(new.id), "arguments": {"text": "Coucou"}, "ask_again": False},
        text="et écris plutôt Coucou",
    )
    newer = await stored(session_factory, renewed.model_results[0]["request_id"])
    assert newer.arguments == {"recipient": "Alex", "text": "Coucou"}


# --- nudging the approver ------------------------------------------------------------------------


async def test_a_reminder_on_such_a_request_nudges_the_approver(
    child_chat,
    adult_chat,
    live_adult_client,
    an_adult_agrees_to_a_childs_rename,
    chat_server,
    session_factory,
    confirmation_status,
):
    older = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    older_id = older.pending_confirmation_id
    child_chat.clock.advance(timedelta(seconds=1))
    younger = await child_chat.say_with_tool("profile_rename", {"name": "Sam C"})
    rid = younger.pending_confirmation_id
    assert rid is not None and rid != older_id
    nudge = await child_chat.say_with_tool(
        "confirmation_remind", {"request_id": str(rid)}, text="rappelle Alex qu'il doit dire oui"
    )
    assert nudge.actions[0] == {"tool": "confirmation_remind", "status": "done"}
    assert nudge.model_results[0] == {"status": "done", "reminders": 1, "nudged": True}
    assert nudge.pending_confirmation_id == rid  # its card comes back under the reply
    assert await confirmation_status(rid) == "pending"
    row = await stored(session_factory, rid)
    assert row.nudged_at is not None and row.reminder_count == 1
    # The words of the one who asked never answer it: what they were shown does not move.
    assert row.last_shown_message_id == younger.message_id
    assert (await stored(session_factory, older_id)).nudged_at is None
    # The approver's zone puts it first, and says so.
    zone = (await live_adult_client.get("/approvals")).text
    first, second = approval_items(zone)
    assert f'data-confirmation-id="{rid}"' in first
    assert f'data-confirmation-id="{older_id}"' in second
    assert "data-nudged" in first.split(">")[0] and "data-nudged" not in second.split(">")[0]
    assert "Nudged by the one who asked." in plain(first)
    assert "Nudged by the one who asked." not in plain(second)
    # The approver's Domo is told, the nudged one first.
    await adult_chat.say("salut")
    others = block_of(calls_for(chat_server, "Alex")[-1].system, OTHERS_MARK)
    assert [(o["request_id"], o["nudged"]) for o in others] == [
        (str(rid), True),
        (str(older_id), False),
    ]
    # Once he showed it to Alex, he is no longer told it was nudged (he would nag); the zone
    # keeps it first.
    await adult_chat.say_with_tool("confirmation_remind", {"request_id": str(rid)})
    adult_chat.use_model(FakeChatModel(tool_script=["unclear", "Je vois."]))
    await adult_chat.say("je regarde")
    others = block_of(calls_for(chat_server, "Alex")[-1].system, OTHERS_MARK)
    assert [(o["request_id"], o["nudged"]) for o in others] == [
        (str(rid), False),
        (str(older_id), False),
    ]
    [first, _] = approval_items((await live_adult_client.get("/approvals")).text)
    assert f'data-confirmation-id="{rid}"' in first and "data-nudged" in first.split(">")[0]
    # A new nudge tells him again.
    child_chat.clock.advance(timedelta(seconds=1))
    await child_chat.say_with_tool("confirmation_remind", {"request_id": str(rid)})
    adult_chat.use_model(FakeChatModel(["D'accord."]))
    await adult_chat.say("autre chose ?")
    others = block_of(calls_for(chat_server, "Alex")[-1].system, OTHERS_MARK)
    assert [(o["request_id"], o["nudged"]) for o in others][0] == (str(rid), True)
    # The one who asked is told it was nudged.
    await child_chat.say("merci")
    nudged = {
        request["request_id"]: request["nudged"] for request in pending_of(chat_server, "Sam")
    }
    assert nudged == {str(older_id): False, str(rid): True}


async def test_the_requesters_words_never_decide_such_a_request(
    child_chat, an_adult_agrees_to_a_childs_rename, confirmation_status, display_name_of
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    await child_chat.say_with_tool("confirmation_remind", {"request_id": str(rid)})
    model = child_chat.use_model(FakeChatModel(["Il faut l'accord d'un adulte."]))
    await child_chat.say("oui")
    # The main model answers; the classifier never reads the words.
    assert len(model.calls) == 1 and model.calls[0].tools
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(child_chat.person) == "Sam"


async def test_the_zone_orders_nudged_requests_first(
    session, add_person, add_account, add_conversation, clock
):
    adult = await add_person("Alex", "adult")
    await add_account(adult, "alex")  # whoever is asked has an account
    child = await add_person("Sam", "child")
    conversation = await add_conversation(child)
    made: list[Confirmation] = []
    for index in range(2):
        made.append(
            await create_confirmation(
                session,
                requester=Actor(child.id, "child", child=True),
                tool_name="profile_rename",
                arguments={"name": f"Sam {index}"},
                action=DescribedAction(
                    "core", "profile.rename", f"person:{child.id}", "household", "household"
                ),
                approver="adult",
                asked=Asked("yes", "judge"),
                conversation_id=conversation.id,
                message_id=None,
                now=clock() + timedelta(seconds=index),
            )
        )
    older, younger = made
    settings = await load_policy_settings(session)
    later = clock() + timedelta(minutes=1)
    listed, total = await awaiting_approval(session, adult, settings, later, limit=20)
    assert [c.id for c, _ in listed] == [older.id, younger.id] and total == 2
    younger.nudged_at = later
    await session.flush()
    listed, total = await awaiting_approval(session, adult, settings, later, limit=20)
    assert [c.id for c, _ in listed] == [younger.id, older.id] and total == 2
    # Only the first page is shown: the nudged one is on it.
    listed, total = await awaiting_approval(session, adult, settings, later, limit=1)
    assert [c.id for c, _ in listed] == [younger.id] and total == 2


async def test_only_the_requests_of_the_one_who_asked_that_wait_for_someone_else_are_theirs(
    session, add_person, add_conversation, clock
):
    child = await add_person("Sam", "child")
    other = await add_person("Lea", "child")
    conversation = await add_conversation(child)

    async def request(
        name: str, approver: Approver, *, age: timedelta = timedelta(0)
    ) -> Confirmation:
        return await create_confirmation(
            session,
            requester=Actor(child.id, "child", child=True),
            tool_name="profile_rename",
            arguments={"name": name},
            action=DescribedAction(
                "core", "profile.rename", f"person:{child.id}", "household", "household"
            ),
            approver=approver,
            asked=Asked("yes", "judge"),
            conversation_id=conversation.id,
            message_id=None,
            now=clock() - age,
        )

    for_an_adult = await request("Sam A", "adult", age=timedelta(seconds=1))  # the older
    for_the_owner = await request("Sam O", "owner")
    await request("Sam S", "self")  # theirs to answer: ``pending_for`` lists it
    await request("Sam X", "adult", age=timedelta(days=30))  # past its lifetime
    (await request("Sam R", "adult")).status = "refused"
    await session.flush()
    settings = await load_policy_settings(session)
    mine = await pending_for_others(session, child.id, conversation.id, settings, clock())
    assert [c.id for c in mine] == [for_an_adult.id, for_the_owner.id]
    # Someone else, in that very conversation, has none of them.
    assert await pending_for_others(session, other.id, conversation.id, settings, clock()) == []


# --- changing it ---------------------------------------------------------------------------------


async def test_a_change_by_the_requester_becomes_a_new_request_for_the_same_approver(
    child_chat,
    live_adult_client,
    an_adult_agrees_to_a_childs_rename,
    session_factory,
    confirmation_status,
    display_name_of,
    count_confirmations,
):  # F7
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    await child_chat.say_with_tool("confirmation_remind", {"request_id": str(rid)})
    # No answer of Sam's gave a change grant: there is none to give for such a request.
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam C"}, "ask_again": False},
        text="finalement, plutôt Sam C",
    )
    result = changed.model_results[0]
    assert (result["outcome"], result["answer_with"]) == ("asked_again", ANSWER_BY_AN_ADULT)
    new = uuid.UUID(result["request_id"])
    assert changed.pending_confirmation_id == new
    old = await stored(session_factory, rid)
    row = await stored(session_factory, new)
    assert (old.status, old.replaced_by) == ("replaced", new)
    assert (row.status, row.approver_kind, row.requester_id, row.conversation_id) == (
        "pending",
        "adult",
        child_chat.person.id,
        old.conversation_id,
    )
    assert row.arguments == {"name": "Sam C"}
    # A nudge is the old request's: the new one waits as any.
    assert old.nudged_at is not None and row.nudged_at is None
    assert row.approver_note is None
    # Nothing ran.
    assert await display_name_of(child_chat.person) == "Sam"
    assert await count_confirmations() == 2
    # The approver sees the new one, and that the one who asked changed it.
    zone = (await live_adult_client.get("/approvals")).text
    assert f'data-confirmation-id="{new}"' in zone and f'data-confirmation-id="{rid}"' not in zone
    assert changed_by("confirmation.changed.by_requester") in plain(zone)


async def test_a_change_that_changes_nothing_is_no_new_request(
    child_chat, an_adult_agrees_to_a_childs_rename, confirmation_status
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam B"}, "ask_again": False},
    )
    assert changed.actions[0]["status"] == "invalid"
    assert "the change changes nothing" in changed.model_result_text
    assert await confirmation_status(rid) == "pending"


async def test_only_the_one_who_asked_changes_it_without_an_answer(
    child_chat,
    adult_chat,
    an_adult_agrees_to_a_childs_rename,
    confirmation_status,
    count_confirmations,
):
    """The approver's change needs their answer to the request, shown to them (spec 5.1): the
    exception is the one who asked's, for their own request that waits for someone else."""
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    changed = await adult_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam C"}, "ask_again": False},
    )
    assert changed.actions[0]["status"] == "invalid"
    assert "only be changed right after the person answered it" in changed.model_result_text
    assert await confirmation_status(rid) == "pending"
    assert await count_confirmations() == 1


async def _sams_change_of_their_waiting_rename(
    child_chat: Any, judge: str, *, before: tuple[ToolCall, ...] = ()
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Sam's rename, which waits for an adult; Sam asks for "Sam C" instead, and Domo changes it
    (after ``before``, his own calls first, say ``ask_before_acting``); the judge says ``judge``
    of the change. The request, and what the change came to."""
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    assert rid is not None
    child_chat.use_model(
        FakeChatModel(
            tool_script=[*before, amend(str(rid), {"name": "Sam C"}), "Un adulte doit dire oui."],
            judge=judge,
        )
    )
    answer = await child_chat.say("finalement, plutôt Sam C")
    return rid, answer.model_results[-1]


async def test_the_judge_judges_a_change_that_waits_for_someone_else(
    child_chat, an_adult_agrees_to_a_childs_rename, chat_server, confirmation_ask, session_factory
):  # spec 4.1, 4.4: a changed action is judged as an action, and the verdict says how the adult
    # says yes
    _, result = await _sams_change_of_their_waiting_rename(child_chat, "proof: odd name")
    assert result["outcome"] == "asked_again"
    new = await stored(session_factory, result["request_id"])
    assert new.approver_kind == "adult"
    assert await confirmation_ask(new.id) == ("proof", "judge", "odd name")
    case = judge_cases(chat_server)[-1]
    assert case["rights"]["needs"] == "adult" and case["person"]["name"] == "Sam"
    assert case["request"]["after"] and case["request"]["yes"] is None
    # The judge lets it go: it waits for the adult's plain yes.
    _, plain_yes = await _sams_change_of_their_waiting_rename(child_chat, "do")
    assert await confirmation_ask(plain_yes["request_id"]) == ("yes", "approver", None)


async def test_domo_may_ask_more_on_a_change_that_waits_for_someone_else(
    child_chat, an_adult_agrees_to_a_childs_rename, confirmation_ask, session_factory
):  # I1: what Domo asks more is never lost on the way to the adult
    rid, result = await _sams_change_of_their_waiting_rename(
        child_chat, "do", before=(ask_before("explicit_yes", "a new name"),)
    )
    assert result["outcome"] == "asked_again"
    assert (result["asked_by"], result["why"]) == ("domo", "a new name")
    assert await confirmation_ask(result["request_id"]) == (
        "explicit_yes",
        "domo",
        "a new name",
    )
    assert (await stored(session_factory, rid)).status == "replaced"


async def test_a_change_the_judge_refuses_leaves_the_request_waiting_for_someone_else(
    child_chat,
    an_adult_agrees_to_a_childs_rename,
    confirmation_status,
    count_confirmations,
    display_name_of,
):  # spec 4.4: the changed version never reaches the adult; Sam gave no yes nor no to the first
    rid, result = await _sams_change_of_their_waiting_rename(child_chat, "do_not: not a name")
    assert (result["outcome"], result["reason"], result["why"]) == (
        "refused",
        "judge",
        "not a name",
    )
    assert await confirmation_status(rid) == "pending"
    assert await count_confirmations() == 1
    assert await display_name_of(child_chat.person) == "Sam"


async def test_a_change_whose_yes_is_now_the_persons_own_is_asked_again(
    child_chat,
    an_adult_agrees_to_a_childs_rename,
    grant,
    session_factory,
    chat_server,
    display_name_of,
):
    """A right gives Sam their own yes to renaming since they asked: their change waits for it."""
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    await grant("profile.rename", person=child_chat.person, approver="self")
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam C"}, "ask_again": False},
    )
    result = changed.model_results[0]
    assert (result["outcome"], result["answer_with"]) == ("asked_again", ANSWER_IN_WORDS)
    row = await stored(session_factory, result["request_id"])
    assert (row.status, row.approver_kind) == ("pending", "self")
    assert await display_name_of(child_chat.person) == "Sam"  # nothing ran
    await child_chat.say("et alors ?")
    [told] = pending_of(chat_server, "Sam")
    assert (told["request_id"], told["answered_by"]) == (result["request_id"], "the person")


async def test_a_change_whose_yes_is_now_the_owners_says_whose_it_is(
    child_chat, an_adult_agrees_to_a_childs_rename, grant, session_factory, display_name_of
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    await grant("profile.rename", person=child_chat.person, approver="owner")
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam C"}, "ask_again": False},
    )
    result = changed.model_results[0]
    assert (result["outcome"], result["answer_with"]) == ("approver_changed", ANSWER_BY_THE_OWNER)
    # Sam's yes never counted: the detail says only that it is now someone else's than before.
    assert "someone else's than before" in result["detail"]
    assert "no longer the person's" not in result["detail"]
    row = await stored(session_factory, result["request_id"])
    assert (row.status, row.approver_kind) == ("pending", "owner")
    assert await display_name_of(child_chat.person) == "Sam"


async def test_a_change_the_barrier_refuses_leaves_the_request_waiting(
    child_chat,
    an_adult_agrees_to_a_childs_rename,
    grant,
    session_factory,
    count_confirmations,
    audit_actions,
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    await grant(
        "profile.rename", person=child_chat.person, limit='"refuse"', summary="Sam keeps a name."
    )
    before = await audit_actions()
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam C"}, "ask_again": False},
    )
    result = changed.model_results[0]
    assert (result["outcome"], result["reason"]) == ("refused", "limit")
    # Sam gave it no yes and no no: it still waits for an adult, as it was.
    row = await stored(session_factory, rid)
    assert (row.status, row.arguments, row.decided_by, row.decided_via) == (
        "pending",
        {"name": "Sam B"},
        None,
        None,
    )
    assert await count_confirmations() == 1
    added = (await audit_actions())[len(before) :]
    assert "policy.decision" in added and "confirmation.decided" not in added


async def test_a_change_without_an_answer_leaves_the_grant_of_another_request(
    adult_chat,
    child_member,
    grant,
    session_factory,
    display_name_of,
):
    """ "Yes, but Alex C, and change my note to Sam": the change of the note, which waits for the
    owner and needs no answer, does not use up the grant the words gave to the rename."""
    await grant("note.leave", role="adult", approver="owner")
    note = await adult_chat.say_with_tool("note_leave", {"recipient": "Sam", "text": "Salut"})
    rename = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    note_id, rename_id = note.pending_confirmation_id, rename.pending_confirmation_id

    def change(call_id: str, rid: object, arguments: dict[str, Any]) -> ToolCall:
        return ToolCall(
            call_id,
            "confirmation_amend",
            json.dumps({"request_id": str(rid), "arguments": arguments, "ask_again": False}),
        )

    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                change("c1", note_id, {"text": "Bravo"}),
                change("c2", rename_id, {"name": "Alex C"}),
                "C'est fait.",
            ],
            judge="do",
        )
    )
    answer = await adult_chat.say("oui mais plutôt Alex C, et change mon mot pour Sam : Bravo")
    assert [result["outcome"] for result in answer.model_results] == ["asked_again", "executed"]
    assert await display_name_of(adult_chat.person) == "Alex C"
    new_note = await stored(session_factory, answer.model_results[0]["request_id"])
    assert (new_note.approver_kind, new_note.arguments) == (
        "owner",
        {"recipient": "Sam", "text": "Bravo"},
    )


async def test_a_request_rerouted_to_the_person_meanwhile_is_not_changed_without_an_answer(
    child_chat,
    an_adult_agrees_to_a_childs_rename,
    session_factory,
    monkeypatch,
    count_confirmations,
):
    """It waited for an adult when Domo read it, and waits for Sam's own yes when it is locked
    (a right changed between the two): a change without an answer is not taken."""
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    real = service.confirmations_by_id
    rerouted: list[bool] = []

    async def read_then_reroute(session: Any, ids: Any) -> Any:
        found = await real(session, ids)
        if not rerouted:
            rerouted.append(True)
            async with session_factory() as writer:
                await writer.execute(
                    update(Confirmation).where(Confirmation.id == rid).values(approver_kind="self")
                )
                await writer.commit()
        return found

    monkeypatch.setattr(service, "confirmations_by_id", read_then_reroute)
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Sam C"}, "ask_again": False},
    )
    assert rerouted and changed.actions[0] == {
        "tool": "confirmation_amend",
        "status": "invalid",
        "reason": "not_found",
    }
    row = await stored(session_factory, rid)
    assert (row.status, row.approver_kind, row.arguments) == ("pending", "self", {"name": "Sam B"})
    assert await count_confirmations() == 1


async def test_an_adults_request_that_waits_for_the_owner_is_seen_nudged_and_changed(
    adult_chat,
    live_owner_client,
    grant,
    chat_server,
    session_factory,
    display_name_of,
):
    await grant("profile.rename", role="adult", approver="owner")
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"})
    rid = reply.pending_confirmation_id
    await adult_chat.say_with_tool("confirmation_remind", {"request_id": str(rid)})
    [told] = pending_of(chat_server, "Alex")
    assert (told["request_id"], told["answered_by"], told["answer_with"], told["nudged"]) == (
        str(rid),
        "the owner",
        ANSWER_BY_THE_OWNER,
        False,  # the turn read it before its own call nudged it
    )
    [item] = approval_items((await live_owner_client.get("/approvals")).text)
    assert f'data-confirmation-id="{rid}"' in item and "data-nudged" in item.split(">")[0]
    changed = await adult_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"name": "Alex C"}, "ask_again": False},
    )
    result = changed.model_results[0]
    assert (result["outcome"], result["answer_with"]) == ("asked_again", ANSWER_BY_THE_OWNER)
    row = await stored(session_factory, result["request_id"])
    assert (row.status, row.approver_kind, row.arguments) == (
        "pending",
        "owner",
        {"name": "Alex C"},
    )
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_change_of_a_childs_chain_by_the_child_is_a_new_request_for_an_adult(
    child_chat,
    owner_member,
    adult_member,
    grant,
    session_factory,
    steps_rows,
    confirmation_status,
    count_invitations,
    chat_server,
):
    await grant("household.invite", person=child_chat.person, access="allow", approver="adult")
    reply = await child_chat.say_with_tool(
        "do_in_order",
        chain(
            ("household_invite", tom_for(7)), ("note_leave", {"recipient": "Owner", "text": "Hi"})
        ),
    )
    rid = reply.pending_confirmation_id
    assert (await stored(session_factory, rid)).approver_kind == "adult"
    # Sam's Domo is told the steps, the note without its words (D4: a waiting note).
    await child_chat.say("et ma demande ?")
    [told] = pending_of(chat_server, "Sam")
    assert told["answered_by"] == "an adult"
    assert told["steps"][1] == "2. a note to Owner (2 characters)"
    # He gives the chain back from what he was told: the note as he was told of it.
    steps = [
        {"tool": "household_invite", "arguments": tom_for(3)},
        {
            "tool": "note_leave",
            "arguments": {"recipient": "Owner", "text": told["steps"][1].removeprefix("2. ")},
        },
    ]
    changed = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(rid), "arguments": {"steps": steps}, "ask_again": False},
        text="plutôt 3 jours",
    )
    result = changed.model_results[0]
    assert result["outcome"] == "asked_again"
    new = uuid.UUID(result["request_id"])
    row = await stored(session_factory, new)
    assert (row.status, row.approver_kind, row.requester_id) == (
        "pending",
        "adult",
        child_chat.person.id,
    )
    assert await confirmation_status(rid) == "replaced"
    rows = await steps_rows(new)
    assert [(r.tool_name, r.status) for r in rows] == [
        ("household_invite", "planned"),
        ("note_leave", "planned"),
    ]
    assert rows[0].arguments["guest_days"] == 3
    # The note given back as it was told of is the note as Sam wrote it.
    assert rows[1].arguments == {"recipient": "Owner", "text": "Hi"}
    assert await count_invitations() == 0  # nothing ran
    # Sam may change the words of their own note: new words are theirs.
    steps[1] = {"tool": "note_leave", "arguments": {"recipient": "Owner", "text": "Coucou"}}
    again = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(new), "arguments": {"steps": steps}, "ask_again": False},
        text="et le mot dit plutôt Coucou",
    )
    assert again.model_results[0]["outcome"] == "asked_again"
    newer = uuid.UUID(again.model_results[0]["request_id"])
    assert (await steps_rows(newer))[1].arguments == {"recipient": "Owner", "text": "Coucou"}
    # "Send my note to Alex instead": the note given back as told, to Alex, keeps Sam's words.
    await child_chat.say("et ma demande ?")
    [told] = pending_of(chat_server, "Sam")
    steps[1] = {
        "tool": "note_leave",
        "arguments": {"recipient": "Alex", "text": told["steps"][1].removeprefix("2. ")},
    }
    moved = await child_chat.say_with_tool(
        "confirmation_amend",
        {"request_id": str(newer), "arguments": {"steps": steps}, "ask_again": False},
        text="envoie plutôt mon mot à Alex",
    )
    assert moved.model_results[0]["outcome"] == "asked_again"
    last = uuid.UUID(moved.model_results[0]["request_id"])
    assert (await steps_rows(last))[1].arguments == {"recipient": "Alex", "text": "Coucou"}


def note_step(position: int, recipient: str, text: str, after: str | None = None) -> Any:
    """A note step of a chain, as its row holds it."""
    return ConfirmationStep(
        confirmation_id=uuid.uuid4(),
        position=position,
        tool_name="note_leave",
        action_type="note.leave",
        arguments={"recipient": recipient, "text": text},
        status="planned",
        after_arrival_of=after,
    )


def test_a_note_given_back_as_told_keeps_its_words_whatever_its_recipient():
    waits = note_step(0, "Owner", "Hi", after="Paul")
    with_wait, without = sorted(note_told_as(waits), key=len, reverse=True)
    assert without == "a note to Owner (2 characters)" and with_wait.startswith(without + " ")
    for told in (with_wait, without):
        [call] = notes_as_told(
            [{"tool": "note_leave", "arguments": {"recipient": "Alex", "text": told}}],
            [waits],
            note_told_as,
        )
        assert call["arguments"] == {"recipient": "Alex", "text": "Hi"}
    # Each note by what it was told of, whomever it goes to now.
    rows = [note_step(0, "Owner", "Hi"), note_step(1, "Alex", "Yo")]
    told = "a note to Owner (2 characters)"
    alex = "a note to Alex (2 characters)"
    calls = notes_as_told(
        [
            {"tool": "note_leave", "arguments": {"recipient": "Alex", "text": alex}},
            {"tool": "note_leave", "arguments": {"recipient": "Robin", "text": told}},
        ],
        rows,
        note_told_as,
    )
    assert [call["arguments"] for call in calls] == [
        {"recipient": "Alex", "text": "Yo"},
        {"recipient": "Robin", "text": "Hi"},
    ]
    # New words are the author's own; a note told of twice is taken once.
    calls = notes_as_told(
        [
            {"tool": "note_leave", "arguments": {"recipient": "Owner", "text": "Coucou"}},
            {"tool": "note_leave", "arguments": {"recipient": "Owner", "text": told}},
            {"tool": "note_leave", "arguments": {"recipient": "Owner", "text": told}},
        ],
        rows[:1],
        note_told_as,
    )
    assert [call["arguments"]["text"] for call in calls] == ["Coucou", "Hi", told]


def test_a_note_given_back_as_its_numbered_line_keeps_its_words():
    # The tool tells Domo to give a step back "exactly as you were told of it": the line has
    # its number, and "(done) " before a step done (``describe.steps_told_of``).
    rows = [note_step(0, "Owner", "Hi"), note_step(1, "Alex", "Yo")]
    rows[0].status = "done"
    lines = [
        "1. (done) a note to Owner (2 characters)",
        "2. a note to Alex (2 characters)",
    ]
    calls = notes_as_told(
        [
            {"tool": "note_leave", "arguments": {"recipient": recipient, "text": line}}
            for recipient, line in zip(("Owner", "Alex"), lines, strict=True)
        ],
        rows,
        note_told_as,
    )
    assert [call["arguments"]["text"] for call in calls] == ["Hi", "Yo"]
    # Words of the person's own that merely start with a number are theirs, as given.
    [call] = notes_as_told(
        [{"tool": "note_leave", "arguments": {"recipient": "Owner", "text": "2. buy bread"}}],
        rows[:1],
        note_told_as,
    )
    assert call["arguments"]["text"] == "2. buy bread"


# --- what the approver said ----------------------------------------------------------------------


async def test_the_approvers_note_reaches_the_requesters_context(
    child_chat, adult_chat, an_adult_agrees_to_a_childs_rename, chat_server, session_factory
):
    reply = await child_chat.say_with_tool("profile_rename", {"name": "Sam B"})
    rid = reply.pending_confirmation_id
    await adult_chat.say_with_tool("confirmation_remind", {"request_id": str(rid)})
    adult_chat.use_model(
        FakeChatModel(
            tool_script=[
                "change",
                amend(str(rid), {"name": "Sam C"}, ask_again=True, note=NOTE),
                "Je le redemande à Sam.",
            ]
        )
    )
    answer = await adult_chat.say("oui mais plutôt Sam C, si Sam veut")
    result = answer.model_results[0]
    assert (result["outcome"], result["answer_with"]) == ("asked_again", ANSWER_AS_APPROVER)
    new = result["request_id"]
    await chat_server.wait_for_wakes()
    child_chat.use_model(FakeChatModel(["Alex propose Sam C."]))
    await child_chat.say("alors ?")
    [told] = pending_of(chat_server, "Sam")
    assert (told["request_id"], told["answered_by"], told["approver_note"]) == (
        new,
        "an adult",
        NOTE,
    )
    assert (await stored(session_factory, new)).approver_kind == "adult"
