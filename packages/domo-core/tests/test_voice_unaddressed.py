"""What a microphone hears that is not said to the assistant (spec 6.6, Task 25): it enters no
discussion, waits in memory only, and is given to him in one batch once the room is quiet; he
keeps what he judges useful with the tools of his memory, the rest is thrown away."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice import session as session_module
from domo_core.channels.voice import unaddressed as unaddressed_module
from domo_core.channels.voice.unaddressed import MAX_AGE, MAX_LINES, HeardLine, Unaddressed
from domo_core.llm.types import ToolCall
from domo_core.memory.models import Memory
from fakes import FakeChatModel
from sqlalchemy import select
from sqlalchemy import text as sql_text
from voice_session_support import House, a_household_that_hears_at_home, until

MIC = "desk-1"
OVERHEARD = "the spare key is under the blue flowerpot"


@pytest.fixture
async def owner(add_member: Callable[..., Awaitable[Person]]) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
def quiet_soon(monkeypatch) -> None:
    monkeypatch.setattr(unaddressed_module, "QUIET_FOR", timedelta(seconds=0.3))


@pytest.fixture
async def house(session_factory, settings, live_clock, owner):
    await a_household_that_hears_at_home(session_factory, owner)
    made: list[House] = []

    def make(model: FakeChatModel | None = None) -> House:
        found = House(
            session_factory, settings, live_clock, owner, model or FakeChatModel(["Of course."])
        )
        found.attention.answer = "not_mine"
        made.append(found)
        return found

    yield make
    for each in made:
        await each.close()


async def everything_written(session_factory) -> str:
    """Every message, every line of the journal, and every memory, as text."""
    async with session_factory() as reader:
        rows = (await reader.execute(sql_text("SELECT row_to_json(m)::text FROM messages m"))).all()
        audit = (
            await reader.execute(sql_text("SELECT row_to_json(a)::text FROM audit_log a"))
        ).all()
        memories = (await reader.execute(select(Memory.content))).scalars().all()
    return "\n".join([*(r[0] for r in rows), *(a[0] for a in audit), *memories])


def sorted_lines(h: House) -> list[dict[str, Any]]:
    """What each sorting was given: the lines heard, as data."""
    found: list[dict[str, Any]] = []
    for call in h.sorting.calls:
        content = str(getattr(call.messages[0], "content", ""))
        heard = json.loads(content[content.index("{") :])
        assert heard["microphone"] == MIC
        found.extend(heard["lines"])
    return found


async def test_what_is_not_for_him_enters_no_discussion(house):
    h = house()
    page = h.page(MIC)
    await page.hears("Did you feed the cat?")
    assert await h.messages() == []
    assert h.model.calls == []
    assert h.service.said == []
    assert page.of("not_taken") == []
    assert len(page.session._unaddressed) == 1  # noqa: SLF001 - what waits, in memory only


@pytest.fixture
def quiet_on_cue(monkeypatch) -> asyncio.Event:
    """The room turns quiet when the test says so (``set``), never on a clock: the batch waits
    for the cue, then goes at once (``QUIET_FOR`` zero)."""
    cue = asyncio.Event()
    monkeypatch.setattr(unaddressed_module, "QUIET_FOR", timedelta(0))
    real = session_module.VoiceSession._when_quiet  # noqa: SLF001

    async def when_quiet(self: session_module.VoiceSession) -> None:
        await cue.wait()
        await real(self)

    monkeypatch.setattr(session_module.VoiceSession, "_when_quiet", when_quiet)
    return cue


async def test_the_batch_is_given_when_the_room_is_quiet_and_then_dropped(house, quiet_on_cue):
    h = house()
    page = h.page(MIC)
    await page.hears("Did you feed the cat?")
    await page.hears("Not yet, after dinner.")
    # Nothing is given while the room is not quiet.
    assert h.sorting.calls == []
    quiet_on_cue.set()
    await until(lambda: bool(h.sorting.calls))
    await h.turns_over()
    # One batch, both lines, who probably said each, with the tools of his memory and nothing else.
    [call] = h.sorting.calls
    assert sorted_lines(h) == [
        {"who": "Alex", "text": "Did you feed the cat?", "sure": True},
        {"who": "Alex", "text": "Not yet, after dinner.", "sure": True},
    ]
    # Look up and keep; Alex surely heard, one of Alex's own memories may be corrected (case 12):
    # overheard words never remove or show what he knows.
    assert {tool.name for tool in call.tools} == {"memory_keep", "memory_search", "memory_change"}
    # Then thrown away: nothing waits any more, and nothing is given twice.
    assert len(page.session._unaddressed) == 0  # noqa: SLF001
    await page.hears("Is it raining?")
    await until(lambda: len(h.sorting.calls) == 2)
    assert sorted_lines(h)[-1] == {"who": "Alex", "text": "Is it raining?", "sure": True}
    assert len(sorted_lines(h)) == 3


async def test_without_a_sorting_model_everything_is_dropped(house, quiet_soon, monkeypatch):
    found_route = unaddressed_module.heard_route

    async def no_sorting_model(deps: Any, person: Any, task: Any) -> Any:
        return None if task == "heard_sorting" else await found_route(deps, person, task)

    monkeypatch.setattr(unaddressed_module, "heard_route", no_sorting_model)
    h = house()
    page = h.page(MIC)
    await page.hears("Did you feed the cat?")
    await until(lambda: len(page.session._unaddressed) == 0)  # noqa: SLF001
    await h.turns_over()
    assert h.sorting.calls == []
    assert await h.messages() == []


async def test_the_lines_never_reach_a_log_or_the_disk(house, quiet_soon, session_factory, caplog):
    caplog.set_level(logging.DEBUG)
    h = house()
    page = h.page(MIC)
    await page.hears(OVERHEARD)
    await until(lambda: bool(h.sorting.calls))
    await h.turns_over()
    # The small model at home was given it, and nothing kept it: no message, no journal, no
    # memory, no log line.
    assert sorted_lines(h) == [{"who": "Alex", "text": OVERHEARD, "sure": True}]
    assert "flowerpot" not in await everything_written(session_factory)
    assert "flowerpot" not in caplog.text
    assert "flowerpot" not in repr(page.session._unaddressed)  # noqa: SLF001
    assert "flowerpot" not in repr(HeardLine("Alex", OVERHEARD, datetime.now(UTC)))


async def test_what_he_keeps_of_it_is_someone_elses_words(house, quiet_soon, session_factory):
    keep = {
        "content": "Overheard at desk-1: Alex said the plumber comes on Friday.",
        "subject": "home",
        "kind": "fact",
        "private": True,
        "core": False,
    }
    h = house()
    h.sorts_with(
        FakeChatModel(tool_script=[ToolCall("k1", "memory_keep", json.dumps(keep)), "done"])
    )
    page = h.page(MIC)
    await page.hears("The plumber comes on Friday.")
    await until(lambda: bool(h.sorting.calls))
    await h.turns_over()
    async with session_factory() as reader:
        [memory] = (await reader.execute(select(Memory))).scalars().all()
    assert memory.origin == "others_words"
    assert memory.content == keep["content"]


def test_the_buffer_is_bounded():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    kept = Unaddressed()
    for n in range(MAX_LINES + 5):
        kept.add(HeardLine(None, f"line {n}", now))
    assert len(kept) == MAX_LINES
    taken = kept.take(now)
    assert taken[0].text == "line 5"  # the oldest went first
    assert len(kept) == 0
    kept.add(HeardLine(None, "old", now - MAX_AGE - timedelta(seconds=1)))
    kept.add(HeardLine(None, "new", now))
    # The old one went as the new one came, not only when the batch is taken.
    assert len(kept) == 1
    assert [line.text for line in kept.take(now)] == ["new"]


# --- a sure voice of the page's person may correct their own memory (final fix wave, case 12) ---


async def test_a_sure_voice_overheard_corrects_its_own_memory_and_nobody_elses(
    house, quiet_soon, session_factory, owner, add_member
):
    dana = await add_member("Dana", "adult")
    async with session_factory() as writer:
        mine = Memory(
            said_by_id=owner.id,
            subject="dentist",
            content="The dentist is on Thursday.",
            kind="fact",
            origin="own_words",
        )
        hers = Memory(
            said_by_id=dana.id,
            subject="car",
            content="The car is blue.",
            kind="fact",
            origin="own_words",
        )
        writer.add_all([mine, hers])
        await writer.commit()
        mine_id, hers_id = str(mine.id), str(hers.id)

    def change(call_id: str, memory_id: str, content: str) -> ToolCall:
        return ToolCall(call_id, "memory_change", json.dumps({"id": memory_id, "content": content}))

    h = house()
    h.sorts_with(
        FakeChatModel(
            tool_script=[
                change("c1", mine_id, "The dentist moved to Friday."),
                change("c2", hers_id, "The car is red."),
                "done",
            ]
        )
    )
    page = h.page(MIC)
    await page.hears("The dentist moved to Friday.")
    await until(lambda: bool(h.sorting.calls))
    await h.turns_over()
    async with session_factory() as reader:
        found = {m.subject: m.content for m in (await reader.execute(select(Memory))).scalars()}
    assert found == {"dentist": "The dentist moved to Friday.", "car": "The car is blue."}


async def test_a_sure_voice_overheard_changes_no_unowned_memory_nor_a_mark(
    house, quiet_soon, session_factory, owner
):
    # Final fix wave B, I-1: only a memory the overheard person said, and only its words.
    async with session_factory() as writer:
        his = Memory(
            by_domo=True,
            subject="house",
            content="The alarm code is set.",
            kind="fact",
            origin="deduced",
        )
        secret = Memory(
            said_by_id=owner.id,
            subject="gift",
            content="The gift is in the attic.",
            kind="fact",
            origin="own_words",
            kept_private=True,
        )
        writer.add_all([his, secret])
        await writer.commit()
        his_id, secret_id = str(his.id), str(secret.id)

    def change(call_id: str, arguments: dict[str, Any]) -> ToolCall:
        return ToolCall(call_id, "memory_change", json.dumps(arguments))

    h = house()
    h.sorts_with(
        FakeChatModel(
            tool_script=[
                change("c1", {"id": his_id, "content": "The alarm code changed."}),
                change("c2", {"id": secret_id, "content": "The gift is gone.", "private": False}),
                change("c3", {"id": secret_id, "content": "The gift moved.", "core": True}),
                "done",
            ]
        )
    )
    page = h.page(MIC)
    await page.hears("The gift moved, the alarm code changed.")
    await until(lambda: bool(h.sorting.calls))
    await h.turns_over()
    async with session_factory() as reader:
        found = {
            m.subject: (m.content, m.kept_private, m.core)
            for m in (await reader.execute(select(Memory))).scalars()
        }
    assert found == {
        "house": ("The alarm code is set.", False, False),
        "gift": ("The gift is in the attic.", True, False),
    }


async def test_an_unsure_voice_overheard_changes_nothing(house, quiet_soon, owner, monkeypatch):
    from domo_core.channels.voice import session as session_module
    from domo_core.conversation.speaker import Clue, Speaker

    def unsure(**kwargs: Any) -> Speaker:
        now = kwargs["now"]
        return Speaker(
            owner.id,
            "voice",
            clues=(
                Clue("session", "for", "strong", "code", now),
                Clue("voice", "against", "weak", "code", now, degree=0.45),
            ),
            microphone=MIC,
        )

    monkeypatch.setattr(session_module, "voice_speaker", unsure)
    h = house()
    page = h.page(MIC)
    await page.hears("The dentist moved to Friday.")
    await until(lambda: bool(h.sorting.calls))
    await h.turns_over()
    [call] = h.sorting.calls
    assert {tool.name for tool in call.tools} == {"memory_keep", "memory_search"}
