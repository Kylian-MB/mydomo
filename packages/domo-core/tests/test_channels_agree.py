"""The two channels agree (spec 7.4, task 8): the same scenarios, played once on the page and once
on the API, keep the same conversation: the same messages and the same actions, identifiers and
dates aside.

The page is Alex's (``adult_chat``); the API is the laptop of Dana, another adult
(``api_support.ApiPilot``), on the same live server and the same model slot.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from api_support import ApiPilot
from chat_support import ChatPilot, ChatServer, Member
from domo_core.accounts.models import Person
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.llm.types import ChatModel
from fakes import FakeChatModel
from sqlalchemy import select


class Speaking(Protocol):
    """What both pilots do."""

    def use_model[M: ChatModel](self, model: M) -> M: ...

    async def say(self, text: str) -> Any: ...

    async def say_with_tool(
        self, name: str, args: dict[str, Any], *, text: str | None = None, judge: str = ...
    ) -> Any: ...


async def words(pilot: Speaking) -> None:
    pilot.use_model(FakeChatModel(["Hello", " there."]))
    await pilot.say("Hello")


async def a_tool(pilot: Speaking) -> None:
    await pilot.say_with_tool(
        "note_leave", {"recipient": "Owner", "text": "Dinner at eight"}, judge="do"
    )


async def a_yes_in_words(pilot: Speaking) -> None:
    await pilot.say_with_tool("note_leave", {"recipient": "Owner", "text": "Back at six"})
    pilot.use_model(FakeChatModel(["Left it."]))
    await pilot.say("yes")


SCENARIOS: tuple[Callable[[Speaking], Awaitable[None]], ...] = (words, a_tool, a_yes_in_words)


async def kept(server: ChatServer, person: Person) -> dict[str, list[Any]]:
    """What the person's conversation keeps, in order, identifiers and dates aside."""
    async with server.session_factory() as reader:
        messages = (
            (
                await reader.execute(
                    select(Message)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(Conversation.person_id == person.id)
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )
        ids = [m.id for m in messages]
        actions = (
            (
                await reader.execute(
                    select(MessageAction)
                    .where(MessageAction.message_id.in_(ids))
                    .order_by(MessageAction.created_at, MessageAction.id)
                )
            )
            .scalars()
            .all()
        )
        requests = (
            (
                await reader.execute(
                    select(Confirmation)
                    .where(Confirmation.requester_id == person.id)
                    .order_by(Confirmation.created_at)
                )
            )
            .scalars()
            .all()
        )
    position = {message_id: index for index, message_id in enumerate(ids)}
    return {
        "messages": [
            (m.role, m.content, m.status, m.provenance, m.model, m.failure) for m in messages
        ],
        "actions": [
            (
                position[a.message_id],
                a.tool_name,
                a.action_type,
                a.status,
                a.summary,
                a.confirmation_id is not None,
            )
            for a in actions
        ],
        "requests": [
            (c.tool_name, c.arguments, c.status, c.decided_via, c.ask, c.approver_kind, c.result)
            for c in requests
        ],
    }


async def test_the_same_scenario_keeps_the_same_conversation_on_both_channels(
    adult_chat: ChatPilot,
    chat_server: ChatServer,
    add_member: Callable[..., Awaitable[Person]],
):
    dana = Member(await add_member("Dana", "adult", username="dana"), "dana")
    async with ApiPilot.paired(chat_server, dana) as laptop:
        for scenario in SCENARIOS:
            await scenario(adult_chat)
            await scenario(laptop)
    on_the_page = await kept(chat_server, adult_chat.person)
    on_the_api = await kept(chat_server, dana.person)
    # Words, a tool, then a request and the yes that answers it: two messages each, four for the
    # last one.
    assert len(on_the_page["messages"]) == 8
    assert {action[1] for action in on_the_page["actions"]} == {"note_leave"}
    assert [request[2] for request in on_the_page["requests"]] == ["executed"]
    assert on_the_page == on_the_api
