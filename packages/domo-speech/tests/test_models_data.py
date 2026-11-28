"""The candidates data file: what the service may run, never a choice, never a machine's measure."""

import json
import logging
from importlib import resources

import pytest
from domo_speech.config import SpeechSettings
from domo_speech.data import candidates, offered
from domo_speech.real import tell_restrictions

_USES = ("hear", "say", "voice_print")


def _raw() -> dict[str, object]:
    text = (resources.files("domo_speech.data") / "models.json").read_text(encoding="utf-8")
    return json.loads(text)


def test_every_use_has_a_floor_candidate_that_runs_without_a_graphics_card() -> None:
    for use in _USES:
        floor = [e for e in candidates() if e.does == use and e.floor and e.needs == "cpu"]
        # The floor of hearing and of voice prints runs inside the service, with no program to
        # install beside it.
        assert any(e.runs == "imported" for e in floor), use


def test_every_entry_names_its_licence_and_how_it_runs() -> None:
    for entry in candidates():
        assert entry.licence and entry.code_licence, entry.id
        assert entry.source.startswith("https://"), entry.id
        assert entry.code.startswith("https://"), entry.id
        assert entry.runs in ("imported", "separate_program"), entry.id
        assert entry.needs in ("cpu", "gpu"), entry.id
        assert entry.size_mb > 0 and entry.languages, entry.id


def test_nothing_gpl_is_imported() -> None:
    copyleft = ("GPL", "AGPL", "LGPL")
    for entry in candidates():
        if entry.licence.startswith(copyleft) or entry.code_licence.startswith(copyleft):
            assert entry.runs == "separate_program", entry.id
        if entry.runs == "separate_program":
            assert entry.program_args, entry.id


def test_every_file_is_pinned_and_has_a_digest() -> None:
    for entry in candidates():
        assert len(entry.revision) == 40, entry.id
        assert entry.files, entry.id
        for file in entry.files:
            assert len(file.sha256) == 64 and file.size > 0, (entry.id, file.path)
            assert ".." not in file.path.split("/") and not file.path.startswith("/")


def test_ids_are_unique() -> None:
    ids = [entry.id for entry in candidates()]
    assert len(ids) == len(set(ids))


def test_thresholds_are_ordered() -> None:
    prints = [entry for entry in candidates() if entry.does == "voice_print"]
    assert prints
    for entry in prints:
        assert entry.thresholds is not None, entry.id
        low, same, high = entry.thresholds
        assert 0 <= low < same < high <= 1, entry.id


def test_the_file_holds_no_choice_and_no_measure_of_a_machine() -> None:
    data = _raw()
    assert set(data) == {"about", "models"}
    for entry in data["models"]:  # type: ignore[union-attr]
        assert not {"chosen", "measures", "machine", "measured"} & set(entry), entry


def test_every_entry_links_its_licence_and_says_if_it_restricts_use() -> None:
    for entry in candidates():
        assert entry.licence_url.startswith("https://"), entry.id
        if entry.licence.startswith("LicenseRef-OpenRAIL"):
            assert entry.restricted, entry.id
    assert any(e.restricted for e in candidates())


def test_restricted_candidates_are_offered_unless_the_owner_says_no() -> None:
    restricted = {e.id for e in candidates() if e.restricted}

    assert restricted <= {e.id for e in offered(restricted=True)}
    assert not restricted & {e.id for e in offered(restricted=False)}
    assert {e.id for e in offered(restricted=False)} == {
        e.id for e in candidates() if not e.restricted
    }


def test_the_setting_reads_no_as_hiding_restricted_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMO_SPEECH_RESTRICTED_MODELS", "no")
    assert SpeechSettings().restricted_models is False
    monkeypatch.delenv("DOMO_SPEECH_RESTRICTED_MODELS")
    assert SpeechSettings().restricted_models is True


def test_whoever_starts_the_service_reads_that_a_voice_has_conditions_of_use(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    tell_restrictions(offered(restricted=True))
    told = caplog.text
    caplog.clear()
    tell_restrictions(offered(restricted=False))

    for entry in (e for e in candidates() if e.restricted):
        assert entry.id in told and entry.licence_url in told
    assert caplog.text == ""
