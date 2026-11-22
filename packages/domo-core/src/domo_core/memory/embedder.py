"""What turns a text into a vector of its meaning, for finding memories by meaning (spec 6.5).

The core has no way to the Internet: the model is a small local one (``fastembed``), downloaded
when the core's image is built (``fetch_model``) and read from ``CoreSettings.embedding_model_dir``,
never from the network. It is loaded at its first use, in a thread, never when this module is
imported; computing a vector runs in a thread too. Anything that goes wrong (the folder is not
there, the library cannot be imported, the model fails) is noted once in the log, by the kind of
error alone, and the embedder answers ``None`` until ``RETRY_AFTER`` has passed: the search then
works by the words alone.

``NO_EMBEDDER`` is the embedder of a core without one (the tests, a turn that is given none): it
always answers ``None``.
"""

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import anyio
import anyio.to_thread

from domo_core.memory.models import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

# The model shipped in the core's image. ``intfloat/multilingual-e5-small``, the one the spec
# names, is not among the models of the version of ``fastembed`` the lock allows; this one is
# multilingual too and gives vectors of the same size.
DEFAULT_MODEL: Final = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# How long an embedder that failed answers ``None`` before it tries again.
RETRY_AFTER: Final = timedelta(hours=1)


class Embedder(Protocol):
    """Turns texts into vectors of ``dimensions`` numbers. ``name`` is the model's (``""`` when
    there is none): only vectors of the same model are compared. ``None`` means it is not
    available now."""

    name: str
    dimensions: int

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        """The vectors of ``texts`` as things to be found (memories), in their order."""
        ...

    async def query(self, text: str) -> list[float] | None:
        """The vector of ``text`` as what is looked for."""
        ...


class _NoEmbedder:
    """No model: every answer is ``None``."""

    name: str = ""
    dimensions: int = 0

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        del texts
        return None

    async def query(self, text: str) -> list[float] | None:
        del text
        return None


NO_EMBEDDER: Final[Embedder] = _NoEmbedder()


def _uses_prefixes(name: str) -> bool:
    """Whether the model wants ``passage: `` and ``query: `` before the texts (the e5 models)."""
    return "/multilingual-e5-" in name or "/e5-" in name


def fetch_model(model_dir: Path, name: str = DEFAULT_MODEL) -> None:
    """Download the model ``name`` into ``model_dir``, the way ``LocalEmbedder`` reads it. For the
    building of the core's image only: the running core never downloads anything."""
    from fastembed import TextEmbedding

    TextEmbedding(model_name=name, cache_dir=str(model_dir), lazy_load=True)


class LocalEmbedder:
    """The local model ``name``, read from ``model_dir`` without ever trying the network.

    A turn never waits for it: ``query`` answers ``None`` at once (the words then answer alone)
    while the model is not loaded, and starts loading it in the background (``warm``, which the
    application calls when it starts). Only the background work (``passages``) waits for the load.
    Once loaded, computing vectors needs no lock: a query never waits behind a batch of passages.

    A load that fails is noted once in the log, by the kind of error, and is not tried again
    before ``RETRY_AFTER``: the embedder answers ``None`` meanwhile. A failure on given texts (an
    odd input, a vector of another size) says nothing of the model: ``passages`` raises it, for
    the caller to tell which text it is (``memory.search.embed_missing``), and ``query`` answers
    ``None`` for that text alone. ``clock`` gives seconds that only go forward
    (``time.monotonic``)."""

    def __init__(
        self,
        model_dir: Path,
        name: str = DEFAULT_MODEL,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.dimensions = EMBEDDING_DIMENSIONS
        self._model_dir = model_dir
        self._clock = clock
        self._model: TextEmbedding | None = None
        self._failed_at: float | None = None
        self._loading: asyncio.Task[None] | None = None
        self._noted: set[str] = set()

    def warm(self) -> None:
        """Start loading the model in the background, unless it is loaded, loading, or failed
        less than ``RETRY_AFTER`` ago. Never waits."""
        if self._model is not None or (self._loading is not None and not self._loading.done()):
            return
        if self._failed_at is not None:
            if self._clock() < self._failed_at + RETRY_AFTER.total_seconds():
                return
            self._failed_at = None
        self._loading = asyncio.get_running_loop().create_task(self._load())

    async def stop(self) -> None:
        """Give up a load in progress (the application stops)."""
        if self._loading is not None and not self._loading.done():
            self._loading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loading

    async def passages(self, texts: Sequence[str]) -> list[list[float]] | None:
        if not texts:
            return []
        self.warm()
        if self._model is None and self._loading is not None:
            # Shielded: a caller that is cancelled does not cancel the load.
            await asyncio.shield(self._loading)
        model = self._model
        if model is None:
            return None
        prefix = "passage: " if _uses_prefixes(self.name) else ""
        return await anyio.to_thread.run_sync(
            self._vectors, model, [prefix + text for text in texts]
        )

    async def query(self, text: str) -> list[float] | None:
        model = self._model
        if model is None:
            self.warm()
            return None
        prefix = "query: " if _uses_prefixes(self.name) else ""
        try:
            # A turn that stops waiting (``memory.search.QUERY_SECONDS``) does not wait for the
            # thread either: it is left to finish on its own.
            vectors = await anyio.to_thread.run_sync(
                self._vectors, model, [prefix + text], abandon_on_cancel=True
            )
        except Exception as error:
            self._note(error, "a query could not be turned into a vector")
            return None
        return vectors[0]

    def _note(self, error: Exception, what: str) -> None:
        kind = type(error).__name__
        if kind not in self._noted:
            self._noted.add(kind)
            logger.warning("The local model failed: %s (%s)", what, kind)

    async def _load(self) -> None:
        try:
            self._model = await anyio.to_thread.run_sync(self._load_model, abandon_on_cancel=True)
        except Exception as error:
            self._failed_at = self._clock()
            logger.warning(
                "The local model is unavailable (%s); memories are found by their words "
                "alone until it is tried again in an hour",
                type(error).__name__,
            )

    def _load_model(self) -> "TextEmbedding":
        """The model, read from its folder alone (in a thread: it takes a moment)."""
        if not self._model_dir.is_dir() or not any(self._model_dir.iterdir()):
            raise FileNotFoundError("The folder of the local model is empty or missing.")
        _quiet_onnxruntime()
        from fastembed import TextEmbedding

        return TextEmbedding(
            model_name=self.name, cache_dir=str(self._model_dir), local_files_only=True
        )

    def _vectors(self, model: "TextEmbedding", texts: list[str]) -> list[list[float]]:
        vectors = [[float(number) for number in vector] for vector in model.embed(texts)]
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            raise ValueError("The local model gave vectors of another size.")
        return vectors


def _quiet_onnxruntime() -> None:
    """No telemetry, and only its errors in the core's log: onnxruntime otherwise warns, when it
    is imported, that it cannot keep a telemetry id on a read-only root. The variable is read at
    that import (the image sets it as well)."""
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
    import onnxruntime  # pyright: ignore[reportMissingTypeStubs]

    onnxruntime.disable_telemetry_events()  # pyright: ignore[reportUnknownMemberType]
    onnxruntime.set_default_logger_severity(3)  # pyright: ignore[reportUnknownMemberType]
