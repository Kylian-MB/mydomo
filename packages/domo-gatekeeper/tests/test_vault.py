import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from domo_common.gatekeeper_api import Binding, InjectionLocation
from domo_gatekeeper.errors import (
    BindingMismatchError,
    InvalidSecretValueError,
    VaultDecryptError,
)
from domo_gatekeeper.vault import Vault, _associated_data, _stored_associated_data
from pydantic import TypeAdapter

B = Binding(host="api.example.com", location=InjectionLocation(kind="header", name="x-api-key"))
EVIL = Binding(host="evil.example.net", location=InjectionLocation(kind="header", name="x-api-key"))
B_AUTH = Binding(
    host="api.example.com",
    location=InjectionLocation(kind="header", name="authorization", template="Bearer {value}"),
)


def raw_rows(db: Path) -> list[tuple[str, bytes, bytes]]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT ref, nonce, ciphertext FROM secrets ORDER BY ref").fetchall()


def test_put_then_reveal_roundtrip(vault):
    vault.put("provider:a:api_key", "s3cr3t-value", purpose="Model API key", bindings=[B])
    assert vault.reveal("provider:a:api_key") == "s3cr3t-value"


def test_ciphertext_does_not_contain_value(vault, vault_db_path):
    vault.put("provider:a:api_key", "s3cr3t-value", purpose="p", bindings=[B])
    assert b"s3cr3t-value" not in vault_db_path.read_bytes()


def test_rebinding_existing_ref_is_refused(vault):
    vault.put("provider:a:api_key", "v1", purpose="p", bindings=[B])
    with pytest.raises(BindingMismatchError):
        vault.put("provider:a:api_key", "v2", purpose="p", bindings=[EVIL])
    assert vault.reveal("provider:a:api_key") == "v1"


def test_same_bindings_rotate_value(vault):
    vault.put("provider:a:api_key", "v1", purpose="p", bindings=[B])
    meta = vault.put("provider:a:api_key", "v2", purpose="p", bindings=[B])
    assert vault.reveal("provider:a:api_key") == "v2" and meta.rotated_at is not None


def test_wrong_key_cannot_decrypt(tmp_path):
    db = tmp_path / "vault.db"
    Vault(db, b"\x01" * 32).put("r", "v", purpose="p", bindings=[B])
    with pytest.raises(VaultDecryptError):
        Vault(db, b"\x02" * 32).reveal("r")


def test_decrypt_error_does_not_leak_the_value(tmp_path):
    db = tmp_path / "vault.db"
    Vault(db, b"\x01" * 32).put("r", "s3cr3t-value", purpose="p", bindings=[B])
    with pytest.raises(VaultDecryptError) as excinfo:
        Vault(db, b"\x02" * 32).reveal("r")
    assert "s3cr3t-value" not in str(excinfo.value)
    assert "s3cr3t-value" not in repr(excinfo.value)


def test_reveal_unknown_ref_raises_key_error(vault):
    with pytest.raises(KeyError):
        vault.reveal("nope")


def test_data_survives_reopening_the_vault(vault, vault_db_path):
    vault.put("r", "v", purpose="p", bindings=[B])
    assert Vault(vault_db_path, b"\x01" * 32).reveal("r") == "v"


def test_adding_or_removing_a_destination_counts_as_a_rebinding(vault):
    vault.put("r", "v1", purpose="p", bindings=[B])
    with pytest.raises(BindingMismatchError):
        vault.put("r", "v2", purpose="p", bindings=[B, EVIL])
    vault.put("r2", "v1", purpose="p", bindings=[B, EVIL])
    with pytest.raises(BindingMismatchError):
        vault.put("r2", "v2", purpose="p", bindings=[B])
    assert vault.reveal("r") == "v1" and vault.reveal("r2") == "v1"


def test_a_different_injection_location_counts_as_a_rebinding(vault):
    vault.put("r", "v1", purpose="p", bindings=[B])
    with pytest.raises(BindingMismatchError):
        vault.put("r", "v2", purpose="p", bindings=[B_AUTH])


