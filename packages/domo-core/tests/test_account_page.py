"""The account page (task 11): each person sees their name, username and role, changes their
display name, and changes their password (the current one is asked for).

Changing the password ends every other session of the person and keeps the one it was done from.
Wrong current passwords are held back like wrong logins (the same limit), on a count of the account
that adding a passkey shares, apart from the login's (R84). A display name is one plain line of 1
to 60 characters, and nobody may take a name that another member of the household, or an
invitation that waits, already has (R70, R81): the page and ``profile_rename`` share the rule.

The pages are tried on the shared ``session`` (``adult_client``, ``second_adult_client``); the
renaming through Domo on a live server (the chat pilots, see ``chat_support.py``).
"""

import asyncio
import html
import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from domo_common.i18n import t
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Account, Person, WebSession
from domo_core.accounts.passwords import MIN_PASSWORD_LENGTH, verify_password
from domo_core.accounts.sessions import (
    SESSION_COOKIE,
    create_session,
    revoke_other_sessions,
    session_id_of,
)
from domo_core.household.service import (
    NameInUse,
    create_invitation,
    name_in_use,
    rename_person,
    resolve_invitation,
    resolve_person,
)
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.tools import household_admin
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.web.deps import get_clock
from sqlalchemy import delete, func, select, update
from web_support import (
    ADULT_PASSWORD,
    ADULT_USERNAME,
    audit_actions,
    audit_entries,
    in_language,
    log_in,
    reads_in,
)

NEW_PASSWORD = "another-long-one"
WRONG_PASSWORD = "wrong-password-x"


def password_form(
    current: str = ADULT_PASSWORD, new: str = NEW_PASSWORD, confirm: str | None = None
) -> dict[str, str]:
    return {"current": current, "new": new, "confirm": new if confirm is None else confirm}


