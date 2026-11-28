"""Fetching a candidate's weights at first need, into ``model_dir/<id>/``.

Every file is pinned by the data file (revision, size, digest): a download is resumed where it
stopped, checked against its digest, and only then put in place. A file whose digest is wrong is
removed and refused. Nothing but the weights is written.
"""

import hashlib
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

from domo_speech.data import FileData, ModelEntryData

logger = logging.getLogger(__name__)

SOURCE: Final = "https://huggingface.co"
_CHUNK: Final = 1024 * 1024
_TIMEOUT: Final = 60.0
# Written once every file of a model was checked, so that a start does not hash gigabytes again.
_CHECKED: Final = ".checked"


class WeightsError(Exception):
    """The weights could not be fetched or were not the published ones (a plain code, never a
    path or a body)."""


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_CHUNK):
            sha.update(block)
    return sha.hexdigest()


def _target(root: Path, file: FileData) -> Path:
    target = (root / file.path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise WeightsError("bad_path")
    return target


def _fetch(url: str, part: Path, file: FileData) -> None:
    """Downloads ``url`` into ``part``, resuming what is already there."""
    have = part.stat().st_size if part.exists() else 0
    if have > file.size:
        part.unlink()
        have = 0
    if have == file.size:
        return
    request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            resumed = response.status == 206
            with part.open("ab" if resumed else "wb") as out:
                while block := response.read(_CHUNK):
                    out.write(block)
                    if out.tell() > file.size:
                        break
    except (urllib.error.URLError, TimeoutError, OSError):
        raise WeightsError("unreachable") from None


def ensure_weights(entry: ModelEntryData, model_dir: Path, *, source: str = SOURCE) -> Path:
    """The directory holding ``entry``'s weights, fetched first if they are not all there.

    Raises ``WeightsError`` when a file cannot be fetched, or is not the published one."""
    root = model_dir / entry.id
    checked = root / _CHECKED
    stamp = "\n".join(f"{f.sha256} {f.path}" for f in entry.files)
    if (
        checked.exists()
        and checked.read_text(encoding="utf-8") == stamp
        and all(_target(root, f).is_file() for f in entry.files)
    ):
        return root
    for file in entry.files:
        target = _target(root, file)
        if target.is_file() and target.stat().st_size == file.size:
            if _digest(target) == file.sha256:
                continue
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        logger.info("Fetching the weights of %s (%s)", entry.id, file.path)
        _fetch(f"{source}/{entry.repo}/resolve/{entry.revision}/{file.path}", part, file)
        if part.stat().st_size != file.size or _digest(part) != file.sha256:
            part.unlink(missing_ok=True)
            logger.warning("The weights of %s were not the published ones", entry.id)
            raise WeightsError("wrong_digest")
        part.replace(target)
    checked.write_text(stamp, encoding="utf-8")
    return root
