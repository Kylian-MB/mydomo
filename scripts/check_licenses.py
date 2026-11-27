"""Fail when an installed dependency has a license outside the allowed set.

Reads the license of every package in the current environment from
``pip-licenses --format=json``. Dual-licensed packages ("A OR B") pass when one
alternative is allowed; packages that combine licenses ("A AND B", or several
classifiers) need every part to be allowed. A package that is not covered by
`ALLOWED` can only get through with an entry in `EXCEPTIONS`, which names the
package and the exact license it is accepted under.

Run it in an environment without the dev tools, which are not distributed:

    uv run --isolated --no-default-groups --group licenses python scripts/check_licenses.py
"""

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

# Scope: runtime dependencies of the shipped images only (run without the dev group, see CI).
ALLOWED: frozenset[str] = frozenset(
    {
        "MIT",
        "MIT-0",
        "BSD",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "PSF-2.0",
        "PostgreSQL",
        "0BSD",
        "MPL-2.0",
    }
)

# package name -> the exact license it is accepted under (with a reason in a comment).
EXCEPTIONS: dict[str, str] = {
    # Brought by fastembed (the core's local model of the meanings of memories), all permissive:
    # numpy's own code is BSD-3-Clause; the parts it bundles add 0BSD, MIT, Zlib and CC0-1.0.
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
    # MIT-CMU: the historical PIL license, a permissive one of the MIT family.
    "pillow": "MIT-CMU",
    # BSD-3-Clause under another spelling of its name.
    "protobuf": "3-Clause BSD License",
    # The package states no license in its metadata; the LICENSE file it ships is MIT.
    "py_rust_stemmers": "UNKNOWN",
}

# The extra `gpu` of domo-speech: the CUDA libraries of the card's maker, proprietary software.
# Installed only in the GPU image a person builds for his own card, never redistributed by the
# project; named here, with the exact license each states, so that the check is not blind to them
# (run it with that extra installed to see them). cuDNN states none in its metadata.
EXCEPTIONS.update(
    {
        "nvidia-cublas-cu12": "LicenseRef-NVIDIA-Proprietary",
        "nvidia-cudnn-cu12": "UNKNOWN",
    }
)

# The workspace members are the project itself, not third-party dependencies.
OWN_PACKAGES: frozenset[str] = frozenset(
    {"domo-common", "domo-gatekeeper", "domo-core", "domo-talk", "domo-sandbox"}
)

# Lower-cased spellings seen in package metadata -> canonical identifier.
_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "mit-0": "MIT-0",
    "mit no attribution": "MIT-0",
    "bsd": "BSD",
    "bsd license": "BSD",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "simplified bsd license": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "postgresql": "PostgreSQL",
    "postgresql license": "PostgreSQL",
    "0bsd": "0BSD",
    "bsd zero clause license": "0BSD",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
}

_OR = re.compile(r"\s+OR\s+", re.IGNORECASE)
_AND = re.compile(r"\s+AND\s+|;", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    package: str
    license: str


def normalize_license(raw: str) -> str:
    """Map a license name as found in package metadata to its canonical identifier.

    Unrecognised values are returned stripped but otherwise unchanged.
    """
    cleaned = raw.strip()
    return _ALIASES.get(cleaned.casefold(), cleaned)


def _unwrap(part: str) -> str:
    """Drop one pair of parentheses wrapping the whole part, as in "(MIT OR ISC)"."""
    part = part.strip()
    if part.startswith("(") and part.endswith(")"):
        return part[1:-1].strip()
    return part


def _is_allowed(expression: str, allowed: frozenset[str]) -> bool:
    for part in _AND.split(expression):
        alternatives = _OR.split(_unwrap(part))
        if not any(normalize_license(alt) in allowed for alt in alternatives):
            return False
    return True


def check(
    licenses: Mapping[str, str],
    allowed: frozenset[str],
    exceptions: Mapping[str, str],
) -> list[Violation]:
    """Return the packages whose license is neither allowed nor excepted, sorted by name."""
    violations: list[Violation] = []
    for package in sorted(licenses):
        license_ = licenses[package]
        excepted = exceptions.get(package)
        if excepted is not None and normalize_license(excepted) == normalize_license(license_):
            continue
        if not _is_allowed(license_, allowed):
            violations.append(Violation(package, license_))
    return violations


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_report(output: str) -> dict[str, str]:
    """Turn the JSON printed by `pip-licenses --format=json` into {package: license}.

    The project's own workspace packages are left out.
    """
    try:
        payload: object = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("unexpected pip-licenses output: not valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("unexpected pip-licenses output: expected a JSON list")
    licenses: dict[str, str] = {}
    for row in cast("list[object]", payload):
        entry = cast("dict[str, object]", row) if isinstance(row, dict) else {}
        name, license_ = entry.get("Name"), entry.get("License")
        if not isinstance(name, str) or not isinstance(license_, str):
            raise ValueError("unexpected pip-licenses output: entry without Name and License")
        if _canonical_name(name) not in OWN_PACKAGES:
            licenses[name] = license_
    return licenses


def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    violations = check(parse_report(completed.stdout), ALLOWED, EXCEPTIONS)
    for violation in violations:
        print(
            f"license not allowed: {violation.package} ({violation.license})",
            file=sys.stderr,
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
