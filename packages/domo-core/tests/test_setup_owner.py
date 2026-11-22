"""First launch: the one-time setup code and the creation of the owner."""

import asyncio
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from types import SimpleNamespace

import httpx
import pytest
from domo_common.i18n import t
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.passwords import MIN_PASSWORD_LENGTH, verify_password
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.audit.log import ChainCheck, verify_audit_chain
from domo_core.proofs.service import means_of
from domo_core.setup_flow import code as setup_code_module
from domo_core.setup_flow import owner_routes
from domo_core.setup_flow.code import (
    SETUP_CODE_ALPHABET,
    generate_setup_code,
    normalize_setup_code,
    rotate_setup_code,
)
from domo_core.setup_flow.code import verify_setup_code as code_is_valid
from domo_core.setup_flow.models import SetupState
from domo_core.setup_flow.owner_routes import SETUP_CODE_MAX_ATTEMPTS
from domo_core.setup_flow.state import setup_stage
from sqlalchemy import select, text
from web_support import (
    OWNER_PASSWORD,
    audit_entries,
    build_test_app,
    cookie_attributes,
    count_owners,
    count_rows,
)

CODE_FORMAT = r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}"
WRONG_CODE = "AAAA-AAAA-AAAA"


@pytest.fixture
def app(settings, session, clock):
    """The application on the shared rolled-back session, with the first launch not over."""
    return build_test_app(settings, session, clock)


def owner_form(
    *,
    code: str,
    display_name: str = "Test Owner",
    username: str = "owner",
    password: str = OWNER_PASSWORD,
) -> dict[str, str]:
    return {
        "code": code,
        "display_name": display_name,
        "username": username,
        "password": password,
    }


def visible(response: httpx.Response) -> str:
    """The text of a page as a person reads it (HTML entities decoded)."""
    return unescape(response.text)


async def setup_state_row(session) -> SetupState:
    return (await session.execute(select(SetupState))).scalar_one()


