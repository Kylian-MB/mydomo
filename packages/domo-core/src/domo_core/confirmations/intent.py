"""Reading a person's answer to the requests the reply just before showed them: for each, yes,
no, a yes with a change, or unclear (spec 11.1).

This is the plan's decision D2. The answer comes from the person's own words, and never from the
main model:

1. when one request was shown, the code first compares the **whole** message, once normalised
   (``normalize``), with short phrases it knows in French and English (``match_phrase``): "oui",
   "non merci", "I confirm". A phrase only counts when it is the entire message: "oui mais mets
   B" is not a "oui";
2. otherwise an isolated reader reads it (``read_replies``): a call to a model of the key of the
   request, of medium level or stronger (``llm.picker``, kind of task ``intent``; without one the
   answer is ``unclear``), with a prompt of its own (``prompts/confirm_classifier.md``), no tool,
   no thinking and a tiny output. It is given one message, a JSON object: who speaks
   (``conversation.speaker.Speaker.as_json``, as the judge reads it), the requests shown, each
   numbered as the person saw them, with what it does and what it asks, the person's last few
   messages before the reply (``READ_BEFORE``, each cut) and the reply. All of it is data: its
   answer is read strictly, one line per request, ``<n>: yes|firm|no|change|unclear``; any other
   line, or a line for a number that was not shown, is ignored, and a request without its line
   is ``unclear``. "Yes, both" answers two requests; "yes, the first one", one.

What each request asks (``ask``, from ``confirmations.store.ask_of``) decides what a yes gives: a
yes asked (or nothing) is given by any yes; an explicit yes by a firm yes, whatever its words (or
the button): "I confirm" is the phrase the code knows, and for any other wording the reader says
whether the yes is a firm, deliberate one ("yes, go ahead, I am sure") or a bare one ("ok"),
which is ``unclear`` (``not_enough``); a known plain yes ("oui") to such a request is asked of
the reader too, since the messages before may make it firm ("je suis sûre", said just before).
A proof is never given in words, however firm. A refusal and a yes with a change are read the same
way whatever is asked.

An explicit yes the owner's rule asks (``asked_by == "rule"``) is read on the reply alone: in a
call of its own that carries no message before. One the judge or Domo asked may be read with
them.

The intent counts, not the formula (the owner's decision, 2026-11-15: in his trial of 0.8.0 only
the literal "je confirme" gave an explicit yes, and "oui, je t'ai déjà dit que c'était ok" did
not). A firm yes the reader read is recorded as an explicit yes given in words
(``explicit_phrase``), like the phrase. In doubt, the reader never says yes.

Who may answer a request is never the reader's to say: the code shows it only the requests the
reply just before showed this person, and decides who may decide each one.
"""

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final, Literal, cast, get_args

from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    JsonObject,
    ModelCallError,
    ToolCall,
)
from domo_core.policy.asks import Ask, AskedBy
from domo_core.prompts import sendable

PhraseMatch = Literal["explicit_yes", "yes", "no"]
Classification = Literal["firm", "yes", "no", "change", "unclear"]
Verdict = Literal["approve", "refuse", "change", "unclear"]
ReplySource = Literal["explicit_phrase", "phrase", "classifier"]

# The longest reply the reader is asked about: a longer one is not an answer to requests (and the
# reader is a cheap call, not a reader of essays).
MAX_CLASSIFIED_LENGTH: Final = 1000
# How many of the person's messages before the reply the reader is given, and how much of each.
READ_BEFORE: Final = 3
_BEFORE_LENGTH: Final = 300
# The reader answers one short line per request (asked without thinking first), and never more
# than a few lines' worth.
_TOKENS_PER_REQUEST: Final = 8
_MAX_OUTPUT_TOKENS: Final = 64
# One line of the reader's answer: a number, a colon, a word (a final period is forgiven).
_LINE = re.compile(r"(\d+)\s*:\s*([a-z]+)\.?")
_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(get_args(Classification))

# What ``normalize`` removes: the sentence punctuation (the three dots too, which NFKC turns into
# periods anyway).
_PUNCTUATION = re.compile(r"[.,!?;:…]")
# What ``normalize`` treats as an emoji, besides the symbols (``So``) and modifiers (``Sk``):
# the variation selectors that turn a character into its emoji form.
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)
_EMOJI_CATEGORIES = frozenset({"So", "Sk", "Cf"})


def normalize(text: str) -> str:
    """``text`` as the phrases are compared: NFKC, case folded, the typographic apostrophe made
    plain, the punctuation ``.,!?;:…`` and the emoji removed, and the spaces reduced to single
    ones between words."""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    kept = "".join(
        character
        for character in folded
        if unicodedata.category(character) not in _EMOJI_CATEGORIES
        and ord(character) not in _VARIATION_SELECTORS
    )
    return " ".join(_PUNCTUATION.sub(" ", kept).split())


def _phrases(*phrases: str) -> frozenset[str]:
    return frozenset(normalize(phrase) for phrase in phrases)


