"""A reply says a thing once (F3, the owner's trial of 0.9.0).

The model wrote a sentence, called a silent tool, then wrote the same sentence again in the next
round; both were kept in the one reply ("Alex left you a note: ..." twice). The stored reply, which
``/since``, a reload and the history read, keeps it once; the live stream cannot be taken back.
"""

import json
from collections.abc import AsyncGenerator, Sequence

from chat_support import ChatPilot, is_text_call
from domo_core.conversation.once import said_once
from domo_core.llm.types import (
    ChatChunk,
    ChatUsage,
    HistoryItem,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from fakes import JUDGE_DEFAULT, FakeChatModel, is_judge_call

NOTE = "Alex left you a note: he will look into it so that I can control the house."


# --- what is kept once --------------------------------------------------------------------------


def test_a_round_that_says_the_same_again_goes():
    assert said_once([NOTE, f"\n\n{NOTE}"]) == NOTE
    # Spaces, case and the final mark do not make it another thing.
    assert said_once(["All right, noted.", "\n\nall  right, NOTED!"]) == "All right, noted."


def test_a_paragraph_said_again_goes_and_the_rest_stays():
    first = "Alex left you a note: he will look into it.\n\nAnything else?"
    later = "\n\nAlex left you a note: he will look into it.\n\nShall I remind you tonight?"
    assert said_once([first, later]) == f"{first}\n\nShall I remind you tonight?"


def test_a_short_paragraph_said_again_stays():
    # Shorter than a sentence of prose: "Noted." twice is not taken for a repeat.
    first = "Noted.\n\nI will avoid pine nuts for him."
    later = "\n\nNoted.\n\nAnything else for tonight?"
    assert said_once([first, later]) == first + later


def test_different_sentences_are_both_kept():
    rounds = ["All right, noted, I forget that.", "\n\nDone, I have forgotten this."]
    assert said_once(rounds) == "".join(rounds)
    # A sentence said again inside a paragraph that says more stays: nothing is rewritten.
    rounds = ["Noted. I will avoid pine nuts for him.", "\n\nNoted. Anything else for tonight?"]
    assert said_once(rounds) == "".join(rounds)


def test_what_repeats_inside_one_round_is_untouched():
    one = "Shopping list:\n- Milk.\n- Bread.\n- Milk."
    assert said_once([one]) == one
    assert said_once([one, "\n\nAnything else?"]) == one + "\n\nAnything else?"


def test_lists_of_two_rounds_are_never_touched():
    # The review's probes: a line of a list said in both rounds is no repeat.
    numbered = ["Liste d'Alex :\n1. Pain\n2. Lait\n", "Liste de Sam :\n1. Oeufs\n2. Lait\n"]
    assert said_once(numbered) == "".join(numbered)
    dashed = ["Alex : \n- lait\n- pain\n", "Sam :\n- lait\n- oeufs"]
    assert said_once(dashed) == "".join(dashed)
    # Even a whole list said again in its own paragraph stays: only prose may go.
    listed = ["Here it is:\n\n- milk\n- bread", "\n\nAgain:\n\n- milk\n- bread"]
    assert said_once(listed) == "".join(listed)


def test_exact_words_quoted_twice_stay():
    quoted = "Her exact words: « Je rentre tard, ne m'attendez pas pour dîner. »"
    assert said_once([quoted, f"\n\n{quoted}"]) == f"{quoted}\n\n{quoted}"
    straight = 'Her exact words: "I will be late, do not wait for me for dinner."'
    assert said_once([straight, f"\n\n{straight}"]) == f"{straight}\n\n{straight}"


def test_a_first_round_without_words_changes_nothing():
    assert said_once(["", NOTE, f"\n\n{NOTE}"]) == NOTE
    assert said_once([]) == ""


# --- in a turn ----------------------------------------------------------------------------------


class RoundsModel:
    """A model whose answers that may call tools say, round by round, what ``rounds`` gives (texts
    and calls, in order); a later one says nothing. The judge says yes, the classifier
    "unclear", the writer and checker of the texts as ``FakeChatModel`` answers them."""

    def __init__(self, rounds: Sequence[Sequence[str | ToolCall]]) -> None:
        self._rounds = [list(r) for r in rounds]
        self._texts = FakeChatModel()

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
        if is_text_call(system):
            async for item in self._texts.stream(
                system=system,
                messages=messages,
                max_output_tokens=max_output_tokens,
                tools=tools,
                tool_choice=tool_choice,
            ):
                yield item
            return
        if is_judge_call(system):
            yield ChatChunk(JUDGE_DEFAULT)
        elif not tools:
            yield ChatChunk("unclear")
        elif self._rounds:
            for item in self._rounds.pop(0):
                yield ChatChunk(item) if isinstance(item, str) else item
        else:
            yield ChatChunk("")
        yield ChatUsage(3, 2)


def _keep(call_id: str) -> ToolCall:
    arguments = {
        "content": "Alex does not like pine nuts.",
        "subject": "the kitchen",
        "kind": "preference",
        "private": False,
        "core": False,
    }
    return ToolCall(call_id, "memory_keep", json.dumps(arguments))


async def test_the_stored_reply_says_once_what_two_rounds_said(owner_chat: ChatPilot):
    owner_chat.use_model(RoundsModel([[NOTE, _keep("k1")], [NOTE]]))
    reply = await owner_chat.say("Anything new?")
    assert reply.actions == [{"tool": "memory_keep", "status": "done"}]
    # The live stream said it twice (it cannot be taken back); what is kept says it once.
    streamed = "".join(event.data for event in reply.events if event.event == "delta")
    assert streamed.count("Alex left you a note") == 2
    assert reply.text == NOTE
    # And what the page reads again of it, on a reload.
    page = (await owner_chat.get("/chat")).text
    assert page.count("Alex left you a note") == 1


async def test_two_different_sentences_around_a_tool_are_both_kept(owner_chat: ChatPilot):
    owner_chat.use_model(
        RoundsModel([["All right, noted.", _keep("k1")], ["I will avoid them for him."]])
    )
    reply = await owner_chat.say("He does not like pine nuts.")
    assert reply.text == "All right, noted.\n\nI will avoid them for him."
