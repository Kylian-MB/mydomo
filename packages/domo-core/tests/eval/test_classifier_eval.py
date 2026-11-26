"""How well the isolated reader understands real replies, on a real model.

The fake model of the other tests cannot tell whether the reader understands an intention:
this evaluation asks a real model about the replies of ``confirmation_replies.json`` (French and
English, in many wordings, spread over the four answers). It is a development tool, outside the
stack: it builds ``OpenAICompatChat`` directly and is skipped unless ``EVAL_BASE_URL`` and
``EVAL_MODEL`` are set (``EVAL_API_KEY`` is optional, for a local model). Run it with
``uv run pytest -m live packages/domo-core/tests/eval`` (see CONTRIBUTING.md).

The bar: at least 90% right, and **never** a ``yes`` for a reply that meant a change, a no or
nothing clear, since that is the error that would run an action the person wanted otherwise.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from domo_core.confirmations.intent import (
    MAX_CLASSIFIED_LENGTH,
    Reading,
    ShownRequest,
    read_replies,
)
from domo_core.llm.adapters.openai_compat import OpenAICompatChat
from domo_core.llm.types import ChatModel
from domo_core.policy.asks import Ask

REPLIES = Path(__file__).with_name("confirmation_replies.json")
ANSWERS = ("yes", "no", "change", "unclear")
# Who speaks, as the reader is told it: the page, with nothing known of the speaker.
SPEAKER = {"channel": "web", "clues": [], "last_heard_minutes_ago": None}


async def read(model: ChatModel, text: str, ask: Ask) -> str:
    """The answer the reader gives ``text``, as a reply to one request that asks ``ask``, in the
    words of the evaluation: a firm yes (``firm``, an explicit yes given), a yes, a no, a change or
    unclear. A phrase the code knows is read by the code, as in a conversation."""
    shown = [ShownRequest(number=1, says="Rename you to Alex B", ask=ask, asked_by="judge")]
    (reading,) = await read_replies(text, shown, speaker=SPEAKER, before=(), model=model)
    return _answer_of(reading)


def _answer_of(reading: Reading) -> str:
    if reading.not_enough:
        return "yes"
    if reading.verdict == "approve":
        return "firm" if reading.source == "explicit_phrase" else "yes"
    return {"refuse": "no", "change": "change"}.get(reading.verdict, "unclear")


@pytest.fixture
def eval_replies() -> list[dict[str, str]]:
    """The replies of the evaluation, each with the answer it should get."""
    return json.loads(REPLIES.read_text(encoding="utf-8"))


@pytest.fixture
def live_model() -> ChatModel:
    """The model the evaluation asks: an OpenAI-compatible endpoint, from ``EVAL_*``."""
    base_url = os.environ.get("EVAL_BASE_URL")
    model = os.environ.get("EVAL_MODEL")
    if not base_url or not model:
        pytest.skip("set EVAL_BASE_URL and EVAL_MODEL (and EVAL_API_KEY if needed) to run it")
    return OpenAICompatChat(
        base_url=base_url,
        token=os.environ.get("EVAL_API_KEY") or "not-needed",
        model=model,
        token_limit="max_tokens",
    )


@pytest.mark.live
async def test_classifier_understands_varied_replies(live_model, eval_replies):
    results = [(case, await read(live_model, case["text"], "yes")) for case in eval_replies]
    wrong = [(c["text"], c["expected"], got) for c, got in results if got != c["expected"]]
    unsafe = [w for w in wrong if w[2] == "yes"]
    assert not unsafe, unsafe
    assert len(wrong) <= len(results) // 10, wrong


def test_the_replies_cover_what_the_evaluation_needs(eval_replies: list[dict[str, Any]]):
    """At least 60 replies, each a text and one of the four answers, every answer well
    represented, in French and in English, and no reply twice."""
    assert len(eval_replies) >= 60
    assert all(set(case) == {"text", "expected"} for case in eval_replies)
    assert all(case["expected"] in ANSWERS for case in eval_replies)
    for answer in ANSWERS:
        assert sum(case["expected"] == answer for case in eval_replies) >= 12, answer
    texts = [case["text"] for case in eval_replies]
    assert len(set(texts)) == len(texts)
    assert all(0 < len(text) <= MAX_CLASSIFIED_LENGTH for text in texts)  # what the reader reads
    french = sum(any(c in text for c in "éèàçù") or " pas " in text for text in texts)
    assert french >= 20 and len(texts) - french >= 20


# --- a firm yes (a request that asks an explicit yes) ---------------------------------------------

EXPLICIT_REPLIES = Path(__file__).with_name("explicit_replies.json")
EXPLICIT_ANSWERS = ("firm", *ANSWERS)


@pytest.fixture
def explicit_replies() -> list[dict[str, str]]:
    """The replies to a request that asks an explicit yes, each with the answer it should get."""
    return json.loads(EXPLICIT_REPLIES.read_text(encoding="utf-8"))


@pytest.mark.live
async def test_classifier_tells_a_firm_yes_from_a_bare_one(live_model, explicit_replies):
    """The bar: at least 90% right, and **never** a ``firm`` for a reply that was not one, since
    that is the error that would run what asked for more than a bare yes."""
    results = [
        (case, await read(live_model, case["text"], "explicit_yes")) for case in explicit_replies
    ]
    wrong = [(c["text"], c["expected"], got) for c, got in results if got != c["expected"]]
    unsafe = [w for w in wrong if w[2] == "firm"]
    assert not unsafe, unsafe
    assert len(wrong) <= len(results) // 10, wrong


def test_the_replies_to_an_explicit_request_cover_what_the_evaluation_needs(
    explicit_replies: list[dict[str, Any]],
):
    """Each a text and one of the five answers, a firm yes and a bare one well represented, in
    French and in English, and no reply twice."""
    assert len(explicit_replies) >= 30
    assert all(set(case) == {"text", "expected"} for case in explicit_replies)
    assert all(case["expected"] in EXPLICIT_ANSWERS for case in explicit_replies)
    for answer, least in (("firm", 10), ("yes", 8), ("no", 3), ("change", 3), ("unclear", 3)):
        assert sum(case["expected"] == answer for case in explicit_replies) >= least, answer
    texts = [case["text"] for case in explicit_replies]
    assert len(set(texts)) == len(texts)
    assert all(0 < len(text) <= MAX_CLASSIFIED_LENGTH for text in texts)
    # The owner's own words, which only the literal "je confirme" used to pass.
    firm = {case["text"] for case in explicit_replies if case["expected"] == "firm"}
    assert "oui je t'ai déjà dit que c'était ok" in firm
