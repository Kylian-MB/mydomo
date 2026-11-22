"""Reading a person's answer to a pending request: yes, no, a yes with a change, or unclear.

This is the plan's decision D2. The answer comes from the person's own words, and never from the
main model:

1. the code first compares the **whole** message, once normalised (``normalize``), with short
   phrases it knows in French and English (``match_phrase``): "oui", "non merci", "I confirm".
   A phrase only counts when it is the entire message: "oui mais mets B" is not a "oui";
2. otherwise an isolated classifier reads it (``classify_reply``): a call to a model of the key
   of the request, of medium level or stronger (``llm.picker``, kind of task ``intent``; without
   one the answer is ``unclear``), with a prompt of its own (``prompts/confirm_classifier.md``)
   that is given **only** the person's message, with no history, no tool and no external
   content, and whose answer is read strictly (one word: ``yes``, ``no`` or ``change``; anything
   else is ``unclear``).

``read_reply`` puts the two together for a request, by what it asks (``ask``, from
``confirmations.store.ask_of``): a yes asked (or nothing) is given by any yes; an explicit yes
by a firm yes, whatever its words (or the button): "I confirm" is the phrase the code knows, and
for any other wording the classifier is asked, with a prompt of its own for such a request
(``prompts/confirm_classifier_explicit.md``, one more word: ``firm``), whether the yes is a firm,
deliberate one ("yes, go ahead, I am sure") or a bare one ("ok"), which is ``unclear``
(``not_enough``) as a known plain yes is; a proof is never given in words, however firm. A
refusal and a yes with a change are read the same way whatever is asked.

The intent counts, not the formula (the owner's decision, 2026-11-15: in his trial of 0.8.0 only
the literal "je confirme" gave an explicit yes, and "oui, je t'ai déjà dit que c'était ok" did
not). A firm yes the classifier read is recorded as an explicit yes given in words
(``explicit_phrase``), like the phrase.
"""

import re
import unicodedata
from contextlib import aclosing
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final, Literal

from domo_core.llm.types import ChatChunk, ChatMessage, ChatModel, ModelCallError, ToolCall
from domo_core.policy.asks import Ask

PhraseMatch = Literal["explicit_yes", "yes", "no"]
Classification = Literal["firm", "yes", "no", "change", "unclear"]
Verdict = Literal["approve", "refuse", "change", "unclear"]
ReplySource = Literal["explicit_phrase", "phrase", "classifier"]

# The longest reply the classifier is asked about: a longer one is not a short answer to a
# request (and the classifier is a cheap call, not a reader of essays).
MAX_CLASSIFIED_LENGTH: Final = 300
# The classifier answers in one word (asked without thinking first: ``ChatModel.stream``).
_CLASSIFIER_OUTPUT_TOKENS: Final = 4

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
    """The system prompt of the classifier (``prompts/confirm_classifier.md``), read once."""
    path = resources.files("domo_core.prompts").joinpath("confirm_classifier.md")
    return path.read_text(encoding="utf-8").strip()


@cache
def explicit_classifier_prompt() -> str:
    """The system prompt of the classifier for a request that asks an explicit yes
    (``prompts/confirm_classifier_explicit.md``), read once: the same first line and the same
    rules, with one more word, ``firm``, for a yes the person means."""
    path = resources.files("domo_core.prompts").joinpath("confirm_classifier_explicit.md")
    return path.read_text(encoding="utf-8").strip()


