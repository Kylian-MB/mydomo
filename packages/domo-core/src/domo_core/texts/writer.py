"""The writer and the checker of the texts people read (spec 4.3, 5.3).

Every text people read on Domo's pages is written by the writer, for one reader, from the meaning
the code gives it (``domo_common.meanings``), then checked by the code (``texts.checks``) and by
the checker before it is kept. Both are isolated calls to a model, like the yes judge and the
rights writer: a prompt of their own, an input that is only data, an answer read strictly, a time
limit, one more try with the errors, and a fallback.

1. ``write_texts`` asks the writer (``prompts/text_writer.md``, whose first line is
   ``TEXT_WRITER_FIRST_LINE``; a call of the kind ``writing``) for the texts of ``keys``. Its one
   message is JSON the code builds (``_writer_message``): who the texts are for (``ForWhom``: their
   language, their name and role, the writing note Domo keeps for them and the indication of a
   rewrite, Domo's own words, data that change only the form) and, for each key, its meaning,
   its note, its holes, its longest length and the text the reader has today (``current``). Never
   the conversation, the words of a note, an outside content, nor anything people typed but names.
   It answers by one call of ``submit_texts`` (``TEXTS_TOOL``): ``{"texts": {"<key>": "<text>"}}``.
   A key it was not asked is ignored; a key asked and not given stays missing (in neither
   ``Written.texts`` nor ``Written.refused``), as long as the answer gives at least one text asked
   (point 4).
2. The code checks each text (``checks.check_text``). The checker (``prompts/text_checker.md``,
   ``TEXT_CHECKER_FIRST_LINE``; a call of the kind ``checking``) reads the meanings and the texts
   that passed, with the language they must be in (never who they are for), and answers by one call
   of ``submit_review`` (``REVIEW_TOOL``): ``{"refused": [{"key": "...", "why": "..."}]}``, the
   texts that say less, more or something else, mislead, are not in that language, or ask what
   the meaning does not ask.
3. Each text refused, by the code or by the checker, goes back to the writer once, with why, as the
   result of its call (``{"accepted": false, "errors": {"<key>": "<why>"}}``, as the rights writer
   is given its errors): its new text goes through the code, then the checker. Refused again, or
   not given again, it is not kept: ``Written.refused`` holds its last reason.
4. ``keys`` are asked in pieces of ``WRITER_KEYS_AT_ONCE`` at most, in order, each a writing of
   its own (points 1 to 3: its call of the writer, then of the checker). A piece without a readable
   answer, or with one that gives not one text asked (an empty ``texts``, keys not asked, no
   string), is asked once more, alone, in a new call; without one again, its keys stay unwritten
   (in neither ``Written.texts`` nor ``Written.refused``) and the other pieces are kept.
   ``WriterUnavailable`` is raised when no piece was answered: at once without a model, and on a
   ``ModelCallError`` or after ``WRITE_SECONDS`` before any piece was answered (such a failure of
   a later piece leaves only its keys unwritten). A failure of a piece's second call (point 3)
   refuses only the texts asked again (with their last reason), and keeps what was accepted. A
   checker that gives no readable answer (or no checker) keeps nothing it was to read: on its
   first reading every key of its piece is refused ``NOT_CHECKED``, on its second the texts of
   that reading.

``write_head`` does the same for the head of one card (spec 4.4), checked by ``checks.check_head``,
and gives ``None`` instead of raising; ``head_written`` gives, with the head, why it was refused in
the end (by the code or the checker) and the writer's model, for its caller to keep and journal.
Given ``at_least``, the writer is asked a model of at least that level (``TextModels.model_for``);
the checker never is.

Nothing here touches the database or the journal: the callers keep what is written and audit what
is refused.
"""

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast

import anyio
from domo_common import meanings

