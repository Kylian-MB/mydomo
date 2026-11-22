"""The vault master key: a 32-byte random key kept in a file only the gatekeeper can read."""

import os
from pathlib import Path
from secrets import token_bytes

from domo_gatekeeper.errors import MasterKeyCorruptError, MasterKeyMissingError

KEY_SIZE = 32


def create_master_key(path: Path) -> bytes:
    """Create a new random key at ``path`` and return it.

    The file is created exclusively (``FileExistsError`` if it exists, so an existing key is
    never overwritten) and private from the first instant: mode 0600, never written then
    chmod-ed. A partially written file is removed.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = token_bytes(KEY_SIZE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return key


def load_or_create_master_key(path: Path, *, vault_has_secrets: bool) -> bytes:
    """Return the master key, creating it on the very first start.

    A key is only generated when the vault is empty. If the file is missing while the vault
    already holds secrets, a fresh key would silently orphan them, so this refuses instead.
    """
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        if vault_has_secrets:
            raise MasterKeyMissingError(
                f"The master key file is missing at {path} but the vault contains secrets: "
                "restore the key from your backup."
            ) from None
        return create_master_key(path)
    if len(key) != KEY_SIZE:
        raise MasterKeyCorruptError(
            f"The master key file at {path} is corrupt: expected {KEY_SIZE} bytes, "
            f"found {len(key)}."
        )
    return key
