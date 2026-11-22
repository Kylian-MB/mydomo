import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from domo_common.sandbox_api import LONGEST_SECONDS, RunEnded, RunRequest, RunResult
from domo_sandbox import limits, runner
from domo_sandbox.limits import MachineHas
from pydantic import ValidationError

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="rlimits and file limits are Linux-only: Task 11 tests them"
)


async def run(request: RunRequest) -> RunResult:
    """Run ``request`` to its end, every request of its program refused: the run as the core
    leads it, without the launcher's routes."""
    live = await runner.start(request)
    try:
        while True:
            event = await live.next_event()
            if isinstance(event, RunEnded):
                return event.result
            await live.answer({"refused": "not_allowed"})
    finally:
        if not live.finished:
            await live.stop()


async def test_a_tool_returns_its_value():
    code = 'def run(entry):\n    return {"sum": entry["a"] + entry["b"]}\n'
    result = await run(RunRequest(code=code, entry={"a": 1, "b": 2}, max_seconds=10))
    assert result.status == "done"
    assert result.output == {"sum": 3}
    assert result.error is None


async def test_a_tool_that_raises_is_an_error_with_one_line():
    code = 'def helper():\n    raise ValueError("bad input")\n\ndef run(entry):\n    helper()\n'
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert result.status == "error"
    assert result.output is None
    assert result.error == "ValueError: bad input"


async def test_an_error_carries_no_path():
    code = "def run(entry):\n    from json import nothing_here\n"
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert result.status == "error"
    assert result.error is not None
    assert "\n" not in result.error
    assert "json" in result.error
    assert ".py" not in result.error
    assert sys.prefix not in result.error


async def test_code_without_run_is_an_error():
    result = await run(RunRequest(code="x = 1\n", entry={}, max_seconds=10))
    assert result.status == "error"
    assert result.error == "TypeError: the code defines no run(entry)"


async def test_a_tool_that_prints_does_not_break_its_answer():
    code = 'def run(entry):\n    print("noise")\n    return 1\n'
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert (result.status, result.output) == ("done", 1)


async def test_a_tool_that_never_ends_is_stopped():
    code = "def run(entry):\n    while True:\n        pass\n"
    started = time.monotonic()
    result = await run(RunRequest(code=code, entry={}, max_seconds=1))
    assert result.status == "timeout"
    assert result.output is None
    assert time.monotonic() - started < 3


async def test_an_output_too_big_is_refused():
    code = 'def run(entry):\n    return "x" * 1_000_000\n'
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert result.status == "too_big"
    assert result.output is None


async def test_an_output_just_over_the_limit_is_refused():
    code = 'def run(entry):\n    return "x" * 200\n'
    result = await run(RunRequest(code=code, entry={}, max_seconds=10, output_bytes=100))
    assert result.status == "too_big"


async def test_a_tool_sees_no_environment():
    code = "import os\n\ndef run(entry):\n    return dict(os.environ)\n"
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert result.status == "done"
    assert isinstance(result.output, dict)
    names = {name.upper() for name in result.output}
    assert not any(name.startswith("DOMO_") or "PROXY" in name for name in names)


async def test_a_tool_does_not_see_site_packages():
    code = "def run(entry):\n    import pydantic\n"
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("ModuleNotFoundError")


@linux_only
async def test_a_tool_writes_no_file():
    code = (
        "def run(entry):\n"
        "    try:\n"
        '        with open("/tmp/x", "w") as handle:\n'
        '            handle.write("x")\n'
        "    except OSError:\n"
        '        return {"wrote": False}\n'
        '    return {"wrote": True}\n'
    )
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert result.output == {"wrote": False}


# What a hostile tool writes straight on the answer's descriptor (the bootstrap's copy of its
# standard output, 3), then leaves before the bootstrap writes its own.
def written_raw(answer: str) -> str:
    return f"import os\n\ndef run(entry):\n    os.write(3, {answer.encode()!r})\n    os._exit(0)\n"


def _made(status, output=None, error=None, note=None) -> RunResult:
    return RunResult(status=status, output=output, error=error, note=note, seconds=0.0)


def test_a_child_ended_by_a_signal_the_sandbox_did_not_send_is_ended_by_the_machine():
    # Final review I4, ruling R32: never "killed" (the sandbox could not stop it).
    request = RunRequest(code="", entry={}, max_seconds=10)
    result = runner._interpret(b"", -9, request, _made)
    assert result.status == "ended_by_machine"
    assert result.error is None
    assert result.note is not None and "machine" in result.note


