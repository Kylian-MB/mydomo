"""Is this for him? Should he stop? The quick judgement on a sentence heard (spec 6.6).

Nothing here is decided by the code: a small model at home (the task ``attention``, chosen by the
assistant as any other, ``llm.speech.routes.speech_route``: at home unless the owner lets what is
heard go out, ``speech.hear_outside``) is given the sentence, who probably said it and how sure
the house is, the name he is called by there (the person's own for him, else the house's),
the microphone that heard it and its place when the house knows one, the last lines
exchanged with that person, and where the assistant's voice is being heard right now
(``Moment``). It answers on one line whether the sentence was said to him and, when he was
speaking, what his voice does: goes quiet and takes the sentence as the next message
(``stop``), goes on (``resume``), or goes quiet with nothing more (``drop``). No word is looked
for in the code: whatever the language and the words, the model understands them.

When the model does not answer in ``ATTENTION_SECONDS``, fails, or says something unreadable, the
sentence is taken as said to him and, when he was speaking, he goes on (``resume``): he never
falls silent by reflex. With no such model at all (``open_attention`` gives ``None``), the voice
session decides what the house does (the owner is asked).

What the model is given and what it answers are never logged: only the kind of a failure is.
"""

import json
import logging
import re
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Final, Literal, Protocol, get_args

import anyio

from domo_core.accounts.models import Person
from domo_core.channels.voice.deps import VoiceDeps
from domo_core.llm.catalog import Catalog
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.metered import MeteredModel
from domo_core.llm.router import Route
from domo_core.llm.speech.routes import speech_route
from domo_core.llm.types import ChatChunk, ChatMessage, ChatModel, JsonObject, ModelCallError
from domo_core.naming import DEFAULT_NAME
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import attention_prompt

logger = logging.getLogger(__name__)

# How long the quick judgement may take, in seconds: past it, the sentence is his and he goes on.
ATTENTION_SECONDS: Final = 2.0
# The longest answer asked of the model, in tokens: one short line.
ATTENTION_OUTPUT_TOKENS: Final = 16
# How many of the last lines exchanged it is given, and the longest of each, in characters.
LINES_SHOWN: Final = 4
LINE_CHARS: Final = 400

# What the assistant's voice does once a sentence was heard while he spoke.
VoiceStep = Literal["stop", "resume", "drop"]
_STEPS: Final[tuple[VoiceStep, ...]] = get_args(VoiceStep)
# The model's word for "said to him", and for "not said to him".
_MINE: Final = "mine"
_NOT_MINE: Final = "not_mine"
_WORD: Final = re.compile(r"[a-z_]+")

# The tasks of talk about what is heard, which stay at home like the sound.
HeardTask = Literal["attention", "heard_sorting"]


@dataclass(frozen=True)
class Judged:
    """What the quick judgement says of a sentence: whether it was said to him (``for_him``),
    and what his voice does (``voice``; read only when he was speaking)."""

    for_him: bool
    voice: VoiceStep | None


@dataclass(frozen=True)
class Exchanged:
    """One of the last lines between the person and the assistant: who said it (``person`` or
    ``assistant``) and what."""

    by: Literal["person", "assistant"]
    text: str = ""

    def __repr__(self) -> str:
        # What was said never reaches a log by a repr.
        return f"Exchanged(by={self.by!r})"


@dataclass(frozen=True)
class Moment:
    """What the quick judgement is given (see the module docstring). ``who`` is the name of the
    person who probably spoke (``None``: nobody known), ``standing`` how sure the house is
    (``sure``, ``unsure``, ``unknown``), ``place`` the place of the microphone when the house
    knows one, ``speaking_at`` the microphones where the assistant's voice is heard now (none:
    he is silent). ``name`` is the name the assistant is called by there (``naming.name_shown``:
    the person's own for him, else the house's), the name to listen for."""

    sentence: str
    who: str | None
    standing: str
    microphone: str
    exchanged: Sequence[Exchanged]
    speaking_at: Sequence[str]
    place: str | None = None
    name: str = DEFAULT_NAME

    def __repr__(self) -> str:
        return f"Moment(microphone={self.microphone!r}, speaking={self.speaking})"

    @property
    def speaking(self) -> bool:
        """Whether the assistant's voice is being heard somewhere now."""
        return bool(self.speaking_at)

    def as_json(self) -> JsonObject:
        heard: JsonObject = {"microphone": self.microphone}
        if self.place is not None:
            heard["place"] = self.place
        return {
            "sentence": self.sentence,
            "his_name": self.name,
            "who": self.who,
            "standing": self.standing,
            **heard,
            "exchanged": [
                {"by": line.by, "text": line.text[:LINE_CHARS]}
                for line in self.exchanged[-LINES_SHOWN:]
            ],
            "speaking_at": [{"microphone": microphone} for microphone in self.speaking_at],
        }


