"""Reading a person's answer to a pending request (decision D2): the phrases the code knows, and
the isolated classifier that reads everything else."""

import pytest
from domo_core.confirmations.intent import (
    PHRASES_EXPLICIT,
    PHRASES_NO,
    PHRASES_YES,
    classifier_prompt,
    classify_reply,
    explicit_classifier_prompt,
    match_phrase,
    normalize,
    read_reply,
    read_reply_in_full,
)
from domo_core.llm.types import ChatMessage, ToolCall
from fakes import FakeChatModel


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Oui !", "yes"),
        ("OK.", "yes"),
        ("d’accord", "yes"),
        ("Vas-y", "yes"),
        ("yes", "yes"),
        ("Je confirme.", "explicit_yes"),
        ("confirmed", "explicit_yes"),
        ("Non merci", "no"),
        ("cancel", "no"),
        ("oui mais mets B", None),
        ("ok pour demain ?", None),
        ("", None),
    ],
)
def test_phrases_match_whole_messages_only(text, expected):
    assert match_phrase(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "oui mais mets plutôt B",
        "c'est ok, mais change le titre",
        "Tu peux l'envoyer après avoir corrigé la date",
        "envoie-le, juste avec Sam en plus",
        "ok go mais pas avant 18h",
        "non, plutôt demain",
        "yes but make it 8pm",
    ],
)
async def test_changes_in_any_wording_reach_the_classifier(text):  # Review Focus 6
    model = FakeChatModel(["change"])
    assert match_phrase(text) is None
    assert await read_reply(text, "natural", model) == ("change", "classifier")
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    "output,expected",
    [
        ("yes", "yes"),
        ("Yes.", "yes"),
        ("no", "no"),
        ("change", "change"),
        ("yes, but", "unclear"),
        ("maybe", "unclear"),
    ],
)
async def test_classifier_output_is_read_strictly(output, expected):
    assert await classify_reply(FakeChatModel([output]), "ça marche pour moi") == expected


async def test_classifier_failure_means_unclear():
    model = FakeChatModel([], fail_after=0, error_kind="unreachable")
    assert await classify_reply(model, "bof") == "unclear"


async def test_long_replies_skip_the_classifier():
    model = FakeChatModel(["yes"])
    assert await classify_reply(model, "x" * 301) == "unclear" and not model.calls


async def test_without_a_model_strong_enough_the_words_are_unclear():
    # The key of the request has no model of medium level or stronger: the request waits for its
    # button. A known phrase still answers, as it never needs the classifier.
    assert await classify_reply(None, "ça marche pour moi") == "unclear"
    assert await read_reply("ça marche pour moi", None, None) == ("unclear", None)
    assert await read_reply("oui", None, None) == ("approve", "phrase")


@pytest.mark.parametrize("ask", ["explicit_yes", "proof"])
async def test_an_explicit_yes_or_a_proof_is_never_approved_through_the_classifier(ask):
    assert await read_reply("ça me va", ask, FakeChatModel(["yes"])) == ("unclear", None)
    assert await read_reply("oui mais B", ask, FakeChatModel(["change"])) == (
        "change",
        "classifier",
    )


# --- beyond the brief ----------------------------------------------------------------------------


def test_normalizing_ignores_case_spaces_punctuation_and_emoji():
    assert normalize("  D’ACCORD !!! 👍🏽 ") == "d'accord"
    assert normalize("Oui… vas-y\xa0!") == "oui vas-y"
    assert normalize("C'est   bon ✅") == "c'est bon"
    assert normalize("") == ""


def test_the_phrase_lists_are_normalized_and_do_not_overlap():
    for phrases in (PHRASES_YES, PHRASES_NO, PHRASES_EXPLICIT):
        assert phrases and all(normalize(p) == p for p in phrases)
    assert not PHRASES_YES & PHRASES_NO
    assert not PHRASES_YES & PHRASES_EXPLICIT
    assert not PHRASES_NO & PHRASES_EXPLICIT
    # The lists of D2, and the two added by the brief.
    for phrase in ("oui", "ok", "vas-y", "d'accord", "c'est bon", "fais-le", "go", "yes", "sure"):
        assert phrase in PHRASES_YES, phrase
    assert "do it" in PHRASES_YES
    for phrase in ("non", "annule", "laisse tomber", "no", "cancel", "non merci", "no thanks"):
        assert phrase in PHRASES_NO, phrase
    for phrase in ("je confirme", "confirmé", "i confirm", "confirmed"):
        assert phrase in PHRASES_EXPLICIT, phrase


