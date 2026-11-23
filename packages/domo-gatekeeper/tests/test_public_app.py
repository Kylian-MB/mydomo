import logging
import re
import urllib.parse
from datetime import timedelta
from html.parser import HTMLParser

import httpx
import pytest
from domo_common.gatekeeper_api import (
    FORM_TEXT_KEYS,
    BasicLocation,
    Binding,
    HeaderLocation,
    LoginLocation,
    SecretRequestCreate,
)
from domo_common.i18n import t
from domo_common.meanings import SIGN
from domo_gatekeeper.public_app import MAX_VALUE_LENGTH, create_public_app
from domo_gatekeeper.vault import join_account
from markupsafe import escape

EXPECTED_CSP = "default-src 'self'; frame-ancestors 'self'; form-action 'self'"
FORM = {"content-type": "application/x-www-form-urlencoded"}


# The texts the core sends once Domo is installed: each one names its key, and the one with a
# hole keeps it for the gatekeeper to fill.
WRITTEN = {key: f"[{key}]" for key in FORM_TEXT_KEYS} | {
    "vault.form.error.length": "[vault.form.error.length] {max}"
}
# The texts the form itself shows.
FORM_KEYS = (
    "vault.form.title",
    "vault.form.purpose",
    "vault.form.destinations",
    "vault.form.privacy",
    "vault.form.value_label",
    "vault.form.submit",
)
LOCK = "🔒"


def token_of(form_path: str) -> str:
    return form_path.rsplit("/", 1)[1]