def test_the_sandbox_s_own_words_are_a_note_and_the_program_s_are_its_error():
    # Final review m3: the field marked as the program's words holds them alone.
    request = RunRequest(code="", entry={}, max_seconds=10)
    said = runner._interpret(b'{"error": "ValueError: bad"}', 0, request, _made)
    assert (said.status, said.error, said.note) == ("error", "ValueError: bad", None)
    silent = runner._interpret(b"", 0, request, _made)
    assert (silent.status, silent.error) == ("error", None)
    assert silent.note == "The tool ended without an answer."
    wrong = runner._interpret(b"[1", 0, request, _made)
    assert (wrong.error, wrong.note) == (None, "The tool's answer is not valid.")


@pytest.mark.parametrize(
    "answer",
    [
        '{"ok": ' + "[" * 30_000 + "]" * 30_000 + "}",  # deeper than json can read
        '{"ok": ' + "[" * 600 + "]" * 600 + "}",  # read, but too deep to give back
        '{"ok": NaN}',
        '{"ok": {"total": Infinity}}',
        '{"ok": -Infinity}',
    ],
    ids=["too_deep_to_read", "too_deep_to_give", "nan", "infinity", "minus_infinity"],
)
async def test_an_answer_that_cannot_be_given_back_is_an_error_never_a_failure_of_the_launcher(
    answer: str,
):
    result = await run(RunRequest(code=written_raw(answer), entry={}, max_seconds=10))
    assert (result.status, result.output) == ("error", None)
    assert (result.error, result.note) == (None, "The tool's answer is not valid.")
    # The launcher gives it back as any other result.
    result.model_dump_json()


async def test_an_answer_written_raw_that_is_right_is_read():
    result = await run(RunRequest(code=written_raw('{"ok": [[1]]}'), entry={}, max_seconds=10))
    assert (result.status, result.output) == ("done", [[1]])


async def test_the_bounds_of_a_run_have_floors_and_no_ceiling():
    # Spec 12.1: they are Domo's settings; a floor only where a smaller one has no meaning.
    huge = RunRequest(code="", entry={}, max_seconds=10**400, memory_mb=10**9, output_bytes=10**15)
    assert (huge.max_seconds, huge.memory_mb, huge.output_bytes) == (10**400, 10**9, 10**15)
    assert RunRequest(code="", entry={}, memory_mb=1, output_bytes=1).memory_mb == 1
    for wrong in ({"max_seconds": 0}, {"memory_mb": 0}, {"output_bytes": 0}):
        with pytest.raises(ValidationError):
            RunRequest(code="", entry={}, **wrong)  # type: ignore[arg-type]


@linux_only
async def test_a_run_is_the_first_the_system_ends_when_memory_runs_out():
    # Fix round 1, I1: the runs, never the launcher, are what the kernel kills at the memory wall.
    code = 'def run(entry):\n    return open("/proc/self/oom_score_adj").read().strip()\n'
    result = await run(RunRequest(code=code, entry={}, max_seconds=10))
    assert (result.status, result.output) == ("done", "1000")


async def test_a_run_with_a_time_past_any_clock_still_runs():
    code = "def run(entry):\n    return 1\n"
    result = await run(RunRequest(code=code, entry={}, max_seconds=10**400))
    assert (result.status, result.output) == ("done", 1)


def test_the_limits_set_in_the_child_fit_what_the_system_takes():
    # Past what a clock holds the time is "never"; a memory past what a limit holds is none.
    assert limits.limits_of(max_seconds=60, memory_mb=256) == (61, 256 * 1024 * 1024)
    assert limits.limits_of(max_seconds=10**400, memory_mb=10**30) == (LONGEST_SECONDS + 1, None)


def test_what_the_machine_has_is_read_from_its_control_group(tmp_path: Path):
    (tmp_path / "memory.max").write_text("536870912\n")
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert limits.machine_of(tmp_path, physical_mb=4096, cpus=8) == MachineHas(512, 2)
    # A limit above the machine is the machine's.
    (tmp_path / "memory.max").write_text(f"{8 * 1024**3}\n")
    assert limits.machine_of(tmp_path, physical_mb=4096, cpus=1) == MachineHas(4096, 1)
    # No limit of its own: the machine's.
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert limits.machine_of(tmp_path, physical_mb=4096, cpus=8) == MachineHas(4096, 8)
    # Nothing to read: what the system says, or nothing known.
    nothing = tmp_path / "none"
    assert limits.machine_of(nothing, physical_mb=None, cpus=None) == MachineHas(None, None)
    assert limits.machine_of(nothing, physical_mb=None, cpus=3) == MachineHas(None, 3)
    # Fix round 1, I1: what the launcher uses itself is not the runs'.
    (tmp_path / "memory.max").write_text("536870912\n")
    assert limits.machine_of(tmp_path, physical_mb=4096, cpus=8, own_mb=150) == MachineHas(362, 8)
    assert limits.machine_of(tmp_path, physical_mb=None, cpus=8, own_mb=600) == MachineHas(0, 8)
    here = limits.machine()
    assert here.processors is None or here.processors >= 1
    assert here.memory_mb is None or here.memory_mb >= 1