# A yes without anything else (D2), in French and English.
PHRASES_YES: Final = _phrases(
    "oui",
    "oui oui",
    "ouais",
    "ok",
    "okay",
    "ok merci",
    "oui merci",
    "oui vas-y",
    "ok vas-y",
    "vas-y",
    "vas y",
    "d'accord",
    "daccord",
    "c'est bon",
    "ça marche",
    "ca marche",
    "parfait",
    "fais-le",
    "fais le",
    "go",
    "yes",
    "yeah",
    "yep",
    "yes please",
    "sure",
    "do it",
    "go ahead",
    "alright",
    "all right",
    "sounds good",
)

# A no without anything else (D2), in French and English.
PHRASES_NO: Final = _phrases(
    "non",
    "non merci",
    "nan",
    "annule",
    "annuler",
    "laisse tomber",
    "surtout pas",
    "no",
    "no thanks",
    "no thank you",
    "nope",
    "cancel",
    "cancel it",
    "don't",
    "do not",
    "never mind",
    "nevermind",
)

# The explicit confirmation (D2): what an explicit yes accepts besides the button.
PHRASES_EXPLICIT: Final = _phrases(
    "je confirme",
    "oui je confirme",
    "confirmé",
    "confirmée",
    "i confirm",
    "yes i confirm",
    "confirmed",
)


def match_phrase(text: str) -> PhraseMatch | None:
    """Which phrase the **whole** message is, once normalised, if it is one: ``explicit_yes``,
    ``yes`` or ``no``. A message that holds a phrase and something else matches nothing."""
    phrase = normalize(text)
    if phrase in PHRASES_EXPLICIT:
        return "explicit_yes"
    if phrase in PHRASES_YES:
        return "yes"
    if phrase in PHRASES_NO:
        return "no"
    return None


@cache
def classifier_prompt() -> str:
    """The system prompt of the reader (``prompts/confirm_classifier.md``), read once."""
    path = resources.files("domo_core.prompts").joinpath("confirm_classifier.md")
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class ShownRequest:
    """A request the reply just before showed the person, as the reader is told of it: its
    ``number``, as the person saw it (from 1), what it does (``says``: never the words of a note,
    ``confirmations.describe.description_for_a_model``), what it asks (``ask``; ``None`` asks
    nothing, which a plain yes gives) and who asked it (``asked_by``)."""

    number: int
    says: str
    ask: Ask | None
    asked_by: AskedBy | None


@dataclass(frozen=True)
class Reading:
    """A reply read as an answer to one request: its ``verdict``, how it was recognised
    (``source``), and whether it was a yes that is not enough for what the request asks
    (``not_enough``: a plain yes for an explicit yes, any yes in words for a proof; the verdict is
    then ``unclear``, though the person did answer the request)."""

    verdict: Verdict
    source: ReplySource | None
    not_enough: bool = False


_UNCLEAR: Final = Reading("unclear", None)
_NOT_ENOUGH: Final = Reading("unclear", None, not_enough=True)


def _a_plain_yes_does(ask: Ask | None) -> bool:
    """Whether a plain yes in words gives what the request asks: a yes, or nothing asked."""
    return ask is None or ask == "yes"


def _read_alone(request: ShownRequest) -> bool:
    """Whether the request is read on the reply alone, with no message before it: an explicit
    yes the owner's rule asks."""
    return request.ask == "explicit_yes" and request.asked_by == "rule"


def _phrase_reading(phrase: PhraseMatch, ask: Ask | None) -> Reading:
    """What a phrase the code knows gives a request that asks ``ask``: "I confirm" approves, but
    a proof; a no refuses; a plain yes approves a yes asked (or nothing), and is not enough for a
    proof or an explicit yes (unless the reader finds it firm: ``read_replies``)."""
    match phrase:
        case "explicit_yes":
            return _NOT_ENOUGH if ask == "proof" else Reading("approve", "explicit_phrase")
        case "no":
            return Reading("refuse", "phrase")
        case "yes":
            return Reading("approve", "phrase") if _a_plain_yes_does(ask) else _NOT_ENOUGH


def _reading_of(word: Classification, ask: Ask | None) -> Reading:
    """What the reader's word gives a request that asks ``ask``: a firm yes gives an explicit yes
    (``explicit_phrase``) and any yes a plain one; a yes is never enough for a proof; ``no``
    refuses, ``change`` is a yes with a change, and the rest is unclear."""
    match word:
        case "firm" if ask == "explicit_yes":
            return Reading("approve", "explicit_phrase")
        case "firm" | "yes":
            return Reading("approve", "classifier") if _a_plain_yes_does(ask) else _NOT_ENOUGH
        case "no":
            return Reading("refuse", "classifier")
        case "change":
            return Reading("change", "classifier")
        case "unclear":
            return _UNCLEAR


