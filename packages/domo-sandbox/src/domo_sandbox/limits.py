"""The limits of one launch, set in the child process just before it starts (Linux only), and
what the machine gives the runs (``machine``: the launcher refuses a run that asks more).

On another system (the tests on a developer's machine) there is nothing to set: the launcher's
own time limit and output cap still apply.
"""

import asyncio
import math
import os
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple, cast

from domo_common.sandbox_api import LONGEST_SECONDS

_MB: Final = 1024 * 1024
# The largest limit the system takes (``rlim_t`` as Python gives it): past it, there is none.
_MOST_A_LIMIT_HOLDS: Final = 2**63 - 1
# Where the launcher's own control group shows what it may use (cgroup v2, or v1's memory).
CGROUP: Final = Path("/sys/fs/cgroup")


class MachineHas(NamedTuple):
    """What the launcher's machine gives its runs in all: memory in megabytes (what the launcher
    uses itself left out) and processors, ``None`` when it cannot tell."""

    memory_mb: int | None
    processors: int | None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _group_memory_mb(cgroup: Path) -> int | None:
    """The memory the control group allows, if it says a figure (``max``: no limit of its
    own)."""
    for name in ("memory.max", "memory/memory.limit_in_bytes"):
        text = _read(cgroup / name)
        if text is not None:
            return int(text) // _MB if text.isdigit() else None
    return None


def _group_processors(cgroup: Path) -> int | None:
    """The processors the control group's quota comes to (``cpu.max``: quota and period), a
    part of one counting as one."""
    text = _read(cgroup / "cpu.max")
    if text is None:
        return None
    quota, _, period = text.partition(" ")
    if not quota.isdigit() or not period.isdigit() or int(period) == 0:
        return None
    return max(1, math.ceil(int(quota) / int(period)))


def _least(first: int | None, second: int | None) -> int | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def machine_of(
    cgroup: Path, *, physical_mb: int | None, cpus: int | None, own_mb: int = 0
) -> MachineHas:
    """What the machine gives the runs: the least of what the control group ``cgroup`` allows and
    what the system has (``physical_mb``, ``cpus``), less what the launcher uses itself
    (``own_mb``: it lives under the same wall)."""
    memory = _least(_group_memory_mb(cgroup), physical_mb)
    return MachineHas(
        memory_mb=None if memory is None else max(0, memory - own_mb),
        processors=_least(_group_processors(cgroup), cpus),
    )


def _own_mb() -> int:
    """The memory the launcher's own process holds now (its resident set), 0 when unknown."""
    text = _read(Path("/proc/self/status"))
    for line in (text or "").splitlines():
        if line.startswith("VmRSS:"):
            kilobytes = line.split()[1]
            return int(kilobytes) // 1024 if kilobytes.isdigit() else 0
    return 0


def _physical_mb() -> int | None:
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:
        return None
    try:
        pages, size = int(sysconf("SC_PHYS_PAGES")), int(sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError):
        return None
    return pages * size // _MB if pages > 0 and size > 0 else None


def _cpus() -> int | None:
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        return len(cast("set[int]", affinity(0))) or None
    return os.cpu_count()


def machine() -> MachineHas:
    """What this machine gives the launcher's runs, read now."""
    return machine_of(CGROUP, physical_mb=_physical_mb(), cpus=_cpus(), own_mb=_own_mb())


def limits_of(*, max_seconds: int, memory_mb: int) -> tuple[int, int | None]:
    """The CPU seconds and the bytes of memory a run is limited to (``None``: no limit, a figure
    past what a limit holds). A time past ``LONGEST_SECONDS`` is that: no clock reaches it."""
    memory = memory_mb * _MB
    return min(max_seconds, LONGEST_SECONDS) + 1, None if memory > _MOST_A_LIMIT_HOLDS else memory


# One account per run, never reused while another run holds it.
UID_RANGE = range(20000, 30000)
# The processes (threads included) an account may hold: the tool, plus a little room.
MAX_PROCESSES = 16


def can_switch_account() -> bool:
    """True when the launcher is root on Linux and so can give each run its own account."""
    if sys.platform == "win32":
        return False
    import os

    return os.geteuid() == 0


def pick_account(taken: set[int]) -> int:
    """A free account of ``UID_RANGE``; the caller records it in ``taken`` while the run lives."""
    while True:
        uid = random.choice(UID_RANGE)
        if uid not in taken:
            return uid


def child_setup(
    *, max_seconds: int, memory_mb: int, own_account: bool
) -> Callable[[], None] | None:
    """What the child does before it executes: CPU time, memory, no file, few processes. The
    figures are the core's (Domo's settings), fitted to what the system takes here, before the
    child starts (``limits_of``)."""
    if sys.platform == "win32":
        return None
    import resource
    import signal

    cpu, bytes_of_memory = limits_of(max_seconds=max_seconds, memory_mb=memory_mb)
    memory = resource.RLIM_INFINITY if bytes_of_memory is None else bytes_of_memory

    def setup() -> None:
        # A write past the file size limit must fail with an error, not kill the process.
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if own_account:
            resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))

    return setup


