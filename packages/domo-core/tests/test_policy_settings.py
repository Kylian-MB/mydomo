"""The owner's policy settings: the defaults that live in the code, what the owner chose
(``policy_settings`` rows), and the rules that read them (plan decision D8): how long a request may
wait, the rights writer's time, and the household's time zone, which has no default (spec 5.5).
How an action is confirmed is no setting: the yes-judge decides it (``test_judge_decides.py``)."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.policy.models import PolicySetting
from domo_core.policy.settings import (
    HEAR_OUTSIDE_KEY,
    HOUSE_LANGUAGE,
    INVITEE_DEFAULT_KEY,
    INVITEE_GENERAL_KEY,
    KEYS_SETTINGS,
    LIFETIME_KEY,
    SETTING_DEFAULTS,
    TOOLS_BASE_KEY,
    UPKEEP_KEY,
    WRITER_SECONDS_KEY,
    InvalidSetting,
    PolicySettings,
    load_policy_settings,
    update_policy_setting,
    validate_setting,
)
from sqlalchemy import func, select, text

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


@pytest.fixture
async def owner(add_person):
    return await add_person("Owner", "owner")


async def stored(session) -> dict[str, object]:
    rows = await session.execute(select(PolicySetting.key, PolicySetting.value))
    return {key: value for key, value in rows}


# --- the defaults --------------------------------------------------------------------------


def test_the_settings_are_the_lifetime_the_writer_time_and_the_zone():
    assert SETTING_DEFAULTS == {"confirmation_max_lifetime_hours": 168, "rights_writer.seconds": 60}
    # The styles of before are no settings: Domo judges the yes.
    for key in (
        "confirmation_style.confirm",
        "confirmation_style.always_confirm",
        "confirmation_style.action.household.invite",
        "confirmation_style.action.chain.run",
    ):
        for value in ("natural", "explicit", None):
            with pytest.raises(InvalidSetting, match="unknown setting"):
                validate_setting(key, value)
    assert [row["key"] for row in PolicySettings({}).rows()] == [
        "confirmation_max_lifetime_hours",
        "rights_writer.seconds",
    ]
    with_zone = PolicySettings({"household.time_zone": "Europe/Paris"})
    assert [row["key"] for row in with_zone.rows()] == [
        "confirmation_max_lifetime_hours",
        "rights_writer.seconds",
        "household.time_zone",
    ]


def test_the_defaults_cannot_be_changed_by_accident():
    with pytest.raises(TypeError):
        SETTING_DEFAULTS["confirmation_max_lifetime_hours"] = 1


async def test_settings_start_from_the_code_defaults(session):
    s = await load_policy_settings(session)
    assert s.max_lifetime == timedelta(hours=168)
    assert s.writer_seconds == 60
    assert s.time_zone is None


# --- changing them -------------------------------------------------------------------------


async def test_the_lifetime_is_read_back_as_a_duration(session, owner):
    await update_policy_setting(session, key=LIFETIME_KEY, value=24, by=owner.id, now=NOW)
    assert (await load_policy_settings(session)).max_lifetime == timedelta(hours=24)


async def test_clearing_the_lifetime_brings_back_the_default(session, owner):
    await update_policy_setting(session, key=LIFETIME_KEY, value=24, by=owner.id, now=NOW)
    old_new = await update_policy_setting(
        session, key=LIFETIME_KEY, value=None, by=owner.id, now=NOW
    )
    assert old_new == (24, None)
    assert (await load_policy_settings(session)).max_lifetime == timedelta(hours=168)
    assert await stored(session) == {}


async def test_the_first_choice_has_no_old_value_and_the_next_one_has(session, owner):
    first = await update_policy_setting(session, key=LIFETIME_KEY, value=24, by=owner.id, now=NOW)
    second = await update_policy_setting(session, key=LIFETIME_KEY, value=48, by=owner.id, now=NOW)
    assert first == (None, 24)
    assert second == (24, 48)
    assert await stored(session) == {LIFETIME_KEY: 48}


async def test_two_first_choices_of_one_key_do_not_collide(session_factory):  # R58
    # Nobody has chosen yet, so there is no row to lock: the second writer must wait for the
    # first one's row, then change it, instead of failing on the key it tries to insert.
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        writer.add(owner)
        await writer.commit()
        owner_id = owner.id
    key = LIFETIME_KEY
    async with session_factory() as first, session_factory() as second:
        first_change = await update_policy_setting(first, key=key, value=24, by=owner_id, now=NOW)
        assert first_change == (None, 24)
        racing = asyncio.create_task(
            update_policy_setting(second, key=key, value=48, by=owner_id, now=LATER)
        )
        await asyncio.sleep(0.3)
        assert not racing.done()  # it waits for the first choice to be committed or not
        await first.commit()
        assert await racing == (24, 48)
        await second.commit()
    async with session_factory() as reader:
        assert await stored(reader) == {key: 48}


async def test_a_first_choice_that_is_rolled_back_leaves_the_key_free(session_factory):  # R58
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        writer.add(owner)
        await writer.commit()
        owner_id = owner.id
    key = LIFETIME_KEY
    async with session_factory() as first, session_factory() as second:
        await update_policy_setting(first, key=key, value=24, by=owner_id, now=NOW)
        racing = asyncio.create_task(
            update_policy_setting(second, key=key, value=48, by=owner_id, now=LATER)
        )
        await asyncio.sleep(0.3)
        assert not racing.done()
        await first.rollback()
        assert await racing == (None, 48)
        await second.commit()
    async with session_factory() as reader:
        assert await stored(reader) == {key: 48}


async def test_clearing_a_choice_nobody_made_changes_nothing(session, owner):
    old_new = await update_policy_setting(
        session, key=LIFETIME_KEY, value=None, by=owner.id, now=NOW
    )
    assert old_new == (None, None)
    assert await stored(session) == {}


async def test_the_row_says_who_chose_and_when(session, owner, add_person):
    other = await add_person("Sam", "adult")
    await update_policy_setting(session, key=LIFETIME_KEY, value=24, by=owner.id, now=NOW)
    await update_policy_setting(session, key=LIFETIME_KEY, value=48, by=other.id, now=LATER)
    row = (await session.execute(select(PolicySetting))).scalar_one()
    assert (row.updated_by, row.updated_at) == (other.id, LATER)


async def test_a_refused_setting_writes_nothing(session, owner):
    with pytest.raises(InvalidSetting):
        await update_policy_setting(
            session, key="confirmation_style.confirm", value="explicit", by=owner.id, now=NOW
        )
    with pytest.raises(InvalidSetting):
        await update_policy_setting(session, key=LIFETIME_KEY, value=0, by=owner.id, now=NOW)
    assert await stored(session) == {}


async def test_updating_a_setting_writes_no_audit_entry(session, owner):
    # The caller writes the audit (and commits): the store only changes the row.
    before = (await session.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
    await update_policy_setting(session, key=LIFETIME_KEY, value=24, by=owner.id, now=NOW)
    after = (await session.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
    assert after == before


# --- reading them back ---------------------------------------------------------------------


async def test_a_row_the_rules_do_not_accept_is_ignored(session, owner):
    # Someone edited the table by hand (or a style of before is still there). A setting that no
    # longer makes sense must not break every decision, and must not loosen anything: the default
    # applies.
    for key, value in [
        ("confirmation_style.always_confirm", "natural"),
        ("confirmation_max_lifetime_hours", 100000),
        ("rights_writer.seconds", 5),
        ("something.else", "x"),
    ]:
        session.add(PolicySetting(key=key, value=value, updated_by=owner.id, updated_at=NOW))
    await session.flush()
    s = await load_policy_settings(session)
    assert s.owner_values == {}
    assert s.max_lifetime == timedelta(hours=168)
    assert s.writer_seconds == 60


async def test_a_json_null_row_counts_as_no_choice(session, owner):
    await session.execute(
        text(
            "INSERT INTO policy_settings (key, value, updated_by) "
            "VALUES ('confirmation_max_lifetime_hours', 'null'::jsonb, :by)"
        ),
        {"by": owner.id},
    )
    s = await load_policy_settings(session)
    assert s.owner_values == {}
    assert s.max_lifetime == timedelta(hours=168)


# --- the rules on a set of values ----------------------------------------------------------


def test_a_value_the_rules_do_not_accept_falls_back_to_the_default():
    s = PolicySettings({"confirmation_max_lifetime_hours": True, "rights_writer.seconds": "90"})
    assert s.max_lifetime == timedelta(hours=168)
    assert s.writer_seconds == 60


def test_rows_list_every_setting_with_where_its_value_comes_from():
    s = PolicySettings({"confirmation_max_lifetime_hours": 24})
    assert s.rows() == [
        {"key": "confirmation_max_lifetime_hours", "value": 24, "source": "owner"},
        {"key": "rights_writer.seconds", "value": 60, "source": "default"},
    ]


def test_rows_say_the_time_zone_once_the_owner_set_it():
    s = PolicySettings({"household.time_zone": "Europe/Paris"})
    assert s.rows()[2:] == [
        {"key": "household.time_zone", "value": "Europe/Paris", "source": "owner"},
    ]
    assert s.time_zone == ZoneInfo("Europe/Paris")
    # A name the rules do not accept is no choice: no time zone, never a guess.
    assert PolicySettings({"household.time_zone": "Mars/Olympus"}).time_zone is None


async def test_the_owner_sets_and_clears_the_time_zone(session, owner):
    assert (await load_policy_settings(session)).time_zone is None
    old, new = await update_policy_setting(
        session, key="household.time_zone", value="Europe/Paris", by=owner.id, now=NOW
    )
    assert (old, new) == (None, "Europe/Paris")
    assert (await load_policy_settings(session)).time_zone == ZoneInfo("Europe/Paris")
    await update_policy_setting(
        session, key="household.time_zone", value=None, by=owner.id, now=LATER
    )
    assert (await load_policy_settings(session)).time_zone is None
    with pytest.raises(InvalidSetting):
        await update_policy_setting(
            session, key="household.time_zone", value="Nowhere/Town", by=owner.id, now=LATER
        )


def test_the_writer_time_takes_30_to_240_seconds():
    key = WRITER_SECONDS_KEY
    assert key == "rights_writer.seconds"
    for seconds in (30, 60, 240):
        validate_setting(key, seconds)
    # Out of the range, not a whole number, and a JSON true (an int to Python) are all refused.
    for refused in (29, 241, 0, -60, 60.5, True, False, "60", ["60"]):
        with pytest.raises(InvalidSetting):
            validate_setting(key, refused)
    # Back to the default.
    validate_setting(key, None)
    assert PolicySettings({}).writer_seconds == 60
    assert PolicySettings({key: 120}).writer_seconds == 120
    # A value the rules do not accept is no choice: the default applies.
    assert PolicySettings({key: 5}).writer_seconds == 60
    assert PolicySettings({key: "90"}).writer_seconds == 60
    assert {"key": key, "value": 60, "source": "default"} in PolicySettings({}).rows()
    assert {"key": key, "value": 120, "source": "owner"} in PolicySettings({key: 120}).rows()


def test_the_tools_database_holds_a_gigabyte_unless_the_owner_says_otherwise():
    key = TOOLS_BASE_KEY
    assert key == "tools.base_mb"
    for megabytes in (16, 1024, 1_048_576):
        validate_setting(key, megabytes)
    for refused in (15, 1_048_577, 0, -1, 10.5, True, "1024", [1024]):
        with pytest.raises(InvalidSetting):
            validate_setting(key, refused)
    validate_setting(key, None)
    assert PolicySettings({}).tools_base_bytes == 1024**3
    assert PolicySettings({key: 2048}).tools_base_bytes == 2 * 1024**3
    # A value the rules do not accept is no choice: the default applies.
    assert PolicySettings({key: 1}).tools_base_bytes == 1024**3
    # Shown once it was set, never before.
    assert key not in {row["key"] for row in PolicySettings({}).rows()}
    assert {"key": key, "value": 2048, "source": "owner"} in PolicySettings({key: 2048}).rows()


async def test_the_owner_sets_and_clears_the_writer_time(session, owner):
    assert (await load_policy_settings(session)).writer_seconds == 60
    old, new = await update_policy_setting(
        session, key=WRITER_SECONDS_KEY, value=90, by=owner.id, now=NOW
    )
    assert (old, new) == (None, 90)
    assert (await load_policy_settings(session)).writer_seconds == 90
    with pytest.raises(InvalidSetting):
        await update_policy_setting(
            session, key=WRITER_SECONDS_KEY, value=241, by=owner.id, now=LATER
        )
    assert (await load_policy_settings(session)).writer_seconds == 90
    old, new = await update_policy_setting(
        session, key=WRITER_SECONDS_KEY, value=None, by=owner.id, now=LATER
    )
    assert (old, new) == (90, None)
    assert (await load_policy_settings(session)).writer_seconds == 60


def test_the_rules_of_the_keys_have_their_defaults():
    assert (INVITEE_DEFAULT_KEY, UPKEEP_KEY) == ("keys.invitee_default", "keys.upkeep")
    # Nothing said: an invitee uses their inviter's key (``None``: the code's default), and the
    # general key serves to ask someone without a key for one.
    nothing = PolicySettings({})
    assert (nothing.invitee_default, nothing.upkeep_on_general) == (None, True)
    said = PolicySettings({INVITEE_DEFAULT_KEY: "own", UPKEEP_KEY: False})
    assert (said.invitee_default, said.upkeep_on_general) == ("own", False)
    for value in ("inviter", "own", None):
        validate_setting(INVITEE_DEFAULT_KEY, value)
    for value in (True, False, None):
        validate_setting(UPKEEP_KEY, value)
    for key, refused in (
        (INVITEE_DEFAULT_KEY, "anyone"),
        (INVITEE_DEFAULT_KEY, 1),
        (INVITEE_DEFAULT_KEY, True),
        (UPKEEP_KEY, 1),
        (UPKEEP_KEY, 0),
        (UPKEEP_KEY, "yes"),
    ):
        with pytest.raises(InvalidSetting):
            validate_setting(key, refused)
    # A value the rules do not accept is no choice: the default applies.
    odd = PolicySettings({INVITEE_DEFAULT_KEY: "anyone", UPKEEP_KEY: 0})
    assert (odd.invitee_default, odd.upkeep_on_general) == (None, True)
    # They are said with the settings once chosen.
    assert said.rows()[2:] == [
        {"key": INVITEE_DEFAULT_KEY, "value": "own", "source": "owner"},
        {"key": UPKEEP_KEY, "value": False, "source": "owner"},
    ]


async def test_the_rules_of_the_keys_are_stored_and_read_back(session, owner):
    await update_policy_setting(session, key=UPKEEP_KEY, value=False, by=owner.id, now=NOW)
    await update_policy_setting(session, key=INVITEE_DEFAULT_KEY, value="own", by=owner.id, now=NOW)
    read = await load_policy_settings(session)
    assert (read.upkeep_on_general, read.invitee_default) == (False, "own")


def test_rows_without_any_choice_are_the_defaults():
    assert PolicySettings({}).rows() == [
        {"key": "confirmation_max_lifetime_hours", "value": 168, "source": "default"},
        {"key": "rights_writer.seconds", "value": 60, "source": "default"},
    ]


# --- what a setting may be -----------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("confirmation_style.confirm", "natural"),
        ("confirmation_style.action.household.invite", "natural"),
        ("confirmation_style.action.chain.run", "explicit"),
        ("confirmation_max_lifetime_hours", 0),
        ("confirmation_max_lifetime_hours", 721),
        ("confirmation_max_lifetime_hours", "48"),
        ("something.else", "x"),
        # A JSON true is an int to Python: it is not a number of hours.
        ("confirmation_max_lifetime_hours", True),
        ("confirmation_max_lifetime_hours", False),
        ("confirmation_max_lifetime_hours", 1.5),
        ("confirmation_max_lifetime_hours", -3),
        ("confirmation_max_lifetime_hours", "natural"),
        ("", "natural"),
        ("confirmation_max_lifetime_hours ", 24),
        # Clearing is only for a setting that exists (R58).
        ("something.else", None),
        ("confirmation_style.confirm", None),
        # A time zone is a name zoneinfo lists, exactly.
        ("household.time_zone", "Mars/Olympus"),
        ("household.time_zone", "Europe"),
        ("household.time_zone", "europe/paris"),
        ("household.time_zone", " Europe/Paris"),
        ("household.time_zone", "/etc/localtime"),
        ("household.time_zone", "../../etc/passwd"),
        ("household.time_zone", ""),
        ("household.time_zone", 1),
        ("household.time_zone", True),
        ("household.time_zone", ["Europe/Paris"]),
        ("household.timezone", "Europe/Paris"),
    ],
)
def test_invalid_settings_are_rejected(key, value):
    with pytest.raises(InvalidSetting):
        validate_setting(key, value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("confirmation_max_lifetime_hours", 1),
        ("confirmation_max_lifetime_hours", 168),
        ("confirmation_max_lifetime_hours", 720),
        ("rights_writer.seconds", 30),
        ("rights_writer.seconds", 60),
        ("rights_writer.seconds", 240),
        ("household.time_zone", "Europe/Paris"),
        ("household.time_zone", "America/New_York"),
        ("household.time_zone", "UTC"),
    ],
)
def test_valid_settings_are_accepted(key, value):
    validate_setting(key, value)


@pytest.mark.parametrize("key", [*SETTING_DEFAULTS, "household.time_zone"])
def test_none_clears_any_setting(key):
    validate_setting(key, None)


def test_the_rule_of_the_general_key_for_invitees_is_follow_or_never():
    assert INVITEE_GENERAL_KEY == "keys.invitee_general"
    assert PolicySettings({}).invitee_general is None
    for value in ("follow", "never"):
        assert PolicySettings({INVITEE_GENERAL_KEY: value}).invitee_general == value
        validate_setting(INVITEE_GENERAL_KEY, value)
    validate_setting(INVITEE_GENERAL_KEY, None)
    # A value the rule does not accept is no choice.
    assert PolicySettings({INVITEE_GENERAL_KEY: "sometimes"}).invitee_general is None
    for refused in ("sometimes", 1, True):
        with pytest.raises(InvalidSetting):
            validate_setting(INVITEE_GENERAL_KEY, refused)


def test_the_language_of_the_house_is_english_until_said_otherwise():
    assert HOUSE_LANGUAGE == "house.language"
    assert PolicySettings({}).house_language == "en"
    assert PolicySettings({HOUSE_LANGUAGE: "fr"}).house_language == "fr"
    assert PolicySettings({HOUSE_LANGUAGE: 3}).house_language == "en"
    validate_setting(HOUSE_LANGUAGE, None)
    # A language tag, in its usual case (Ruling R35): it reaches the writer and the gatekeeper.
    for accepted in ("fr", "pt-BR", "zh-Hant-TW", "es-419"):
        validate_setting(HOUSE_LANGUAGE, accepted)
    for refused in ("", "  ", 3, True, "x" * 100, "French", "FR", "pt-br", "en_US", " fr"):
        with pytest.raises(InvalidSetting):
            validate_setting(HOUSE_LANGUAGE, refused)
    # A name kept before is no choice: the default applies.
    assert PolicySettings({HOUSE_LANGUAGE: "French"}).house_language == "en"


async def test_the_language_of_the_house_is_stored_and_read_back(session, owner):
    await update_policy_setting(session, key=HOUSE_LANGUAGE, value="fr", by=owner.id, now=NOW)
    assert (await load_policy_settings(session)).house_language == "fr"


def test_hear_outside_is_no_by_default_and_the_owners_to_change():
    key = HEAR_OUTSIDE_KEY
    assert key == "speech.hear_outside"
    for accepted in ("no", "yes", None):
        validate_setting(key, accepted)
    for refused in ("maybe", "Yes", True, 1, ["yes"]):
        with pytest.raises(InvalidSetting):
            validate_setting(key, refused)
    assert PolicySettings({}).hear_outside is False
    assert PolicySettings({key: "yes"}).hear_outside is True
    assert PolicySettings({key: "no"}).hear_outside is False
    # A value the rules do not accept is no choice: the default applies.
    assert PolicySettings({key: "sure"}).hear_outside is False
    # Shown once it was set, never before.
    assert key not in {row["key"] for row in PolicySettings({}).rows()}
    assert {"key": key, "value": "yes", "source": "owner"} in PolicySettings({key: "yes"}).rows()
    # Not one of the rules of the keys: the owner's policy_update changes it.
    assert key not in KEYS_SETTINGS
