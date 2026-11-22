"""Throttling of attempts that can be guessed at: by default five per key in any window of
fifteen minutes.

The login counts per username. The one-time setup code (``setup_flow.owner_routes``) counts
wrong guesses for the whole instance, with a limit of its own. Others choose their own limit and
window: the passkey login options, per client address, for the life of a challenge
(``accounts.passkey_routes``).

The state lives in the memory of the process (the core runs as a single process in 1a), behind a
small class that takes its clock as a parameter, so that tests control time.

The attempt is counted *before* the password is verified, and forgotten if the login succeeds:
verifying takes a while, so counting afterwards would let a burst of parallel requests all get
through before the first failure was recorded.
"""

import hashlib
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from domo_core.accounts.models import Account

MAX_FAILED_ATTEMPTS = 5
WINDOW = timedelta(minutes=15)
# Usernames are hashed and the table is bounded, so that someone trying millions of different
# names cannot fill the memory. When it is full, the name that has been idle longest is forgotten:
# flushing a blocked name this way takes thousands of requests, each costing a verification.
MAX_TRACKED = 10_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _key(username: str) -> str:
    return hashlib.sha256(username.encode(errors="surrogatepass")).hexdigest()


class LoginThrottle:
    """Counts attempts per key: the login's username (already normalised by the caller), or a
    fixed key for something that is throttled as a whole. ``max_attempts`` of them are allowed in
    any ``window`` (fifteen minutes unless said otherwise)."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        max_tracked: int = MAX_TRACKED,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        window: timedelta = WINDOW,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if window <= timedelta(0):
            raise ValueError("window must be longer than nothing")
        self._clock = clock
        self._max_tracked = max_tracked
        self._max_attempts = max_attempts
        self._window = window
        # Per username (hashed), the times of its counted attempts still inside the window, in
        # order; the dictionary itself is ordered by the time of each name's latest attempt.
        self._attempts: OrderedDict[str, deque[datetime]] = OrderedDict()

    def register_attempt(self, username: str) -> float | None:
        """Count one attempt for ``username``.

        Returns ``None`` when the attempt may go ahead (it is counted, as a failure until
        ``clear`` says otherwise). Returns the number of seconds to wait when ``username`` has
        already had ``max_attempts`` attempts (five by default) in the last ``window``: the
        attempt is then refused and not counted, so that hammering a blocked username does not
        prolong the block.
        """
        now = self._clock()
        self._forget_stale(now)
        key = _key(username)
        attempts = self._attempts.get(key) or deque[datetime]()
        while attempts and attempts[0] <= now - self._window:
            attempts.popleft()
        if len(attempts) >= self._max_attempts:
            # Refused attempts are not counted, so there are exactly ``max_attempts``: the
            # username is allowed again when the oldest one leaves the window.
            return max((attempts[0] + self._window - now).total_seconds(), 1.0)
        attempts.append(now)
        self._attempts[key] = attempts
        self._attempts.move_to_end(key)
        while len(self._attempts) > self._max_tracked:
            self._attempts.popitem(last=False)
        return None

    def clear(self, username: str) -> None:
        """Forget the attempts of ``username`` (its owner logged in)."""
        self._attempts.pop(_key(username), None)

    def tracked_count(self) -> int:
        """How many usernames are being tracked (for tests)."""
        return len(self._attempts)

    def _forget_stale(self, now: datetime) -> None:
        """Drop the names whose latest attempt left the window (the oldest come first)."""
        while self._attempts:
            key, attempts = next(iter(self._attempts.items()))
            if attempts and attempts[-1] > now - self._window:
                break
            del self._attempts[key]


def password_check_key(account: Account) -> str:
    """The key of ``account`` in the throttle of the password checks: the account page, the
    password asked before adding a passkey and the proof by password count under this one key
    (``account_routes.get_password_check_throttle``)."""
    return f"account:{account.id}"
