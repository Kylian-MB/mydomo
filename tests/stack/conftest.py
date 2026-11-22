"""Fixtures of the stack tests: the whole Docker Compose stack, started once per test session.

``stack`` builds the images from the working tree and runs
``docker compose -p domo-test --env-file tests/stack/test.env -f compose.yaml up -d --build
--wait``; at the end of the session the stack is removed with its volumes (``down -v``), even
when tests fail. See ``stack_support.py`` (``running_stack`` takes more compose files and more
environment, for variants of the stack).

These tests need a Docker engine (28 or later, with Compose) that runs Linux containers. They are
skipped where it only runs Windows containers, and fail when there is none.
"""

from collections.abc import Iterator

import pytest

from .stack_support import StackInfo, running_stack


@pytest.fixture(scope="session")
def stack() -> Iterator[StackInfo]:
    with running_stack() as info:
        yield info
