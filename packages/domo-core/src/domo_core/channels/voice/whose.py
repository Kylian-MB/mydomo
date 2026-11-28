"""Is this still the page's person? One small question when the voice says nobody (audit case 5).

At a page signed in as someone, a sentence whose voice reads far from that person's and like
nobody else the house knows is not made a stranger's by the score alone: the quick model at home
(the task ``attention``'s, ``attention.heard_route``, counted like it) is asked ONE separate
question, given the page's person, the microphone, the last lines exchanged with that person at
this microphone in this visit and how long ago, and the sentence: is this plausibly that person
carrying on (hoarse, ill, shouting, far from the microphone), or someone else? It answers one word
(``theirs`` or ``someone_else``). Only ``theirs`` changes anything: the caller then takes the
sentence as the page person's, unsure, with the voice's clue against them kept for the judge.
When the model is silent, fails, takes longer than ``attention.ATTENTION_SECONDS`` or says
anything else, it is someone else: the stranger's path, as without this question.

The caller asks only once the code weighed the sentence against any open proof window and when
no phrase moment may take it: a spoken phrase never reaches this model. What the model is given
and what it answers are never logged: only the kind of a failure is.
"""

import json
import logging
import re
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Final

import anyio

from domo_core.accounts.models import Person
from domo_core.channels.voice.attention import (
    ATTENTION_SECONDS,
    LINE_CHARS,
    LINES_SHOWN,
    Exchanged,
    heard_route,
)
from domo_core.channels.voice.deps import VoiceDeps
from domo_core.llm.metered import MeteredModel
from domo_core.llm.types import ChatChunk, ChatMessage, ChatModel, JsonObject, ModelCallError
from domo_core.prompts import whose_voice_prompt

logger = logging.getLogger(__name__)

# The longest answer asked of the model, in tokens: one word.
WHOSE_OUTPUT_TOKENS: Final = 8
_THEIRS: Final = "theirs"
_WORD: Final = re.compile(r"[a-z_]+")


@dataclass(frozen=True)
class Asked:
    """What the question is given (see the module docstring). ``last_minutes_ago``: how long ago
    the last line of ``exchanged`` was, ``None`` when there is none."""

    sentence: str
    page_person: str
    microphone: str
    exchanged: Sequence[Exchanged]
    last_minutes_ago: int | None

    def __repr__(self) -> str:
        # What was said never reaches a log by a repr.
        return f"Asked(microphone={self.microphone!r})"

    def as_json(self) -> JsonObject:
        return {
            "page_person": self.page_person,
            "microphone": self.microphone,
            "exchanged": [
                {"by": line.by, "text": line.text[:LINE_CHARS]}
                for line in self.exchanged[-LINES_SHOWN:]
            ],
            "last_minutes_ago": self.last_minutes_ago,
            "sentence": self.sentence,
        }


def read_answer(answer: str) -> bool:
    """Whether the model's ``answer`` says ``theirs`` (its first word); anything else is not."""
    words = _WORD.findall(answer.strip().lower())
    return bool(words) and words[0] == _THEIRS


class WhoseVoice:
    """The question with ``model`` (see the module docstring)."""

    def __init__(self, model: ChatModel) -> None:
        self._model = model

    async def theirs(self, asked: Asked) -> bool:
        """Whether the model says the sentence is the page person's; ``False`` on any failure."""
        pieces: list[str] = []
        given = json.dumps(asked.as_json(), ensure_ascii=False)
        with anyio.move_on_after(ATTENTION_SECONDS):
            try:
                stream = self._model.stream(
                    system=whose_voice_prompt(),
                    messages=[ChatMessage("user", given)],
                    max_output_tokens=WHOSE_OUTPUT_TOKENS,
                    think=False,
                )
                async with aclosing(stream) as items:
                    async for item in items:
                        if isinstance(item, ChatChunk):
                            pieces.append(item.text)
            except ModelCallError as error:
                logger.info("Whose voice it is could not be asked (%s)", error.kind)
                return False
            return read_answer("".join(pieces))
        logger.info("Whose voice it is took too long to ask")
        return False


async def open_whose_voice(deps: VoiceDeps, person: Person) -> WhoseVoice | None:
    """The question for a page of ``person``, on the quick judgement's model they may use, counted
    for them (``MeteredModel``); ``None`` when there is none."""
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
    return WhoseVoice(model)
