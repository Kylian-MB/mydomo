# pyright: basic
"""The analyser: Bandit (Apache-2.0) run as a library on a code held in memory.

Nothing is written to disk and the code is only parsed, never executed. A ``# nosec`` comment
is ignored: the code being scanned must not be able to silence its own alerts.
"""

import ast
import io

from bandit.core.config import BanditConfig
from bandit.core.manager import BanditManager
from domo_common.sandbox_api import ScanFinding, ScanResult

_NAME = "<tool>"


def _alert(line: int, text: str) -> ScanResult:
    return ScanResult(findings=[ScanFinding(test_id="B000", severity="HIGH", line=line, text=text)])


def scan(code: str) -> ScanResult:
    """The alerts Bandit raises on ``code``.

    It fails closed: a code that does not parse, or that Bandit did not analyse for any reason,
    is itself a HIGH alert, never an empty list (which would read as "clean").
    """
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        line = exc.lineno if isinstance(exc, SyntaxError) and exc.lineno else 1
        return _alert(line, f"The code does not parse: {type(exc).__name__}.")
    try:
        manager = BanditManager(BanditConfig(), "file", ignore_nosec=True)
        manager._parse_file(_NAME, io.BytesIO(code.encode("utf-8")), [_NAME])
        if manager.skipped:
            return _alert(1, "The analyser could not read the code.")
        findings = [
            ScanFinding(
                test_id=str(issue.test_id),
                severity=str(issue.severity),
                line=int(issue.lineno),
                text=str(issue.text),
            )
            for issue in manager.get_issue_list()
        ]
    except (Exception, SystemExit):  # Bandit exits the interpreter on some of its failures
        return _alert(1, "The analyser could not read the code.")
    return ScanResult(findings=sorted(findings, key=lambda f: (f.line, f.test_id)))
