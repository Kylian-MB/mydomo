"""The one-time setup code.

Anyone who can reach the web page on the local network could claim a fresh instance by creating
its owner first. To prevent that, the core generates a code when it starts, writes it in its logs
(which only whoever runs the server can read) and asks for it when the owner is created.

The code is twelve characters out of an alphabet of 31 (about 59 bits), shown in three groups
(``XXXX-XXXX-XXXX``). The alphabet leaves out the characters that look alike (``0``/``O``,
``1``/``I``/``L``). Only the SHA-256 of the code is stored: the code itself exists in the logs and
in nobody's database. A plain hash is enough for something this random and this short-lived (a
new code replaces the old one at each start, and creating the owner clears it); wrong guesses are
also throttled (``owner_routes``).
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.setup_flow.models import SetupState
from domo_core.setup_flow.state import owner_exists

SETUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_GROUPS: Final = 3
_GROUP_LENGTH: Final = 4

# What a submitted code is compared with when no code exists (the owner is created, or nothing
# was ever issued): the comparison takes as long, and nothing can match it.
_NO_CODE_HASH: Final = hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def generate_setup_code() -> str:
    """A new random code, ``XXXX-XXXX-XXXX``."""
    return "-".join(
        "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(_GROUP_LENGTH))
        for _ in range(_GROUPS)
    )


def normalize_setup_code(typed: str) -> str:
    """A typed code in the form it was shown: upper case, with its dashes.

    Whitespace and dashes are ignored, so that a code typed on a phone (lower case, no dashes) or
    pasted with a trailing newline works. Anything that is not twelve characters stays as it is
    (without the ignored characters) and so matches nothing.
    """
    compact = "".join(ch for ch in typed.upper() if not ch.isspace() and ch != "-")
    if len(compact) != _GROUPS * _GROUP_LENGTH:
        return compact
    return "-".join(
        compact[start : start + _GROUP_LENGTH] for start in range(0, len(compact), _GROUP_LENGTH)
    )


def _hash(code: str) -> str:
    # ``surrogatepass``: whatever was typed can be hashed, and so can only fail to match.
    return hashlib.sha256(code.encode("utf-8", errors="surrogatepass")).hexdigest()


async def rotate_setup_code(session: AsyncSession) -> str | None:
    """Issue a new setup code, which replaces (and so invalidates) the previous one.

    Returns ``None`` when the instance already has its owner: there is nothing left to claim.
    Otherwise stores the SHA-256 of the new code and returns the code, for the caller to show to
    whoever runs the server. Nothing is committed: the caller owns the transaction.
    """
    if await owner_exists(session):
        return None
    code = generate_setup_code()
    result = await session.execute(
        update(SetupState)
        .where(SetupState.id == 1)
        .values(setup_code_hash=_hash(code), code_created_at=datetime.now(UTC))
        .returning(SetupState.id)
    )
    if result.scalar_one_or_none() is None:
        raise RuntimeError("the setup_state row is missing: the database migrations have not run")
    return code


async def verify_setup_code(session: AsyncSession, typed: str) -> bool:
    """Whether ``typed`` is the current setup code (see ``normalize_setup_code`` for the spelling
    that is accepted).

    The hashes are compared in constant time, and a comparison is made when there is no code too,
    so that the time taken does not tell whether one exists.
    """
    result = await session.execute(select(SetupState.setup_code_hash).where(SetupState.id == 1))
    stored = result.scalar_one_or_none()
    expected = _NO_CODE_HASH if stored is None else stored
    matches = hmac.compare_digest(_hash(normalize_setup_code(typed)), expected)
    return matches and stored is not None
