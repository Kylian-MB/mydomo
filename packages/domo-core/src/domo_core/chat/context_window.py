"""How much of the conversation the model is shown.

The model only sees a window of the conversation: the most recent messages that fit a budget of
characters (``max_history_chars``). Characters stand in for tokens, which only the provider can
count: the budget is a safe approximation, not a promise about the provider's own limit.
"""

from collections.abc import Sequence

from domo_core.llm.types import ChatMessage


def merge_consecutive_roles(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """``messages`` with each run of messages from the same side made into one message, whose
    text is theirs joined by a blank line.

    A conversation sent to a model has to alternate: strict chat templates refuse two messages
    in a row from one side, and a reply that failed (and so is not sent back) leaves two
    questions next to each other that would make every later turn fail too.
    """
    merged: list[ChatMessage] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            merged[-1] = ChatMessage(message.role, f"{merged[-1].content}\n\n{message.content}")
        else:
            merged.append(message)
    return merged


def select_history(messages: Sequence[ChatMessage], *, max_chars: int) -> list[ChatMessage]:
    """The window of ``messages`` (oldest first) to send to the model.

    Starting from the most recent message, messages are added for as long as the total number of
    characters of their content stays within ``max_chars``. A most recent message that exceeds the
    budget by itself is kept, cut down to its last ``max_chars`` characters (the end of a long
    text is what the question is about), and nothing else is added. The window is in
    chronological order, and it never starts with an assistant message: a conversation begins
    with the person, and a reply whose question was left out says nothing to the model.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    window: list[ChatMessage] = []
    used = 0
    for position, message in enumerate(reversed(messages)):
        size = len(message.content)
        if position == 0 and size > max_chars:
            window.append(ChatMessage(message.role, message.content[-max_chars:]))
            break
        if used + size > max_chars:
            break
        window.append(message)
        used += size
    window.reverse()
    while window and window[0].role == "assistant":
        window.pop(0)
    return window
