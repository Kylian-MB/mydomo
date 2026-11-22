"""The first-model check: does a model answer?"""

from domo_core.llm.types import ChatMessage, ChatModel

PROBE_SYSTEM = "Reply with the single word: pong."
PROBE_MESSAGE = ChatMessage("user", "ping")
PROBE_MAX_OUTPUT_TOKENS = 16


async def probe_model(model: ChatModel) -> None:
    """Ask ``model`` for one word and read the whole answer.

    Returns when the model answered, whatever it said (even nothing: a model that thinks anyway
    and is cut before its first word did answer the call). The ``ModelCallError`` of a model that
    cannot be reached, refuses its key or the request, or fails, is not caught.
    """
    async for _ in model.stream(
        system=PROBE_SYSTEM,
        messages=[PROBE_MESSAGE],
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        # One word: a model that thinks first would spend its few tokens on it.
        think=False,
    ):
        pass