def test_header_name_case_does_not_make_a_different_binding(vault):
    upper = Binding(
        host="api.example.com",
        location=InjectionLocation(kind="header", name="X-API-Key"),
    )
    vault.put("r", "v1", purpose="p", bindings=[upper])
    meta = vault.put("r", "v2", purpose="p", bindings=[B])  # B uses "x-api-key"
    assert meta.rotated_at is not None
    assert vault.reveal("r") == "v2"


def test_binding_order_and_duplicates_do_not_matter(vault):
    vault.put("r", "v1", purpose="p", bindings=[B, EVIL])
    meta = vault.put("r", "v2", purpose="p", bindings=[EVIL, B, B])
    assert vault.reveal("r") == "v2"
    assert meta.rotated_at is not None


def test_binding_mismatch_error_carries_no_value(vault):
    vault.put("r", "old-secret", purpose="p", bindings=[B])
    with pytest.raises(BindingMismatchError) as excinfo:
        vault.put("r", "new-secret", purpose="p", bindings=[EVIL])
    assert "old-secret" not in str(excinfo.value)
    assert "new-secret" not in str(excinfo.value)


def test_metadata_of_a_new_secret(vault):
    before = datetime.now(UTC)
    meta = vault.put("r", "s3cr3t-value", purpose="Model API key", bindings=[B])
    assert meta == vault.metadata("r")
    assert meta.ref == "r"
    assert meta.purpose == "Model API key"
    assert meta.bindings == [B]
    assert meta.created_at.utcoffset() is not None and meta.created_at >= before
    assert meta.rotated_at is None
    assert meta.last_used_at is None


def test_metadata_never_contains_the_value(vault):
    meta = vault.put("r", "s3cr3t-value", purpose="p", bindings=[B])
    assert "s3cr3t-value" not in repr(meta)
    assert "s3cr3t-value" not in meta.model_dump_json()
    assert "s3cr3t-value" not in repr(vault.list_metadata())


def test_metadata_of_unknown_ref_is_none(vault):
    assert vault.metadata("nope") is None


def test_rotation_keeps_creation_date_and_bindings(vault):
    first = vault.put("r", "v1", purpose="p", bindings=[B, EVIL])
    rotated = vault.put("r", "v2", purpose="p", bindings=[EVIL, B])
    assert rotated.created_at == first.created_at
    assert rotated.rotated_at is not None and rotated.rotated_at >= first.created_at
    assert rotated.rotated_at.utcoffset() is not None
    # The same destinations; their order follows the latest write (see the rewrite test below).
    assert set(rotated.bindings) == set(first.bindings)


def test_every_write_uses_a_fresh_12_byte_nonce(vault, vault_db_path):
    vault.put("r", "same", purpose="p", bindings=[B])
    first = raw_rows(vault_db_path)[0]
    vault.put("r", "same", purpose="p", bindings=[B])
    second = raw_rows(vault_db_path)[0]
    assert len(first[1]) == 12 and len(second[1]) == 12
    assert first[1] != second[1]
    assert first[2] != second[2]


def test_ciphertext_is_bound_to_its_ref(vault, vault_db_path):
    vault.put("a", "value-of-a", purpose="p", bindings=[B])
    vault.put("b", "value-of-b", purpose="p", bindings=[B])
    (_, nonce_a, cipher_a), _ = raw_rows(vault_db_path)
    with sqlite3.connect(vault_db_path) as conn:
        conn.execute(
            "UPDATE secrets SET nonce = ?, ciphertext = ? WHERE ref = 'b'", (nonce_a, cipher_a)
        )
    with pytest.raises(VaultDecryptError):
        vault.reveal("b")


def set_stored_bindings(db: Path, ref: str, bindings_json: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE secrets SET bindings_json = ? WHERE ref = ?", (bindings_json, ref))


def test_editing_the_stored_bindings_makes_reveal_fail(vault, vault_db_path):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B])
    # Someone with write access to the file (but not the key) re-points the secret.
    set_stored_bindings(vault_db_path, "r", TypeAdapter(list[Binding]).dump_json([EVIL]).decode())
    with pytest.raises(VaultDecryptError):
        vault.reveal("r")
    with pytest.raises(VaultDecryptError):
        vault.values()


