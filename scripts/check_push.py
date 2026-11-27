"""Refuse a push that would publish another history, or personal data in its commits.

Run by pre-commit at the ``pre-push`` stage, on what is about to leave:

- the history must start where the remote's own history starts: a branch grown from another
  repository's past (an older, private one for instance) is refused;
- the message, the author and the committer of each new commit must hold no term of the
  personal-data denylist (the files themselves are read by ``check_personal_data.py``).

It reports commit ids only: it never prints the matched term.
"""

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

from scripts.check_personal_data import denylist_path, load_terms

_FIELDS = "%an%n%ae%n%cn%n%ce%n%B"


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return done.stdout


def foreign_roots(pushed: set[str], known: set[str]) -> set[str]:
    """The first commits of the pushed history that the remote's history does not start from.

    A remote nothing is known of yet (a first push) has no history to compare with.
    """
    return pushed - known if known else set()


def commits_with_terms(texts: Mapping[str, str], terms: Sequence[str]) -> list[str]:
    """The ids of the commits whose text holds a term, case-insensitively, in the given order."""
    needles = [term.strip().casefold() for term in terms if term.strip()]
    return [
        commit
        for commit, text in texts.items()
        if any(needle in text.casefold() for needle in needles)
    ]


def roots(*revisions: str) -> set[str]:
    return set(git("rev-list", "--max-parents=0", *revisions).split())


def remote_roots(remote: str) -> set[str]:
    if not git("for-each-ref", "--count=1", f"refs/remotes/{remote}").strip():
        return set()
    return roots(f"--remotes={remote}")


def new_commit_texts(to_ref: str, remote: str) -> dict[str, str]:
    commits = git("rev-list", "--reverse", to_ref, "--not", f"--remotes={remote}").split()
    return {commit: git("show", "--no-patch", f"--format={_FIELDS}", commit) for commit in commits}


def main() -> int:
    to_ref = os.environ.get("PRE_COMMIT_TO_REF") or "HEAD"
    remote = os.environ.get("PRE_COMMIT_REMOTE_NAME") or "origin"
    failed = False

    for root in sorted(foreign_roots(roots(to_ref), remote_roots(remote))):
        failed = True
        print(
            f"check_push: this history starts at {root[:12]}, where {remote}'s does not: "
            "it comes from another repository and is not pushed",
            file=sys.stderr,
        )

    source = denylist_path()
    if source.is_file():
        for commit in commits_with_terms(new_commit_texts(to_ref, remote), load_terms(source)):
            failed = True
            print(
                f"check_push: commit {commit[:12]}: its message, author or committer contains "
                "a term from the personal-data denylist",
                file=sys.stderr,
            )
    else:
        print(
            f"check_push: no denylist at {source}; the commits' personal-data check is skipped",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
