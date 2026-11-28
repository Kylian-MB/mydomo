"""Forgetting someone ("forget me"): a record emptied for good (spec 6.8, rulings 12, 17).

A guest or a relative is forgotten without the owner's yes: their name and everything their record
carries, their links, nicknames and trust, the memories about them (and what they said in
private), the traces of merges, their conversations and their account go. The row itself stays,
emptied (``empty_row``), so that the journal (numbers only) and every column that names it keep
standing: the keys they gave, the people they invited, the acts done on them.

What forgetting does to each column that names a record is written in ``WHAT_FORGETTING_DOES``,
by ``table.column``: its rows are ``deleted``, the column is ``emptied`` (the row stays, about
someone else, no longer tied to them), or it is ``kept`` pointing at the emptied row. A table added
later that names a record must say it there (a test reads the metadata).

Forgotten at their own request, a person is forgotten in a call that runs in their conversation
(their turn, or their request after a yes): what it runs in (their conversations, requests and
account) goes once it is over (``finish``: at the end of the turn, else by the upkeep), so that
the call's journal lines and the turn's reply land first.

What the code holds here, whoever asks:

* the owner is never forgotten (``NotPossible("the_owner")``);
* someone who lives here and has access is forgotten as anyone is, their access with them (the
  owner's answer of 2026-11-27, "Domo juge": ``loses_access`` is shown to the judge), and the
  owner is told afterwards unless he forgot them himself (an act owed to him, his news);
* a withdrawal or a refusal of the owner's in force stays, with a description to know them again
  (the name they had, when it had none), and the row keeps its state. The references a channel
  holds for them go too, unless ``keeps_recognition`` says that what a channel needs to know them
  again is kept (``KEEP_RECOGNITION_OF_THE_REFUSED``): then they stay on the emptied row.

The functions flush and do not commit: the caller does.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy import Column, ColumnElement, Table, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import marks_of
from domo_core.accounts.models import Account, Person
from domo_core.chat.models import Conversation, Message, MessageAction
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.db import Base, SessionFactory, load_models
from domo_core.household.models import Invitation
from domo_core.household.service import off_the_successors
from domo_core.learning.models import WentWrong
from domo_core.memory.models import Memory
from domo_core.people import directives, made_roles, records
from domo_core.people.acts import note_act, tell_owner
from domo_core.people.models import (
    AccessPromise,
    MadeRole,
    OwnerDirective,
    PersonAct,
    RecordMerge,
)
from domo_core.people.news import news_for
from domo_core.policy.models import PolicyRight

__all__ = [
    "FINISH_AFTER",
    "FORGOTTEN_REASON",
    "KEEP_RECOGNITION_OF_THE_REFUSED",
    "WHAT_FORGETTING_DOES",
    "empty_row",
    "finish",
    "finish_after_the_turn",
    "finish_the_forgotten",
    "forget",
    "keeps_recognition",
    "loses_access",
    "recognition_kept",
    "why_not",
]

logger = logging.getLogger(__name__)

Fate = Literal["deleted", "emptied", "kept"]

# The journal (the audit log) is left as it is, the name a line holds included: the owner's
# decision of 2026-11-27 ("the name stays").
# What forgetting does to each column that names a record. Some have a step of their own before
# the rest (``forget``): their requests and conversations, the invitations that name them, what
# they said in private, their place on the successors' list, the owner's directives (one in force
# stays, those ended go), the reasons of the acts done on them or in their turn, the content of
# the traces of merges.
WHAT_FORGETTING_DOES: Final[Mapping[str, Fate]] = {
    # The people they invited still hold; the records merged into them are emptied too.
    "persons.invited_by": "kept",
    "persons.merged_into_id": "kept",
    # A promise that is over goes; one still open is first kept for nobody (``_what_names_them``).
    "access_promises.kept_person_id": "deleted",
    "accounts.person_id": "deleted",
    "conversations.person_id": "deleted",
    "device_pairings.person_id": "deleted",
    "devices.person_id": "deleted",
    "hand_runs.opened_by": "emptied",
    "hand_runs.for_person_id": "deleted",
    "identity_clues.person_id": "deleted",
    # The invitations they made for others still hold; the ones that named them go.
    "invitations.created_by": "kept",
    "invitations.accepted_person_id": "deleted",
    "invitations.person_id": "deleted",
    "invitations.link_for_id": "emptied",
    "made_tool_triggers.returner_id": "deleted",
    "made_tool_triggers.for_person_id": "deleted",
    # What they said for everyone stays, no longer theirs; what they said in private goes.
    "memories.said_by_id": "emptied",
    "memories.about_id": "deleted",
    "model_choices.changed_by": "emptied",
    "notes.author_id": "deleted",
    "notes.recipient_id": "deleted",
    # A withdrawal or a refusal in force stays; the owner sets and lifts them.
    "owner_directives.person_id": "kept",
    "owner_directives.set_by": "kept",
    "owner_directives.lifted_by": "kept",
    "person_channel_refs.person_id": "deleted",
    "person_links.from_id": "deleted",
    "person_links.to_id": "deleted",
    "person_links.said_by_id": "emptied",
    "person_nicknames.person_id": "deleted",
    "person_nicknames.target_id": "deleted",
    "policy_rights_drafts.created_by": "kept",
    "policy_settings.updated_by": "kept",
    "proof_means.person_id": "deleted",
    # A key they gave still holds.
    "providers.given_by": "kept",
    # The ids stay, what moved is emptied.
    "record_merges.kept_id": "kept",
    "record_merges.absorbed_id": "kept",
    "service_accounts.person_id": "deleted",
    "service_accounts.given_by": "emptied",
    "standing_agreements.for_person_id": "deleted",
    "standing_agreements.given_by": "emptied",
    "successors.person_id": "deleted",
    "tools_spaces.person_id": "emptied",
    "written_texts.person_id": "deleted",
    # Numbers only: what an agreement of the house counted stays right.
    "declared_spending.for_person_id": "kept",
    "key_newcomers.person_id": "deleted",
    "launch_stops.for_person_id": "deleted",
    "made_tool_runs.person_id": "deleted",
    "model_levels.set_by": "kept",
    # The rows stay for the ids, their reasons go.
    "person_acts.person_id": "kept",
    "person_acts.other_id": "kept",
    "person_acts.turn_of": "kept",
    # Their rights go with their words (``summary``, ``owner_words``).
    "policy_rights.person_id": "deleted",
    "policy_rights.created_by": "kept",
    "provider_users.person_id": "deleted",
    "provider_users.set_by": "kept",
    "spending_limits.person_id": "deleted",
    "spending_limits.set_by": "kept",
    "tool_builds.opened_by": "emptied",
    "trust_facts.person_id": "deleted",
    "trust_facts.noted_for_id": "emptied",
    "trust_facts.reported_by_id": "emptied",
    "confirmations.requester_id": "deleted",
    "confirmations.decided_by": "emptied",
    "confirmations.concerned_id": "emptied",
    "deliveries.person_id": "deleted",
    # Numbers only: what the house and each role spent stays right.
    "model_calls.person_id": "kept",
    "plans.person_id": "deleted",
    "card_texts.reader_id": "deleted",
    "went_wrong.by_person_id": "emptied",
    "went_wrong.about_person_id": "deleted",
}

_PERSON: Final = "person:"
_RIGHTS: Final = "policy_rights.person_id"
# The references a channel holds for a person (``people.hooks``).
_CHANNEL_REFS: Final = "person_channel_refs.person_id"
# Whether a channel keeps what it needs to know again someone the owner refused or withdrew, once
# they are forgotten: their references stay on the emptied row. Not yet: only the directive's
# description is kept, until the owner says whether he allows it (open point 17); his answer would
# become a setting read by ``keeps_recognition``, and nothing else would change.
KEEP_RECOGNITION_OF_THE_REFUSED: Final[bool] = False
# What a forgetting that runs in the person's own call leaves for after it (``finish``).
_AFTER_THE_CALL: Final = frozenset(
    {"accounts.person_id", "conversations.person_id", "confirmations.requester_id"}
)
# How long after a forgetting the upkeep finishes what its call left (``finish_the_forgotten``):
# longer than the longest turn.
FINISH_AFTER: Final = timedelta(minutes=15)
# Why a request of someone else's that waited for a forgotten person was refused (``went_wrong``).
FORGOTTEN_REASON: Final = "person_forgotten"
# The columns of ``confirmations`` that name a message.
_MESSAGE_COLUMNS: Final = (
    "message_id",
    "last_shown_message_id",
    "decided_message_id",
    "shown_to_approver_message_id",
)


def loses_access(person: Person) -> bool:
    """Whether forgetting ``person`` takes away the access of someone who lives here: they live
    here and have access. Nothing refuses it (the owner's answer of 2026-11-27): the judge is
    shown it, and the owner is told afterwards."""
    return person.state == "has_access" and marks_of(person)["resident"] is True


def keeps_recognition(directive: OwnerDirective | None) -> bool:
    """Whether forgetting someone under ``directive`` (the owner's withdrawal or refusal in force
    for them, ``None`` when there is none) keeps the references a channel holds for them: the one
    place that reads ``KEEP_RECOGNITION_OF_THE_REFUSED``."""
    return directive is not None and KEEP_RECOGNITION_OF_THE_REFUSED


async def recognition_kept(session: AsyncSession, person_id: UUID, now: datetime) -> bool:
    """``keeps_recognition`` for that record, under the directive in force for it at ``now``."""
    return keeps_recognition(await directives.under_directive(session, person_id, now))


async def _kept_columns(session: AsyncSession, person_id: UUID, now: datetime) -> frozenset[str]:
    """What forgetting that record leaves as it is: the references a channel holds for them, when
    ``recognition_kept``."""
    if await recognition_kept(session, person_id, now):
        return frozenset({_CHANNEL_REFS})
    return frozenset()


def why_not(person: Person) -> str | None:
    """Why ``person`` cannot be forgotten, in a word (``not_a_record``, ``the_owner``), or
    ``None`` when they can. Reads only the record."""
    if person.merged_into_id is not None or person.forgotten_at is not None:
        return "not_a_record"
    if person.role == "owner":
        return "the_owner"
    return None


def _pointing_columns() -> list[Column[object]]:
    """Every column that names a record, children's tables first (what a row needs is deleted
    after it)."""
    load_models()
    return [
        column
        for table in reversed(Base.metadata.sorted_tables)
        for column in table.columns
        if any(key.column.table.name == "persons" for key in column.foreign_keys)
    ]


async def _merged_into(session: AsyncSession, person: Person) -> list[UUID]:
    """``person`` and every record merged into them, at any depth."""
    ids = [person.id]
    found = [person.id]
    while found:
        found = list(
            await session.scalars(select(Person.id).where(Person.merged_into_id.in_(found)))
        )
        ids.extend(one for one in found if one not in ids)
    return ids


async def _keep_the_directive(
    session: AsyncSession, person: Person, ids: list[UUID], now: datetime
) -> None:
    """A withdrawal or a refusal in force stays, with a description to know them again (the name
    they had, when it had none); the directives about them that ended go."""
    found = await directives.under_directive(session, person.id, now)
    if found is not None and not found.description and person.display_name:
        found.description = person.display_name[:300]
    await session.execute(
        delete(OwnerDirective).where(OwnerDirective.person_id.in_(ids), ~directives.in_force(now))
    )


async def _refuse_what_waited_for_them(
    session: AsyncSession, ids: list[UUID], now: datetime
) -> None:
    """A request of someone else's that waited for their word, or for their arrival, is refused,
    and its asker is told (news kept for them, ``people.news.news_for``, with the fact of what went
    wrong, ``refused`` for ``person_forgotten``); the invitation it waited for goes."""
    # Not at the top: ``learning.record`` reaches the policy, which reads the household.
    from domo_core.learning.record import note_went_wrong

    invitations = select(Invitation.id).where(
        or_(Invitation.person_id.in_(ids), Invitation.accepted_person_id.in_(ids))
    )
    refused = list(
        await session.scalars(
            update(Confirmation)
            .where(
                or_(
                    Confirmation.concerned_id.in_(ids),
                    Confirmation.waiting_for_invitation_id.in_(invitations),
                ),
                Confirmation.requester_id.not_in(ids),
                Confirmation.status.in_(("pending", "waiting_arrival")),
            )
            .values(status="refused", decided_at=now)
            .returning(Confirmation)
            .execution_options(synchronize_session=False)
        )
    )
    for request in refused:
        action_type = request.action.get("action_type")
        await note_went_wrong(
            session,
            kind="refused",
            action_type=action_type if isinstance(action_type, str) else "",
            now=now,
            subject_key=request.subject_key,
            confirmation_id=request.id,
            by="domo",
            of_domo=request.by_domo or request.idea_reason is not None,
            about=request.requester_id,
            reason=FORGOTTEN_REASON,
        )
        await news_for(session, request.requester_id, now=now)
    await session.execute(
        update(Confirmation)
        .where(Confirmation.waiting_for_invitation_id.in_(invitations))
        .values(waiting_for_invitation_id=None)
        .execution_options(synchronize_session=False)
    )


async def _their_requests_and_conversations(session: AsyncSession, ids: list[UUID]) -> None:
    """Their requests go, and nothing of anyone else's keeps pointing at their conversations
    (deleted with their messages afterwards, ``_the_rest``)."""
    conversations = select(Conversation.id).where(Conversation.person_id.in_(ids))
    messages = select(Message.id).where(Message.conversation_id.in_(conversations))
    theirs = list(
        await session.scalars(
            select(Confirmation.id).where(
                or_(
                    Confirmation.requester_id.in_(ids),
                    Confirmation.conversation_id.in_(conversations),
                )
            )
        )
    )
    if theirs:
        await session.execute(
            delete(ConfirmationStep).where(ConfirmationStep.confirmation_id.in_(theirs))
        )
        await session.execute(
            update(MessageAction)
            .where(MessageAction.confirmation_id.in_(theirs))
            .values(confirmation_id=None)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(Confirmation)
            .where(Confirmation.replaced_by.in_(theirs))
            .values(replaced_by=None)
            .execution_options(synchronize_session=False)
        )
        await session.execute(delete(Confirmation).where(Confirmation.id.in_(theirs)))
    table = cast("Table", Confirmation.__table__)
    for name in _MESSAGE_COLUMNS:
        await session.execute(update(table).where(table.c[name].in_(messages)).values({name: None}))


async def _empty_the_merges(session: AsyncSession, ids: list[UUID]) -> None:
    """The traces of their merges keep their ids and nothing of what moved, but what the owner is
    still owed of one he was not told (facts, numbers only: ``merge.purge`` keeps it the same)."""
    # Not at the top: ``people.merge`` empties its rows with this module.
    from domo_core.people.merge import told_to_owner

    traces = await session.scalars(
        select(RecordMerge).where(
            or_(RecordMerge.kept_id.in_(ids), RecordMerge.absorbed_id.in_(ids))
        )
    )
    for trace in list(traces):
        owed = trace.moved.get("owed")
        trace.moved = {} if owed is None or await told_to_owner(session, trace) else {"owed": owed}
    await session.flush()


async def _what_names_them(session: AsyncSession, ids: list[UUID]) -> None:
    """The steps of their own, before the rest: what they said in private and the memories and
    facts keyed on them, their place on the successors' list, the reasons of the acts done on
    them or in their turn, and what the traces of merges moved."""
    keys = [f"{_PERSON}{one}" for one in ids]
    await session.execute(
        delete(Memory).where(
            or_(
                Memory.said_by_id.in_(ids) & Memory.kept_private.is_(True),
                Memory.lesson_subject_key.in_(keys),
            )
        )
    )
    await session.execute(delete(WentWrong).where(WentWrong.subject_key.in_(keys)))
    for one in ids:
        await off_the_successors(session, one)
    await session.execute(
        update(PersonAct)
        .where(
            or_(
                PersonAct.person_id.in_(ids),
                PersonAct.other_id.in_(ids),
                PersonAct.turn_of.in_(ids),
            )
        )
        .values(reason=None)
        .execution_options(synchronize_session=False)
    )
    # A promise still open waits for others too ("Dana's children"): it stays, kept for nobody;
    # one that is over goes with them (``WHAT_FORGETTING_DOES``).
    await session.execute(
        update(AccessPromise)
        .where(
            AccessPromise.kept_person_id.in_(ids),
            AccessPromise.kept_at.is_(None),
            AccessPromise.dropped_at.is_(None),
        )
        .values(kept_person_id=None)
        .execution_options(synchronize_session=False)
    )
    await _empty_the_merges(session, ids)


def _owners_deny() -> ColumnElement[bool]:
    """A personal right that is the owner's deny, in force: what forgetting keeps."""
    return (
        (PolicyRight.access == "deny")
        & PolicyRight.revoked_at.is_(None)
        & PolicyRight.created_by.in_(select(Person.id).where(Person.role == "owner"))
    )