def plain(page: str) -> str:
    """What a person reads in a page."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def attribute(page: str, name: str) -> str:
    """The value of the one ``data-<name>`` attribute of the page (tests read it, whatever the
    language)."""
    values = re.findall(rf'data-{name}="([^"]*)"', page)
    assert len(values) == 1, values
    return html.unescape(values[0])


async def stored_hash(session, account: Account) -> str:
    result = await session.execute(select(Account.password_hash).where(Account.id == account.id))
    return result.scalar_one()


async def name_of(session, account: Account) -> str:
    result = await session.execute(
        select(Person.display_name).where(Person.id == account.person_id)
    )
    return result.scalar_one()


async def session_count(session, account: Account) -> int:
    result = await session.execute(
        select(func.count()).select_from(WebSession).where(WebSession.account_id == account.id)
    )
    return result.scalar_one()


@pytest.fixture
def moving_time(app, live_clock):
    """The pages read the test clock, set to the real time (sessions are stamped with it), so
    that a test can move it."""
    app.dependency_overrides[get_clock] = lambda: live_clock
    return live_clock


# --- the brief's tests ---------------------------------------------------------------------------


async def test_password_change_requires_the_current_one(adult_client):
    r = await adult_client.post(
        "/account/password",
        data={
            "current": "wrong-password-x",
            "new": "another-long-one",
            "confirm": "another-long-one",
        },
    )
    assert r.status_code == 400


async def test_password_change_revokes_other_sessions(adult_client, second_adult_client):
    await adult_client.post(
        "/account/password",
        data={"current": ADULT_PASSWORD, "new": "another-long-one", "confirm": "another-long-one"},
    )
    assert (await second_adult_client.get("/chat")).status_code == 303
    assert (await adult_client.get("/chat")).status_code == 200


async def test_rename_is_escaped_and_audited(adult_client, session):
    await adult_client.post("/account/name", data={"name": "<b>Alex</b>"})
    assert "&lt;b&gt;Alex&lt;/b&gt;" in (await adult_client.get("/account")).text
    assert "profile.renamed" in await audit_actions(session)


# --- the page ------------------------------------------------------------------------------------


async def test_a_visitor_is_sent_to_the_login(client):
    for method, path in [
        ("get", "/account"),
        ("post", "/account/name"),
        ("post", "/account/password"),
    ]:
        r = await getattr(client, method)(path)
        assert (r.status_code, r.headers["location"]) == (303, "/login"), path


async def test_the_page_shows_name_username_and_role(adult_client):
    r = await adult_client.get("/account")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert attribute(r.text, "account-name") == "Alex"
    assert attribute(r.text, "account-username") == ADULT_USERNAME
    assert attribute(r.text, "account-role") == "adult"
    assert t("household.role.adult", "en") in plain(r.text)
    assert t("account.title", "en") in plain(r.text)


async def test_the_page_speaks_the_language_of_its_reader(adult_client, adult_account, session):
    await reads_in(session, adult_account.person_id, "fr")
    r = await adult_client.get("/account")
    assert '<html lang="fr">' in r.text
    assert in_language("fr", "account.title") in plain(r.text)
    assert in_language("fr", "household.role.adult") in plain(r.text)


async def test_the_owner_sees_their_own_account(client, owner_account):
    await log_in(client)
    r = await client.get("/account")
    assert r.status_code == 200
    assert attribute(r.text, "account-role") == "owner"
    assert attribute(r.text, "account-username") == owner_account.username


async def test_the_page_leaves_a_place_for_passkeys(adult_client):
    page = (await adult_client.get("/account")).text
    assert attribute(page, "passkeys") == "slot"
    assert t("account.passkeys.heading", "en") in plain(page)


async def test_the_page_has_no_script_of_its_own_and_no_style(adult_client):
    r = await adult_client.get("/account")
    assert r.status_code == 200
    page = r.text
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert scripts and all('src="/static/' in script for script in scripts)
    assert "<style" not in page and "style=" not in page and " onclick" not in page
    assert "hx-trigger" not in page


async def test_every_page_of_a_person_links_to_their_account(adult_client, client, owner_account):
    for path in ("/chat", "/account"):
        page = (await adult_client.get(path)).text
        assert 'href="/account"' in page
        assert t("nav.account", "en") in plain(page)
    await log_in(client)  # the owner has it too, next to their own links
    assert 'href="/account"' in (await client.get("/chat")).text


async def test_a_notice_follows_a_change_and_only_a_known_one(adult_client):
    page = (await adult_client.get("/account?saved=name")).text
    assert attribute(page, "saved") == "name"
    assert t("account.saved.name", "en") in plain(page)
    page = (await adult_client.get("/account?saved=password")).text
    assert attribute(page, "saved") == "password"
    assert t("account.saved.password", "en") in plain(page)
    for unknown in ("<b>x</b>", "", "Name", "name&saved=password"):
        page = (await adult_client.get("/account", params={"saved": unknown})).text
        assert "data-saved" not in page and "<b>x</b>" not in page


# --- renaming ------------------------------------------------------------------------------------


async def test_renaming_changes_the_name_and_audits_it_without_the_name(
    adult_client, adult_account, session
):
    r = await adult_client.post("/account/name", data={"name": "  Alexandra Q  "})
    assert (r.status_code, r.headers["location"]) == (303, "/account?saved=name")
    assert await name_of(session, adult_account) == "Alexandra Q"
    page = (await adult_client.get(r.headers["location"])).text
    assert attribute(page, "account-name") == "Alexandra Q"
    renamed = [e for e in await audit_entries(session) if e.action == "profile.renamed"]
    assert [(e.actor, e.details) for e in renamed] == [(f"person:{adult_account.person_id}", {})]
    # The audit log is append-only: the name is not written in it.
    assert "Alexandra" not in repr([tuple(e) for e in await audit_entries(session)])


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "x" * 61,
        "bad\x00name",
        "two\nlines",
        "tab\there",
        "line\N{LINE SEPARATOR}separator",
        "para\N{PARAGRAPH SEPARATOR}separator",
        "right\N{RIGHT-TO-LEFT OVERRIDE}to left",
        "isolate\N{LEFT-TO-RIGHT ISOLATE}here",
        "mark\N{RIGHT-TO-LEFT MARK}here",
        "\N{ZERO WIDTH SPACE}",
        " \N{ZERO WIDTH JOINER}\N{HANGUL FILLER} ",
    ],
    ids=[
        "empty",
        "blank",
        "too long",
        "NUL",
        "newline",
        "tab",
        "line separator",
        "paragraph separator",
        "right-to-left override",
        "isolate",
        "right-to-left mark",
        "zero width space alone",
        "nothing visible",
    ],
)
async def test_a_name_that_is_not_one_plain_line_of_1_to_60_characters_is_refused(
    adult_client, adult_account, session, name
):
    r = await adult_client.post("/account/name", data={"name": name})
    assert r.status_code == 400
    assert t("account.name.error.invalid", "en", max="60") in plain(r.text)
    assert await name_of(session, adult_account) == "Alex"
    assert "profile.renamed" not in await audit_actions(session)


async def test_a_name_of_exactly_60_characters_is_fine(adult_client):
    r = await adult_client.post("/account/name", data={"name": "é" * 60})
    assert r.status_code == 303


async def test_a_missing_name_is_a_refusal_not_a_crash(adult_client):
    r = await adult_client.post("/account/name")
    assert r.status_code == 400


async def test_a_refused_name_is_given_back_escaped_for_the_person_to_fix(adult_client):
    r = await adult_client.post("/account/name", data={"name": "<i>" + "x" * 70})
    assert r.status_code == 400
    assert "&lt;i&gt;" in r.text and "<i>" not in r.text


@pytest.mark.parametrize("name", ["Sam", "sam", "  SAM ", "sAM"])
async def test_a_name_another_member_has_is_refused(
    adult_client, adult_account, add_person, session, name
):
    await add_person("Sam", "child")
    r = await adult_client.post("/account/name", data={"name": name})
    assert r.status_code == 400
    assert t("account.name.error.in_use", "en") in plain(r.text)
    assert await name_of(session, adult_account) == "Alex"
    assert "profile.renamed" not in await audit_actions(session)


async def test_names_are_compared_whatever_the_encoding_of_their_accents(adult_client, add_person):
    await add_person("Zoé", "child")  # the accented letter as one character
    r = await adult_client.post(
        "/account/name", data={"name": "Zoe\N{COMBINING ACUTE ACCENT}"}
    )  # a letter, an accent
    assert r.status_code == 400


async def test_a_name_an_open_invitation_has_is_refused(
    adult_client, add_person, create_invitation_for_test
):
    await add_person("Owner", "owner")
    await create_invitation_for_test("Robin", "guest")
    r = await adult_client.post("/account/name", data={"name": "robin"})
    assert r.status_code == 400
    assert t("account.name.error.in_use", "en") in plain(r.text)


async def test_a_name_whose_invitation_ended_is_free(
    adult_client, add_person, create_invitation_for_test, moving_time
):
    await add_person("Owner", "owner")
    await create_invitation_for_test("Robin", "guest")
    moving_time.advance(timedelta(days=8))  # the link works for seven days
    r = await adult_client.post("/account/name", data={"name": "Robin"})
    assert r.status_code == 303


async def test_the_name_of_someone_who_left_is_free(adult_client, add_person, session, moving_time):
    gone = await add_person("Sam", "child")
    gone.disabled_at = moving_time()
    gone.state = "gone"
    over = await add_person("Robin", "guest")
    over.expires_at = moving_time() - timedelta(hours=1)
    await session.flush()
    for name in ("Sam", "Robin"):
        r = await adult_client.post("/account/name", data={"name": name})
        assert r.status_code == 303, name


async def test_a_person_may_change_the_case_of_their_own_name(adult_client, adult_account, session):
    r = await adult_client.post("/account/name", data={"name": "ALEX"})
    assert r.status_code == 303
    assert await name_of(session, adult_account) == "ALEX"


async def test_changes_from_another_site_are_refused(
    adult_client, client_factory, app, adult_account, session
):
    token = adult_client.cookies[SESSION_COOKIE]
    for origin in (None, "https://evil.example.net"):
        async with client_factory(app, origin=origin) as elsewhere:
            elsewhere.cookies.set(SESSION_COOKIE, token)
            named = await elsewhere.post("/account/name", data={"name": "Mallory"})
            changed = await elsewhere.post("/account/password", data=password_form())
        assert (named.status_code, changed.status_code) == (403, 403), origin
    assert await name_of(session, adult_account) == "Alex"
    assert verify_password(await stored_hash(session, adult_account), ADULT_PASSWORD)
    # The same request from the page itself goes through.
    assert (await adult_client.post("/account/name", data={"name": "Alex B"})).status_code == 303


# --- the password --------------------------------------------------------------------------------


async def test_changing_the_password_replaces_it_and_audits_it(
    adult_client, adult_account, session, client
):
    r = await adult_client.post("/account/password", data=password_form())
    assert (r.status_code, r.headers["location"]) == (303, "/account?saved=password")
    new_hash = await stored_hash(session, adult_account)
    assert verify_password(new_hash, NEW_PASSWORD)
    assert not verify_password(new_hash, ADULT_PASSWORD)
    # The old password no longer logs in, the new one does.
    old = await client.post("/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD})
    assert old.status_code == 400
    new = await client.post("/login", data={"username": ADULT_USERNAME, "password": NEW_PASSWORD})
    assert new.status_code == 303
    changed = [e for e in await audit_entries(session) if e.action == "auth.password_changed"]
    assert [(e.actor, e.details) for e in changed] == [
        (f"person:{adult_account.person_id}", {"revoked_sessions": 0})
    ]
    assert NEW_PASSWORD not in repr([tuple(e) for e in await audit_entries(session)])


async def test_the_audit_says_how_many_other_sessions_ended(
    adult_client, second_adult_client, adult_account, session
):
    await adult_client.post("/account/password", data=password_form())
    changed = [e for e in await audit_entries(session) if e.action == "auth.password_changed"]
    assert [e.details for e in changed] == [{"revoked_sessions": 1}]
    assert await session_count(session, adult_account) == 1


async def test_only_the_sessions_of_this_person_end(
    adult_client, second_adult_client, client, owner_account
):
    await log_in(client)  # the owner has a session too
    await adult_client.post("/account/password", data=password_form())
    assert (await second_adult_client.get("/chat")).status_code == 303
    assert (await client.get("/chat")).status_code == 200


async def test_the_session_it_was_done_from_stays_and_still_works_on_the_page(adult_client):
    r = await adult_client.post("/account/password", data=password_form())
    assert r.status_code == 303
    page = await adult_client.get(r.headers["location"])
    assert page.status_code == 200
    assert t("account.saved.password", "en") in plain(page.text)


async def test_a_wrong_current_password_changes_nothing(
    adult_client, second_adult_client, adult_account, session
):
    before = await stored_hash(session, adult_account)
    r = await adult_client.post("/account/password", data=password_form(current=WRONG_PASSWORD))
    assert r.status_code == 400
    assert t("account.password.error.current", "en") in plain(r.text)
    assert await stored_hash(session, adult_account) == before
    assert (await second_adult_client.get("/chat")).status_code == 200  # no session ended
    assert "auth.password_changed" not in await audit_actions(session)
    # Nothing that was typed comes back in the page.
    assert WRONG_PASSWORD not in r.text and NEW_PASSWORD not in r.text


async def test_a_wrong_current_password_is_audited_without_it(adult_client, adult_account, session):
    await adult_client.post("/account/password", data=password_form(current=WRONG_PASSWORD))
    failed = [e for e in await audit_entries(session) if e.action == "auth.password_change_failed"]
    assert [(e.actor, e.details) for e in failed] == [(f"person:{adult_account.person_id}", {})]
    assert WRONG_PASSWORD not in repr([tuple(e) for e in await audit_entries(session)])


async def test_the_new_password_must_be_long_enough_counted_in_characters(
    adult_client, adult_account, session
):
    before = await stored_hash(session, adult_account)
    short = "é" * (MIN_PASSWORD_LENGTH - 1)  # more bytes than characters
    r = await adult_client.post("/account/password", data=password_form(new=short))
    assert r.status_code == 400
    assert t("account.password.error.short", "en", min=str(MIN_PASSWORD_LENGTH)) in plain(r.text)
    assert await stored_hash(session, adult_account) == before
    enough = "é" * MIN_PASSWORD_LENGTH
    r = await adult_client.post("/account/password", data=password_form(new=enough))
    assert r.status_code == 303
    assert verify_password(await stored_hash(session, adult_account), enough)


async def test_the_two_new_passwords_must_match(adult_client, adult_account, session):
    before = await stored_hash(session, adult_account)
    r = await adult_client.post("/account/password", data=password_form(confirm="another-long-two"))
    assert r.status_code == 400
    assert t("account.password.error.mismatch", "en") in plain(r.text)
    assert await stored_hash(session, adult_account) == before


async def test_a_new_password_is_kept_as_typed_not_trimmed(adult_client, adult_account, session):
    spaced = "  spaces around me  "
    r = await adult_client.post("/account/password", data=password_form(new=spaced))
    assert r.status_code == 303
    new_hash = await stored_hash(session, adult_account)
    assert verify_password(new_hash, spaced) and not verify_password(new_hash, spaced.strip())


async def test_missing_fields_are_a_refusal_not_a_crash(adult_client):
    r = await adult_client.post("/account/password")
    assert r.status_code == 400


async def test_a_refusal_for_the_new_password_does_not_cost_an_attempt(adult_client):
    """What is wrong with the new password says nothing about the current one: it is not a guess."""
    for _ in range(8):
        r = await adult_client.post("/account/password", data=password_form(new="short"))
        assert r.status_code == 400
    r = await adult_client.post("/account/password", data=password_form())
    assert r.status_code == 303


# --- held back like a login ----------------------------------------------------------------------


async def test_wrong_current_passwords_are_throttled_like_logins(
    adult_client, adult_account, session, clock
):
    wrong = password_form(current=WRONG_PASSWORD)
    for _ in range(5):
        assert (await adult_client.post("/account/password", data=wrong)).status_code == 400
    before = await stored_hash(session, adult_account)
    # Even the right password is refused while the limit is reached.
    r = await adult_client.post("/account/password", data=password_form())
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert t("account.password.error.throttled", "en", minutes="15") in plain(r.text)
    assert await stored_hash(session, adult_account) == before
    clock.advance(timedelta(minutes=16))
    assert (await adult_client.post("/account/password", data=password_form())).status_code == 303


async def test_a_blocked_login_does_not_keep_a_person_from_changing_their_password(
    adult_client, client
):
    """R84: anyone may hammer the login with someone's username; the account page counts per
    account, apart from it, so that the person can still change their password."""
    for _ in range(5):
        await client.post("/login", data={"username": ADULT_USERNAME, "password": WRONG_PASSWORD})
    blocked = await client.post(
        "/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD}
    )
    assert blocked.status_code == 429
    assert (await adult_client.post("/account/password", data=password_form())).status_code == 303


async def test_the_page_counts_wrong_passwords_apart_from_the_login(adult_client, client):
    """A stolen session cannot guess the password at its leisure on the page (five in any window
    of fifteen minutes, as at the login), and its guesses there are not the login's."""
    for _ in range(5):
        await adult_client.post("/account/password", data=password_form(current=WRONG_PASSWORD))
    assert (await adult_client.post("/account/password", data=password_form())).status_code == 429
    r = await client.post("/login", data={"username": ADULT_USERNAME, "password": ADULT_PASSWORD})
    assert r.status_code == 303


