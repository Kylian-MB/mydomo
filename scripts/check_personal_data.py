"""Fail when a file contains a term from the developer's personal-data denylist.

The denylist lives outside the repository (one term per line), so the terms
themselves never end up in version control. It is read from the file named by
``$DOMO_DENYLIST`` or, by default, ``~/.config/domo-dev/denylist.txt``.

The hook reports ``path:line`` only: it never prints the matched term.
"""

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Match:
    path: Path
    line: int


def denylist_path() -> Path:
    override = os.environ.get("DOMO_DENYLIST")
    if override:
        return Path(override)
    return Path.home() / ".config" / "domo-dev" / "denylist.txt"


def load_terms(path: Path) -> list[str]:
    """Read the denylist: one term per line, blank lines and `#` comments ignored."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    terms = (line.strip() for line in lines)
    return [term for term in terms if term and not term.startswith("#")]


def find_matches(paths: Sequence[Path], terms: Sequence[str]) -> list[Match]:
    """Return one `Match` per line containing any term, case-insensitively.

    Binary files and files that cannot be read are skipped.
    """
    needles = [term.strip().casefold() for term in terms if term.strip()]
    if not needles:
        return []
    matches: list[Match] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.split("\n"), start=1):
            folded = line.casefold()
            if any(needle in folded for needle in needles):
                matches.append(Match(path=path, line=number))
    return matches


def main(argv: Sequence[str]) -> int:
    source = denylist_path()
    if not source.is_file():
        print(
            f"check_personal_data: no denylist at {source}; personal-data check skipped",
            file=sys.stderr,
        )
        return 0
    matches = find_matches([Path(arg) for arg in argv], load_terms(source))
    for match in matches:
        print(
            f"{match.path}:{match.line}: contains a term from the personal-data denylist",
            file=sys.stderr,
        )
    return 1 if matches else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