async def owners_deny_kept(session: AsyncSession, person_id: UUID) -> bool:
    """Whether a deny the owner wrote for that record is in force on it (kept when it was
    forgotten: what lets a forgotten record be known again, as a directive does)."""
    found = await session.scalar(
        select(PolicyRight.id).where(PolicyRight.person_id == person_id, _owners_deny()).limit(1)
    )
    return found is not None


async def _the_rest(session: AsyncSession, ids: list[UUID], *, skip: frozenset[str]) -> None:
    """Every column that names them, as ``WHAT_FORGETTING_DOES`` says (``KeyError`` for one it
    does not name), but those of ``skip``."""
    for column in _pointing_columns():
        where = f"{column.table.name}.{column.name}"
        fate = WHAT_FORGETTING_DOES[where]
        if where in skip:
            continue
        if fate == "deleted" and where == _RIGHTS:
            # Their rights go, but a deny the owner wrote for them stays, as a refusal does (the
            # owner's answer of 2026-11-27): it holds if they are known again.
            await session.execute(delete(column.table).where(column.in_(ids), ~_owners_deny()))
            # A deny holds back: never left paused, whatever their leaving paused.
            await session.execute(
                update(PolicyRight)
                .where(PolicyRight.person_id.in_(ids), _owners_deny())
                .values(paused_at=None)
                .execution_options(synchronize_session=False)
            )
        elif fate == "deleted":
            await session.execute(delete(column.table).where(column.in_(ids)))
        elif fate == "emptied":
            await session.execute(
                update(column.table).where(column.in_(ids)).values({column.name: None})
            )


