import json

import pytest

from scripts.check_licenses import ALLOWED, Violation, check, normalize_license, parse_report


def test_normalize_license_variants() -> None:
    assert normalize_license("Apache Software License") == "Apache-2.0"
    assert normalize_license("MIT License") == "MIT"
    assert normalize_license("BSD License") == "BSD"


def test_normalize_license_keeps_unknown_values() -> None:
    assert normalize_license("  GPL-3.0 ") == "GPL-3.0"


def test_gpl_is_rejected() -> None:
    assert check({"somepkg": "GPL-3.0"}, ALLOWED, {}) == [Violation("somepkg", "GPL-3.0")]


def test_exception_allows_only_the_named_package() -> None:
    assert check({"urwid": "LGPL-2.1"}, ALLOWED, {"urwid": "LGPL-2.1"}) == []
    assert check({"other": "LGPL-2.1"}, ALLOWED, {"urwid": "LGPL-2.1"}) != []


def test_exception_does_not_cover_a_different_license() -> None:
    assert check({"urwid": "GPL-3.0"}, ALLOWED, {"urwid": "LGPL-2.1"}) != []


def test_allowed_licenses_pass_after_normalization() -> None:
    licenses = {
        "a": "MIT License",
        "b": "Apache Software License",
        "c": "Mozilla Public License 2.0 (MPL 2.0)",
        "d": "ISC License (ISCL)",
        "e": "Python Software Foundation License",
    }
    assert check(licenses, ALLOWED, {}) == []


def test_mit_0_is_allowed() -> None:
    # MIT without the attribution clause (cffi, a dependency of cryptography).
    assert check({"cffi": "MIT-0"}, ALLOWED, {}) == []
    assert check({"cffi": "mit-0"}, ALLOWED, {}) == []
    assert check({"cffi": "MIT No Attribution"}, ALLOWED, {}) == []
    assert check({"p": "MIT-0 OR GPL-3.0"}, ALLOWED, {}) == []
    assert check({"p": "MIT-0 AND GPL-3.0"}, ALLOWED, {}) != []


def test_unknown_license_is_rejected() -> None:
    assert check({"mystery": "UNKNOWN"}, ALLOWED, {}) == [Violation("mystery", "UNKNOWN")]


def test_or_expression_needs_one_allowed_alternative() -> None:
    assert check({"p": "Apache-2.0 OR BSD-2-Clause"}, ALLOWED, {}) == []
    assert check({"p": "MIT OR GPL-3.0"}, ALLOWED, {}) == []
    assert check({"p": "GPL-2.0 OR GPL-3.0"}, ALLOWED, {}) != []


def test_and_expression_and_classifier_lists_need_every_part_allowed() -> None:
    assert check({"p": "MIT AND BSD-3-Clause"}, ALLOWED, {}) == []
    assert check({"p": "MIT AND GPL-3.0"}, ALLOWED, {}) != []
    assert check({"p": "MIT License; BSD License"}, ALLOWED, {}) == []
    assert check({"p": "MIT License; GNU General Public License v3 (GPLv3)"}, ALLOWED, {}) != []


def test_violations_are_reported_sorted_by_package() -> None:
    licenses = {"zeta": "GPL-3.0", "alpha": "AGPL-3.0"}
    assert [v.package for v in check(licenses, ALLOWED, {})] == ["alpha", "zeta"]


def test_parse_report_maps_names_to_licenses_and_skips_own_packages() -> None:
    report = json.dumps(
        [
            {"Name": "requests", "Version": "1.0", "License": "Apache Software License"},
            {"Name": "domo-core", "Version": "0.1.0", "License": "AGPL-3.0-only"},
            {"Name": "domo_common", "Version": "0.1.0", "License": "AGPL-3.0-only"},
        ]
    )
    assert parse_report(report) == {"requests": "Apache Software License"}


def test_parse_report_rejects_malformed_output() -> None:
    with pytest.raises(ValueError, match="pip-licenses"):
        parse_report('{"not": "a list"}')


def test_parenthesized_expression_is_unwrapped() -> None:
    assert check({"p": "(MIT OR GPL-3.0)"}, ALLOWED, {}) == []
    assert check({"p": "(GPL-3.0)"}, ALLOWED, {}) != []
