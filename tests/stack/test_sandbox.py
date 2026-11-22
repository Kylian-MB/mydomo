"""The real sandbox holds hostile programs (spec 6 and 8: "the sandbox catches the error").

Each program is sent to the launcher from inside the core's container, through the socket, by the
core's own client (``SandboxClient.run`` over the run's channel, every request of the program
refused), exactly as the core sends a tool's code: it never went through the core's checks (the
import check would refuse most of them), on purpose. What is tested is the container and the
launcher: each run ends ``error``, ``timeout``, ``killed``, ``ended_by_machine`` or ``too_big``, or
``done`` with an output that proves nothing was reached, and the launcher answers an ordinary run
right after.

Two holes these programs found are closed, and stay tested here (both needed a program that got
past the import check, which refuses ``socket``, ``ctypes`` and ``open``):

* a run could connect to the launcher's own socket and send it runs of its own, outside the core's
  counts, with the priority of a turn: the socket is now root's and the core's group's, mode 0660
  (``test_a_run_cannot_reach_the_launcher_s_own_socket``);
* a run could leave System V shared memory, a message queue or a semaphore set, or a POSIX message
  queue, which a later run under another account found and read (the sandbox's private IPC
  namespace is shared by every run): the launcher now removes what a run's account left once its
  processes are gone (``test_nothing_a_run_leaves_is_found_by_a_later_one``).
"""

import json
import secrets
from typing import Any

import pytest

from .stack_support import StackInfo

pytestmark = pytest.mark.stack

SOCKET = "/run/domo/sandbox/launcher.sock"
UIDS = range(20000, 30000)
LIMIT_STATUSES = {"error", "timeout", "killed", "too_big", "ended_by_machine"}

# Run in the core's container: runs each body of the JSON list in ``sys.argv[1]`` with the core's
# client, one after the other, every request of the program refused, and prints the results as a
# JSON list.
SEND = (
    "import asyncio, json, sys\n"
    "from pathlib import Path\n"
    "from domo_common.sandbox_api import RunRequest\n"
    "from domo_core.sandbox_client import REFUSE_ALL, SandboxClient, new_http_client\n"
    "async def main():\n"
    f"    http = new_http_client(Path({SOCKET!r}))\n"
    "    client = SandboxClient(http)\n"
    "    answers = []\n"
    "    for body in json.loads(sys.argv[1]):\n"
    "        result = await client.run(RunRequest(**body), answer=REFUSE_ALL)\n"
    "        answers.append(result.model_dump(mode='json'))\n"
    "    await http.aclose()\n"
    "    print(json.dumps(answers))\n"
    "asyncio.run(main())\n"
)

# Run in the core's container: what the launcher answers on the route of a run without a channel.
OLD_ROUTE = (
    "import httpx\n"
    f"transport = httpx.HTTPTransport(uds={SOCKET!r})\n"
    "with httpx.Client(transport=transport) as client:\n"
    "    body = {'code': 'def run(entry):\\n    return 1\\n', 'entry': {}}\n"
    "    print(client.post('http://sandbox/run', json=body).status_code)\n"
)

ORDINARY = 'def run(entry):\n    return {"result": entry["arguments"]["x"] * 2}\n'


def ordinary_run() -> dict[str, Any]:
    return {"code": ORDINARY, "entry": {"arguments": {"x": 21}}}


def sent(stack: StackInfo, *bodies: dict[str, Any]) -> list[dict[str, Any]]:
    """The launcher's answers to ``bodies``, sent one after the other from the core."""
    result = stack.exec("core", ["python", "-c", SEND, json.dumps(list(bodies))])
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def hostile_then_ordinary(
    stack: StackInfo, code: str, *, max_seconds: int = 20, **limits: int
) -> dict[str, Any]:
    """Send a hostile ``code``, then an ordinary run: the ordinary one must work. Returns the
    answer to the hostile one."""
    hostile = {"code": code, "entry": {}, "max_seconds": max_seconds, **limits}
    answer, after = sent(stack, hostile, ordinary_run())
    # What each program came to, in the output of a run with -s (the report of the trial).
    print(f"hostile run: {answer['status']} in {answer['seconds']} s: {answer['error']}")
    assert after["status"] == "done" and after["output"] == {"result": 42}, after
    return answer


def run_accounts_alive(stack: StackInfo) -> list[str]:
    """The live processes of the sandbox whose account is one a run gets (seen as root, from the
    sandbox itself)."""
    code = (
        "import os\n"
        "found = []\n"
        "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
        "    try:\n"
        "        lines = open(f'/proc/{pid}/status').read().splitlines()\n"
        "    except OSError:\n"
        "        continue\n"
        "    uid = next(l.split()[1] for l in lines if l.startswith('Uid:'))\n"
        "    state = next(l.split()[1] for l in lines if l.startswith('State:'))\n"
        "    if 20000 <= int(uid) < 30000 and state not in ('Z', 'X'):\n"
        "        found.append(f'{pid}:{uid}:{state}')\n"
        "print(' '.join(found))\n"
    )
    result = stack.exec("sandbox", ["python", "-c", code])
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


# --- the channel ----------------------------------------------------------------------------------

ASKS = """
import domo

def run(entry):
    try:
        domo.read("SELECT 1")
    except domo.Refused as refused:
        return {"refused": refused.reason}
    return {"refused": None}
"""


def test_a_run_s_requests_reach_the_core_and_are_refused(stack):
    answer = hostile_then_ordinary(stack, ASKS)
    assert (answer["status"], answer["output"]) == ("done", {"refused": "not_allowed"}), answer


def test_the_old_route_is_gone(stack):
    result = stack.exec("core", ["python", "-c", OLD_ROUTE])
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "404"


# --- what a run can read ------------------------------------------------------------------------

READ_ENVIRONMENTS = """
def run(entry):
    import os
    found = {"own": dict(os.environ), "others": {}}
    for pid in [p for p in os.listdir("/proc") if p.isdigit()]:
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                found["others"][pid] = handle.read().decode("latin-1")
        except OSError as error:
            found["others"][pid] = type(error).__name__
    return found
"""