class _Shown(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.texts.append(data.strip())


def shown(html: str) -> list[str]:
    """Each text the page shows, between its tags, as a person reads it."""
    parser = _Shown()
    parser.feed(html)
    return parser.texts


def written(request: SecretRequestCreate, language: str = "it") -> SecretRequestCreate:
    """``request`` with the texts of its form, in ``language``, as the core sends it."""
    return SecretRequestCreate.model_validate(
        {**request.model_dump(mode="json"), "language": language, "texts": WRITTEN}
    )


@pytest.fixture
def written_path(store, vault, req) -> str:
    """The form path of a pending request that carries the texts of its form (``WRITTEN``)."""
    path = store.create(written(req), vault).form_path
    assert path is not None
    return path


def assert_hardened(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["x-domo-component"] == "gatekeeper"


async def test_form_shows_purpose_and_hosts(public_client, form_path, req):
    r = await public_client.get(form_path, headers={"accept-language": "fr"})
    assert r.status_code == 200
    assert "api.example.com" in r.text and req.purpose in r.text and 'type="password"' in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-domo-component"] == "gatekeeper"


async def test_form_responses_carry_the_security_headers(public_client, form_path):
    r = await public_client.get(form_path)
    assert r.headers["content-type"].startswith("text/html")
    assert_hardened(r)


async def test_form_is_a_javascript_free_password_field_posting_to_the_same_path(
    public_client, form_path
):
    html = (await public_client.get(form_path)).text
    assert re.search(r'<input[^>]*\bname="value"', html)
    assert re.search(r'<input[^>]*\btype="password"', html)
    assert re.search(r'<input[^>]*\bautocomplete="off"', html)
    assert re.search(r'<form[^>]*\bmethod="post"', html, re.IGNORECASE)
    assert not re.search(r"<form[^>]*\baction=", html, re.IGNORECASE)  # same path
    assert "<script" not in html.lower()
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)  # no inline handlers
    assert 'name="viewport"' in html
    assert str(MAX_VALUE_LENGTH) in html  # maxlength attribute


async def test_form_does_not_contain_the_token(public_client, form_path):
    assert token_of(form_path) not in (await public_client.get(form_path)).text


async def test_form_language_follows_accept_language(public_client, form_path):
    fr = await public_client.get(form_path, headers={"accept-language": "fr-FR,fr;q=0.9"})
    en = await public_client.get(form_path, headers={"accept-language": "en-US"})
    default = await public_client.get(form_path)
    unsupported = await public_client.get(form_path, headers={"accept-language": "de-DE"})
    assert '<html lang="fr">' in fr.text and '<html lang="en">' in en.text
    assert fr.text != en.text
    assert default.text == en.text == unsupported.text


@pytest.mark.parametrize("lang", ["en", "fr"])
async def test_form_says_the_value_goes_straight_to_the_vault(public_client, form_path, lang):
    html = (await public_client.get(form_path, headers={"accept-language": lang})).text
    privacy = t("vault.form.privacy", lang)
    assert "assistant" in privacy
    assert str(escape(privacy)) in html


async def test_form_lists_every_destination_with_its_port_and_header(public_client, store, vault):
    request = SecretRequestCreate(
        ref="provider:multi:api_key",
        purpose="multi",
        bindings=[
            Binding(
                host="api.example.com",
                location=HeaderLocation(kind="header", name="x-api-key"),
            ),
            Binding(
                host="192.168.1.10",
                port=8443,
                location=HeaderLocation(
                    kind="header", name="authorization", template="Bearer {value}"
                ),
            ),
        ],
    )
    path = store.create(request, vault).form_path
    html = (await public_client.get(path)).text
    assert "api.example.com" in html and "x-api-key" in html
    assert "192.168.1.10:8443" in html and "authorization" in html


async def test_untrusted_text_is_html_escaped(public_client, store, vault):
    request = SecretRequestCreate(
        ref="provider:xss:api_key",
        purpose='<script>alert("x")</script>',
        bindings=[
            Binding(
                host="api.example.com",
                location=HeaderLocation(kind="header", name="x-api-key"),
            )
        ],
    )
    html = (await public_client.get(store.create(request, vault).form_path)).text
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html


async def test_submit_stores_value_and_never_echoes_it(public_client, form_path, vault, req):
    r = await public_client.post(form_path, data={"value": "sk-test-123"})
    assert r.status_code == 200 and "sk-test-123" not in r.text
    assert vault.reveal(req.ref) == "sk-test-123"
    assert_hardened(r)


async def test_submit_stores_the_requested_purpose_and_bindings(
    public_client, form_path, vault, req
):
    await public_client.post(form_path, data={"value": "sk-test-123"})
    meta = vault.metadata(req.ref)
    assert meta.purpose == req.purpose and meta.bindings == list(req.bindings)


async def test_saved_page_is_localised_and_javascript_free(public_client, form_path):
    r = await public_client.post(
        form_path, data={"value": "sk-test-123"}, headers={"accept-language": "fr"}
    )
    assert '<html lang="fr">' in r.text and "<script" not in r.text.lower()


async def test_submit_handles_special_and_non_ascii_characters(
    public_client, form_path, vault, req
):
    value = "p@ss w&rd=%+é€\xa0'\"<>"
    r = await public_client.post(form_path, data={"value": value})
    assert r.status_code == 200
    assert vault.reveal(req.ref) == value


async def test_a_submitted_request_cannot_be_used_again(public_client, form_path, vault, req):
    first = await public_client.post(form_path, data={"value": "v1"})
    second = await public_client.post(form_path, data={"value": "v2"})
    again = await public_client.get(form_path)
    assert (first.status_code, second.status_code, again.status_code) == (200, 404, 404)
    assert vault.reveal(req.ref) == "v1"
    assert_hardened(second)
    assert_hardened(again)


async def test_value_of_the_maximum_length_is_accepted(public_client, form_path, vault, req):
    value = "k" * MAX_VALUE_LENGTH
    r = await public_client.post(form_path, data={"value": value})
    assert r.status_code == 200 and vault.reveal(req.ref) == value


async def test_too_long_value_is_rejected_and_not_echoed(public_client, form_path, store, vault):
    value = "z-" * (MAX_VALUE_LENGTH // 2) + "xx"  # MAX_VALUE_LENGTH + 2 characters
    r = await public_client.post(form_path, data={"value": value})
    assert r.status_code == 400 and value not in r.text and 'type="password"' in r.text
    assert store.pending_by_token(token_of(form_path)) is not None
    assert_hardened(r)


async def test_empty_value_is_rejected_and_request_stays_pending(public_client, form_path, store):
    r = await public_client.post(form_path, data={"value": ""})
    assert r.status_code == 400
    assert store.pending_by_token(token_of(form_path)) is not None


async def test_rejected_submission_shows_a_localised_error_in_the_form(public_client, form_path):
    r = await public_client.post(form_path, data={"value": ""}, headers={"accept-language": "fr"})
    assert r.status_code == 400 and '<html lang="fr">' in r.text
    assert 'role="alert"' in r.text and 'type="password"' in r.text


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"", FORM),
        (b"other=1", FORM),
        (b'{"value": "v"}', {"content-type": "application/json"}),
        (b"value=v", {}),  # no content type: not a form
    ],
)
async def test_missing_value_field_is_a_400(public_client, form_path, store, body, headers):
    r = await public_client.post(form_path, content=body, headers=headers)
    assert r.status_code == 400
    assert store.pending_by_token(token_of(form_path)) is not None


