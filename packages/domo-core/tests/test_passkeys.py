"""Passkeys (task 12): a person adds one from their account page, then signs in with it, without a
password.

Passkeys only work when the page is opened by a host name (D5): browsers refuse WebAuthn on an
IP address, so there the pages explain why and keep the password. The relying party id is the
host name of the request, and it must be one of the site addresses (``DOMO_SITE_ADDRESSES``);
the origin is ``https://``, that name and the configured HTTPS port (``DOMO_HTTPS_PORT``): a
request that names another port gets no passkey.

The ceremonies are checked with real signatures: ``soft_authenticator`` (``passkey_support.py``)
answers the core's options as a browser and its authenticator would, with ES256 keys made for the
test, and py_webauthn verifies them for real. What this does not cover is the browser itself
(``static/passkeys.js`` and ``navigator.credentials``): the end-to-end test of task 15 does, with
Chromium's virtual authenticator.
"""

import asyncio
import html
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources

import pytest
from domo_common.i18n import t
from domo_core.accounts import passkeys
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.passkey_models import PasskeyChallenge, PasskeyCredential
from domo_core.accounts.passkey_routes import MAX_LOGIN_OPTIONS_PER_ADDRESS
from domo_core.accounts.passkeys import (
    CHALLENGE_LIFETIME,
    MAX_PASSKEY_LABEL_LENGTH,
    MAX_PASSKEYS_PER_ACCOUNT,
    PasskeyRejected,
    finish_login,
    finish_proof,
    finish_registration,
    login_options,
    passkey_availability,
    proof_options,
    registration_options,
    remove_passkey,
    rp_id_for,
    site_names,
)
from domo_core.accounts.throttle import LoginThrottle
from domo_core.config import CoreSettings
from domo_core.web.deps import get_settings
from domo_core.web.templating import static_url
from passkey_support import b64url, from_b64url
from pydantic import ValidationError
from sqlalchemy import func, select
from web_support import (
    ADULT_PASSWORD,
    ADULT_USERNAME,
    audit_entries,
    cookie_attributes,
    house_reads,
    in_language,
    log_in,
    reads_in,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
ORIGIN = "https://localhost:8443"
WRONG_PASSWORD = "wrong-password-x"


def plain(page: str) -> str:
    """What a person reads in a page."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def attribute(page: str, name: str) -> str:
    """The value of the one ``data-<name>`` attribute of the page."""
    values = re.findall(rf'data-{name}="([^"]*)"', page)
    assert len(values) == 1, values
    return html.unescape(values[0])


async def count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def stored_passkeys(session, account_id: uuid.UUID) -> list[PasskeyCredential]:
    result = await session.execute(
        select(PasskeyCredential).where(PasskeyCredential.account_id == account_id)
    )
    return list(result.scalars())


async def registered(session, account, soft_authenticator, label="laptop", **create_options):
    """A passkey of ``account``, registered on ``localhost`` at ``NOW``."""
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts, **create_options)
    return await finish_registration(session, account, "localhost", ORIGIN, payload, label, NOW)


async def log_in_with(session, soft_authenticator, cred, now=NOW, **get_options) -> Account:
    login = await login_options(session, "localhost", now)
    payload = soft_authenticator.get(login, cred, **get_options)
    return await finish_login(session, "localhost", ORIGIN, payload, now)


# --- where passkeys work ----------------------------------------------------------------------


def test_rp_id_rejects_ip_literals():
    assert rp_id_for("192.168.1.10:8443") is None and rp_id_for("[::1]:8443") is None
    assert rp_id_for("domo.local:8443") == "domo.local" and rp_id_for("localhost") == "localhost"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("", None),
        ("DOMO.Local:8443", "domo.local"),
        ("domo.example.net", "domo.example.net"),
        ("10.0.0.1", None),
        # What a browser reads as an IPv4 address too: fewer parts, hexadecimal.
        ("127.1", None),
        ("0x7f.1:8443", None),
        ("[::1]", None),
        ("[2001:db8::1]:443", None),
        ("localhost.", None),
        (".local", None),
        ("domo..local", None),
        ("domo.local:", None),
        ("domo.local:99999", None),
        ("user@domo.local", None),
        ("domo local", None),
    ],
)
def test_rp_id_is_the_host_name_without_the_port(host, expected):
    assert rp_id_for(host) == expected


def test_the_site_addresses_give_the_host_names_passkeys_may_use():
    assert site_names("localhost, 192.168.1.10") == {"localhost"}
    assert site_names("domo.local,localhost  Domo.Example.net:8443") == {
        "domo.local",
        "localhost",
        "domo.example.net",
    }
    assert site_names("https://domo.local") == {"domo.local"}
    assert site_names("192.168.1.10") == set()
    assert site_names("") == set()


@pytest.mark.parametrize(
    ("host", "scheme", "addresses", "port", "rp_id", "origin"),
    [
        ("localhost:8443", "https", "localhost", 8443, "localhost", "https://localhost:8443"),
        ("LocalHost:8443", "https", "localhost", 8443, "localhost", "https://localhost:8443"),
        # A browser leaves the default port out of the origin (and of Host).
        ("localhost", "https", "localhost", 443, "localhost", "https://localhost"),
        ("localhost:443", "https", "localhost", 443, "localhost", "https://localhost"),
        (
            "domo.local:8443",
            "https",
            "192.168.1.10, domo.local",
            8443,
            "domo.local",
            "https://domo.local:8443",
        ),
    ],
)
def test_passkeys_work_on_a_site_name_over_https(host, scheme, addresses, port, rp_id, origin):
    availability = passkey_availability(host, scheme, addresses, port)
    assert availability.unavailable is None
    assert availability.site is not None
    assert (availability.site.rp_id, availability.site.origin) == (rp_id, origin)


@pytest.mark.parametrize(
    ("host", "scheme", "addresses", "reason"),
    [
        ("192.168.1.10:8443", "https", "localhost, 192.168.1.10", "ip"),
        ("192.168.1.10", "http", "192.168.1.10", "ip"),
        ("[::1]:8443", "https", "localhost", "ip"),
        # A name that is not one of the site addresses (the edge would not have sent it).
        ("domo.local:8443", "https", "localhost", "address"),
        # Not over HTTPS (X-Forwarded-Proto, as the trusted edge set it, says so).
        ("localhost:8443", "http", "localhost", "address"),
        (None, "https", "localhost", "address"),
        ("", "https", "localhost", "address"),
        # Another port than the page's (DOMO_HTTPS_PORT, 8443 here): the origin the core expects
        # never comes from the request.
        ("localhost:9443", "https", "localhost", "address"),
        ("localhost", "https", "localhost", "address"),
        ("localhost:443", "https", "localhost", "address"),
    ],
)
def test_passkeys_are_unavailable_elsewhere(host, scheme, addresses, reason):
    availability = passkey_availability(host, scheme, addresses, 8443)
    assert availability.site is None
    assert availability.unavailable == reason


def test_the_core_reads_the_site_addresses_of_the_edge(monkeypatch):
    assert CoreSettings().site_addresses == "localhost"
    monkeypatch.setenv("DOMO_SITE_ADDRESSES", "192.168.1.10, domo.local")
    assert CoreSettings().site_addresses == "192.168.1.10, domo.local"
    monkeypatch.setenv("DOMO_SITE_ADDRESSES", " ")
    assert CoreSettings().site_addresses == "localhost"


def test_the_core_reads_the_https_port_of_the_edge(monkeypatch):
    assert CoreSettings().https_port == 8443
    monkeypatch.setenv("DOMO_HTTPS_PORT", "443")
    assert CoreSettings().https_port == 443
    monkeypatch.setenv("DOMO_HTTPS_PORT", " ")
    assert CoreSettings().https_port == 8443
    for wrong in ("0", "65536", "port"):
        monkeypatch.setenv("DOMO_HTTPS_PORT", wrong)
        with pytest.raises(ValidationError):
            CoreSettings()


# --- registration -----------------------------------------------------------------------------


async def test_registration_and_login_roundtrip(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    cred = await finish_registration(
        session,
        account,
        "localhost",
        "https://localhost:8443",
        soft_authenticator.create(opts),
        "laptop",
        NOW,
    )
    login = await login_options(session, "localhost", NOW)
    assert (
        await finish_login(
            session, "localhost", "https://localhost:8443", soft_authenticator.get(login, cred), NOW
        )
    ).id == account.id


async def test_challenge_is_single_use(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    await finish_registration(
        session, account, "localhost", "https://localhost:8443", payload, "a", NOW
    )
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session, account, "localhost", "https://localhost:8443", payload, "b", NOW
        )
    assert refused.value.reason == "challenge"


async def test_wrong_origin_is_rejected(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session,
            account,
            "localhost",
            "https://evil.example.net",
            soft_authenticator.create(opts),
            "x",
            NOW,
        )
    assert refused.value.reason == "verification"


async def test_registration_options_ask_for_a_resident_key_and_user_verification(session, account):
    opts = await registration_options(session, account, "localhost", NOW)
    assert opts["rp"] == {"name": "Domo", "id": "localhost"}
    selection = opts["authenticatorSelection"]
    assert selection["residentKey"] == "required"
    assert selection["requireResidentKey"] is True
    assert selection["userVerification"] == "required"
    assert opts["attestation"] == "none"
    # The user handle is the account's id: no name, no personal data.
    assert from_b64url(opts["user"]["id"]) == account.id.bytes
    assert opts["user"]["name"] == opts["user"]["displayName"] == account.username
    assert opts["timeout"] == CHALLENGE_LIFETIME / timedelta(milliseconds=1)
    assert opts["excludeCredentials"] == []
    assert {-7, -8, -257} <= {param["alg"] for param in opts["pubKeyCredParams"]}


async def test_the_challenge_is_kept_by_the_core_for_five_minutes(session, account):
    opts = await registration_options(session, account, "localhost", NOW)
    challenge = (await session.execute(select(PasskeyChallenge))).scalar_one()
    assert challenge.purpose == "register"
    assert challenge.account_id == account.id
    assert challenge.challenge == from_b64url(opts["challenge"])
    assert len(challenge.challenge) >= 32
    assert challenge.expires_at == NOW + timedelta(minutes=5)
    assert challenge.used_at is None


async def test_a_registration_is_stored_with_its_public_key_only(
    session, account, soft_authenticator
):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    cred = await finish_registration(
        session, account, "localhost", ORIGIN, payload, "  laptop  ", NOW
    )
    assert cred.account_id == account.id
    assert cred.label == "laptop"
    assert cred.credential_id == from_b64url(payload["rawId"])
    assert cred.sign_count == 0
    assert cred.transports == ["internal", "hybrid"]
    assert cred.created_at == NOW and cred.last_used_at is None
    assert await stored_passkeys(session, account.id) == [cred]


async def test_a_passkey_that_cannot_verify_its_user_is_refused(
    session, account, soft_authenticator
):
    """A passkey replaces the password: a key that only proves it was touched (no PIN, no
    fingerprint) is refused when it is added, rather than stored and never usable."""
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts, user_verified=False)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "verification"
    assert await stored_passkeys(session, account.id) == []


@pytest.mark.parametrize(("length", "accepted"), [(1023, True), (1024, False)])
async def test_a_credential_id_is_1023_bytes_at_most(
    session, account, soft_authenticator, length, accepted
):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts, credential_id=b"k" * length)
    if accepted:
        cred = await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
        assert len(cred.credential_id) == length
        return
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "malformed"
    assert await stored_passkeys(session, account.id) == []


async def test_the_options_leave_out_the_passkeys_already_there(
    session, account, soft_authenticator
):
    cred = await registered(session, account, soft_authenticator)
    opts = await registration_options(session, account, "localhost", NOW)
    assert opts["excludeCredentials"] == [
        {"id": b64url(cred.credential_id), "type": "public-key", "transports": cred.transports}
    ]


async def test_a_challenge_is_good_for_five_minutes(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session, account, "localhost", ORIGIN, payload, "x", NOW + CHALLENGE_LIFETIME
        )
    assert refused.value.reason == "challenge"
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    just_in_time = NOW + CHALLENGE_LIFETIME - timedelta(seconds=1)
    await finish_registration(session, account, "localhost", ORIGIN, payload, "x", just_in_time)


async def test_a_spent_challenge_stays_spent_after_a_failure(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session, account, "localhost", "https://evil.example.net", payload, "x", NOW
        )
    assert refused.value.reason == "verification"
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "challenge"
    assert await stored_passkeys(session, account.id) == []


async def test_a_registration_challenge_is_only_good_for_its_account(
    session, account, add_person, add_account, soft_authenticator
):
    other = await add_account(await add_person("Sam", "adult"), "sam")
    opts = await registration_options(session, account, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session, other, "localhost", ORIGIN, soft_authenticator.create(opts), "x", NOW
        )
    assert refused.value.reason == "challenge"


async def test_a_login_challenge_cannot_register(session, account, soft_authenticator):
    login = await login_options(session, "localhost", NOW)
    made_up = {
        "rp": {"id": "localhost"},
        "user": {"id": b64url(account.id.bytes)},
        "challenge": login["challenge"],
    }
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session, account, "localhost", ORIGIN, soft_authenticator.create(made_up), "x", NOW
        )
    assert refused.value.reason == "challenge"


async def test_a_registration_for_another_rp_id_is_rejected(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts, rp_id="evil.example.net")
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "verification"


async def test_a_cross_origin_registration_is_rejected(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts, client_data_extra={"crossOrigin": True})
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "verification"


async def test_a_new_registration_request_replaces_the_previous_one(
    session, account, soft_authenticator
):
    first = await registration_options(session, account, "localhost", NOW)
    second = await registration_options(session, account, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(
            session, account, "localhost", ORIGIN, soft_authenticator.create(first), "x", NOW
        )
    assert refused.value.reason == "challenge"
    await finish_registration(
        session, account, "localhost", ORIGIN, soft_authenticator.create(second), "x", NOW
    )


@pytest.mark.parametrize("whose", ["same account", "another account"])
async def test_a_credential_belongs_to_exactly_one_account(
    session, account, add_person, add_account, soft_authenticator, whose
):
    cred = await registered(session, account, soft_authenticator)
    if whose == "same account":
        taker = account
    else:
        taker = await add_account(await add_person("Sam", "adult"), "sam")
    opts = await registration_options(session, taker, "localhost", NOW)
    payload = soft_authenticator.create(opts, credential_id=cred.credential_id)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, taker, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "taken"
    rows = (await session.execute(select(PasskeyCredential))).scalars().all()
    assert [(row.id, row.account_id) for row in rows] == [(cred.id, account.id)]


@pytest.mark.parametrize(
    "label",
    [
        "",
        "   ",
        "a" * (MAX_PASSKEY_LABEL_LENGTH + 1),
        "a\nb",
        "\N{RIGHT-TO-LEFT OVERRIDE}enohp",
        "a\x00b",
    ],
)
async def test_a_label_is_one_plain_line_of_60_characters_at_most(
    session, account, soft_authenticator, label
):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, label, NOW)
    assert refused.value.reason == "label"
    # The challenge was not spent: the same answer goes through with a good label.
    cred = await finish_registration(
        session, account, "localhost", ORIGIN, payload, "a" * MAX_PASSKEY_LABEL_LENGTH, NOW
    )
    assert cred.label == "a" * MAX_PASSKEY_LABEL_LENGTH


async def test_an_account_has_at_most_20_passkeys(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    for index in range(MAX_PASSKEYS_PER_ACCOUNT):
        session.add(
            PasskeyCredential(
                account_id=account.id,
                credential_id=bytes([index]) * 16,
                public_key=b"key",
                sign_count=0,
                transports=[],
                label=f"key {index}",
            )
        )
    await session.flush()
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "too_many"
    with pytest.raises(PasskeyRejected) as refused:
        await registration_options(session, account, "localhost", NOW)
    assert refused.value.reason == "too_many"


MALFORMED = [
    {},
    {"response": "x"},
    {"response": {}},
    {"response": {"clientDataJSON": 12}},
    {"response": {"clientDataJSON": "!!!"}},
    {"response": {"clientDataJSON": b64url(b"not json")}},
    {"response": {"clientDataJSON": b64url(b"[]")}},
    {"response": {"clientDataJSON": b64url(json.dumps({"type": "webauthn.create"}).encode())}},
]


@pytest.mark.parametrize("payload", MALFORMED)
async def test_a_malformed_registration_is_rejected(session, account, payload):
    await registration_options(session, account, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "malformed"


async def test_a_registration_with_a_broken_attestation_is_rejected(
    session, account, soft_authenticator
):
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.create(opts)
    payload["response"]["attestationObject"] = b64url(b"\xa0")
    with pytest.raises(PasskeyRejected) as refused:
        await finish_registration(session, account, "localhost", ORIGIN, payload, "x", NOW)
    assert refused.value.reason == "verification"


# --- login --------------------------------------------------------------------------------------


async def test_login_options_name_no_credential(session):
    opts = await login_options(session, "localhost", NOW)
    assert opts["rpId"] == "localhost"
    assert opts["userVerification"] == "required"
    assert opts["allowCredentials"] == []
    assert opts["timeout"] == CHALLENGE_LIFETIME / timedelta(milliseconds=1)
    challenge = (await session.execute(select(PasskeyChallenge))).scalar_one()
    assert (challenge.purpose, challenge.account_id) == ("login", None)
    assert challenge.challenge == from_b64url(opts["challenge"])
    assert challenge.expires_at == NOW + CHALLENGE_LIFETIME


async def test_login_records_the_counter_and_the_time(session, account, soft_authenticator):
    cred = await registered(session, account, soft_authenticator)
    later = NOW + timedelta(days=1)
    assert (await log_in_with(session, soft_authenticator, cred, now=later)).id == account.id
    await session.refresh(cred)
    assert cred.sign_count == 1
    assert cred.last_used_at == later


async def test_a_login_challenge_is_single_use(session, account, soft_authenticator):
    # An authenticator that does not count: only the challenge stops the replay.
    cred = await registered(session, account, soft_authenticator, counting=False)
    login = await login_options(session, "localhost", NOW)
    payload = soft_authenticator.get(login, cred)
    await finish_login(session, "localhost", ORIGIN, payload, NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_login(session, "localhost", ORIGIN, payload, NOW)
    assert refused.value.reason == "challenge"


async def test_a_login_challenge_expires_after_five_minutes(session, account, soft_authenticator):
    cred = await registered(session, account, soft_authenticator)
    login = await login_options(session, "localhost", NOW)
    payload = soft_authenticator.get(login, cred)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_login(session, "localhost", ORIGIN, payload, NOW + CHALLENGE_LIFETIME)
    assert refused.value.reason == "challenge"


async def test_a_registration_challenge_cannot_log_in(session, account, soft_authenticator):
    cred = await registered(session, account, soft_authenticator)
    opts = await registration_options(session, account, "localhost", NOW)
    payload = soft_authenticator.get({"challenge": opts["challenge"]}, cred)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_login(session, "localhost", ORIGIN, payload, NOW)
    assert refused.value.reason == "challenge"


@pytest.mark.parametrize(
    "lie",
    [
        {"origin": "https://evil.example.net"},
        {"origin": "https://localhost:9443"},
        {"origin": "http://localhost:8443"},
        {"rp_id": "evil.example.net"},
        {"tamper": True},
        {"client_data_extra": {"crossOrigin": True}},
        # Only touched: no PIN, no fingerprint. A passkey stands in for the password.
        {"user_verified": False},
    ],
)
async def test_a_login_that_does_not_check_out_is_rejected(
    session, account, soft_authenticator, lie
):
    cred = await registered(session, account, soft_authenticator)
    with pytest.raises(PasskeyRejected) as refused:
        await log_in_with(session, soft_authenticator, cred, **lie)
    assert refused.value.reason == "verification"
    await session.refresh(cred)
    assert (cred.sign_count, cred.last_used_at) == (0, None)


async def test_an_unknown_credential_is_rejected(session, account, soft_authenticator):
    opts = await registration_options(session, account, "localhost", NOW)
    never_registered = soft_authenticator.create(opts)
    with pytest.raises(PasskeyRejected) as refused:
        await log_in_with(session, soft_authenticator, from_b64url(never_registered["rawId"]))
    assert refused.value.reason == "unknown_credential"


async def test_the_user_handle_must_be_the_account_of_the_credential(
    session, account, add_person, add_account, soft_authenticator
):
    other = await add_account(await add_person("Sam", "adult"), "sam")
    cred = await registered(session, account, soft_authenticator)
    with pytest.raises(PasskeyRejected) as refused:
        await log_in_with(session, soft_authenticator, cred, user_handle=other.id.bytes)
    assert refused.value.reason == "verification"
    # An authenticator may leave it out.
    assert (await log_in_with(session, soft_authenticator, cred, user_handle=None)).id == account.id


async def test_the_counter_must_go_up(session, account, soft_authenticator):
    cred = await registered(session, account, soft_authenticator)
    await log_in_with(session, soft_authenticator, cred, sign_count=5)
    for replayed in (5, 4, 0):
        with pytest.raises(PasskeyRejected) as refused:
            await log_in_with(session, soft_authenticator, cred, sign_count=replayed)
        assert refused.value.reason == "verification"
    await log_in_with(session, soft_authenticator, cred, sign_count=6)
    await session.refresh(cred)
    assert cred.sign_count == 6


async def test_an_authenticator_that_does_not_count_keeps_working(
    session, account, soft_authenticator
):
    """0 and 0 is the one case the specification lets through: synced passkeys never count."""
    cred = await registered(session, account, soft_authenticator, counting=False)
    for _ in range(2):
        assert (await log_in_with(session, soft_authenticator, cred)).id == account.id
    await session.refresh(cred)
    assert cred.sign_count == 0


@pytest.mark.parametrize("left", ["removed", "stay_over"])
async def test_someone_no_longer_in_the_household_cannot_log_in_with_a_passkey(
    session, add_person, add_account, soft_authenticator, left
):
    """R77: a removed person, or a guest whose stay is over, is refused like a failed login."""
    person = await add_person("Alex", "adult" if left == "removed" else "guest")
    alex = await add_account(person, "alex")
    cred = await registered(session, alex, soft_authenticator)
    if left == "removed":
        person.disabled_at = NOW
        person.state = "gone"
    else:
        person.expires_at = NOW
    await session.flush()
    with pytest.raises(PasskeyRejected) as refused:
        await log_in_with(session, soft_authenticator, cred)
    assert refused.value.reason == "not_in_household"
    await session.refresh(cred)
    assert (cred.sign_count, cred.last_used_at) == (0, None)


MALFORMED_LOGINS = [
    {},
    {"id": "AA", "rawId": "AA", "type": "public-key", "response": {}},
    {
        "id": "AA",
        "rawId": "AA",
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url(b"not json"),
            "authenticatorData": "AA",
            "signature": "AA",
        },
    },
    {
        "id": "AA",
        "rawId": "AA",
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url(b"{}"),
            "authenticatorData": "AA",
            "signature": "AA",
            "userHandle": 12,
        },
    },
]


@pytest.mark.parametrize("payload", MALFORMED_LOGINS)
async def test_a_malformed_login_is_rejected(session, payload):
    await login_options(session, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_login(session, "localhost", ORIGIN, payload, NOW)
    assert refused.value.reason == "malformed"


async def test_finished_challenges_are_forgotten(session, account, soft_authenticator):
    cred = await registered(session, account, soft_authenticator)  # a used challenge
    await login_options(session, "localhost", NOW)  # one that will expire
    await login_options(session, "localhost", NOW + CHALLENGE_LIFETIME)
    remaining = (await session.execute(select(PasskeyChallenge))).scalars().all()
    assert [(c.purpose, c.expires_at) for c in remaining] == [
        ("login", NOW + 2 * CHALLENGE_LIFETIME)
    ]
    assert cred.id is not None


async def test_pending_login_challenges_are_capped(session, monkeypatch):
    """Anyone can ask for login options: the challenges that wait are bounded."""
    monkeypatch.setattr(passkeys, "MAX_PENDING_LOGIN_CHALLENGES", 3)
    for _ in range(3):
        await login_options(session, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await login_options(session, "localhost", NOW)
    assert refused.value.reason == "busy"
    assert await count(session, PasskeyChallenge) == 3
    # Once they have expired, there is room again.
    await login_options(session, "localhost", NOW + CHALLENGE_LIFETIME)


# --- proving it is the person (spec 4.5) --------------------------------------------------------


async def prove_with(session, account_id, soft_authenticator, cred, now=NOW, **get_options):
    opts = await proof_options(session, account_id, "localhost", now)
    payload = soft_authenticator.get(opts, cred, **get_options)
    await finish_proof(session, "localhost", ORIGIN, payload, account_id, now)


async def test_proof_options_name_the_passkeys_of_the_account(
    session, account, add_person, add_account, soft_authenticator
):
    other = await add_account(await add_person("Sam", "adult"), "sam")
    first = await registered(session, account, soft_authenticator)
    second = await registered(session, account, soft_authenticator, label="phone")
    await registered(session, other, soft_authenticator)
    opts = await proof_options(session, account.id, "localhost", NOW)
    assert opts["rpId"] == "localhost"
    # The person is verified (fingerprint, face, PIN): a touch proves nothing.
    assert opts["userVerification"] == "required"
    assert sorted(item["id"] for item in opts["allowCredentials"]) == sorted(
        [b64url(first.credential_id), b64url(second.credential_id)]
    )
    stored = (
        await session.execute(select(PasskeyChallenge).where(PasskeyChallenge.purpose == "proof"))
    ).scalar_one()
    assert (stored.account_id, stored.expires_at) == (account.id, NOW + CHALLENGE_LIFETIME)
    assert stored.challenge == from_b64url(opts["challenge"])


async def test_proof_options_need_a_passkey(session, account):
    with pytest.raises(PasskeyRejected) as refused:
        await proof_options(session, account.id, "localhost", NOW)
    assert refused.value.reason == "no_passkey"
    assert await count(session, PasskeyChallenge) == 0


async def test_a_proof_records_the_counter_and_the_time(session, account, soft_authenticator):
    cred = await registered(session, account, soft_authenticator)
    later = NOW + timedelta(hours=1)
    await prove_with(session, account.id, soft_authenticator, cred, now=later)
    await session.refresh(cred)
    assert (cred.sign_count, cred.last_used_at) == (1, later)
    stored = (
        await session.execute(select(PasskeyChallenge).where(PasskeyChallenge.purpose == "proof"))
    ).scalar_one()
    assert stored.used_at == later


async def test_a_proof_challenge_is_good_for_one_answer_of_its_account(
    session, account, add_person, add_account, soft_authenticator
):
    other = await add_account(await add_person("Sam", "adult"), "sam")
    cred = await registered(session, account, soft_authenticator, counting=False)
    others = await registered(session, other, soft_authenticator)
    opts = await proof_options(session, account.id, "localhost", NOW)
    payload = soft_authenticator.get(opts, cred)
    # Not for another account, even with that account's passkey answering.
    with pytest.raises(PasskeyRejected) as refused:
        await finish_proof(session, "localhost", ORIGIN, payload, other.id, NOW)
    assert refused.value.reason == "challenge"
    await finish_proof(session, "localhost", ORIGIN, payload, account.id, NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_proof(session, "localhost", ORIGIN, payload, account.id, NOW)
    assert refused.value.reason == "challenge"
    # Someone else's passkey on the account's own challenge proves nothing.
    opts = await proof_options(session, account.id, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_proof(
            session, "localhost", ORIGIN, soft_authenticator.get(opts, others), account.id, NOW
        )
    assert refused.value.reason == "verification"


async def test_a_proof_and_a_login_do_not_stand_in_for_each_other(
    session, account, soft_authenticator
):
    cred = await registered(session, account, soft_authenticator)
    login = await login_options(session, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_proof(
            session, "localhost", ORIGIN, soft_authenticator.get(login, cred), account.id, NOW
        )
    assert refused.value.reason == "challenge"
    proof = await proof_options(session, account.id, "localhost", NOW)
    with pytest.raises(PasskeyRejected) as refused:
        await finish_login(session, "localhost", ORIGIN, soft_authenticator.get(proof, cred), NOW)
    assert refused.value.reason == "challenge"


@pytest.mark.parametrize(
    "lie",
    [
        {"origin": "https://evil.example.net"},
        {"rp_id": "evil.example.net"},
        {"tamper": True},
        {"user_verified": False},
    ],
)
async def test_a_proof_that_does_not_check_out_is_rejected(
    session, account, soft_authenticator, lie
):
    cred = await registered(session, account, soft_authenticator)
    with pytest.raises(PasskeyRejected) as refused:
        await prove_with(session, account.id, soft_authenticator, cred, **lie)
    assert refused.value.reason == "verification"
    await session.refresh(cred)
    assert (cred.sign_count, cred.last_used_at) == (0, None)


async def test_removing_a_passkey(session, account, add_person, add_account, soft_authenticator):
    other = await add_account(await add_person("Sam", "adult"), "sam")
    cred = await registered(session, account, soft_authenticator)
    assert not await remove_passkey(session, other.id, cred.id)
    assert await remove_passkey(session, account.id, cred.id)
    assert not await remove_passkey(session, account.id, cred.id)
    assert await stored_passkeys(session, account.id) == []


# --- the pages ----------------------------------------------------------------------------------


async def test_ip_access_hides_passkeys(client_by_ip):
    assert 'data-action="passkey-login"' not in (await client_by_ip.get("/login")).text


async def test_on_an_ip_address_the_login_page_says_why_and_keeps_the_password(client_by_ip):
    page = (await client_by_ip.get("/login")).text
    assert t("account.passkeys.ip_unavailable", "en") in plain(page)
    assert "/static/passkeys.js" not in page
    assert 'name="password"' in page


async def test_on_an_ip_address_the_account_page_says_why(client_by_ip, adult_account):
    await log_in(client_by_ip, ADULT_USERNAME, ADULT_PASSWORD)
    page = (await client_by_ip.get("/account")).text
    assert 'data-action="passkey-register"' not in page
    assert "/static/passkeys.js" not in page
    assert attribute(page, "passkeys-unavailable") == "ip"
    assert t("account.passkeys.ip_unavailable", "en") in plain(page)
    assert attribute(page, "passkeys") == "slot"


async def test_on_a_host_name_the_account_page_offers_passkeys(client_by_name, adult_account):
    """R44."""
    await log_in(client_by_name, ADULT_USERNAME, ADULT_PASSWORD)
    page = (await client_by_name.get("/account")).text
    assert 'data-action="passkey-register"' in page
    assert f'<script src="{static_url("passkeys.js")}" defer></script>' in page
    assert 'data-options-url="/account/passkeys/options"' in page
    assert "passkeys-unavailable" not in page
    assert t("account.passkeys.none", "en") in plain(page)


async def test_on_a_host_name_the_login_page_offers_passkeys(client_by_name):
    page = (await client_by_name.get("/login")).text
    assert 'data-action="passkey-login"' in page
    assert f'<script src="{static_url("passkeys.js")}" defer></script>' in page
    assert 'data-options-url="/login/passkey/options"' in page
    assert t("account.passkeys.ip_unavailable", "en") not in plain(page)


async def test_the_pages_speak_the_house_language_about_passkeys(
    client_by_ip, client_by_name, owner_account, session
):
    await house_reads(session, "fr")
    assert in_language("fr", "account.passkeys.ip_unavailable") in plain(
        (await client_by_ip.get("/login")).text
    )
    assert in_language("fr", "login.passkey.submit") in plain(
        (await client_by_name.get("/login")).text
    )


async def test_a_name_that_is_not_a_site_address_offers_no_passkey(client, adult_account):
    """``testserver`` is not one of the site addresses (``localhost`` by default)."""
    assert 'data-action="passkey-login"' not in (await client.get("/login")).text
    await log_in(client, ADULT_USERNAME, ADULT_PASSWORD)
    page = (await client.get("/account")).text
    assert 'data-action="passkey-register"' not in page
    assert attribute(page, "passkeys-unavailable") == "address"
    assert t("account.passkeys.unavailable", "en") in plain(page)


async def test_plain_http_offers_no_passkey(app, client_factory):
    url = "http://localhost:8443"
    async with client_factory(app, origin=url, base_url=url) as by_http:
        assert 'data-action="passkey-login"' not in (await by_http.get("/login")).text
        assert (await by_http.post("/login/passkey/options")).status_code == 404


async def test_the_site_addresses_of_the_owner_are_the_ones_that_offer_passkeys(
    app, settings, client_factory, client_by_name
):
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"site_addresses": "192.168.1.10, domo.local"}
    )
    url = "https://domo.local:8443"
    async with client_factory(app, origin=url, base_url=url) as by_name:
        assert 'data-action="passkey-login"' in (await by_name.get("/login")).text
        options = await by_name.post("/login/passkey/options")
        assert options.json()["rpId"] == "domo.local"
    assert 'data-action="passkey-login"' not in (await client_by_name.get("/login")).text


async def test_the_passkey_script_ships_and_is_served(offline_client):
    script = resources.files("domo_core").joinpath("web", "static", "passkeys.js")
    assert script.is_file()
    r = await offline_client.get("/static/passkeys.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    source = r.text
    assert "navigator.credentials" in source
    assert "eval(" not in source and "innerHTML" not in source
    # The forms are taken by delegation, so that a card htmx brings in later works too; a proof
    # is posted by its form's htmx request.
    assert 'document.addEventListener("submit"' in source and "htmx:load" in source
    assert "requestSubmit" in source and "[data-passkey-only]" in source


# --- adding and removing a passkey on the account page ------------------------------------------


async def add_through_the_page(client, soft_authenticator, label="phone", password=ADULT_PASSWORD):
    """What ``passkeys.js`` does: ask for the options (with the password), let the authenticator
    answer, and post the answer with the label."""
    r = await client.post("/account/passkeys/options", data={"current": password})
    assert r.status_code == 200, r.text
    payload = soft_authenticator.create(r.json())
    return await client.post(
        "/account/passkeys", data={"label": label, "credential": json.dumps(payload)}
    )


@pytest.fixture
async def adult_by_name(client_by_name, adult_account):
    """The adult, logged in on ``https://localhost:8443``."""
    await log_in(client_by_name, ADULT_USERNAME, ADULT_PASSWORD)
    return client_by_name


async def test_a_person_adds_a_passkey_from_the_account_page(
    adult_by_name, adult_account, soft_authenticator, session
):
    r = await add_through_the_page(adult_by_name, soft_authenticator, "<b>phone</b>")
    assert r.status_code == 303
    assert r.headers["location"] == "/account?saved=passkey"
    [cred] = await stored_passkeys(session, adult_account.id)
    assert cred.label == "<b>phone</b>"
    page = (await adult_by_name.get(r.headers["location"])).text
    assert "&lt;b&gt;phone&lt;/b&gt;" in page and "<b>phone" not in page
    assert t("account.saved.passkey", "en") in plain(page)
    assert t("account.passkeys.none", "en") not in plain(page)
    entry = (await audit_entries(session))[-1]
    assert entry.action == "auth.passkey_added"
    assert entry.actor == f"person:{adult_account.person_id}"
    # Which passkey, and nothing a person wrote nor anything of the key itself.
    assert entry.details == {"passkey_id": str(cred.id)}


async def test_adding_a_passkey_asks_for_the_password(adult_by_name, adult_account, session):
    r = await adult_by_name.post("/account/passkeys/options", data={"current": WRONG_PASSWORD})
    assert r.status_code == 400
    assert r.json() == {"error": t("account.password.error.current", "en")}
    assert await count(session, PasskeyChallenge) == 0
    entry = (await audit_entries(session))[-1]
    assert (entry.action, entry.details) == ("auth.passkey_add_failed", {})


async def test_wrong_passwords_for_a_passkey_are_held_back_like_logins(adult_by_name):
    for _ in range(5):
        r = await adult_by_name.post("/account/passkeys/options", data={"current": WRONG_PASSWORD})
        assert r.status_code == 400
    r = await adult_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert r.json() == {"error": t("account.password.error.throttled", "en", minutes="15")}


async def test_a_blocked_login_does_not_keep_a_person_from_adding_a_passkey(
    app, client_factory, adult_by_name
):
    """R89: the password asked for a passkey is counted per account, apart from the login's
    username, which anyone may hammer."""
    async with client_factory(app, origin=ORIGIN, base_url=ORIGIN) as stranger:
        for _ in range(5):
            await stranger.post(
                "/login", data={"username": ADULT_USERNAME, "password": WRONG_PASSWORD}
            )
        blocked = await stranger.post(
            "/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD}
        )
        assert blocked.status_code == 429
    r = await adult_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 200


async def test_the_passkey_and_the_account_page_count_wrong_passwords_together(adult_by_name):
    """Both ask a signed-in person for their password (R84, R89): one count per account, so going
    from one form to the other gives no more guesses."""
    wrong_change = {
        "current": WRONG_PASSWORD,
        "new": "another-long-one",
        "confirm": "another-long-one",
    }
    for _ in range(3):
        assert (await adult_by_name.post("/account/password", data=wrong_change)).status_code == 400
    for _ in range(2):
        r = await adult_by_name.post("/account/passkeys/options", data={"current": WRONG_PASSWORD})
        assert r.status_code == 400
    r = await adult_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 429
    right_change = wrong_change | {"current": ADULT_PASSWORD}
    assert (await adult_by_name.post("/account/password", data=right_change)).status_code == 429


async def test_the_passkey_errors_speak_the_language_of_their_reader(
    adult_by_name, adult_account, session
):
    await reads_in(session, adult_account.person_id, "fr")
    r = await adult_by_name.post("/account/passkeys/options", data={"current": WRONG_PASSWORD})
    assert r.json() == {"error": in_language("fr", "account.password.error.current")}


async def test_a_refused_passkey_shows_the_page_again(
    adult_by_name, adult_account, soft_authenticator, session
):
    r = await adult_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    payload = soft_authenticator.create(r.json(), origin="https://evil.example.net")
    r = await adult_by_name.post(
        "/account/passkeys", data={"label": "phone", "credential": json.dumps(payload)}
    )
    assert r.status_code == 400
    assert t("account.passkeys.error.failed", "en") in plain(r.text)
    assert await stored_passkeys(session, adult_account.id) == []
    assert "auth.passkey_added" not in [entry.action for entry in await audit_entries(session)]


async def test_a_bad_label_is_refused_and_nothing_is_stored(
    adult_by_name, adult_account, soft_authenticator, session
):
    r = await add_through_the_page(adult_by_name, soft_authenticator, "a\nb")
    assert r.status_code == 400
    message = t("account.passkeys.error.label", "en", max=str(MAX_PASSKEY_LABEL_LENGTH))
    assert message in plain(r.text)
    assert await stored_passkeys(session, adult_account.id) == []


@pytest.mark.parametrize(
    "credential",
    ["", "not json", "[1]", "{" * 10, "x" * 70_000],
    ids=["empty", "not json", "a list", "broken", "too long"],
)
async def test_the_answer_must_be_a_json_object(adult_by_name, adult_account, session, credential):
    r = await adult_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 200
    r = await adult_by_name.post(
        "/account/passkeys", data={"label": "phone", "credential": credential}
    )
    assert r.status_code == 400
    assert t("account.passkeys.error.failed", "en") in plain(r.text)
    assert await stored_passkeys(session, adult_account.id) == []


async def test_too_many_passkeys_are_refused_before_the_authenticator_is_asked(
    adult_by_name, adult_account, session
):
    for index in range(MAX_PASSKEYS_PER_ACCOUNT):
        session.add(
            PasskeyCredential(
                account_id=adult_account.id,
                credential_id=bytes([index]) * 16,
                public_key=b"key",
                sign_count=0,
                transports=[],
                label=f"key {index}",
            )
        )
    await session.flush()
    r = await adult_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 400
    assert r.json() == {
        "error": t("account.passkeys.error.too_many", "en", max=str(MAX_PASSKEYS_PER_ACCOUNT))
    }


async def test_a_person_removes_their_passkey(
    adult_by_name, adult_account, soft_authenticator, session
):
    await add_through_the_page(adult_by_name, soft_authenticator)
    [cred] = await stored_passkeys(session, adult_account.id)
    page = (await adult_by_name.get("/account")).text
    assert f'action="/account/passkeys/{cred.id}/delete"' in page
    r = await adult_by_name.post(f"/account/passkeys/{cred.id}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/account?saved=passkey_removed"
    assert await stored_passkeys(session, adult_account.id) == []
    entry = (await audit_entries(session))[-1]
    assert (entry.action, entry.details) == ("auth.passkey_removed", {"passkey_id": str(cred.id)})
    assert t("account.saved.passkey_removed", "en") in plain(
        (await adult_by_name.get(r.headers["location"])).text
    )


async def test_a_passkey_can_be_removed_from_an_ip_address(
    client_by_ip, adult_account, soft_authenticator, session
):
    cred = await registered(session, adult_account, soft_authenticator)
    await log_in(client_by_ip, ADULT_USERNAME, ADULT_PASSWORD)
    page = (await client_by_ip.get("/account")).text
    assert "laptop" in plain(page)
    r = await client_by_ip.post(f"/account/passkeys/{cred.id}/delete")
    assert r.status_code == 303
    assert await stored_passkeys(session, adult_account.id) == []


@pytest.mark.parametrize("which", ["someone else's", "unknown", "not an id"])
async def test_nobody_removes_a_passkey_that_is_not_theirs(
    adult_by_name, owner_account, soft_authenticator, session, which
):
    cred = await registered(session, owner_account, soft_authenticator)
    target = {"someone else's": str(cred.id), "unknown": str(uuid.uuid4()), "not an id": "x"}
    r = await adult_by_name.post(f"/account/passkeys/{target[which]}/delete")
    assert r.status_code == 404
    assert await stored_passkeys(session, owner_account.id) == [cred]


async def test_the_account_passkey_routes_need_a_session(client_by_name):
    for path in (
        "/account/passkeys/options",
        "/account/passkeys",
        f"/account/passkeys/{uuid.uuid4()}/delete",
    ):
        r = await client_by_name.post(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login"


async def test_the_passkey_options_are_refused_on_an_ip_address(client_by_ip, adult_account):
    assert (await client_by_ip.post("/login/passkey/options")).status_code == 404
    await log_in(client_by_ip, ADULT_USERNAME, ADULT_PASSWORD)
    r = await client_by_ip.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
    assert r.status_code == 404


async def test_a_cross_site_page_cannot_ask_for_options(app, client_factory):
    url = "https://localhost:8443"
    async with client_factory(app, origin="https://evil.example.net", base_url=url) as other:
        assert (await other.post("/login/passkey/options")).status_code == 403


# --- signing in with a passkey ----------------------------------------------------------------


async def sign_in(client, soft_authenticator, cred, **get_options):
    """What ``passkeys.js`` does on the login page."""
    r = await client.post("/login/passkey/options")
    assert r.status_code == 200, r.text
    payload = soft_authenticator.get(r.json(), cred, **get_options)
    return await client.post("/login/passkey", data={"credential": json.dumps(payload)})


async def count_sessions(session) -> int:
    return await count(session, WebSession)


async def test_a_person_signs_in_with_a_passkey(
    client_by_name, adult_account, soft_authenticator, session
):
    cred = await registered(session, adult_account, soft_authenticator)
    r = await sign_in(client_by_name, soft_authenticator, cred)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat"
    assert cookie_attributes(r)["samesite"] == "strict"
    assert {"secure", "httponly"} <= set(cookie_attributes(r))
    whoami = await client_by_name.get("/whoami")
    assert whoami.json()["id"] == str(adult_account.person_id)
    entry = (await audit_entries(session))[-1]
    assert (entry.actor, entry.action, entry.details) == (
        f"person:{adult_account.person_id}",
        "auth.login",
        {"method": "passkey"},
    )
    await session.refresh(cred)
    assert cred.sign_count == 1 and cred.last_used_at is not None


async def test_a_passkey_login_replaces_the_session_the_browser_had(
    client_by_name, adult_account, soft_authenticator, session
):
    cred = await registered(session, adult_account, soft_authenticator)
    await log_in(client_by_name, ADULT_USERNAME, ADULT_PASSWORD)
    await sign_in(client_by_name, soft_authenticator, cred)
    assert await count_sessions(session) == 1


async def test_a_failed_passkey_login_gets_the_answer_of_a_wrong_password(
    client_by_name, adult_account, soft_authenticator, session
):
    cred = await registered(session, adult_account, soft_authenticator)
    wrong_password = await client_by_name.post(
        "/login", data={"username": "", "password": WRONG_PASSWORD}
    )
    r = await sign_in(client_by_name, soft_authenticator, cred, tamper=True)
    assert r.status_code == wrong_password.status_code == 400
    assert "set-cookie" not in r.headers
    assert t("login.error.invalid", "en") in plain(r.text)
    assert r.text == wrong_password.text
    assert await count_sessions(session) == 0
    entry = (await audit_entries(session))[-1]
    assert (entry.action, entry.details) == ("auth.login_failed", {"method": "passkey"})


@pytest.mark.parametrize(
    "credential",
    ["", "not json", "[]", "x" * 70_000],
    ids=["empty", "not json", "a list", "too long"],
)
async def test_a_login_answer_that_is_not_a_json_object_is_a_failed_login(
    client_by_name, session, credential
):
    await client_by_name.post("/login/passkey/options")
    r = await client_by_name.post("/login/passkey", data={"credential": credential})
    assert r.status_code == 400
    assert t("login.error.invalid", "en") in plain(r.text)
    assert await count_sessions(session) == 0


@pytest.mark.parametrize("left", ["removed", "stay_over"])
async def test_someone_who_left_cannot_sign_in_with_their_passkey(
    client_by_name, adult_account, soft_authenticator, session, left
):
    """R77, through the page: the answer of a failed login, and no session."""
    cred = await registered(session, adult_account, soft_authenticator)
    person = await session.get(Person, adult_account.person_id)
    assert person is not None
    before = datetime.now(UTC) - timedelta(minutes=1)
    if left == "removed":
        person.disabled_at = before
        person.state = "gone"
    else:
        person.role, person.expires_at = "guest", before
    await session.flush()
    r = await sign_in(client_by_name, soft_authenticator, cred)
    assert r.status_code == 400
    assert "set-cookie" not in r.headers
    assert t("login.error.invalid", "en") in plain(r.text)
    assert await count_sessions(session) == 0


async def test_failed_passkey_logins_are_held_back_like_wrong_passwords(
    client_by_name, adult_account, soft_authenticator, session
):
    cred = await registered(session, adult_account, soft_authenticator)
    for _ in range(5):
        assert (
            await sign_in(client_by_name, soft_authenticator, cred, tamper=True)
        ).status_code == 400
    r = await sign_in(client_by_name, soft_authenticator, cred)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert t("login.error.throttled", "en", minutes="15") in plain(r.text)
    assert await count_sessions(session) == 0


async def test_a_held_back_passkey_does_not_lock_the_password_out(
    client_by_name, adult_account, soft_authenticator, session
):
    """A passkey's attempts count against that passkey, not against the username: someone who
    hammers it does not lock its owner out of their password."""
    cred = await registered(session, adult_account, soft_authenticator)
    for _ in range(5):
        await sign_in(client_by_name, soft_authenticator, cred, tamper=True)
    r = await client_by_name.post(
        "/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD}
    )
    assert r.status_code == 303


async def test_a_held_back_username_does_not_lock_the_passkey_out(
    client_by_name, adult_account, soft_authenticator, session
):
    """Nor the other way round: someone who hammers the username at the password login does not
    stop its owner from signing in with their passkey."""
    cred = await registered(session, adult_account, soft_authenticator)
    for _ in range(5):
        await client_by_name.post(
            "/login", data={"username": ADULT_USERNAME, "password": WRONG_PASSWORD}
        )
    assert (
        await client_by_name.post(
            "/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD}
        )
    ).status_code == 429
    assert (await sign_in(client_by_name, soft_authenticator, cred)).status_code == 303


async def test_too_many_sign_ins_at_once_are_refused(client_by_name, monkeypatch):
    monkeypatch.setattr(passkeys, "MAX_PENDING_LOGIN_CHALLENGES", 1)
    assert (await client_by_name.post("/login/passkey/options")).status_code == 200
    r = await client_by_name.post("/login/passkey/options")
    assert r.status_code == 429
    assert r.json() == {"error": t("login.passkey.busy", "en")}


# --- the port of the page, the throttles, two answers at once (fix round 1) --------------------


async def test_a_request_that_names_another_port_gets_no_passkey(
    app, client_factory, client_by_name, adult_account, soft_authenticator, session
):
    """The origin the core expects is built from the configured port (``DOMO_HTTPS_PORT``),
    never from the port a request names: an assertion made on another port of the same name
    (another service there) is refused, even when its ``Host`` and ``Origin`` agree with it."""
    cred = await registered(session, adult_account, soft_authenticator)
    other_port = "https://localhost:9443"
    async with client_factory(app, origin=other_port, base_url=other_port) as elsewhere:
        assert 'data-action="passkey-login"' not in (await elsewhere.get("/login")).text
        assert (await elsewhere.post("/login/passkey/options")).status_code == 404
        # A challenge taken where passkeys work, answered for the other port, posted there.
        options = (await client_by_name.post("/login/passkey/options")).json()
        payload = soft_authenticator.get(options, cred, origin=other_port)
        r = await elsewhere.post("/login/passkey", data={"credential": json.dumps(payload)})
    assert r.status_code == 400
    assert "set-cookie" not in r.headers
    assert t("login.error.invalid", "en") in plain(r.text)
    assert await count_sessions(session) == 0


async def test_the_origin_follows_the_configured_port(
    app, settings, client_factory, adult_account, soft_authenticator, session
):
    """A page served on 443 has an origin without a port."""
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"https_port": 443})
    cred = await registered(session, adult_account, soft_authenticator)
    url = "https://localhost"
    async with client_factory(app, origin=url, base_url=url) as by_name:
        options = (await by_name.post("/login/passkey/options")).json()
        payload = soft_authenticator.get(options, cred, origin=url)
        r = await by_name.post("/login/passkey", data={"credential": json.dumps(payload)})
    assert r.status_code == 303


async def test_passkey_attempts_cannot_flush_the_password_throttle(
    app, client_by_name, adult_account, clock
):
    """A passkey attempt costs no password verification: if its keys shared the login's bounded
    throttle, a burst of them with made-up credential ids would push a blocked username out of it.
    They are counted by a throttle of their own."""
    app.state.login_throttle = LoginThrottle(clock=clock, max_tracked=3)
    for _ in range(5):
        await client_by_name.post(
            "/login", data={"username": ADULT_USERNAME, "password": WRONG_PASSWORD}
        )
    for index in range(10):
        made_up = json.dumps({"rawId": f"made-up-{index}"})
        r = await client_by_name.post("/login/passkey", data={"credential": made_up})
        assert r.status_code == 400
    r = await client_by_name.post(
        "/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD}
    )
    assert r.status_code == 429


def test_one_address_asks_for_fewer_options_than_the_household_may_have_waiting():
    assert MAX_LOGIN_OPTIONS_PER_ADDRESS < passkeys.MAX_PENDING_LOGIN_CHALLENGES


async def test_one_address_cannot_keep_everyone_at_busy(app, client_factory, clock, monkeypatch):
    """Login options are counted per client address (the real one, behind the trusted edge):
    one address that keeps asking is held back long before the pool of the household is full."""
    monkeypatch.setattr(passkeys, "MAX_PENDING_LOGIN_CHALLENGES", 5)
    app.state.passkey_options_throttle = LoginThrottle(clock=clock, max_attempts=3)
    url = "https://localhost:8443"
    flooder = client_factory(app, origin=url, base_url=url, address="192.0.2.7")
    neighbour = client_factory(app, origin=url, base_url=url, address="192.0.2.8")
    async with flooder, neighbour:
        statuses = [(await flooder.post("/login/passkey/options")).status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        refused = await flooder.post("/login/passkey/options")
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) > 0
        assert refused.json() == {"error": t("login.passkey.busy", "en")}
        assert (await neighbour.post("/login/passkey/options")).status_code == 200


async def test_an_address_is_held_back_no_longer_than_a_challenge_lives(app, client_factory, clock):
    """R91: the per-address window is the life of a challenge (five minutes), so that where the
    household shares one source address (Docker Desktop), the hold lasts no longer than the
    challenges it protects the pool from."""
    assert timedelta(minutes=5) == passkeys.CHALLENGE_LIFETIME
    url = "https://localhost:8443"
    async with client_factory(app, origin=url, base_url=url, address="192.0.2.7") as flooder:
        for _ in range(MAX_LOGIN_OPTIONS_PER_ADDRESS):
            assert (await flooder.post("/login/passkey/options")).status_code == 200
        refused = await flooder.post("/login/passkey/options")
        assert refused.status_code == 429
        assert 0 < int(refused.headers["retry-after"]) <= 300
        clock.advance(passkeys.CHALLENGE_LIFETIME + timedelta(seconds=1))
        assert (await flooder.post("/login/passkey/options")).status_code == 200


async def test_signing_in_frees_the_address(
    app, client_by_name, adult_account, soft_authenticator, session, clock
):
    """Someone who cancelled a few times and then signed in starts again from zero."""
    app.state.passkey_options_throttle = LoginThrottle(clock=clock, max_attempts=2)
    cred = await registered(session, adult_account, soft_authenticator)
    assert (await client_by_name.post("/login/passkey/options")).status_code == 200
    assert (await sign_in(client_by_name, soft_authenticator, cred)).status_code == 303
    assert (await client_by_name.post("/login/passkey/options")).status_code == 200


async def test_the_pending_cap_is_logged_when_it_is_reached(session, monkeypatch, caplog):
    monkeypatch.setattr(passkeys, "MAX_PENDING_LOGIN_CHALLENGES", 1)
    await login_options(session, "localhost", NOW)
    with (
        caplog.at_level(logging.WARNING, logger="domo_core.accounts.passkeys"),
        pytest.raises(PasskeyRejected) as refused,
    ):
        await login_options(session, "localhost", NOW)
    assert refused.value.reason == "busy"
    assert "login challenges" in caplog.text


async def test_the_same_answer_posted_twice_at_once_opens_one_session(
    app_factory, client_factory, session_factory, soft_authenticator
):
    """The challenge is spent by one conditional update: the same answer, posted twice at the
    same moment (sessions per request, committed for real), signs in once."""
    async with session_factory() as writer:
        person = Person(display_name="Alex", role="adult")
        writer.add(person)
        await writer.flush()
        account = Account(person_id=person.id, username="alex", password_hash="unused")
        writer.add(account)
        await writer.flush()
        cred = await registered(writer, account, soft_authenticator)
        await writer.commit()
    live = app_factory()
    url = "https://localhost:8443"
    async with (
        client_factory(live, origin=url, base_url=url) as first,
        client_factory(live, origin=url, base_url=url) as second,
    ):
        options = (await first.post("/login/passkey/options")).json()
        form = {"credential": json.dumps(soft_authenticator.get(options, cred))}
        answers = await asyncio.gather(
            first.post("/login/passkey", data=form), second.post("/login/passkey", data=form)
        )
    assert sorted(answer.status_code for answer in answers) == [303, 400]
    async with session_factory() as reader:
        assert await count(reader, WebSession) == 1