def test_a_run_sees_no_environment_and_cannot_read_another_process_s(stack):
    answer = hostile_then_ordinary(stack, READ_ENVIRONMENTS)
    assert answer["status"] == "done", answer
    found = answer["output"]
    # The launcher gives the run an empty environment; Python itself sets its locale (PEP 538).
    assert set(found["own"]) <= {"LC_CTYPE"}, found["own"]
    # pid 1 (the container's init) and the launcher are root's: unreadable. A run's own process,
    # the only one it may read, has nothing but Python's locale in its environment.
    assert found["others"]["1"] == "PermissionError"
    own = {"", "LC_CTYPE=C.UTF-8\x00"}
    assert all(value in ("PermissionError", *own) for value in found["others"].values()), found
    text = json.dumps(found).lower()
    for word in ("domo_", "proxy", "token", "password", "secret", "key="):
        assert word not in text, word


# --- the network --------------------------------------------------------------------------------

REACH = """
def run(entry):
    import socket
    found = {}
    for host, port in [("gatekeeper", 8000), ("db", 5432), ("core", 8000), ("edge", 443),
                       ("1.1.1.1", 443), ("example.com", 443), ("127.0.0.1", 8000)]:
        try:
            socket.create_connection((host, port), timeout=3).close()
            found[f"{host}:{port}"] = "REACHED"
        except OSError as error:
            found[f"{host}:{port}"] = type(error).__name__
    return found
"""


def test_a_run_reaches_no_service_and_not_the_outside(stack):
    answer = hostile_then_ordinary(stack, REACH, max_seconds=40)
    assert answer["status"] == "done", answer
    assert "REACHED" not in answer["output"].values(), answer["output"]
    assert len(answer["output"]) == 7


# Connects to the launcher and, if it can, sends it a run of its own with the priority of a turn.
REACH_THE_LAUNCHER = """
def run(entry):
    import json, socket
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    try:
        s.connect("/run/domo/sandbox/launcher.sock")
    except OSError as error:
        return {"socket": type(error).__name__}
    body = json.dumps({"code": "def run(entry):\\n    return 'mine'\\n", "entry": {}}).encode()
    head = (
        "POST /runs HTTP/1.0\\r\\nContent-Type: application/json\\r\\nx-domo-waits: turn\\r\\n"
        f"Content-Length: {len(body)}\\r\\n\\r\\n"
    )
    s.sendall(head.encode() + body)
    answer = b""
    while chunk := s.recv(4096):
        answer += chunk
    return {"socket": "REACHED", "answer": answer.decode("latin-1").split("\\r\\n\\r\\n")[-1]}
"""


def test_a_run_cannot_reach_the_launcher_s_own_socket(stack):
    """A run that could reach the launcher could send runs of its own, outside every count of the
    core, with the priority of a turn, and take the places a turn waits for: it cannot even
    connect."""
    answer = hostile_then_ordinary(stack, REACH_THE_LAUNCHER)
    print("a run that tries the launcher's socket:", answer["output"])
    assert answer["status"] == "done", answer
    assert answer["output"] == {"socket": "PermissionError"}, answer


# What a run tries on the socket's file and its directory: it is neither's owner.
TOUCH_THE_SOCKET = """
def run(entry):
    import os
    found = {"groups": os.getgroups()}
    for name, attempt in (
        ("chmod", lambda: os.chmod("/run/domo/sandbox/launcher.sock", 0o666)),
        ("unlink", lambda: os.unlink("/run/domo/sandbox/launcher.sock")),
        ("rename", lambda: os.rename("/run/domo/sandbox/launcher.sock", "/run/domo/sandbox/x")),
        ("create", lambda: open("/run/domo/sandbox/x", "w").close()),
    ):
        try:
            attempt()
            found[name] = "DONE"
        except OSError as error:
            found[name] = type(error).__name__
    return found
"""
STAT_THE_SOCKET = (
    "import os, stat\n"
    "for path in ('/run/domo/sandbox', '/run/domo/sandbox/launcher.sock'):\n"
    "    s = os.stat(path)\n"
    "    print(s.st_uid, s.st_gid, oct(stat.S_IMODE(s.st_mode)))\n"
)


def test_the_socket_is_the_core_s_and_root_s_and_a_run_cannot_change_it(stack):
    listed = stack.exec("sandbox", ["python", "-c", STAT_THE_SOCKET])
    assert listed.returncode == 0, listed.stderr
    directory, socket = listed.stdout.split("\n")[:2]
    assert socket == "0 10001 0o660"
    uid, _, mode = directory.split()
    assert uid == "0" and int(mode, 8) & 0o022 == 0  # root's, writable by nobody else
    answer = hostile_then_ordinary(stack, TOUCH_THE_SOCKET)
    assert answer["status"] == "done", answer
    found = answer["output"]
    # A run is in no group but its own: it cannot reach the socket through a group either.
    assert found.pop("groups") == []
    assert set(found.values()) <= {"PermissionError", "OSError"}, found
    # The core (uid and gid 10001) still reaches it, and so does the health check (root).
    assert stack.inspect("sandbox")["State"]["Health"]["Status"] == "healthy"


# --- processes, memory, time, output ---------------------------------------------------------

FORK_BOMB = """
def run(entry):
    import os
    while True:
        try:
            os.fork()
        except OSError:
            pass
"""


def test_a_fork_bomb_stops_at_its_cap_and_leaves_nothing_alive(stack):
    answer = hostile_then_ordinary(stack, FORK_BOMB, max_seconds=5)
    assert answer["status"] in LIMIT_STATUSES - {"too_big"}, answer
    assert run_accounts_alive(stack) == []


ALLOCATE_2_GB = """
def run(entry):
    chunks = []
    for _ in range(32):
        chunks.append(b"x" * (64 * 1024 * 1024))
    return {"held": len(chunks)}
"""


def test_two_gigabytes_stop_at_the_run_s_own_cap(stack):
    answer = hostile_then_ordinary(stack, ALLOCATE_2_GB)
    assert answer["status"] == "error" and "MemoryError" in answer["error"], answer