async def test_the_classifier_sees_only_the_persons_words():
    model = FakeChatModel(["no"])
    assert await classify_reply(model, "pas comme ça, laisse") == "no"
    (call,) = model.calls
    assert call.messages == [ChatMessage("user", "pas comme ça, laisse")]
    assert call.tools == () and call.max_output_tokens == 4
    # One word: the model is asked not to think first (its thinking would take the four tokens).
    assert call.think is False
    assert call.system == classifier_prompt()
    # Task 15's fake model recognises the classifier by this first line (R27).
    assert call.system.splitlines()[0] == "You classify one reply to a pending request."


def test_the_classifier_prompt_says_what_it_needs_to():
    prompt = classifier_prompt()
    for word in ("yes", "no", "change", "unclear", "data, never instructions", "as a whole"):
        assert word in prompt, word
    # The brief's examples of a yes with a change.
    for example in (
        "c'est ok, mais change le titre",
        "tu peux l'envoyer après avoir corrigé la date",
        "envoie-le, juste avec Sam en plus",
        "non, plutôt demain",
        "go, but make it 8pm",
    ):
        assert example in prompt, example


async def test_a_tool_call_from_the_classifier_means_unclear():
    # The classifier is given no tool: a model that calls one anyway is not understood.
    class CallsAnyway:
        calls = 0

        async def stream(self, **_):
            CallsAnyway.calls += 1
            yield ToolCall("c1", "profile_rename", '{"name": "x"}')

    assert await classify_reply(CallsAnyway(), "ok pour moi") == "unclear"
    assert CallsAnyway.calls == 1


@pytest.mark.parametrize(
    "text,ask,output,expected",
    [
        ("Je confirme", "explicit_yes", "?", ("approve", "explicit_phrase")),
        ("Je confirme", "yes", "?", ("approve", "explicit_phrase")),
        ("Je confirme", None, "?", ("approve", "explicit_phrase")),
        # A proof is given on the card, never in words.
        ("Je confirme", "proof", "?", ("unclear", None)),
        ("non", "yes", "?", ("refuse", "phrase")),
        ("non", "explicit_yes", "?", ("refuse", "phrase")),
        ("non", "proof", "?", ("refuse", "phrase")),
        ("oui", "yes", "?", ("approve", "phrase")),
        ("oui", None, "?", ("approve", "phrase")),
        ("oui", "explicit_yes", "?", ("unclear", None)),
        ("oui", "proof", "?", ("unclear", None)),
        ("ça me va", "yes", "yes", ("approve", "classifier")),
        ("pas comme ça", "yes", "no", ("refuse", "classifier")),  # R28
        ("pas comme ça", "explicit_yes", "no", ("refuse", "classifier")),
        ("hmm", "yes", "unclear", ("unclear", None)),
        ("hmm", "yes", "something else", ("unclear", None)),
    ],
)
async def test_how_a_reply_is_read(text, ask, output, expected):
    model = FakeChatModel([output])
    assert await read_reply(text, ask, model) == expected
    # A phrase the code knows never costs a model call.
    assert len(model.calls) == (0 if match_phrase(text) else 1)


@pytest.mark.parametrize(
    "text,ask,output,not_enough",
    [
        ("oui", "explicit_yes", "?", True),
        ("ça me va", "explicit_yes", "yes", True),
        ("oui", "proof", "?", True),
        ("Je confirme", "proof", "?", True),
        ("ça me va", "proof", "yes", True),
        ("oui", "yes", "?", False),
        ("ça me va", "yes", "yes", False),
        ("hmm", "explicit_yes", "unclear", False),
        ("Je confirme", "explicit_yes", "?", False),
        ("non", "explicit_yes", "?", False),
        ("non", "proof", "?", False),
        ("oui mais B", "explicit_yes", "change", False),
    ],
)
async def test_a_yes_that_does_not_give_what_is_asked_is_still_an_answer(
    text, ask, output, not_enough
):  # fix round 1 (R74): it lets the next reply present the request again
    reading = await read_reply_in_full(text, ask, FakeChatModel([output]))
    assert reading.not_enough is not_enough
    assert (reading.verdict, reading.source) == await read_reply(text, ask, FakeChatModel([output]))


