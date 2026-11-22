import json
from collections.abc import Mapping
from importlib import resources

import pytest
from domo_common import i18n
from domo_common.i18n import Language, negotiate_language, t


def test_negotiate_language():
    assert negotiate_language("fr-FR,fr;q=0.9,en;q=0.8") == "fr"
    assert negotiate_language(None) == "en"
    assert negotiate_language("de-DE") == "en"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", "en"),
        ("   ", "en"),
        ("*", "en"),
        ("en", "en"),
        ("EN-gb", "en"),
        ("FR", "fr"),
        ("fr-CA", "fr"),
        ("en;q=0.5, fr;q=0.9", "fr"),
        ("fr;q=0.5, en;q=0.9", "en"),
        ("de, fr;q=0.3", "fr"),
        ("fr;q=0", "en"),  # q=0 means "not acceptable"
        ("fr;q=0, de", "en"),
        ("en, fr", "en"),  # same weight: the first one listed wins
        ("fr, en", "fr"),
        ("fr ; q=0.8 , en ; q=0.2", "fr"),
        ("fr;q=abc", "en"),  # malformed weight: the entry is ignored
        ("fr;q=2", "en"),  # out of range: ignored
        ("french", "en"),  # not a language tag
        (",,;;,", "en"),
    ],
)
def test_negotiate_language_edge_cases(header: str, expected: Language):
    assert negotiate_language(header) == expected


def test_t_returns_the_text_of_the_requested_language():
    assert t("vault.form.submit", "en") != t("vault.form.submit", "fr")


def test_t_substitutes_parameters(monkeypatch: pytest.MonkeyPatch):
    catalogs = {"en": {"k": "Hello {name}, {count} items"}, "fr": {"k": "Bonjour {name}"}}
    monkeypatch.setattr(i18n, "_catalog", lambda lang: catalogs[lang])
    assert t("k", "en", name="Ada", count="3") == "Hello Ada, 3 items"
    assert t("k", "fr", name="Ada") == "Bonjour Ada"


def test_t_takes_parameters_named_key_and_lang(monkeypatch: pytest.MonkeyPatch):
    # The parameters may come from the arguments of a tool call, which can use any name.
    monkeypatch.setattr(i18n, "_catalog", lambda lang: {"k": "{key} in {lang}"})
    assert t("k", "en", key="x", lang="y") == "x in y"


def test_t_does_not_interpret_braces_in_parameter_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(i18n, "_catalog", lambda lang: {"k": "Value: {v}"})
    assert t("k", "en", v="{0} {name}") == "Value: {0} {name}"


def test_t_falls_back_to_english_when_the_key_is_missing_in_french(
    monkeypatch: pytest.MonkeyPatch,
):
    catalogs = {"en": {"only.en": "English text"}, "fr": {}}
    monkeypatch.setattr(i18n, "_catalog", lambda lang: catalogs[lang])
    assert t("only.en", "fr") == "English text"


def test_t_raises_key_error_when_the_key_is_missing_everywhere(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(i18n, "_catalog", lambda lang: {})
    with pytest.raises(KeyError):
        t("missing.key", "fr")
    with pytest.raises(KeyError):
        t("missing.key", "en")


def _read_catalog(name: str) -> Mapping[str, str]:
    text = resources.files("domo_common").joinpath("locales", name).read_text(encoding="utf-8")
    catalog = json.loads(text)
    assert isinstance(catalog, dict)
    return catalog


def test_catalogs_are_shipped_as_package_data():
    for name in ("en.json", "fr.json"):
        catalog = _read_catalog(name)
        assert catalog
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in catalog.items())
