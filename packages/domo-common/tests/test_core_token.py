"""The core token file: the one rule the core and the gatekeeper both read it with."""

import os
import stat
from pathlib import Path

import pytest
from domo_common.core_token import CoreTokenError, load_core_token


def write_token(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "core_token"
    path.write_bytes(content)
    return path


def test_the_token_is_read_from_the_file(tmp_path):
    assert load_core_token(write_token(tmp_path, b"abc-DEF_123")) == "abc-DEF_123"


@pytest.mark.parametrize(
    "content", [b"abc123\n", b"abc123\r\n", b"  abc123  \n\n", b"  abc123\r\n"]
)
def test_surrounding_whitespace_is_not_part_of_the_token(tmp_path, content):
    assert load_core_token(write_token(tmp_path, content)) == "abc123"


def test_every_printable_ascii_character_can_be_part_of_the_token(tmp_path):
    every = bytes(range(0x21, 0x7F))
    assert load_core_token(write_token(tmp_path, every)) == every.decode("ascii")


def test_a_missing_token_file_is_a_clear_error(tmp_path):
    path = tmp_path / "absent" / "core_token"
    with pytest.raises(CoreTokenError) as excinfo:
        load_core_token(path)
    message = str(excinfo.value)
    assert "core token file is missing" in message and str(path) in message
    assert "DOMO_CORE_TOKEN_FILE" in message


@pytest.mark.parametrize("content", [b"", b"\n", b"   \r\n"])
def test_an_empty_token_file_is_refused(tmp_path, content):
    with pytest.raises(CoreTokenError, match="empty"):
        load_core_token(write_token(tmp_path, content))


@pytest.mark.parametrize(
    "content",
    [b"two words", b"tab\there", b"line\nbreak", "café".encode(), b"\xff\xfe", b"\x00abc"],
)
def test_a_token_that_cannot_travel_in_a_header_is_refused_without_echoing_it(tmp_path, content):
    with pytest.raises(CoreTokenError, match="printable ASCII") as excinfo:
        load_core_token(write_token(tmp_path, content))
    # The message names the file's problem, never the content: it is a credential.
    message = str(excinfo.value)
    assert content.decode("latin-1").strip() not in message
    assert "caf" not in message and "words" not in message


def test_a_directory_instead_of_a_file_is_refused(tmp_path):
    with pytest.raises(CoreTokenError, match="cannot be read"):
        load_core_token(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_an_unreadable_token_file_is_refused(tmp_path):
    path = write_token(tmp_path, b"abc123")
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):  # running as root: permissions do not apply
            pytest.skip("the file is readable despite its mode")
        with pytest.raises(CoreTokenError, match="cannot be read"):
            load_core_token(path)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_the_error_is_a_value_error_and_chains_nothing(tmp_path):
    """Callers turn it into their own configuration error; no OS error rides along with it."""
    with pytest.raises(CoreTokenError) as excinfo:
        load_core_token(tmp_path / "absent")
    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
