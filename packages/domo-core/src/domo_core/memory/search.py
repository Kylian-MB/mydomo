"""Finding memories, by their words and by their meaning (spec 6.5).

Two legs, both in PostgreSQL, both filtered in the query itself by what the person's turn may see
(``memory.service.visible_to``) before anything is ranked: a memory Domo keeps for himself never
reaches another person's turn by either leg, unless he asks for it on purpose (``search`` with
``with_kept``). The words leg is the full-text search (``simple`` configuration), each word of
the query enough on its own; the meaning leg compares the query's vector with the memories'
(``pgvector``, cosine distance), among those of the same model only. The two lists are merged by
reciprocal rank (``1 / (RRF_K + rank)``); at equal score, the most recently served comes first.

Without a model (``NO_EMBEDDER``), when it fails, or when it does not answer within
``QUERY_SECONDS``, the words leg alone answers: nothing here raises or waits because of the
model. The vectors of the memories are computed outside the turns (``embed_missing``), never in
the request that writes them.

Nothing here commits but ``embed_missing``, which works in sessions of its own.
"""

import logging
import re
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

import anyio
from sqlalchemy import ColumnElement, Select, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.db import SessionFactory
from domo_core.memory.embedder import Embedder
from domo_core.memory.models import EMBEDDING_DIMENSIONS, Memory
from domo_core.memory.service import visible_to

logger = logging.getLogger(__name__)

# The constant of the reciprocal rank fusion.
RRF_K: Final = 60
# How many memories each leg brings at most before the two lists are merged.
CANDIDATES: Final = 50
# How many words of a query are searched (the last messages of a person may be long).
MAX_WORDS: Final = 64
# How long a search waits for the vector of its query before it finds by the words alone.
QUERY_SECONDS: Final = 2.0

_WORD: Final = re.compile(r"\w+")
# What starts ``embedding_model`` for a memory the model failed on (``failed_mark``).
FAILED_PREFIX: Final = "failed:"


def _words_query(words: str) -> ColumnElement[str] | None:
    """The full-text query of ``words``: each word on its own, joined by OR. ``None`` without
    any word."""
    found = _WORD.findall(words)[:MAX_WORDS]
    if not found:
        return None
    query: ColumnElement[str] = func.websearch_to_tsquery("simple", literal(found[0]))
    for word in found[1:]:
        query = query.op("||")(func.websearch_to_tsquery("simple", literal(word)))
    return query


def _served() -> ColumnElement[object]:
    return func.coalesce(Memory.served_at, Memory.created_at)


def _filtered(
    statement: Select[uuid.UUID],
    person_id: uuid.UUID,
    with_kept: bool,
    exclude: Collection[uuid.UUID],
    only: ColumnElement[bool] | None = None,
) -> Select[uuid.UUID]:
    statement = statement.where(visible_to(person_id, with_kept=with_kept))
    if only is not None:
        statement = statement.where(only)
    if exclude:
        statement = statement.where(Memory.id.not_in(list(exclude)))
    return statement


async def _by_words(
    session: AsyncSession,
    person_id: uuid.UUID,
    words: str,
    *,
    with_kept: bool,
    exclude: Collection[uuid.UUID],
    only: ColumnElement[bool] | None = None,
) -> list[uuid.UUID]:
    query = _words_query(words)
    if query is None:
        return []
    statement = _filtered(
        select(Memory.id).where(Memory.search.op("@@")(query)), person_id, with_kept, exclude, only
    )
    rank = func.ts_rank(Memory.search, query)
    ranked = statement.order_by(rank.desc(), _served().desc(), Memory.id).limit(CANDIDATES)
    return list((await session.scalars(ranked)).all())


