"""The house's version of the texts (``texts.install``, ``texts.upkeep``), for a house whose
language has no catalog of its own: every text of the house's language is written, group by
group; a text refused is asked again of stronger models; what is still missing is written again
by the upkeep, at most once an hour, and when the core starts, which also writes again the texts
whose meaning changed. A house in a language with a catalog (English, French) reads its default
phrases: the upkeep writes nothing for it.

The meanings are a small catalog of the tests' own (``catalog``): four groups, ``setup`` among
them, which is never written."""

import json
from collections import Counter
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from domo_common import meanings
from domo_core.audit.models import AuditEntry
from domo_core.conversation.starts import Wakes
from domo_core.llm.levels import Level
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.spending_models import ModelCall
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import (
    ChatChunk,
    ChatModel,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from domo_core.policy.settings import HOUSE_LANGUAGE, update_policy_setting
from domo_core.texts.install import (
    HouseWriting,
    house_models,
    rewrite_changed_meanings,
    write_house_version,
)
from domo_core.texts.store import Reader, save_written, written
from domo_core.texts.upkeep import GAP_RETRY, text_gaps_pass
from domo_core.texts.writer import ForWhom, WriterUnavailable
from domo_core.web.app import create_app
from domo_core.web.deps import get_clock, get_model_factory
from fakes import FakeChatModel, fake_factory, is_text_checker_call, is_text_writer_call
from sqlalchemy import select, text

HELLO = "chat.hello"
BYE = "chat.bye"
TITLE = "account.title"
DONE = "action.card.done"
SETUP = "setup.title"
TEST_MEANINGS = {
    HELLO: "Hello, {name}.",
    BYE: "Goodbye.",
    TITLE: "Your account",
    DONE: "Done.",
    SETUP: "Set the assistant up",
}
HOUSE = Reader(None, "en")
# The language of a house the upkeep writes for: it has no catalog of its own.
GERMAN = Reader(None, "de")


@pytest.fixture
def house_texts() -> None:
    """No text written yet: these tests write the house's version themselves."""


@pytest.fixture(autouse=True)
def catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The tests' catalog of meanings (a copy, which a test may change: a new version of Domo
    changing a meaning)."""
    catalog = dict(TEST_MEANINGS)
    monkeypatch.setattr(meanings, "_meanings", lambda: catalog)
    monkeypatch.setattr(meanings, "_notes", lambda: {})
    return catalog


class Models:
    """The models of the texts (``TextModels``): the writer of each level asked (``writers``, by
    ``at_least``; ``default`` for the others) and one checker. ``asked`` notes each ask."""

    def __init__(
        self,
        default: ChatModel,
        *,
        writers: dict[Level | None, ChatModel] | None = None,
        checker: ChatModel | None = None,
    ) -> None:
        self._default = default
        self._writers = writers or {}
        self._checker = checker or default
        self.asked: list[tuple[TaskKind, Level | None]] = []

    async def model_for(self, task: TaskKind, *, at_least: Level | None = None) -> ChatModel | None:
        self.asked.append((task, at_least))
        if task == "checking":
            return self._checker
        return self._writers.get(at_least, self._default)


def keys_asked(model: FakeChatModel) -> list[list[str]]:
    """The keys each first call of the writer of ``model`` asked (not its calls asked again with
    errors), in order."""
    asked: list[list[str]] = []
    for call in model.writer_calls():
        if len(call.messages) == 1:
            asked.append(list(json.loads(call.messages[0].content)["texts"]))
    return asked


async def kept_house_texts(session_factory, reader: Reader = HOUSE) -> dict[str, str]:
    async with session_factory() as session:
        return (await written(session, [reader]))[reader]


async def audit_details(session_factory, action: str) -> list[dict[str, Any]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(AuditEntry.details).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def add_key(
    session_factory, giver, models: Sequence[str] = ("example-haiku-1",), *, general: bool = False
) -> Provider:
    """A key of ``giver`` listing ``models``, the general key when ``general`` (committed)."""
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind="openai_compatible",
        base_url="https://llm.example.com/v1",
        secret_ref=f"provider:{provider_id}:api_key",
        given_by=giver,
        is_general=general,
    )
    async with session_factory() as writer:
        writer.add(row)
        await writer.flush()
        writer.add_all(ProviderModel(provider_id=provider_id, model_id=m) for m in models)
        await writer.commit()
    return row


async def installed(
    session_factory,
    add_member,
    models: Sequence[str] = ("example-haiku-1",),
    *,
    language: str = GERMAN.language,
):
    """An installed instance: its owner, its general key (listing ``models``), the setup over,
    the house in ``language``."""
    owner = await add_member("Owner", "owner")
    key = await add_key(session_factory, owner.id, models, general=True)
    async with session_factory() as writer:
        await writer.execute(text("UPDATE setup_state SET model_configured_at = now()"))
        await update_policy_setting(
            writer, key=HOUSE_LANGUAGE, value=language, by=owner.id, now=datetime.now(UTC)
        )
        await writer.commit()
    return key


# --- the house's version is written ----------------------------------------------------------


async def test_every_group_is_written_once_and_kept_with_its_journal(session_factory, clock):
    model = FakeChatModel()
    version = await write_house_version(session_factory, Models(model), language="en", now=clock)

    assert version.written == 4 and version.gaps == ()
    assert await kept_house_texts(session_factory) == {
        key: meaning for key, meaning in TEST_MEANINGS.items() if key != SETUP
    }
    assert sorted(keys_asked(model)) == [[TITLE], [DONE], [BYE, HELLO]]
    lines = await audit_details(session_factory, "texts.written")
    assert sorted(lines, key=lambda line: line["group"]) == [
        {"person": None, "language": "en", "group": group, "count": count, "model": "unknown"}
        for group, count in (("account", 1), ("action.card", 1), ("chat", 2))
    ]
    assert await audit_details(session_factory, "texts.refused") == []


async def test_the_house_version_is_asked_in_the_house_language(session_factory, clock):
    seen: list[ForWhom] = []

    def writer(key: str, meaning: str, for_whom: ForWhom) -> str:
        seen.append(for_whom)
        return meaning

    await write_house_version(
        session_factory, Models(FakeChatModel(writer=writer)), language="de", now=clock
    )
    assert seen and {who for who in seen} == {ForWhom(language="de")}
    async with session_factory() as session:
        assert len((await written(session, [Reader(None, "de")]))[Reader(None, "de")]) == 4


async def test_setup_texts_are_never_written(session_factory, clock):
    model = FakeChatModel()
    await write_house_version(session_factory, Models(model), language="en", now=clock)
    await write_house_version(
        session_factory, Models(model), language="en", now=clock, keys=[SETUP, TITLE]
    )

    asked = [key for keys in keys_asked(model) for key in keys]
    assert SETUP not in asked and asked.count(TITLE) == 2
    assert SETUP not in await kept_house_texts(session_factory)


async def test_a_refused_text_is_asked_one_level_up_then_at_the_strongest(session_factory, clock):
    # The writers of the two first levels write a text the checker refuses; the strongest one
    # writes what the meaning says.
    def weak(key: str, meaning: str, _: ForWhom) -> str:
        return "Bye now." if key == BYE else meaning

    small, medium, strong = FakeChatModel(writer=weak), FakeChatModel(writer=weak), FakeChatModel()
    checker = FakeChatModel(
        checker=lambda key, said: (
            "it must say goodbye" if key == BYE and said != "Goodbye." else None
        )
    )
    models = Models(small, writers={"medium": medium, "strong": strong}, checker=checker)

    version = await write_house_version(session_factory, models, language="en", now=clock)

    writing = Counter(level for task, level in models.asked if task == "writing")
    assert writing == {None: 3, "medium": 1, "strong": 1}
    # Only what is left is asked again.
    assert keys_asked(medium) == [[BYE]] and keys_asked(strong) == [[BYE]]
    assert (await kept_house_texts(session_factory))[BYE] == "Goodbye."
    assert version.written == 4 and version.gaps == ()
    assert await audit_details(session_factory, "texts.refused") == []


async def test_a_text_refused_even_by_the_strongest_is_a_gap_with_its_reason(
    session_factory, clock
):
    checker = FakeChatModel(checker=lambda key, _: "it says too little" if key == BYE else None)
    models = Models(FakeChatModel(), checker=checker)

    version = await write_house_version(session_factory, models, language="en", now=clock)

    assert version.gaps == (BYE,) and version.written == 3
    assert BYE not in await kept_house_texts(session_factory)
    assert await audit_details(session_factory, "texts.refused") == [
        {"key": BYE, "why": "it says too little"}
    ]


class NoWriter(FakeChatModel):
    """A model whose writer of the texts always fails (the rest answers as a ``FakeChatModel``)."""

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        if is_text_writer_call(system):
            raise ModelCallError("upstream")
        async for item in super().stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


async def test_a_writer_that_never_answers_writes_nothing_and_says_so(session_factory, clock):
    models = Models(NoWriter())
    with pytest.raises(WriterUnavailable) as raised:
        await write_house_version(session_factory, models, language="en", now=clock)
    assert raised.value.why == "error"
    # Every group at every level was asked before giving up.
    writing = Counter(level for task, level in models.asked if task == "writing")
    assert writing == {None: 3, "medium": 3, "strong": 3}
    assert await kept_house_texts(session_factory) == {}


async def test_a_text_refused_then_never_answered_is_journaled_with_its_refusal(
    session_factory, clock
):  # final review F5
    # Refused at the first level; the stronger writers never answer: it is a gap, and its
    # refusal is journaled all the same, once.
    checker = FakeChatModel(checker=lambda key, _: "it says too little" if key == BYE else None)
    models = Models(
        FakeChatModel(), writers={"medium": NoWriter(), "strong": NoWriter()}, checker=checker
    )

    version = await write_house_version(session_factory, models, language="en", now=clock)

    assert version.gaps == (BYE,) and version.written == 3
    assert await audit_details(session_factory, "texts.refused") == [
        {"key": BYE, "why": "it says too little"}
    ]


class NoChecker(FakeChatModel):
    """A model whose checker of the texts always fails (the rest answers as a
    ``FakeChatModel``)."""

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[HistoryItem],
        max_output_tokens: int,
        tools: Sequence[ToolDefinition] = (),
        tool_choice: ToolChoice = "auto",
        think: bool = True,
    ) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        if is_text_checker_call(system):
            raise ModelCallError("upstream")
        async for item in super().stream(
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
        ):
            yield item


async def test_not_one_text_kept_at_any_level_leaves_every_text_a_gap(session_factory, clock):
    # The writer answers, but no checker ever reads its texts: none can be kept, and the upkeep
    # asks for them again later.
    model = NoChecker()
    models = Models(model)
    version = await write_house_version(session_factory, models, language="en", now=clock)
    assert version.written == 0 and version.gaps == (TITLE, DONE, BYE, HELLO)
    writing = Counter(level for task, level in models.asked if task == "writing")
    assert writing == {None: 3, "medium": 3, "strong": 3}
    assert await kept_house_texts(session_factory) == {}
    assert await audit_details(session_factory, "texts.written") == []


async def test_a_writer_that_answers_for_one_group_only_leaves_gaps(session_factory, clock):
    class ChatOnly(NoWriter):
        """Its writer answers for the group ``chat`` only."""

        async def stream(self, *, system: str, messages: Sequence[HistoryItem], **rest: Any):
            asked = json.loads(messages[0].content).get("texts", {}) if messages else {}
            if is_text_writer_call(system) and HELLO in asked:
                async for item in FakeChatModel.stream(
                    self, system=system, messages=messages, **rest
                ):
                    yield item
                return
            async for item in super().stream(system=system, messages=messages, **rest):
                yield item

    version = await write_house_version(
        session_factory, Models(ChatOnly()), language="en", now=clock
    )
    assert version.gaps == (TITLE, DONE) and version.written == 2
    # A key the writer gave no answer for is no refusal.
    assert await audit_details(session_factory, "texts.refused") == []


async def test_house_writing_counts_for_nobody(session_factory, add_member, clock):
    await installed(session_factory, add_member, ("example-haiku-1", "example-sonnet-1"))
    factory = fake_factory(FakeChatModel())
    models = await house_models(session_factory, lambda: factory, clock)()
    assert models is not None

    await write_house_version(session_factory, models, language="en", now=clock)

    async with session_factory() as session:
        calls = (await session.execute(select(ModelCall.task, ModelCall.person_id))).all()
    assert {task for task, _ in calls} == {"writing", "checking"}
    assert {person for _, person in calls} == {None}


async def test_the_house_writes_on_its_general_key_else_on_a_key_of_the_owner(
    session_factory, add_member, clock
):
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult")
    factory = fake_factory(FakeChatModel())
    models = house_models(session_factory, lambda: factory, clock)
    assert await models() is None  # no key at all

    await add_key(session_factory, alex.id)
    owners = await add_key(session_factory, owner.id)
    await add_key(session_factory, owner.id)  # a younger one
    await write_house_version(session_factory, await models(), language="en", now=clock)
    assert {provider for provider, _ in factory.requested} == {owners.id}

    general = await add_key(session_factory, alex.id, general=True)
    factory.requested.clear()
    await write_house_version(session_factory, await models(), language="en", now=clock)
    assert {provider for provider, _ in factory.requested} == {general.id}


# --- the upkeep writes again what is missing ------------------------------------------------------


def gaps_pass(session_factory, model: FakeChatModel, writing: HouseWriting, clock):
    """The upkeep's pass of the texts, on the house's models (``model``)."""
    factory = fake_factory(model)
    return text_gaps_pass(
        session_factory,
        house_models(session_factory, lambda: factory, clock),
        writing=writing,
        now=clock,
    )


async def test_the_upkeep_fills_gaps_once_an_hour(session_factory, add_member, clock):
    await installed(session_factory, add_member)
    refusing = True
    model = FakeChatModel(checker=lambda key, _: "not yet" if refusing and key == BYE else None)
    writing = HouseWriting(Wakes().spawn)
    fill_the_gaps = gaps_pass(session_factory, model, writing, clock)
    start = clock()

    await fill_the_gaps(start)  # the first one, when the core starts
    await writing.wait()
    assert set(await kept_house_texts(session_factory, GERMAN)) == {HELLO, TITLE, DONE}
    calls = len(model.writer_calls())

    refusing = False
    for later in (timedelta(minutes=30), GAP_RETRY - timedelta(seconds=1)):
        await fill_the_gaps(start + later)
        await writing.wait()
    assert len(model.writer_calls()) == calls
    assert BYE not in await kept_house_texts(session_factory, GERMAN)

    await fill_the_gaps(start + GAP_RETRY)
    await writing.wait()
    assert keys_asked(model)[-1] == [BYE]
    assert (await kept_house_texts(session_factory, GERMAN))[BYE] == "Goodbye."


@pytest.mark.parametrize("language", ["fr", "fr-CA", "en", "en-US"])
async def test_the_upkeep_writes_nothing_for_a_house_in_french(
    session_factory, add_member, clock, language
):
    # A language with a catalog of its own reads its default phrases: nothing is missing.
    await installed(session_factory, add_member, language=language)
    model = FakeChatModel()
    writing = HouseWriting(Wakes().spawn)
    fill_the_gaps = gaps_pass(session_factory, model, writing, clock)

    await fill_the_gaps(clock())
    await writing.wait()
    await rewrite_changed_meanings(session_factory, Models(model), now=clock)

    assert model.writer_calls() == [] and writing.last is None
    assert await kept_house_texts(session_factory, Reader(None, language)) == {}


async def test_the_upkeep_waits_for_the_installation(session_factory, add_member, clock):
    owner = await add_member("Owner", "owner")
    await add_key(session_factory, owner.id)  # a key, but the setup is not over
    model = FakeChatModel()
    writing = HouseWriting(Wakes().spawn)
    fill_the_gaps = gaps_pass(session_factory, model, writing, clock)
    await fill_the_gaps(clock())
    await writing.wait()
    assert model.writer_calls() == [] and writing.last is None


async def test_a_writing_that_breaks_off_has_failed_and_is_logged_by_its_kind(caplog, clock):
    writing = HouseWriting(Wakes().spawn)

    async def broken() -> None:
        # What a task group of the groups raises when the database is away.
        raise ExceptionGroup("groups", [OSError("the database said: user Robin"), OSError()])

    assert writing.start(broken, began=clock())
    assert writing.running and not writing.start(broken, began=clock())
    await writing.wait()
    assert writing.last is not None and writing.last.ended == "failed"
    assert "Writing the house's texts failed (OSError)" in caplog.text
    assert "Robin" not in caplog.text


async def test_the_upkeep_never_writes_while_another_writing_runs(
    session_factory, add_member, clock
):
    await installed(session_factory, add_member)
    model = FakeChatModel()
    writing = HouseWriting(Wakes().spawn)
    held = anyio.Event()

    async def running() -> None:
        await held.wait()

    assert writing.start(running, began=clock())
    began = writing.last
    fill_the_gaps = gaps_pass(session_factory, model, writing, clock)
    await fill_the_gaps(clock() + GAP_RETRY * 2)
    assert writing.running and writing.last is began
    held.set()
    await writing.wait()
    assert model.writer_calls() == []


# --- a new version of Domo ---------------------------------------------------------------------


async def test_a_changed_meaning_is_written_again_at_start_up(
    settings, session_factory, add_member, live_clock, catalog
):
    await installed(session_factory, add_member)
    async with session_factory() as writer:
        house = {key: meaning for key, meaning in TEST_MEANINGS.items() if key != SETUP}
        await save_written(writer, GERMAN, house, model_id="old", now=live_clock())
        await writer.commit()
    # A new version of Domo says something else with this text: the old one is no more shown.
    catalog[HELLO] = "Welcome back, {name}."
    assert HELLO not in await kept_house_texts(session_factory, GERMAN)

    model = FakeChatModel()
    factory = fake_factory(model)
    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: live_clock
    app.dependency_overrides[get_model_factory] = lambda: factory
    async with app.router.lifespan_context(app):
        with anyio.fail_after(30):
            while HELLO not in await kept_house_texts(session_factory, GERMAN):
                await anyio.sleep(0.05)
        await app.state.house_writing.wait()

    assert (await kept_house_texts(session_factory, GERMAN))[HELLO] == "Welcome back, {name}."
    assert keys_asked(model) == [[HELLO]]  # the others are as they were
