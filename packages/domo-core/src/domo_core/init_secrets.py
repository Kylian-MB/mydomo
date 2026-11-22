"""Creates the secrets of an instance, once: ``python -m domo_core.init_secrets``.

The ``init`` service of the compose file runs this before the other services start. It writes
two files, each in a volume mounted by exactly the services that read it:

* ``/run/domo/db-auth/db_password``: the password of the database user, read by PostgreSQL
  (``POSTGRES_PASSWORD_FILE``, when it creates the database) and by the core;
* ``/run/domo/core-auth/core_token``: the token the core authenticates to the gatekeeper
  with, read by both.

Each value is ``secrets.token_urlsafe(32)``: 43 characters, 256 random bits. A file that exists is
never rewritten, whatever it holds: the database was created with that password, and the core and
the gatekeeper share that token. Deleting a secret's volume does not change the secret anywhere
else (PostgreSQL only reads its password file when it creates the database): the volumes of an
instance go together.

Volume membership is the access boundary. Inside a volume, each file belongs to its readers and
can only be read: ``0440`` for the password (owner: the core's user; group: PostgreSQL's),
``0400`` for the token (the core and the gatekeeper run as the same user).

Giving a file to another user takes root (``chown``): the service runs as root for that reason
alone, with no capability but ``CHOWN``, no network and a read-only root filesystem. A file that
exists but belongs to someone else is given to its readers again, its content untouched: that
also mends a start that stopped between writing a file and giving it away.

The values are never logged.
"""

import logging
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Where the services look for the files (the defaults of the core's and gatekeeper's settings).
DB_AUTH_DIR: Path = Path("/run/domo/db-auth")
CORE_AUTH_DIR: Path = Path("/run/domo/core-auth")
DB_PASSWORD_NAME: Final = "db_password"
CORE_TOKEN_NAME: Final = "core_token"

# Random bytes in each value: 32 bytes are 43 URL-safe characters.
SECRET_BYTES: Final = 32

DB_PASSWORD_MODE: Final = 0o440
CORE_TOKEN_MODE: Final = 0o400

logger = logging.getLogger("domo_core.init_secrets")


class InitError(Exception):
    """The secrets could not be put in place. The message names files, never a value."""


@dataclass(frozen=True)
class FileOwner:
    uid: int
    gid: int


@dataclass(frozen=True)
class SecretOwners:
    """Who each secret file belongs to."""

    db_password: FileOwner
    core_token: FileOwner


# The users of the images: the core and the gatekeeper run as 10001:10001 (their Dockerfiles),
# PostgreSQL as 999:999 (the postgres user of the official image, which pgvector's builds on).
SERVICE_USER: Final = FileOwner(uid=10001, gid=10001)
POSTGRES_GID: Final = 999
CONTAINER_OWNERS: Final = SecretOwners(
    db_password=FileOwner(uid=SERVICE_USER.uid, gid=POSTGRES_GID),
    core_token=SERVICE_USER,
)


def ensure_instance_secrets(
    db_auth_dir: Path, core_auth_dir: Path, *, owners: SecretOwners | None = None
) -> None:
    """Create ``db_auth_dir/db_password`` and ``core_auth_dir/core_token`` when they are missing
    (and their directories), never overwriting a file that exists.

    With ``owners`` (POSIX only, and root only), each file is given to its readers. Raises
    ``InitError`` when a name is taken by something that is not a regular file, or when a file
    cannot be given away; ``OSError`` for the other failures of the file system.
    """
    _ensure_secret_file(
        db_auth_dir / DB_PASSWORD_NAME,
        DB_PASSWORD_MODE,
        None if owners is None else owners.db_password,
    )
    _ensure_secret_file(
        core_auth_dir / CORE_TOKEN_NAME,
        CORE_TOKEN_MODE,
        None if owners is None else owners.core_token,
    )


def _ensure_secret_file(path: Path, mode: int, owner: FileOwner | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _create(path, mode, owner):
        logger.info("Created %s", path)
        return
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise InitError(
            f"{path} exists but is not a regular file: remove it (or the volume) and start again."
        )
    if owner is not None and (info.st_uid, info.st_gid) != (owner.uid, owner.gid):
        _chown_path(path, owner)
        logger.info("Gave the existing %s to uid %d, gid %d", path, owner.uid, owner.gid)
    logger.info("Kept the existing %s", path)


def _create(path: Path, mode: int, owner: FileOwner | None) -> bool:
    """Write a new value to ``path`` unless something is there already (then ``False``).

    The value goes to a temporary file next to it, which is then linked under its final name: a
    link never replaces an existing file, and the final name never shows a half-written value.
    The file is given away after the link, through the descriptor still open on it (a hard link
    to a file of another user would need more than the ``CHOWN`` capability).
    """
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            _write_all(descriptor, secrets.token_urlsafe(SECRET_BYTES).encode("ascii"))
            if sys.platform != "win32":
                os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            if owner is not None:
                _chown_descriptor(descriptor, path, owner)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(path.parent)
    return True


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _cannot_give(path: Path, owner: FileOwner) -> InitError:
    return InitError(
        f"Cannot give {path} to uid {owner.uid}, gid {owner.gid}: the init service must run as "
        "root (with the CHOWN capability)."
    )


def _chown_descriptor(descriptor: int, path: Path, owner: FileOwner) -> None:
    if sys.platform == "win32":
        raise InitError("File owners can only be set on a POSIX system.")
    try:
        os.fchown(descriptor, owner.uid, owner.gid)
    except PermissionError:
        raise _cannot_give(path, owner) from None


def _chown_path(path: Path, owner: FileOwner) -> None:
    if sys.platform == "win32":
        raise InitError("File owners can only be set on a POSIX system.")
    try:
        os.chown(path, owner.uid, owner.gid, follow_symlinks=False)
    except PermissionError:
        raise _cannot_give(path, owner) from None


def _sync_directory(directory: Path) -> None:
    """Make the new name durable (POSIX; Windows cannot open a directory this way)."""
    if sys.platform == "win32":
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        ensure_instance_secrets(DB_AUTH_DIR, CORE_AUTH_DIR, owners=CONTAINER_OWNERS)
    except (InitError, OSError) as exc:
        # Paths and reasons only: no value is ever part of an error.
        sys.exit(f"domo-init: {exc}")


if __name__ == "__main__":
    main()