async def _leave_their_role(session: AsyncSession, row: Person, now: datetime) -> None:
    """The row leaves its made role (``records.leave_role``); a role that went with it keeps no
    reason that would name them (``made_roles.UNUSED`` in its stead), nor do its acts."""
    role_id = row.made_role_id
    await records.leave_role(session, row, now=now)
    role = None if role_id is None else await session.get(MadeRole, role_id, populate_existing=True)
    if role is None or role.deleted_at is None:
        return
    role.reason = made_roles.UNUSED
    await session.execute(
        update(PersonAct)
        .where(PersonAct.role_id == role.id)
        .values(reason=None)
        .execution_options(synchronize_session=False)
    )


async def forget(
    session: AsyncSession,
    person: Person,
    *,
    by: str,
    turn_of: UUID | None,
    now: datetime,
    finish_later: bool = False,
    by_owner: bool = False,
    owner_knows: Callable[[UUID], bool] = lambda _owner: False,
) -> UUID | None:
    """Forget ``person`` for good, with the records merged into them: ``by`` is who acts
    (``domo`` or ``person:<id>``), ``turn_of`` whose turn it is. ``records.NotPossible``:
    ``not_a_record``, ``the_owner`` (``why_not``). Everything about them goes as
    ``WHAT_FORGETTING_DOES`` says, they leave their made role, their rows are emptied
    (``empty_row``), and the act ``forgotten`` is traced with no reason (a reason would name them).
    A withdrawal or a refusal in force stays, and the row keeps its state (and the references a
    channel holds for them when ``recognition_kept``); else a record that had access is ``gone``
    (nothing of theirs is allowed any more).

    ``finish_later``: their conversations, requests and account are what the call itself runs in
    (their own turn, or their request that runs after a yes): they go once it is over
    (``finish``: at the end of the turn, else by the upkeep, ``finish_the_forgotten``).

    Someone who lived here with access (``loses_access``) forgotten by anyone but the owner
    (``by_owner``): the act is owed to the owner, told once (``acts.tell_owner``: marked told when
    he knows of it already, ``owner_knows``, else his news is kept and his id returned, for the
    caller to tell the core once it committed). ``None`` otherwise. Flushed."""
    refused = why_not(person)
    if refused is not None:
        raise records.NotPossible(refused)
    owed = loses_access(person) and not by_owner
    ids = await _merged_into(session, person)
    kept = await _kept_columns(session, person.id, now)
    await _keep_the_directive(session, person, ids, now)
    await _refuse_what_waited_for_them(session, ids, now)
    if not finish_later:
        await _their_requests_and_conversations(session, ids)
    await _what_names_them(session, ids)
    await _the_rest(session, ids, skip=(kept | _AFTER_THE_CALL) if finish_later else kept)
    await session.flush()
    for one in ids:
        row = await session.get(Person, one, populate_existing=True)
        if row is None:
            continue
        await _leave_their_role(session, row, now)
        if row.state == "has_access":
            row.state = "gone"
            row.disabled_at = now
        await empty_row(session, row, now)
    act = await note_act(
        session,
        act="forgotten",
        by=by,
        reason=None,
        turn_of=turn_of,
        person_id=person.id,
        owed_to_owner=owed,
        now=now,
    )
    await session.refresh(person)
    if not owed:
        return None
    return await tell_owner(session, act, knows=owner_knows, now=now)


