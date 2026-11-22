"""The writer and the checker of the texts (``texts.writer``): the writer writes texts from their
meanings only, for one reader; the code, then the checker, read each text before it is kept; a
text refused goes back to the writer once, with why; refused again, it is not kept.

The meanings are a small catalog of the tests' own (``catalog``). The model is a ``FakeChatModel``
(it answers the writer and the checker apart), or ``Answers`` when a test needs an answer of its
own: a broken one, a key not asked, a key left out."""

import asyncio
import json
from collections import Counter
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import pytest
from domo_common import meanings
from domo_core.llm.levels import Level
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatMessage,
    ChatModel,
    ChatUsage,
    HistoryItem,
    ModelCallError,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
)
from domo_core.prompts import text_checker_prompt, text_writer_prompt
from domo_core.texts import writer as text_writer
from domo_core.texts.checks import HEAD_MAX_LENGTH, check_head, check_text
from domo_core.texts.writer import (
    REVIEW_TOOL,
    TEXT_CHECKER_FIRST_LINE,
    TEXT_WRITER_FIRST_LINE,
    TEXTS_TOOL,
    WRITER_KEYS_AT_ONCE,
    ForWhom,
    WriterUnavailable,
    write_head,
    write_texts,
)
from fakes import FakeChatModel, fake_factory
from test_picker import add_key, picker_on

CONFIRM = "test.confirm"
WELCOME = "test.welcome"
SAVED = "test.saved"
TEST_MEANINGS = {
    CONFIRM: "Confirm",
    WELCOME: "Welcome back, {name}.",
    SAVED: "The key is saved.",
}
TEST_NOTES = {CONFIRM: "Label of the button on a request card that gives the yes."}

FOR_ALEX = ForWhom(
    language="fr",
    name="Alex",
    role="adult",
    writing_note="Tutoie Alex, sans blague sur les cartes.",
    indication="Plus court.",
)


@pytest.fixture(autouse=True)
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meanings, "_meanings", lambda: TEST_MEANINGS)
    monkeypatch.setattr(meanings, "_notes", lambda: TEST_NOTES)


class Models:
    """The models of the texts (``TextModels``): ``writer`` for the writing, ``checker`` for the
    checking (the writer's own model unless told otherwise). ``asked`` notes each ask: the kind
    of task and the level asked at least."""

    def __init__(self, writer: ChatModel | None, checker: Any = "same") -> None:
        self._writer = writer
        self._checker = writer if checker == "same" else checker
        self.asked: list[tuple[TaskKind, Level | None]] = []

    async def model_for(self, task: TaskKind, *, at_least: Level | None = None) -> ChatModel | None:
        self.asked.append((task, at_least))
        return self._writer if task == "writing" else self._checker


class Answers:
    """A model that answers each call with the next of ``answers``: the arguments of one call of
    the tool ``name`` (a dict, or a text sent as it is), a ``ModelCallError`` to raise, or
    ``Said(text)``: words and no call. ``messages`` are what each call received."""

    def __init__(self, name: str, *answers: object) -> None:
        self._name = name
        self._answers = list(answers)
        self.messages: list[list[HistoryItem]] = []

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
        self.messages.append(list(messages))
        answer = self._answers.pop(0)
        if isinstance(answer, ModelCallError):
            raise answer
        if isinstance(answer, Said):
            yield ChatChunk(answer.text)
            return
        arguments = answer if isinstance(answer, str) else json.dumps(answer)
        yield ToolCall(f"call-{len(self.messages)}", self._name, arguments)


class Said:
    def __init__(self, text: str) -> None:
        self.text = text