async def test_a_right_current_password_forgets_the_wrong_ones(adult_client):
    wrong = password_form(current=WRONG_PASSWORD)
    for _ in range(4):
        assert (await adult_client.post("/account/password", data=wrong)).status_code == 400
    assert (await adult_client.post("/account/password", data=password_form())).status_code == 303
    for _ in range(5):  # the count started again: none of these is held back
        assert (await adult_client.post("/account/password", data=wrong)).status_code == 400


async def test_a_password_changed_meanwhile_is_not_overwritten(
    adult_client, adult_account, session, monkeypatch
):
    """The slow steps run without a lock: if the password changed while they ran, the change is
    refused rather than written over the newer one."""
    from domo_core.accounts import account_routes

    real = account_routes.hash_password_limited
    account_id = adult_account.id

    async def racing(plain):
        await session.execute(
            update(Account).where(Account.id == account_id).values(password_hash="newer")
        )
        return await real(plain)

    monkeypatch.setattr(account_routes, "hash_password_limited", racing)
    r = await adult_client.post("/account/password", data=password_form())
    assert r.status_code == 400
    assert await stored_hash(session, adult_account) == "newer"
    assert "auth.password_changed" not in await audit_actions(session)


# --- one rule for every display name (R70, R80) --------------------------------------------------