async def _query_vector(embedder: Embedder, words: str) -> list[float] | None:
    """The vector of ``words``, or ``None`` (the words alone then answer) when the model has none,
    fails, or takes more than ``QUERY_SECONDS``: a turn never waits on the local model."""
    if not embedder.name or not words.strip():
        return None
    vector: list[float] | None = None
    try:
        with anyio.move_on_after(QUERY_SECONDS) as waited:
            vector = await embedder.query(words)
    except Exception as error:
        logger.warning(
            "The model of the meanings failed (%s); found by the words alone",
            type(error).__name__,
        )
        return None
    if waited.cancelled_caught:
        logger.warning("The model of the meanings took too long; found by the words alone")
        return None
    if vector is None or len(vector) != EMBEDDING_DIMENSIONS:
        return None
    return vector


async def _by_meaning(
    session: AsyncSession,
    person_id: uuid.UUID,
    vector: list[float] | None,
    *,
    model: str,
    with_kept: bool,
    exclude: Collection[uuid.UUID],
    only: ColumnElement[bool] | None = None,
) -> list[uuid.UUID]:
    if vector is None:
        return []
    distance = Memory.embedding.cosine_distance(vector)
    statement = _filtered(
        select(Memory.id).where(Memory.embedding.is_not(None), Memory.embedding_model == model),
        person_id,
        with_kept,
        exclude,
        only,
    )
    ranked = statement.order_by(distance, _served().desc(), Memory.id).limit(CANDIDATES)
    return list((await session.scalars(ranked)).all())


async def _found(
    session: AsyncSession,
    person_id: uuid.UUID,
    words: str,
    *,
    embedder: Embedder,
    vector: list[float] | None,
    with_kept: bool,
    exclude: Collection[uuid.UUID],
    only: ColumnElement[bool] | None = None,
) -> list[Memory]:
    """The memories both legs found, the best first (``vector``: the one of ``words``, ``None``
    when the model gave none: the words alone answer), among those ``only`` lets through, when
    given, too."""
    legs = (
        await _by_words(session, person_id, words, with_kept=with_kept, exclude=exclude, only=only),
        await _by_meaning(
            session,
            person_id,
            vector,
            model=embedder.name,
            with_kept=with_kept,
            exclude=exclude,
            only=only,
        ),
    )
    scores: dict[uuid.UUID, float] = {}
    for leg in legs:
        for rank, memory_id in enumerate(leg, start=1):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (RRF_K + rank)
    if not scores:
        return []
    memories = (await session.scalars(select(Memory).where(Memory.id.in_(scores)))).all()

    def order(memory: Memory) -> tuple[float, float, str]:
        served = memory.served_at or memory.created_at
        return (-scores[memory.id], -served.timestamp(), str(memory.id))

    return sorted(memories, key=order)


async def search(
    session: AsyncSession,
    person_id: uuid.UUID,
    words: str,
    *,
    embedder: Embedder,
    limit: int = 10,
    with_kept: bool = False,
    only: ColumnElement[bool] | None = None,
) -> list[Memory]:
    """The memories ``words`` find for this person's turn, the best first, ``limit`` at most.
    ``with_kept`` (Domo asks for what he keeps for himself, on purpose) lets every memory in.
    ``only`` narrows both legs further, in the query itself (what the yes-judge may read:
    ``memory.told.for_judge``)."""
    found = await _found(
        session,
        person_id,
        words,
        embedder=embedder,
        vector=await _query_vector(embedder, words),
        with_kept=with_kept,
        exclude=(),
        only=only,
    )
    return found[:limit]


@dataclass(frozen=True)
class Recalled:
    """What a recall found (``memories``, the best first) and whether the meaning leg could
    answer (``by_meaning``: the model gave the vector of the query; else the words alone did)."""

    memories: tuple[Memory, ...]
    by_meaning: bool


async def recall(
    session: AsyncSession,
    person_id: uuid.UUID,
    query: str,
    *,
    embedder: Embedder,
    max_chars: int,
    exclude: Collection[uuid.UUID] = (),
) -> list[Memory]:
    """The memories that touch on ``query`` (the person's last messages) for their turn, never
    what Domo keeps for himself of someone else, nor ``exclude`` (what the core already gave),
    the best first, as many as fit ``max_chars`` (a memory weighs its subject and its content)."""
    found = await recalled(
        session, person_id, query, embedder=embedder, max_chars=max_chars, exclude=exclude
    )
    return list(found.memories)


