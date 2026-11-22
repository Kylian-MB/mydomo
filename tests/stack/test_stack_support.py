"""The helpers of the stack tests themselves (``stack_support.py``)."""

import pytest

from .stack_support import StackInfo, docker_environment

pytestmark = pytest.mark.stack


def test_a_log_that_cannot_be_read_is_an_error_not_an_empty_log():
    """A test that checks that something is absent from a log must not pass on a log that
    `docker compose logs` failed to give."""
    nowhere = StackInfo(
        base_url="https://localhost:1",
        http_url="http://localhost:1",
        project="domo-nothing",
        compose_command=("docker", "compose", "-p", "domo-nothing", "-f", "no-such-file.yaml"),
        env=docker_environment(),
    )
    with pytest.raises(AssertionError, match="docker compose logs core"):
        nowhere.logs("core")