def test_the_rule_of_a_display_name_is_in_a_neutral_module():
    from domo_core.text import MAX_DISPLAY_NAME_LENGTH, InvalidDisplayName, display_name_value

    assert MAX_DISPLAY_NAME_LENGTH == 60
    assert display_name_value("  Alex B  ") == "Alex B"
    assert display_name_value("é" * 60) == "é" * 60
    for bad in (
        "",
        "   ",
        "x" * 61,
        "a\nb",
        "a\x00b",
        "a\N{RIGHT-TO-LEFT OVERRIDE}b",
        "a\N{LINE SEPARATOR}b",
        5,
        None,
        ["a"],
    ):
        with pytest.raises(InvalidDisplayName):
            display_name_value(bad)
    with pytest.raises(InvalidDisplayName) as too_long:
        display_name_value("x" * 61)
    assert too_long.value.code == "too_long"
    with pytest.raises(InvalidDisplayName) as invalid:
        display_name_value("a\nb")
    assert invalid.value.code == "invalid"


@pytest.mark.parametrize(
    "invisible",
    [
        "\N{ZERO WIDTH SPACE}",
        "\N{ZERO WIDTH JOINER}\N{ZERO WIDTH NON-JOINER}",
        "\N{SOFT HYPHEN}",
        "\N{ZERO WIDTH NO-BREAK SPACE}",
        "\N{WORD JOINER} \N{WORD JOINER}",
        "\N{HANGUL FILLER}",
        "\N{VARIATION SELECTOR-16}",
        "\N{COMBINING GRAPHEME JOINER}",
    ],
)
def test_a_name_made_only_of_invisible_characters_is_refused(invisible):
    """R86: such a name reads as no name at all, and the comparison of names (``name_key``) would
    find it empty."""
    from domo_core.text import InvalidDisplayName, display_name_value, name_key

    assert name_key(invisible) == ""
    with pytest.raises(InvalidDisplayName) as refused:
        display_name_value(invisible)
    assert refused.value.code == "invalid"
    assert display_name_value(f"Sam{invisible}") == f"Sam{invisible}"  # around a name: fine


