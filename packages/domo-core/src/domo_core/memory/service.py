"""Writing, changing and forgetting memories, and who sees which (spec 6.1).

Everyone can write, change and forget a memory: what Domo lets through is his judgement, not a
right the code reads. What the code does is keep who said it (``said_by_id``), keep what Domo
keeps for himself out of other people's turns (``visible_to``), refuse text that has the shape of
a key, a token or a device code (``refuses_secret_shapes``: a short code said in words has no
shape, the prompt tells Domo not to keep it) and bound the core.

What someone says about another person of the household is their word, not a fact, until that
person confirms it (2026-11-16, the owner's trial of 0.9.0: told by Sam that Alex does not like pine
nuts, Domo planned a salad for Alex around it, and it was false): ``unconfirmed`` says which
memories are such, and every context and search marks them. A memory said by the person it is
about, or about nobody of the household, never is.

None of the functions commits: the caller owns the transaction (an audit entry commits with the
change it describes). Nothing here knows the web.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, delete, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.memory.models import Kind, Memory, Origin
from domo_core.setup_flow.code import SETUP_CODE_ALPHABET

MAX_SUBJECT: Final = 120
MAX_CONTENT: Final = 600
# Who said a memory, as Domo is told: the person of the turn, or someone no longer in the
# database.
SAID_BY_YOU: Final = "you"
SAID_BY_SOMEONE_WHO_LEFT: Final = "someone who left"

_LONG_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{20,}")
_URL_SECRET: Final = re.compile(r"https?://\S*[?&](?:token|code|key)=", re.IGNORECASE)
_GROUP: Final = f"[{SETUP_CODE_ALPHABET}]{{4}}"
# Three groups of four of the code alphabet, in any case, apart by a dash or a single space.
_GROUPED_CODE: Final = re.compile(
    rf"(?<![A-Za-z0-9]){_GROUP}[- ]{_GROUP}[- ]{_GROUP}(?![A-Za-z0-9])", re.IGNORECASE
)
# Twelve characters of the alphabet in one piece (``normalize_setup_code`` accepts it too).
_COMPACT_CODE: Final = re.compile(rf"(?<![A-Za-z0-9]){_GROUP * 3}(?![A-Za-z0-9])", re.IGNORECASE)


class SecretRefused(Exception):
    """The text has the shape of a key, a token, a link with a secret or a device code."""


@dataclass(frozen=True)
class CoreMemories:
    """The core that fits ``max_chars`` (``items``, the most recently served first) and how many
    core memories of the person did not fit (``left_out``)."""

    items: tuple[Memory, ...]
    left_out: int


def refuses_secret_shapes(text: str) -> bool:
    """Whether ``text`` has the shape of something that must never be kept: 20 or more characters
    without a space, made of letters, digits, ``-`` and ``_`` with at least one letter and one
    digit (a key or a token); a link with a ``token=``, ``code=`` or ``key=`` parameter; or a
    device code as ``normalize_setup_code`` would accept it: three groups of four characters of the
    code alphabet apart by a dash or a space, in any case (so three ordinary four-letter words
    made of those letters, such as "same data team", are refused too; "that blue door" has an
    ``l`` and an ``o``, which the alphabet lacks, and passes), or the twelve characters in one
    piece when they hold a digit or are all upper case. Twelve letters in one piece in lower or
    mixed case are not recognised: they cannot be told from an ordinary word."""
    for run in _LONG_TOKEN.findall(text):
        if any(ch.isalpha() for ch in run) and any(ch.isdigit() for ch in run):
            return True
    if _URL_SECRET.search(text) or _GROUPED_CODE.search(text):
        return True
    return any(
        any(ch.isdigit() for ch in found.group()) or found.group().isupper()
        for found in _COMPACT_CODE.finditer(text)
    )


def unconfirmed(memory: Memory) -> bool:
    """Whether ``memory`` is someone's word about another person of the household that this
    person has not confirmed: it is about someone (``about_id``), said by someone else (or by
    someone whose row is gone), and has no ``confirmed_at``."""
    return (
        memory.about_id is not None
        and memory.said_by_id != memory.about_id
        and memory.confirmed_at is None
    )


def visible_to(person_id: uuid.UUID, *, with_kept: bool = False) -> ColumnElement[bool]:
    """The condition that keeps the memories ``person_id``'s turn may see of itself: what Domo
    does not keep for himself, and what this person said. ``with_kept`` (Domo asked for what he
    keeps for himself, on purpose) lets everything through."""
    if with_kept:
        return true()
    return or_(Memory.kept_private.is_(False), Memory.said_by_id == person_id)


def of_domo() -> ColumnElement[bool]:
    """The condition that keeps the memories the assistant himself wrote."""
    return Memory.by_domo.is_(True)


def _check(subject: str, content: str) -> None:
    if not subject.strip() or not content.strip():
        raise ValueError("A subject and a memory are not blank.")
    if not 1 <= len(subject) <= MAX_SUBJECT:
        raise ValueError(f"A subject is 1 to {MAX_SUBJECT} characters.")
    if not 1 <= len(content) <= MAX_CONTENT:
        raise ValueError(f"A memory is 1 to {MAX_CONTENT} characters.")
    if refuses_secret_shapes(subject) or refuses_secret_shapes(content):
        raise SecretRefused


async def keep(
    session: AsyncSession,
    *,
    said_by: uuid.UUID | None,
    subject: str,
    content: str,
    kind: Kind,
    origin: Origin,
    kept_private: bool,
    core: bool,
    now: datetime,
    about_id: uuid.UUID | None = None,
    action_type: str | None = None,
    by_domo: bool = False,
    for_everyone: bool = False,
    lesson_action_type: str | None = None,
    lesson_subject_key: str | None = None,
    words_checked: bool = False,
) -> Memory:
    """A new memory said by ``said_by``, or by the assistant himself (``by_domo``: ``said_by`` may
    be ``None`` then, and only then); ``words_checked`` when the code checked that its content is
    itself a passage of their own message, word for word (``Memory.words_checked``). Raises
    ``SecretRefused`` for a text that has the shape of a secret and ``ValueError`` for a subject
    or content out of bounds, an ``action_type`` on a memory that is not ``told_by_domo``, a
    lesson's fields (``for_everyone``,
    ``lesson_action_type``, ``lesson_subject_key``) on another kind, or nobody who said it without
    ``by_domo``; nothing is written then. Flushed."""
    _check(subject, content)
    if said_by is None and not by_domo:
        raise ValueError("A memory is said by a person, unless the assistant wrote it himself.")
    if kind != "lesson" and (
        for_everyone or lesson_action_type is not None or lesson_subject_key is not None
    ):
        raise ValueError("What a lesson is for and about goes with a lesson only.")
    if action_type is not None and origin != "told_by_domo":
        raise ValueError("An action type goes with what the assistant told the person only.")
    memory = Memory(
        said_by_id=said_by,
        about_id=about_id,
        subject=subject,
        content=content,
        kind=kind,
        origin=origin,
        kept_private=kept_private,
        core=core,
        action_type=action_type,
        by_domo=by_domo,
        for_everyone=for_everyone,
        lesson_action_type=lesson_action_type,
        lesson_subject_key=lesson_subject_key,
        words_checked=words_checked,
        created_at=now,
        changed_at=now,
    )
    session.add(memory)
    await session.flush()
    return memory


async def change(
    session: AsyncSession,
    memory: Memory,
    *,
    by: uuid.UUID,
    now: datetime,
    content: str | None = None,
    subject: str | None = None,
    core: bool | None = None,
    kept_private: bool | None = None,
    origin: Origin | None = None,
    said_by: uuid.UUID | None = None,
    confirmed_at: datetime | None = None,
    unconfirm: bool = False,
    words_checked: bool = False,
) -> Memory:
    """Change what is given of ``memory``; ``said_by_id`` stays (it says in whose turn it was
    born, not who changed it), and so does ``origin`` unless the caller sets it again from the turn
    of the change (``origin``: words rewritten are words written, spec 6.3). Anyone's turn may
    change it, ``kept_private`` too: that is Domo's judgement, and ``by`` (the person whose turn
    it is) is for the journal entry the caller writes. Same refusals as ``keep``; a new origin on
    a memory that has an ``action_type`` (what Domo told the person) is a ``ValueError``. A new
    subject or content empties the vector of its meaning, computed again later
    (``memory.search.embed_missing``).

    ``said_by`` makes the memory the words of another person (the person it is about corrected
    it in their own turn, or a third person gave it new words in Domo's answer to them: it is
    theirs now); ``confirmed_at`` says that the person it is about
    confirmed it; ``unconfirm`` takes a confirmation away (someone else's new words). New words,
    or words that become someone else's, keep ``words_checked`` only when the caller checked them
    again (``words_checked``). Flushed."""
    del by
    if origin is not None and origin != "told_by_domo" and memory.action_type is not None:
        raise ValueError("An action type goes with what the assistant told the person only.")
    new_subject = memory.subject if subject is None else subject
    new_content = memory.content if content is None else content
    _check(new_subject, new_content)
    if (new_subject, new_content) != (memory.subject, memory.content):
        # The vector was of the old words: computed again outside the turn (``memory.search``).
        memory.embedding = None
        memory.embedding_model = None
    if new_content != memory.content or (said_by is not None and said_by != memory.said_by_id):
        # What was checked was the old words, or someone else's.
        memory.words_checked = words_checked
    memory.subject = new_subject
    memory.content = new_content
    if core is not None:
        memory.core = core
    if kept_private is not None:
        memory.kept_private = kept_private
    if origin is not None:
        memory.origin = origin
    if said_by is not None:
        memory.said_by_id = said_by
    if confirmed_at is not None:
        memory.confirmed_at = confirmed_at
    elif unconfirm:
        memory.confirmed_at = None
    memory.changed_at = now
    await session.flush()
    return memory


async def forget(session: AsyncSession, ids: Sequence[uuid.UUID]) -> int:
    """Delete these memories for good, whoever said them and whether or not Domo keeps them for
    himself (he decided what to forget). The number deleted. Flushed."""
    if not ids:
        return 0
    result = await session.execute(delete(Memory).where(Memory.id.in_(ids)).returning(Memory.id))
    return len(result.all())


async def get(session: AsyncSession, memory_id: uuid.UUID) -> Memory | None:
    """The memory with this id, whoever it was said by, or ``None``."""
    return await session.get(Memory, memory_id)


async def core_of(session: AsyncSession, person_id: uuid.UUID, *, max_chars: int) -> CoreMemories:
    """The core memories this person's turn may see, as many as fit ``max_chars`` (a memory
    weighs its subject and its content), the most recently served first (one never served counts
    from when it was created). Those that do not fit are the least recently served, and are
    counted in ``left_out``."""
    served = func.coalesce(Memory.served_at, Memory.created_at)
    rows = (
        await session.scalars(
            select(Memory)
            .where(Memory.core.is_(True), visible_to(person_id))
            .order_by(served.desc(), Memory.id)
        )
    ).all()
    items: list[Memory] = []
    used = 0
    for memory in rows:
        weight = len(memory.subject) + len(memory.content)
        if used + weight > max_chars:
            break
        items.append(memory)
        used += weight
    return CoreMemories(items=tuple(items), left_out=len(rows) - len(items))


async def mark_served(session: AsyncSession, ids: Sequence[uuid.UUID], now: datetime) -> None:
    """These memories were put under Domo's eyes at ``now``. Flushed."""
    if ids:
        await session.execute(update(Memory).where(Memory.id.in_(ids)).values(served_at=now))


async def who_said(
    session: AsyncSession, memories: Sequence[Memory], speaker: uuid.UUID
) -> dict[uuid.UUID, str]:
    """Who said each of ``memories``, by the memory's id, as Domo is told it in ``speaker``'s
    turn: ``"you"`` for the speaker, else their display name now; ``"someone who left"`` once their
    row is gone."""
    others = {
        m.said_by_id for m in memories if m.said_by_id is not None and m.said_by_id != speaker
    }
    names: dict[uuid.UUID, str] = {}
    if others:
        found = await session.execute(
            select(Person.id, Person.display_name).where(Person.id.in_(others))
        )
        names = {person_id: name for person_id, name in found.all()}

    def label(memory: Memory) -> str:
        if memory.said_by_id is None:
            return SAID_BY_SOMEONE_WHO_LEFT
        if memory.said_by_id == speaker:
            return SAID_BY_YOU
        return names.get(memory.said_by_id, SAID_BY_SOMEONE_WHO_LEFT)

    return {memory.id: label(memory) for memory in memories}
