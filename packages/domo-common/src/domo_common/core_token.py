"""The token the core authenticates to the gatekeeper with, read from its file.

The ``init`` service writes the token to a file in a volume that the core and the gatekeeper both
mount; each reads it when it starts. A token that one side accepts and the other refuses would
make every call fail with 401, so the rule and the reading live here, in one place.
"""

import re
from pathlib import Path

# What can travel in an HTTP header and survives being typed into an SDK's "API key" field:
# printable ASCII, without spaces.
_TOKEN = re.compile(rb"[\x21-\x7e]+")


class CoreTokenError(ValueError):
    """The core token file cannot be used.

    The message names the file and its problem, never its content: the token is a credential.
    """


def load_core_token(path: Path) -> str:
    """The token in the file at ``path``, without its surrounding whitespace.

    There is no fallback: a service without the token would either accept anyone or nobody.
    Raises ``CoreTokenError`` when the file is missing, unreadable, empty, or holds something
    that cannot be a header value.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise CoreTokenError(
            f"The core token file is missing at {path}. The init service creates it: "
            "check that it ran, or set DOMO_CORE_TOKEN_FILE."
        ) from None
    except OSError as exc:
        raise CoreTokenError(
            f"The core token file at {path} cannot be read: {exc.strerror or 'I/O error'}."
        ) from None
    token = raw.strip()
    if not token:
        raise CoreTokenError(f"The core token file at {path} is empty.")
    if _TOKEN.fullmatch(token) is None:
        raise CoreTokenError(f"The core token in {path} must be printable ASCII without spaces.")
    return token.decode("ascii")