def test_the_comparison_of_names_is_in_the_neutral_module_too():
    from domo_core.household import service
    from domo_core.text import name_key

    assert name_key("  ＡＬＥＸ\N{ZERO WIDTH SPACE}  b ") == name_key("alex b")
    assert "def _name_key" not in inspect.getsource(service)


def test_names_are_set_without_reaching_into_the_tools_layer_for_the_rule():
    from domo_core.accounts import account_routes
    from domo_core.household import service
    from domo_core.setup_flow import owner_routes

    for module in (service, account_routes, owner_routes):
        assert "domo_core.tools.profile" not in inspect.getsource(module), module.__name__


def test_the_tool_still_speaks_the_tools_language():
    """``profile_rename``'s own checks answer with ``InvalidArguments``, as before."""
    from domo_core.tools.profile import display_name_value

    assert display_name_value(" Alex ") == "Alex"
    cases = [
        ("", "invalid"),
        ("x" * 61, "too_long"),
        ("a\N{RIGHT-TO-LEFT OVERRIDE}b", "invalid"),
        (3, "invalid"),
    ]
    for bad, code in cases:
        with pytest.raises(InvalidArguments) as refused:
            display_name_value(bad)
        assert refused.value.code == code


async def test_two_people_cannot_both_take_the_same_free_name(session_factory):
    """Both look, and both find the name free: the lock of the display names makes the second
    one wait for the first one's commit, and then see it."""
    people = []
    async with session_factory() as writer:
        for name in ("Alex", "Sam"):
            person = Person(display_name=name, role="adult")
            writer.add(person)
            people.append(person)
        await writer.commit()

    async def rename(person: Person) -> str:
        async with session_factory() as own:
            try:
                await rename_person(own, person, "Robin", now=datetime.now(UTC))
            except NameInUse:
                return "in use"
            await asyncio.sleep(0.3)  # the other one arrives while this one has not committed yet
            await own.commit()
            return "renamed"

    results = await asyncio.gather(*(rename(person) for person in people))
    assert sorted(results) == ["in use", "renamed"]


# --- profile_rename keeps the same names apart (R81) ---------------------------------------------