# Run in the core's container: what the launcher says its machine has, and its answer to a run
# that asks more memory than that.
MACHINE = (
    "import httpx, json\n"
    f"transport = httpx.HTTPTransport(uds={SOCKET!r})\n"
    "with httpx.Client(transport=transport, base_url='http://sandbox') as client:\n"
    "    has = client.get('/machine').json()\n"
    "    body = {'code': 'def run(entry):\\n    return 1\\n', 'entry': {},\n"
    "            'memory_mb': has['memory_mb'] + 1}\n"
    "    refused = client.post('/runs', json=body)\n"
    "    print(json.dumps([has, refused.status_code, refused.json()]))\n"
)


def test_more_memory_than_the_container_has_is_refused_with_what_it_has(stack):
    """The bounds of a run are Domo's settings, with no ceiling of the code: the launcher
    refuses only what its machine cannot give, here the container's memory limit (1536 MB, its
    control group), and says what it has."""
    result = stack.exec("core", ["python", "-c", MACHINE])
    assert result.returncode == 0, result.stderr[-2000:]
    has, status, refused = json.loads(result.stdout)
    # The container's 1536 MB, less what the launcher uses itself.
    assert 1000 < has["memory_mb"] < 1536 and has["processors"] >= 1, has
    assert status == 409
    assert refused == {"refused": "not_here", "beyond": ["memory_mb"], "has": has}


OOM_SCORES = (
    "import os\n"
    "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
    "    try:\n"
    "        command = open(f'/proc/{pid}/cmdline', 'rb').read()\n"
    "    except OSError:\n"
    "        continue\n"
    "    if b'domo_sandbox' in command:\n"
    "        print(int(open(f'/proc/{pid}/oom_score_adj').read()))\n"
    "        break\n"
)


def test_at_the_memory_wall_the_kernel_ends_a_run_never_the_launcher(stack):
    """Fix round 1, I1: a run makes itself the first the kernel kills (``oom_score_adj`` 1000),
    the launcher keeps its own (0): the wall they share ends a run (see
    ``test_runs_that_fill_the_container_together_stop_at_its_wall``), and the launcher lives on
    with the other runs."""
    read_own = 'def run(entry):\n    return open("/proc/self/oom_score_adj").read().strip()\n'
    [answer] = sent(stack, {"code": read_own, "entry": {}})
    assert (answer["status"], answer["output"]) == ("done", "1000"), answer
    launcher = stack.exec("sandbox", ["python", "-c", OOM_SCORES])
    assert launcher.returncode == 0 and int(launcher.stdout.strip()) < 1000, launcher.stdout


def has_memory(stack: StackInfo) -> int:
    """What the launcher says its machine gives the runs, in megabytes."""
    code = (
        "import httpx\n"
        f"transport = httpx.HTTPTransport(uds={SOCKET!r})\n"
        "with httpx.Client(transport=transport, base_url='http://sandbox') as client:\n"
        "    print(client.get('/machine').json()['memory_mb'])\n"
    )
    result = stack.exec("core", ["python", "-c", code])
    assert result.returncode == 0, result.stderr[-2000:]
    return int(result.stdout.strip())


# Run in the core's container: two runs at once, each allowed what the launcher gives the runs in
# all, and every request refused; prints their results.
SEND_AT_ONCE = (
    "import asyncio, json, sys\n"
    "from pathlib import Path\n"
    "from domo_common.sandbox_api import RunRequest\n"
    "from domo_core.sandbox_client import REFUSE_ALL, SandboxClient, new_http_client\n"
    "async def main():\n"
    f"    http = new_http_client(Path({SOCKET!r}))\n"
    "    client = SandboxClient(http)\n"
    "    body = json.loads(sys.argv[1])\n"
    "    ran = await asyncio.gather(*(client.run(RunRequest(**body), answer=REFUSE_ALL)\n"
    "                                 for _ in range(2)))\n"
    "    await http.aclose()\n"
    "    print(json.dumps([result.model_dump(mode='json') for result in ran]))\n"
    "asyncio.run(main())\n"
)


def test_runs_that_fill_the_container_together_stop_at_its_wall(stack):
    """Two runs, each allowed all the launcher gives the runs (the container's 1536 MB, no swap,
    less the launcher's own), together meet the container's memory limit: the kernel kills one of
    them (a run is the first it ends, ``oom_score_adj`` 1000), never the launcher, which answers
    an ordinary run after. The compose file says so; this is the proof.

    Only the container can end it this way: the kernel's OOM kill is a signal with no answer and
    that the sandbox did not send: ``ended_by_machine`` (ruling R32), never ``killed``. The CPU
    cap is a signal too, but only after ``max_seconds`` + 1 = 61 s: the kill must come long
    before."""
    body = {"code": ALLOCATE_2_GB, "entry": {}, "max_seconds": 60, "memory_mb": has_memory(stack)}
    result = stack.exec("core", ["python", "-c", SEND_AT_ONCE, json.dumps(body)])
    assert result.returncode == 0, result.stderr[-2000:]
    answers = json.loads(result.stdout)
    print("runs at the wall:", [(a["status"], a["seconds"]) for a in answers])
    assert "ended_by_machine" in {answer["status"] for answer in answers}, answers
    killed = next(answer for answer in answers if answer["status"] == "ended_by_machine")
    assert killed["error"] is None and "memory is the likely cause" in killed["note"], killed
    assert killed["seconds"] < 30, killed
    assert stack.inspect("sandbox")["State"]["Running"] is True
    [after] = sent(stack, ordinary_run())
    assert after["status"] == "done" and after["output"] == {"result": 42}, after
    assert run_accounts_alive(stack) == []


WRITE_100_MB = """
def run(entry):
    found = {}
    for path in ("/tmp/big", "/app/big", "/big", "/dev/shm/big", "/var/tmp/big", "big"):
        try:
            with open(path, "wb") as handle:
                for _ in range(100):
                    handle.write(b"x" * (1024 * 1024))
            found[path] = "WRITTEN"
        except OSError as error:
            found[path] = type(error).__name__
    return found
"""


def test_a_run_writes_nothing_anywhere(stack):
    answer = hostile_then_ordinary(stack, WRITE_100_MB)
    assert answer["status"] == "done", answer
    assert "WRITTEN" not in answer["output"].values(), answer["output"]
    listing = stack.exec(
        "sandbox", ["python", "-c", "import os; print(os.listdir('/tmp'), os.listdir('/app'))"]
    )
    assert listing.stdout.strip() == "[] ['.venv']", listing.stdout


