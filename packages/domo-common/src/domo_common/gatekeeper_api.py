"""Models shared by the Domo core and the gatekeeper.

The core and the model only ever handle secret *references* (``ref``) and metadata, never
secret values. A secret is bound to destinations (``Binding``): the host it may be sent to
and the exact place where it is injected (``InjectionLocation``: a header, Basic, or the body of a
login request). Bindings are immutable once a secret is stored. A secret is one value (a key, a
token) or an account, a user and a password (``SecretFields``): the place says which.

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

from domo_common.sandbox_api import ANSWER_CEILING_BYTES
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
        "vault.form.user_label",
        "vault.form.password_label",
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
# The path of a login request: absolute, printable ASCII, without a query or a fragment.
_LOGIN_PATH_RE = re.compile(r"/[\x21-\x22\x24-\x3e\x40-\x7e]*")
_FIELD_NAME_MAX = 128

Ref = Annotated[str, StringConstraints(pattern=REF_PATTERN)]

# The headers a secret is never placed in: what marks the requests of Domo's tools (``via``,
# which his own page refuses), the framing of a request, and the session's cookies.
NEVER_A_PLACE: Final[frozenset[str]] = frozenset(
    {"via", "host", "content-length", "transfer-encoding", "connection", "cookie", "set-cookie"}
)


class _Model(BaseModel):
    """Base for every gatekeeper API model: immutable, and strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HeaderLocation(_Model):
    """A secret of one value, put in a header of the request: ``template`` with the value at
    ``{value}`` (a token, ``Bearer {value}``)."""

    kind: Literal["header"]
    name: str
    template: str = _VALUE_PLACEHOLDER

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        if _HEADER_NAME_RE.fullmatch(name) is None:
            raise ValueError("invalid HTTP header name")
        # Header names are case-insensitive: one spelling, so equal destinations compare equal.
        name = name.lower()
        if name in NEVER_A_PLACE:
            raise ValueError("a secret never goes in a header that the gatekeeper decides")
        return name

    @field_validator("template")
    @classmethod
    def _check_template(cls, template: str) -> str:
        if _VALUE_PLACEHOLDER not in template:
            raise ValueError(f"template must contain {_VALUE_PLACEHOLDER}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in template):
            raise ValueError("template must not contain control characters")
        return template


class BasicLocation(_Model):
    """An account (a user and a password) sent as ``Authorization: Basic base64(user:password)``."""

    kind: Literal["basic"]


class LoginLocation(_Model):
    """An account put in the body of a login request: only in a ``POST`` to exactly ``path`` of
    a bound host, as the two fields ``user_field`` and ``password_field`` of a form or of a JSON
    object (``encoding``)."""

    kind: Literal["login"]
    path: str
    encoding: Literal["form", "json"]
    user_field: str
    password_field: str

    @field_validator("path")
    @classmethod
    def _check_path(cls, path: str) -> str:
        # "//host/..." would name another host once joined to an address.
        if _LOGIN_PATH_RE.fullmatch(path) is None or path.startswith("//"):
            raise ValueError("path must start with / and be printable ASCII, without a query")
        return path

    @field_validator("user_field", "password_field")
    @classmethod
    def _check_field(cls, field: str) -> str:
        if not 1 <= len(field) <= _FIELD_NAME_MAX or not field.isprintable():
            raise ValueError(f"a field name is 1 to {_FIELD_NAME_MAX} printable characters")
        return field

    @model_validator(mode="after")
    def _two_fields(self) -> Self:
        if self.user_field == self.password_field:
            raise ValueError("the user and the password go in two different fields")
        return self


# Where a secret is put in the outgoing request, told apart by ``kind``.
InjectionLocation = Annotated[
    HeaderLocation | BasicLocation | LoginLocation, Field(discriminator="kind")
]

# What a secret is made of: one value (a key, a token), or an account (a user and a password).
SecretFields = Literal["value", "user_password"]


def location_fields(location: InjectionLocation) -> SecretFields:
    """What a secret put at ``location`` is made of: a header takes one value, Basic and a login
    body an account."""
    return "value" if isinstance(location, HeaderLocation) else "user_password"


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
    the installation (``language`` is ``None``). ``fields`` is what the secret is made of, and so
    which fields the form has: it is the one every place of ``bindings`` takes
    (``location_fields``)."""

    ref: Ref
    purpose: str = Field(max_length=PURPOSE_MAX_LENGTH)
    bindings: list[Binding] = Field(min_length=1)
    fields: SecretFields = "value"
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
    def _fields_of_every_place(self) -> Self:
        if any(location_fields(b.location) != self.fields for b in self.bindings):
            raise ValueError("every place of the secret must take what it is made of (fields)")
        return self

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


# --- A program's request (``POST /internal/out``) -------------------------------------------------

# Where an address leads, seen from the gatekeeper: a device of the house, the internet, the
# machine that hosts Domo, or nowhere a program may go.
Zone = Literal["house", "internet", "host", "never"]


class ProviderZone(_Model):
    """Where a declared provider's base URL leads (``GET /internal/providers/{id}/zone``): the
    zone of its address as the gatekeeper resolves it, ``never`` when the name gives none."""

    zone: Zone


# Why the gatekeeper did not relay a program's request: the only thing said of a refusal or of a
# failure, never the text of an error.
OutRefusalCode = Literal[
    "closed_address",
    "never_reachable",
    "host_not_opened",
    "needs_https",
    "account_not_for_address",
    "account_not_found",
    "bad_redirect",
    "too_big",
    "too_slow",
    "unreachable",
    "bad_request",
    "zone_changed",
]

_OPEN_HOST_RE = re.compile(r"[a-z0-9._:\[\]-]+")


class OpenAddress(_Model):
    """An address a program may reach: ``host`` as the tool wrote it (a name or an IP literal,
    compared as written, in lower-case ASCII) and its port. ``on_host`` says it was opened as a
    port of the machine that hosts Domo, which is closed otherwise."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    on_host: bool = False

    @field_validator("host")
    @classmethod
    def _check_host(cls, host: str) -> str:
        host = host.lower()
        if _OPEN_HOST_RE.fullmatch(host) is None:
            raise ValueError("host must be a name or an IP literal, in ASCII")
        return host


OutMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


class OutRequest(_Model):
    """A request a program sends through the gatekeeper, to one of the ``open`` addresses only.

    ``account`` names the account the gatekeeper places in it (the program never sees it).
    ``seconds`` bounds the whole request, the lookup of the name and every redirect included;
    ``most_bytes`` the decoded body of the answer. With ``resolve_only``, nothing is sent: the
    answer only says the ``zone`` of the address (or why it is refused). With ``expect_zone``,
    the first address must lead to that zone on the very lookup the connection goes to, else the
    request is refused ``zone_changed`` and nothing is sent (the core judged that zone).
    """

    method: OutMethod
    url: str = Field(min_length=1, max_length=8192)
    headers: dict[str, str] = Field(default_factory=dict[str, str])
    body_b64: str | None = None
    account: Ref | None = None
    open: list[OpenAddress]
    seconds: int = Field(ge=1)
    most_bytes: int = Field(ge=0, le=ANSWER_CEILING_BYTES)
    session_idle_seconds: int = Field(ge=1)
    resolve_only: bool = False
    expect_zone: Zone | None = None


class OutResponse(_Model):
    """The answer to a program's request, scrubbed of every secret and of the session's cookies.

    ``zone`` is the zone of the address the request was sent to (before any redirect); ``hops``
    the redirects followed. A ``resolve_only`` request gets status 0, no header and no body.
    """

    status: int
    headers: dict[str, str]
    body_b64: str
    zone: Zone
    hops: int


# --- The spoken phrase (``/internal/phrases/{ref}``) ----------------------------------------------

# How a heard text compares with a person's phrase: it is the phrase, nearly (a failed try), or
# nothing like it (an ordinary sentence, not a try at all).
PhraseResult = Literal["it_is", "almost", "nothing_like"]


class PhraseSaid(_Model):
    """What was heard, sent to be kept or compared. Never logged, never echoed back."""

    heard: str = Field(min_length=1, max_length=400)


class PhraseEnrolled(_Model):
    """Where agreeing on a phrase stands: the first saying kept, both agreed (the phrase is now
    this one), the second not the same as the first (both forgotten), or too short to be one."""

    step: Literal["first_kept", "agreed", "not_the_same", "too_short"]


class PhraseCompared(_Model):
    result: PhraseResult
