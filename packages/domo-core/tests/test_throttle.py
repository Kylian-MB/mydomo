from datetime import timedelta

from domo_core.accounts.throttle import MAX_FAILED_ATTEMPTS, WINDOW, LoginThrottle


def test_the_limits_are_five_attempts_in_fifteen_minutes():
    assert MAX_FAILED_ATTEMPTS == 5
    assert timedelta(minutes=15) == WINDOW


def test_five_attempts_are_allowed_the_sixth_is_not(clock):
    throttle = LoginThrottle(clock=clock)
    assert [throttle.register_attempt("owner") for _ in range(5)] == [None] * 5
    wait = throttle.register_attempt("owner")
    assert wait is not None
    assert 0 < wait <= WINDOW.total_seconds()


def test_the_block_ends_when_the_window_has_passed(clock):
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        throttle.register_attempt("owner")
    clock.advance(WINDOW - timedelta(seconds=1))
    assert throttle.register_attempt("owner") is not None
    clock.advance(timedelta(seconds=2))
    assert throttle.register_attempt("owner") is None


def test_the_wait_shrinks_as_time_passes(clock):
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        throttle.register_attempt("owner")
    first = throttle.register_attempt("owner")
    clock.advance(timedelta(minutes=5))
    second = throttle.register_attempt("owner")
    assert first == WINDOW.total_seconds()
    assert second == (WINDOW - timedelta(minutes=5)).total_seconds()


def test_blocked_attempts_do_not_extend_the_block(clock):
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        throttle.register_attempt("owner")
    for _ in range(50):
        clock.advance(timedelta(seconds=10))
        throttle.register_attempt("owner")  # blocked: must not count
    clock.advance(WINDOW)  # 50 * 10 s already passed: far beyond the 15 minutes
    assert throttle.register_attempt("owner") is None


def test_the_window_slides(clock):
    """Only the attempts of the last 15 minutes count, whenever the first one was."""
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):  # attempts at minutes 0, 4, 8 and 12
        throttle.register_attempt("owner")
        clock.advance(timedelta(minutes=4))
    # It is minute 16: the first attempt is out of the window, three remain.
    assert throttle.register_attempt("owner") is None  # the 4th in the window
    assert throttle.register_attempt("owner") is None  # the 5th
    assert throttle.register_attempt("owner") is not None  # five in the window: blocked


def test_usernames_are_independent(clock):
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        throttle.register_attempt("owner")
    assert throttle.register_attempt("owner") is not None
    assert throttle.register_attempt("someone-else") is None


def test_clear_forgets_the_attempts(clock):
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):
        throttle.register_attempt("owner")
    throttle.clear("owner")
    assert [throttle.register_attempt("owner") for _ in range(5)] == [None] * 5


def test_clearing_an_unknown_username_is_harmless(clock):
    LoginThrottle(clock=clock).clear("nobody")


def test_memory_is_bounded_whatever_usernames_are_tried(clock):
    throttle = LoginThrottle(clock=clock, max_tracked=100)
    for index in range(1000):
        throttle.register_attempt(f"user-{index}-" + "x" * 5000)
    assert throttle.tracked_count() <= 100


def test_the_username_idle_longest_is_forgotten_first(clock):
    throttle = LoginThrottle(clock=clock, max_tracked=3)
    for _ in range(5):
        throttle.register_attempt("a")
        clock.advance(timedelta(seconds=1))
    assert throttle.register_attempt("a") is not None
    for name in ("b", "c", "d"):  # the table is full: "a" has been idle the longest
        throttle.register_attempt(name)
        clock.advance(timedelta(seconds=1))
    assert throttle.tracked_count() == 3
    assert throttle.register_attempt("a") is None  # forgotten
    assert throttle.register_attempt("d") is None


def test_stale_entries_are_dropped(clock):
    throttle = LoginThrottle(clock=clock)
    throttle.register_attempt("owner")
    clock.advance(WINDOW + timedelta(seconds=1))
    throttle.register_attempt("someone-else")  # any call prunes the stale entries
    assert throttle.tracked_count() == 1


def test_the_number_of_attempts_can_be_chosen(clock):
    throttle = LoginThrottle(clock=clock, max_attempts=3)
    assert [throttle.register_attempt("key") for _ in range(3)] == [None] * 3
    wait = throttle.register_attempt("key")
    assert wait is not None and 0 < wait <= WINDOW.total_seconds()
    clock.advance(WINDOW + timedelta(seconds=1))
    assert throttle.register_attempt("key") is None


def test_five_attempts_remain_the_default(clock):
    throttle = LoginThrottle(clock=clock)
    assert [throttle.register_attempt("key") for _ in range(5)] == [None] * 5
    assert throttle.register_attempt("key") is not None


def test_the_window_can_be_chosen(clock):
    """R91: the login options of passkeys are held back for the life of a challenge."""
    window = timedelta(minutes=5)
    throttle = LoginThrottle(clock=clock, max_attempts=2, window=window)
    assert [throttle.register_attempt("key") for _ in range(2)] == [None] * 2
    assert throttle.register_attempt("key") == window.total_seconds()
    clock.advance(window - timedelta(seconds=1))
    assert throttle.register_attempt("key") is not None
    clock.advance(timedelta(seconds=2))
    assert throttle.register_attempt("key") is None
    clock.advance(window + timedelta(seconds=1))
    throttle.register_attempt("other")  # any call prunes what left the window
    assert throttle.tracked_count() == 1
