import threading

import pytest
from domo_core.accounts import passwords
from domo_core.accounts.passwords import (
    MIN_PASSWORD_LENGTH,
    VERIFY_CONCURRENCY,
    Argon2Busy,
    dummy_verify,
    dummy_verify_limited,
    hash_password,
    hash_password_limited,
    verify_password,
    verify_password_limited,
    waiting_hashes,
)


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple")
    assert not verify_password(h, "wrong password!!")


def test_minimum_length_is_eight():
    assert MIN_PASSWORD_LENGTH == 8


def test_hash_is_argon2id_and_never_contains_the_password():
    h = hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert "correct horse" not in h


def test_hashes_are_salted():
    assert hash_password("same password, twice") != hash_password("same password, twice")


@pytest.mark.parametrize(
    "password", ["pässwörd-with-accents", "密码密码密码密码密码密码", "x" * 5000]
)
def test_unicode_and_long_passwords_roundtrip(password: str):
    assert verify_password(hash_password(password), password)


def test_verification_is_case_and_whitespace_sensitive():
    h = hash_password("Correct Horse")
    assert not verify_password(h, "correct horse")
    assert not verify_password(h, "Correct Horse ")


@pytest.mark.parametrize("bad_hash", ["", "hash", "$argon2id$garbage", "plaintext-password"])
def test_a_malformed_stored_hash_never_verifies_and_never_raises(bad_hash: str):
    assert verify_password(bad_hash, "anything at all") is False


def test_a_password_that_cannot_be_encoded_is_simply_wrong():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "\ud800 lone surrogate") is False


def test_dummy_verify_does_the_work_of_one_verification(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []
    real_verify = passwords.verify_password

    def spy(password_hash: str, plain: str) -> bool:
        calls.append((password_hash, plain))
        return real_verify(password_hash, plain)

    monkeypatch.setattr(passwords, "verify_password", spy)
    assert dummy_verify("some password") is None
    assert len(calls) == 1
    password_hash, plain = calls[0]
    assert password_hash.startswith("$argon2id$")  # a real hash, with the real parameters
    assert plain == "some password"


def test_dummy_verify_accepts_any_text():
    dummy_verify("")
    dummy_verify("\ud800")


# --- the limited path (task 14) ------------------------------------------------------------------


async def test_the_limited_path_answers_like_the_plain_functions():
    h = await hash_password_limited("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple")
    assert await verify_password_limited(h, "correct horse battery staple") is True
    assert await verify_password_limited(h, "wrong password!!") is False
    assert await verify_password_limited("not-a-hash", "anything at all") is False
    assert await dummy_verify_limited("anything at all") is None


async def test_the_limited_path_runs_in_the_threads_of_argon2(monkeypatch):
    """Off the event loop, and in threads of its own: not the pool of the sync routes."""
    threads: list[str] = []
    real = passwords._hasher

    class Spy:
        def hash(self, password: str) -> str:
            threads.append(threading.current_thread().name)
            return real.hash(password)

        def verify(self, password_hash: str, password: str) -> bool:
            threads.append(threading.current_thread().name)
            return real.verify(password_hash, password)

    monkeypatch.setattr(passwords, "_hasher", Spy())
    h = await hash_password_limited("correct horse battery staple")
    await verify_password_limited(h, "correct horse battery staple")
    await dummy_verify_limited("anything at all")
    assert len(threads) == 3
    assert all(name.startswith("argon2") for name in threads), threads


def test_four_hashes_at_most_run_at_once():
    assert VERIFY_CONCURRENCY == 4
    assert passwords._argon2_threads._max_workers == VERIFY_CONCURRENCY


async def test_a_full_line_refuses_at_once_without_hashing(monkeypatch):
    """Fix round 1: beyond ``MAX_WAITING_HASHES`` waiting callers, nothing is handed to argon2."""
    called: list[str] = []

    class Spy:
        def hash(self, password: str) -> str:
            called.append("hash")
            return "never"

        def verify(self, password_hash: str, password: str) -> bool:
            called.append("verify")
            return True

    monkeypatch.setattr(passwords, "_hasher", Spy())
    monkeypatch.setattr(passwords, "MAX_WAITING_HASHES", 0)
    with pytest.raises(Argon2Busy):
        await verify_password_limited("hash", "plain")
    with pytest.raises(Argon2Busy):
        await dummy_verify_limited("plain")
    with pytest.raises(Argon2Busy):
        await hash_password_limited("plain")
    assert called == [] and waiting_hashes() == 0
