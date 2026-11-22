from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_common.gatekeeper_api import (
    FORM_TEXT_KEYS,
    FORM_TEXT_MAX,
    Binding,
    InjectionLocation,
    ProviderRoute,
    SecretMetadata,
    SecretRequestCreate,
    SecretRequestView,
)
from domo_common.meanings import all_keys, max_length, meaning
from pydantic import ValidationError

B = Binding(host="api.example.com", location=InjectionLocation(kind="header", name="x-api-key"))
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def test_ref_pattern_rejects_bad_refs():
    with pytest.raises(ValidationError):
        SecretRequestCreate(ref="../x", purpose="p", bindings=[B])


@pytest.mark.parametrize(
    "ref",
    ["r", "provider:a:api_key", "a.b-c_d:e", "0abc", "a" * 128],
)
def test_ref_pattern_accepts_good_refs(ref: str):
    assert SecretRequestCreate(ref=ref, purpose="p", bindings=[B]).ref == ref


@pytest.mark.parametrize(
    "ref",
    ["", "Upper", "-leading-dash", "_leading", "has space", "a/b", "trailing\n", "a" * 129],
)
def test_ref_pattern_rejects_malformed_refs(ref: str):
    with pytest.raises(ValidationError):
        SecretRequestCreate(ref=ref, purpose="p", bindings=[B])


def test_provider_route_rejects_non_http_scheme():
    with pytest.raises(ValidationError):
        ProviderRoute(id="p1", base_url="file:///etc/passwd", secret_ref=None)


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://api.example.com",
        "javascript:alert(1)",
        "api.example.com/v1",
        "https://",
        "https://user:pw@api.example.com/v1",
        "https://api.example.com/v1?key=1",
        "https://api.example.com/v1#frag",
    ],
)
def test_provider_route_rejects_unusable_base_urls(base_url: str):
    with pytest.raises(ValidationError):
        ProviderRoute(id="p1", base_url=base_url, secret_ref=None)


@pytest.mark.parametrize(
    "base_url",
    ["https://api.example.com/v1", "http://localhost:8080", "http://192.168.1.10:11434/v1"],
)
def test_provider_route_accepts_http_and_https(base_url: str):
    route = ProviderRoute(id="p1", base_url=base_url, secret_ref="provider:p1:api_key")
    assert route.base_url == base_url
    assert route.secret_ref == "provider:p1:api_key"


@pytest.mark.parametrize("route_id", ["", "P1", "a b", "a_b", "x" * 65, "a/b"])
def test_provider_route_rejects_bad_ids(route_id: str):
    with pytest.raises(ValidationError):
        ProviderRoute(id=route_id, base_url="https://api.example.com", secret_ref=None)


def test_provider_route_rejects_bad_secret_ref():
    with pytest.raises(ValidationError):
        ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref="../x")


def test_binding_host_is_normalised_to_lowercase():
    binding = Binding(
        host="API.Example.COM", location=InjectionLocation(kind="header", name="x-api-key")
    )
    assert binding.host == "api.example.com"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "https://api.example.com",
        "api.example.com:8443",
        "api.example.com/v1",
        "user@api.example.com",
        "api example.com",
        "api.example.com\n",
        ".example.com",
        "api.K.example.com",  # the Kelvin sign lower-cases to an ASCII "k"
        "[::1]",
    ],
)
def test_binding_rejects_malformed_hosts(host: str):
    with pytest.raises(ValidationError):
        Binding(host=host, location=InjectionLocation(kind="header", name="x-api-key"))


@pytest.mark.parametrize("host", ["localhost", "192.168.1.10", "fake-llm", "evil.example.net"])
def test_binding_accepts_plain_hosts(host: str):
    location = InjectionLocation(kind="header", name="x-api-key")
    assert Binding(host=host, location=location).host == host


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_binding_rejects_out_of_range_ports(port: int):
    with pytest.raises(ValidationError):
        Binding(
            host="api.example.com",
            port=port,
            location=InjectionLocation(kind="header", name="x-api-key"),
        )


