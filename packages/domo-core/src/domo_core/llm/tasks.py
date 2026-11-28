"""The kinds of tasks a model is asked to do, and what each needs.

``start`` is the level a task begins with; ``minimum`` the level it never goes under (``None`` for
no floor). The protected tasks (the yes judge, the reading of a yes in words, the writing of
rights, the review of a made tool) never take a model under ``medium``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from domo_core.llm.levels import Level

TaskKind = Literal[
    "conversation",
    "judge",
    "intent",
    "rights_writer",
    "key_check",
    "upkeep",
    "writing",
    "checking",
    "arrival",
    "memory",
    "toolmaking",
    "tool_review",
    "hear",
    "say",
    "voice_print",
    "attention",
    "heard_sorting",
]

# What a model does for a task: talk (a chat model), or one of the jobs of sound.
Does = Literal["chat", "hear", "say", "voice_print"]


@dataclass(frozen=True)
class TaskSpec:
    needs_tools: bool
    start: Level
    minimum: Level | None
    does: Does = "chat"


TASKS: Final[Mapping[TaskKind, TaskSpec]] = MappingProxyType(
    {
        "conversation": TaskSpec(True, "medium", None),
        "judge": TaskSpec(False, "medium", "medium"),
        "intent": TaskSpec(False, "medium", "medium"),
        "rights_writer": TaskSpec(True, "medium", "medium"),
        "key_check": TaskSpec(True, "small", None),
        "upkeep": TaskSpec(True, "small", None),
        "writing": TaskSpec(True, "small", None),
        "checking": TaskSpec(True, "medium", None),
        "arrival": TaskSpec(True, "small", None),
        # Going back over a conversation to sort what Domo remembers (``memory.review``).
        "memory": TaskSpec(True, "small", None),
        # Writing a tool's code in a build, and the second look at it before it is kept.
        "toolmaking": TaskSpec(True, "medium", None),
        "tool_review": TaskSpec(False, "medium", "medium"),
        # Sound: a speech model hears, says or tells a voice; the last two are chat tasks.
        "hear": TaskSpec(False, "small", None, does="hear"),
        "say": TaskSpec(False, "small", None, does="say"),
        "voice_print": TaskSpec(False, "small", None, does="voice_print"),
        # Whether a heard phrase is for Domo, and the sorting of what was heard.
        "attention": TaskSpec(False, "small", None),
        "heard_sorting": TaskSpec(True, "small", None),
    }
)

PROTECTED: Final[frozenset[TaskKind]] = frozenset(
    {"judge", "intent", "rights_writer", "tool_review"}
)

# What is heard never leaves the house unless the owner says so (``speech.hear_outside``).
HOME_ONLY: Final[frozenset[TaskKind]] = frozenset(
    {"hear", "voice_print", "attention", "heard_sorting"}
)