class Slow:
    """A model that takes ``seconds`` before it says anything."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    async def stream(self, **_: object) -> AsyncGenerator[ChatChunk | ToolCall | ChatUsage]:
        await asyncio.sleep(self._seconds)
        yield ChatChunk("late")


def asked_of(call_messages: Sequence[HistoryItem]) -> dict[str, Any]:
    """The JSON of the one message a writer's or a checker's call starts with."""
    first = call_messages[0]
    assert isinstance(first, ChatMessage) and first.role == "user"
    return json.loads(first.content)


def checked_keys(model: FakeChatModel) -> list[list[str]]:
    """The keys each call of the checker was given, in order."""
    return [list(asked_of(call.messages)["texts"]) for call in model.checker_calls()]


# --- what the writer and the checker see --------------------------------------------------------


async def test_the_writer_sees_only_meanings_and_for_whom():
    model = FakeChatModel()
    current = {WELCOME: "Re-bonjour, {name}.", "test.not_asked": "Ailleurs."}
    written = await write_texts(Models(model), [CONFIRM, WELCOME], FOR_ALEX, current=current)

    [call] = model.writer_calls()
    assert call.system == text_writer_prompt()
    assert call.system.splitlines()[0] == TEXT_WRITER_FIRST_LINE
    assert call.tools == (TEXTS_TOOL,)
    # One message, JSON the code built: who the texts are for, and each key's meaning, note,
    # holes, longest length and the text of today. Nothing else: no conversation, no words left
    # by someone, no other key's text.
    assert len(call.messages) == 1
    assert asked_of(call.messages) == {
        "for_whom": {
            "language": "fr",
            "name": "Alex",
            "role": "adult",
            "writing_note": "Tutoie Alex, sans blague sur les cartes.",
            "indication": "Plus court.",
        },
        "texts": {
            CONFIRM: {
                "meaning": "Confirm",
                "note": TEST_NOTES[CONFIRM],
                "holes": [],
                "max_length": 2 * len("Confirm") + 40,
            },
            WELCOME: {
                "meaning": "Welcome back, {name}.",
                "holes": ["name"],
                "max_length": 2 * len("Welcome back, {name}.") + 40,
                "current": "Re-bonjour, {name}.",
            },
        },
    }
    # The fake writer gives each meaning, or the text of today when there is one.
    assert written.texts == {CONFIRM: "Confirm", WELCOME: "Re-bonjour, {name}."}
    assert written.refused == {}
    # The writer's and the checker's calls are apart from the others.
    assert model.calls == []


async def test_the_checker_sees_the_meanings_the_texts_and_the_language():
    model = FakeChatModel(writer=lambda key, meaning, for_whom: f"[{for_whom.language}] {meaning}")
    await write_texts(Models(model), [CONFIRM, WELCOME], FOR_ALEX)

    [call] = model.checker_calls()
    assert call.system == text_checker_prompt()
    assert call.system.splitlines()[0] == TEXT_CHECKER_FIRST_LINE
    assert call.tools == (REVIEW_TOOL,)
    assert len(call.messages) == 1
    # Not who the texts are for (but their language), not their writing note, not the indication.
    assert asked_of(call.messages) == {
        "language": "fr",
        "texts": {
            CONFIRM: {
                "meaning": "Confirm",
                "note": TEST_NOTES[CONFIRM],
                "holes": [],
                "text": "[fr] Confirm",
            },
            WELCOME: {
                "meaning": "Welcome back, {name}.",
                "holes": ["name"],
                "text": "[fr] Welcome back, {name}.",
            },
        },
    }


async def test_the_checker_has_room_to_refuse_a_whole_piece(monkeypatch):
    many = many_meanings(monkeypatch, WRITER_KEYS_AT_ONCE)
    one, forty = FakeChatModel(), FakeChatModel()
    await write_texts(Models(one), [CONFIRM], FOR_ALEX)
    await write_texts(Models(forty), list(many), FOR_ALEX)

    [few] = one.checker_calls()
    [lots] = forty.checker_calls()
    # The room the checker is given grows with what it reads: enough to refuse every text of a
    # whole piece with a sentence each (about 35 tokens a refusal), never beyond the writer's own.
    assert few.max_output_tokens < lots.max_output_tokens
    assert WRITER_KEYS_AT_ONCE * 64 <= lots.max_output_tokens <= 8192


# --- a large group, in pieces -------------------------------------------------------------------


def many_meanings(monkeypatch: pytest.MonkeyPatch, count: int) -> dict[str, str]:
    """``count`` more meanings in the catalog, in order: ``test.many.000`` and on."""
    many = {f"test.many.{n:03}": f"Text number {n}." for n in range(count)}
    monkeypatch.setattr(meanings, "_meanings", lambda: {**TEST_MEANINGS, **many})
    return many


def asked_keys(model: Answers) -> list[list[str]]:
    """The keys each call of a writer was asked, in order."""
    return [list(asked_of(messages)["texts"]) for messages in model.messages]


def as_written(keys: Sequence[str]) -> dict[str, Any]:
    """The writer's answer that gives each of ``keys`` its meaning as it is."""
    return {"texts": {key: meanings.meaning(key) for key in keys}}