def lenient_client(app) -> httpx.AsyncClient:
    """A client that gets a 500 response instead of the exception the application re-raises."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport, base_url="https://testserver", headers={"origin": "https://testserver"}
    )


# --- the code --------------------------------------------------------------------------------


def test_setup_code_format():
    assert re.fullmatch(CODE_FORMAT, generate_setup_code())


def test_the_alphabet_leaves_out_the_characters_that_look_alike():
    assert SETUP_CODE_ALPHABET == "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    assert not set("ILO01") & set(SETUP_CODE_ALPHABET)


def test_codes_use_only_the_alphabet_and_do_not_repeat():
    codes = {generate_setup_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert set(code.replace("-", "")) <= set(SETUP_CODE_ALPHABET)


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("ABCD-EFGH-JKMN", "ABCD-EFGH-JKMN"),
        ("  abcd-efgh-jkmn \n", "ABCD-EFGH-JKMN"),  # a phone keyboard, a pasted line
        ("abcdefghjkmn", "ABCD-EFGH-JKMN"),  # no dashes
        ("ABCD EFGH JKMN", "ABCD-EFGH-JKMN"),  # spaces instead
        ("ABCD-EFGH", "ABCDEFGH"),  # too short stays too short (and so never matches)
        ("", ""),
    ],
)
def test_a_typed_code_is_normalised(typed, canonical):
    assert normalize_setup_code(typed) == canonical


async def test_rotation_invalidates_previous_code(session):
    first = await rotate_setup_code(session)
    second = await rotate_setup_code(session)
    assert first != second and not await code_is_valid(session, first)
    assert await code_is_valid(session, second)


async def test_a_rotated_code_has_the_documented_format(session):
    code = await rotate_setup_code(session)
    assert code is not None and re.fullmatch(CODE_FORMAT, code)


async def test_only_the_hash_of_the_code_is_stored(session):
    code = await rotate_setup_code(session)
    row = await setup_state_row(session)
    assert row.setup_code_hash == hashlib.sha256(code.encode()).hexdigest()
    assert row.code_created_at is not None
    assert code not in repr([row.setup_code_hash, row.code_created_at])


async def test_there_is_no_code_once_an_owner_exists(session, add_person):
    await add_person("Owner", "owner")
    assert await rotate_setup_code(session) is None
    assert (await setup_state_row(session)).setup_code_hash is None


async def test_there_is_no_code_once_the_owner_is_recorded_as_created(session):
    await session.execute(text("UPDATE setup_state SET owner_created_at = now()"))
    assert await rotate_setup_code(session) is None


async def test_a_member_is_not_an_owner_for_the_code(session, add_person):
    await add_person("Member", "adult")
    assert await rotate_setup_code(session) is not None


async def test_rotation_refuses_to_run_without_the_state_row(session):
    await session.execute(text("DELETE FROM setup_state"))
    with pytest.raises(RuntimeError, match="setup_state"):
        await rotate_setup_code(session)


async def test_rotation_leaves_the_transaction_to_the_caller(session_factory):
    async with session_factory() as writer, session_factory() as reader:
        code = await rotate_setup_code(writer)
        assert code is not None and not await code_is_valid(reader, code)
        await writer.commit()
        assert await code_is_valid(reader, code)


@pytest.mark.parametrize("typed", ["", WRONG_CODE, "ABCD-EFGH-JKMN-PQRS", "x" * 5000, "ab\x00cd"])
async def test_a_wrong_code_is_not_valid(session, typed):
    await rotate_setup_code(session)
    assert not await code_is_valid(session, typed)


async def test_a_code_is_valid_however_it_was_typed(session):
    code = await rotate_setup_code(session)
    assert await code_is_valid(session, code.lower())
    assert await code_is_valid(session, f" {code.replace('-', '')} ")


async def test_no_code_is_valid_when_none_exists(session):
    assert not await code_is_valid(session, "")
    assert not await code_is_valid(session, WRONG_CODE)


async def test_the_comparison_is_constant_time_even_when_there_is_no_code(session, monkeypatch):
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    # Only this module's view of ``hmac`` is replaced: nothing else is observed.
    monkeypatch.setattr(setup_code_module, "hmac", SimpleNamespace(compare_digest=spy))
    assert not await code_is_valid(session, WRONG_CODE)  # no code stored: still compared
    await rotate_setup_code(session)
    assert not await code_is_valid(session, WRONG_CODE)
    assert len(calls) == 2
    assert all(len(a) == len(b) == 64 for a, b in calls)  # two SHA-256 hex digests, never a code


# --- the page --------------------------------------------------------------------------------


async def test_redirects_to_setup_until_done(client):
    r = await client.get("/chat")
    assert r.status_code == 303 and r.headers["location"] == "/setup/owner"


async def test_the_owner_page_is_a_form_with_the_expected_fields(client):
    r = await client.get("/setup/owner")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"
    html = r.text
    assert re.search(r'<form[^>]*method="post"[^>]*action="/setup/owner"', html)
    for name in ("code", "display_name", "username", "password"):
        assert re.search(rf'<input[^>]*name="{name}"', html), name
    assert re.search(r'<input[^>]*name="password"[^>]*type="password"', html)
    assert 'autocomplete="new-password"' in html
    assert 'autocomplete="username"' in html
    assert html.count('type="submit"') == 1  # the end-to-end test clicks "button[type=submit]"
    assert 'role="alert"' not in html


async def test_the_owner_page_explains_where_the_code_is(client):
    r = await client.get("/setup/owner")
    assert "docker compose logs core" in visible(r)


@pytest.mark.parametrize(
    ("language", "section"),
    [("en", "The browser warning"), ("fr", "L'avertissement du navigateur")],
)
async def test_the_owner_page_explains_the_certificate_warning(client, language, section):
    """Spec 7.1: the welcome screen says the browser's warning is expected, links to the
    authority's certificate (served by the edge) and points to the README's section on
    installing it on the household's devices."""
    r = await client.get("/setup/owner", headers={"accept-language": language})
    explanation = (
        t("setup.owner.certificate.before", language)
        + t("setup.owner.certificate.link", language)
        + t("setup.owner.certificate.after", language)
    )
    # One sentence, read as such once the markup of its link is left out.
    assert unescape(re.sub(r"<[^>]+>", "", r.text)).count(explanation) == 1
    assert "README" in explanation and section in explanation
    link = re.search(r'<a href="/ca\.crt">([^<]*)</a>', r.text)
    assert link is not None
    assert unescape(link.group(1)) == t("setup.owner.certificate.link", language)


async def test_the_certificate_explanation_adds_no_inline_script_or_style(client):
    r = await client.get("/setup/owner")
    paragraph = re.search(r"<p[^>]*>[^<]*<a href=\"/ca\.crt\">.*?</p>", r.text, re.DOTALL)
    assert paragraph is not None
    assert "style=" not in paragraph.group(0) and "<script" not in paragraph.group(0)
    assert re.search(r"\son[a-z]+=", paragraph.group(0)) is None