# Run as the account itself: it may signal every process it owns, and none of another account.
# ``kill(-1)`` reaches the ones that left the process group (``setsid``) as well.
_KILL_ACCOUNT_CODE = "import os, signal\nos.kill(-1, signal.SIGKILL)\n"
_KILL_HELPER_SECONDS = 5


async def kill_account(uid: int) -> None:
    """Send SIGKILL to every process of the account ``uid``, from a helper that runs as it.

    The helper needs no capability beyond the one that lets the launcher change account.
    """
    if sys.platform == "win32":
        return
    interpreter = cast("str", getattr(sys, "_base_executable", sys.executable))
    try:
        helper = await asyncio.create_subprocess_exec(
            interpreter,
            "-I",
            "-S",
            "-c",
            _KILL_ACCOUNT_CODE,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={},
            user=uid,
            group=uid,
            extra_groups=[],
        )
        async with asyncio.timeout(_KILL_HELPER_SECONDS):
            await helper.wait()
    except (OSError, TimeoutError):
        pass  # the caller checks whether the account is empty


# Run as the account itself, once its processes are gone: removes the System V objects it created
# or owns (as their creator or owner it may) and the POSIX message queues it owns (``/dev/mqueue``
# is sticky: only the owner unlinks). Trusted code of the launcher, not the tool's.
_CLEAR_IPC_CODE = r"""
import ctypes, os
uid = os.getuid()
libc = ctypes.CDLL("libc.so.6", use_errno=True)
IPC_RMID = 0
for kind, id_column in (("shm", "shmid"), ("sem", "semid"), ("msg", "msqid")):
    try:
        with open(f"/proc/sysvipc/{kind}") as table:
            rows = table.read().split("\n")
    except OSError:
        continue
    columns = rows[0].split()
    for row in rows[1:]:
        values = row.split()
        if len(values) != len(columns):
            continue
        fields = dict(zip(columns, values))
        if str(uid) not in (fields.get("uid"), fields.get("cuid")):
            continue
        found = int(fields[id_column])
        if kind == "shm":
            libc.shmctl(found, IPC_RMID, None)
        elif kind == "sem":
            libc.semctl(found, 0, IPC_RMID)
        else:
            libc.msgctl(found, IPC_RMID, None)
try:
    names = os.listdir("/dev/mqueue")
except OSError:
    names = []
for name in names:
    path = os.path.join("/dev/mqueue", name)
    try:
        if os.lstat(path).st_uid == uid:
            os.unlink(path)
    except OSError:
        pass
"""


async def clear_ipc(uid: int) -> None:
    """Remove what the account ``uid`` leaves in the sandbox's IPC namespace (System V shared
    memory, semaphores and message queues, POSIX message queues), from a helper that runs as it,
    once its processes are gone. Whether it worked is checked by ``account_has_ipc``."""
    if sys.platform == "win32":
        return
    interpreter = cast("str", getattr(sys, "_base_executable", sys.executable))
    try:
        helper = await asyncio.create_subprocess_exec(
            interpreter,
            "-I",
            "-S",
            "-c",
            _CLEAR_IPC_CODE,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={},
            user=uid,
            group=uid,
            extra_groups=[],
        )
        async with asyncio.timeout(_KILL_HELPER_SECONDS):
            await helper.wait()
    except (OSError, TimeoutError):
        pass  # the caller checks whether the account left anything


def _ipc_rows_of(text: str, uid: int) -> bool:
    """Whether a table of ``/proc/sysvipc`` (its text) has a row the account ``uid`` created or
    owns."""
    rows = text.split("\n")
    columns = rows[0].split()
    for row in rows[1:]:
        values = row.split()
        if len(values) == len(columns):
            fields = dict(zip(columns, values, strict=True))
            if str(uid) in (fields.get("uid"), fields.get("cuid")):
                return True
    return False


def account_has_ipc(uid: int) -> bool:
    """True when the account ``uid`` still has a System V object or a POSIX message queue, or when
    that cannot be confirmed: a table or the queues' directory that cannot be read (but for one
    that does not exist, a kernel without it) counts as not clean, so that the account stays out of
    use. A queue that went away while it was looked at is no queue."""
    if sys.platform == "win32":
        return False
    import os

    for kind in ("shm", "sem", "msg"):
        try:
            with open(f"/proc/sysvipc/{kind}", encoding="ascii", errors="replace") as table:
                if _ipc_rows_of(table.read(), uid):
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    try:
        names = os.listdir("/dev/mqueue")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for name in names:
        try:
            if os.lstat(os.path.join("/dev/mqueue", name)).st_uid == uid:
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def account_has_processes(uid: int) -> bool:
    """True when a live process (a zombie is not one) belongs to the account ``uid``."""
    if sys.platform == "win32":
        return False
    import os

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="ascii", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue  # it ended meanwhile
        state = next((line.split()[1] for line in lines if line.startswith("State:")), "")
        owner = next((line.split()[1] for line in lines if line.startswith("Uid:")), "")
        if owner == str(uid) and state not in ("Z", "X"):
            return True
    return False