def test_adding_a_destination_to_the_stored_bindings_makes_reveal_fail(vault, vault_db_path):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B])
    set_stored_bindings(
        vault_db_path, "r", TypeAdapter(list[Binding]).dump_json([B, EVIL]).decode()
    )
    with pytest.raises(VaultDecryptError):
        vault.reveal("r")


def test_unparseable_stored_bindings_make_reveal_fail_cleanly(vault, vault_db_path):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B])
    set_stored_bindings(vault_db_path, "r", "not json")
    with pytest.raises(VaultDecryptError) as excinfo:
        vault.reveal("r")
    assert "s3cr3t-value" not in str(excinfo.value)


def test_a_whole_row_moved_to_another_ref_cannot_be_revealed(vault, vault_db_path):
    vault.put("a", "value-of-a", purpose="p", bindings=[B])
    vault.put("b", "value-of-b", purpose="p", bindings=[EVIL])
    with sqlite3.connect(vault_db_path) as conn:
        conn.execute(
            "UPDATE secrets SET (nonce, ciphertext, bindings_json) ="
            " (SELECT nonce, ciphertext, bindings_json FROM secrets WHERE ref = 'a')"
            " WHERE ref = 'b'"
        )
    with pytest.raises(VaultDecryptError):
        vault.reveal("b")


def test_reordered_but_equivalent_stored_bindings_still_decrypt(vault, vault_db_path):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B, EVIL])
    reordered = TypeAdapter(list[Binding]).dump_json([EVIL, B, B]).decode()
    set_stored_bindings(vault_db_path, "r", reordered)
    assert vault.reveal("r") == "s3cr3t-value"


def test_rotation_with_reordered_bindings_keeps_the_value_readable(vault):
    vault.put("r", "v1", purpose="p", bindings=[B, EVIL])
    vault.put("r", "v2", purpose="p", bindings=[EVIL, B])
    vault.put("r", "v3", purpose="p", bindings=[B, EVIL, B])
    assert vault.reveal("r") == "v3"
    assert vault.values() == ["v3"]


def test_list_metadata_is_sorted_by_ref(vault):
    vault.put("b", "v", purpose="p", bindings=[B])
    vault.put("a", "v", purpose="p", bindings=[B])
    assert [m.ref for m in vault.list_metadata()] == ["a", "b"]


def test_list_metadata_of_an_empty_vault(vault):
    assert vault.list_metadata() == []


def test_values_returns_every_plaintext(vault):
    vault.put("a", "value-of-a", purpose="p", bindings=[B])
    vault.put("b", "value-of-b", purpose="p", bindings=[B])
    assert sorted(vault.values()) == ["value-of-a", "value-of-b"]


def test_values_follows_rotation(vault):
    vault.put("a", "old", purpose="p", bindings=[B])
    vault.put("a", "new", purpose="p", bindings=[B])
    assert vault.values() == ["new"]


def test_mark_used_sets_last_used_at(vault):
    vault.put("r", "v", purpose="p", bindings=[B])
    vault.mark_used("r")
    meta = vault.metadata("r")
    assert meta is not None
    assert meta.last_used_at is not None and meta.last_used_at.utcoffset() is not None


def test_mark_used_unknown_ref_raises_key_error(vault):
    with pytest.raises(KeyError):
        vault.mark_used("nope")


def test_rotation_keeps_last_used_at(vault):
    vault.put("r", "v1", purpose="p", bindings=[B])
    vault.mark_used("r")
    used = vault.metadata("r")
    assert used is not None
    rotated = vault.put("r", "v2", purpose="p", bindings=[B])
    assert rotated.last_used_at == used.last_used_at


def test_count_secrets_is_zero_when_the_file_does_not_exist(tmp_path):
    db = tmp_path / "vault.db"
    assert Vault.count_secrets(db) == 0
    assert not db.exists()


def test_count_secrets_is_zero_for_an_empty_database_file(tmp_path):
    db = tmp_path / "vault.db"
    sqlite3.connect(db).close()
    assert Vault.count_secrets(db) == 0