async def test_the_owner_page_speaks_french_when_asked(client):
    r = await client.get("/setup/owner", headers={"accept-language": "fr"})
    assert t("setup.owner.submit", "fr") in visible(r)
    assert t("setup.owner.code", "fr") in visible(r)
    assert t("setup.owner.submit", "en") not in visible(r)


# --- creating the owner ----------------------------------------------------------------------


async def test_creating_the_owner_creates_the_person_the_account_and_the_session(
    client, setup_code, session
):
    r = await client.post(
        "/setup/owner",
        data=owner_form(code=setup_code, display_name="  Ada  ", username="  Owner  "),
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/model"

    person = (await session.execute(select(Person))).scalar_one()
    assert (person.role, person.display_name) == ("owner", "Ada")
    account = (await session.execute(select(Account))).scalar_one()
    assert account.person_id == person.id
    assert account.username == "owner"  # normalised, as the login looks it up
    assert OWNER_PASSWORD not in account.password_hash
    assert verify_password(account.password_hash, OWNER_PASSWORD)
    state = await setup_state_row(session)
    assert state.setup_code_hash is None
    assert state.owner_created_at is not None
    assert state.model_configured_at is None
    assert await setup_stage(session) == "model"


async def test_the_new_owner_has_the_password_as_a_means_of_proof(client, setup_code, session):
    r = await client.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 303
    person = (await session.execute(select(Person))).scalar_one()
    [mean] = await means_of(session, person, datetime.now(UTC))
    assert (mean.kind, mean.proposed_by) == ("password", "code")


async def test_the_new_owner_is_logged_in_with_a_hardened_cookie(
    client, second_client, setup_code, session, settings
):
    r = await client.post("/setup/owner", data=owner_form(code=setup_code))
    attributes = cookie_attributes(r)
    assert {"secure", "httponly", "samesite", "path", "max-age"} <= attributes.keys()
    assert attributes["samesite"].lower() == "strict"
    assert attributes["path"] == "/"
    assert "domain" not in attributes
    assert int(attributes["max-age"]) == settings.session_ttl_days * 24 * 3600

    row = (await session.execute(select(WebSession))).scalar_one()
    token = attributes[SESSION_COOKIE]
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash

    # The next step needs the owner: the cookie works, another browser is sent to the login.
    assert (await client.get("/setup/model")).status_code == 200
    other = await second_client.get("/setup/model")
    assert other.status_code == 303 and other.headers["location"] == "/login"


async def test_the_owner_is_recorded_in_the_audit_log_and_the_chain_verifies(
    client, setup_code, session
):
    await client.post("/setup/owner", data=owner_form(code=setup_code))
    person = (await session.execute(select(Person))).scalar_one()
    entries = await audit_entries(session)
    assert [(e.actor, e.action, e.details) for e in entries] == [
        (f"person:{person.id}", "setup.owner_created", {"username": "owner"})
    ]
    assert await verify_audit_chain(session) == ChainCheck(ok=True, count=1, first_bad_id=None)


async def test_the_setup_code_never_reaches_the_audit_log_or_a_response(
    client, setup_code, session
):
    page = await client.get("/setup/owner")
    wrong = await client.post("/setup/owner", data=owner_form(code="ZZZZ-ZZZZ-ZZZZ"))
    short = await client.post("/setup/owner", data=owner_form(code=setup_code, password="short"))
    created = await client.post("/setup/owner", data=owner_form(code=setup_code))
    assert (wrong.status_code, short.status_code, created.status_code) == (400, 400, 303)
    for response in (page, wrong, short, created):
        assert setup_code not in response.text
        assert setup_code not in repr(dict(response.headers))
    assert "ZZZZ-ZZZZ-ZZZZ" not in wrong.text  # a typed code is not echoed back either
    assert setup_code not in repr(await audit_entries(session))


async def test_the_form_keeps_what_was_typed_except_the_secrets(client, setup_code):
    r = await client.post(
        "/setup/owner",
        data=owner_form(code=setup_code, display_name="Ada", username="ada", password="short-p"),
    )
    assert r.status_code == 400
    assert re.search(r'name="display_name"[^>]*value="Ada"', r.text)
    assert re.search(r'name="username"[^>]*value="ada"', r.text)
    assert "short-p" not in r.text
    assert setup_code not in r.text


async def test_wrong_code_refused(client, session):
    r = await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    assert r.status_code == 400 and await count_owners(session) == 0


async def test_a_wrong_code_creates_nothing_and_says_so(client, setup_code, session):
    r = await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    assert t("setup.owner.error.code", "en") in visible(r)
    assert 'role="alert"' in r.text
    assert "set-cookie" not in r.headers
    assert await count_rows(session, Person) == 0
    assert await count_rows(session, Account) == 0
    assert await count_rows(session, WebSession) == 0
    assert await audit_entries(session) == []
    assert await code_is_valid(session, setup_code)  # the real code is still good


async def test_the_wrong_code_error_is_translated(client, setup_code):
    r = await client.post(
        "/setup/owner", data=owner_form(code=WRONG_CODE), headers={"accept-language": "fr"}
    )
    assert r.status_code == 400
    assert t("setup.owner.error.code", "fr") in visible(r)


async def test_a_code_typed_in_lower_case_without_dashes_works(client, setup_code, session):
    typed = setup_code.lower().replace("-", "")
    r = await client.post("/setup/owner", data=owner_form(code=typed))
    assert r.status_code == 303 and await count_owners(session) == 1


async def test_without_any_code_in_the_database_every_code_is_refused(client, session):
    # An instance that was never started by __main__ has no code: nothing can claim it.
    r = await client.post("/setup/owner", data=owner_form(code=""))
    assert r.status_code == 400
    r = await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    assert r.status_code == 400 and await count_owners(session) == 0


async def test_a_missing_field_is_refused_like_an_empty_one(client, setup_code, session):
    r = await client.post("/setup/owner", data={"code": setup_code})
    assert r.status_code == 400 and await count_owners(session) == 0


async def test_code_is_single_use(client, second_client, setup_code, session):
    assert (await client.post("/setup/owner", data=owner_form(code=setup_code))).status_code == 303
    r = await second_client.post("/setup/owner", data=owner_form(code=setup_code, username="other"))
    assert r.status_code in (303, 409) and await count_owners(session) == 1


async def test_once_the_owner_exists_the_setup_page_is_closed(client, setup_code, session):
    await client.post("/setup/owner", data=owner_form(code=setup_code))
    # Still the model step: the owner page goes there, whatever the method.
    for method in ("GET", "POST"):
        r = await client.request(method, "/setup/owner", data=owner_form(code=setup_code))
        assert r.status_code == 303 and r.headers["location"] == "/setup/model"
    assert await count_owners(session) == 1


async def test_concurrent_owner_creation_creates_one_owner(
    app_factory, client_factory, setup_code, session, monkeypatch
):  # Review Focus 1
    # Both requests get past the gate and the code check, then wait for each other before they
    # write: the unique index on the owner role, not luck, has to pick the winner.
    barrier = asyncio.Barrier(2)
    real_hash = owner_routes.hash_password_limited

    async def rendezvous(password: str) -> str:
        async with asyncio.timeout(30):
            await barrier.wait()
        return await real_hash(password)

    monkeypatch.setattr(owner_routes, "hash_password_limited", rendezvous)
    async with client_factory(app_factory()) as first, client_factory(app_factory()) as second:
        results = await asyncio.gather(
            *(
                c.post("/setup/owner", data=owner_form(code=setup_code, username=f"u{i}"))
                for i, c in enumerate((first, second))
            )
        )
    assert sorted(r.status_code for r in results).count(303) == 1
    assert await count_owners(session) == 1
    assert sorted(r.status_code for r in results) == [303, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert t("error.setup_done.title", "en") in visible(loser)
    assert "set-cookie" not in loser.headers
    assert await count_rows(session, Account) == 1
    assert await count_rows(session, WebSession) == 1
    assert [e.action for e in await audit_entries(session)] == ["setup.owner_created"]


# --- one transaction -------------------------------------------------------------------------


async def test_a_failing_audit_entry_leaves_no_owner_and_keeps_the_code(
    app_factory, setup_code, session_factory, monkeypatch
):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("the audit log is unavailable")

    monkeypatch.setattr(owner_routes, "append_audit", unavailable)
    async with lenient_client(app_factory()) as http:
        r = await http.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 500
    assert "set-cookie" not in r.headers
    async with session_factory() as other:
        assert await count_rows(other, Person) == 0
        assert await count_rows(other, Account) == 0
        assert await count_rows(other, WebSession) == 0
        assert (await setup_state_row(other)).owner_created_at is None
        assert await code_is_valid(other, setup_code)  # nothing was used up


async def test_the_audit_entry_is_appended_last_just_before_the_commit(
    app_factory, client_factory, setup_code, session_factory, monkeypatch
):
    # The audit log serialises its writers with a lock held until the end of the transaction:
    # appending last keeps that lock for as short a time as possible.
    real = owner_routes.append_audit
    seen: dict[str, object] = {}

    async def spying(db_session, **kwargs):
        seen["persons"] = await count_rows(db_session, Person)
        seen["accounts"] = await count_rows(db_session, Account)
        seen["sessions"] = await count_rows(db_session, WebSession)
        state = await setup_state_row(db_session)
        seen["code_cleared"] = state.setup_code_hash is None
        seen["owner_recorded"] = state.owner_created_at is not None
        async with session_factory() as other:
            seen["committed_elsewhere"] = await count_rows(other, Person)
        return await real(db_session, **kwargs)

    monkeypatch.setattr(owner_routes, "append_audit", spying)
    async with client_factory(app_factory()) as http:
        r = await http.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 303
    assert seen == {
        "persons": 1,
        "accounts": 1,
        "sessions": 1,
        "code_cleared": True,
        "owner_recorded": True,
        "committed_elsewhere": 0,  # not committed yet when the entry is written
    }
    async with session_factory() as other:
        assert await count_owners(other) == 1
        assert [e.action for e in await audit_entries(other)] == ["setup.owner_created"]


async def test_the_owner_creation_is_committed_for_real(
    app_factory, client_factory, setup_code, session_factory
):
    async with client_factory(app_factory()) as http:
        r = await http.post("/setup/owner", data=owner_form(code=setup_code))
        assert r.status_code == 303
        assert (await http.get("/setup/model")).status_code == 200
    async with session_factory() as other:  # another connection: only what was committed
        assert await count_owners(other) == 1
        assert await count_rows(other, Account) == 1
        assert await count_rows(other, WebSession) == 1
        assert not await code_is_valid(other, setup_code)
        assert await setup_stage(other) == "model"


async def test_a_username_that_cannot_be_used_is_refused_and_the_code_survives(
    client, setup_code, session, add_person, add_account
):
    taken = await add_person("Member", "adult")
    await add_account(taken, "taken")
    await session.commit()  # a rollback in the request must not take the fixture rows with it

    r = await client.post("/setup/owner", data=owner_form(code=setup_code, username="Taken"))
    assert r.status_code == 400
    assert t("setup.owner.error.username", "en", max="64") in visible(r)
    assert "set-cookie" not in r.headers
    assert await count_owners(session) == 0
    assert await count_rows(session, WebSession) == 0
    assert await audit_entries(session) == []
    assert await code_is_valid(session, setup_code)  # the failed attempt used nothing up
    r = await client.post("/setup/owner", data=owner_form(code=setup_code, username="free"))
    assert r.status_code == 303


# --- what is asked of the owner --------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "error_key", "params"),
    [
        ("display_name", "   ", "setup.owner.error.display_name", {"max": "60"}),
        ("display_name", "x" * 61, "setup.owner.error.display_name", {"max": "60"}),
        ("display_name", "bad\x00name", "setup.owner.error.display_name", {"max": "60"}),
        (
            "display_name",
            "Ada\N{RIGHT-TO-LEFT OVERRIDE}B",
            "setup.owner.error.display_name",
            {"max": "60"},
        ),
        (
            "display_name",
            "two\N{LINE SEPARATOR}lines",
            "setup.owner.error.display_name",
            {"max": "60"},
        ),
        ("username", "   ", "setup.owner.error.username", {"max": "64"}),
        ("username", "u" * 65, "setup.owner.error.username", {"max": "64"}),
        ("username", "bad\x00name", "setup.owner.error.username", {"max": "64"}),
        ("username", "two\nlines", "setup.owner.error.username", {"max": "64"}),
        ("password", "short", "setup.owner.error.password", {"min": str(MIN_PASSWORD_LENGTH)}),
        (
            "password",
            "é" * (MIN_PASSWORD_LENGTH - 1),
            "setup.owner.error.password",
            {"min": str(MIN_PASSWORD_LENGTH)},
        ),
        ("password", "", "setup.owner.error.password", {"min": str(MIN_PASSWORD_LENGTH)}),
    ],
    ids=[
        "blank name",
        "long name",
        "NUL in name",
        "bidi control in name",
        "line separator in name",
        "blank username",
        "long username",
        "NUL in username",
        "newline in username",
        "short password",
        "short password in characters, not bytes",
        "no password",
    ],
)
async def test_invalid_fields_are_refused_without_using_the_code_up(
    client, setup_code, session, field, value, error_key, params
):
    r = await client.post("/setup/owner", data=owner_form(code=setup_code) | {field: value})
    assert r.status_code == 400
    assert t(error_key, "en", **params) in visible(r)
    assert await count_owners(session) == 0
    assert await code_is_valid(session, setup_code)


def test_the_minimum_password_length_is_the_login_rule():
    assert MIN_PASSWORD_LENGTH == 8


async def test_password_too_short_refused(client, setup_code):
    r = await client.post("/setup/owner", data=owner_form(code=setup_code, password="short"))
    assert r.status_code == 400


async def test_a_password_of_the_minimum_length_is_accepted(client, setup_code, session):
    password = "é" * MIN_PASSWORD_LENGTH  # counted in characters
    r = await client.post("/setup/owner", data=owner_form(code=setup_code, password=password))
    assert r.status_code == 303
    account = (await session.execute(select(Account))).scalar_one()
    assert verify_password(account.password_hash, password)


async def test_the_password_is_stored_as_typed_not_trimmed(client, setup_code, session):
    password = "  spaces around me  "
    await client.post("/setup/owner", data=owner_form(code=setup_code, password=password))
    account = (await session.execute(select(Account))).scalar_one()
    assert verify_password(account.password_hash, password)
    assert not verify_password(account.password_hash, password.strip())


# --- throttling the code ---------------------------------------------------------------------


async def test_wrong_codes_are_throttled_globally(client, second_client, setup_code, session):
    for i in range(SETUP_CODE_MAX_ATTEMPTS):
        who = client if i % 2 else second_client  # the limit is for the whole instance
        r = await who.post("/setup/owner", data=owner_form(code=WRONG_CODE))
        assert r.status_code == 400
    r = await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert t("setup.owner.error.throttled", "en", minutes="15") in visible(r)
    # A lockout is a lockout: the right code does not get through either, until the window ends.
    r = await second_client.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 429
    assert await count_owners(session) == 0
    assert "set-cookie" not in r.headers


async def test_the_throttle_message_is_translated(client):
    for _ in range(SETUP_CODE_MAX_ATTEMPTS):
        await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    r = await client.post(
        "/setup/owner", data=owner_form(code=WRONG_CODE), headers={"accept-language": "fr"}
    )
    assert r.status_code == 429
    assert t("setup.owner.error.throttled", "fr", minutes="15") in visible(r)


async def test_the_throttle_is_ten_attempts_in_fifteen_minutes(client, setup_code, session, clock):
    assert SETUP_CODE_MAX_ATTEMPTS == 10
    for _ in range(SETUP_CODE_MAX_ATTEMPTS):
        await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    clock.advance(timedelta(minutes=14, seconds=59))
    assert (await client.post("/setup/owner", data=owner_form(code=setup_code))).status_code == 429
    clock.advance(timedelta(seconds=2))
    r = await client.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 303 and await count_owners(session) == 1


async def test_the_attempts_before_the_limit_do_not_stop_the_right_code(
    client, setup_code, session
):
    for _ in range(SETUP_CODE_MAX_ATTEMPTS - 1):
        await client.post("/setup/owner", data=owner_form(code=WRONG_CODE))
    r = await client.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 303 and await count_owners(session) == 1


async def test_forms_that_are_invalid_do_not_count_as_wrong_codes(client, setup_code, session):
    for _ in range(SETUP_CODE_MAX_ATTEMPTS + 3):
        r = await client.post("/setup/owner", data=owner_form(code=setup_code, password="short"))
        assert r.status_code == 400
    r = await client.post("/setup/owner", data=owner_form(code=setup_code))
    assert r.status_code == 303 and await count_owners(session) == 1


async def test_a_burst_of_parallel_guesses_cannot_beat_the_limit(
    app_factory, client_factory, setup_code
):
    """The attempt is counted before the code is checked, not after: a burst sent at once must
    not all get through."""
    async with client_factory(app_factory()) as http:
        results = await asyncio.gather(
            *(
                http.post("/setup/owner", data=owner_form(code=WRONG_CODE))
                for _ in range(SETUP_CODE_MAX_ATTEMPTS + 8)
            )
        )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [400] * SETUP_CODE_MAX_ATTEMPTS + [429] * 8
