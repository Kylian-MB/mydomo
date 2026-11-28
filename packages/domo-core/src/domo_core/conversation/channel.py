"""What a channel can do, and what the conversation needs from whoever listens."""

from dataclasses import dataclass
from typing import Final, Protocol

from domo_core.conversation.events import Event


@dataclass(frozen=True)
class Abilities:
    """What a channel can do. The conversation reads this, never the name of the channel."""

    buttons: bool  # can show a request with a button
    shows_once: bool  # can show something once, to the person alone
    safe_screen: bool  # can open a vault form
    hears_unasked: bool  # receives what Domo says on his own
    proof_kinds: frozenset[str]  # the kinds of proof the person can give here
    spoken: bool = False  # what Domo says is said aloud, not read


NOBODY_LISTENS: Final = Abilities(False, False, False, False, frozenset())


class ListenerGone(Exception):
    """Nobody can receive any more."""


class Listener(Protocol):
    """Whoever receives the events of a turn."""

    @property
    def reading(self) -> bool:
        """Whether someone is reading right now."""
        ...

    async def open(self) -> None:
        """Get ready to receive."""
        ...

    async def emit(self, event: Event) -> None:
        """Give one event. Raises ``ListenerGone`` when nobody can receive any more."""
        ...
