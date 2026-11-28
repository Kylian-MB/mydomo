"""The dependencies the pages share: `current_person`, `require_owner` and `require_action`."""

from datetime import UTC, datetime, timedelta

import pytest
from domo_common.i18n import t
from domo_core.accounts.models import WebSession
from domo_core.accounts.sessions import SESSION_COOKIE, create_session
from domo_core.policy.models import PolicyRight
from domo_core.web.deps import get_clock
from sqlalchemy import select
from web_support import in_language, reads_in


def with_cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={token}"}


async def test_a_visitor_without_a_session_is_sent_to_the_login(client):
    r = await client.get("/whoami")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_the_redirect_also_answers_unsafe_methods(client):
    r = await client.post("/whoami")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.parametrize("token", ["garbage", "x" * 4000, "../../etc/passwd"])
async def test_an_unknown_token_is_sent_to_the_login(client, owner_account, token):
    r = await client.get("/whoami", headers=with_cookie(token))
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_an_expired_session_is_sent_to_the_login(client, session, owner_account):
    token = await create_session(session, owner_account.id, ttl=timedelta(seconds=-1))
    r = await client.get("/whoami", headers=with_cookie(token))
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_a_valid_session_gives_the_person(client, session, owner_account):
    token = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    r = await client.get("/whoami", headers=with_cookie(token))
    assert r.status_code == 200
    assert r.json() == {
        "id": str(owner_account.person_id),
        "name": "Owner",
        "role": "owner",
    }


async def test_a_removed_person_is_sent_to_the_login(client, session, add_person, add_account):
    member = await add_person("Member", "adult")
    account = await add_account(member, "member")
    token = await create_session(session, account.id, ttl=timedelta(days=1))
    assert (await client.get("/whoami", headers=with_cookie(token))).status_code == 200
    member.disabled_at = datetime.now(UTC)
    member.state = "gone"
    await session.flush()
    r = await client.get("/whoami", headers=with_cookie(token))
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_the_session_is_checked_at_the_time_of_the_pages_clock(
    app, client, session, owner_account
):  # R36
    token = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    later = datetime.now(UTC) + timedelta(days=2)
    app.dependency_overrides[get_clock] = lambda: lambda: later
    r = await client.get("/whoami", headers=with_cookie(token))
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_using_a_session_records_that_it_was_seen(client, session, owner_account):
    token = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    before = (await session.execute(select(WebSession.last_seen_at))).scalar_one()
    await client.get("/whoami", headers=with_cookie(token))
    after = (await session.execute(select(WebSession.last_seen_at))).scalar_one()
    assert after > before


# --- htmx requests ---------------------------------------------------------------------------


async def test_an_htmx_request_without_a_session_gets_401_and_a_redirect_header(client):
    r = await client.get("/whoami", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"
    assert "location" not in r.headers


async def test_an_htmx_post_is_answered_the_same_way(client):
    r = await client.post("/whoami", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"


async def test_a_request_that_only_looks_like_htmx_is_a_normal_one(client):
    r = await client.get("/whoami", headers={"HX-Request": "false"})
    assert r.status_code == 303
    assert "hx-redirect" not in r.headers


async def test_an_htmx_request_with_a_session_is_served(client, session, owner_account):
    token = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    r = await client.get("/whoami", headers={"HX-Request": "true", **with_cookie(token)})
    assert r.status_code == 200


# --- require_owner ---------------------------------------------------------------------------


async def test_the_owner_passes_require_owner(client, session, owner_account):
    token = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    r = await client.get("/owner-only", headers=with_cookie(token))
    assert r.status_code == 200


async def test_someone_who_is_not_the_owner_gets_403(client, session, add_person, add_account):
    member = await add_person("Member", "adult")
    account = await add_account(member, "member")
    token = await create_session(session, account.id, ttl=timedelta(days=1))
    r = await client.get("/owner-only", headers=with_cookie(token))
    assert r.status_code == 403
    assert "text/html" in r.headers["content-type"]
    assert t("error.forbidden.title", "en") in r.text
    await reads_in(session, member.id, "fr")
    french = await client.get("/owner-only", headers=with_cookie(token))
    assert in_language("fr", "error.forbidden.title") in french.text


async def test_require_owner_still_asks_visitors_to_log_in(client):
    r = await client.get("/owner-only")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- require_action --------------------------------------------------------------------------


async def a_right(session, owner_id, **fields) -> None:
    """A right of the owner, written in the shared session (flushed)."""
    session.add(
        PolicyRight(
            action_type="household.invite",
            summary="test right",
            owner_words="",
            created_by=owner_id,
            created_at=datetime.now(UTC),
            **fields,
        )
    )
    await session.flush()


async def test_a_page_for_an_action_is_for_whoever_the_rights_give_it_to(
    client, session, owner_account, add_person, add_account
):
    adult = await add_person("Adult", "adult")
    child = await add_person("Child", "child")
    other = await add_person("Other", "child")
    tokens = {}
    for person, username in ((adult, "adult"), (child, "child"), (other, "other")):
        account = await add_account(person, username)
        tokens[username] = await create_session(session, account.id, ttl=timedelta(days=1))
    owner_token = await create_session(session, owner_account.id, ttl=timedelta(days=1))

    async def status(token: str) -> int:
        return (await client.get("/can-invite", headers=with_cookie(token))).status_code

    # By default the owner's alone: inviting is in no role (ruling 14).
    assert [await status(owner_token), await status(tokens["adult"])] == [200, 403]
    assert [await status(tokens["child"]), await status(tokens["other"])] == [403, 403]
    await a_right(session, owner_account.person_id, person_id=adult.id, access="allow")
    assert await status(tokens["adult"]) == 200

    # A right of the person is theirs only; a right of a role is its people's.
    await a_right(session, owner_account.person_id, person_id=other.id, access="allow")
    assert [await status(tokens["other"]), await status(tokens["child"])] == [200, 403]
    await a_right(session, owner_account.person_id, role="child", access="allow")
    assert await status(tokens["child"]) == 200

    # And taken away it is gone (even from the owner: the page follows the same rights as the
    # tools a person is shown).
    await a_right(
        session, owner_account.person_id, person_id=owner_account.person_id, access="deny"
    )
    assert await status(owner_token) == 403


async def test_a_page_for_an_action_still_asks_visitors_to_log_in(client):
    r = await client.get("/can-invite")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
