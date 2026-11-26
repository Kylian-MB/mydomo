"""The house's name for the assistant (``house.domo_name``) and the one function that picks the
name a reader sees (``naming.name_shown``)."""

import uuid

import pytest
from domo_core.naming import DEFAULT_NAME, MAX_DOMO_NAME_LENGTH, is_domo_name, name_shown
from domo_core.policy.roles import Actor
from domo_core.policy.settings import (
    HOUSE_DOMO_NAME,
    HOUSE_DOMO_NAME_BEFORE,
    NAME_SETTINGS,
    InvalidSetting,
    PolicySettings,
    validate_setting,
)
from domo_core.tools.profile import MAX_DOMO_NAME_LENGTH as PROFILE_MAX
from domo_core.tools.registry import TOOLS, InvalidArguments


def test_name_shown_order():
    assert name_shown("Brenda", "Friday") == "Brenda"
    assert name_shown(None, "Friday") == "Friday"
    assert name_shown("", "Friday") == "Friday"
    assert name_shown(None, None) == DEFAULT_NAME == "Domo"


def test_the_limit_is_one_value_everywhere():
    assert PROFILE_MAX == MAX_DOMO_NAME_LENGTH == 40


@pytest.mark.parametrize("key", sorted(NAME_SETTINGS))
@pytest.mark.parametrize("bad", ["", " Max", "a" * 41, "Max\nMax", 7, True, "Max\u202e"])
def test_setting_refuses(key, bad):
    with pytest.raises(InvalidSetting):
        validate_setting(key, bad)
    assert not is_domo_name(bad)


def test_setting_accepts_and_clears():
    validate_setting(HOUSE_DOMO_NAME, "Friday")
    validate_setting(HOUSE_DOMO_NAME, "a" * 40)
    validate_setting(HOUSE_DOMO_NAME, None)
    validate_setting(HOUSE_DOMO_NAME_BEFORE, "Domo")


def test_bad_row_reads_as_none():
    assert PolicySettings({HOUSE_DOMO_NAME: "a" * 41}).house_domo_name is None
    assert PolicySettings({}).house_domo_name is None
    assert PolicySettings({HOUSE_DOMO_NAME: "Friday"}).house_domo_name == "Friday"
    assert PolicySettings({HOUSE_DOMO_NAME_BEFORE: "Max"}).house_domo_name_before == "Max"
    assert PolicySettings({HOUSE_DOMO_NAME_BEFORE: 7}).house_domo_name_before is None


def test_rows_show_the_name_once_set():
    rows = PolicySettings({HOUSE_DOMO_NAME: "Friday"}).rows()
    assert {"key": HOUSE_DOMO_NAME, "value": "Friday", "source": "owner"} in rows
    assert all(r["key"] != HOUSE_DOMO_NAME_BEFORE for r in rows)
    assert all(r["key"] != HOUSE_DOMO_NAME for r in PolicySettings({}).rows())


@pytest.mark.parametrize("key", sorted(NAME_SETTINGS))
@pytest.mark.parametrize("value", ["Friday", None])
async def test_policy_update_refuses_the_name_keys(key, value):
    arguments = {"key": key, "value": value}
    actor = Actor(person_id=uuid.uuid4(), role="owner")
    with pytest.raises(InvalidArguments) as refused:
        await TOOLS["policy_update"].describe(arguments, actor, None)
    assert refused.value.code == "invalid"
    with pytest.raises(InvalidArguments):
        await TOOLS["policy_update"].execute(arguments, None)