async def finish(session: AsyncSession, person_id: UUID, now: datetime) -> None:
    """What was left of a forgotten record for after the call that forgot it (``forget``'s
    ``finish_later``) goes now: their requests, conversations and account, and anything that
    named them since (the rest of their turn: a model's call, a card's text). Nothing for a record
    that is not forgotten. Flushed."""
    person = await session.get(Person, person_id, populate_existing=True)
    if person is None or person.forgotten_at is None:
        return
    ids = await _merged_into(session, person)
    kept = await _kept_columns(session, person.id, now)
    await _refuse_what_waited_for_them(session, ids, now)
    await _their_requests_and_conversations(session, ids)
    await _what_names_them(session, ids)
    await _the_rest(session, ids, skip=kept)
    await session.flush()


async def finish_after_the_turn(
    session_factory: SessionFactory, person_ids: Sequence[UUID], now: datetime
) -> None:
    """``finish`` for the people forgotten in a turn, once it is over, in a session of its own,
    committed (``conversation.turn``). A failure is logged: the upkeep finishes it later."""
    try:
        async with session_factory() as session:
            for person_id in person_ids:
                await finish(session, person_id, now)
            await session.commit()
    except Exception as error:
        logger.error("Forgetting could not be finished after the turn (%s)", type(error).__name__)