PRINT_10_MB = """
def run(entry):
    print("x" * (10 * 1024 * 1024))
    return {"printed": True}
"""

RETURN_10_MB = """
def run(entry):
    return {"result": "x" * (10 * 1024 * 1024)}
"""


def test_what_a_run_prints_goes_nowhere_and_a_huge_answer_is_too_big(stack):
    printed = hostile_then_ordinary(stack, PRINT_10_MB)
    assert printed["status"] == "done" and printed["output"] == {"printed": True}, printed
    answer = hostile_then_ordinary(stack, RETURN_10_MB)
    assert answer["status"] == "too_big", answer
    assert answer["output"] is None


def test_an_endless_loop_ends_at_its_time(stack):
    answer = hostile_then_ordinary(
        stack, "def run(entry):\n    while True:\n        pass\n", max_seconds=2
    )
    assert answer["status"] == "timeout", answer
    assert answer["seconds"] < 10


LEAVES_A_CHILD = """
def run(entry):
    import os, time
    if os.fork() == 0:
        os.setsid()
        os.closerange(0, 1024)
        time.sleep(300)
        os._exit(0)
    return {"uid": os.getuid()}
"""


def test_a_child_that_leaves_the_run_s_group_does_not_outlive_the_run(stack):
    answer = hostile_then_ordinary(stack, LEAVES_A_CHILD)
    assert answer["status"] == "done", answer
    assert answer["output"]["uid"] in UIDS
    assert run_accounts_alive(stack) == []


# --- what persists between two runs -----------------------------------------------------------

# What run A, under one account, leaves in every place it can, as a hostile run would (open to
# every account: umask 0, mode 0666), each with the words "left by A": files, a POSIX message
# queue, a System V shared memory segment, message queue and semaphore set. The names and the key
# are the entry's (``key``), new at each trial.
LEAVE_BEHIND = r"""
import ctypes, os

IPC_CREAT = 0o1000
FILES = ("/tmp/left", "/dev/shm/left", "/var/tmp/left", "/app/left", "left")

class Message(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_long), ("text", ctypes.c_char * 32)]

def run(entry):
    key = entry["key"]
    os.umask(0)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    left = {"uid": os.getuid()}
    for path in FILES:
        try:
            with open(path, "w") as handle:
                handle.write("left by A")
            left[path] = "written"
        except OSError as error:
            left[path] = type(error).__name__
    libc.mq_open.restype = ctypes.c_int
    queue = libc.mq_open(f"/left{key}".encode(), os.O_CREAT | os.O_RDWR, 0o666, None)
    sent = queue >= 0 and libc.mq_send(queue, b"left by A", 9, 0) == 0
    left["posix_queue"] = "left" if sent else os.strerror(ctypes.get_errno())
    libc.shmget.restype = ctypes.c_int
    segment = libc.shmget(key, 4096, IPC_CREAT | 0o666)
    if segment >= 0:
        libc.shmat.restype = ctypes.c_void_p
        ctypes.memmove(libc.shmat(segment, None, 0), b"left by A", 9)
        left["shared_memory"] = "left"
    else:
        left["shared_memory"] = os.strerror(ctypes.get_errno())
    messages = libc.msgget(key, IPC_CREAT | 0o666)
    message = Message(1, b"left by A")
    sent = messages >= 0 and libc.msgsnd(messages, ctypes.byref(message), 9, 0) == 0
    left["message_queue"] = "left" if sent else os.strerror(ctypes.get_errno())
    made = libc.semget(key, 1, IPC_CREAT | 0o666) >= 0
    left["semaphores"] = "left" if made else os.strerror(ctypes.get_errno())
    return left
"""

# What run B, under another account, finds of it: what each place gives back, or "none".
FIND = r"""
import ctypes, os

IPC_NOWAIT = 0o4000
FILES = ("/tmp/left", "/dev/shm/left", "/var/tmp/left", "/app/left", "left")

class Message(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_long), ("text", ctypes.c_char * 32)]

def run(entry):
    key = entry["key"]
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    found = {"uid": os.getuid()}
    for path in FILES:
        try:
            with open(path) as handle:
                found[path] = handle.read()
        except OSError:
            found[path] = "none"
    libc.mq_open.restype = ctypes.c_int
    libc.mq_receive.restype = ctypes.c_ssize_t
    queue = libc.mq_open(f"/left{key}".encode(), os.O_RDONLY | os.O_NONBLOCK)
    found["posix_queue"] = "none"
    if queue >= 0:
        buffer = ctypes.create_string_buffer(1 << 16)
        size = libc.mq_receive(queue, buffer, 1 << 16, None)
        found["posix_queue"] = buffer.raw[:size].decode() if size >= 0 else "empty"
    libc.shmget.restype = ctypes.c_int
    segment = libc.shmget(key, 0, 0)
    found["shared_memory"] = "none"
    if segment >= 0:
        libc.shmat.restype = ctypes.c_void_p
        found["shared_memory"] = ctypes.string_at(libc.shmat(segment, None, 0), 9).decode()
    messages = libc.msgget(key, 0)
    found["message_queue"] = "none"
    if messages >= 0:
        message = Message()
        size = libc.msgrcv(messages, ctypes.byref(message), 32, 0, IPC_NOWAIT)
        found["message_queue"] = message.text[:size].decode() if size >= 0 else "empty"
    found["semaphores"] = "found" if libc.semget(key, 0, 0) >= 0 else "none"
    return found
"""
IPC_OBJECTS = ("posix_queue", "shared_memory", "message_queue", "semaphores")
LEFT_FILES = ("/tmp/left", "/dev/shm/left", "/var/tmp/left", "/app/left", "left")