def test_binding_accepts_a_port():
    binding = Binding(
        host="localhost", port=8080, location=InjectionLocation(kind="header", name="x-api-key")
    )
    assert binding.port == 8080


def test_header_name_is_normalised_to_lowercase():
    assert InjectionLocation(kind="header", name="X-API-Key").name == "x-api-key"
    upper = Binding(
        host="api.example.com", location=InjectionLocation(kind="header", name="Authorization")
    )
    lower = Binding(
        host="api.example.com", location=InjectionLocation(kind="header", name="authorization")
    )
    assert upper == lower
    assert hash(upper) == hash(lower)


def test_injection_location_defaults_template_to_value_placeholder():
    assert InjectionLocation(kind="header", name="authorization").template == "{value}"


@pytest.mark.parametrize("name", ["", "bad name", "bad:name", "bad\r\nname", "café"])
def test_injection_location_rejects_bad_header_names(name: str):
    with pytest.raises(ValidationError):
        InjectionLocation(kind="header", name=name)


@pytest.mark.parametrize(
    "template", ["Bearer", "no placeholder", "{value}\r\nx-evil: 1", "{value}\n"]
)
def test_injection_location_rejects_bad_templates(template: str):
    with pytest.raises(ValidationError):
        InjectionLocation(kind="header", name="authorization", template=template)


def test_injection_location_accepts_bearer_template():
    location = InjectionLocation(kind="header", name="authorization", template="Bearer {value}")
    assert location.template == "Bearer {value}"


def test_injection_location_only_supports_header_in_1a():
    with pytest.raises(ValidationError):
        InjectionLocation(kind="query", name="key")  # type: ignore[arg-type]


def test_secret_request_create_limits_purpose_length():
    assert SecretRequestCreate(ref="r", purpose="p" * 200, bindings=[B])
    with pytest.raises(ValidationError):
        SecretRequestCreate(ref="r", purpose="p" * 201, bindings=[B])


def test_secret_request_create_needs_at_least_one_binding():
    with pytest.raises(ValidationError):
        SecretRequestCreate(ref="r", purpose="p", bindings=[])


def test_models_refuse_unknown_fields():
    with pytest.raises(ValidationError):
        SecretRequestCreate.model_validate(
            {"ref": "r", "purpose": "p", "bindings": [B.model_dump()], "value": "oops"}
        )


def test_bindings_are_immutable_and_hashable():
    assert hash(B) == hash(B.model_copy())
    with pytest.raises(ValidationError):
        B.host = "evil.example.net"


def test_secret_request_view_roundtrips_from_json():
    view = SecretRequestView(
        id="abc", ref="r", status="pending", form_path="/vault/request/abc", expires_at=NOW
    )
    assert SecretRequestView.model_validate_json(view.model_dump_json()) == view


def test_secret_request_view_rejects_unknown_status():
    with pytest.raises(ValidationError):
        SecretRequestView(
            id="abc",
            ref="r",
            status="done",  # type: ignore[arg-type]
            form_path=None,
            expires_at=NOW,
        )


def test_datetimes_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        SecretRequestView(
            id="abc", ref="r", status="pending", form_path=None, expires_at=datetime(2030, 1, 1)
        )
    with pytest.raises(ValidationError):
        SecretMetadata(
            ref="r",
            purpose="p",
            bindings=[B],
            created_at=datetime(2030, 1, 1),
            rotated_at=None,
            last_used_at=None,
        )


def test_secret_metadata_carries_only_refs_and_metadata():
    meta = SecretMetadata(
        ref="r",
        purpose="p",
        bindings=[B],
        created_at=NOW,
        rotated_at=NOW + timedelta(days=1),
        last_used_at=None,
    )
    dumped: dict[str, Any] = meta.model_dump()
    assert set(dumped) == {
        "ref",
        "purpose",
        "bindings",
        "created_at",
        "rotated_at",
        "last_used_at",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        # urlsplit silently drops \t \r \n, which would let the relay and the host check
        # disagree about the destination.
        "https://api.example.com/v1\t",
        "https://api.example.com\n.evil.example.net/v1",
        "https://api.exa\rmple.com",
        "https://api.example.com/v\t1",
        "https://api.example.com/v1\n",
        " https://api.example.com",
        "https://api.example.com/v1 ",
        "https://api .example.com",
        "https://api.example.com/a b",
        "https://api.example.com/\x00",
        "https://api.example.com/\x1b[0m",
        "https://api.example.com/\x7f",
        "https://api.example.com/\xa0",
        "https://api.example.com/ ",
    ],
)
def test_provider_route_rejects_whitespace_and_control_characters_in_base_url(base_url: str):
    with pytest.raises(ValidationError):
        ProviderRoute(id="p1", base_url=base_url, secret_ref=None)


