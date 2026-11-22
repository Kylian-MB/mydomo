from datetime import timedelta
from pathlib import Path

import pytest
from domo_core.config import ConfigurationError, CoreSettings
from pydantic import ValidationError
from sqlalchemy.engine import make_url

# A password that every URL parser would choke on if it were not encoded.
AWKWARD_PASSWORD = "p@ss:w/ord%20 #?&=+é"


def test_defaults_match_the_container_layout():
    settings = CoreSettings()
    assert settings.db_host == "db"
    assert settings.db_name == "domo"
    assert settings.db_user == "domo"
    assert settings.db_password_file == Path("/run/domo/db-auth/db_password")
    assert settings.gatekeeper_url == "http://gatekeeper:8081"
    assert settings.core_token_file == Path("/run/domo/core-auth/core_token")
    assert settings.max_history_chars == 48_000
    assert settings.max_output_tokens == 2048
    assert settings.session_ttl_days == 30
    assert settings.trusted_proxies == "127.0.0.1"


def test_every_variable_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DOMO_DB_HOST", "db.example.com")
    monkeypatch.setenv("DOMO_DB_NAME", "other")
    monkeypatch.setenv("DOMO_DB_USER", "someone")
    monkeypatch.setenv("DOMO_DB_PASSWORD_FILE", str(tmp_path / "pw"))
    monkeypatch.setenv("DOMO_GATEKEEPER_URL", "http://gatekeeper.example.com:9000")
    monkeypatch.setenv("DOMO_CORE_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("DOMO_MAX_HISTORY_CHARS", "1000")
    monkeypatch.setenv("DOMO_MAX_OUTPUT_TOKENS", "64")
    monkeypatch.setenv("DOMO_SESSION_TTL_DAYS", "7")
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", "172.20.0.2")
    settings = CoreSettings()
    assert (settings.db_host, settings.db_name, settings.db_user) == (
        "db.example.com",
        "other",
        "someone",
    )
    assert settings.db_password_file == tmp_path / "pw"
    assert settings.gatekeeper_url == "http://gatekeeper.example.com:9000"
    assert settings.core_token_file == tmp_path / "token"
    assert settings.max_history_chars == 1000
    assert settings.max_output_tokens == 64
    assert settings.session_ttl_days == 7
    assert settings.trusted_proxies == "172.20.0.2"


def test_the_gatekeeper_settings_in_the_same_environment_are_ignored(monkeypatch):
    monkeypatch.setenv("DOMO_GATEKEEPER_DATA_DIR", "/elsewhere")
    assert CoreSettings().db_host == "db"


def test_database_url_is_built_from_the_password_file(tmp_path):
    password_file = tmp_path / "db_password"
    password_file.write_text("s3cret-value\n", encoding="utf-8")
    settings = CoreSettings(
        db_host="db.example.com", db_name="domo", db_user="domo", db_password_file=password_file
    )
    assert settings.database_url == "postgresql+asyncpg://domo:s3cret-value@db.example.com/domo"


def test_the_password_is_url_encoded(tmp_path):
    password_file = tmp_path / "db_password"
    password_file.write_text(AWKWARD_PASSWORD, encoding="utf-8")
    settings = CoreSettings(db_password_file=password_file)
    url = settings.database_url
    assert AWKWARD_PASSWORD not in url
    assert url.count("@") == 1  # only the separator before the host
    parsed = make_url(url)
    assert parsed.password == AWKWARD_PASSWORD
    assert (parsed.username, parsed.host, parsed.database) == ("domo", "db", "domo")
    assert parsed.drivername == "postgresql+asyncpg"


def test_only_trailing_newlines_are_stripped_from_the_password(tmp_path):
    password_file = tmp_path / "db_password"
    password_file.write_bytes(b" padded \r\n")
    assert make_url(CoreSettings(db_password_file=password_file).database_url).password == (
        " padded "
    )


def test_the_password_file_is_read_each_time(tmp_path):
    password_file = tmp_path / "db_password"
    password_file.write_text("first", encoding="utf-8")
    settings = CoreSettings(db_password_file=password_file)
    assert make_url(settings.database_url).password == "first"
    password_file.write_text("second", encoding="utf-8")
    assert make_url(settings.database_url).password == "second"


def test_missing_password_file_is_a_clear_error(tmp_path):
    settings = CoreSettings(db_password_file=tmp_path / "missing")
    with pytest.raises(ConfigurationError, match="missing") as error:
        _ = settings.database_url
    message = str(error.value)
    assert str(tmp_path / "missing") in message
    assert "DOMO_DB_PASSWORD_FILE" in message


@pytest.mark.parametrize("content", ["", "\n", "\r\n"])
def test_empty_password_file_is_an_error_not_an_empty_password(tmp_path, content):
    password_file = tmp_path / "db_password"
    password_file.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="empty"):
        _ = CoreSettings(db_password_file=password_file).database_url


def test_unreadable_password_file_is_a_clear_error(tmp_path):
    # A directory cannot be read as a file, on every platform.
    with pytest.raises(ConfigurationError, match="cannot be read"):
        _ = CoreSettings(db_password_file=tmp_path).database_url


def test_database_url_override_wins_and_needs_no_password_file(monkeypatch, tmp_path):
    url = "postgresql+asyncpg://user:pw@localhost:5432/test"
    monkeypatch.setenv("DOMO_DATABASE_URL", url)
    settings = CoreSettings(db_password_file=tmp_path / "missing")
    assert settings.database_url == url