@pytest.fixture(scope="module")
def left_and_found(stack: StackInfo) -> tuple[dict[str, Any], dict[str, Any]]:
    """What run A left, and what run B, under another account, found of it, one after the
    other."""
    key = {"key": secrets.randbelow(1 << 30) + 1}
    left, found = sent(
        stack,
        {"code": LEAVE_BEHIND, "entry": key, "max_seconds": 20},
        {"code": FIND, "entry": key, "max_seconds": 20},
    )
    assert left["status"] == "done" and found["status"] == "done", (left, found)
    a, b = left["output"], found["output"]
    print("run A left:", a, "\nrun B found:", b)
    assert a["uid"] in UIDS and b["uid"] in UIDS and a["uid"] != b["uid"]
    return a, b


def test_no_file_a_run_writes_is_found_by_a_later_one(left_and_found):
    a, b = left_and_found
    for path in LEFT_FILES:
        assert a[path] != "written" and b[path] == "none", path


def test_the_shared_memory_objects_left_behind_are_real(left_and_found):
    """The control of the next test: run A did leave each object (the next test is about whether
    another run finds them, not whether they could be made)."""
    a, _ = left_and_found
    assert all(a[name] == "left" for name in IPC_OBJECTS), a


def test_nothing_a_run_leaves_is_found_by_a_later_one(stack, left_and_found):
    """The launcher removes what a run's account left once its processes are gone: the next run
    finds nothing, and nothing of any run's account is left in the namespace."""
    _, b = left_and_found
    assert all(b[name] == "none" for name in IPC_OBJECTS), b
    left = stack.exec("sandbox", ["python", "-c", IPC_OF_RUN_ACCOUNTS])
    assert left.returncode == 0, left.stderr
    assert left.stdout.strip() == "[]"


# Seen as root from the sandbox: the IPC objects and POSIX queues of any run account.
IPC_OF_RUN_ACCOUNTS = (
    "import os\n"
    "found = []\n"
    "for kind in ('shm', 'sem', 'msg'):\n"
    "    rows = open(f'/proc/sysvipc/{kind}').read().split('\\n')\n"
    "    columns = rows[0].split()\n"
    "    for row in rows[1:]:\n"
    "        fields = dict(zip(columns, row.split()))\n"
    "        if any(20000 <= int(fields.get(c, 0)) < 30000 for c in ('uid', 'cuid')):\n"
    "            found.append(kind)\n"
    "for name in os.listdir('/dev/mqueue'):\n"
    "    if 20000 <= os.stat('/dev/mqueue/' + name).st_uid < 30000:\n"
    "        found.append(name)\n"
    "print(found)\n"
)


# --- the places a turn waits for ---------------------------------------------------------------

HOLD_A_PLACE = """
def run(entry):
    import time
    started = time.time()
    time.sleep(entry["seconds"])
    return {"started": started, "ended": time.time()}
"""
WHEN_STARTED = "def run(entry):\n    import time\n    return {'started': time.time()}\n"

# Run in the core: four runs hold the four places (they end one after the other), then a run
# without the header queues, then one a turn waits for: when the first place comes free, it goes to
# the one the turn waits for, though it came later.
SLOTS = (
    "import asyncio, json\n"
    "from pathlib import Path\n"
    "from domo_common.sandbox_api import RunRequest\n"
    "from domo_core.sandbox_client import REFUSE_ALL, SandboxClient, new_http_client\n"
    f"HOLD = {HOLD_A_PLACE!r}\n"
    f"PROBE = {WHEN_STARTED!r}\n"
    "async def main():\n"
    f"    http = new_http_client(Path({SOCKET!r}))\n"
    "    client = SandboxClient(http)\n"
    "    async def send(body, turn=False):\n"
    "        request = RunRequest(**body)\n"
    "        result = await client.run(request, answer=REFUSE_ALL, waited_by_a_turn=turn)\n"
    "        return result.model_dump(mode='json')\n"
    "    tasks = {}\n"
    "    for place, seconds in enumerate((12, 18, 24, 30)):\n"
    "        body = {'code': HOLD, 'entry': {'seconds': seconds}, 'max_seconds': 60}\n"
    "        tasks[f'hold{place}'] = asyncio.create_task(send(body))\n"
    "    await asyncio.sleep(3)\n"
    "    tasks['other'] = asyncio.create_task(send({'code': PROBE, 'entry': {}}))\n"
    "    await asyncio.sleep(1)\n"
    "    tasks['turn'] = asyncio.create_task(send({'code': PROBE, 'entry': {}}, turn=True))\n"
    "    answers = {name: await task for name, task in tasks.items()}\n"
    "    await http.aclose()\n"
    "    print(json.dumps(answers))\n"
    "asyncio.run(main())\n"
)


def test_a_run_a_turn_waits_for_takes_the_first_free_place(stack):
    result = stack.exec("core", ["python", "-c", SLOTS])
    assert result.returncode == 0, result.stderr[-2000:]
    answers = json.loads(result.stdout)
    assert all(answer["status"] == "done" for answer in answers.values()), answers
    out = {name: answer["output"] for name, answer in answers.items()}
    print("the places:", out)
    # Every place was held when the two came: neither started before the first holder ended.
    assert out["turn"]["started"] >= out["hold0"]["ended"] - 0.5
    assert out["other"]["started"] >= out["hold0"]["ended"] - 0.5
    # The turn's run, sent a second after the other, took the first place that came free; the
    # other one waited for the next (the turn's own, a moment later).
    assert out["turn"]["started"] < out["other"]["started"]
    assert out["turn"]["started"] < out["hold1"]["ended"]


# --- the routes around the import check, run for real --------------------------------------------

THROUGH_OS = """
def run(entry):
    import os, signal
    found = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}
    found["root"] = sorted(os.listdir("/"))
    found["run"] = sorted(os.listdir("/run/domo")) if os.path.isdir("/run/domo") else None
    found["passwd"] = open("/etc/passwd").read().count("\\n")
    written = []
    for top in ("/", "/app", "/etc", "/run/domo/sandbox", "/tmp", "/root", "/home"):
        try:
            open(os.path.join(top, "x"), "w").close()
            written.append(top)
        except OSError:
            pass
    found["written"] = written
    launcher = None
    for pid in [p for p in os.listdir("/proc") if p.isdigit()]:
        try:
            if b"domo_sandbox" in open(f"/proc/{pid}/cmdline", "rb").read():
                launcher = int(pid)
        except OSError:
            pass
    found["launcher"] = launcher
    killed = {}
    for pid in [pid for pid in (1, launcher) if pid is not None]:
        try:
            os.kill(pid, signal.SIGKILL)
            killed[str(pid)] = "KILLED"
        except OSError as error:
            killed[str(pid)] = type(error).__name__
    found["kill"] = killed
    try:
        os.setuid(0)
        found["setuid"] = "ROOT"
    except OSError as error:
        found["setuid"] = type(error).__name__
    try:
        found["root_home"] = os.listdir("/root")
    except OSError as error:
        found["root_home"] = type(error).__name__
    return found
"""