# --- the texts of a vault form (spec 4.5) -----------------------------------------------------


def _written(**more: Any) -> dict[str, Any]:
    return {"ref": "r", "purpose": "p", "bindings": [B.model_dump()], **more}


def test_the_form_texts_are_the_vault_texts_of_the_catalog():
    vault_keys = {key for key in all_keys() if key.startswith("vault.")}
    assert vault_keys == FORM_TEXT_KEYS
    # The gatekeeper shows each address itself, without words: no text presents one.
    assert not any("{host}" in meaning(key) for key in FORM_TEXT_KEYS)


def test_a_written_form_text_always_fits():
    assert all(max_length(key) <= FORM_TEXT_MAX for key in FORM_TEXT_KEYS)


def test_a_request_may_carry_the_texts_of_its_form_in_a_language():
    texts = {key: f"({key})" for key in FORM_TEXT_KEYS}
    created = SecretRequestCreate.model_validate(_written(language="pt-BR", texts=texts))
    assert created.language == "pt-BR" and created.texts == texts
    # Without them (the installation), the gatekeeper keeps its fixed texts.
    fixed = SecretRequestCreate.model_validate(_written())
    assert fixed.language is None and fixed.texts is None


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        '<a href="https://evil.example">here</a>',
        "1 > 0",
        "line\nbreak",
        "bell\x07",
        "a" + chr(0x2028) + "b",
        chr(0x202E) + "esrever",
        "x" * (FORM_TEXT_MAX + 1),
        "Type it at https://evil.example",
        "Type it at www.evil.example",
        "Type it at FTP://evil.example",
    ],
)
def test_a_form_text_is_plain_and_short(text: str):
    with pytest.raises(ValidationError):
        SecretRequestCreate.model_validate(
            _written(language="en", texts={"vault.form.title": text})
        )
    assert SecretRequestCreate.model_validate(
        _written(language="en", texts={"vault.form.title": "x" * FORM_TEXT_MAX})
    )


@pytest.mark.parametrize("key", ["vault.form.destination", "chat.send", "vault.form.title.x"])
def test_a_form_text_has_a_key_of_the_form(key: str):
    with pytest.raises(ValidationError):
        SecretRequestCreate.model_validate(_written(language="en", texts={key: "words"}))


@pytest.mark.parametrize(
    "more",
    [
        {"language": "en"},
        {"texts": {"vault.form.title": "words"}},
        {"language": "not a tag", "texts": {"vault.form.title": "words"}},
        {"language": "en-<b>", "texts": {"vault.form.title": "words"}},
        {"language": "en-U" + chr(0x212A), "texts": {"vault.form.title": "words"}},
    ],
)
def test_texts_come_with_their_language(more: dict[str, Any]):
    with pytest.raises(ValidationError):
        SecretRequestCreate.model_validate(_written(**more))


@pytest.mark.parametrize(
    "purpose",
    [
        "<b>API key</b>",
        "API key, see https://evil.example",
        "API key\nand your password",
        chr(0x202E) + "yek IPA",
    ],
)
def test_with_texts_the_purpose_is_plain_too(purpose: str):
    texts = {"vault.form.title": "words"}
    with pytest.raises(ValidationError):
        SecretRequestCreate.model_validate(
            {**_written(language="en", texts=texts), "purpose": purpose}
        )
    # During the installation, the purpose is only escaped where it is shown.
    assert SecretRequestCreate.model_validate({**_written(), "purpose": purpose})
