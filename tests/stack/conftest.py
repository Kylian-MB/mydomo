"""Fixtures of the stack tests: the whole Docker Compose stack, started once per test session.

``stack`` builds the images from the working tree and runs
``docker compose -p domo-test --env-file tests/stack/test.env -f compose.yaml up -d --build
--wait``; at the end of the session the stack is removed with its volumes (``down -v``), even
when tests fail. See ``stack_support.py`` (``running_stack`` takes more compose files and more
environment, for variants of the stack).

``going_out_stack`` is the variant of the tests of going out (``test_going_out.py``): the stack
plus the fakes of ``compose.going-out.yaml``, in a project of its own (``domo-test-out``,
``going_out.env``), for one module.

These tests need a Docker engine (28 or later, with Compose) that runs Linux containers. They are
skipped where it only runs Windows containers, and fail when there is none.
"""

from collections.abc import Iterator

import pytest

from .stack_support import GoingOutStack, StackInfo, running_stack, running_stack_going_out

# The name the fake site is reached by, and its certificate made for (compose.going-out.yaml).
SITE_NAME = "site"


@pytest.fixture(scope="session")
def stack() -> Iterator[StackInfo]:
    with running_stack() as info:
        yield info


@pytest.fixture(scope="module")
def going_out_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GoingOutStack]:
    """The stack with the fakes of going out (a device on another machine, a device on a port of
    this machine, a site with a login), in a project of its own, for one module: it is taken down
    when the module's tests are over."""
    with running_stack_going_out(tmp_path_factory.mktemp("going-out"), SITE_NAME) as info:
        yield info