def test_count_secrets_does_not_need_the_key(vault, vault_db_path):
    assert Vault.count_secrets(vault_db_path) == 0
    vault.put("a", "v", purpose="p", bindings=[B])
    vault.put("b", "v", purpose="p", bindings=[B])
    vault.put("b", "v2", purpose="p", bindings=[B])
    assert Vault.count_secrets(vault_db_path) == 2


def test_count_secrets_does_not_modify_the_database(vault, vault_db_path):
    vault.put("a", "v", purpose="p", bindings=[B])
    before = vault_db_path.read_bytes()
    Vault.count_secrets(vault_db_path)
    assert vault_db_path.read_bytes() == before


def test_count_secrets_refuses_a_file_that_is_not_a_database(tmp_path):
    db = tmp_path / "vault.db"
    db.write_bytes(b"this is definitely not an sqlite database" * 20)
    with pytest.raises(sqlite3.DatabaseError):
        Vault.count_secrets(db)


def test_vault_creates_missing_parent_directories(tmp_path):
    vault = Vault(tmp_path / "nested" / "vault.db", b"\x01" * 32)
    vault.put("r", "v", purpose="p", bindings=[B])
    assert vault.reveal("r") == "v"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_vault_file_is_private(vault, vault_db_path):
    vault.put("r", "v", purpose="p", bindings=[B])
    assert vault_db_path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("key", [b"", b"\x01" * 16, b"\x01" * 31, b"\x01" * 33])
def test_vault_requires_a_32_byte_key(tmp_path, key):
    with pytest.raises(ValueError, match="32 bytes"):
        Vault(tmp_path / "vault.db", key)


def test_vault_does_not_expose_its_key(vault):
    # The raw key is not kept on the object, so it cannot show up in a repr or a traceback.
    assert repr(b"\x01" * 32) not in repr(vars(vault))
    assert not any(isinstance(v, bytes) and v == b"\x01" * 32 for v in vars(vault).values())


@pytest.mark.parametrize("ref", ["", "../x", "Upper", "a b", "trailing\n"])
def test_put_rejects_invalid_refs(vault, ref):
    with pytest.raises(ValueError, match="ref"):
        vault.put(ref, "v", purpose="p", bindings=[B])


def test_put_rejects_an_empty_value(vault):
    with pytest.raises(ValueError, match="value"):
        vault.put("r", "", purpose="p", bindings=[B])
    assert vault.metadata("r") is None


def test_put_rejects_text_that_is_not_valid_unicode_without_leaking_it(vault):
    value = "\ud800s3cr3t-value"  # a lone surrogate cannot be encoded to UTF-8
    with pytest.raises(ValueError, match="valid Unicode") as excinfo:
        vault.put("r", value, purpose="p", bindings=[B])
    error = excinfo.value
    assert isinstance(error, InvalidSecretValueError)
    assert "s3cr3t-value" not in str(error) + repr(error)
    # The encoder's own error (whose repr holds the whole value) is not chained either.
    assert error.__cause__ is None and error.__context__ is None
    assert vault.metadata("r") is None


def test_a_rejected_rotation_keeps_the_stored_value(vault):
    vault.put("r", "old", purpose="p", bindings=[B])
    with pytest.raises(InvalidSecretValueError):
        vault.put("r", "\udfff", purpose="p", bindings=[B])
    assert vault.reveal("r") == "old"


def test_put_requires_at_least_one_binding(vault):
    with pytest.raises(ValueError, match="binding"):
        vault.put("r", "v", purpose="p", bindings=[])
    assert vault.metadata("r") is None


def test_non_ascii_values_roundtrip(vault):
    vault.put("r", "clé-secrète-✓", purpose="p", bindings=[B])
    assert vault.reveal("r") == "clé-secrète-✓"


def test_vault_is_usable_from_several_threads(vault):
    def work(i: int) -> str:
        ref = f"secret-{i}"
        vault.put(ref, f"value-{i}", purpose="p", bindings=[B])
        vault.mark_used(ref)
        vault.list_metadata()
        return vault.reveal(ref)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(32)))
    assert results == [f"value-{i}" for i in range(32)]
    assert len(vault.list_metadata()) == 32


