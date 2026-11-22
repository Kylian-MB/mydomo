"""The secrets of an instance, created once by the ``init`` service: the database password and the
token the core authenticates to the gatekeeper with.

Giving the files to the services' users needs root: here ``os.fchown``/``os.chown`` are replaced
by recorders, and the real ownership is checked by the stack tests. File modes and owners are
POSIX matters: those tests are skipped on Windows.
"""

import logging
import os
import re
import stat
from pathlib import Path

import pytest
from domo_core import init_secrets
from domo_core.config import CoreSettings
from domo_core.gatekeeper_client import load_core_token
from domo_core.init_secrets import (
    CONTAINER_OWNERS,
    FileOwner,
    InitError,
    SecretOwners,
    ensure_instance_secrets,
)
from sqlalchemy.engine import make_url

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and links")

URLSAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,}")
OWNERS = SecretOwners(db_password=FileOwner(uid=10001, gid=999), core_token=FileOwner(10001, 10001))


def read_both(db_dir: Path, core_dir: Path) -> tuple[str, str]:
    return (
        (db_dir / "db_password").read_text(encoding="ascii"),
        (core_dir / "core_token").read_text(encoding="ascii"),
    )


class OwnershipRecorder:
    """Stands in for ``os.fchown`` and ``os.chown``: records which file was given to whom."""

    def __init__(self) -> None:
        self.given: dict[int, tuple[int, int]] = {}  # inode -> (uid, gid)
        self.chowned: list[tuple[str, int, int, bool]] = []

    def fchown(self, fd: int, uid: int, gid: int) -> None:
        self.given[os.fstat(fd).st_ino] = (uid, gid)

    def chown(self, path: str | Path, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        self.chowned.append((str(path), uid, gid, follow_symlinks))

    def owner_of(self, path: Path) -> tuple[int, int] | None:
        return self.given.get(path.stat().st_ino)


@pytest.fixture
def ownership(monkeypatch: pytest.MonkeyPatch) -> OwnershipRecorder:
    recorder = OwnershipRecorder()
    monkeypatch.setattr(os, "fchown", recorder.fchown, raising=False)
    monkeypatch.setattr(os, "chown", recorder.chown, raising=False)
    return recorder


def test_init_secrets_creates_once(tmp_path):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    first = (tmp_path / "core" / "core_token").read_text()
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    assert (tmp_path / "core" / "core_token").read_text() == first and len(first) >= 40


def test_both_secrets_are_long_random_url_safe_values_without_a_newline(tmp_path):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    password, token = read_both(tmp_path / "db", tmp_path / "core")
    assert URLSAFE_TOKEN.fullmatch(password)
    assert URLSAFE_TOKEN.fullmatch(token)
    assert password != token


def test_each_instance_gets_its_own_secrets(tmp_path):
    ensure_instance_secrets(tmp_path / "a" / "db", tmp_path / "a" / "core")
    ensure_instance_secrets(tmp_path / "b" / "db", tmp_path / "b" / "core")
    first = read_both(tmp_path / "a" / "db", tmp_path / "a" / "core")
    second = read_both(tmp_path / "b" / "db", tmp_path / "b" / "core")
    assert set(first).isdisjoint(second)


def test_the_password_is_kept_too(tmp_path):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    before = read_both(tmp_path / "db", tmp_path / "core")
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    assert read_both(tmp_path / "db", tmp_path / "core") == before


def test_an_existing_file_is_never_overwritten_and_the_missing_one_is_created(tmp_path):
    db_dir, core_dir = tmp_path / "db", tmp_path / "core"
    db_dir.mkdir()
    (db_dir / "db_password").write_text("chosen-by-the-owner", encoding="ascii")
    ensure_instance_secrets(db_dir, core_dir)
    password, token = read_both(db_dir, core_dir)
    assert password == "chosen-by-the-owner"
    assert URLSAFE_TOKEN.fullmatch(token)


def test_an_existing_empty_file_is_not_replaced_either(tmp_path):
    """The rule has no exception: a file that is there is the instance's, whatever it holds (the
    services refuse an empty one with a clear message)."""
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "core_token").write_bytes(b"")
    ensure_instance_secrets(tmp_path / "db", core_dir)
    assert (core_dir / "core_token").read_bytes() == b""


def test_no_temporary_file_is_left_behind(tmp_path):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    assert sorted(p.name for p in (tmp_path / "db").iterdir()) == ["db_password"]
    assert sorted(p.name for p in (tmp_path / "core").iterdir()) == ["core_token"]


def test_the_services_accept_what_init_wrote(tmp_path):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    password, token = read_both(tmp_path / "db", tmp_path / "core")
    settings = CoreSettings(db_password_file=tmp_path / "db" / "db_password")
    assert make_url(settings.database_url).password == password
    assert load_core_token(tmp_path / "core" / "core_token") == token


def test_the_directories_are_where_the_services_look_for_the_files():
    settings = CoreSettings()
    assert settings.db_password_file == init_secrets.DB_AUTH_DIR / "db_password"
    assert settings.core_token_file == init_secrets.CORE_AUTH_DIR / "core_token"


