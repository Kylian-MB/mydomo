"""Conversations and their messages."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class Conversation(Base):
    """A person's conversation with Domo.

    ``reviewed_upto_at`` is the time of the last message Domo read when he last went back over
    the conversation (``memory.review``): the messages after it are still to go over. ``None``: he
    never did."""

    __tablename__ = "conversations"
    __table_args__ = (
        # "The latest conversation of this person".
        Index("ix_conversations_person_id_created_at", "person_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    reviewed_upto_at: Mapped[datetime | None]


class Message(Base):
    """One message of a conversation.

    ``role`` is who speaks in the model's terms (``user`` or ``assistant``); ``provenance`` is
    where the text comes from: ``person`` for what a human typed, ``model`` for what Domo
    answered. ``status`` follows an assistant reply while it streams (``streaming``), then
    settles: ``complete``, ``partial`` (the reader left) or ``error``. ``own_answer`` says, of a
    reply, that it is Domo's own answer to a message of the person, written in a turn that was
    given nothing from outside (no note someone left them, no request of someone else, no word of
    an approver: ``ToolContext.answers_them``). The yes-judge reads Domo's last reply either
    way, and is told when it is not his own answer (``relayed``, ``confirmations.judge``); the
    background review of memories reads it too (``memory.review``). It is false for any other
    message, and for a reply the code cannot tell that of.
    """

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_values"),
        CheckConstraint(
            "status IN ('streaming', 'complete', 'partial', 'error')", name="status_values"
        ),
        CheckConstraint("provenance IN ('person', 'model')", name="provenance_values"),
        # A conversation's messages, in order.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str]
    # Empty until the first chunk of a streaming reply arrives.
    content: Mapped[str] = mapped_column(server_default=text("''"))
    status: Mapped[str]
    provenance: Mapped[str]
    # The model that wrote an assistant message; null for the messages people wrote.
    model: Mapped[str | None]
    # Why a reply ended in ``error`` (``None`` for any other): the kind of the failure, ``no_key``,
    # or ``taken_back`` for a wake's reply whose turn could not be written
    # (``conversation.visits``).
    failure: Mapped[str | None]
    own_answer: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), onupdate=func.clock_timestamp()
    )


class MessageAction(Base):
    """What Domo did, or was stopped from doing, while writing an assistant message.

    One row per tool call of the reply: ``status`` is ``done``, ``waiting`` (for a confirmation,
    in ``confirmation_id``), ``refused`` by the barrier, ``invalid`` (the arguments were
    rejected) or ``failed``. ``summary`` is what the chat page shows as the action's card: it
    holds no secret, token or link. ``action_type`` is empty when the model called a name that
    is no tool's. ``by_domo``: the call was judged as Domo's own act (making his tools, or any
    call of a turn he acts in on his own, Task 9): it is in his history, never in the person's
    whose conversation it is (``policy.facts``).
    """

    __tablename__ = "message_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('done', 'waiting', 'refused', 'invalid', 'failed')", name="status_values"
        ),
        # "The actions of these replies" (the page, the history sent to the model).
        Index("ix_message_actions_message_id", "message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    tool_name: Mapped[str]
    action_type: Mapped[str]
    status: Mapped[str]
    confirmation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("confirmations.id"))
    summary: Mapped[dict[str, object]] = mapped_column(JSONB)
    by_domo: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