# --- Ending a run: the account of a run is cleared, and a kill that failed is not hidden ----------

LOOP = "def run(entry):\n    while True:\n        pass\n"


class FakeAccount:
    """Stands in for the Linux account of a run: what was killed, and what stays alive."""

    def __init__(self) -> None:
        self.killed: list[int] = []
        self.alive = False
        # What the account keeps in the IPC namespace, and whether clearing it works.
        self.ipc = False
        self.ipc_stays = False
        self.steps: list[str] = []

    async def kill_account(self, uid: int) -> None:
        self.killed.append(uid)
        self.steps.append("kill")

    def has_processes(self, uid: int) -> bool:
        return self.alive

    async def clear_ipc(self, uid: int) -> None:
        self.steps.append(f"clear_ipc {uid}" + (" with processes alive" if self.alive else ""))
        if not self.ipc_stays:
            self.ipc = False

    def has_ipc(self, uid: int) -> bool:
        return self.ipc


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch) -> FakeAccount:
    fake = FakeAccount()
    monkeypatch.setattr(runner.limits, "can_switch_account", lambda: True)
    monkeypatch.setattr(runner.limits, "pick_account", lambda taken: 20123)
    monkeypatch.setattr(runner.limits, "kill_account", fake.kill_account)
    monkeypatch.setattr(runner.limits, "account_has_processes", fake.has_processes)
    monkeypatch.setattr(runner.limits, "clear_ipc", fake.clear_ipc)
    monkeypatch.setattr(runner.limits, "account_has_ipc", fake.has_ipc)
    monkeypatch.setattr(runner, "_account_options", lambda uid: {})
    monkeypatch.setattr(runner, "_REAP_SECONDS", 0.3)
    monkeypatch.setattr(runner, "_taken_accounts", set())
    return fake


async def test_the_account_is_cleared_then_freed_when_a_run_times_out(account: FakeAccount):
    result = await run(RunRequest(code=LOOP, entry={}, max_seconds=1))
    assert result.status == "timeout"
    assert account.killed == [20123]
    assert runner._taken_accounts == set()


async def test_the_account_is_cleared_after_a_normal_answer_too(account: FakeAccount):
    result = await run(RunRequest(code="def run(entry):\n    return 1\n", entry={}, max_seconds=10))
    assert (result.status, result.output) == ("done", 1)
    assert account.killed == [20123]
    assert runner._taken_accounts == set()


async def test_an_account_that_keeps_a_process_is_not_freed_and_the_run_is_killed(
    account: FakeAccount,
):
    account.alive = True
    result = await run(RunRequest(code=LOOP, entry={}, max_seconds=1))
    assert result.status == "killed"
    assert (result.error, result.note) == (None, "The run could not be ended.")
    assert runner._taken_accounts == {20123}


async def test_what_the_account_left_in_ipc_is_cleared_once_its_processes_are_gone(
    account: FakeAccount,
):
    account.ipc = True
    result = await run(RunRequest(code="def run(entry):\n    return 1\n", entry={}, max_seconds=10))
    assert (result.status, result.output) == ("done", 1)
    # Cleared after the kill, never while a process of the account lives.
    assert account.steps == ["kill", "clear_ipc 20123"]
    assert runner._taken_accounts == set()


async def test_an_account_whose_ipc_cannot_be_cleared_is_not_freed_and_the_run_is_killed(
    account: FakeAccount,
):
    account.ipc = account.ipc_stays = True
    result = await run(RunRequest(code="def run(entry):\n    return 1\n", entry={}, max_seconds=10))
    assert result.status == "killed"
    assert (result.error, result.note) == (None, "The run could not be ended.")
    assert runner._taken_accounts == {20123}


