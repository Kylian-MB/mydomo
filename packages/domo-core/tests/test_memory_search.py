"""Finding memories by their words and by their meaning (spec 6.5, task 3).

The meaning comes from a ``FakeEmbedder`` whose vectors the test fixes: the real model is only
used by the image's own test (``tests/stack``)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from domo_core.accounts.models import Person
from domo_core.memory.embedder import NO_EMBEDDER
from domo_core.memory.models import Memory
from domo_core.memory.search import embed_missing, failed_mark, recall, search
from domo_core.memory.service import change, keep
from domo_core.upkeep import VECTORS_PER_PASS
from sqlalchemy import select
from sqlalchemy.orm import undefer

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
SIZE = 384


def axis(index: int, lean: float = 0.0) -> list[float]:
    """A vector along ``index``, leaning a little towards the next axis."""
    vector = [0.0] * SIZE
    vector[index] = 1.0
    vector[index + 1] = lean
    return vector


ELSEWHERE = axis(300)


class FakeEmbedder:
    """The vector of a text is the one of the first key it contains (``ELSEWHERE`` otherwise)."""

    name = "fake-model"
    dimensions = SIZE

    def __init__(self, vectors: Mapping[str, list[float]]) -> None:
        self.vectors = vectors
        self.passages_asked: list[str] = []

    def _of(self, text: str) -> list[float]:
        return next((v for key, v in self.vectors.items() if key in text), ELSEWHERE)

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        self.passages_asked.extend(texts)
        return [self._of(text) for text in texts]

    async def query(self, text: str) -> list[float] | None:
        return self._of(text)


class BrokenEmbedder:
    name = "broken-model"
    dimensions = SIZE

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        raise RuntimeError("the model is gone")

    async def query(self, text: str) -> list[float] | None:
        raise RuntimeError("the model is gone")


async def write(
    session,
    said_by: Person,
    content: str,
    *,
    subject: str = "home",
    kept_private: bool = False,
    served_at: datetime | None = None,
    vector: list[float] | None = None,
    model: str = FakeEmbedder.name,
) -> Memory:
    memory = await keep(
        session,
        said_by=said_by.id,
        subject=subject,
        content=content,
        kind="fact",
        origin="own_words",
        kept_private=kept_private,
        core=False,
        now=NOW,
    )
    memory.served_at = served_at
    if vector is not None:
        memory.embedding = vector
        memory.embedding_model = model
    await session.flush()
    return memory


def contents(memories: Sequence[Memory]) -> list[str]:
    return [memory.content for memory in memories]


async def test_words_alone_find_a_memory_when_there_is_no_embedder(session, add_person):
    alex = await add_person("Alex", "owner")
    await write(session, alex, "Rex is Alex's dog", subject="Rex")
    await write(session, alex, "the boiler is noisy", subject="boiler")
    # One of the words is enough.
    found = await search(session, alex.id, "any dog here?", embedder=NO_EMBEDDER)
    assert contents(found) == ["Rex is Alex's dog"]
    assert contents(
        await recall(session, alex.id, "a cat, a dog", embedder=NO_EMBEDDER, max_chars=500)
    ) == ["Rex is Alex's dog"]
    assert await search(session, alex.id, "my pet", embedder=NO_EMBEDDER) == []
    assert await search(session, alex.id, " ?! ", embedder=NO_EMBEDDER) == []


async def test_meaning_finds_what_words_miss(session, add_person):
    alex = await add_person("Alex", "owner")
    embedder = FakeEmbedder({"pet": axis(0, 0.1)})
    await write(session, alex, "Rex is Alex's dog", subject="Rex", vector=axis(0))
    await write(session, alex, "the boiler is noisy", subject="boiler", vector=axis(5))
    found = await search(session, alex.id, "my pet", embedder=embedder)
    assert contents(found)[0] == "Rex is Alex's dog"
    recalled = await recall(session, alex.id, "my pet", embedder=embedder, max_chars=20)
    assert contents(recalled) == ["Rex is Alex's dog"]


async def test_vectors_of_another_model_are_not_compared(session, add_person):
    alex = await add_person("Alex", "owner")
    embedder = FakeEmbedder({"pet": axis(0)})
    await write(session, alex, "Rex is Alex's dog", subject="Rex", vector=axis(0), model="old")
    assert await search(session, alex.id, "my pet", embedder=embedder) == []


async def test_both_legs_together_come_first(session, add_person):
    alex = await add_person("Alex", "owner")
    embedder = FakeEmbedder({"dog": axis(0)})
    # Found by the words alone, by the meaning alone, and by both.
    await write(session, alex, "the dog bowl is blue", subject="bowl", vector=axis(9))
    await write(session, alex, "Rex sleeps upstairs", subject="Rex", vector=axis(0, 0.2))
    await write(session, alex, "Rex is a dog", subject="Rex", vector=axis(0))
    found = await search(session, alex.id, "dog", embedder=embedder)
    assert contents(found)[0] == "Rex is a dog"


async def test_at_equal_score_the_most_recently_served_comes_first(session, add_person):
    alex = await add_person("Alex", "owner")
    await write(session, alex, "likes tea", subject="tea", served_at=NOW - timedelta(days=2))
    await write(session, alex, "hates tea", subject="tea", served_at=NOW - timedelta(days=1))
    await write(session, alex, "buys tea", subject="tea")
    found = await search(session, alex.id, "tea", embedder=NO_EMBEDDER)
    # Never served counts from when it was written (NOW).
    assert contents(found) == ["buys tea", "hates tea", "likes tea"]
    assert contents(await search(session, alex.id, "tea", embedder=NO_EMBEDDER, limit=2)) == [
        "buys tea",
        "hates tea",
    ]


async def test_someone_elses_private_memory_is_never_found_by_either_leg_unless_asked(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    sam = await add_person("Sam", "adult")
    secret = "the surprise party for Sam is on Friday"
    embedder = FakeEmbedder({"surprise": axis(0)})
    await write(session, alex, secret, subject="party", kept_private=True, vector=axis(0))
    for leg in (NO_EMBEDDER, embedder):
        assert await recall(session, sam.id, secret, embedder=leg, max_chars=1000) == []
        assert await search(session, sam.id, secret, embedder=leg) == []
        assert contents(await search(session, sam.id, secret, embedder=leg, with_kept=True)) == [
            secret
        ]
        # Who said it sees it in their own turn.
        assert contents(await recall(session, alex.id, secret, embedder=leg, max_chars=1000)) == [
            secret
        ]


async def test_recall_stops_at_its_size_and_skips_what_the_core_already_gave(session, add_person):
    alex = await add_person("Alex", "owner")
    given = await write(session, alex, "likes green tea", subject="tea", served_at=NOW)
    await write(session, alex, "drinks tea at four", subject="tea", served_at=NOW - timedelta(1))
    await write(session, alex, "hates tea with milk", subject="tea", served_at=NOW - timedelta(2))
    recalled = await recall(
        session, alex.id, "tea", embedder=NO_EMBEDDER, max_chars=30, exclude={given.id}
    )
    # 3 + 18 fit in 30; the next one (3 + 19) does not.
    assert contents(recalled) == ["drinks tea at four"]
    everything = await recall(session, alex.id, "tea", embedder=NO_EMBEDDER, max_chars=1000)
    assert len(everything) == 3
    assert await recall(session, alex.id, "tea", embedder=NO_EMBEDDER, max_chars=5) == []


async def test_a_broken_embedder_leaves_the_search_working(session, add_person):
    alex = await add_person("Alex", "owner")
    await write(session, alex, "Rex is Alex's dog", subject="Rex")
    found = await search(session, alex.id, "dog", embedder=BrokenEmbedder())
    assert contents(found) == ["Rex is Alex's dog"]
    recalled = await recall(session, alex.id, "dog", embedder=BrokenEmbedder(), max_chars=100)
    assert contents(recalled) == ["Rex is Alex's dog"]


async def embedding_of(session_factory, memory_id) -> tuple[list[float] | None, str | None]:
    async with session_factory() as session:
        memory = await session.scalar(
            select(Memory)
            .where(Memory.id == memory_id)
            .options(undefer(Memory.embedding), undefer(Memory.embedding_model))
        )
        assert memory is not None
        return memory.embedding, memory.embedding_model


async def committed_memories(session_factory, count: int) -> tuple[Person, list[Memory]]:
    """Alex and ``count`` memories of theirs, committed; the first one is about Rex."""
    async with session_factory() as session:
        alex = Person(display_name="Alex", role="owner")
        session.add(alex)
        await session.flush()
        memories = [
            await keep(
                session,
                said_by=alex.id,
                subject="Rex" if n == 0 else f"thing {n}",
                content="Rex is Alex's dog" if n == 0 else f"fact number {n}",
                kind="fact",
                origin="own_words",
                kept_private=False,
                core=False,
                now=NOW,
            )
            for n in range(count)
        ]
        await session.commit()
    return alex, memories


class UnavailableEmbedder:
    name = "absent-model"
    dimensions = SIZE

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        return None

    async def query(self, text: str) -> list[float] | None:
        return None


class FailsOnRex(FakeEmbedder):
    """Fails on any batch that holds the text about Rex."""

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        if any("Rex" in text for text in texts):
            raise RuntimeError("an odd text")
        return await super().passages(texts)


async def test_embed_missing_stops_without_raising_when_the_model_is_not_there(
    session_factory,
):
    _, memories = await committed_memories(session_factory, 2)
    assert await embed_missing(session_factory, UnavailableEmbedder()) == 0
    assert await embedding_of(session_factory, memories[0].id) == (None, None)


async def test_a_memory_the_model_fails_on_is_skipped_and_not_tried_again(session_factory):
    alex, memories = await committed_memories(session_factory, 3)
    rex_id = memories[0].id
    embedder = FailsOnRex({})
    # The batch fails, so it is tried text by text: the two others get their vector.
    assert await embed_missing(session_factory, embedder) == 2
    assert await embedding_of(session_factory, rex_id) == (None, failed_mark("fake-model"))
    assert await embedding_of(session_factory, memories[1].id) == (ELSEWHERE, "fake-model")
    # Not tried again at the next pass, and never found by the meaning.
    embedder.passages_asked.clear()
    assert await embed_missing(session_factory, embedder) == 0
    assert embedder.passages_asked == []
    # New words: tried again.
    async with session_factory() as session:
        rex = await session.get(Memory, rex_id)
        assert rex is not None
        await change(session, rex, by=alex.id, now=NOW, content="Rex is a good boy")
        await session.commit()
    assert await embed_missing(session_factory, FakeEmbedder({})) == 1
    assert await embedding_of(session_factory, rex_id) == (ELSEWHERE, "fake-model")


async def test_embed_missing_works_off_a_backlog_a_bounded_number_at_a_time(session_factory):
    # Final review M7: one round of the upkeep computes VECTORS_PER_PASS at most.
    assert VECTORS_PER_PASS == 200
    await committed_memories(session_factory, 5)
    embedder = FakeEmbedder({})
    assert await embed_missing(session_factory, embedder, batch=2, limit=3) == 3
    assert len(embedder.passages_asked) == 3
    assert await embed_missing(session_factory, embedder, batch=2, limit=3) == 2
    assert await embed_missing(session_factory, embedder, limit=3) == 0


async def test_embed_missing_fills_the_vectors_later(session_factory):
    alex, memories = await committed_memories(session_factory, 5)
    rex_id = memories[0].id
    # Written without a vector: computed outside the turn.
    assert await embedding_of(session_factory, rex_id) == (None, None)
    embedder = FakeEmbedder({"Rex": axis(0)})
    assert await embed_missing(session_factory, embedder, batch=2) == 5
    assert await embedding_of(session_factory, rex_id) == (axis(0), "fake-model")
    assert "Rex: Rex is Alex's dog" in embedder.passages_asked
    assert await embed_missing(session_factory, embedder) == 0
    assert await embed_missing(session_factory, NO_EMBEDDER) == 0

    # A new content empties the vector; the next call computes it again.
    async with session_factory() as session:
        rex = await session.get(Memory, rex_id)
        assert rex is not None
        await change(session, rex, by=alex.id, now=NOW, content="Rex is Sam's dog now")
        await session.commit()
    assert await embedding_of(session_factory, rex_id) == (None, None)
    assert await embed_missing(session_factory, embedder) == 1

    # A vector of another model is computed again with this one.
    other = FakeEmbedder({"Rex": axis(7)})
    other.name = "other-model"
    assert await embed_missing(session_factory, other) == 5
    assert await embedding_of(session_factory, rex_id) == (axis(7), "other-model")
