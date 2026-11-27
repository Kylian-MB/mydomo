import subprocess
from pathlib import Path

import pytest

from scripts.check_push import commits_with_terms, foreign_roots, main


def test_a_history_starting_elsewhere_is_foreign() -> None:
    assert foreign_roots({"aaa", "bbb"}, {"aaa"}) == {"bbb"}


def test_a_history_starting_where_the_remote_does_is_not_foreign() -> None:
    assert foreign_roots({"aaa"}, {"aaa", "ccc"}) == set()


def test_a_remote_nothing_is_known_of_refuses_no_history() -> None:
    assert foreign_roots({"aaa"}, set()) == set()


def test_commits_with_terms_is_case_insensitive_and_keeps_the_order() -> None:
    texts = {"one": "A PrivateTerm here", "two": "nothing", "three": "privateterm@example.org"}
    assert commits_with_terms(texts, ["privateterm"]) == ["one", "three"]


def test_commits_with_terms_ignores_empty_terms() -> None:
    assert commits_with_terms({"one": "anything at all"}, ["", "  "]) == []


def _git(folder: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Someone",
            "-c",
            "user.email=someone@example.org",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=folder,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone one commit ahead of its remote, with the push's variables unset."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "--quiet", "--initial-branch=main")
    _git(upstream, "commit", "--quiet", "--allow-empty", "-m", "first")
    folder = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(upstream), str(folder))
    _git(folder, "commit", "--quiet", "--allow-empty", "-m", "a quiet change")
    monkeypatch.chdir(folder)
    monkeypatch.delenv("PRE_COMMIT_TO_REF", raising=False)
    monkeypatch.delenv("PRE_COMMIT_REMOTE_NAME", raising=False)
    monkeypatch.setenv("DOMO_DENYLIST", str(tmp_path / "absent.txt"))
    return folder


def test_a_branch_grown_from_the_remote_passes(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main() == 0
    assert "no denylist" in capsys.readouterr().err


def test_a_branch_from_another_history_is_refused(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(clone, "checkout", "--quiet", "--orphan", "elsewhere")
    _git(clone, "commit", "--quiet", "--allow-empty", "-m", "another past")
    root = _git(clone, "rev-parse", "HEAD")
    assert main() == 1
    assert f"starts at {root[:12]}" in capsys.readouterr().err


def test_a_term_in_a_new_commit_message_is_refused_without_being_printed(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("PrivateTerm\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(denylist))
    _git(clone, "commit", "--quiet", "--allow-empty", "-m", "mentions privateterm")
    commit = _git(clone, "rev-parse", "HEAD")
    assert main() == 1
    err = capsys.readouterr().err
    assert commit[:12] in err
    assert "privateterm" not in err.casefold()


def test_a_term_only_in_a_commit_the_remote_already_has_is_not_looked_at(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("first\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(denylist))
    assert main() == 0


def test_the_author_of_a_new_commit_is_read_too(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("someone@example.org\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(denylist))
    assert main() == 1