def context(person: Person, session_factory) -> ToolContext:
    """The context of a call made by ``person`` (what the tools do not use is left empty)."""
    return ToolContext(
        session_factory=session_factory,
        actor=actor_of(person),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.local/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


async def test_domo_does_not_rename_someone_to_a_name_another_member_has(
    adult_chat, child_member, display_name_of
):
    for name in ("Sam", "sam", " SAM "):
        reply = await adult_chat.say_with_tool("profile_rename", {"name": name})
        assert reply.actions == [
            {"tool": "profile_rename", "status": "invalid", "reason": "invalid"}
        ]
    assert await display_name_of(adult_chat.person) == "Alex"
    assert await display_name_of(child_member.person) == "Sam"


async def test_domo_does_not_rename_someone_to_the_name_of_an_open_invitation(
    adult_chat, owner_member, session_factory, display_name_of
):
    async with session_factory() as writer:
        await create_invitation(
            writer,
            display_name="Robin",
            role="guest",
            guest_days=None,
            created_by=owner_member.person.id,
            now=datetime.now(UTC),
        )
        await writer.commit()
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "robin"})
    assert reply.actions == [{"tool": "profile_rename", "status": "invalid", "reason": "invalid"}]
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_domo_lets_a_person_change_the_case_of_their_own_name(adult_chat, display_name_of):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "ALEX"}, judge="do")
    assert reply.actions == [{"tool": "profile_rename", "status": "done"}]
    assert await display_name_of(adult_chat.person) == "ALEX"


async def test_a_name_taken_after_the_request_was_made_is_refused_when_it_runs(
    adult_member, child_member, session_factory, display_name_of
):
    """A request that waits for a yes can be approved later: the name is checked again then."""
    tool = TOOLS["profile_rename"]
    ctx = context(adult_member.person, session_factory)
    await tool.describe({"name": "Robin"}, ctx.actor, ctx)  # free when it was asked
    async with session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == child_member.person.id).values(display_name="robin")
        )
        await writer.commit()
    with pytest.raises(InvalidArguments) as refused:
        await tool.execute({"name": "Robin"}, ctx)
    assert refused.value.code == "invalid"
    assert await display_name_of(adult_member.person) == "Alex"


# --- names that read the same are the same name (R81) --------------------------------------------


def fullwidth(text: str) -> str:
    """``text`` with its letters as full-width letters: they read like the same letters."""
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


# (the name someone has, a name that reads the same to the people who see it but is another text)
LOOK_ALIKES = [
    pytest.param("Sam Bee", "Sam Bee\N{ZERO WIDTH SPACE}", id="zero width space"),
    pytest.param("Sam Bee", "Sam B\N{ZERO WIDTH JOINER}ee", id="zero width joiner"),
    pytest.param("Sam Bee", "Sam B\N{SOFT HYPHEN}ee", id="soft hyphen"),
    pytest.param("Sam Bee", "Sam Bee\N{WORD JOINER}", id="word joiner"),
    pytest.param("Sam Bee", "\N{ZERO WIDTH NO-BREAK SPACE}Sam Bee", id="byte order mark"),
    pytest.param("Sam Bee", "Sam\N{ZERO WIDTH SPACE} Bee", id="zero width space before a space"),
    pytest.param("Sam Bee", "Sam \N{ZERO WIDTH SPACE} Bee", id="zero width space between spaces"),
    pytest.param("Sam Bee", "Sam Be\N{COMBINING GRAPHEME JOINER}e", id="grapheme joiner"),
    pytest.param("Sam Bee", "Sam Bee\N{HANGUL FILLER}", id="hangul filler"),
    pytest.param("Sam Bee", "Sam Bee\N{HALFWIDTH HANGUL FILLER}", id="half-width hangul filler"),
    pytest.param("Sam Bee", "Sam Bee\N{VARIATION SELECTOR-16}", id="variation selector 16"),
    pytest.param("Sam Bee", "Sam Bee\N{VARIATION SELECTOR-17}", id="variation selector 17"),
    pytest.param(
        "Sam Bee", "Sam Bee\N{MONGOLIAN FREE VARIATION SELECTOR ONE}", id="mongolian selector"
    ),
    pytest.param("Sam Bee", "Sam  Bee", id="two spaces"),
    pytest.param("Sam Bee", "Sam   Bee", id="three spaces"),
    pytest.param("Sam Bee", "Sam\N{NO-BREAK SPACE}Bee", id="no-break space"),
    pytest.param("Sam Bee", "Sam\N{EM SPACE}Bee", id="em space"),
    pytest.param("Sam Bee", "Sam\N{IDEOGRAPHIC SPACE}Bee", id="ideographic space"),
    pytest.param("Sam Bee", "Sam\N{OGHAM SPACE MARK}Bee", id="ogham space mark"),
    pytest.param("Sam Bee", "Sam\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}Bee", id="two no-break spaces"),
    pytest.param("Sam Bee", fullwidth("Sam") + " " + fullwidth("Bee"), id="full-width letters"),
    pytest.param("Fiona", "\N{LATIN SMALL LIGATURE FI}ona", id="ligature"),
    pytest.param(
        "Zo\N{LATIN SMALL LETTER E WITH ACUTE}", "Zoe\N{COMBINING ACUTE ACCENT}", id="accent"
    ),
]


@pytest.mark.parametrize(("name", "look_alike"), LOOK_ALIKES)
async def test_a_name_that_reads_like_another_is_refused_on_the_account_page(
    adult_client, adult_account, add_person, session, name, look_alike
):
    await add_person(name, "child")
    r = await adult_client.post("/account/name", data={"name": look_alike})
    assert r.status_code == 400
    assert t("account.name.error.in_use", "en") in plain(r.text)
    assert await name_of(session, adult_account) == "Alex"
    assert "profile.renamed" not in await audit_actions(session)


