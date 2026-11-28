"""The models a key opens, listed through the gatekeeper's relay: what each kind of provider
answers, and the limits on what a provider can make Domo keep."""

import json

import pytest
from domo_core.llm.listing import MAX_LISTED_MODELS, MAX_MODEL_ID_LENGTH, list_models
from domo_core.llm.types import ModelCallError
from llm_support import CORE_TOKEN, GATEKEEPER_URL, provider


def openai_list(*ids: str) -> dict[str, object]:
    return {
        "object": "list",
        "data": [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in ids],
    }


def anthropic_page(*ids: str, more: bool = False) -> bytes:
    page = {
        "data": [
            {"type": "model", "id": i, "display_name": i, "created_at": "2025-10-01T00:00:00Z"}
            for i in ids
        ],
        "has_more": more,
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
    }
    return json.dumps(page).encode()


async def test_openai_lists_its_models(wire_for, gk_client):
    wire = wire_for("openai")
    wire.reply_json(openai_list("gpt-5-mini", "gpt-5"))
    models = await list_models(gk_client, provider("openai"), transport=wire.transport)
    assert models == ("gpt-5-mini", "gpt-5")
    assert wire.last.method == "GET"
    assert wire.last.url == f"{GATEKEEPER_URL}/llm/p1/models"
    assert wire.last.headers["authorization"] == f"Bearer {CORE_TOKEN}"


async def test_anthropic_lists_every_page(wire_for, gk_client):
    wire = wire_for("anthropic")
    wire.reply_each(
        [
            anthropic_page("claude-haiku-4-5", "claude-sonnet-4-5", more=True),
            anthropic_page("claude-opus-4-1"),
        ]
    )
    models = await list_models(gk_client, provider("anthropic"), transport=wire.transport)
    assert models == ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-1")
    assert len(wire.requests) == 2
    assert wire.requests[0].path == "/llm/p1/v1/models"
    assert "after_id=claude-sonnet-4-5" in wire.requests[1].url
    assert wire.last.headers["x-api-key"] == CORE_TOKEN


async def test_google_keeps_the_models_that_generate_without_their_prefix(wire_for, gk_client):
    wire = wire_for("google")
    generate, embed = ["generateContent"], ["embedContent"]
    wire.reply_json(
        {
            "models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": generate},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": embed},
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": generate},
            ]
        }
    )
    models = await list_models(gk_client, provider("google"))
    assert models == ("gemini-2.5-flash", "gemini-2.5-pro")
    assert wire.last.path == "/llm/p1/v1beta/models"
    assert wire.last.headers["x-goog-api-key"] == CORE_TOKEN


async def test_a_compatible_server_without_a_list_gives_none(wire_for, gk_client):
    wire = wire_for("openai_compatible")
    wire.reply(404, body=b'{"error": "not found"}')
    kind = provider("openai_compatible")
    assert await list_models(gk_client, kind, transport=wire.transport) == ()


async def test_a_hearing_the_speech_service_marks_slow_is_listed_and_so_used(wire_for, gk_client):
    """On a weak machine the house's speech service keeps its fastest hearing although too slow,
    marked ``slow``: the core lists it like any model (the house hears, slowly), not as missing."""
    wire = wire_for("openai_compatible")
    wire.reply_json(
        {
            "data": [
                {
                    "id": "faster-whisper-base",
                    "object": "model",
                    "does": "hear",
                    "at_once": 1,
                    "slow": True,
                    "measures": {"device": "cpu", "real_time_factor": 0.56},
                },
                {"id": "supertonic-3", "object": "model", "does": "say", "at_once": 1},
            ]
        }
    )
    kind = provider("openai_compatible")
    models = await list_models(gk_client, kind, transport=wire.transport)
    assert models == ("faster-whisper-base", "supertonic-3")


async def test_a_missing_list_is_a_failure_for_the_other_kinds(wire_for, gk_client):
    wire = wire_for("openai")
    wire.reply(404, body=b'{"error": "not found"}')
    with pytest.raises(ModelCallError) as raised:
        await list_models(gk_client, provider("openai"), transport=wire.transport)
    assert raised.value.kind == "bad_request"


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "anthropic"])
async def test_a_wrong_key_lists_nothing(wire_for, gk_client, kind):
    wire = wire_for(kind)
    wire.reply(401, body=b'{"error": "bad key"}')
    with pytest.raises(ModelCallError) as raised:
        await list_models(gk_client, provider(kind), transport=wire.transport)
    assert raised.value.kind == "auth"
    assert CORE_TOKEN not in str(raised.value)


async def test_an_unreachable_provider_is_told_so(wire_for, gk_client):
    wire = wire_for("openai")
    wire.fail("connect")
    with pytest.raises(ModelCallError) as raised:
        await list_models(gk_client, provider("openai"), transport=wire.transport)
    assert raised.value.kind == "unreachable"


async def test_a_list_is_cut_at_500_names(wire_for, gk_client):
    wire = wire_for("openai")
    wire.reply_json(openai_list(*(f"model-{n}" for n in range(MAX_LISTED_MODELS + 100))))
    models = await list_models(gk_client, provider("openai"), transport=wire.transport)
    assert len(models) == MAX_LISTED_MODELS
    assert models[0] == "model-0" and models[-1] == f"model-{MAX_LISTED_MODELS - 1}"


async def test_a_list_drops_names_that_are_too_long_or_have_control_characters(wire_for, gk_client):
    wire = wire_for("openai")
    wire.reply_json(
        openai_list(
            "fine",
            "x" * (MAX_MODEL_ID_LENGTH + 1),
            "line\nbreak",
            "bell\u0007",
            "x" * MAX_MODEL_ID_LENGTH,
            "also-fine",
        )
    )
    models = await list_models(gk_client, provider("openai"), transport=wire.transport)
    assert models == ("fine", "x" * MAX_MODEL_ID_LENGTH, "also-fine")
