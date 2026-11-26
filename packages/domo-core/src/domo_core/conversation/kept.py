"""What a person writes while Domo answers them (spec 11.3): kept, never refused.

``Conversation.say`` stores such a message as any message of theirs (it is never lost, not even
across a restart of the core), and keeps here only that it came during the turn that runs
(``KeptMessages``), with what the channel said of it (``TurnRequest``). The turn that runs takes
them between two of its rounds of calls (``chat.agent.run_turn``, ``between_rounds``) and Domo
reads them there, as their words; those it had no round left to read are answered by a turn of
their own, once it is over (``Conversation.answer_kept``).

One process: what is kept is in its memory (``app.state.kept``), like the replies being written.
After a restart, a kept message is a message of theirs that no reply follows: Domo sees it in the
conversation of their next turn.
"""

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Only for the type of what is kept: the preparation of a turn imports this module.
    from domo_core.conversation.prepare import TurnRequest


class KeptMessages:
    """The messages people wrote while a turn of their conversation ran, by conversation, in the
    order they came; nobody's turn has read them yet."""

    def __init__(self) -> None:
        self._kept: dict[uuid.UUID, list[tuple[uuid.UUID, TurnRequest]]] = {}

    def keep(
        self, conversation_id: uuid.UUID, message_id: uuid.UUID, request: "TurnRequest"
    ) -> None:
        """The message ``message_id`` (stored) came in ``conversation_id`` while a turn ran there,
        through a channel that said ``request``."""
        self._kept.setdefault(conversation_id, []).append((message_id, request))

    def take(self, conversation_id: uuid.UUID) -> list[tuple[uuid.UUID, "TurnRequest"]]:
        """The messages kept for ``conversation_id``, oldest first, forgotten here."""
        return self._kept.pop(conversation_id, [])