@pytest.mark.parametrize(("name", "look_alike"), LOOK_ALIKES)
async def test_a_name_that_reads_like_the_one_of_an_open_invitation_is_refused(
    adult_client, add_person, create_invitation_for_test, name, look_alike
):
    await add_person("Owner", "owner")
    await create_invitation_for_test(name, "guest")
    r = await adult_client.post("/account/name", data={"name": look_alike})
    assert r.status_code == 400
    assert t("account.name.error.in_use", "en") in plain(r.text)


@pytest.mark.parametrize(("name", "look_alike"), LOOK_ALIKES)
async def test_a_look_alike_is_in_use_unless_it_is_the_persons_own_name(
    session, add_person, create_invitation_for_test, clock, name, look_alike
):
    await add_person("Owner", "owner")
    person = await add_person(name, "child")
    assert await name_in_use(session, look_alike, now=clock())
    # Someone may write their own name another way: that is not a second person.
    assert not await name_in_use(session, look_alike, now=clock(), excluding=person.id)
    person.disabled_at = clock()
    person.state = "gone"
    await session.flush()
    assert not await name_in_use(session, look_alike, now=clock())
    await create_invitation_for_test(name, "adult")
    assert await name_in_use(session, look_alike, now=clock())


@pytest.mark.parametrize(("name", "look_alike"), LOOK_ALIKES)
async def test_asked_for_by_a_look_alike_the_person_and_the_invitation_are_still_found(
    session, add_person, create_invitation_for_test, clock, name, look_alike
):
    await add_person("Owner", "owner")
    person = await add_person(name, "child")
    invitation, _ = await create_invitation_for_test(name, "adult")
    assert (await resolve_person(session, look_alike, now=clock())).id == person.id
    assert (await resolve_invitation(session, look_alike, now=clock())).id == invitation.id


async def test_asking_for_alex_with_an_invisible_character_still_finds_alex(
    session, add_person, clock
):
    alex = await add_person("Alex", "adult")
    asked_for = [
        "Alex\N{ZERO WIDTH SPACE}",
        "al\N{SOFT HYPHEN}ex",
        " ALEX ",
        fullwidth("Alex"),
    ]
    for asked in asked_for:
        assert (await resolve_person(session, asked, now=clock())).id == alex.id, ascii(asked)
    with pytest.raises(InvalidArguments) as unknown:
        await resolve_person(session, "Alexa", now=clock())
    assert unknown.value.code == "not_found"


async def test_two_people_who_read_alike_are_ambiguous_not_guessed(session, add_person, clock):
    """People who joined before the stricter comparison may already read alike: nobody is
    picked at random."""
    await add_person("Alex", "adult")
    await add_person("Alex\N{ZERO WIDTH SPACE}", "child")
    with pytest.raises(InvalidArguments) as ambiguous:
        await resolve_person(session, "Alex", now=clock())
    assert ambiguous.value.code == "ambiguous"


async def test_names_that_differ_are_not_folded_together(adult_client, add_person):
    await add_person("Sam Bee", "child")
    for other in ("SamBee", "Sam Beee", "Sam B", "Sami Bee", "Bee Sam"):
        r = await adult_client.post("/account/name", data={"name": other})
        assert r.status_code == 303, other


async def test_domo_does_not_invite_a_name_that_reads_like_a_member(
    owner_chat, adult_member, count_invitations
):
    look_alikes = [
        "Alex\N{ZERO WIDTH SPACE}",
        "Al\N{ZERO WIDTH JOINER}ex",
        "Al\N{SOFT HYPHEN}ex",
        "\N{ZERO WIDTH NO-BREAK SPACE}Alex",
        "Alex\N{VARIATION SELECTOR-16}",
        fullwidth("Alex"),
    ]
    for look_alike in look_alikes:
        reply = await owner_chat.say_with_tool(
            "household_invite", {"display_name": look_alike, "role": "guest"}
        )
        assert reply.actions == [
            {"tool": "household_invite", "status": "invalid", "reason": "invalid"}
        ], ascii(look_alike)
    assert await count_invitations() == 0


async def test_domo_does_not_rename_someone_to_a_look_alike_of_a_member(
    adult_chat, child_member, display_name_of
):
    for look_alike in ("Sam\N{ZERO WIDTH SPACE}", "S\N{WORD JOINER}am", fullwidth("Sam")):
        reply = await adult_chat.say_with_tool("profile_rename", {"name": look_alike})
        assert reply.actions == [
            {"tool": "profile_rename", "status": "invalid", "reason": "invalid"}
        ], ascii(look_alike)
    assert await display_name_of(adult_chat.person) == "Alex"


# --- an invitation and a rename cannot take the same free name (R81, the invitation's side) ------


