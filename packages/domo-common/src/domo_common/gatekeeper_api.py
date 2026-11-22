"""Models shared by the Domo core and the gatekeeper.

The core and the model only ever handle secret *references* (``ref``) and metadata, never
secret values. A secret is bound to destinations (``Binding``): the host it may be sent to
and the exact place where it is injected. Bindings are immutable once a secret is stored.

Once Domo is installed, a request for a secret also carries the texts of its form, in the
words of the one who gives it (spec 4.5): ``language`` and ``texts``, the texts of
``FORM_TEXT_KEYS`` with their holes left in (``{max}``), which the gatekeeper fills itself. Each
is plain text on one line, as the core's checks keep them (``text_rules``: no markup, no web
address, no control character or direction mark), ``FORM_TEXT_MAX`` characters at most, and so is
the purpose then. The gatekeeper shows only them, and each address itself, without words. Without
them (the installation, before Domo can write), it shows its own fixed texts, and the purpose is
only escaped where it is shown.
"""

import re
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from domo_common.text_rules import LANGUAGE_TAG, is_plain_line, plain_problem

# Allowed secret references. Pydantic (Rust regex) only matches ``$`` at the very end of the
# string, but Python's ``re`` also lets it match before a trailing newline: outside pydantic,
# check a ref with ``re.fullmatch``.
REF_PATTERN = r"^[a-z0-9][a-z0-9_.:-]{0,127}$"

PURPOSE_MAX_LENGTH = 200

# The texts a vault form and its pages show: the form, the page once the secret is saved, and the
# notice of each refusal.
FORM_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "vault.form.title",
        "vault.form.purpose",
        "vault.form.destinations",
        "vault.form.value_label",
        "vault.form.privacy",
        "vault.form.submit",
        "vault.form.error.length",
        "vault.form.error.invalid",
        "vault.saved.title",
        "vault.saved.message",
        "vault.unavailable.title",
        "vault.unavailable.message",
        "vault.conflict.title",
        "vault.conflict.message",
        "vault.too_large.title",
        "vault.too_large.message",
        "vault.error.title",
        "vault.error.message",
    }
)
# The longest text of a form.
FORM_TEXT_MAX: Final = 600

_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 "token"
_VALUE_PLACEHOLDER = "{value}"

Ref = Annotated[str, StringConstraints(pattern=REF_PATTERN)]


class _Model(BaseModel):
    """Base for every gatekeeper API model: immutable, and strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class InjectionLocation(_Model):
    """Where a secret is injected into the outgoing request (1a: a header only)."""

    kind: Literal["header"]
    name: str
    template: str = _VALUE_PLACEHOLDER

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        if _HEADER_NAME_RE.fullmatch(name) is None:
            raise ValueError("invalid HTTP header name")
        # Header names are case-insensitive: one spelling, so equal destinations compare equal.
        return name.lower()

    @field_validator("template")
    @classmethod
    def _check_template(cls, template: str) -> str:
        if _VALUE_PLACEHOLDER not in template:
            raise ValueError(f"template must contain {_VALUE_PLACEHOLDER}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in template):
            raise ValueError("template must not contain control characters")
        return template


class Binding(_Model):
    """A destination a secret may be sent to: a host (no scheme or port) and a place."""

    host: str
    port: int | None = Field(default=None, ge=1, le=65535)
    location: InjectionLocation

    @field_validator("host")
    @classmethod
    def _check_host(cls, host: str) -> str:
        if not host.isascii():
            raise ValueError("host must be ASCII (use the punycode form)")
        host = host.lower()
        if _HOST_RE.fullmatch(host) is None:
            raise ValueError("host must be a bare host name or IPv4 address (no scheme or port)")
        return host


def _is_plain(text: str) -> bool:
    """Whether ``text`` is plain text on one line, as the core's checks keep a text."""
    return plain_problem(text) is None and is_plain_line(text)


class SecretRequestCreate(_Model):
    """What the core sends to the gatekeeper to ask the owner for a secret: with the texts of its
    form in ``language`` once Domo is installed (see the module docstring), without them during
    the installation (``language`` is ``None``)."""

    ref: Ref
    purpose: str = Field(max_length=PURPOSE_MAX_LENGTH)
    bindings: list[Binding] = Field(min_length=1)
    language: str | None = None
    texts: dict[str, str] | None = None

    @field_validator("language")
    @classmethod
    def _check_language(cls, language: str | None) -> str | None:
        if language is not None and LANGUAGE_TAG.fullmatch(language) is None:
            raise ValueError("language must be a language tag")
        return language

    @field_validator("texts")
    @classmethod
    def _check_texts(cls, texts: dict[str, str] | None) -> dict[str, str] | None:
        for key, text in (texts or {}).items():
            if key not in FORM_TEXT_KEYS:
                raise ValueError("a text of the form has an unknown key")
            if len(text) > FORM_TEXT_MAX:
                raise ValueError(f"a text of the form has more than {FORM_TEXT_MAX} characters")
            if not _is_plain(text):
                raise ValueError("a text of the form must be plain text, on one line")
        return texts

    @model_validator(mode="after")
    def _texts_in_a_language(self) -> Self:
        if (self.language is None) != (self.texts is None):
            raise ValueError("the texts of the form come with their language, and only them")
        if self.texts is not None and not _is_plain(self.purpose):
            raise ValueError("with the texts of the form, the purpose is plain text too")
        return self


class SecretRequestView(_Model):
    """What the gatekeeper tells the core about a secret request."""

    id: str
    ref: Ref
    status: Literal["pending", "fulfilled", "expired", "superseded"]
    form_path: str | None
    expires_at: AwareDatetime


class SecretMetadata(_Model):
    """Everything known about a stored secret except its value."""

    ref: Ref
    purpose: str
    bindings: list[Binding]
    created_at: AwareDatetime
    rotated_at: AwareDatetime | None
    last_used_at: AwareDatetime | None


def _check_base_url(url: str) -> str:
    # urlsplit silently drops tabs and line breaks (and callers may strip the rest), so what the
    # relay compares with a secret's bindings could differ from what is actually requested.
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise ValueError("base_url must not contain whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("base_url must use http or https")
    if not parts.hostname:
        raise ValueError("base_url must name a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("base_url must not embed credentials")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not carry a query or a fragment")
    _ = parts.port  # raises ValueError when the port is not a valid number
    return url


class ProviderRoute(_Model):
    """A model provider the gatekeeper relays to, and the secret that authenticates it."""

    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    base_url: Annotated[str, AfterValidator(_check_base_url)]
    secret_ref: Ref | None