def test_a_run_that_reaches_os_harms_nothing_and_finds_nothing_of_the_household(stack):
    answer = hostile_then_ordinary(stack, THROUGH_OS)
    assert answer["status"] == "done", answer
    found = answer["output"]
    print("what a run that reaches os sees:", found)
    assert found["uid"] in UIDS and found["gid"] == found["uid"] and found["groups"] == []
    assert found["written"] == []
    assert found["launcher"] is not None
    assert set(found["kill"].values()) == {"PermissionError"}, found["kill"]
    assert found["setuid"] == "PermissionError"
    assert found["root_home"] == "PermissionError"
    # The only thing of Domo in the container is the launcher's socket: no secret, no data.
    assert found["run"] == ["sandbox"]
    assert found["passwd"] > 0
    # The launcher is still the same process, still answering (``hostile_then_ordinary``).
    assert stack.inspect("sandbox")["RestartCount"] == 0


# --- hostile programs through the core: the channel and the tools' base ---------------------------
#
# Here the programs are answered by the core itself (``madetools.means``, the barrier, the real
# tools' base), not refused: each is a program Domo runs by hand (``launch.launch_by_hand``) for
# Dana, kept as ``program_run`` keeps it but without its checks (on purpose, as above). Each one is
# refused or ended, and the next launch works. Spec part 9's programs that need the network (a
# password in a response, a redirect to a closed address) are step 2's.

# Sam keeps a line in his own corner; it is read back at the end, untouched.
SAM_KEEPS = """
import domo

def run(entry):
    domo.begin("keep")
    domo.write("CREATE TABLE IF NOT EXISTS kept (secret text)", [], "mine")
    domo.write("INSERT INTO kept VALUES ($1)", ["sam-only"], "mine")
    return domo.read("SELECT secret FROM kept", [], "mine")
"""
SAM_READS = """
import domo

def run(entry):
    return domo.read("SELECT secret FROM kept", [], "mine")
"""

# Each hostile program tries its statements one by one and says how each ended: what came back,
# or the reason of the refusal.
TRIES = """
import domo

def tried(kind, sql):
    try:
        return getattr(domo, kind)(sql)
    except domo.Refused as refused:
        return refused.reason
"""

PERSONS = (
    TRIES
    + """
def run(entry):
    return {
        "public": tried("read", "SELECT * FROM public.persons"),
        "bare": tried("read", "SELECT * FROM persons"),
        "another database": tried("read", "SELECT * FROM domo.public.persons"),
    }
"""
)

OTHER_CORNER = (
    TRIES
    + """
def run(entry):
    corner = entry["arguments"]["corner"]
    domo.begin("keep")
    return {
        "read": tried("read", f"SELECT secret FROM {corner}.kept"),
        "write": tried("write", f"INSERT INTO {corner}.kept VALUES ('from dana')"),
        "path": tried("write", f"SET search_path = {corner}"),
        "after the path": tried("read", "SELECT secret FROM kept"),
    }
"""
)

SET_ROLE = (
    TRIES
    + """
def run(entry):
    domo.begin("keep")
    found = {
        "the house's": tried("write", "SET ROLE jt_house"),
        "the server's": tried("read", "SET ROLE postgres"),
        "a session's": tried("write", "SET SESSION AUTHORIZATION postgres"),
    }
    found["who"] = domo.read("SELECT current_user::text AS now, session_user::text AS logged")
    return found
"""
)

SERVER_FILES = (
    TRIES
    + """
def run(entry):
    domo.begin("keep")
    return {
        "read a file": tried("read", "SELECT pg_read_file('/etc/passwd') AS f"),
        "read its bytes": tried("read", "SELECT pg_read_binary_file('/etc/passwd') AS f"),
        "list a directory": tried("read", "SELECT pg_ls_dir('.') AS f"),
        "copy to a file": tried("write", "COPY (SELECT 1) TO '/tmp/x'"),
        "run a program": tried("write", "COPY (SELECT 1) TO PROGRAM 'id'"),
        "import a file": tried("write", "SELECT lo_import('/etc/passwd') AS f"),
    }
"""
)

TWO_STATEMENTS = (
    TRIES
    + """
def run(entry):
    domo.begin("keep")
    return {
        "write": tried("write", "CREATE TABLE twice (n int); DROP TABLE twice"),
        "read": tried("read", "SELECT 1 AS a; SELECT 2 AS b"),
        "left": domo.read("SELECT to_regclass('twice')::text AS t"),
    }
"""
)

# One request line of more than 70 000 characters: past the line's cap (64 KB).
LONG_LINE = """
import domo

def run(entry):
    domo.read("SELECT '" + "x" * 70_000 + "' AS x")
    return {"answered": True}
"""

# One request more than a launch may make.
TOO_MANY = """
import domo

def run(entry):
    for _ in range(1001):
        domo.read("SELECT 1 AS n")
    return {"all": 1001}
"""

# A request that names another run (one the launcher holds open at that moment): inside the
# request, then beside it on its line.
NAMES_ANOTHER_INSIDE = """
import domo

def run(entry):
    other = entry["arguments"]["other"]
    try:
        return {"inside": domo._ask("read", {"sql": "SELECT 1 AS n", "run": other})}
    except domo.Refused as refused:
        return {"inside": refused.reason}
"""
NAMES_ANOTHER_BESIDE = """
import json
import domo

def run(entry):
    line = {"ask": {"do": "read", "sql": "SELECT 1 AS n"}, "run": entry["arguments"]["other"]}
    domo._asks.write(json.dumps(line).encode() + bytes([10]))
    domo._asks.flush()
    return {"answer": domo._answers.readline().decode()}
"""

