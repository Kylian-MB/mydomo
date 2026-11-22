import sys

import pytest
from domo_gatekeeper import master_key
from domo_gatekeeper.errors import MasterKeyCorruptError, MasterKeyMissingError
from domo_gatekeeper.master_key import load_or_create_master_key


def test_missing_key_with_existing_secrets_refuses(tmp_path):  # Review Focus 2
    with pytest.raises(MasterKeyMissingError):
        load_or_create_master_key(tmp_path / "master.key", vault_has_secrets=True)
    assert not (tmp_path / "master.key").exists()


def test_missing_key_error_names_the_path_and_the_remedy(tmp_path):
    path = tmp_path / "master.key"
    with pytest.raises(MasterKeyMissingError) as excinfo:
        load_or_create_master_key(path, vault_has_secrets=True)
    assert str(path) in str(excinfo.value)
    assert "restore the key from your backup" in str(excinfo.value)


def test_corrupt_key_refuses(tmp_path):
    (tmp_path / "master.key").write_bytes(b"short")
    with pytest.raises(MasterKeyCorruptError):
        load_or_create_master_key(tmp_path / "master.key", vault_has_secrets=False)


@pytest.mark.parametrize("content", [b"", b"\x00" * 31, b"\x00" * 33, b"\x00" * 64])
def test_key_of_the_wrong_size_refuses_even_with_secrets(tmp_path, content):
    (tmp_path / "master.key").write_bytes(content)
    with pytest.raises(MasterKeyCorruptError):
        load_or_create_master_key(tmp_path / "master.key", vault_has_secrets=True)


def test_corrupt_key_error_does_not_leak_the_file_content(tmp_path):
    (tmp_path / "master.key").write_bytes(b"leaky-key-material")
    with pytest.raises(MasterKeyCorruptError) as excinfo:
        load_or_create_master_key(tmp_path / "master.key", vault_has_secrets=False)
    assert "leaky-key-material" not in str(excinfo.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_new_key_file_is_private(tmp_path):
    load_or_create_master_key(tmp_path / "master.key", vault_has_secrets=False)
    assert (tmp_path / "master.key").stat().st_mode & 0o077 == 0


def test_new_key_is_32_random_bytes_and_is_persisted(tmp_path):
    path = tmp_path / "master.key"
    key = load_or_create_master_key(path, vault_has_secrets=False)
    assert len(key) == 32
    assert path.read_bytes() == key


def test_two_new_keys_differ(tmp_path):
    first = load_or_create_master_key(tmp_path / "a.key", vault_has_secrets=False)
    second = load_or_create_master_key(tmp_path / "b.key", vault_has_secrets=False)
    assert first != second


def test_existing_key_is_loaded_unchanged(tmp_path):
    path = tmp_path / "master.key"
    created = load_or_create_master_key(path, vault_has_secrets=False)
    assert load_or_create_master_key(path, vault_has_secrets=False) == created
    assert load_or_create_master_key(path, vault_has_secrets=True) == created
    assert path.read_bytes() == created


def test_new_key_is_written_as_raw_bytes(tmp_path, monkeypatch):
    # Newline bytes must survive on Windows, where a text-mode write would turn \n into \r\n.
    tricky = b"\n\r\n" * 10 + b"\x00\x1a"
    assert len(tricky) == 32
    monkeypatch.setattr(master_key, "token_bytes", lambda n: tricky)
    path = tmp_path / "master.key"
    assert load_or_create_master_key(path, vault_has_secrets=False) == tricky
    assert path.read_bytes() == tricky


def test_parent_directory_is_created(tmp_path):
    path = tmp_path / "nested" / "dir" / "master.key"
    key = load_or_create_master_key(path, vault_has_secrets=False)
    assert path.read_bytes() == key


def test_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / "master.key"
    existing = b"\x07" * 32
    path.write_bytes(existing)
    # Even if a caller raced and thought the key was missing, creation must not clobber it.
    with pytest.raises(FileExistsError):
        master_key.create_master_key(path)
    assert path.read_bytes() == existing