async def finish_the_forgotten(session: AsyncSession, now: datetime) -> int:
    """``finish`` every record forgotten at least ``FINISH_AFTER`` ago that still has an account or
    a conversation: a turn that stopped before its end, or a request that ran outside a turn (a
    pass of the upkeep, ``household.upkeep.departures_pass``). Returns how many. Flushed."""
    left = list(
        await session.scalars(
            select(Person.id).where(
                Person.forgotten_at.is_not(None),
                Person.forgotten_at <= now - FINISH_AFTER,
                or_(
                    exists().where(Account.person_id == Person.id),
                    exists().where(Conversation.person_id == Person.id),
                ),
            )
        )
    )
    for person_id in left:
        await finish(session, person_id, now)
    return len(left)


async def empty_row(session: AsyncSession, person: Person, now: datetime) -> None:
    """The row of a record that is gone for good: no name, nothing optional, ``forgotten_at`` set,
    no review owed. It stays, so that the journal (which holds numbers only) and every column that
    names it keep standing. Its origin goes back to the plain default, since ``domo`` keeps a
    reason. The one writer of an emptied row: a record forgotten, erased (``records.erase``), or
    absorbed by a merge whose trace is past (``merge.purge``). Flushed."""
    await session.execute(
        update(Person)
        .where(Person.id == person.id)
        .values(
            display_name="",
            origin="invitation",
            origin_reason=None,
            expires_at=None,
            invited_by=None,
            language=None,
            writing_note=None,
            last_visit_at=None,
            news_at=None,
            last_review_due=False,
            calls_domo=None,
            born_on=None,
            access_returns=False,
            made_role_id=None,
            forgotten_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    await session.refresh(person)