async def recalled(
    session: AsyncSession,
    person_id: uuid.UUID,
    query: str,
    *,
    embedder: Embedder,
    max_chars: int,
    exclude: Collection[uuid.UUID] = (),
) -> Recalled:
    """``recall``, with whether the meaning leg could answer (the context of a turn says when the
    search works by the words alone)."""
    vector = await _query_vector(embedder, query)
    found = await _found(
        session,
        person_id,
        query,
        embedder=embedder,
        vector=vector,
        with_kept=False,
        exclude=exclude,
    )
    kept: list[Memory] = []
    used = 0
    for memory in found:
        weight = len(memory.subject) + len(memory.content)
        if used + weight > max_chars:
            break
        kept.append(memory)
        used += weight
    return Recalled(memories=tuple(kept), by_meaning=vector is not None)


def _passage(subject: str, content: str) -> str:
    return f"{subject}: {content}"


def failed_mark(model: str) -> str:
    """What ``embedding_model`` says of a memory ``model`` could not turn into a vector (its
    ``embedding`` stays empty): it is not tried again by that model until its words change
    (``memory.service.change`` empties both)."""
    return FAILED_PREFIX + model


async def embed_missing(
    session_factory: SessionFactory,
    embedder: Embedder,
    *,
    batch: int = 32,
    limit: int | None = None,
) -> int:
    """Compute the vectors the memories lack (none yet, or one of another model), ``batch`` at a
    time, each batch committed in a session of its own, ``limit`` memories at most (all of them
    without one). A memory that changed while its vector was computed keeps none (the next call
    computes it). A batch the model fails on is tried again text by text: a memory the model fails
    on alone is marked (``failed_mark``) and skipped from then on. Stops, without raising, as soon
    as the model is not available. The number of vectors written."""
    if not embedder.name:
        return 0
    written = 0
    tried = 0
    after: uuid.UUID | None = None
    while limit is None or tried < limit:
        size = batch if limit is None else min(batch, limit - tried)
        async with session_factory() as session:
            statement = select(Memory.id, Memory.subject, Memory.content).where(
                Memory.embedding_model.is_distinct_from(embedder.name),
                Memory.embedding_model.is_distinct_from(failed_mark(embedder.name)),
            )
            if after is not None:
                statement = statement.where(Memory.id > after)
            rows = (await session.execute(statement.order_by(Memory.id).limit(size))).all()
        if not rows:
            return written
        after = rows[-1].id
        tried += len(rows)
        vectors = await _passage_vectors(embedder, [_passage(r.subject, r.content) for r in rows])
        if vectors is None:
            return written
        async with session_factory() as session:
            for row, vector in zip(rows, vectors, strict=True):
                model = embedder.name if vector is not None else failed_mark(embedder.name)
                result = await session.execute(
                    update(Memory)
                    .where(
                        Memory.id == row.id,
                        Memory.subject == row.subject,
                        Memory.content == row.content,
                    )
                    .values(embedding=vector, embedding_model=model)
                    .returning(Memory.id)
                )
                if vector is not None:
                    written += len(result.all())
            await session.commit()
    return written


async def _passage_vectors(
    embedder: Embedder, texts: Sequence[str]
) -> list[list[float] | None] | None:
    """The vector of each of ``texts``, ``None`` for a text the model fails on; ``None`` when the
    model is not available."""
    try:
        vectors = await embedder.passages(texts)
    except Exception as error:
        if len(texts) == 1:
            logger.warning("A memory could not be turned into a vector (%s)", type(error).__name__)
            return [None]
        # Which text it was is found below, one by one.
    else:
        if vectors is None:
            return None
        if len(vectors) == len(texts):
            return [v if len(v) == EMBEDDING_DIMENSIONS else None for v in vectors]
        if len(texts) == 1:
            return [None]
    found: list[list[float] | None] = []
    for text in texts:
        one = await _passage_vectors(embedder, [text])
        if one is None:
            return None
        found.extend(one)
    return found
