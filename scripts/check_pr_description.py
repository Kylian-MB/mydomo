"""Check that a pull request's description is complete.

Runs in CI on the event GitHub gives (``GITHUB_EVENT_PATH``). It uses the standard library only
and never executes anything from the pull request. The report holds fixed texts only: what
someone wrote in the description is never repeated in the comment posted back.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

WRITTEN_SECTIONS = (
    "Summary",
    "Why",
    "What changes",
    "Behaviour before and after",
    "How it was tested",
)
RISKS_SECTION = "Risks and impact"
RISK_LINES = (
    "Security and privacy",
    "Data and migrations",
    "Compatibility and configuration",
    "Dependencies",
    "Documentation",
)
CHECKLIST_SECTION = "Checklist"
CHECKLIST_ITEMS = 5

# Answers that say nothing in a section meant to explain something.
EMPTY_ANSWERS = frozenset(
    {"", "n a", "na", "none", "nothing", "no", "todo", "tbd", "wip", "same", "see diff", "see code"}
)

_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
_TICKED = re.compile(r"^[ \t]*[-*+][ \t]+\[[xX]\][ \t]+\S", re.MULTILINE)


def split_sections(body: str) -> dict[str, str]:
    """Return each ``##`` section by lowercase title; a heading inside a code block is not one."""
    text = _COMMENT.sub("", body.replace("\r\n", "\n"))
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            current = sections.setdefault(heading.group(1).casefold(), [])
        elif current is not None:
            current.append(line)
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def _says_something(text: str) -> bool:
    return " ".join(re.sub(r"[\W_]+", " ", text).casefold().split()) not in EMPTY_ANSWERS


def _risk_answered(section: str, label: str) -> bool:
    line = re.search(
        rf"^[ \t]*[-*+][ \t]+\*\*{re.escape(label)}:?\*\*:?[ \t]*(.*)$",
        section,
        re.MULTILINE | re.IGNORECASE,
    )
    return line is not None and re.search(r"\w", line.group(1)) is not None


def find_problems(body: str | None) -> list[str]:
    """Return what is missing from the description, in the order of the template."""
    sections = split_sections(body or "")
    problems: list[str] = []
    for name in WRITTEN_SECTIONS:
        content = sections.get(name.casefold())
        if content is None:
            problems.append(f"The section **{name}** is missing.")
        elif not _says_something(content):
            problems.append(f"The section **{name}** needs to be filled in.")

    risks = sections.get(RISKS_SECTION.casefold())
    if risks is None:
        problems.append(f"The section **{RISKS_SECTION}** is missing.")
    else:
        problems.extend(
            f"In **{RISKS_SECTION}**, answer the line **{label}** (“None” is fine if true)."
            for label in RISK_LINES
            if not _risk_answered(risks, label)
        )

    checklist = sections.get(CHECKLIST_SECTION.casefold())
    if checklist is None:
        problems.append(f"The section **{CHECKLIST_SECTION}** is missing.")
    elif len(_TICKED.findall(checklist)) < CHECKLIST_ITEMS:
        problems.append(f"Tick all {CHECKLIST_ITEMS} boxes of the **{CHECKLIST_SECTION}**.")
    return problems


def render_report(problems: list[str]) -> str:
    """Return the comment posted on the pull request."""
    if not problems:
        return "The description is complete. Thank you!\n"
    items = "\n".join(f"- {problem}" for problem in problems)
    return (
        "Thank you for this pull request! Its description is not complete yet:\n\n"
        f"{items}\n\n"
        "Edit the description (no new commit needed) and this check runs again by itself. "
        "If the sections are gone, copy them from "
        "[the template](https://github.com/Kylian-MB/mydomo/blob/main/.github/pull_request_template.md).\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="file to write the comment to")
    args = parser.parse_args(argv)
    report_path = cast(Path, args.report)

    event = cast(
        dict[str, Any], json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text("utf-8"))
    )
    pull_request = cast(dict[str, Any], event["pull_request"])
    if pull_request.get("draft"):
        report_path.write_text("", encoding="utf-8")
        print("Draft: the description is checked once it is marked ready.")
        return 0

    problems = find_problems(cast(str | None, pull_request.get("body")))
    report_path.write_text(render_report(problems), encoding="utf-8")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