from domo_core.llm.levels import Level
from domo_core.llm.metered import MeteredModel
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import (
    AssistantToolCalls,
    ChatChunk,
    ChatMessage,
    ChatModel,
    HistoryItem,
    JsonObject,
    ModelCallError,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from domo_core.prompts import sendable, text_checker_prompt, text_writer_prompt
from domo_core.text import is_plain_line
from domo_core.texts.checks import HEAD_MAX_LENGTH, check_head, check_text

# The first lines of the writer's and the checker's prompts: the fake models of the tests know
# their calls by them.
TEXT_WRITER_FIRST_LINE: Final = "You write the texts people read on the assistant's pages."
TEXT_CHECKER_FIRST_LINE: Final = (
    "You check texts written for the assistant's pages against what they must say."
)
# How long each call of the writer or the checker may take.
WRITE_SECONDS: Final = 60
# The most texts the writer is asked in one call: a larger group is asked in pieces.
WRITER_KEYS_AT_ONCE: Final = 40

# The reason of a text no checker read.
NOT_CHECKED: Final = "not checked"
# The key the head of a card is written under.
HEAD_KEY: Final = "head"
# The id said of a model that is not a counted one (``metered.MeteredModel``).
UNKNOWN_MODEL: Final = "unknown"

# The most output tokens a call asks for (a model that gives fewer is asked no more than it gives:
# ``metered.MeteredModel``): room enough for a piece of ``WRITER_KEYS_AT_ONCE`` texts, in any
# language; for a head of ``HEAD_MAX_LENGTH`` characters, even at two tokens a character (some
# scripts) and its call around it. The checker gives only its refusals: room for a base, then for
# a refusal of each text it reads (about 35 tokens with its reason), with some to spare; never
# more than the writer.
_WRITER_OUTPUT_TOKENS: Final = 8192
_HEAD_OUTPUT_TOKENS: Final = 3072
_CHECKER_BASE_TOKENS: Final = 256
_CHECKER_TOKENS_PER_TEXT: Final = 96
# The longest arguments of a call read: beyond, the answer cannot be read.
_MAX_ARGUMENTS_LENGTH: Final = 200_000
# The longest reason of the checker kept.
_MAX_REASON_LENGTH: Final = 300
_NOT_A_TEXT: Final = "the text is not a string"
# What the checker is told of each text, besides the text.
_CHECKER_PARTS: Final = ("meaning", "note", "holes")
_REFUSED_WITHOUT_REASON: Final = "the checker refused it"

# What the writer is told of the head of a card, besides its reference (its meaning).
_HEAD_NOTE: Final = (
    "The top of a card in the chat, for its reader: it takes the place of the parts of the card "
    "its meaning shows, one per line. Keep each person of names as written and each amount of "
    "amounts in digits. The code puts in each hole later: {reason}, {question}, {words} and "
    "{words_before} are someone else's words, a quote maybe in another language; any other hole "
    "is a date, said in the reader's words."
)

TEXTS_TOOL: Final = ToolDefinition(
    name="submit_texts",
    description="Give the texts you wrote, once: each key you were given, with its text.",
    parameters={
        "type": "object",
        "properties": {
            "texts": {
                "type": "object",
                "description": "Each key you were given, with the text you wrote for it.",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["texts"],
        "additionalProperties": False,
    },
)

REVIEW_TOOL: Final = ToolDefinition(
    name="submit_review",
    description=(
        "Give your review, once: each text you refuse, with why; an empty list when every text "
        "is right."
    ),
    parameters={
        "type": "object",
        "properties": {
            "refused": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "The key of a text you refuse."},
                        "why": {
                            "type": "string",
                            "description": "Why, in one short English sentence for the writer.",
                        },
                    },
                    "required": ["key", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["refused"],
        "additionalProperties": False,
    },
)

# Why a model gave no answer: none to ask, the call failed, it took too long, or what it gave
# cannot be read.
Unanswered = Literal["no_model", "error", "timeout", "unreadable"]


@dataclass(frozen=True)
class ForWhom:
    """Who the texts are written for: their ``language`` (a tag: ``fr``, ``de``, ``pt-BR``...),
    their ``name`` and ``role`` when the texts are theirs (a version of a language has neither),
    the ``writing_note`` Domo keeps for them and the ``indication`` of a rewrite they asked
    for. The note and the indication are Domo's words: data, which change only the form."""

    language: str
    name: str | None = None
    role: str | None = None
    writing_note: str | None = None
    indication: str | None = None


@dataclass(frozen=True)
class Written:
    """What a writing gives: the ``texts`` the code and the checker accepted, by key; the keys
    ``refused`` in the end, each with its last reason (the code's, the checker's, or
    ``NOT_CHECKED``); and the id of the writer's model (``model_id``; empty when nothing was
    asked)."""

    texts: dict[str, str]
    refused: dict[str, str]
    model_id: str


@dataclass(frozen=True)
class HeadWritten:
    """What the writing of a head gives (``head_written``): the ``head`` the code and the checker
    accepted, its holes left as they are (``None`` when there is none); why it was refused in the
    end, by the code or the checker (``refused``: ``None`` when it was not, nor when nobody
    answered, or no checker read it); and the id of the writer's model (``model_id``; empty when
    there was none)."""

    head: str | None
    refused: str | None
    model_id: str


class WriterUnavailable(Exception):
    """The writer gave no answer to its first call: ``why`` says how (no model, an error, a
    timeout, nothing readable)."""

    def __init__(self, why: Unanswered) -> None:
        super().__init__(f"the writer of the texts did not answer ({why})")
        self.why: Unanswered = why


class TextModels(Protocol):
    """Where the writer and the checker take their models (``llm.picker.ModelPicker`` is one)."""

    async def model_for(self, task: TaskKind, *, at_least: Level | None = None) -> ChatModel | None:
        """The model of a call of ``task``, of at least the level ``at_least`` when given;
        ``None`` when there is none."""
        ...


# --- what the writer and the checker are told -----------------------------------------------------


def _as_json(value: object) -> str:
    """``value`` as the JSON a model reads (``prompts.sendable``: nothing that cannot be sent)."""
    return json.dumps(sendable(value), ensure_ascii=False)


def _meaning_json(key: str, current: str | None) -> JsonObject:
    """What the writer is told of ``key``: its meaning, its note when it has one, its holes, its
    longest length, and the text of today when there is one."""
    told: JsonObject = {"meaning": meanings.meaning(key)}
    note = meanings.meaning_note(key)
    if note is not None:
        told["note"] = note
    told["holes"] = sorted(meanings.holes(key))
    told["max_length"] = meanings.max_length(key)
    if current is not None:
        told["current"] = current
    return told


def _writer_message(for_whom: ForWhom, items: Mapping[str, JsonObject]) -> str:
    """The writer's one message: who the texts are for, and what each must say."""
    return _as_json(
        {
            "for_whom": {
                "language": for_whom.language,
                "name": for_whom.name,
                "role": for_whom.role,
                "writing_note": for_whom.writing_note,
                "indication": for_whom.indication,
            },
            "texts": items,
        }
    )


def _checker_message(
    language: str, items: Mapping[str, JsonObject], texts: Mapping[str, str]
) -> str:
    """The checker's one message: the language the texts must be in, and for each text its
    meaning, its note, its holes and the text (not who it is for, nor what the writer was told
    of the form)."""
    told: dict[str, JsonObject] = {}
    for key, text in texts.items():
        told[key] = {part: items[key][part] for part in _CHECKER_PARTS if part in items[key]}
        told[key]["text"] = text
    return _as_json({"language": language, "texts": told})


# --- what they answer ---------------------------------------------------------------------------


async def _ask(
    model: ChatModel,
    *,
    system: str,
    history: Sequence[HistoryItem],
    tool: ToolDefinition,
    output_tokens: int,
) -> tuple[str, ToolCall] | Unanswered:
    """One call, within ``WRITE_SECONDS``: what the model said and its one call of ``tool``; or
    why there is no answer (it failed, took too long, or did not make exactly that one call)."""
    pieces: list[str] = []
    calls: list[ToolCall] = []
    with anyio.move_on_after(WRITE_SECONDS) as limit:
        try:
            stream = model.stream(
                system=system, messages=history, max_output_tokens=output_tokens, tools=(tool,)
            )
            async with aclosing(stream) as items:
                async for item in items:
                    if isinstance(item, ToolCall):
                        calls.append(item)
                    elif isinstance(item, ChatChunk):
                        pieces.append(item.text)
        except ModelCallError:
            return "error"
    if limit.cancelled_caught:
        return "timeout"
    if len(calls) != 1 or calls[0].name != tool.name:
        return "unreadable"
    return "".join(pieces), calls[0]


def _arguments(call: ToolCall, field: str) -> object | None:
    """The one field ``field`` of the arguments of ``call``; ``None`` unless they are a JSON
    object of that field alone."""
    if len(call.arguments) > _MAX_ARGUMENTS_LENGTH:
        return None
    try:
        arguments: object = json.loads(call.arguments)
    except (ValueError, RecursionError):
        return None
    if not isinstance(arguments, dict):
        return None
    given = cast(dict[str, object], arguments)
    return given[field] if set(given) == {field} else None


def _read_texts(call: ToolCall) -> dict[str, object] | None:
    """The texts of the writer's call, by key, as given (``None`` when it cannot be read)."""
    texts = _arguments(call, "texts")
    return cast(dict[str, object], texts) if isinstance(texts, dict) else None


def _reason_of(why: str) -> str:
    """A reason of the checker as it is kept: on one line, ``_MAX_REASON_LENGTH`` characters at
    most; a fixed one when it is empty or cannot be shown."""
    said = " ".join(why.split())[:_MAX_REASON_LENGTH].rstrip()
    return said if said and is_plain_line(said) else _REFUSED_WITHOUT_REASON


def _read_review(call: ToolCall) -> dict[str, str] | None:
    """The keys the checker refuses, each with its reason (``_reason_of``; the first, for a key
    refused twice); ``None`` when its call cannot be read."""
    refused = _arguments(call, "refused")
    if not isinstance(refused, list):
        return None
    reasons: dict[str, str] = {}
    for entry in cast(list[object], refused):
        if not isinstance(entry, dict):
            return None
        item = cast(dict[str, object], entry)
        key, why = item.get("key"), item.get("why")
        if set(item) != {"key", "why"} or not isinstance(key, str) or not isinstance(why, str):
            return None
        reasons.setdefault(key, _reason_of(why))
    return reasons


def _checked(
    given: Mapping[str, object], asked: Collection[str], check: Callable[[str, str], str | None]
) -> tuple[dict[str, str], dict[str, str]]:
    """The texts of ``given`` for the keys ``asked`` that pass the code (``check``), and the
    reasons of those that do not. A key not asked is ignored; a key asked and not given is in
    neither."""
    passed: dict[str, str] = {}
    refused: dict[str, str] = {}
    for key in asked:
        if key not in given:
            continue
        text = given[key]
        if not isinstance(text, str):
            refused[key] = _NOT_A_TEXT
            continue
        why = check(key, text)
        if why is None:
            passed[key] = text
        else:
            refused[key] = why
    return passed, refused


def _checker_output_tokens(count: int) -> int:
    """The room the checker is given to read ``count`` texts: enough to refuse them all."""
    return min(_WRITER_OUTPUT_TOKENS, _CHECKER_BASE_TOKENS + _CHECKER_TOKENS_PER_TEXT * count)


class _Checker:
    """The checker of one writing: its model is asked for once, when there is first something to
    read (``TextModels.model_for("checking")``, never raised)."""

    def __init__(self, models: TextModels, language: str, items: Mapping[str, JsonObject]) -> None:
        self._models = models
        self._language = language
        self._items = items
        self._model: ChatModel | None = None
        self._asked = False

    async def review(self, texts: Mapping[str, str]) -> dict[str, str] | None:
        """The reasons of the texts the checker refuses among ``texts`` (empty: none); ``None``
        when there is no checker, or it gave no readable answer. Nothing is asked of it when
        there is nothing to read."""
        if not texts:
            return {}
        if not self._asked:
            self._asked = True
            self._model = await self._models.model_for("checking")
        if self._model is None:
            return None
        answer = await _ask(
            self._model,
            system=text_checker_prompt(),
            history=[ChatMessage("user", _checker_message(self._language, self._items, texts))],
            tool=REVIEW_TOOL,
            output_tokens=_checker_output_tokens(len(texts)),
        )
        reasons = None if isinstance(answer, str) else _read_review(answer[1])
        if reasons is None:
            return None
        return {key: why for key, why in reasons.items() if key in texts}


async def _write(
    models: TextModels,
    writer: ChatModel,
    items: Mapping[str, JsonObject],
    for_whom: ForWhom,
    check: Callable[[str, str], str | None],
    *,
    output_tokens: int,
) -> tuple[dict[str, str], dict[str, str]]:
    """The texts of ``items`` that ``writer`` wrote and the code (``check``) and the checker
    accepted, and the last reasons of those refused (see the module docstring).
    ``WriterUnavailable`` when the writer's first call gives no answer."""
    history: list[HistoryItem] = [ChatMessage("user", _writer_message(for_whom, items))]
    checker = _Checker(models, for_whom.language, items)

    async def ask_writer() -> tuple[str, ToolCall, dict[str, object]] | Unanswered:
        answer = await _ask(
            writer,
            system=text_writer_prompt(),
            history=history,
            tool=TEXTS_TOOL,
            output_tokens=output_tokens,
        )
        if isinstance(answer, str):
            return answer
        given = _read_texts(answer[1])
        return "unreadable" if given is None else (*answer, given)

    first = await ask_writer()
    if isinstance(first, str):
        raise WriterUnavailable(first)
    said, call, given = first
    if not any(isinstance(given.get(key), str) for key in items):
        # Readable, but not one text asked: as good as no answer.
        raise WriterUnavailable("unreadable")
    passed, reasons = _checked(given, items, check)
    review = await checker.review(passed)
    if review is None:
        return {}, dict.fromkeys(items, NOT_CHECKED)
    kept = {key: text for key, text in passed.items() if key not in review}
    reasons.update(review)
    if not reasons:
        return kept, {}

    # Each text refused goes back once, with why, as the result of the writer's call.
    errors = {"accepted": False, "errors": reasons}
    history.append(AssistantToolCalls(said, (call,)))
    history.append(ToolResult(call.id, call.name, _as_json(errors)))
    again = await ask_writer()
    if isinstance(again, str):
        return kept, reasons
    passed, refused_again = _checked(again[2], reasons, check)
    # A text not given again keeps its last reason.
    refused = {
        key: refused_again.get(key, why) for key, why in reasons.items() if key not in passed
    }
    review = await checker.review(passed)
    if review is None:
        refused.update(dict.fromkeys(passed, NOT_CHECKED))
        return kept, refused
    kept.update({key: text for key, text in passed.items() if key not in review})
    refused.update(review)
    return kept, refused


def _model_id(model: ChatModel) -> str:
    """The id of the writer's model: its route's, for a counted model (what a ``ModelPicker``
    gives); ``UNKNOWN_MODEL`` for any other."""
    return model.route.model_id if isinstance(model, MeteredModel) else UNKNOWN_MODEL


async def write_texts(
    models: TextModels,
    keys: Sequence[str],
    for_whom: ForWhom,
    *,
    current: Mapping[str, str] | None = None,
    at_least: Level | None = None,
) -> Written:
    """The texts of ``keys`` written for ``for_whom`` and checked (see the module docstring).
    ``current`` gives the texts the reader has today (those of other keys are ignored);
    ``at_least`` is the least level of the writer's model. ``KeyError`` for a key that is not in
    the catalog of meanings, before any call; ``WriterUnavailable`` when no piece of ``keys``
    was answered (point 4). No key, nothing asked: nothing written."""
    today = current or {}
    items = {key: _meaning_json(key, today.get(key)) for key in keys}
    if not items:
        return Written({}, {}, "")
    writer = await models.model_for("writing", at_least=at_least)
    if writer is None:
        raise WriterUnavailable("no_model")
    texts: dict[str, str] = {}
    refused: dict[str, str] = {}
    unanswered: WriterUnavailable | None = None
    answered = False
    ordered = list(items)
    for start in range(0, len(ordered), WRITER_KEYS_AT_ONCE):
        piece = {key: items[key] for key in ordered[start : start + WRITER_KEYS_AT_ONCE]}
        try:
            kept, reasons = await _write_piece(models, writer, piece, for_whom)
        except WriterUnavailable as unavailable:
            if not answered and unavailable.why != "unreadable":
                # An error or a timeout before any piece answered: there is no writer.
                raise
            # Its keys stay unwritten; the other pieces are asked all the same.
            unanswered = unavailable
            continue
        answered = True
        texts.update(kept)
        refused.update(reasons)
    if not answered and unanswered is not None:
        raise unanswered
    return Written(texts, refused, _model_id(writer))


async def _write_piece(
    models: TextModels, writer: ChatModel, piece: Mapping[str, JsonObject], for_whom: ForWhom
) -> tuple[dict[str, str], dict[str, str]]:
    """``_write`` for one piece of a group; a piece without a readable answer is asked once more,
    alone, in a call of its own. ``WriterUnavailable`` when it gives none again, or at once on an
    error or a timeout."""
    try:
        return await _write(
            models, writer, piece, for_whom, check_text, output_tokens=_WRITER_OUTPUT_TOKENS
        )
    except WriterUnavailable as unavailable:
        if unavailable.why != "unreadable":
            raise
    return await _write(
        models, writer, piece, for_whom, check_text, output_tokens=_WRITER_OUTPUT_TOKENS
    )


async def write_head(
    models: TextModels,
    *,
    reference: str,
    names: Sequence[str],
    amounts: Sequence[str],
    holes: Collection[str],
    for_whom: ForWhom,
    at_least: Level | None = None,
) -> str | None:
    """The head of a card written for ``for_whom`` from ``reference`` (the English head they
    would read today, with its ``holes`` left as they are) and checked, like a text of
    ``write_texts``, by ``checks.check_head``: each of ``names`` and ``amounts`` in it. ``None``
    when no head could be written and checked, whatever the reason."""
    written = await head_written(
        models,
        reference=reference,
        names=names,
        amounts=amounts,
        holes=holes,
        for_whom=for_whom,
        at_least=at_least,
    )
    return written.head


async def head_written(
    models: TextModels,
    *,
    reference: str,
    names: Sequence[str],
    amounts: Sequence[str],
    holes: Collection[str],
    for_whom: ForWhom,
    at_least: Level | None = None,
) -> HeadWritten:
    """``write_head``, with why the head was refused in the end and the writer's model
    (``HeadWritten``)."""
    items: dict[str, JsonObject] = {
        HEAD_KEY: {
            "meaning": reference,
            "note": _HEAD_NOTE,
            "holes": sorted(holes),
            "max_length": HEAD_MAX_LENGTH,
            "names": list(names),
            "amounts": list(amounts),
        }
    }
    writer = await models.model_for("writing", at_least=at_least)
    if writer is None:
        return HeadWritten(None, None, "")

    def check(_: str, text: str) -> str | None:
        return check_head(text, names=names, amounts=amounts, holes=holes)

    model_id = _model_id(writer)
    try:
        texts, refused = await _write(
            models, writer, items, for_whom, check, output_tokens=_HEAD_OUTPUT_TOKENS
        )
    except WriterUnavailable:
        return HeadWritten(None, None, model_id)
    why = refused.get(HEAD_KEY)
    # A head no checker read was not refused: nobody answered.
    return HeadWritten(texts.get(HEAD_KEY), None if why == NOT_CHECKED else why, model_id)
