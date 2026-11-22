"""Password hashing: argon2id through ``argon2-cffi``, with the library's default parameters
(RFC 9106's low-memory profile: 64 MiB, 3 passes, 4 lanes).

Hashing and verifying are slow on purpose (tens of milliseconds) and take 64 MiB each. The
requests never run them themselves: they go through the limited path (``verify_password_limited``,
``dummy_verify_limited``, ``hash_password_limited``), where at most ``VERIFY_CONCURRENCY`` run at
once, in threads of their own, while the others wait for their turn. So a flood of logins (each
with a username of its own, which the per-username throttle does not stop) costs at most
``VERIFY_CONCURRENCY`` × 64 MiB and as many cores, never the whole server. A request that waits for
its turn must not hold a connection of the database meanwhile: the callers end their transaction
before they ask (``accounts.routes``), or the waiting requests would empty the pool.

The line is bounded, and so is the wait in it. When ``MAX_WAITING_HASHES`` requests wait already,
the next one is refused at once, and when a request has waited ``HASH_WAIT_TIMEOUT_S`` without
its turn coming, it gives its place up and is refused: ``Argon2Busy``, and nothing of it is
hashed. The routes answer it with a 503 and ``Retry-After: BUSY_RETRY_AFTER_S``, the same answer
whatever the request was about (a username that exists or not, a right password or a wrong one).
So a flood that lasts cannot pile up requests without end, and once it stops the line empties in
seconds.

A place is given back in three cases only: when its hash starts (a hash that runs keeps its
thread until argon2 ends: it cannot be stopped halfway), when its wait times out, and when the
task of its request is cancelled before its turn. That last one depends on the server: a plain
form post is not cancelled when its visitor leaves, so the timeout is what bounds a wait that
nobody is waiting for any more.

The hash string carries its own salt and parameters, so the parameters can be raised later without
breaking the stored hashes.
"""

import asyncio
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# The shortest password, in characters (the owner, 2026-11-12: 12 was too long to live with).
MIN_PASSWORD_LENGTH = 8
# How many argon2 hashes and verifications may run at once, for the whole process.
VERIFY_CONCURRENCY: Final = 4
# How many requests may wait for their turn at once; one more is refused at once. At tens of
# milliseconds a hash, the line empties in about a second.
MAX_WAITING_HASHES: Final = 64
# How long a request may wait for its turn before it gives up its place and is refused.
HASH_WAIT_TIMEOUT_S: Final = 30.0
# What a refused request tells the browser: when to try again (``Retry-After``, in seconds).
BUSY_RETRY_AFTER_S: Final = 10

_hasher = PasswordHasher()
# The hash of a password nobody knows. Logging in with a username that does not exist verifies
# against it, so that the request costs as much as one for an account that does.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))
# The threads of the limited path: there are ``VERIFY_CONCURRENCY`` of them, so that is how many
# hashes run at once; the work handed to them waits in their queue, first come first served. They
# belong to no event loop.
_argon2_threads = ThreadPoolExecutor(max_workers=VERIFY_CONCURRENCY, thread_name_prefix="argon2")
# How many requests wait in the line now (handed over, not started). Requests of any event loop and
# the threads of argon2 change it: a lock keeps it right.
_line_lock = threading.Lock()
_waiting = 0


class Argon2Busy(Exception):
    """The limited path could not take the request: ``MAX_WAITING_HASHES`` wait already, or it
    waited ``HASH_WAIT_TIMEOUT_S`` without its turn coming. Nothing was hashed. The routes answer
    503 with ``Retry-After: BUSY_RETRY_AFTER_S`` (``busy_headers``)."""


def busy_headers() -> dict[str, str]:
    """The headers of the answer to ``Argon2Busy``: come back in ``BUSY_RETRY_AFTER_S``."""
    return {"Retry-After": str(BUSY_RETRY_AFTER_S)}


def waiting_hashes() -> int:
    """How many requests wait for their turn now (for tests and diagnostics)."""
    with _line_lock:
        return _waiting


class _Place:
    """A place in the line, given back once: when its hash starts, or when its request gives up
    before that."""

    def __init__(self) -> None:
        self._held = True

    def give_back(self) -> None:
        global _waiting
        with _line_lock:
            if self._held:
                self._held = False
                _waiting -= 1


def _take_place() -> _Place:
    """A place in the line; ``Argon2Busy`` when it is full."""
    global _waiting
    with _line_lock:
        if _waiting >= MAX_WAITING_HASHES:
            raise Argon2Busy("the line of argon2 is full")
        _waiting += 1
    return _Place()


def _start[T](place: _Place, work: Callable[[], T]) -> T:
    """Run in a thread of argon2: the request's turn has come."""
    place.give_back()
    return work()


async def _limited[T](work: Callable[[], T]) -> T:
    """Run ``work`` on the limited path (see the module docstring)."""
    place = _take_place()
    try:
        handed = _argon2_threads.submit(_start, place, work)
        result = asyncio.wrap_future(handed)
        try:
            done, _ = await asyncio.wait({result}, timeout=HASH_WAIT_TIMEOUT_S)
        except asyncio.CancelledError:
            handed.cancel()  # gives the place up if the turn has not come; a running hash ends
            raise
        if not done and handed.cancel():
            raise Argon2Busy("the wait for argon2 took too long")
        # Done, or started just in time: a running hash ends within its own time.
        return await result
    finally:
        place.give_back()


def hash_password(plain: str) -> str:
    """The argon2id hash of ``plain`` (salted: the same password hashes differently each time).
    Slow: async code calls ``hash_password_limited``."""
    return _hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    """Whether ``plain`` is the password ``password_hash`` was made from.

    A hash that is malformed, and a password that cannot be encoded, simply do not verify: the
    answer is ``False``, never an exception. Slow: async code calls ``verify_password_limited``.
    """
    try:
        return _hasher.verify(password_hash, plain)
    except (VerificationError, InvalidHashError, UnicodeError):
        return False


def dummy_verify(plain: str) -> None:
    """Spend the time of one verification, for a login that names no account."""
    verify_password(_DUMMY_HASH, plain)


async def verify_password_limited(password_hash: str, plain: str) -> bool:
    """``verify_password``, on the limited path: it waits for its turn, then runs in a thread of
    argon2. ``Argon2Busy`` when the line is full or the wait too long."""
    return await _limited(partial(verify_password, password_hash, plain))


async def dummy_verify_limited(plain: str) -> None:
    """``dummy_verify``, on the limited path: a login that names no account waits in the same line,
    spends the same time and is refused the same way as one that does (R48)."""
    await _limited(partial(dummy_verify, plain))


async def hash_password_limited(plain: str) -> str:
    """``hash_password``, on the limited path (``Argon2Busy`` as ``verify_password_limited``)."""
    return await _limited(partial(hash_password, plain))
