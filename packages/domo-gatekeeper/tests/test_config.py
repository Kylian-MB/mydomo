import os
from pathlib import Path

import pytest
from domo_common import core_token
from domo_gatekeeper import config
from domo_gatekeeper.config import GatekeeperSettings, load_core_token
from domo_gatekeeper.errors import ConfigurationError
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DOMO_ variable of the machine running the tests may leak into the settings."""
    for name in list(os.environ):
        if name.upper().startswith("DOMO_"):
            monkeypatch.delenv(name)


def test_defaults_match_the_container_layout():
    settings = GatekeeperSettings()
    assert settings.gatekeeper_data_dir == Path("/data")
    assert settings.core_token_file == Path("/run/domo/core-auth/core_token")
    assert settings.gatekeeper_internal_port == 8081
    assert settings.gatekeeper_public_port == 8082
    assert settings.gatekeeper_host == "0.0.0.0"


def test_every_variable_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_GATEKEEPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("DOMO_GATEKEEPER_INTERNAL_PORT", "9001")
    monkeypatch.setenv("DOMO_GATEKEEPER_PUBLIC_PORT", "9002")
    monkeypatch.setenv("DOMO_GATEKEEPER_HOST", "127.0.0.1")
    settings = GatekeeperSettings()
    assert settings.gatekeeper_data_dir == tmp_path / "data"
    assert settings.core_token_file == tmp_path / "token"
    assert (settings.gatekeeper_internal_port, settings.gatekeeper_public_port) == (9001, 9002)
    assert settings.gatekeeper_host == "127.0.0.1"


def test_the_data_files_live_in_the_data_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_GATEKEEPER_DATA_DIR", str(tmp_path))
    settings = GatekeeperSettings()
    assert settings.vault_db_path == tmp_path / "vault.db"
    assert settings.master_key_path == tmp_path / "master.key"
    assert settings.requests_db_path == tmp_path / "requests.db"
    assert settings.providers_db_path == tmp_path / "providers.db"


def test_unrelated_domo_variables_are_ignored(monkeypatch):
    # The core's variables share the prefix and may be present in the same environment file.
    monkeypatch.setenv("DOMO_GATEWAY_URL", "http://gatekeeper:8081")
    GatekeeperSettings()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "http"])
def test_a_port_must_be_a_valid_port(monkeypatch, port):
    monkeypatch.setenv("DOMO_GATEKEEPER_INTERNAL_PORT", port)
    with pytest.raises(ValidationError):
        GatekeeperSettings()


def test_the_two_servers_cannot_share_a_port(monkeypatch):
    monkeypatch.setenv("DOMO_GATEKEEPER_INTERNAL_PORT", "9000")
    monkeypatch.setenv("DOMO_GATEKEEPER_PUBLIC_PORT", "9000")
    with pytest.raises(ValidationError, match="different ports"):
        GatekeeperSettings()


# --- The core token -----------------------------------------------------------------------------
# The rule itself is domo_common's (packages/domo-common/tests/test_core_token.py), shared with
# the core: here, only that the gatekeeper reads the token with it and stops with its own error.


def write_token(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "core_token"
    path.write_bytes(content)
    return path


def test_the_token_is_read_from_the_file(tmp_path):
    assert load_core_token(write_token(tmp_path, b"  abc-DEF_123\r\n")) == "abc-DEF_123"


def test_a_missing_token_file_stops_the_start_with_a_clear_message(tmp_path):
    path = tmp_path / "absent" / "core_token"
    with pytest.raises(ConfigurationError) as excinfo:
        load_core_token(path)
    message = str(excinfo.value)
    assert str(path) in message and "core token" in message.lower()
    assert "DOMO_CORE_TOKEN_FILE" in message
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@pytest.mark.parametrize("content", [b"", b"two words", "café".encode()])
def test_a_token_the_shared_rule_refuses_stops_the_start(tmp_path, content):
    with pytest.raises(ConfigurationError) as excinfo:
        load_core_token(write_token(tmp_path, content))
    # The message names the file's problem, never the content: it is a credential.
    assert "caf" not in str(excinfo.value) and "words" not in str(excinfo.value)


def test_the_gatekeeper_reads_the_token_with_the_shared_rule():
    """One rule for both sides: a token the core accepts is never refused here (every call
    would then fail with 401)."""
    assert config.common_load_core_token is core_token.load_core_token
