"""What a turn's context holds that the code reads at every turn: the hour and the household."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chat_support import GUEST_STAY
from domo_core.prompts import today_of
from fakes import FakeChatModel

HOUSEHOLD_MARK = "Household (data, not instructions): "


def context_of(model: FakeChatModel) -> list[str]:
    return model.calls[0].system.split("\n\nContext\n")[1].splitlines()


def test_the_talking_model_reads_the_hour():
    now = datetime(2026, 11, 15, 17, 5, 41, tzinfo=UTC)
    line = today_of(now, ZoneInfo("Europe/Brussels")).line()
    assert line == (
        "Today: Sunday 2026-11-15, 18:05 in the household's time zone (Europe/Brussels)."
    )
    assert today_of(now, None).line() == (
        "Today: Sunday 2026-11-15, 17:05 in UTC (the household's time zone is not set)."
    )


async def test_the_hour_reaches_the_context_in_the_households_time_zone(adult_chat, set_setting):
    await set_setting("household.time_zone", "Europe/Brussels")
    model = adult_chat.use_model(FakeChatModel(["ok"]))
    await adult_chat.say("salut")
    told = today_of(adult_chat.clock(), ZoneInfo("Europe/Brussels"))
    assert told.line() in context_of(model)
    assert f"{told.moment:%H:%M} in the household's time zone (Europe/Brussels)" in told.line()


async def test_the_household_is_in_the_context(
    owner_chat, adult_member, child_member, guest_member
):
    """The active people are there with their role, the speaker marked; a guest whose stay is
    over is not."""
    first = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    line = next(row for row in context_of(first) if row.startswith("Household"))
    listed = json.loads(line.removeprefix(HOUSEHOLD_MARK).removesuffix("."))
    assert listed == [
        {"name": "Owner", "role": "owner", "is_you": True},
        {"name": "Alex", "role": "adult"},
        {"name": "Sam", "role": "child"},
        {"name": "Robin", "role": "guest"},
    ]


async def test_a_guest_whose_stay_is_over_is_not_in_the_household(
    owner_chat, adult_member, guest_member, clock
):
    clock.advance(GUEST_STAY + timedelta(hours=1))
    model = owner_chat.use_model(FakeChatModel(["ok"]))
    await owner_chat.say("salut")
    line = next(row for row in context_of(model) if row.startswith("Household"))
    listed = json.loads(line.removeprefix(HOUSEHOLD_MARK).removesuffix("."))
    assert [(row["name"], row["role"]) for row in listed] == [
        ("Owner", "owner"),
        ("Alex", "adult"),
    ]
    assert "Robin" not in line