def test_concurrent_puts_of_one_ref_cannot_rebind_it(vault):
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def put(binding: Binding, value: str) -> None:
        start.wait()
        try:
            vault.put("shared", value, purpose="p", bindings=[binding])
            outcomes.append("stored")
        except BindingMismatchError:
            outcomes.append("refused")

    threads = [
        threading.Thread(target=put, args=(B, "from-b")),
        threading.Thread(target=put, args=(EVIL, "from-evil")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["refused", "stored"]
    meta = vault.metadata("shared")
    assert meta is not None and len(meta.bindings) == 1


# The v1 format of the associated data is a storage format: changing it makes every existing
# secret undecryptable. These tests pin it with literals.
STORED_B_JSON = (
    '[{"host":"api.example.com","port":null,'
    '"location":{"kind":"header","name":"x-api-key","template":"{value}"}}]'
)
AAD_V1_FOR_R_AND_B = (
    b"domo-vault-aad/v1\n"
    b"r\n"
    b'[{"host":"api.example.com","location":{"kind":"header","name":"x-api-key",'
    b'"template":"{value}"},"port":null}]'
)


def test_associated_data_v1_format_is_pinned():
    assert _associated_data("r", [B]) == AAD_V1_FOR_R_AND_B
    assert _stored_associated_data("r", STORED_B_JSON) == AAD_V1_FOR_R_AND_B


def test_associated_data_v1_is_canonical_for_several_bindings():
    other = Binding(
        host="localhost",
        port=8080,
        location=InjectionLocation(kind="header", name="authorization", template="Bearer {value}"),
    )
    expected = (
        b"domo-vault-aad/v1\nr\n["
        b'{"host":"api.example.com","location":{"kind":"header","name":"x-api-key",'
        b'"template":"{value}"},"port":null},'
        b'{"host":"localhost","location":{"kind":"header","name":"authorization",'
        b'"template":"Bearer {value}"},"port":8080}]'
    )
    assert _associated_data("r", [other, B, other]) == expected
    assert _associated_data("r", [B, other]) == expected


def insert_row(db: Path, *, ref: str, bindings_json: str, value: str, aad: bytes) -> None:
    nonce = b"\x07" * 12
    ciphertext = AESGCM(b"\x01" * 32).encrypt(nonce, value.encode(), aad)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO secrets (ref, purpose, bindings_json, nonce, ciphertext, created_at)"
            " VALUES (?, 'p', ?, ?, ?, '2030-01-01T00:00:00+00:00')",
            (ref, bindings_json, nonce, ciphertext),
        )


def test_a_row_sealed_with_the_literal_v1_aad_can_be_revealed(vault, vault_db_path):
    insert_row(
        vault_db_path, ref="r", bindings_json=STORED_B_JSON, value="legacy", aad=AAD_V1_FOR_R_AND_B
    )
    assert vault.reveal("r") == "legacy"
    assert vault.values() == ["legacy"]


def test_reveal_does_not_depend_on_the_current_model_shape(vault, vault_db_path):
    # A later version may add fields to the models: rows written before must still decrypt, and
    # the fields the v1 format does not cover cannot change what it authenticates.
    future_json = (
        '[{"host":"api.example.com","port":null,"future":1,'
        '"location":{"kind":"header","name":"x-api-key","template":"{value}","future":true}}]'
    )
    insert_row(
        vault_db_path, ref="r", bindings_json=future_json, value="legacy", aad=AAD_V1_FOR_R_AND_B
    )
    assert vault.reveal("r") == "legacy"


def test_a_ref_only_or_unversioned_aad_is_not_accepted(vault, vault_db_path):
    insert_row(vault_db_path, ref="r", bindings_json=STORED_B_JSON, value="old", aad=b"r")
    with pytest.raises(VaultDecryptError):
        vault.reveal("r")


# --- Task 4 carry-overs -------------------------------------------------------------------------


def test_the_binding_models_have_exactly_the_fields_the_aad_covers():
    # The v1 associated data covers exactly these fields (see vault._canonical_binding). Adding
    # a field to either model means deciding whether it is part of the authenticated data: if it
    # is, that needs a v2 AAD (new prefix, and v1 rows must stay readable).
    assert set(Binding.model_fields) == {"host", "port", "location"}
    assert set(InjectionLocation.model_fields) == {"kind", "name", "template"}


def stored_bindings_json(db: Path, ref: str) -> str:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT bindings_json FROM secrets WHERE ref = ?", (ref,)).fetchone()[0]


def test_rotation_rewrites_the_stored_bindings_json(vault, vault_db_path):
    vault.put("r", "v1", purpose="p", bindings=[B, EVIL])
    # Same destinations, written in another order (and with a duplicate): the stored JSON must
    # be the one the new ciphertext was sealed with.
    vault.put("r", "v2", purpose="p", bindings=[EVIL, B, B])
    fresh = TypeAdapter(list[Binding]).dump_json([EVIL, B]).decode()
    assert stored_bindings_json(vault_db_path, "r") == fresh
    assert vault.reveal("r") == "v2"


def test_the_stored_bindings_json_always_matches_the_sealed_associated_data(vault, vault_db_path):
    vault.put("r", "v1", purpose="p", bindings=[B, EVIL])
    vault.put("r", "v2", purpose="p", bindings=[EVIL, B])
    stored = stored_bindings_json(vault_db_path, "r")
    assert _stored_associated_data("r", stored) == _associated_data("r", [EVIL, B])


def test_deeply_nested_stored_bindings_make_reveal_fail_cleanly(vault, vault_db_path):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B])
    set_stored_bindings(vault_db_path, "r", "[" * 200_000 + "]" * 200_000)
    with pytest.raises(VaultDecryptError) as excinfo:
        vault.reveal("r")
    assert "s3cr3t-value" not in str(excinfo.value)
    with pytest.raises(VaultDecryptError):
        vault.values()
    with pytest.raises(VaultDecryptError):
        vault.reveal_bound("r")


