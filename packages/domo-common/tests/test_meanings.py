import json
from importlib import resources
from string import Formatter

import pytest
from domo_common import meanings
from domo_common.meanings import (
    DEFAULT_LANGUAGES,
    all_keys,
    default_text,
    group_of,
    has_defaults,
    has_meaning,
    holes,
    is_install_key,
    keys_of_group,
    max_length,
    meaning,
    meaning_hash,
    meaning_note,
    written_groups,
)

KEY_WITH_HOLE = "action.card.done.household.change_role.adult"


def test_every_key_has_a_group() -> None:
    for key in all_keys():
        group = group_of(key)
        assert group
        assert key in keys_of_group(group)


def test_group_rule() -> None:
    assert group_of("action.card.done.household.change_role.adult") == "action.card"
    assert group_of("nav.chat") == "nav"
    assert group_of("setup.owner.title") == "setup"
    assert group_of("action.describe.household.list") == "action.describe"
    assert group_of("chat.key.lost") == "chat"
    assert group_of("confirmation.confirm") == "confirmation"
    assert group_of("rights.example.generic") == "rights"


def test_setup_is_never_written() -> None:
    assert "setup" not in written_groups()
    assert "nav" in written_groups()
    assert list(written_groups()) == sorted(written_groups())


def test_holes_are_the_placeholders() -> None:
    assert holes(KEY_WITH_HOLE) == {"name"}
    assert holes("nav.chat") == frozenset()


def test_the_hash_changes_with_the_note(monkeypatch) -> None:
    before = meaning_hash("nav.chat")
    assert len(before) == 64
    monkeypatch.setattr(meanings, "_notes", lambda: {"nav.chat": "A different note."})
    assert meaning_hash("nav.chat") != before


def test_max_length_follows_the_meaning() -> None:
    for key in all_keys():
        assert max_length(key) == 2 * len(meaning(key)) + 40


def test_unknown_key_raises() -> None:
    try:
        meaning("no.such.key")
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_a_key_has_a_meaning_only_in_the_catalog() -> None:
    assert has_meaning("nav.chat")
    assert not has_meaning("no.such.key")


def test_install_keys() -> None:
    assert is_install_key("setup.owner.title")
    assert is_install_key("account.passkeys.ip_unavailable")
    assert not is_install_key("chat.send")


def test_notes_only_name_known_keys() -> None:
    path = resources.files("domo_common").joinpath("locales", "notes.json")
    notes = json.loads(path.read_text(encoding="utf-8"))
    assert notes
    keys = set(all_keys())
    for key, note in notes.items():
        assert key in keys
        assert note.strip() and "\n" not in note
        assert meaning_note(key) == note
    assert meaning_note("nav.chat") is None or "\n" not in (meaning_note("nav.chat") or "")


def test_unreachable_provider_does_not_claim_the_key_is_saved() -> None:
    assert "saved" not in meaning("chat.key.error.unreachable")


def _catalogue(name: str) -> dict[str, str]:
    path = resources.files("domo_common").joinpath("locales", name)
    return json.loads(path.read_text(encoding="utf-8"))


def _holes_of(text: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(text) if field}


def test_both_catalogues_have_the_same_keys_and_holes() -> None:
    # Every meaning has its default phrase in French too, with the same holes.
    en, fr = _catalogue("en.json"), _catalogue("fr.json")
    assert set(en) == set(fr)
    for key in en:
        assert _holes_of(en[key]) == _holes_of(fr[key]), key


def test_default_text_reads_a_close_language() -> None:
    assert DEFAULT_LANGUAGES == ("en", "fr")
    assert default_text("nav.chat", "en") == meaning("nav.chat")
    assert default_text("nav.chat", "fr") == _catalogue("fr.json")["nav.chat"]
    assert default_text("nav.chat", "fr") != default_text("nav.chat", "en")
    assert default_text("nav.chat", "fr-CA") == default_text("nav.chat", "fr")
    assert default_text("nav.chat", "FR") == default_text("nav.chat", "fr")
    assert default_text("nav.chat", "de") == default_text("nav.chat", "en")
    assert default_text("nav.chat", "und") == default_text("nav.chat", "en")
    assert default_text("nav.chat", "fr_CA") == default_text("nav.chat", "fr")
    with pytest.raises(KeyError):
        default_text("no.such.key", "fr")


def test_a_language_has_defaults_when_it_reads_a_catalogue_of_its_own() -> None:
    assert has_defaults("en") and has_defaults("fr") and has_defaults("fr-CA")
    assert has_defaults("fr_CA")
    assert not has_defaults("de") and not has_defaults("und")