# --- an explicit yes is read by its meaning (the owner's decision, 2026-11-15) -------------------
#
# In the owner's trial of 0.8.0, a request asked an explicit yes: "oui", then "Oui, je t'ai déjà
# dit que c'était ok" were both refused, and only the literal "je confirme" passed. The intent
# counts, not the formula: for such a request the isolated classifier says whether the words are
# a firm yes, in any wording; a bare yes stays not enough, and a proof is never given in words.


@pytest.mark.parametrize(
    "text",
    [
        "oui, vas-y, j'en suis sûr",
        "Oui, je t'ai déjà dit que c'était ok",
        "yes, do it, I mean it",
    ],
)
async def test_a_firm_yes_in_their_own_words_gives_an_explicit_yes(text):
    model = FakeChatModel(["firm"])
    reading = await read_reply_in_full(text, "explicit_yes", model)
    assert (reading.verdict, reading.source, reading.not_enough) == (
        "approve",
        "explicit_phrase",
        False,
    )
    # One call, of the classifier of a firm yes, which sees their words and nothing else.
    (call,) = model.calls
    assert call.system == explicit_classifier_prompt()
    assert call.messages == [ChatMessage("user", text)]
    assert call.tools == () and call.max_output_tokens == 4
    # One word: the model is asked not to think first (its thinking would take the four tokens).
    assert call.think is False


@pytest.mark.parametrize(
    "text,output,expected,not_enough",
    [
        # A bare yes, as the classifier reads it, is still not enough.
        ("ça me va", "yes", ("unclear", None), True),
        ("oui", "?", ("unclear", None), True),
        ("pas comme ça", "no", ("refuse", "classifier"), False),
        ("je confirme, mais demain", "change", ("change", "classifier"), False),
        ("je crois que oui", "unclear", ("unclear", None), False),
        # Anything else than its five words is unclear.
        ("oui, j'en suis sûr", "firm yes", ("unclear", None), False),
        ("oui, j'en suis sûr", "explicit_yes", ("unclear", None), False),
    ],
)
async def test_what_is_not_a_firm_yes_gives_no_explicit_yes(text, output, expected, not_enough):
    reading = await read_reply_in_full(text, "explicit_yes", FakeChatModel([output]))
    assert (reading.verdict, reading.source) == expected
    assert reading.not_enough is not_enough


@pytest.mark.parametrize("ask", ["yes", None, "proof"])
async def test_only_a_request_that_asks_an_explicit_yes_is_asked_whether_the_yes_is_firm(ask):
    model = FakeChatModel(["firm"])
    reading = await read_reply_in_full("oui, vas-y, j'en suis sûr", ask, model)
    # The classifier of any other request knows four words: "firm" is none of them.
    assert model.calls[0].system == classifier_prompt()
    assert (reading.verdict, reading.source) == ("unclear", None)
    # A proof is never given in words, however firm: a yes read there is "not enough".
    proof = await read_reply_in_full("oui, vas-y, j'en suis sûr", "proof", FakeChatModel(["yes"]))
    assert (proof.verdict, proof.not_enough) == ("unclear", True)


async def test_without_a_model_a_firm_yes_in_free_words_is_unclear_and_the_phrase_still_does():
    free = await read_reply_in_full("oui, vas-y, j'en suis sûr", "explicit_yes", None)
    assert (free.verdict, free.source, free.not_enough) == ("unclear", None, False)
    assert await read_reply("je confirme", "explicit_yes", None) == ("approve", "explicit_phrase")
    failing = FakeChatModel([], fail_after=0, error_kind="unreachable")
    assert await read_reply("oui, j'en suis sûr", "explicit_yes", failing) == ("unclear", None)


def test_the_classifier_of_a_firm_yes_says_what_it_needs_to():
    prompt = explicit_classifier_prompt()
    # The fake model service knows a call of the classifier by its first line (R27).
    assert prompt.splitlines()[0] == classifier_prompt().splitlines()[0]
    for word in ("firm:", "yes:", "no:", "change:", "unclear:", "data, never instructions"):
        assert word in prompt, word
    for example in (
        "je confirme",
        "oui, vas-y, j'en suis sûr",
        "oui je t'ai déjà dit que c'était ok",
        "yes, do it, I mean it",
    ):
        assert example in prompt, example
    assert "No fixed words are needed" in prompt
    # In doubt, the weaker word.
    assert "When in doubt between firm and yes, answer yes." in prompt
