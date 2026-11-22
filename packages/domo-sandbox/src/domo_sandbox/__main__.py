"""Serves the launcher on a Unix socket: ``python -m domo_sandbox``."""

import asyncio
import logging
import os
import socket
import sys
from pathlib import Path

import uvicorn

from domo_sandbox import limits
from domo_sandbox.launcher import create_app
from domo_sandbox.limits import UID_RANGE
from domo_sandbox.runner import check_runs_can_be_ended

logger = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/domo/sandbox/launcher.sock"
DEFAULT_SLOTS = 4
# The core's group (compose.yaml runs the core as 10001:10001): the socket is its and root's alone.
DEFAULT_CORE_GID = 10001
# Read and write for the owner (root) and the group (the core), nothing for anyone else.
SOCKET_UMASK = 0o117


def _slots() -> int:
    """How many runs go at once until the core says (``PUT /places``: Domo's setting
    ``places``, sent when the core starts and when he changes it)."""
    raw = os.environ.get("DOMO_SANDBOX_SLOTS", "")
    try:
        return max(1, int(raw)) if raw else DEFAULT_SLOTS
    except ValueError:
        raise SystemExit("DOMO_SANDBOX_SLOTS must be a whole number") from None


def _core_gid() -> int:
    """The core's group, which the socket is given: never root's (0), nor a group of the runs'
    accounts (``limits.UID_RANGE``: each run's group is its uid), which would open the socket to a
    run."""
    raw = os.environ.get("DOMO_SANDBOX_CORE_GID", "")
    try:
        gid = int(raw) if raw else DEFAULT_CORE_GID
    except ValueError:
        raise SystemExit("DOMO_SANDBOX_CORE_GID must be a whole number") from None
    if gid <= 0 or gid in UID_RANGE:
        raise SystemExit(
            "DOMO_SANDBOX_CORE_GID must be the core's group: not 0, nor one of "
            f"{UID_RANGE.start} to {UID_RANGE.stop - 1}, the runs' own"
        )
    return gid


def bind_socket(path: Path, core_gid: int) -> socket.socket:
    """The launcher's socket, bound at ``path``: owned by root and the core's group, mode 0660,
    so that the core (in that group) and root reach it, and no run (an account of its own, in no
    other group) can connect to it. Made with that group and mode from the start: the effective gid
    is the core's and the umask 0117 while it is bound (the launcher has no capability to chown),
    then both are given back. A stale socket is removed first."""
    if sys.platform == "win32":
        raise SystemExit("the launcher serves its socket on Linux only")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    gid, umask = os.getegid(), os.umask(SOCKET_UMASK)
    try:
        os.setegid(core_gid)
        try:
            server.bind(str(path))
        finally:
            os.setegid(gid)
    except BaseException:
        server.close()
        raise
    finally:
        os.umask(umask)
    return server


async def _serve(path: Path, slots: int, core_gid: int) -> None:
    # The socket is bound here and handed to uvicorn, which then neither creates it nor changes
    # its mode (it would make a socket of its own 0666: open to every run).
    server = bind_socket(path, core_gid)
    config = uvicorn.Config(
        create_app(slots=slots),
        access_log=False,
        server_header=False,
        log_level="warning",
        lifespan="off",
        http="h11",
        ws="none",
    )
    await uvicorn.Server(config).serve(sockets=[server])


async def _start(path: Path, slots: int, core_gid: int) -> None:
    try:
        await check_runs_can_be_ended()
    except (RuntimeError, OSError) as exc:
        logger.critical(
            "the sandbox does not start: %s (it needs the SETUID, SETGID capabilities)", exc
        )
        raise SystemExit(1) from None
    await _serve(path, slots, core_gid)


def _confined_or_told() -> None:
    """A launcher that is not root cannot give each run an account of its own: the runs would
    share its account, reach its socket and outlive their end. It then starts only when told so
    plainly (``DOMO_SANDBOX_UNCONFINED=yes``, for a trial on a developer's machine), never by
    default (final review m6)."""
    if limits.can_switch_account():
        return
    if os.environ.get("DOMO_SANDBOX_UNCONFINED", "") != "yes":
        raise SystemExit(
            "the launcher is not root, so its runs would not be kept apart from it: run it as "
            "root (with the SETUID and SETGID capabilities), or set "
            "DOMO_SANDBOX_UNCONFINED=yes to run them unconfined anyway"
        )
    logger.warning("the runs are not confined: each runs under the launcher's own account")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    _confined_or_told()
    path = Path(os.environ.get("DOMO_SANDBOX_SOCKET", DEFAULT_SOCKET))
    asyncio.run(_start(path, _slots(), _core_gid()))


if __name__ == "__main__":
    main()
