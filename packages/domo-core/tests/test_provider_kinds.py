"""The kinds of provider: the table is fixed, and says where each one wants its key."""

import dataclasses

import pytest
from domo_common.gatekeeper_api import Binding, InjectionLocation
from domo_core.llm.provider_kinds import PROVIDER_KINDS, ProviderKindSpec
from domo_core.llm.types import ModelCallError


def test_the_table_has_exactly_these_kinds():
    assert set(PROVIDER_KINDS) == {"anthropic", "openai", "google", "openai_compatible"}


@pytest.mark.parametrize(
    ("kind", "base_url", "header", "template", "required"),
    [
        ("anthropic", "https://api.anthropic.com", "x-api-key", "{value}", True),
        ("openai", "https://api.openai.com/v1", "authorization", "Bearer {value}", True),
        (
            "google",
            "https://generativelanguage.googleapis.com",
            "x-goog-api-key",
            "{value}",
            True,
        ),
        ("openai_compatible", None, "authorization", "Bearer {value}", False),
    ],
)
def test_each_kind_has_its_address_and_the_place_of_its_key(
    kind, base_url, header, template, required
):
    spec = PROVIDER_KINDS[kind]
    assert spec.default_base_url == base_url
    assert spec.key_location == InjectionLocation(kind="header", name=header, template=template)
    assert spec.key_required is required


def test_a_key_location_can_be_the_binding_of_a_secret():
    """The setup registers the location of the kind as the destination of the key."""
    for spec in PROVIDER_KINDS.values():
        binding = Binding(host="api.example.com", location=spec.key_location)
        assert binding.location == spec.key_location


def test_the_table_cannot_be_changed():
    with pytest.raises(TypeError):
        PROVIDER_KINDS["openai"] = PROVIDER_KINDS["anthropic"]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        PROVIDER_KINDS["openai"].key_required = False  # type: ignore[misc]
    assert isinstance(PROVIDER_KINDS["openai"], ProviderKindSpec)


@pytest.mark.parametrize("kind", ["auth", "unreachable", "rate_limited", "bad_request", "upstream"])
def test_a_model_call_error_is_its_kind_and_nothing_else(kind):
    error = ModelCallError(kind)
    assert error.kind == kind
    assert str(error) == kind
    assert error.args == (kind,)