# A tool launched from a program while every other place of the sandbox is taken (after a
# begin: a request other than a read belongs to an operation).
NESTED = """
import domo

def run(entry):
    domo.begin("keep")
    try:
        return {"nested": domo.tool(entry["arguments"]["tool"], {})}
    except domo.Refused as refused:
        return {"nested": refused.reason}
"""

ITS_OWN = 'def run(entry):\n    return {"own": 7}\n'
DOUBLES = 'def run(entry):\n    return {"doubled": 21 * 2}\n'
HOLDS = "def run(entry):\n    import time\n    time.sleep(entry['seconds'])\n    return 'held'\n"

# Run in the core's container: the household's people and one ready tool of Domo are written,
# then each program above is launched by hand for its person through the core's own path, in the
# order below; prints what came of each as JSON.
THROUGH_THE_CORE = """
import asyncio, json, secrets, sys
from datetime import UTC, datetime
from sqlalchemy import func, select, text
from domo_common.sandbox_api import RunRequest
from domo_core.accounts.models import Person
from domo_core.config import CoreSettings
from domo_core.db import create_engine, load_models, make_session_factory
from domo_core.madetools.launch import ForWhom, LaunchDeps, launch_by_hand
from domo_core.madetools.models import ActionKind, HandRun, MadeTool, MadeToolVersion
from domo_core.madetools.risks import kept_operations, read_operations
from domo_core.madetools.service import fingerprint
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.sandbox_client import SandboxClient, new_http_client
from domo_core.toolsdb.setup import ToolsAdmin, asyncpg_dsn
from domo_core.toolsdb.spaces import space_for
from domo_core.toolsdb.store import ToolsBase

GIVEN = json.loads(sys.argv[1])
P = GIVEN["programs"]
KEEP = {"keep": {"says": "Keeps a line.", "risks": []}}


def now():
    return datetime.now(UTC)


async def main():
    load_models()
    settings = CoreSettings()
    factory = make_session_factory(create_engine(settings))
    http = new_http_client(settings.sandbox_socket)
    admin = ToolsAdmin(asyncpg_dsn(settings.database_url), settings.tools_database)
    deps = LaunchDeps(
        session_factory=factory,
        sandbox=SandboxClient(http),
        clock=now,
        policy_base=DEFAULT_POLICY,
        tools_base=ToolsBase(admin),
    )
    tag = secrets.token_hex(3)
    echo = f"echo_{tag}"
    own = GIVEN["own"]
    async with factory() as session:
        dana = Person(display_name=f"Dana {tag}", role="adult")
        sam = Person(display_name=f"Sam {tag}", role="adult")
        session.add_all([dana, sam])
        await session.flush()
        tool = MadeTool(name=echo, description="Echoes.", parameters={"type": "object"})
        session.add(tool)
        await session.flush()
        version = MadeToolVersion(
            tool_id=tool.id, fingerprint=fingerprint(own), code=own, state="ready"
        )
        session.add(version)
        await session.flush()
        tool.current_version_id = version.id
        session.add(
            ActionKind(
                action_type=f"made.{echo}",
                default_roles=["owner", "adult", "child", "guest"],
                description="Echoes.",
            )
        )
        await session.commit()
        persons = await session.scalar(select(func.count()).select_from(Person))

    async def by_hand(person, code, arguments=None, *, keeps=False, max_seconds=60):
        async with factory() as session:
            run = HandRun(
                code=code,
                fingerprint=fingerprint(code),
                operations=kept_operations(read_operations(KEEP)) if keeps else {},
                arguments=arguments or {},
                opened_by=person.id,
                for_person_id=person.id,
                max_seconds=max_seconds,
                created_at=now(),
            )
            session.add(run)
            await session.commit()
            run_id = run.id
        launched = await launch_by_hand(deps, run_id, for_whom=ForWhom(person))
        return {"status": launched.status, "result": launched.result}

    async def opened(code, entry):
        body = RunRequest(code=code, entry=entry, max_seconds=60).model_dump(mode="json")
        response = await http.post("/runs", json=body, timeout=90)
        response.raise_for_status()
        return response.json()["run"]

    found = {}
    after = found.setdefault("after", {})

    async def hostile(name, *args, **named):
        # Each hostile program, then an ordinary one: the next launch works after each.
        found[name] = await by_hand(dana, *args, **named)
        after[name] = await by_hand(dana, P["next"])

    found["sam_keeps"] = await by_hand(sam, P["sam_keeps"], keeps=True)
    async with factory() as session:
        corner = (await space_for(session, admin, sam.id)).corner
        await session.commit()
    for name in ("persons", "set_role", "server_files", "two_statements", "long_line"):
        await hostile(name, P[name], keeps=True)
    await hostile("other_corner", P["other_corner"], {"corner": corner}, keeps=True)
    await hostile("too_many", P["too_many"], max_seconds=600)
    other = await opened(own, {})
    await hostile("inside", P["inside"], {"other": other})
    await hostile("beside", P["beside"], {"other": other})
    answered = await http.post(f"/runs/{other}/next", json={"answer": None}, timeout=90)
    found["the other run"] = answered.json()
    holders = [await opened(GIVEN["holds"], {"seconds": 50}) for _ in range(3)]
    # The ordinary run after it takes the place the nested one could not have.
    await hostile("nested", P["nested"], {"tool": echo}, keeps=True)
    for holder in holders:
        await http.post(f"/runs/{holder}/stop", timeout=30)
    found["next"] = await by_hand(dana, P["next"])
    found["sam_reads"] = await by_hand(sam, P["sam_reads"])
    async with factory() as session:
        found["persons_counted"] = [
            persons,
            await session.scalar(select(func.count()).select_from(Person)),
        ]
    # What this script wrote in its stack's database goes (the corners with their people, by the
    # upkeep's pass of the corners).
    people = [str(dana.id), str(sam.id)]
    found["cleanup"] = []
    for statement in (
        "DELETE FROM hand_runs WHERE for_person_id = ANY(CAST(:people AS uuid[]))",
        "DELETE FROM made_tools WHERE name = :echo",
        "DELETE FROM action_kinds WHERE action_type = :kind",
        "DELETE FROM persons WHERE id = ANY(CAST(:people AS uuid[]))",
    ):
        async with factory() as session:
            try:
                await session.execute(
                    text(statement), {"people": people, "echo": echo, "kind": f"made.{echo}"}
                )
                await session.commit()
            except Exception as error:
                found["cleanup"].append(f"{statement[:40]}: {type(error).__name__}: {error}"[:300])
    async with factory() as session:
        found["left"] = await session.scalar(
            select(func.count()).select_from(Person).where(Person.id.in_([dana.id, sam.id]))
        )
    await http.aclose()
    print(json.dumps(found))


asyncio.run(main())
"""
# How long the whole script may take: the 1 001 requests go one by one through the channel.
THROUGH_THE_CORE_TIMEOUT_S = 900