@pytest.mark.parametrize(
    "invitation_first", [True, False], ids=["invitation first", "rename first"]
)
async def test_an_invitation_and_a_rename_cannot_both_take_the_same_free_name(
    session_factory, monkeypatch, invitation_first
):
    """Both look, and both find "Robin" free. The one that came first keeps the lock of the
    display names until it commits (its commit is delayed on purpose), so the other waits and then
    sees the name taken. Without the lock in ``household_invite.execute`` both would win."""
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        alex = Person(display_name="Alex", role="adult")
        writer.add_all([owner, alex])
        await writer.commit()

    real_create = household_admin.create_invitation

    async def create_then_wait(*args, **kwargs):
        made = await real_create(*args, **kwargs)
        if invitation_first:
            await asyncio.sleep(0.5)  # not committed yet: the rename arrives now
        return made

    monkeypatch.setattr(household_admin, "create_invitation", create_then_wait)

    async def invite() -> str:
        try:
            await TOOLS["household_invite"].execute(
                {"display_name": "Robin", "role": "adult"}, context(owner, session_factory)
            )
        except InvalidArguments:
            return "in use"
        return "invited"

    async def rename() -> str:
        async with session_factory() as own:
            try:
                await rename_person(own, alex, "robin", now=datetime.now(UTC))
            except NameInUse:
                return "in use"
            if not invitation_first:
                await asyncio.sleep(0.5)  # not committed yet: the invitation arrives now
            await own.commit()
            return "renamed"

    first, second = (invite, rename) if invitation_first else (rename, invite)
    started = asyncio.create_task(first())
    await asyncio.sleep(0.2)
    results = [await second(), await started]
    winner = "invited" if invitation_first else "renamed"
    assert sorted(results) == sorted(["in use", winner])


# --- the session that is kept is named, never guessed --------------------------------------------


async def three_sessions(session, account):
    tokens = [await create_session(session, account.id, ttl=timedelta(days=1)) for _ in range(3)]
    return tokens, [await session_id_of(session, token) for token in tokens]


async def test_the_session_of_a_token_is_found_by_its_id(session, adult_account):
    _, ids = await three_sessions(session, adult_account)
    assert all(ids) and len(set(ids)) == 3
    assert await session_id_of(session, "no-such-token") is None
    assert await session_id_of(session, "") is None


async def test_the_other_sessions_end_and_the_one_given_stays(session, adult_account):
    _, ids = await three_sessions(session, adult_account)
    assert await revoke_other_sessions(session, adult_account.id, ids[1]) == 2
    left = await session.execute(
        select(WebSession.id).where(WebSession.account_id == adult_account.id)
    )
    assert left.scalars().all() == [ids[1]]


async def test_the_sessions_of_other_people_are_not_touched(session, adult_account, owner_account):
    _, ids = await three_sessions(session, adult_account)
    await three_sessions(session, owner_account)
    await revoke_other_sessions(session, adult_account.id, ids[0])
    assert await session_count(session, owner_account) == 3


@pytest.mark.parametrize("kind", ["missing", "unknown", "of another account"])
async def test_a_session_to_keep_that_is_not_one_of_the_account_revokes_nothing(
    session, adult_account, owner_account, kind
):
    """An empty or wrong "keep" is an error, never "revoke them all"."""
    await three_sessions(session, adult_account)
    _, owner_ids = await three_sessions(session, owner_account)
    keep = {"missing": None, "unknown": uuid.uuid4(), "of another account": owner_ids[0]}[kind]
    with pytest.raises(ValueError):
        await revoke_other_sessions(session, adult_account.id, keep)  # type: ignore[arg-type]
    assert await session_count(session, adult_account) == 3
    assert await session_count(session, owner_account) == 3


async def test_a_session_that_ended_during_the_change_sends_to_the_login(
    adult_client, adult_account, session, monkeypatch
):
    """R85: the session the change comes from ended while the slow steps ran (a logout in another
    tab, say), so the other sessions cannot be told from it: nothing is written and the person
    signs in again. Never a 500."""
    from domo_core.accounts import account_routes

    real = account_routes.revoke_other_sessions

    async def ended_meanwhile(db, account_id, keep_session_id):
        await db.execute(delete(WebSession).where(WebSession.id == keep_session_id))
        return await real(db, account_id, keep_session_id)

    monkeypatch.setattr(account_routes, "revoke_other_sessions", ended_meanwhile)
    account_id = adult_account.id  # the rollback expires what the session had loaded
    before = await stored_hash(session, adult_account)
    r = await adult_client.post("/account/password", data=password_form())
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    stored = await session.execute(select(Account.password_hash).where(Account.id == account_id))
    assert stored.scalar_one() == before
    assert "auth.password_changed" not in await audit_actions(session)


async def test_a_password_change_from_a_session_that_is_gone_changes_nothing(
    adult_client, adult_account, session, monkeypatch
):
    """The session the change comes from vanished (a logout in another tab, say): the person is
    not signed in any more, so they are sent to the login and nothing is written."""
    from domo_core.accounts import account_routes

    async def gone(session, raw_token):
        return None

    monkeypatch.setattr(account_routes, "session_id_of", gone)
    before = await stored_hash(session, adult_account)
    r = await adult_client.post("/account/password", data=password_form())
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    assert await stored_hash(session, adult_account) == before
    assert "auth.password_changed" not in await audit_actions(session)