async def test_file_upload_in_place_of_the_value_is_a_400_that_echoes_nothing(
    public_client, form_path, store
):
    r = await public_client.post(
        form_path, files={"value": ("v.txt", b"sk-from-a-file", "text/plain")}
    )
    assert r.status_code == 400 and "sk-from-a-file" not in r.text
    assert store.pending_by_token(token_of(form_path)) is not None


async def test_unknown_token_is_404(public_client):
    assert (await public_client.get("/vault/r/unknown")).status_code == 404


async def test_unknown_token_pages_are_hardened_and_localised(public_client):
    r = await public_client.get("/vault/r/unknown", headers={"accept-language": "fr"})
    assert_hardened(r)
    assert '<html lang="fr">' in r.text


async def test_submitting_to_an_unknown_token_is_404_and_stores_nothing(public_client, vault):
    r = await public_client.post("/vault/r/unknown", data={"value": "sk-test-123"})
    assert r.status_code == 404 and "sk-test-123" not in r.text
    assert vault.list_metadata() == []


async def test_superseded_link_is_404(public_client, store, vault, req, form_path):
    store.create(req, vault)
    assert (await public_client.get(form_path)).status_code == 404
    assert (await public_client.post(form_path, data={"value": "v"})).status_code == 404
    assert vault.list_metadata() == []


async def test_expired_link_is_404(store_with_clock, vault, req, clock):
    path = store_with_clock.create(req, vault).form_path
    transport = httpx.ASGITransport(app=create_public_app(store_with_clock, vault))
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        assert (await client.get(path)).status_code == 200
        clock.advance(timedelta(minutes=16))
        assert (await client.get(path)).status_code == 404
        assert (await client.post(path, data={"value": "v"})).status_code == 404
    assert vault.list_metadata() == []


async def test_conflicting_bindings_found_at_submission_are_a_409(
    public_client, store, vault, req, form_path
):
    other = Binding(
        host="evil.example.net", location=HeaderLocation(kind="header", name="x-api-key")
    )
    vault.put(req.ref, "existing", purpose="p", bindings=[other])
    r = await public_client.post(form_path, data={"value": "sk-test-123"})
    assert r.status_code == 409 and "sk-test-123" not in r.text
    assert vault.reveal(req.ref) == "existing"
    assert_hardened(r)


async def test_a_lone_surrogate_is_a_400_like_an_empty_value(
    public_client, form_path, store, vault, monkeypatch
):
    # Starlette replaces invalid UTF-8 with U+FFFD, so a lone surrogate cannot normally reach
    # the app; let it through, as a different parser could, to prove the vault's guard holds.
    import starlette.formparsers

    original = urllib.parse.unquote_plus
    monkeypatch.setattr(
        starlette.formparsers,
        "unquote_plus",
        lambda text: original(text, errors="surrogatepass"),
    )
    r = await public_client.post(form_path, content=b"value=%ED%A0%80sk-test-123", headers=FORM)
    assert r.status_code == 400
    assert "sk-test-123" not in r.text and "ud800" not in r.text.lower()
    assert store.pending_by_token(token_of(form_path)) is not None
    assert vault.list_metadata() == []


