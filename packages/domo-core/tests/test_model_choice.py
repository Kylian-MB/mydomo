"""Choosing a model among the ones a key opens: the cheapest of the level wanted, the levels around
it, the tasks that never take a model of an unknown level, and the price of what is not known."""

from datetime import date
from decimal import Decimal

from domo_core.llm.catalog import FREE, UNKNOWN_PRICES, Catalog, ModelInfo, Prices
from domo_core.llm.choice import Candidate, candidates, is_local, pick_model
from domo_core.llm.models import Provider
from llm_support import provider


def prices(input: int, output: int) -> Prices:
    return Prices(Decimal(input), Decimal(output), Decimal(input), Decimal(output))


def info(model_id: str, input: int, output: int, *, tools: bool = True, context: int | None = None):
    return ModelInfo(model_id, prices(input, output), context, None, tools, False)


def catalog_of(*infos: ModelInfo, kind: str = "anthropic") -> Catalog:
    return Catalog(
        fetched=date(2026, 11, 8),
        usd_per_eur=Decimal("1.1"),
        entries={kind: {i.model_id: i for i in infos}},  # type: ignore[dict-item]
    )


def cand(
    model_id: str,
    level: str | None,
    price: int = 1,
    *,
    tools: bool = True,
    context: int | None = None,
) -> Candidate:
    return Candidate(model_id, level, prices(price, price), tools, context, True)  # type: ignore[arg-type]


def test_the_cheapest_of_the_wanted_level():
    options = [cand("a", "medium", 5), cand("b", "medium", 3), cand("c", "small", 1)]
    assert pick_model(options, "conversation", wanted="medium").model_id == "b"  # type: ignore[union-attr]


def test_the_next_level_up_when_none_is_at_the_level():
    options = [
        cand("small-one", "small", 1),
        cand("strong-b", "strong", 9),
        cand("strong-a", "strong", 4),
    ]
    assert pick_model(options, "conversation", wanted="medium").model_id == "strong-a"  # type: ignore[union-attr]


def test_a_protected_task_never_takes_a_model_without_a_known_level():
    local = provider("openai_compatible")
    local.secret_ref = None
    only = candidates(catalog_of(), local, ["llama3"], {})
    assert only[0].level is None
    assert pick_model(only, "judge", wanted="medium") is None
    assert pick_model(only, "intent", wanted="medium") is None
    assert pick_model(only, "rights_writer", wanted="medium") is None
    # A task that is not protected takes it.
    assert pick_model(only, "conversation", wanted="medium") == only[0]


def test_a_protected_task_never_takes_a_model_under_its_minimum():
    options = [cand("tiny", "small", 1)]
    assert pick_model(options, "judge", wanted="medium") is None


def test_the_owner_word_gives_a_level():
    local = provider("openai_compatible")
    local.secret_ref = None
    found = candidates(catalog_of(), local, ["llama3"], {"llama3": "medium"})
    assert found[0].level == "medium"
    assert pick_model(found, "judge", wanted="medium") == found[0]


def test_a_conversation_takes_the_strongest_below_when_nothing_is_higher():
    options = [cand("s", "small", 1), cand("m-dear", "medium", 8), cand("m-cheap", "medium", 2)]
    assert pick_model(options, "conversation", wanted="strong").model_id == "m-cheap"  # type: ignore[union-attr]
    only_small = [cand("s1", "small", 3), cand("s2", "small", 1)]
    assert pick_model(only_small, "conversation", wanted="medium").model_id == "s2"  # type: ignore[union-attr]


def test_a_local_key_is_free():
    local = provider("openai_compatible")
    local.secret_ref = None
    found = candidates(catalog_of(), local, ["llama3"], {})
    assert found[0].prices == FREE and found[0].price_known
    assert is_local(local)
    # A key sent over the network to a public host is not local, whatever its kind.
    remote = Provider(
        id="p2",
        kind="openai_compatible",
        base_url="https://llm.example.com/v1",
        secret_ref="k",
    )
    assert not is_local(remote)
    assert not is_local(provider("anthropic"))
    on_the_lan = provider("openai_compatible")  # 192.168.1.10 with a key: cleartext is allowed
    assert is_local(on_the_lan)


def test_a_pinned_model_is_taken_when_listed():
    options = [cand("cheap", "medium", 1), cand("pinned", "strong", 90)]
    assert (
        pick_model(options, "conversation", wanted="medium", pinned="pinned").model_id == "pinned"
    )  # type: ignore[union-attr]
    assert pick_model(options, "conversation", wanted="medium", pinned="absent").model_id == "cheap"  # type: ignore[union-attr]


def test_an_unknown_price_counts_at_the_highest_of_its_provider():
    catalog = catalog_of(info("claude-haiku-4-5", 1, 5), info("claude-opus-4-1", 15, 75))
    key = provider("anthropic")
    found = candidates(catalog, key, ["claude-haiku-4-5", "claude-mystery"], {})
    known, unknown = found
    assert known.prices == prices(1, 5) and known.price_known
    assert unknown.prices == prices(15, 75) and not unknown.price_known
    # A provider with no entry at all falls back to the project's unknown prices.
    other = candidates(catalog, provider("google"), ["gemini-x"], {})
    assert other[0].prices == UNKNOWN_PRICES and not other[0].price_known


def test_at_an_equal_price_a_known_level_comes_first():
    options = [cand("aaa-no-level", None, 2), cand("zzz-small", "small", 2)]
    assert pick_model(options, "key_check", wanted="small").model_id == "zzz-small"  # type: ignore[union-attr]
    same = [cand("b", "small", 2), cand("a", "small", 2)]
    assert pick_model(same, "key_check", wanted="small").model_id == "a"  # type: ignore[union-attr]


def test_a_model_with_too_small_a_context_is_dropped():
    options = [cand("tiny", "medium", 1, context=8_000), cand("wide", "medium", 5, context=200_000)]
    assert (
        pick_model(options, "conversation", wanted="medium", min_context=50_000).model_id == "wide"
    )  # type: ignore[union-attr]
    unknown = [cand("tiny", "medium", 1, context=8_000), cand("unknown", "medium", 9, context=None)]
    assert (
        pick_model(unknown, "conversation", wanted="medium", min_context=50_000).model_id
        == "unknown"
    )  # type: ignore[union-attr]
    assert pick_model(options, "conversation", wanted="medium", min_context=500_000) is None


def test_a_model_that_cannot_call_tools_is_not_taken_for_a_task_that_needs_them():
    found = candidates(
        catalog_of(info("claude-haiku-4-5", 1, 5, tools=False)),
        provider("anthropic"),
        ["claude-haiku-4-5", "unlisted"],
        {},
    )
    assert [c.tools for c in found] == [False, True]  # unknown capability = can call tools
    assert pick_model(found, "key_check", wanted="small").model_id == "unlisted"  # type: ignore[union-attr]
    assert pick_model(found[:1], "key_check", wanted="small") is None
    assert pick_model(found[:1], "judge", wanted="small") is None  # protected: level small < medium
