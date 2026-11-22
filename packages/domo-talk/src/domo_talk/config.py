"""The one file where the program keeps what it must: the address of Domo, the secret of this
computer's device, and where the certificate authority of Domo is, if the person gave one.

The file is in the user's configuration folder (``%APPDATA%\\domo-talk`` on Windows, else
``$XDG_CONFIG_HOME/domo-talk`` or ``~/.config/domo-talk``), made readable by its owner alone
where the system has file modes. The secret is written there and nowhere else, and never shown.
"""

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class Saved:
    """What a pairing leaves: where Domo is, the secret of this device (never in a repr), and
    the path of the certificate authority to trust, if any."""

    url: str
    secret: str = field(repr=False)
    ca: str | None


def config_dir() -> Path:
    """The folder of the program in the user's configuration folder (read from the environment
    each time, so that a test points it at a temporary place)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / "domo-talk"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> Saved | None:
    """What was saved; ``None`` when nothing was, or when the file cannot be read as it is."""
    try:
        found: object = json.loads(config_path().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    fields = cast(dict[str, object], found)
    url, secret, ca = fields.get("url"), fields.get("secret"), fields.get("ca")
    if not isinstance(url, str) or not isinstance(secret, str):
        return None
    return Saved(url=url, secret=secret, ca=ca if isinstance(ca, str) else None)


def save(saved: Saved) -> None:
    """Write ``saved``, replacing what was there. The file is created with its tight mode from the
    start, never made tight afterwards."""
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps({"url": saved.url, "secret": saved.secret, "ca": saved.ca})
    temporary = path.with_name(path.name + ".new")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(temporary, path)
    finally:
        # Whatever stopped the write, the secret is not left in the temporary file.
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def forget() -> None:
    """Delete the saved secret (nothing to delete is not an error)."""
    with contextlib.suppress(FileNotFoundError):
        config_path().unlink()
