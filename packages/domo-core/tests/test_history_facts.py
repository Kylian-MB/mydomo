"""The history the yes-judge reads tells a refusal from a silence, and counts the requests the
assistant made in his own name: refused ones count as refused, and one that never ran
is not done."""

from datetime import UTC, datetime

from domo_core.confirmations.models import Confirmation
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, record_decision
from domo_core.policy.facts import domo_judge_history, person_history
from domo_core.policy.roles import domo_actor

INVITE = "household.invite"
NOTE = DescribedAction("core", "note.leave", None, "message", "household")


def _request(person, conversation_id, action_type: str, status: str, **values) -> Confirmation:
    return Confirmation(
        requester_id=person.id,
        approver_kind="self",
        conversation_id=conversation_id,
        tool_name=action_type.replace(".", "_"),
        arguments={},
        action={"action_type": action_type},
        ask="yes",
        status=status,
        **values,
    )


async def test_a_refusal_and_an_expiry_are_counted_apart(session, add_person, add_conversation):
    dana = await add_person("Dana", "adult")
    conversation = await add_conversation(dana)
    session.add_all(
        [
            _request(dana, conversation.id, INVITE, "refused"),
            _request(dana, conversation.id, INVITE, "expired"),
        ]
    )
    await session.flush()
    history = await person_history(session, dana.id, INVITE, datetime.now(UTC))
    assert (history.refused, history.expired) == (1, 1)


async def test_the_owner_s_refusals_of_his_own_requests_count(
    session, add_person, add_conversation
):
    alex = await add_person("Alex", "owner")
    conversation = await add_conversation(alex)
    session.add_all(
        [_request(alex, conversation.id, "note.leave", "refused", by_domo=True) for _ in range(3)]
    )
    # A refusal of the person's own request is not his.
    session.add(_request(alex, conversation.id, "note.leave", "refused"))
    await session.flush()
    judged = await domo_judge_history(session, "note.leave", datetime.now(UTC))
    assert (judged.refused, judged.expired) == (3, 0)


async def test_a_request_of_his_that_never_ran_is_not_done(session, add_person, add_conversation):
    alex = await add_person("Alex", "owner")
    conversation = await add_conversation(alex)
    # The barrier let it through to ask for a yes: its ``allow`` is written when it is made.
    await record_decision(session, domo_actor(), NOTE, Allow(), tool_name="note_leave")
    session.add(
        _request(
            alex, conversation.id, "note.leave", "refused", by_domo=True, counted_for_domo=True
        )
    )
    await session.flush()
    judged = await domo_judge_history(session, "note.leave", datetime.now(UTC))
    assert (judged.ever, judged.month, judged.refused) == (0, 0, 1)
    assert judged.last_at is None
