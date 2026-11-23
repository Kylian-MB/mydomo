import json
from pathlib import Path

import pytest

from scripts.check_pr_description import find_problems, main, render_report

TEMPLATE = Path(".github/pull_request_template.md").read_text(encoding="utf-8")

COMPLETE = """\
## Summary
Adds a reminder for the bins.

## Why
People forget them on Tuesdays.

## What changes
A new plan in the core.

## Behaviour before and after
Before: no reminder. After: one on Monday evening.

## How it was tested
uv run pytest -m "not stack": all pass.

## Risks and impact
- **Security and privacy:** None
- **Data and migrations:** None
- **Compatibility and configuration:** None
- **Dependencies:** None
- **Documentation:** README updated

## Checklist
- [x] one
- [x] two
- [X] three
- [x] four
- [x] five
"""


def test_a_complete_description_passes() -> None:
    assert find_problems(COMPLETE) == []
    assert find_problems(COMPLETE.replace("\n", "\r\n")) == []


@pytest.mark.parametrize("body", [None, "", "fix stuff"])
def test_a_description_without_the_template_is_refused(body: str | None) -> None:
    assert len(find_problems(body)) == 7


def test_the_untouched_template_is_refused_on_every_point() -> None:
    assert len(find_problems(TEMPLATE)) == 11


def test_the_template_has_what_the_check_asks_for() -> None:
    filled = TEMPLATE
    for label in ("Security and privacy", "Data and migrations", "Dependencies"):
        filled = filled.replace(f"**{label}:**", f"**{label}:** None")
    problems = find_problems(filled)
    assert not any("is missing" in problem for problem in problems)
    assert TEMPLATE.count("- [ ]") == 5


@pytest.mark.parametrize("answer", ["N/A", "todo", "none.", "-", "<!-- later -->"])
def test_an_empty_answer_does_not_explain_anything(answer: str) -> None:
    body = COMPLETE.replace("People forget them on Tuesdays.", answer)
    assert find_problems(body) == ["The section **Why** needs to be filled in."]


def test_a_risk_left_blank_is_named() -> None:
    body = COMPLETE.replace("**Dependencies:** None", "**Dependencies:**")
    assert len(problems := find_problems(body)) == 1
    assert "**Dependencies**" in problems[0]


def test_a_box_not_ticked_or_removed_is_refused() -> None:
    assert find_problems(COMPLETE.replace("- [x] two", "- [ ] two"))
    assert find_problems(COMPLETE.replace("- [x] two\n", ""))


def test_a_heading_inside_a_code_block_is_not_a_section() -> None:
    body = COMPLETE.replace(
        "## Why\nPeople forget them on Tuesdays.\n", "```\n## Why\nin code\n```\n"
    )
    assert "The section **Why** is missing." in find_problems(body)


def test_the_report_never_repeats_the_description() -> None:
    body = "@someone <img src=x> ## Summary"
    assert "@someone" not in render_report(find_problems(body))


def _event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, body: str, draft: bool) -> Path:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": body, "draft": draft}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    return tmp_path / "report.md"


def test_main_passes_a_complete_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _event(tmp_path, monkeypatch, body=COMPLETE, draft=False)
    assert main(["--report", str(report)]) == 0
    assert "complete" in report.read_text(encoding="utf-8")


def test_main_refuses_an_incomplete_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _event(tmp_path, monkeypatch, body="short", draft=False)
    assert main(["--report", str(report)]) == 1
    assert "**Summary**" in report.read_text(encoding="utf-8")


def test_main_lets_a_draft_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _event(tmp_path, monkeypatch, body="", draft=True)
    assert main(["--report", str(report)]) == 0
    assert report.read_text(encoding="utf-8") == ""
