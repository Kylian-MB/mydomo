from pathlib import Path

import pytest

from scripts.check_personal_data import Match, denylist_path, find_matches, main


def test_find_matches_is_case_insensitive(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("hello\nSome PrivateTerm here\n", encoding="utf-8")
    assert find_matches([f], ["privateterm"]) == [Match(path=f, line=2)]


def test_find_matches_reports_one_match_per_line_in_order(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("alpha beta\nnothing\nbeta\n", encoding="utf-8")
    assert find_matches([f], ["alpha", "beta"]) == [Match(path=f, line=1), Match(path=f, line=3)]


def test_find_matches_ignores_empty_terms(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("anything at all\n", encoding="utf-8")
    assert find_matches([f], ["", "  "]) == []


def test_find_matches_skips_binary_and_missing_files(tmp_path: Path) -> None:
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01privateterm\x00")
    missing = tmp_path / "gone.md"
    assert find_matches([binary, missing], ["privateterm"]) == []


def test_missing_denylist_warns_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOMO_DENYLIST", str(tmp_path / "absent.txt"))
    assert main([str(tmp_path / "x.md")]) == 0
    assert "no denylist" in capsys.readouterr().err


def test_match_fails_the_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("privateterm\n", encoding="utf-8")
    f = tmp_path / "a.py"
    f.write_text("x = 'PRIVATETERM'\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(deny))
    assert main([str(f)]) == 1


def test_clean_files_pass_the_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("privateterm\n", encoding="utf-8")
    f = tmp_path / "a.py"
    f.write_text("x = 'harmless'\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(deny))
    assert main([str(f)]) == 0


def test_report_names_the_location_but_never_the_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("privateterm\n", encoding="utf-8")
    f = tmp_path / "a.py"
    f.write_text("ok\nx = 'PRIVATETERM'\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(deny))
    assert main([str(f)]) == 1
    err = capsys.readouterr().err
    assert f"{f}:2" in err
    assert "privateterm" not in err.lower()


def test_blank_lines_and_comments_in_denylist_are_not_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("# a comment\n\n   \nprivateterm\n", encoding="utf-8")
    f = tmp_path / "a.py"
    f.write_text("x = 'harmless'  # a comment\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DENYLIST", str(deny))
    assert main([str(f)]) == 0


def test_denylist_path_defaults_to_the_user_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOMO_DENYLIST", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert denylist_path() == tmp_path / ".config" / "domo-dev" / "denylist.txt"