def fallback(moment: Moment) -> Judged:
    """What holds when the judgement cannot be had: the sentence is his; he goes on when he was
    speaking (never silent by reflex), else nothing of his voice changes."""
    return Judged(True, "resume" if moment.speaking else "stop")


def read_judgement(answer: str, moment: Moment) -> Judged | None:
    """The judgement in the model's ``answer``: its first line that says something, ``mine`` or
    ``not_mine``, then (when he was speaking) ``stop``, ``resume`` or ``drop``; a speaking moment
    with no word for the voice goes on (``resume``). ``None`` when the line says neither."""
    line = next((line for line in answer.strip().splitlines() if line.strip()), "")
    words = _WORD.findall(line.lower())
    if not words or words[0] not in (_MINE, _NOT_MINE):
        return None
    for_him = words[0] == _MINE
    if not moment.speaking:
        return Judged(for_him, None)
    for word in words[1:]:
        for step in _STEPS:
            if word == step:
                return Judged(for_him, step)
    return Judged(for_him, "resume")


class Attention(Protocol):
    """What weighs a sentence heard (see the module docstring)."""

    async def weigh(self, moment: Moment) -> Judged: ...


class QuickAttention:
    """The quick judgement with ``model`` (see the module docstring)."""

    def __init__(self, model: ChatModel) -> None:
        self._model = model

    async def weigh(self, moment: Moment) -> Judged:
        pieces: list[str] = []
        asked = json.dumps(moment.as_json(), ensure_ascii=False)
        with anyio.move_on_after(ATTENTION_SECONDS):
            try:
                stream = self._model.stream(
                    system=attention_prompt(),
                    messages=[ChatMessage("user", asked)],
                    max_output_tokens=ATTENTION_OUTPUT_TOKENS,
                    think=False,
                )
                async with aclosing(stream) as items:
                    async for item in items:
                        if isinstance(item, ChatChunk):
                            pieces.append(item.text)
            except ModelCallError as error:
                logger.info("The quick judgement could not be had (%s)", error.kind)
                return fallback(moment)
            judged = read_judgement("".join(pieces), moment)
            if judged is None:
                logger.info("The quick judgement was unreadable")
                return fallback(moment)
            return judged
        logger.info("The quick judgement took too long")
        return fallback(moment)


async def heard_route(
    deps: VoiceDeps, person: Person, task: HeardTask
) -> tuple[Route, Catalog] | None:
    """The route of ``task`` among the keys ``person`` may use, at home unless the owner lets what
    is heard go out (``speech_route``), with the catalog it was chosen with; ``None`` when no model
    there is fit for it."""
    async with deps.turn.session_factory() as session:
        settings = await load_policy_settings(session)
        catalog = await current_catalog(session)
        route = await speech_route(
            session,
            person,
            task,
            catalog=catalog,
            settings=settings,
            at_home=deps.speech.at_home,
        )
    return None if route is None else (route, catalog)


async def open_attention(deps: VoiceDeps, person: Person) -> QuickAttention | None:
    """The quick judgement for a page of ``person``, on the model of the task ``attention`` they
    may use, counted for them (``MeteredModel``); ``None`` when there is none."""
    found = await heard_route(deps, person, "attention")
    if found is None:
        return None
    route, catalog = found
    turn = deps.turn
    model = MeteredModel(
        turn.model_factory(route.provider, route.model_id),
        route=route,
        person_id=person.id,
        session_factory=turn.session_factory,
        clock=turn.clock,
        catalog=catalog,
    )
    return QuickAttention(model)