async def test_a_group_of_138_keys_is_asked_in_four_calls(monkeypatch):
    assert WRITER_KEYS_AT_ONCE == 40
    many = list(many_meanings(monkeypatch, 138))
    model = FakeChatModel()
    written = await write_texts(Models(model), many, FOR_ALEX)

    asked = [list(asked_of(call.messages)["texts"]) for call in model.writer_calls()]
    assert [len(keys) for keys in asked] == [40, 40, 40, 18]
    # In order, each key once.
    assert [key for keys in asked for key in keys] == many
    # Each piece is read by the checker on its own.
    assert checked_keys(model) == asked
    assert written.texts == {key: meanings.meaning(key) for key in many}
    assert written.refused == {}


async def test_an_unreadable_piece_is_asked_again_alone_and_the_others_are_kept(monkeypatch):
    many = list(many_meanings(monkeypatch, 90))
    first, second, third = many[:40], many[40:80], many[80:]
    writer = Answers(
        "submit_texts",
        # The first piece: unreadable, then written.
        "not JSON",
        as_written(first),
        # The second: unreadable twice.
        Said("Too many texts."),
        {"texts": {}},
        # The third: written at once.
        as_written(third),
    )
    written = await write_texts(Models(writer, FakeChatModel()), many, FOR_ALEX)

    assert asked_keys(writer) == [first, first, second, second, third]
    # Each asked again alone: a new call, not the result of the one before.
    assert [len(messages) for messages in writer.messages] == [1, 1, 1, 1, 1]
    # The keys of the piece that never answered stay unwritten: neither written nor refused.
    assert written.texts == {key: meanings.meaning(key) for key in first + third}
    assert written.refused == {}


async def test_every_piece_unreadable_is_writer_unavailable(monkeypatch):
    many = list(many_meanings(monkeypatch, 50))
    checker = FakeChatModel()
    writer = Answers("submit_texts", "not JSON", Said("No."), {"texts": {}}, "[1]")
    with pytest.raises(WriterUnavailable) as raised:
        await write_texts(Models(writer, checker), many, FOR_ALEX)

    assert raised.value.why == "unreadable"
    assert asked_keys(writer) == [many[:40], many[:40], many[40:], many[40:]]
    assert checker.checker_calls() == []


async def test_a_writer_that_fails_on_its_first_piece_is_asked_no_more(monkeypatch):
    # An error or a timeout before any piece answered: there is no writer, nothing is asked again.
    many = list(many_meanings(monkeypatch, 50))
    writer = Answers("submit_texts", ModelCallError("upstream"))
    with pytest.raises(WriterUnavailable) as raised:
        await write_texts(Models(writer, FakeChatModel()), many, FOR_ALEX)

    assert raised.value.why == "error"
    assert len(writer.messages) == 1


async def test_a_piece_that_fails_after_another_answered_stays_unwritten(monkeypatch):
    many = list(many_meanings(monkeypatch, 50))
    writer = Answers("submit_texts", as_written(many[:40]), ModelCallError("limit_reached"))
    written = await write_texts(Models(writer, FakeChatModel()), many, FOR_ALEX)

    assert written.texts == {key: meanings.meaning(key) for key in many[:40]}
    assert written.refused == {}
    assert len(writer.messages) == 2


async def test_writer_and_checker_go_on_their_own_kinds():
    models = Models(FakeChatModel())
    await write_texts(models, [CONFIRM], FOR_ALEX)
    assert models.asked == [("writing", None), ("checking", None)]


