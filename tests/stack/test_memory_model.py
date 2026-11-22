"""The local model that gives the meaning of memories is in the core's image (spec 6.5): the
core, without any way to the Internet and with a read-only root, computes a vector with it.

The vector is asked as a memory's (``passages``), which waits for the model to load: a query
never waits for it (it answers ``None`` while the model loads, and the words answer alone)."""

import pytest

from .stack_support import StackInfo

pytestmark = pytest.mark.stack

EMBED = (
    "import asyncio, logging\n"
    "from domo_core.config import CoreSettings\n"
    "from domo_core.memory.embedder import LocalEmbedder\n"
    "logging.basicConfig()\n"
    "embedder = LocalEmbedder(CoreSettings().embedding_model_dir)\n"
    "vectors = asyncio.run(embedder.passages(['Rex is a dog']))\n"
    "print(len(vectors[0]) if vectors else 'none')\n"
)


def test_the_image_holds_the_model(stack: StackInfo):
    found = stack.exec("core", ["python", "-c", EMBED])
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip().splitlines()[-1] == "384", found.stderr