@pytest.mark.parametrize("empty", ["", "   ", "\t"])
def test_an_empty_database_url_counts_as_unset(monkeypatch, tmp_path, empty):
    """Compose files pass ``DOMO_DATABASE_URL=`` when the variable is not set: the stack's
    database (password file) must be used, not an empty URL."""
    password_file = tmp_path / "db_password"
    password_file.write_text("s3cret-value\n", encoding="utf-8")
    monkeypatch.setenv("DOMO_DATABASE_URL", empty)
    settings = CoreSettings(db_host="db.example.com", db_password_file=password_file)
    assert settings.database_url_override is None
    assert settings.database_url == "postgresql+asyncpg://domo:s3cret-value@db.example.com/domo"


def test_an_empty_database_url_given_directly_counts_as_unset(tmp_path):
    settings = CoreSettings(database_url_override="", db_password_file=tmp_path / "missing")
    assert settings.database_url_override is None
    with pytest.raises(ConfigurationError, match="missing"):
        _ = settings.database_url


def test_database_url_override_can_be_given_directly():
    url = "postgresql+asyncpg://user:pw@localhost:5432/test"
    assert CoreSettings(database_url_override=url).database_url == url


def test_the_session_ttl_is_the_configured_number_of_days():
    assert CoreSettings().session_ttl == timedelta(days=30)
    assert CoreSettings(session_ttl_days=7).session_ttl == timedelta(days=7)


def test_the_password_never_shows_in_a_repr_or_a_dump(tmp_path):
    password_file = tmp_path / "db_password"
    password_file.write_text("do-not-leak-me", encoding="utf-8")
    from_file = CoreSettings(db_password_file=password_file)
    _ = from_file.database_url  # reading it must not cache it where a repr could show it
    overridden = CoreSettings(database_url_override="postgresql+asyncpg://user:do-not-leak-me@h/d")
    for settings in (from_file, overridden):
        for rendering in (repr(settings), str(settings), str(settings.model_dump())):
            assert "do-not-leak-me" not in rendering


@pytest.mark.parametrize("name", ["max_history_chars", "max_output_tokens", "session_ttl_days"])
def test_limits_must_be_positive(name):
    with pytest.raises(ValidationError):
        CoreSettings(**{name: 0})


def test_the_trusted_proxies_default_to_the_local_machine_not_to_everyone():
    assert CoreSettings().trusted_proxies == "127.0.0.1"


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_trusted_proxies_count_as_unset(monkeypatch, empty):
    # Compose passes an empty value for a variable that is not set.
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", empty)
    assert CoreSettings().trusted_proxies == "127.0.0.1"


def test_trusted_proxies_are_trimmed(monkeypatch):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", " 172.20.0.2 ")
    assert CoreSettings().trusted_proxies == "172.20.0.2"


# --- trusted proxies: addresses and networks only ---------------------------------------------
# uvicorn compares the client's address with each entry and never resolves a name: an entry that
# is not an address or a network silently matches nothing, and "*" trusts everyone.


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("10.247.81.2", "10.247.81.2"),
        ("10.247.81.0/24", "10.247.81.0/24"),
        ("::1", "::1"),
        ("fd00:1::/64", "fd00:1::/64"),
        ("10.247.81.2, 127.0.0.1", "10.247.81.2,127.0.0.1"),
        ("172.20.0.0/16,172.21.0.2", "172.20.0.0/16,172.21.0.2"),
    ],
)
def test_trusted_proxies_accept_addresses_and_networks(monkeypatch, given, kept):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", given)
    assert CoreSettings().trusted_proxies == kept


@pytest.mark.parametrize("everyone", ["*", "10.247.81.2,*", " * "])
def test_trusting_every_address_is_refused(monkeypatch, everyone):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", everyone)
    with pytest.raises(ValidationError, match="every address"):
        CoreSettings()


@pytest.mark.parametrize(
    "everyone", ["0.0.0.0/0", "::/0", "10.247.81.2, 0.0.0.0/0", "10.0.0.1/0", "::1/0"]
)
def test_a_network_of_every_address_is_refused_like_the_star(monkeypatch, everyone):
    # A prefix length of 0 covers every address of its family: the same as "*".
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", everyone)
    with pytest.raises(ValidationError, match="every address"):
        CoreSettings()


def test_a_wide_but_not_universal_network_is_still_accepted(monkeypatch):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", "10.0.0.0/8")
    assert CoreSettings().trusted_proxies == "10.0.0.0/8"


@pytest.mark.parametrize("name", ["edge", "localhost", "edge.example.com"])
def test_host_names_are_refused_since_nothing_resolves_them(monkeypatch, name):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", f"127.0.0.1,{name}")
    with pytest.raises(ValidationError, match="not an IP address or a network") as error:
        CoreSettings()
    assert name in str(error.value)


def test_a_network_with_host_bits_is_refused_with_the_network_meant(monkeypatch):
    # uvicorn would take "10.247.81.2/24" for a name, and it would match nothing.
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", "10.247.81.2/24")
    with pytest.raises(ValidationError, match=r"10\.247\.81\.0/24"):
        CoreSettings()


@pytest.mark.parametrize("value", ["10.247.81.2,", ",10.247.81.2", "10.247.81.2,,127.0.0.1"])
def test_an_empty_entry_is_refused(monkeypatch, value):
    monkeypatch.setenv("DOMO_TRUSTED_PROXIES", value)
    with pytest.raises(ValidationError, match="empty entry"):
        CoreSettings()


def test_trusted_proxies_given_directly_are_checked_too():
    with pytest.raises(ValidationError, match="every address"):
        CoreSettings(trusted_proxies="*")