@pytest.fixture(scope="module")
def through_the_core(stack: StackInfo) -> dict[str, Any]:
    """What came of each hostile program launched by hand through the core, in the order of
    ``THROUGH_THE_CORE``."""
    given = {
        "programs": {
            "sam_keeps": SAM_KEEPS,
            "sam_reads": SAM_READS,
            "persons": PERSONS,
            "other_corner": OTHER_CORNER,
            "set_role": SET_ROLE,
            "server_files": SERVER_FILES,
            "two_statements": TWO_STATEMENTS,
            "long_line": LONG_LINE,
            "too_many": TOO_MANY,
            "inside": NAMES_ANOTHER_INSIDE,
            "beside": NAMES_ANOTHER_BESIDE,
            "nested": NESTED,
            "next": DOUBLES,
        },
        "own": ITS_OWN,
        "holds": HOLDS,
    }
    result = stack.compose(
        "exec",
        "-T",
        "core",
        "python",
        "-c",
        THROUGH_THE_CORE,
        json.dumps(given),
        timeout=THROUGH_THE_CORE_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    found = json.loads(result.stdout.strip().splitlines()[-1])
    for name, came in found.items():
        print(f"through the core, {name}: {json.dumps(came)[:300]}")
    return found


def refused_each(came: dict[str, Any], *names: str) -> None:
    """The launch ended done, and each of ``names`` in what it gave was refused by the base."""
    assert came["status"] == "done", came
    for name in names:
        assert came["result"][name] == "sql_error", (name, came)


def test_a_program_reaches_no_table_of_the_core(through_the_core):
    refused_each(through_the_core["persons"], "public", "bare", "another database")
    # Nothing was added to the core's people by a program.
    before, after = through_the_core["persons_counted"]
    assert before == after


def test_a_program_reaches_nothing_of_another_corner_by_its_schema_name(through_the_core):
    came = through_the_core["other_corner"]
    refused_each(came, "read", "write", "after the path")
    assert "sam-only" not in json.dumps(came)
    # Sam's line is there, as he left it, and Dana's never reached it.
    assert through_the_core["sam_keeps"]["result"] == [{"secret": "sam-only"}]
    assert through_the_core["sam_reads"] == {"status": "done", "result": [{"secret": "sam-only"}]}


def test_a_program_cannot_switch_its_role(through_the_core):
    came = through_the_core["set_role"]
    refused_each(came, "the house's", "the server's", "a session's")
    [who] = came["result"]["who"]
    # Still Dana's own login, nobody else's.
    assert who["now"] == who["logged"] and who["now"].startswith("jt_"), who
    assert who["now"] != "jt_house"


def test_a_program_reads_no_file_of_the_server_and_runs_nothing_there(through_the_core):
    refused_each(
        through_the_core["server_files"],
        "read a file",
        "read its bytes",
        "list a directory",
        "copy to a file",
        "run a program",
        "import a file",
    )
    assert "root:" not in json.dumps(through_the_core["server_files"])


def test_two_statements_in_one_request_are_refused_by_the_server(through_the_core):
    came = through_the_core["two_statements"]
    refused_each(came, "write", "read")
    # Neither half ran.
    assert came["result"]["left"] == [{"t": None}]


def test_a_request_line_past_its_cap_ends_the_run(through_the_core):
    """The launcher's own cap on a request line ends the run, with its own words: past it, the
    core's cap on a statement (20 000 characters) would only refuse the request."""
    came = through_the_core["long_line"]
    assert came["status"] == "failed", came
    assert came["result"]["note"] == "A request of the tool is bigger than allowed.", came
    assert "answered" not in json.dumps(came)


def test_the_request_past_a_thousand_ends_the_run(through_the_core):
    came = through_the_core["too_many"]
    assert came["status"] != "done", came
    assert "1000 requests" in json.dumps(came), came


def test_a_request_that_names_another_run_reaches_only_its_own(through_the_core):
    # Inside the request: not a field a request has.
    assert through_the_core["inside"] == {"status": "done", "result": {"inside": "not_valid"}}
    # Beside it, on its line: the launcher ends the run that wrote it.
    beside = through_the_core["beside"]
    assert beside["status"] == "failed", beside
    assert beside["result"]["note"] == "A request of the tool is not valid.", beside
    # The other run was never touched: it ends with its own result.
    other = through_the_core["the other run"]
    assert other["kind"] == "end", other
    assert (other["result"]["status"], other["result"]["output"]) == ("done", {"own": 7}), other


def test_a_tool_a_program_launches_with_every_place_taken_is_busy(through_the_core):
    assert through_the_core["nested"] == {"status": "done", "result": {"nested": "busy"}}


def test_after_every_hostile_program_the_next_launch_works(through_the_core):
    after = through_the_core["after"]
    assert set(after) == {
        "persons",
        "set_role",
        "server_files",
        "two_statements",
        "long_line",
        "other_corner",
        "too_many",
        "inside",
        "beside",
        "nested",
    }
    for name, came in after.items():
        assert came == {"status": "done", "result": {"doubled": 42}}, name
    assert through_the_core["next"] == {"status": "done", "result": {"doubled": 42}}


def test_what_the_hostile_programs_wrote_is_taken_away(through_the_core):
    assert through_the_core["cleanup"] == [] and through_the_core["left"] == 0
