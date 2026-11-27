"""The spoken phrase: kept encrypted in the vault's file, compared with tolerance."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from domo_gatekeeper.errors import PhraseHeldBack, VaultDecryptError
from domo_gatekeeper.phrases import (
    ALMOST_AT,
    IT_IS_AT,
    MAX_COMPARISONS,
    MAX_NEAR_MISSES,
    PhraseStore,
    compare,
    normalised,
    ratio,
)
from domo_gatekeeper.vault import Vault

KEY = b"\x01" * 32
REF = "phrase.00000000-0000-0000-0000-000000000001"
PHRASE = "le chat bleu dort sur le toit"
NEAR = "le chien gris dort sur un toit"
ORDINARY = "non, envoie-moi plutôt un message"


@pytest.fixture
def phrases(vault_db_path: Path, clock) -> PhraseStore:
    return PhraseStore(vault_db_path, KEY, clock=clock)


def agree(store: PhraseStore, ref: str, heard: str) -> None:
    assert store.enrol(ref, heard).step == "first_kept"
    assert store.enrol(ref, heard).step == "agreed"


# --- The comparison -----------------------------------------------------------------------------


def test_small_hearing_slips_are_the_phrase():
    assert compare(PHRASE, "le chat bleu dors sur le toit") == "it_is"


def test_a_near_phrase_is_almost():
    assert ALMOST_AT <= ratio(normalised(PHRASE), normalised(NEAR)) < IT_IS_AT
    assert compare(PHRASE, NEAR) == "almost"


def test_an_ordinary_sentence_is_nothing_like():
    assert compare(PHRASE, ORDINARY) == "nothing_like"


def test_case_accents_kept_punctuation_dropped():
    assert normalised("  Le CHÂT, bleu !\n— dort…  ") == "le chât bleu dort"
    assert normalised("ﬁn２") == "fin2"  # compatibility forms are folded (NFKC)
    assert compare("Le chât bleu dort sur le toit", "le chât, bleu dort sur le toit !") == "it_is"
    # An accent is a letter of its own: dropping it is a slip, not nothing.
    assert normalised("chât") != normalised("chat")


def test_the_thresholds_are_the_documented_ones():
    assert (IT_IS_AT, ALMOST_AT) == (0.85, 0.60)


# --- Agreeing on a phrase -----------------------------------------------------------------------


def test_two_sayings_are_needed_and_must_agree(phrases):
    assert phrases.enrol(REF, PHRASE).step == "first_kept"
    assert not phrases.has(REF)
    assert phrases.enrol(REF, "le chat bleu dors sur le toit").step == "agreed"
    assert phrases.has(REF)
    assert phrases.compare(REF, PHRASE) == "it_is"


def test_a_second_saying_that_differs_drops_the_first(phrases):
    phrases.enrol(REF, PHRASE)
    assert phrases.enrol(REF, ORDINARY).step == "not_the_same"
    assert not phrases.has(REF)
    # The first one is gone: the next saying starts over.
    assert phrases.enrol(REF, PHRASE).step == "first_kept"


def test_the_first_saying_waits_five_minutes_only(phrases, clock):
    phrases.enrol(REF, PHRASE)
    clock.advance(timedelta(minutes=5, seconds=1))
    assert phrases.enrol(REF, PHRASE).step == "first_kept"
    clock.advance(timedelta(minutes=4))
    assert phrases.enrol(REF, PHRASE).step == "agreed"


@pytest.mark.parametrize(
    "heard", ["chat bleu", "le chat dor", "longuesphrases", "a b c d e", "!!! ... ???"]
)
def test_a_phrase_too_short_is_refused(phrases, heard):
    assert phrases.enrol(REF, heard).step == "too_short"
    assert phrases.enrol(REF, heard).step == "too_short"
    assert not phrases.has(REF)


def test_a_too_short_saying_leaves_the_first_one_waiting(phrases):
    phrases.enrol(REF, PHRASE)
    assert phrases.enrol(REF, "le chat").step == "too_short"
    assert phrases.enrol(REF, PHRASE).step == "agreed"


def test_a_changed_phrase_replaces_the_old_one(phrases):
    agree(phrases, REF, PHRASE)
    other = "la pluie tombe sur la mer grise"
    # While the new one is only said once, the old one still holds.
    assert phrases.enrol(REF, other).step == "first_kept"
    assert phrases.compare(REF, PHRASE) == "it_is"
    assert phrases.enrol(REF, other).step == "agreed"
    assert phrases.compare(REF, other) == "it_is"
    assert phrases.compare(REF, PHRASE) == "nothing_like"


def test_each_person_has_a_phrase_of_their_own(phrases):
    other_ref = "phrase.00000000-0000-0000-0000-000000000002"
    agree(phrases, REF, PHRASE)
    assert not phrases.has(other_ref)
    with pytest.raises(KeyError):
        phrases.compare(other_ref, PHRASE)


def test_a_phrase_is_removed(phrases):
    agree(phrases, REF, PHRASE)
    assert phrases.remove(REF) is True
    assert not phrases.has(REF)
    assert phrases.remove(REF) is False
    with pytest.raises(KeyError):
        phrases.compare(REF, PHRASE)


def test_a_first_saying_alone_is_no_phrase(phrases):
    phrases.enrol(REF, PHRASE)
    with pytest.raises(KeyError):
        phrases.compare(REF, PHRASE)
    assert phrases.remove(REF) is False
    # Removing forgot the waiting saying too.
    assert phrases.enrol(REF, PHRASE).step == "first_kept"


def test_a_ref_must_be_a_ref(phrases):
    with pytest.raises(ValueError, match="ref"):
        phrases.enrol("Not A Ref", PHRASE)


# --- Tries ---------------------------------------------------------------------------------------


def test_five_near_misses_hold_back(phrases, clock):
    agree(phrases, REF, PHRASE)
    for _ in range(5):
        assert phrases.compare(REF, NEAR) == "almost"
    with pytest.raises(PhraseHeldBack) as held:
        phrases.compare(REF, PHRASE)  # even the right phrase waits
    assert 0 < held.value.wait <= 15 * 60
    clock.advance(timedelta(minutes=15, seconds=1))
    assert phrases.compare(REF, PHRASE) == "it_is"


def test_the_right_phrase_clears_the_near_misses(phrases):
    agree(phrases, REF, PHRASE)
    for _ in range(4):
        phrases.compare(REF, NEAR)
    assert phrases.compare(REF, PHRASE) == "it_is"
    for _ in range(4):
        assert phrases.compare(REF, NEAR) == "almost"


def test_nothing_like_is_not_an_attempt(phrases):
    agree(phrases, REF, PHRASE)
    for _ in range(20):
        assert phrases.compare(REF, ORDINARY) == "nothing_like"
    assert phrases.compare(REF, PHRASE) == "it_is"


def test_a_held_back_ref_answers_nothing_else(phrases):
    # Not even "nothing like": a held-back ref must not answer "is it near?" for free.
    agree(phrases, REF, PHRASE)
    for _ in range(MAX_NEAR_MISSES):
        phrases.compare(REF, NEAR)
    for heard in (ORDINARY, NEAR, PHRASE):
        with pytest.raises(PhraseHeldBack):
            phrases.compare(REF, heard)


def test_every_comparison_counts_toward_a_larger_cap(phrases, clock):
    assert MAX_COMPARISONS == 50
    agree(phrases, REF, PHRASE)
    for _ in range(MAX_COMPARISONS):
        assert phrases.compare(REF, ORDINARY) == "nothing_like"
    with pytest.raises(PhraseHeldBack) as held:
        phrases.compare(REF, PHRASE)
    assert 0 < held.value.wait <= 15 * 60
    clock.advance(timedelta(minutes=15, seconds=1))
    assert phrases.compare(REF, PHRASE) == "it_is"


def test_the_right_phrase_does_not_clear_the_larger_cap(phrases):
    agree(phrases, REF, PHRASE)
    for _ in range(MAX_COMPARISONS - 1):
        phrases.compare(REF, ORDINARY)
    assert phrases.compare(REF, PHRASE) == "it_is"
    with pytest.raises(PhraseHeldBack):
        phrases.compare(REF, PHRASE)


def test_a_ref_without_a_phrase_is_counted_the_same(phrases):
    # Being held back must not tell whether a phrase exists.
    for _ in range(MAX_COMPARISONS):
        with pytest.raises(KeyError):
            phrases.compare(REF, ORDINARY)
    with pytest.raises(PhraseHeldBack):
        phrases.compare(REF, ORDINARY)


def test_near_misses_are_counted_per_person(phrases):
    other_ref = "phrase.00000000-0000-0000-0000-000000000002"
    agree(phrases, REF, PHRASE)
    agree(phrases, other_ref, PHRASE)
    for _ in range(5):
        phrases.compare(REF, NEAR)
    assert phrases.compare(other_ref, PHRASE) == "it_is"


# --- What the file holds -------------------------------------------------------------------------


def test_the_file_holds_no_readable_phrase(phrases, vault_db_path):
    agree(phrases, REF, PHRASE)
    phrases.enrol(REF, "la pluie tombe sur la mer grise")  # a waiting saying too
    data = b"".join(path.read_bytes() for path in vault_db_path.parent.iterdir())
    for word in ("chat", "bleu", "toit", "pluie", "grise"):
        assert word.encode() not in data


def test_phrases_are_not_among_the_values_scrubbed_from_answers(phrases, vault_db_path):
    agree(phrases, REF, PHRASE)
    assert Vault(vault_db_path, KEY).values() == []


def test_the_phrase_shares_the_vault_file_without_harming_it(phrases, vault_db_path):
    vault = Vault(vault_db_path, KEY)
    agree(phrases, REF, PHRASE)
    assert vault.list_metadata() == []
    assert Vault.count_secrets(vault_db_path) == 0


def test_a_phrase_cannot_be_moved_to_another_person(phrases, vault_db_path):
    other_ref = "phrase.00000000-0000-0000-0000-000000000002"
    agree(phrases, REF, PHRASE)
    agree(phrases, other_ref, "la pluie tombe sur la mer grise")
    with sqlite3.connect(vault_db_path) as conn:
        row = conn.execute(
            "SELECT nonce, ciphertext FROM spoken_phrases WHERE ref = ?", (REF,)
        ).fetchone()
        conn.execute(
            "UPDATE spoken_phrases SET nonce = ?, ciphertext = ? WHERE ref = ?", (*row, other_ref)
        )
    with pytest.raises(VaultDecryptError) as failed:
        phrases.compare(other_ref, PHRASE)
    assert "chat" not in str(failed.value)


def test_a_waiting_saying_cannot_be_made_the_phrase_in_the_file(phrases, vault_db_path):
    agree(phrases, REF, PHRASE)
    phrases.enrol(REF, "la pluie tombe sur la mer grise")
    with sqlite3.connect(vault_db_path) as conn:
        conn.execute(
            "UPDATE spoken_phrases SET nonce = pending_nonce, ciphertext = pending_ciphertext"
            " WHERE ref = ?",
            (REF,),
        )
    with pytest.raises(VaultDecryptError):
        phrases.compare(REF, "la pluie tombe sur la mer grise")


def test_a_wrong_key_cannot_read_a_phrase(phrases, vault_db_path, clock):
    agree(phrases, REF, PHRASE)
    with pytest.raises(VaultDecryptError):
        PhraseStore(vault_db_path, b"\x02" * 32, clock=clock).compare(REF, PHRASE)