@posix_only
def test_the_files_are_only_readable(tmp_path):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    db_mode = stat.S_IMODE((tmp_path / "db" / "db_password").stat().st_mode)
    token_mode = stat.S_IMODE((tmp_path / "core" / "core_token").stat().st_mode)
    # The password: its owner (the core) and its group (postgres). The token: its owner alone
    # (the core and the gatekeeper run as the same user).
    assert db_mode == 0o440
    assert token_mode == 0o400


def test_without_owners_nothing_is_given_away(tmp_path, ownership):
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core")
    assert ownership.given == {}
    assert ownership.chowned == []


@posix_only
def test_the_new_files_are_given_to_the_services_users(tmp_path, ownership):
    owners = SecretOwners(db_password=FileOwner(uid=10001, gid=999), core_token=FileOwner(7, 8))
    ensure_instance_secrets(tmp_path / "db", tmp_path / "core", owners=owners)
    assert ownership.owner_of(tmp_path / "db" / "db_password") == (10001, 999)
    assert ownership.owner_of(tmp_path / "core" / "core_token") == (7, 8)


def test_the_container_layout_gives_the_password_to_postgres_and_the_core():
    # The core and the gatekeeper run as 10001:10001 (their Dockerfiles), PostgreSQL as 999:999
    # (the official image).
    assert CONTAINER_OWNERS == OWNERS


@posix_only
def test_an_existing_file_with_the_wrong_owner_gets_the_right_one_but_keeps_its_value(
    tmp_path, ownership
):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / "db_password").write_text("chosen-by-the-owner", encoding="ascii")
    ensure_instance_secrets(db_dir, tmp_path / "core", owners=OWNERS)
    assert (db_dir / "db_password").read_text(encoding="ascii") == "chosen-by-the-owner"
    # The test runs as neither 10001 nor 999: the file's owner was wrong. A link is not followed.
    assert ownership.chowned == [(str(db_dir / "db_password"), 10001, 999, False)]


@posix_only
def test_an_existing_file_with_the_right_owner_is_left_alone(tmp_path, ownership):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / "db_password").write_text("chosen-by-the-owner", encoding="ascii")
    info = (db_dir / "db_password").stat()
    current = FileOwner(uid=info.st_uid, gid=info.st_gid)
    owners = SecretOwners(db_password=current, core_token=current)
    ensure_instance_secrets(db_dir, tmp_path / "core", owners=owners)
    assert ownership.chowned == []


@posix_only
def test_a_symbolic_link_is_refused_and_its_target_left_alone(tmp_path, ownership):
    target = tmp_path / "elsewhere"
    target.write_text("not a secret", encoding="ascii")
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / "db_password").symlink_to(target)
    with pytest.raises(InitError, match="not a regular file"):
        ensure_instance_secrets(db_dir, tmp_path / "core", owners=OWNERS)
    assert target.read_text(encoding="ascii") == "not a secret"
    assert ownership.chowned == []


@posix_only
def test_giving_a_file_away_without_the_right_is_a_clear_error(tmp_path, monkeypatch):
    def refuse(fd: int, uid: int, gid: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refuse, raising=False)
    with pytest.raises(InitError, match="root") as error:
        ensure_instance_secrets(tmp_path / "db", tmp_path / "core", owners=OWNERS)
    assert "10001" in str(error.value)


@posix_only
def test_main_creates_the_files_in_the_volumes_and_gives_them_away(
    tmp_path, monkeypatch, ownership
):
    monkeypatch.setattr(init_secrets, "DB_AUTH_DIR", tmp_path / "db-auth")
    monkeypatch.setattr(init_secrets, "CORE_AUTH_DIR", tmp_path / "core-auth")
    init_secrets.main()
    assert ownership.owner_of(tmp_path / "db-auth" / "db_password") == (10001, 999)
    assert ownership.owner_of(tmp_path / "core-auth" / "core_token") == (10001, 10001)


@posix_only
def test_main_stops_with_a_message_when_it_cannot_give_the_files_away(tmp_path, monkeypatch):
    def refuse(fd: int, uid: int, gid: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refuse, raising=False)
    monkeypatch.setattr(init_secrets, "DB_AUTH_DIR", tmp_path / "db-auth")
    monkeypatch.setattr(init_secrets, "CORE_AUTH_DIR", tmp_path / "core-auth")
    with pytest.raises(SystemExit) as stopped:
        init_secrets.main()
    assert "root" in str(stopped.value)


def test_the_values_never_reach_the_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    ensure_instance_secrets(tmp_path / "db-auth", tmp_path / "core-auth")
    ensure_instance_secrets(tmp_path / "db-auth", tmp_path / "core-auth")
    password, token = read_both(tmp_path / "db-auth", tmp_path / "core-auth")
    messages = [record.getMessage() for record in caplog.records]
    assert any("db_password" in m for m in messages)  # what happened is logged...
    assert not any(password in m or token in m for m in messages)  # ...but never a value
