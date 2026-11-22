"""Reading and writing conversations: the store the chat pages work with.

The store only runs queries on the session it is given; the caller owns the transaction and
commits. A reply that streams is a message whose ``status`` is ``streaming`` until the turn ends
and ``finish_message`` settles it, or ``drop_reply`` takes it away (a wake of Domo in which he
had nothing to say). What Domo did while writing a reply is stored with it, one
``MessageAction`` per call of the model (``add_action``, ``actions_of``).
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.text import without_marks

# How many messages ``recent_messages`` returns unless told otherwise.
DEFAULT_RECENT_LIMIT = 200


class ConversationStore:
    """The conversations of the people and their messages, on one database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_conversation(self, person_id: uuid.UUID) -> Conversation | None:
        """The most recent conversation of the person, if they have one. Creates nothing."""
        result = await self._session.execute(
            select(Conversation)
            .where(Conversation.person_id == person_id)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def active_conversation(self, person_id: uuid.UUID) -> Conversation:
        """The person's most recent conversation; the first one is created when there is none.

        Two requests that both find none do not create two: the person's row is locked (until the
        caller's transaction ends) while the first one is created.
        """
        found = await self.latest_conversation(person_id)
        if found is not None:
            return found
        await self._session.execute(
            select(Person.id).where(Person.id == person_id).with_for_update()
        )
        found = await self.latest_conversation(person_id)
        if found is not None:
            return found
        conversation = Conversation(person_id=person_id)
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def lock_conversation(self, conversation_id: uuid.UUID) -> None:
        """Wait for the other transactions that work on the conversation, and keep them waiting
        until this one ends: what is checked next (is a reply streaming?) stays true until the
        message that depends on it is added."""
        await self._session.execute(
            select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
        )

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        *,
        role: str,
        content: str,
        status: str,
        provenance: str,
        model: str | None = None,
    ) -> Message:
        """Add a message to the conversation. ``role`` is ``user`` or ``assistant``; ``provenance``
        says where the text comes from (``person`` or ``model``); ``model`` names the model that
        wrote an assistant message."""
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            status=status,
            provenance=provenance,
            model=model,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def finish_message(
        self,
        message_id: uuid.UUID,
        *,
        content: str,
        status: str,
        model: str | None = None,
        failure: str | None = None,
        own_answer: bool = False,
    ) -> bool:
        """Settle a reply that was streaming: store its text and its final ``status``, the
        ``model`` that wrote it when one did (it is chosen call by call, so only known at the end),
        and, for a reply that ends in ``error``, why (``failure``, when it is known).
        ``own_answer`` says that it is Domo's own answer to a message of the person, in a turn
        that was given nothing from outside (``Message.own_answer``).

        Only a message that is still ``streaming`` is changed. A reply that someone else already
        settled (the conversation marked it as failed because it took too long, say) stays as it
        is, and a reply is settled once. Returns whether this call settled it: the caller that
        gets ``False`` must not report the turn as its own.

        The text is kept without the markdown marks a model may write (``without_marks``): it is
        what people read, and what Domo reads of himself in the next turns.
        """
        values: dict[str, str | bool] = {
            "content": without_marks(content),
            "status": status,
            "own_answer": own_answer,
        }
        if model is not None:
            values["model"] = model
        if failure is not None:
            values["failure"] = failure
        result = await self._session.execute(
            update(Message)
            .where(Message.id == message_id, Message.status == "streaming")
            .values(**values)
            .returning(Message.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def drop_reply(self, message_id: uuid.UUID) -> bool:
        """Take away a reply that is still ``streaming``, as if it had never been opened: a turn
        that had nothing to say, and may stay silent, leaves nothing in the conversation. A reply
        that someone else settled meanwhile stays. Returns whether this call took it away: as for
        ``finish_message``, the caller that gets ``False`` must not report the turn as its own."""
        result = await self._session.execute(
            delete(Message)
            .where(Message.id == message_id, Message.status == "streaming")
            .returning(Message.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def messages_after(
        self, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> list[Message]:
        """The messages of the conversation made after the message ``message_id`` (of any
        conversation), oldest first; at most ``DEFAULT_RECENT_LIMIT``, the oldest of them."""
        since = select(Message.created_at).where(Message.id == message_id).scalar_subquery()
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.created_at > since)
            .order_by(Message.created_at)
            .limit(DEFAULT_RECENT_LIMIT)
        )
        return list(result.scalars().all())

    async def recent_messages(
        self, conversation_id: uuid.UUID, limit: int = DEFAULT_RECENT_LIMIT
    ) -> list[Message]:
        """The latest ``limit`` messages of the conversation, oldest first."""
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        newest_first: Sequence[Message] = result.scalars().all()
        return list(reversed(newest_first))

    async def streaming_message(self, conversation_id: uuid.UUID) -> Message | None:
        """The reply of the conversation that is still streaming, if any."""
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.status == "streaming")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def message_of(self, person_id: uuid.UUID, message_id: uuid.UUID) -> Message | None:
        """The message, if it is in a conversation of this person (nobody else's)."""
        result = await self._session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id == message_id, Conversation.person_id == person_id)
        )
        return result.scalar_one_or_none()

    async def add_action(
        self,
        message_id: uuid.UUID,
        *,
        tool_name: str,
        action_type: str,
        status: str,
        summary: Mapping[str, object],
        confirmation_id: uuid.UUID | None = None,
        by_domo: bool = False,
    ) -> MessageAction:
        """Record an action of the reply ``message_id``. ``summary`` is its card: what the page
        shows, never a secret, a token or a link. ``by_domo``: it was judged as Domo's own
        act (``MessageAction.by_domo``)."""
        action = MessageAction(
            message_id=message_id,
            tool_name=tool_name,
            action_type=action_type,
            status=status,
            confirmation_id=confirmation_id,
            summary=dict(summary),
            by_domo=by_domo,
        )
        self._session.add(action)
        await self._session.flush()
        return action

    async def actions_of(
        self, message_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[MessageAction]]:
        """The actions of each of these messages, in the order they were taken (messages without
        any are left out)."""
        ids = list(message_ids)
        if not ids:
            return {}
        result = await self._session.execute(
            select(MessageAction)
            .where(MessageAction.message_id.in_(ids))
            .order_by(MessageAction.created_at, MessageAction.id)
        )
        actions: dict[uuid.UUID, list[MessageAction]] = {}
        for action in result.scalars():
            actions.setdefault(action.message_id, []).append(action)
        return actions
