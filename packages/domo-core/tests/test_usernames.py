import pytest
from domo_core.accounts.usernames import normalize_username


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("owner", "owner"),
        ("Owner", "owner"),
        ("  OWNER\t", "owner"),
        ("\nowner \n", "owner"),
        ("first last", "first last"),  # spaces inside the name are part of it
        ("ÉLODIE", "élodie"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_usernames_are_trimmed_and_lower_cased(typed: str, stored: str):
    assert normalize_username(typed) == stored


@pytest.mark.parametrize("typed", ["Owner", " A b ", "ÉLODIE"])
def test_normalising_twice_changes_nothing(typed: str):
    once = normalize_username(typed)
    assert normalize_username(once) == once