def _lines(answer: str, numbers: Iterable[int]) -> dict[int, Classification]:
    """The reader's word for each request it was asked about, from its lines ``<n>: word``:
    anything else is ignored, and so is a line for a number it was not asked about. An unknown
    word, or two different words for one request, is ``unclear`` (in doubt, never yes)."""
    asked = set(numbers)
    said: dict[int, Classification] = {}
    for line in answer.splitlines():
        found = _LINE.fullmatch(line.strip().casefold())
        if found is None or int(found.group(1)) not in asked:
            continue
        number, word = int(found.group(1)), found.group(2)
        read = cast(Classification, word) if word in _CLASSIFICATIONS else "unclear"
        said[number] = read if said.get(number, read) == read else "unclear"
    return said


async def _ask_the_reader(
    model: ChatModel,
    text: str,
    requests: Sequence[ShownRequest],
    *,
    speaker: JsonObject | None,
    before: Sequence[str],
) -> dict[int, Classification]:
    """One call of the isolated reader about ``requests``: its word for each, as its lines say
    (``_lines``). The call holds one user message, a JSON object (data, whatever it says), with
    no tool, no thinking (a few tokens per line: a model that thinks first would spend them on
    it) and ``_TOKENS_PER_REQUEST`` tokens a request, ``_MAX_OUTPUT_TOKENS`` at most. A call that
    fails, or that answers with a tool call, gives no word."""
    given = {
        "speaker": speaker,
        "requests": [
            {"n": request.number, "says": request.says, "asks": request.ask or "yes"}
            for request in requests
        ],
        "before": [said[:_BEFORE_LENGTH] for said in before[-READ_BEFORE:]],
        "reply": text,
    }
    pieces: list[str] = []
    try:
        stream = model.stream(
            system=classifier_prompt(),
            messages=[ChatMessage("user", json.dumps(sendable(given), ensure_ascii=False))],
            max_output_tokens=min(_TOKENS_PER_REQUEST * len(requests), _MAX_OUTPUT_TOKENS),
            think=False,
        )
        async with aclosing(stream) as items:
            async for item in items:
                if isinstance(item, ToolCall):
                    return {}
                if isinstance(item, ChatChunk):
                    pieces.append(item.text)
    except ModelCallError:
        return {}
    return _lines("".join(pieces), (request.number for request in requests))


def reader_needed(text: str, shown: Sequence[ShownRequest]) -> bool:
    """Whether reading ``text`` as an answer to ``shown`` may take the reader: always, but for one
    request shown and a phrase that says all there is to say (not a plain yes to an explicit
    yes, which the reader may find firm from the messages before it; but for an explicit yes the
    owner's rule asks, read on the reply alone, where a bare yes can never be firm: the code says
    it is not enough, with no model)."""
    if len(shown) != 1:
        return bool(shown)
    (request,) = shown
    phrase = match_phrase(text)
    return phrase is None or (
        phrase == "yes" and request.ask == "explicit_yes" and not _read_alone(request)
    )


async def read_replies(
    text: str,
    shown: Sequence[ShownRequest],
    *,
    speaker: JsonObject | None,
    before: Sequence[str],
    model: ChatModel | None,
) -> tuple[Reading, ...]:
    """The person's reply ``text`` read as an answer to each request ``shown``, in order: one
    ``Reading`` each.

    One request shown and a phrase the code knows (``match_phrase``): the phrase answers, with no
    model (``_phrase_reading``), but for a plain yes to an explicit yes, which the reader is asked
    about: firm, it gives the explicit yes; anything else, or no reader, and it is not enough.
    Otherwise the isolated reader reads it (``_ask_the_reader``), given who speaks (``speaker``,
    ``Speaker.as_json``), the requests and the person's messages ``before`` the reply (the last
    ``READ_BEFORE``, each cut); an explicit yes the owner's rule asks is read in a call of its
    own, with no message before (``_read_alone``). A request the reader gave no word is unclear,
    and so is every one when there is no model (the key has none strong enough: the requests wait
    for their buttons) or the reply is over ``MAX_CLASSIFIED_LENGTH`` characters."""
    if not shown:
        return ()
    phrase = match_phrase(text) if len(shown) == 1 else None
    if phrase is not None and not reader_needed(text, shown):
        return (_phrase_reading(phrase, shown[0].ask),)
    if model is None or len(text) > MAX_CLASSIFIED_LENGTH:
        return tuple(_NOT_ENOUGH if phrase is not None else _UNCLEAR for _ in shown)
    words: dict[int, Classification] = {}
    together = [request for request in shown if not _read_alone(request)]
    if together:
        words |= await _ask_the_reader(model, text, together, speaker=speaker, before=before)
    for request in shown:
        if _read_alone(request):
            words |= await _ask_the_reader(model, text, [request], speaker=speaker, before=())
    if phrase is not None:
        # A plain yes to an explicit yes: firm, or not enough.
        firm = words.get(shown[0].number) == "firm"
        return (Reading("approve", "explicit_phrase") if firm else _NOT_ENOUGH,)
    return tuple(
        _reading_of(words.get(request.number, "unclear"), request.ask) for request in shown
    )