async def classify_reply(
    model: ChatModel | None, text: str, *, explicit: bool = False
) -> Classification:
    """What the person meant, as the isolated classifier reads it.

    The call holds one user message, the person's text, under the classifier's prompt, with no
    tool, ``max_output_tokens=4`` and ``think=False`` (one word: a model that thinks first would
    spend its four tokens on it and say nothing). Its answer, normalised, must be exactly
    ``yes``, ``no`` or ``change``; anything else is ``unclear``, and so is a call that fails or
    that answers with a tool call. A text over ``MAX_CLASSIFIED_LENGTH`` characters is
    ``unclear`` without a call, and so is any text when there is no model (the key of the request
    has none strong enough: the request waits for its button).

    For a request that asks an explicit yes (``explicit``), the prompt is the one that also tells
    a firm yes from a bare one (``explicit_classifier_prompt``), and ``firm`` is one more answer;
    it is ``unclear`` like any other unexpected word when the request asks none.
    """
    if model is None or len(text) > MAX_CLASSIFIED_LENGTH:
        return "unclear"
    pieces: list[str] = []
    try:
        stream = model.stream(
            system=explicit_classifier_prompt() if explicit else classifier_prompt(),
            messages=[ChatMessage("user", text)],
            max_output_tokens=_CLASSIFIER_OUTPUT_TOKENS,
            # One word: thinking first would take the few tokens it has.
            think=False,
        )
        async with aclosing(stream) as items:
            async for item in items:
                if isinstance(item, ToolCall):
                    return "unclear"
                if isinstance(item, ChatChunk):
                    pieces.append(item.text)
    except ModelCallError:
        return "unclear"
    answer = normalize("".join(pieces))
    if answer == "firm" and explicit:
        return "firm"
    if answer == "yes":
        return "yes"
    if answer == "no":
        return "no"
    if answer == "change":
        return "change"
    return "unclear"


@dataclass(frozen=True)
class Reading:
    """A reply read as an answer to one pending request: its ``verdict``, how it was recognised
    (``source``), and whether it was a yes that is not enough for what the request asks
    (``not_enough``: a plain yes for an explicit yes, any yes in words for a proof; the verdict is
    then ``unclear``, though the person did answer the request)."""

    verdict: Verdict
    source: ReplySource | None
    not_enough: bool = False


def _a_plain_yes_does(ask: Ask | None) -> bool:
    """Whether a plain yes in words gives what the request asks: a yes, or nothing asked."""
    return ask is None or ask == "yes"


async def read_reply_in_full(text: str, ask: Ask | None, model: ChatModel | None) -> Reading:
    """``read_reply``, with ``not_enough`` besides (see ``Reading``)."""
    match match_phrase(text):
        case "explicit_yes":
            if ask == "proof":
                return Reading("unclear", None, not_enough=True)
            return Reading("approve", "explicit_phrase")
        case "no":
            return Reading("refuse", "phrase")
        case "yes":
            if _a_plain_yes_does(ask):
                return Reading("approve", "phrase")
            return Reading("unclear", None, not_enough=True)
        case None:
            pass
    match await classify_reply(model, text, explicit=ask == "explicit_yes"):
        case "firm":
            # Asked of a request that asks an explicit yes only: their words give it.
            return Reading("approve", "explicit_phrase")
        case "no":
            return Reading("refuse", "classifier")
        case "change":
            return Reading("change", "classifier")
        case "yes":
            if _a_plain_yes_does(ask):
                return Reading("approve", "classifier")
            return Reading("unclear", None, not_enough=True)
        case _:
            return Reading("unclear", None)


async def read_reply(
    text: str, ask: Ask | None, model: ChatModel | None
) -> tuple[Verdict, ReplySource | None]:
    """The verdict of a reply to the one pending request, which asks ``ask``, and how it was
    recognised.

    1. "I confirm" approves (``explicit_phrase``), but for a proof (``unclear``); a known no
       refuses (``phrase``);
    2. a known yes approves (``phrase``) when a yes is asked (or nothing), and is ``unclear`` for
       an explicit yes or a proof;
    3. any other message goes to the classifier: ``no`` refuses, ``change`` is a yes with a
       change, ``yes`` approves when a yes is asked (or nothing) only, ``firm`` (a firm yes, which
       it is asked about for a request that asks an explicit yes) approves that request
       (``explicit_phrase``), and the rest is ``unclear``.

    An ``unclear`` verdict has no source: nothing was recognised.
    """
    reading = await read_reply_in_full(text, ask, model)
    return reading.verdict, reading.source
