"""Keeping the texts Domo wrote (``written_texts``), for a person or for a language.

A text is read only while the meaning it was written for is still the catalog's
(``meaning_hash``); a key that left the catalog is not read at all. Writing a text again keeps
the one it replaces in ``previous`` (``KEPT_VERSIONS`` of them, newest first), and
``restore_previous`` brings one back. The functions flush but never commit: the caller does.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from domo_common.meanings import meaning_hash
from sqlalchemy import ColumnElement, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.texts.models import WrittenText

KEPT_VERSIONS: Final = 5

# The text a row replaces, as an entry of ``previous``; and ``previous`` once that entry is put
# first and only ``KEPT_VERSIONS`` are kept. Both read the row being replaced.
_TABLE: Final = WrittenText.__tablename__
_REPLACED: Final = (
    f"jsonb_build_object('text', {_TABLE}.text, 'meaning_hash', {_TABLE}.meaning_hash, "
    f"'written_at', {_TABLE}.written_at)"
)
_KEPT: Final = (
    "(SELECT coalesce(jsonb_agg(item ORDER BY position), '[]'::jsonb) "
    f"FROM jsonb_array_elements(jsonb_build_array({_REPLACED}) || {_TABLE}.previous) "
    f"WITH ORDINALITY AS kept(item, position) WHERE position <= {KEPT_VERSIONS})"
)


@dataclass(frozen=True)
class Reader:
    """Who a text is written for: a person (``person_id``), or the version of a language
    (``person_id`` is ``None``). A person's texts are kept by ``person_id`` alone: their
    ``language`` is the one they read now, not part of where their texts are."""

    person_id: UUID | None
    language: str


def _is(reader: Reader) -> ColumnElement[bool]:
    if reader.person_id is not None:
        return WrittenText.person_id == reader.person_id
    return WrittenText.language == reader.language


def _current_hash(key: str) -> str | None:
    """The hash of the meaning of ``key`` now, ``None`` for a key no longer in the catalog."""
    try:
        return meaning_hash(key)
    except KeyError:
        return None


async def written(session: AsyncSession, readers: Sequence[Reader]) -> dict[Reader, dict[str, str]]:
    """The texts of each reader whose meaning is still the catalog's, by key (an empty mapping for
    a reader with none)."""
    found: dict[Reader, dict[str, str]] = {reader: {} for reader in readers}
    if not readers:
        return found
    people = {r.person_id for r in readers if r.person_id is not None}
    languages = {r.language for r in readers if r.person_id is None}
    rows = await session.execute(
        select(
            WrittenText.person_id,
            WrittenText.language,
            WrittenText.key,
            WrittenText.text,
            WrittenText.meaning_hash,
        ).where(or_(WrittenText.person_id.in_(people), WrittenText.language.in_(languages)))
    )
    hashes: dict[str, str | None] = {}
    for row in rows:
        if row.key not in hashes:
            hashes[row.key] = _current_hash(row.key)
        if hashes[row.key] != row.meaning_hash:
            continue
        for reader in readers:
            mine = (
                row.person_id == reader.person_id
                if reader.person_id is not None
                else row.language == reader.language
            )
            if mine:
                found[reader][row.key] = row.text
    return found


async def missing_keys(session: AsyncSession, reader: Reader, keys: Iterable[str]) -> list[str]:
    """Those of ``keys`` that have no text to read for ``reader`` (never written, or written for
    another meaning), in the order given. A key no longer in the catalog is not asked for."""
    wanted = [key for key in dict.fromkeys(keys) if _current_hash(key) is not None]
    have = (await written(session, [reader]))[reader]
    return [key for key in wanted if key not in have]


async def save_written(
    session: AsyncSession,
    reader: Reader,
    texts: Mapping[str, str],
    *,
    model_id: str,
    now: datetime,
) -> None:
    """Keep ``texts`` (by key) as the texts of ``reader``, in place of the ones they had.

    Each key is one atomic ``INSERT ... ON CONFLICT DO UPDATE`` on the reader's unique index (one
    statement, run for each key at once), in the order of the keys, so two saves at once never
    fail and never leave two rows. The text replaced goes first into ``previous`` (which keeps
    ``KEPT_VERSIONS``); saving the same text for the same meaning leaves the row as it is. A key
    not in the catalog raises ``KeyError``.
    """
    if not texts:
        return
    no_previous: list[dict[str, str]] = []
    rows: list[dict[str, object]] = [
        {
            "person_id": reader.person_id,
            "language": None if reader.person_id is not None else reader.language,
            "key": key,
            "text": texts[key],
            "meaning_hash": meaning_hash(key),
            "model_id": model_id,
            "written_at": now,
            "previous": no_previous,
        }
        for key in sorted(texts)
    ]
    statement = insert(WrittenText)
    target: dict[str, Any]
    if reader.person_id is not None:
        target = {
            "index_elements": [WrittenText.person_id, WrittenText.key],
            "index_where": WrittenText.person_id.is_not(None),
        }
    else:
        target = {
            "index_elements": [WrittenText.language, WrittenText.key],
            "index_where": WrittenText.language.is_not(None),
        }
    await session.execute(
        statement.on_conflict_do_update(
            **target,
            set_={
                "text": statement.excluded.text,
                "meaning_hash": statement.excluded.meaning_hash,
                "model_id": statement.excluded.model_id,
                "written_at": statement.excluded.written_at,
                "previous": literal_column(_KEPT),
            },
            where=or_(
                WrittenText.text != statement.excluded.text,
                WrittenText.meaning_hash != statement.excluded.meaning_hash,
            ),
        ),
        rows,
    )


async def restore_previous(session: AsyncSession, reader: Reader, keys: Iterable[str]) -> list[str]:
    """Bring back, for each of ``keys``, the newest earlier text written for the meaning the key
    has now. The text it replaces is dropped, and so are the newer versions above the one
    restored. A key with no such version (or none at all) is left as it is and not returned."""
    restored: list[str] = []
    for key in dict.fromkeys(keys):
        current = _current_hash(key)
        if current is None:
            continue
        row = (
            await session.execute(
                select(WrittenText)
                .where(_is(reader), WrittenText.key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        for index, version in enumerate(row.previous):
            if version.get("meaning_hash") == current:
                row.text = version["text"]
                row.meaning_hash = current
                row.written_at = datetime.fromisoformat(version["written_at"])
                row.previous = row.previous[index + 1 :]
                restored.append(key)
                break
    await session.flush()
    return restored


async def languages_written_since(session: AsyncSession, since: datetime) -> list[str]:
    """The languages whose version was begun since ``since`` (the earliest text of the language
    is not older), the oldest first."""
    begun = func.min(WrittenText.written_at)
    rows = await session.execute(
        select(WrittenText.language)
        .where(WrittenText.language.is_not(None))
        .group_by(WrittenText.language)
        .having(begun >= since)
        .order_by(begun, WrittenText.language)
    )
    return [language for language in rows.scalars() if language is not None]
