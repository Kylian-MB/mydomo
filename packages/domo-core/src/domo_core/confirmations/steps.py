"""The steps of a chain of actions, on the caller's session (the caller commits).

A chain (``tools.chain.DoInOrder``) that waits for a yes is one request (``confirmations``) and its
steps, in order (``ConfirmationStep``): each one the call as Domo put it in the chain, its type,
the action its tool described when the chain was asked for, or none for a step checked in its turn
(``PlannedStep.action`` is ``None``). The request and its steps are stored in one transaction
(``conversation.execution``). After the yes, each step is marked with what came of it
(``mark_step``, by ``confirmations.chain.run_steps``);
a step checked in its turn that was done gets, on its row, the action it was described as in its
turn. A step still to run that a chain taken up again copied from a step that failed and may have
acted carries ``MAYBE_DONE`` in its result (``maybe_done``): its card says so. A step may wait for
someone's arrival (``PlannedStep.after_arrival_of``, the name of an invitation, spec 4.3b): the
chain pauses before it until that invitation is accepted (``confirmations.chain``).

A chain is judged as a whole when it is asked for (``conversation.execution``, spec 4.2): what its
request asks for is the request's (``Confirmation.ask``), not its steps'.

A step done that made something only the one who asked may see (an invitation's link, the form for
the model's key) never shows it at once (``ToolContext.in_a_chain``): its card, on its row, names
the invitation or the provider (``"invitation"``, ``"rekey"``), and the card of the chain has a
button for it, for the one who asked (``StepButton``, ``button_of``, spec 4.5).

These rows are the source of truth for the steps of a chain (their order, tool, arguments and
state): the request's own ``arguments`` keep the call of ``do_in_order``, its steps flattened (a
chain inside it in its place), for the record only.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.confirmations.models import ConfirmationStep
from domo_core.llm.types import JsonObject
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, NeverAsk

# What became of a step: it waits its turn (``planned``), it was done or it failed, or the chain
# stopped before it (``stopped``).
StepStatus = Literal["planned", "done", "failed", "stopped"]

# The key set in the ``result`` of a step still to run, copied into a new request from a step that
# failed and may already have acted (``tools.chain.ChainContinue``, spec 4.4): its card says it may
# already have been done. Running it writes its own result over it.
MAYBE_DONE: Final = "maybe_done"


@dataclass(frozen=True)
class PlannedStep:
    """A step of a chain, as it was planned when the chain was asked for: the call (``tool_name``
    and ``arguments``), its ``action_type``, the ``action`` its tool described then (``None`` when
    it is checked in its turn: an earlier step changes whom it acts on). ``with_basis``: the step
    is judged with what the chain remembers of the one who asked (it is as they asked for it).
    ``after_arrival_of``: the name of the invitation whose arrival the step waits for, if it waits
    for one. ``never_ask``: the owner's "never ask me" for it, as the rights said when it was
    planned (``Resolved.never_ask``), for the judge; ``rule``: what the owner's rule on it asks
    (``Resolved.ask``), for what a chain changed or taken up again asks
    (``confirmations.judge.ask_after_a_change``). Neither is stored: a chain changed or taken up
    again is planned again."""

    tool_name: str
    action_type: str
    arguments: JsonObject
    action: DescribedAction | None
    with_basis: bool = True
    after_arrival_of: str | None = None
    never_ask: NeverAsk | None = None
    rule: Ask | None = None


@dataclass(frozen=True)
class StepButton:
    """What a step done left for the one who asked, behind a button on the card of its chain: the
    ``step`` (from 1), and the invitation whose link the button shows (``invitation``, its id), the
    provider whose key's form it opens (``rekey``, its id), the form of a new key
    (``key_new``), or the provider whose admin key's form it opens (``admin_key``, its id).
    Identifiers only: never a link, a token or a form."""

    step: int
    invitation: str | None
    rekey: str | None
    key_new: bool = False
    admin_key: str | None = None

    def as_json(self) -> JsonObject:
        """As a chain's card carries it (``{"step", "invitation", "rekey", "key_new",
        "admin_key"}``)."""
        return {
            "step": self.step,
            "invitation": self.invitation,
            "rekey": self.rekey,
            "key_new": self.key_new,
            "admin_key": self.admin_key,
        }


def button_of(position: int, card: Mapping[str, object]) -> StepButton | None:
    """The button the step at ``position`` (from 0) left with its ``card``, if it names an
    invitation (``"invitation"``), the provider of a key (``"rekey"``) or of an admin key
    (``"admin_key"``), or opens the form of a new key (``"key_new"``); ``None`` otherwise. What a
    page may post to is checked again when the card is shown
    (``channels.page.cards.card_buttons``)."""
    invitation = card.get("invitation")
    rekey = card.get("rekey")
    admin_key = card.get("admin_key")
    found = StepButton(
        position + 1,
        invitation if isinstance(invitation, str) else None,
        rekey if isinstance(rekey, str) else None,
        card.get("key_new") is True,
        admin_key if isinstance(admin_key, str) else None,
    )
    if (
        found.invitation is None
        and found.rekey is None
        and not found.key_new
        and found.admin_key is None
    ):
        return None
    return found


async def add_steps(
    session: AsyncSession,
    confirmation_id: uuid.UUID,
    steps: Sequence[PlannedStep],
    *,
    start: int = 0,
) -> None:
    """Store ``steps``, in their order (positions from ``start``: after the steps already done of
    a chain that is changed), as the steps of the request ``confirmation_id``, each ``planned``,
    with ``with_basis`` and the arrival it waits for. Flushed, not committed."""
    for position, step in enumerate(steps, start=start):
        session.add(
            ConfirmationStep(
                confirmation_id=confirmation_id,
                position=position,
                tool_name=step.tool_name,
                action_type=step.action_type,
                arguments=dict(step.arguments),
                action=None if step.action is None else step.action.as_json(),
                status="planned",
                with_basis=step.with_basis,
                after_arrival_of=step.after_arrival_of,
            )
        )
    await session.flush()


def maybe_done(row: ConfirmationStep) -> bool:
    """Whether the step ``row``, still to run, may already have been done (``MAYBE_DONE``)."""
    return row.status == "planned" and row.result is not None and row.result.get(MAYBE_DONE) is True


async def steps_of(session: AsyncSession, confirmation_id: uuid.UUID) -> list[ConfirmationStep]:
    """The steps of the request ``confirmation_id``, in their order (none for a request that is no
    chain), as the database holds them now."""
    result = await session.execute(
        select(ConfirmationStep)
        .where(ConfirmationStep.confirmation_id == confirmation_id)
        .order_by(ConfirmationStep.position)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def mark_step(
    session: AsyncSession,
    confirmation_id: uuid.UUID,
    position: int,
    *,
    status: StepStatus,
    result: JsonObject | None,
    now: datetime,
    action: DescribedAction | None = None,
) -> None:
    """Mark the step at ``position`` of the request ``confirmation_id`` with what came of it:
    its ``status`` and ``result`` (the card of what it did), ended at ``now``; a step made
    ``planned`` again has not ended. ``action`` is, for a step checked in its turn that was done,
    the action its tool described it as in its turn: stored on its row, which had none, so that
    the row tells what the step did (a hand-over of the owner's role it made counts when the
    chain is continued, ``confirmations.chain.run_steps``); without it, the row keeps the action
    it has. ``LookupError`` when the request has no step at that position: what came of a step is
    never lost without a word. Flushed, not committed."""
    values: dict[str, object] = {
        "status": status,
        "result": None if result is None else dict(result),
        "done_at": None if status == "planned" else now,
    }
    if action is not None:
        values["action"] = action.as_json()
    marked = await session.execute(
        update(ConfirmationStep)
        .where(
            ConfirmationStep.confirmation_id == confirmation_id,
            ConfirmationStep.position == position,
        )
        .values(values)
        .returning(ConfirmationStep.position)
        .execution_options(synchronize_session=False)
    )
    if marked.first() is None:
        raise LookupError("the request has no step at this position")
    await session.flush()