async def test_a_run_stopped_while_it_is_ended_keeps_its_account_out_of_use(
    account: FakeAccount, monkeypatch: pytest.MonkeyPatch
):
    """A run cancelled inside its clean-up (the core hung up as it ended) has nothing confirmed:
    its account is never given to another run."""
    real_end = runner._end
    reached = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def end_that_is_stopped(process: asyncio.subprocess.Process, uid: int | None) -> bool:
        processes.append(process)
        reached.set()
        await asyncio.sleep(3600)
        return await real_end(process, uid)

    monkeypatch.setattr(runner, "_end", end_that_is_stopped)
    task = asyncio.create_task(
        run(RunRequest(code="def run(entry):\n    return 1\n", entry={}, max_seconds=10))
    )
    await asyncio.wait_for(reached.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._taken_accounts == {20123}
    # The test ends the process itself.
    for process in processes:
        await real_end(process, None)


async def test_a_run_that_could_not_start_gives_its_account_back(
    account: FakeAccount, monkeypatch: pytest.MonkeyPatch
):
    async def refused(*args: object, **kwargs: object) -> None:
        raise OSError("no process")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", refused)
    with pytest.raises(OSError):
        await run(RunRequest(code="def run(entry):\n    return 1\n", entry={}, max_seconds=10))
    assert runner._taken_accounts == set()


async def test_a_run_that_fails_before_it_is_fed_is_still_ended(
    account: FakeAccount, monkeypatch: pytest.MonkeyPatch
):
    """What goes wrong between the start of the process and the wait for its answer still ends
    the process and clears the account."""

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("cannot feed")

    monkeypatch.setattr(runner, "_feed", broken)
    with pytest.raises(RuntimeError):
        await run(RunRequest(code=LOOP, entry={}, max_seconds=10))
    assert account.killed == [20123]
    assert runner._taken_accounts == set()


async def test_ipc_is_not_cleared_while_a_process_of_the_account_lives(account: FakeAccount):
    account.alive = True
    await run(RunRequest(code=LOOP, entry={}, max_seconds=1))
    assert not any(step.startswith("clear_ipc") for step in account.steps)


def test_a_table_of_sysvipc_is_read_by_its_creator_and_its_owner():
    table = (
        "       key      shmid perms       size  cpid  lpid nattch   uid   gid  cuid  cgid\n"
        "     19009          3   666       4096    10    11      0 20001 20001 20002 20002\n"
    )
    assert runner.limits._ipc_rows_of(table, 20001)  # its owner
    assert runner.limits._ipc_rows_of(table, 20002)  # its creator, who gave it away
    assert not runner.limits._ipc_rows_of(table, 20003)
    assert not runner.limits._ipc_rows_of(table.split("\n")[0], 20001)


@pytest.mark.parametrize(
    ("failing", "error", "has_ipc"),
    [
        # A table that cannot be read, or the queues that cannot be listed: not confirmed clean.
        ("/proc/sysvipc/sem", PermissionError, True),
        ("/dev/mqueue", PermissionError, True),
        ("/dev/mqueue/q", OSError, True),
        # A kernel without them: nothing there to leave.
        ("/proc/sysvipc/sem", FileNotFoundError, False),
        ("/dev/mqueue", FileNotFoundError, False),
        # A queue that went away while it was looked at.
        ("/dev/mqueue/q", FileNotFoundError, False),
    ],
)
def test_ipc_that_cannot_be_read_is_not_taken_for_clean(
    monkeypatch: pytest.MonkeyPatch, failing: str, error: type[OSError], has_ipc: bool
):
    limits = runner.limits
    monkeypatch.setattr(limits.sys, "platform", "linux")
    empty = "       key      shmid perms       size  cpid  lpid nattch   uid   gid  cuid  cgid\n"

    class Table:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> "Table":
            if self.path == failing:
                raise error(self.path)
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> str:
            return empty

    class Stat:
        st_uid = 30001  # someone else's

    def listdir(path: str) -> list[str]:
        if path == failing:
            raise error(path)
        return ["q"]

    def lstat(path: str) -> Stat:
        if path.replace("\\", "/") == failing:
            raise error(path)
        return Stat()

    monkeypatch.setattr("builtins.open", lambda path, *a, **k: Table(path))
    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "lstat", lstat)
    assert limits.account_has_ipc(20001) is has_ipc


async def test_a_kill_that_did_not_work_is_not_called_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(runner, "_REAP_SECONDS", 0.3)
    survivors: list[asyncio.subprocess.Process] = []
    monkeypatch.setattr(runner, "_kill", survivors.append)  # the kill is refused: nothing happens
    try:
        result = await run(RunRequest(code=LOOP, entry={}, max_seconds=1))
        assert result.status == "killed"
        assert (result.error, result.note) == (None, "The run could not be ended.")
    finally:
        for process in survivors:
            process.kill()
            await process.wait()


async def test_the_launcher_refuses_to_start_when_it_cannot_end_a_process(account: FakeAccount):
    account.alive = True
    with pytest.raises(RuntimeError, match="cannot end"):
        await runner.check_runs_can_be_ended()


async def test_the_launcher_starts_when_it_can_end_a_process(account: FakeAccount):
    await runner.check_runs_can_be_ended()
    assert account.killed == [20123]


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="a run gets an account of its own only on Linux, as root: Task 11 tests it",
)
async def test_a_child_that_leaves_the_group_does_not_outlive_the_run():
    code = (
        "import os, time\n"
        "def run(entry):\n"
        "    if os.fork() == 0:\n"
        "        os.setsid()\n"
        "        time.sleep(300)\n"
        "    while True:\n"
        "        pass\n"
    )
    result = await run(RunRequest(code=code, entry={}, max_seconds=1))
    assert result.status == "timeout"
    assert runner._taken_accounts == set()
