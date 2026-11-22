"""What a turn gives back, one event at a time, with no HTML anywhere."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from domo_core.chat.agent import ActionRecord

if TYPE_CHECKING:
    # Only for the type of ``Decided.outcome``: the confirmations' service runs its calls through
    # the conversation.
    from domo_core.confirmations.service import DecideOutcome

# Why a turn could not go on: no key Domo may use for the person (``no_key``), or the kind of
# failure of a model's call (``llm.types.ErrorKind``), which is also the catalog's
# ``chat.error.<reason>``.
FailureReason = Literal[
    "no_key",
    "auth",
    "upstream",
    "limit",
    "busy",
    "timeout",
    "unreachable",
    "rate_limited",
    "bad_request",
    "no_model",
    "limit_reached",
]


@dataclass(frozen=True)
class Words:
    """Some of what Domo says, as plain text with the marks already removed."""

    text: str


@dataclass(frozen=True)
class Acted:
    """An action of the turn. Its card, what is shown once and the request that waits for a yes
    are in the record."""

    record: ActionRecord


@dataclass(frozen=True)
class Decided:
    """A request the person's words just answered, what the decision came to (``outcome``), and
    the action that followed, if any."""

    confirmation_id: UUID
    record: ActionRecord | None
    outcome: "DecideOutcome"


@dataclass(frozen=True)
class Failed:
    """The turn could not go on. ``provider_id`` is the key the turn went on, when the person may
    give it again (they gave it, and it has a secret in the vault): a key the provider refuses
    (``auth``) can only be mended by them."""

    reason: FailureReason
    provider_id: str | None = None


@dataclass(frozen=True)
class Ended:
    """The turn is over."""

    status: Literal["complete", "error", "partial"]


Event = Words | Acted | Decided | Failed | Ended