# --- reveal_bound: one authenticated read -------------------------------------------------------


def test_reveal_bound_returns_the_value_with_its_authenticated_bindings(vault):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B, B_AUTH])
    value, bindings = vault.reveal_bound("r")
    assert value == "s3cr3t-value"
    assert set(bindings) == {B, B_AUTH}
    assert all(isinstance(binding, Binding) for binding in bindings)


def test_reveal_bound_follows_rotation(vault):
    vault.put("r", "v1", purpose="p", bindings=[B])
    vault.put("r", "v2", purpose="p", bindings=[B])
    assert vault.reveal_bound("r") == ("v2", [B])


def test_reveal_bound_of_unknown_ref_raises_key_error(vault):
    with pytest.raises(KeyError):
        vault.reveal_bound("nope")


def test_reveal_bound_refuses_edited_bindings(vault, vault_db_path):
    vault.put("r", "s3cr3t-value", purpose="p", bindings=[B])
    set_stored_bindings(vault_db_path, "r", TypeAdapter(list[Binding]).dump_json([EVIL]).decode())
    with pytest.raises(VaultDecryptError) as excinfo:
        vault.reveal_bound("r")
    assert "s3cr3t-value" not in str(excinfo.value)


def test_reveal_bound_refuses_a_wrong_master_key(vault_db_path):
    Vault(vault_db_path, b"\x01" * 32).put("r", "v", purpose="p", bindings=[B])
    with pytest.raises(VaultDecryptError):
        Vault(vault_db_path, b"\x02" * 32).reveal_bound("r")


def test_reveal_bound_returns_only_what_the_aad_authenticated(vault, vault_db_path):
    # Fields the v1 format does not cover are not authenticated, so they are not returned.
    future_json = (
        '[{"host":"api.example.com","port":null,"future":1,'
        '"location":{"kind":"header","name":"x-api-key","template":"{value}","future":true}}]'
    )
    insert_row(
        vault_db_path, ref="r", bindings_json=future_json, value="legacy", aad=AAD_V1_FOR_R_AND_B
    )
    assert vault.reveal_bound("r") == ("legacy", [B])


def test_reveal_bound_reads_the_database_once(vault, monkeypatch):
    vault.put("r", "v", purpose="p", bindings=[B])
    opened = 0
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("domo_gatekeeper.vault.sqlite3.connect", counting_connect)
    vault.reveal_bound("r")
    assert opened == 1