async def test_oversized_body_is_rejected_before_it_is_read(public_client, form_path, store):
    r = await public_client.post(form_path, content=b"value=" + b"a" * 300_000, headers=FORM)
    assert r.status_code == 413
    assert_hardened(r)
    assert store.pending_by_token(token_of(form_path)) is not None


async def test_the_value_is_never_logged(public_client, form_path, caplog):
    caplog.set_level(logging.DEBUG)
    await public_client.post(form_path, data={"value": "sk-never-logged-123"})
    await public_client.post(form_path, data={"value": "sk-never-logged-456"})  # refused: used
    assert "sk-never-logged" not in caplog.text


@pytest.mark.parametrize("path", ["/", "/docs", "/redoc", "/openapi.json", "/vault/r/"])
async def test_nothing_else_is_exposed(public_client, path):
    r = await public_client.get(path)
    assert r.status_code == 404
    assert_hardened(r)


async def test_other_methods_on_the_form_path_are_refused(public_client, form_path):
    for method in ("PUT", "DELETE", "PATCH"):
        r = await public_client.request(method, form_path)
        assert r.status_code == 405
        assert_hardened(r)


async def test_an_unexpected_error_is_a_hardened_500_that_leaks_nothing(
    store, vault, form_path, monkeypatch
):
    def boom(token: str) -> None:
        raise RuntimeError("internal detail sk-leaky-123")

    monkeypatch.setattr(store, "pending_by_token", boom)
    transport = httpx.ASGITransport(app=create_public_app(store, vault), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        r = await client.post(form_path, data={"value": "sk-test-123"})
    assert r.status_code == 500
    assert "sk-leaky-123" not in r.text and "sk-test-123" not in r.text
    assert_hardened(r)


# --- the texts of the form: Domo's, or the installation's (spec 4.5) ----------------------------


async def test_the_form_shows_only_the_texts_it_was_sent(public_client, written_path, req):
    # The language of the texts, whatever the browser prefers.
    form = await public_client.get(written_path, headers={"accept-language": "fr"})
    assert form.status_code == 200 and '<html lang="it">' in form.text
    address = {LOCK, "api.example.com", "x-api-key"}
    sent = {WRITTEN[key] for key in FORM_KEYS} | {req.purpose}
    assert set(shown(form.text)) == sent | address
    # A value refused: the form again, its error with the hole the gatekeeper fills itself.
    refused = await public_client.post(written_path, data={"value": ""})
    assert refused.status_code == 400
    assert set(shown(refused.text)) == sent | address | {
        f"[vault.form.error.length] {MAX_VALUE_LENGTH}"
    }
    saved = await public_client.post(written_path, data={"value": "sk-test-123"})
    assert saved.status_code == 200 and '<html lang="it">' in saved.text
    assert set(shown(saved.text)) == {WRITTEN["vault.saved.title"], WRITTEN["vault.saved.message"]}


async def test_the_pages_of_a_request_say_its_texts(public_client, store, vault, req):
    path = store.create(written(req), vault).form_path
    other = Binding(
        host="evil.example.net", location=HeaderLocation(kind="header", name="x-api-key")
    )
    too_large = await public_client.post(path, content=b"value=" + b"a" * 300_000, headers=FORM)
    assert too_large.status_code == 413
    assert set(shown(too_large.text)) == {
        WRITTEN["vault.too_large.title"],
        WRITTEN["vault.too_large.message"],
    }
    vault.put(req.ref, "existing", purpose="p", bindings=[other])
    conflict = await public_client.post(path, data={"value": "sk-test-123"})
    assert conflict.status_code == 409
    assert set(shown(conflict.text)) == {
        WRITTEN["vault.conflict.title"],
        WRITTEN["vault.conflict.message"],
    }


@pytest.mark.parametrize("with_texts", [True, False])
async def test_each_address_is_shown_by_the_gatekeeper(public_client, store, vault, with_texts):
    request = SecretRequestCreate(
        ref="provider:multi:api_key",
        purpose="multi",
        bindings=[
            Binding(
                host="api.example.com",
                location=HeaderLocation(kind="header", name="x-api-key"),
            ),
            Binding(
                host="192.168.1.10",
                port=8443,
                location=HeaderLocation(
                    kind="header", name="authorization", template="Bearer {value}"
                ),
            ),
        ],
    )
    path = store.create(written(request) if with_texts else request, vault).form_path
    html = (await public_client.get(path)).text
    # A symbol, the host in bold, the header in code: no words, whatever the texts.
    addresses = [shown(item) for item in re.findall(r"<li>(.*?)</li>", html, re.DOTALL)]
    assert addresses == [
        [LOCK, "api.example.com", "x-api-key"],
        [LOCK, "192.168.1.10:8443", "authorization"],
    ]
    assert "<strong>api.example.com</strong>" in html and "<code>x-api-key</code>" in html
    assert "<strong>192.168.1.10:8443</strong>" in html and "<code>authorization</code>" in html


@pytest.mark.parametrize("lang", ["en", "fr"])
async def test_without_texts_the_form_keeps_its_fixed_texts(public_client, form_path, req, lang):
    form = await public_client.get(form_path, headers={"accept-language": lang})
    assert f'<html lang="{lang}">' in form.text
    fixed = {t(key, lang) for key in FORM_KEYS} | {req.purpose}
    assert set(shown(form.text)) == fixed | {LOCK, "api.example.com", "x-api-key"}
    saved = await public_client.post(
        form_path, data={"value": "sk-test-123"}, headers={"accept-language": lang}
    )
    assert set(shown(saved.text)) == {t("vault.saved.title", lang), t("vault.saved.message", lang)}


# --- an account: a user and a password --------------------------------------------------------

BASIC = Binding(host="192.168.1.20", port=8080, location=BasicLocation(kind="basic"))
LOGIN = Binding(
    host="site.example.com",
    location=LoginLocation(
        kind="login",
        path="/api/login",
        encoding="json",
        user_field="username",
        password_field="password",
    ),
)
ACCOUNT_KEYS = (
    "vault.form.title",
    "vault.form.purpose",
    "vault.form.destinations",
    "vault.form.privacy",
    "vault.form.user_label",
    "vault.form.password_label",
    "vault.form.submit",
)


@pytest.fixture
def account_req() -> SecretRequestCreate:
    return SecretRequestCreate(
        ref="account:house",
        purpose="The account of the house",
        bindings=[BASIC, LOGIN],
        fields="user_password",
    )


@pytest.fixture
def account_path(store, vault, account_req) -> str:
    path = store.create(account_req, vault).form_path
    assert path is not None
    return path


async def test_an_account_form_has_a_user_field_and_a_password_field(public_client, account_path):
    html = (await public_client.get(account_path)).text
    assert re.search(r'<input[^>]*\bname="user"[^>]*\btype="text"', html)
    assert re.search(r'<input[^>]*\bname="password"[^>]*\btype="password"', html)
    assert not re.search(r'<input[^>]*\bname="value"', html)
    assert "<script" not in html.lower()


async def test_an_account_form_stores_both_values(public_client, account_path, vault, account_req):
    r = await public_client.post(
        account_path, data={"user": "house-user", "password": "s3cr3t-password"}
    )
    assert r.status_code == 200
    assert "house-user" not in r.text and "s3cr3t-password" not in r.text
    assert vault.reveal(account_req.ref) == join_account("house-user", "s3cr3t-password")
    meta = vault.metadata(account_req.ref)
    assert meta is not None and set(meta.bindings) == {BASIC, LOGIN}
    assert_hardened(r)


@pytest.mark.parametrize(
    "data",
    [
        {"user": "house-user"},
        {"password": "s3cr3t-password"},
        {"user": "", "password": "s3cr3t-password"},
        {"user": "house-user", "password": ""},
        {"user": "house-user", "password": "p" * (MAX_VALUE_LENGTH + 1)},
        {"user": "u" * (MAX_VALUE_LENGTH + 1), "password": "s3cr3t-password"},
        {"value": "s3cr3t-password"},
    ],
)
async def test_an_account_needs_both_values(public_client, account_path, store, vault, data):
    r = await public_client.post(account_path, data=data)
    assert r.status_code == 400 and 'role="alert"' in r.text
    assert "s3cr3t-password" not in r.text and "house-user" not in r.text
    assert store.pending_by_token(token_of(account_path)) is not None
    assert vault.list_metadata() == []


async def test_a_basic_user_with_a_colon_is_refused_in_the_form(public_client, account_path, store):
    r = await public_client.post(account_path, data={"user": "a:b", "password": "pw-123456"})
    assert r.status_code == 400 and "pw-123456" not in r.text
    assert store.pending_by_token(token_of(account_path)) is not None


async def test_both_values_of_the_longest_length_are_accepted(
    public_client, account_path, vault, account_req
):
    user, password = "€" * MAX_VALUE_LENGTH, "\U0001f511" * MAX_VALUE_LENGTH
    r = await public_client.post(account_path, data={"user": user, "password": password})
    assert r.status_code == 200
    assert vault.reveal(account_req.ref) == join_account(user, password)


async def test_an_account_form_shows_only_the_texts_it_was_sent(
    public_client, store, vault, account_req
):
    path = store.create(written(account_req), vault).form_path
    form = await public_client.get(path)
    places = {LOCK, "192.168.1.20:8080", "authorization: Basic", "site.example.com"}
    places |= {"POST /api/login"}
    sent = {WRITTEN[key] for key in ACCOUNT_KEYS} | {account_req.purpose}
    assert set(shown(form.text)) == sent | places


@pytest.mark.parametrize("lang", ["en", "fr"])
async def test_without_texts_an_account_form_keeps_its_fixed_texts(
    public_client, account_path, account_req, lang
):
    form = await public_client.get(account_path, headers={"accept-language": lang})
    fixed = {t(key, lang) for key in ACCOUNT_KEYS} | {account_req.purpose}
    places = {LOCK, "192.168.1.20:8080", "authorization: Basic", "site.example.com"}
    places |= {"POST /api/login"}
    assert set(shown(form.text)) == fixed | places


async def test_each_place_of_an_account_is_shown_without_words(public_client, account_path):
    html = (await public_client.get(account_path)).text
    addresses = [shown(item) for item in re.findall(r"<li>(.*?)</li>", html, re.DOTALL)]
    assert addresses == [
        [LOCK, "192.168.1.20:8080", "authorization: Basic"],
        [LOCK, "site.example.com", "POST /api/login"],
    ]


async def test_an_unknown_token_shows_a_sign(public_client):
    for response in (
        await public_client.get("/vault/r/unknown", headers={"accept-language": "fr"}),
        await public_client.post("/vault/r/unknown", data={"value": "sk-test-123"}),
    ):
        assert response.status_code == 404
        # A sign without words: Domo's page, around the form, says what it is about.
        assert set(shown(response.text)) == {SIGN}
        assert_hardened(response)


async def test_a_link_used_up_shows_a_sign(public_client, written_path):
    await public_client.post(written_path, data={"value": "sk-test-123"})
    again = await public_client.get(written_path)
    assert again.status_code == 404 and set(shown(again.text)) == {SIGN}


async def test_the_error_page_of_a_known_request_says_its_texts(store, vault, req, monkeypatch):
    path = store.create(written(req), vault).form_path

    def boom(*args: object) -> None:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(store, "fulfil", boom)
    transport = httpx.ASGITransport(app=create_public_app(store, vault), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        r = await client.post(path, data={"value": "sk-test-123"})
    assert r.status_code == 500 and '<html lang="it">' in r.text
    assert set(shown(r.text)) == {WRITTEN["vault.error.title"], WRITTEN["vault.error.message"]}
    assert "sk-test-123" not in r.text and "internal detail" not in r.text
    assert_hardened(r)


async def test_the_error_page_of_an_unknown_request_shows_a_sign(store, vault, monkeypatch):
    def boom(token: str) -> None:
        raise RuntimeError("the store does not answer")

    monkeypatch.setattr(store, "pending_by_token", boom)
    transport = httpx.ASGITransport(app=create_public_app(store, vault), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        r = await client.get("/vault/r/some-token")
    assert r.status_code == 500 and set(shown(r.text)) == {SIGN}
    assert_hardened(r)