async def test_at_least_raises_the_writer_and_never_the_checker():
    models = Models(FakeChatModel())
    await write_texts(models, [CONFIRM], FOR_ALEX, at_least="strong")
    assert models.asked == [("writing", "strong"), ("checking", None)]

    models = Models(FakeChatModel())
    head = await write_head(
        models,
        reference="Alex asks.",
        names=("Alex",),
        amounts=(),
        holes=(),
        for_whom=FOR_ALEX,
        at_least="medium",
    )
    assert head == "Alex asks."
    assert models.asked == [("writing", "medium"), ("checking", None)]


async def test_an_unknown_key_is_refused_before_any_call():
    model = FakeChatModel()
    with pytest.raises(KeyError):
        await write_texts(Models(model), [CONFIRM, "test.unknown"], FOR_ALEX)
    assert model.writer_calls() == []


# --- what is refused, and asked again once ------------------------------------------------------


async def test_a_text_refused_by_the_code_is_asked_again_once():
    tries: Counter[str] = Counter()

    def writer(key: str, meaning: str, for_whom: ForWhom) -> str:
        tries[key] += 1
        # The first text of the welcome loses its hole.
        return "Bon retour." if key == WELCOME and tries[key] == 1 else meaning

    model = FakeChatModel(writer=writer)
    written = await write_texts(Models(model), [CONFIRM, WELCOME], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirm", WELCOME: "Welcome back, {name}."}
    assert written.refused == {}
    assert tries == {CONFIRM: 1, WELCOME: 2}
    first, second = model.writer_calls()
    # Asked again in the same conversation: its call, then the code's reasons as its result.
    assert second.messages[0] == first.messages[0]
    answered, result = second.messages[1:]
    assert isinstance(answered, AssistantToolCalls) and isinstance(result, ToolResult)
    [call] = answered.calls
    assert (result.call_id, result.name) == (call.id, call.name) == (call.id, "submit_texts")
    assert json.loads(result.content) == {
        "accepted": False,
        "errors": {WELCOME: check_text(WELCOME, "Bon retour.")},
    }
    # The checker read what passed the code, each text once.
    assert checked_keys(model) == [[CONFIRM], [WELCOME]]


async def test_a_text_refused_twice_is_not_kept():
    model = FakeChatModel(
        writer=lambda key, meaning, for_whom: "Bon retour." if key == WELCOME else meaning
    )
    written = await write_texts(Models(model), [CONFIRM, WELCOME], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirm"}
    assert written.refused == {WELCOME: check_text(WELCOME, "Bon retour.")}
    # Asked twice, never a third time; the checker never read it.
    assert len(model.writer_calls()) == 2
    assert checked_keys(model) == [[CONFIRM]]


async def test_the_checker_refusal_goes_back_to_the_writer_once():
    texts = iter(["Annuler", "Confirmer"])
    model = FakeChatModel(
        writer=lambda key, meaning, for_whom: next(texts) if key == CONFIRM else meaning,
        checker=lambda key, text: (
            "a button that confirms may not say Cancel" if text == "Annuler" else None
        ),
    )
    written = await write_texts(Models(model), [CONFIRM, WELCOME], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirmer", WELCOME: "Welcome back, {name}."}
    assert written.refused == {}
    _, second = model.writer_calls()
    result = second.messages[-1]
    assert isinstance(result, ToolResult)
    assert json.loads(result.content) == {
        "accepted": False,
        "errors": {CONFIRM: "a button that confirms may not say Cancel"},
    }
    # The new text went through the code, then the checker again.
    assert checked_keys(model) == [[CONFIRM, WELCOME], [CONFIRM]]


async def test_a_text_the_checker_refuses_twice_is_not_kept():
    model = FakeChatModel(
        checker=lambda key, text: "it says more than its meaning" if key == CONFIRM else None
    )
    written = await write_texts(Models(model), [CONFIRM, WELCOME], FOR_ALEX)

    assert written.texts == {WELCOME: "Welcome back, {name}."}
    assert written.refused == {CONFIRM: "it says more than its meaning"}
    assert len(model.writer_calls()) == 2
    assert checked_keys(model) == [[CONFIRM, WELCOME], [CONFIRM]]


async def test_both_refusals_go_back_in_one_call():
    model = FakeChatModel(
        writer=lambda key, meaning, for_whom: "Bon retour." if key == WELCOME else meaning,
        checker=lambda key, text: "not French" if key == CONFIRM else None,
    )
    written = await write_texts(Models(model), [CONFIRM, WELCOME, SAVED], FOR_ALEX)

    assert written.texts == {SAVED: "The key is saved."}
    assert set(written.refused) == {CONFIRM, WELCOME}
    _, second = model.writer_calls()
    result = second.messages[-1]
    assert isinstance(result, ToolResult)
    assert json.loads(result.content)["errors"] == {
        WELCOME: check_text(WELCOME, "Bon retour."),
        CONFIRM: "not French",
    }


async def test_a_checker_reason_that_cannot_be_shown_is_replaced():
    model = FakeChatModel(
        checker=lambda key, text: "bad‮ reason\nsecond line" if key == CONFIRM else None
    )
    written = await write_texts(Models(model), [CONFIRM], FOR_ALEX)
    assert written.texts == {}
    [why] = written.refused.values()
    assert "‮" not in why and "\n" not in why and why


async def test_keys_not_asked_are_ignored():
    writer = Answers(
        "submit_texts",
        {"texts": {CONFIRM: "Confirmer", "test.not_asked": "Ailleurs.", SAVED: "C'est fait."}},
    )
    checker = FakeChatModel()
    written = await write_texts(Models(writer, checker), [CONFIRM, SAVED], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirmer", SAVED: "C'est fait."}
    assert written.refused == {}
    assert checked_keys(checker) == [[CONFIRM, SAVED]]


async def test_keys_not_given_stay_missing():
    # The writer is asked once: a second call would find no answer left.
    writer = Answers("submit_texts", {"texts": {CONFIRM: "Confirmer"}})
    written = await write_texts(Models(writer, FakeChatModel()), [CONFIRM, SAVED], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirmer"}
    assert written.refused == {}
    assert len(writer.messages) == 1


async def test_a_text_that_is_not_a_string_is_refused_by_the_code():
    writer = Answers(
        "submit_texts",
        {"texts": {CONFIRM: 12, SAVED: "C'est fait."}},
        {"texts": {CONFIRM: ["Confirmer"]}},
    )
    written = await write_texts(Models(writer, FakeChatModel()), [CONFIRM, SAVED], FOR_ALEX)

    assert written.texts == {SAVED: "C'est fait."}
    assert set(written.refused) == {CONFIRM}


# --- a writer or a checker that does not answer -------------------------------------------------


async def test_no_model_raises_writer_unavailable():
    with pytest.raises(WriterUnavailable):
        await write_texts(Models(None), [CONFIRM], FOR_ALEX)


@pytest.mark.parametrize(
    "answer",
    [
        ModelCallError("upstream"),
        ModelCallError("limit_reached"),
        Said("Here are your texts."),
        "not JSON",
        "[1, 2]",
        {"texts": ["Confirmer"]},
        {"texts": {CONFIRM: "Confirmer"}, "more": True},
        {"nothing": {}},
    ],
)
async def test_a_first_call_without_a_readable_answer_raises_writer_unavailable(answer):
    checker = FakeChatModel()
    writer = Answers("submit_texts", answer, answer)
    with pytest.raises(WriterUnavailable):
        await write_texts(Models(writer, checker), [CONFIRM], FOR_ALEX)
    # An unreadable answer is asked again once; an error is not.
    assert len(writer.messages) == (1 if isinstance(answer, ModelCallError) else 2)
    assert checker.checker_calls() == []


@pytest.mark.parametrize(
    "answer",
    [
        {"texts": {}},
        {"texts": {"test.not_asked": "Ailleurs."}},
        {"texts": {CONFIRM: 12, SAVED: None}},
    ],
)
async def test_a_first_answer_without_any_text_asked_raises_writer_unavailable(answer):
    # Readable, but nothing in it can be used: as if the writer had not answered.
    checker = FakeChatModel()
    writer = Answers("submit_texts", answer, answer)
    with pytest.raises(WriterUnavailable) as raised:
        await write_texts(Models(writer, checker), [CONFIRM, SAVED], FOR_ALEX)
    assert raised.value.why == "unreadable"
    # Asked again once, alone, in a call of its own.
    assert [len(messages) for messages in writer.messages] == [1, 1]
    assert checker.checker_calls() == []


async def test_a_call_of_another_tool_is_no_answer():
    checker = FakeChatModel()
    writer = Answers("submit_review", *[{"texts": {CONFIRM: "Confirmer"}}] * 2)
    with pytest.raises(WriterUnavailable):
        await write_texts(Models(writer, checker), [CONFIRM], FOR_ALEX)


async def test_a_writer_slower_than_its_time_is_unavailable(monkeypatch):
    monkeypatch.setattr(text_writer, "WRITE_SECONDS", 0.05)
    with pytest.raises(WriterUnavailable):
        await write_texts(Models(Slow(5)), [CONFIRM], FOR_ALEX)


@pytest.mark.parametrize(
    "silence",
    [
        None,
        Answers("submit_review", Said("All good.")),
        Answers("submit_review", ModelCallError("upstream")),
        Answers("submit_review", {"refused": [{"key": CONFIRM}]}),
        Answers("submit_review", {"refused": {}}),
        Answers("submit_texts", {"refused": []}),
    ],
)
async def test_a_silent_checker_keeps_nothing(silence):
    writer = Answers("submit_texts", {"texts": {CONFIRM: "Confirmer", WELCOME: "Bon retour."}})
    written = await write_texts(Models(writer, silence), [CONFIRM, WELCOME, SAVED], FOR_ALEX)

    assert written.texts == {}
    assert written.refused == {CONFIRM: "not checked", WELCOME: "not checked", SAVED: "not checked"}
    # Nothing is asked again: without a checker, nothing could be kept.
    assert len(writer.messages) == 1


@pytest.mark.parametrize(
    "retry", [ModelCallError("upstream"), Said("Sorry."), {"texts": "Bon retour, {name}."}]
)
async def test_a_retry_without_an_answer_keeps_what_was_accepted(retry):
    writer = Answers(
        "submit_texts", {"texts": {CONFIRM: "Confirmer", WELCOME: "Bon retour."}}, retry
    )
    written = await write_texts(Models(writer, FakeChatModel()), [CONFIRM, WELCOME], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirmer"}
    assert written.refused == {WELCOME: check_text(WELCOME, "Bon retour.")}


async def test_a_text_not_given_again_keeps_its_reason():
    writer = Answers(
        "submit_texts",
        {"texts": {CONFIRM: "Confirmer", WELCOME: "Bon retour."}},
        {"texts": {CONFIRM: "Encore."}},
    )
    written = await write_texts(Models(writer, FakeChatModel()), [CONFIRM, WELCOME], FOR_ALEX)

    # The key asked again and not given keeps its reason; the one given unasked is ignored.
    assert written.texts == {CONFIRM: "Confirmer"}
    assert written.refused == {WELCOME: check_text(WELCOME, "Bon retour.")}


async def test_a_checker_silent_on_its_second_pass_keeps_the_first():
    writer = Answers(
        "submit_texts",
        {"texts": {CONFIRM: "Confirmer", WELCOME: "Bon retour."}},
        {"texts": {WELCOME: "Bon retour, {name}."}},
    )
    checker = Answers("submit_review", {"refused": []}, Said("..."))
    written = await write_texts(Models(writer, checker), [CONFIRM, WELCOME], FOR_ALEX)

    assert written.texts == {CONFIRM: "Confirmer"}
    assert written.refused == {WELCOME: "not checked"}


# --- which model wrote ------------------------------------------------------------------------


async def test_the_picker_gives_each_its_model_and_the_texts_say_which_wrote(
    add_member, session_factory, clock
):
    owner = await add_member("Owner", "owner")
    key = await add_key(session_factory, owner.id)
    factory = fake_factory(FakeChatModel())
    picker = picker_on(key, session_factory, clock, factory)

    written = await write_texts(picker, [CONFIRM], FOR_ALEX)
    assert written.texts == {CONFIRM: "Confirm"}
    # The writing starts small, the checking medium.
    assert written.model_id == "example-haiku-1"
    assert factory.requested == [(key.id, "example-haiku-1"), (key.id, "example-sonnet-1")]

    written = await write_texts(picker, [CONFIRM], FOR_ALEX, at_least="strong")
    assert written.model_id == "example-opus-1"
    assert factory.requested[2:] == [(key.id, "example-opus-1"), (key.id, "example-sonnet-1")]


# --- the head of a card -------------------------------------------------------------------------

REFERENCE = "Alex asks to invite Robin for 3 days.\nThe judge says: {reason}"
HEAD_FACTS: dict[str, Any] = {
    "reference": REFERENCE,
    "names": ("Alex", "Robin"),
    "amounts": ("3",),
    "holes": ("reason",),
}


async def test_a_head_is_written_for_its_reader_from_its_reference():
    model = FakeChatModel()
    head = await write_head(Models(model), **HEAD_FACTS, for_whom=FOR_ALEX)

    assert head == REFERENCE
    [call] = model.writer_calls()
    assert call.system == text_writer_prompt()
    asked = asked_of(call.messages)
    assert asked["for_whom"]["language"] == "fr"
    [entry] = asked["texts"].values()
    assert entry["meaning"] == REFERENCE
    assert entry["holes"] == ["reason"]
    assert entry["names"] == ["Alex", "Robin"] and entry["amounts"] == ["3"]
    assert entry["max_length"] == HEAD_MAX_LENGTH
    assert len(model.checker_calls()) == 1


async def test_a_head_that_forgets_a_name_is_asked_again_then_dropped():
    heads = iter(
        ["Quelqu'un invite Robin 3 jours. {reason}", "Alex invite Robin 3 jours. {reason}"]
    )
    model = FakeChatModel(writer=lambda key, meaning, for_whom: next(heads))
    head = await write_head(Models(model), **HEAD_FACTS, for_whom=FOR_ALEX)
    assert head == "Alex invite Robin 3 jours. {reason}"
    result = model.writer_calls()[1].messages[-1]
    assert isinstance(result, ToolResult)
    [why] = json.loads(result.content)["errors"].values()
    assert why == check_head(
        "Quelqu'un invite Robin 3 jours. {reason}",
        names=("Alex", "Robin"),
        amounts=("3",),
        holes=("reason",),
    )

    model = FakeChatModel(writer=lambda key, meaning, for_whom: "Robin, 3 jours. {reason}")
    assert await write_head(Models(model), **HEAD_FACTS, for_whom=FOR_ALEX) is None
    assert len(model.writer_calls()) == 2


@pytest.mark.parametrize(
    "models",
    [
        lambda: Models(None),
        lambda: Models(Answers("submit_texts", ModelCallError("upstream"))),
        lambda: Models(Answers("submit_texts", Said("..."))),
        lambda: Models(FakeChatModel(), None),
        lambda: Models(FakeChatModel(), Answers("submit_review", Said("..."))),
        lambda: Models(FakeChatModel(checker=lambda key, text: "it says less")),
    ],
)
async def test_a_head_that_cannot_be_written_is_none(models):
    assert await write_head(models(), **HEAD_FACTS, for_whom=FOR_ALEX) is None


@pytest.mark.parametrize(
    "answer", [{"texts": {}}, {"texts": {"other": "Alex, Robin, 3."}}, {"texts": {"head": 12}}]
)
async def test_a_first_answer_without_a_head_is_none_at_once(answer):
    checker = FakeChatModel()
    writer = Answers("submit_texts", answer)
    assert await write_head(Models(writer, checker), **HEAD_FACTS, for_whom=FOR_ALEX) is None
    # Not asked again, and nothing to check.
    assert len(writer.messages) == 1
    assert checker.checker_calls() == []
