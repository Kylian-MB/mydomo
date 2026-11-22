"""The local model that gives the meaning of memories (spec 6.5, task 3).

The real model is never used here (the core's image holds it: ``tests/stack``); what is tried is
how the embedder behaves without it, around it and when it fails."""

import logging
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path

import anyio
import pytest
from domo_core.config import CoreSettings
from domo_core.memory.embedder import (
    DEFAULT_MODEL,
    NO_EMBEDDER,
    RETRY_AFTER,
    LocalEmbedder,
)


class Clock:
    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds


class FakeModel:
    """Stands for ``fastembed.TextEmbedding``: a vector of 384 numbers per text, the texts kept."""

    def __init__(self, size: int = 384) -> None:
        self.size = size
        self.seen: list[str] = []

    def embed(self, texts: Iterable[str]) -> Iterator[list[float]]:
        for text in texts:
            self.seen.append(text)
            yield [float(len(text))] * self.size


def unavailable(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "local model is unavailable" in r.getMessage()]


async def test_no_embedder_always_answers_none():
    assert NO_EMBEDDER.name == ""
    assert await NO_EMBEDDER.passages(["a"]) is None
    assert await NO_EMBEDDER.query("a") is None


async def test_the_local_embedder_without_its_files_answers_none(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    empty = LocalEmbedder(tmp_path, clock=clock)
    missing = LocalEmbedder(tmp_path / "nowhere", clock=clock)
    for embedder in (empty, missing):
        assert await embedder.passages(["Rex is Alex's dog"]) is None
        assert await embedder.query("my pet") is None
        assert await embedder.passages(["Rex is Alex's dog"]) is None
    # Noted once for each, by the kind of error alone, and not tried again within the hour.
    records = unavailable(caplog)
    assert len(records) == 2
    assert all("FileNotFoundError" in r.getMessage() for r in records)
    assert all(str(tmp_path) not in r.getMessage() for r in records)


async def test_a_failed_load_is_tried_again_after_an_hour(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    embedder = LocalEmbedder(tmp_path, clock=clock)
    assert await embedder.passages(["a"]) is None
    clock.seconds += RETRY_AFTER.total_seconds() - 1
    assert await embedder.passages(["a"]) is None
    assert len(unavailable(caplog)) == 1

    model = FakeModel()
    monkeypatch.setattr(LocalEmbedder, "_load_model", lambda self: model)
    assert await embedder.passages(["a"]) is None  # still within the hour
    clock.seconds += 1
    assert await embedder.passages(["a"]) == [[1.0] * 384]
    vector = await embedder.query("my pet")
    assert vector is not None and len(vector) == 384


async def test_the_model_is_loaded_once_and_only_in_the_background(tmp_path, monkeypatch):
    loads: list[FakeModel] = []

    def load(self: LocalEmbedder) -> FakeModel:
        loads.append(FakeModel())
        return loads[-1]

    monkeypatch.setattr(LocalEmbedder, "_load_model", load)
    embedder = LocalEmbedder(tmp_path)
    assert loads == []
    assert await embedder.passages([]) == []
    assert loads == []
    embedder.warm()
    vectors = await embedder.passages(["a", "bb"])
    assert vectors == [[1.0] * 384, [2.0] * 384]
    assert await embedder.query("ccc") == [3.0] * 384
    embedder.warm()
    assert len(loads) == 1


async def test_a_query_never_waits_for_a_slow_load(tmp_path, monkeypatch):
    release = threading.Event()

    def slow_load(self: LocalEmbedder) -> FakeModel:
        release.wait(10)
        return FakeModel()

    monkeypatch.setattr(LocalEmbedder, "_load_model", slow_load)
    embedder = LocalEmbedder(tmp_path)
    embedder.warm()
    with anyio.fail_after(1):
        assert await embedder.query("my pet") is None
    release.set()
    assert await embedder.passages(["a"]) == [[1.0] * 384]
    assert await embedder.query("my pet") == [6.0] * 384


async def test_a_query_never_waits_behind_slow_passages(tmp_path, monkeypatch):
    release = threading.Event()

    class SlowOnSome(FakeModel):
        def embed(self, texts: Iterable[str]) -> Iterator[list[float]]:
            texts = list(texts)
            if "slow" in texts:
                release.wait(10)
            return super().embed(texts)

    monkeypatch.setattr(LocalEmbedder, "_load_model", lambda self: SlowOnSome())
    embedder = LocalEmbedder(tmp_path)
    assert await embedder.passages(["a"]) == [[1.0] * 384]
    async with anyio.create_task_group() as group:
        group.start_soon(embedder.passages, ["slow"])
        await anyio.sleep(0.2)  # the passages are being computed
        with anyio.fail_after(1):
            assert await embedder.query("fast") == [4.0] * 384
        release.set()


async def test_a_text_the_model_fails_on_does_not_turn_it_off(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)

    class FailsOnOdd(FakeModel):
        def embed(self, texts: Iterable[str]) -> Iterator[list[float]]:
            texts = list(texts)
            if "odd" in texts:
                raise RuntimeError("the input is odd")
            return super().embed(texts)

    monkeypatch.setattr(LocalEmbedder, "_load_model", lambda self: FailsOnOdd())
    embedder = LocalEmbedder(tmp_path)
    # The background work is told which batch failed, to find the text (memory.search).
    with pytest.raises(RuntimeError):
        await embedder.passages(["odd"])
    assert await embedder.query("odd") is None
    assert await embedder.query("odd") is None
    # The model still answers for every other text.
    assert await embedder.passages(["a"]) == [[1.0] * 384]
    assert await embedder.query("ab") == [2.0] * 384
    assert unavailable(caplog) == []
    failed = [r for r in caplog.records if "local model failed" in r.getMessage()]
    assert len(failed) == 1 and "RuntimeError" in failed[0].getMessage()
    assert "odd" not in failed[0].getMessage()


async def test_vectors_of_another_size_are_not_given(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalEmbedder, "_load_model", lambda self: FakeModel(size=1024))
    embedder = LocalEmbedder(tmp_path)
    with pytest.raises(ValueError):
        await embedder.passages(["a"])
    assert await embedder.query("a") is None


async def test_the_e5_models_are_given_their_prefixes(tmp_path, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(LocalEmbedder, "_load_model", lambda self: model)
    e5 = LocalEmbedder(tmp_path, "intfloat/multilingual-e5-small")
    await e5.passages(["Rex is Alex's dog"])
    await e5.query("my pet")
    assert model.seen == ["passage: Rex is Alex's dog", "query: my pet"]
    model.seen.clear()
    shipped = LocalEmbedder(tmp_path)
    assert shipped.name == DEFAULT_MODEL
    await shipped.passages(["Rex is Alex's dog"])
    await shipped.query("my pet")
    assert model.seen == ["Rex is Alex's dog", "my pet"]


async def test_stop_gives_up_a_load_in_progress(tmp_path, monkeypatch):
    release = threading.Event()

    def slow_load(self: LocalEmbedder) -> FakeModel:
        release.wait(10)
        return FakeModel()

    monkeypatch.setattr(LocalEmbedder, "_load_model", slow_load)
    embedder = LocalEmbedder(tmp_path)
    embedder.warm()
    with anyio.fail_after(1):
        await embedder.stop()
    release.set()
    # Another load may begin later.
    assert await embedder.passages(["a"]) == [[1.0] * 384]


def test_importing_the_embedder_does_not_load_the_model_library():
    found = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, domo_core.memory.embedder, domo_core.web.app; "
            "print('fastembed' in sys.modules, 'onnxruntime' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert found.stdout.split() == ["False", "False"]


def test_the_folder_of_the_model_is_the_image_s_unless_set(monkeypatch):
    assert CoreSettings().embedding_model_dir == Path("/opt/domo/models")
    monkeypatch.setenv("DOMO_EMBEDDING_MODEL_DIR", "/elsewhere")
    assert CoreSettings().embedding_model_dir == Path("/elsewhere")
