"""The means of proof each person has agreed (spec 6.4): rows of the database, each pointing to a
kind the code knows how to verify, asked for in a place that says what the channel can carry.

The two proofs of before (the password, the passkey) are the first two kinds. Nothing changes for
a person on the page: a new account has its password, a first passkey adds the passkey, the last
one taken away removes it, and a means that is not agreed (or no longer) is refused exactly as a
means that is not offered.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from chat_support import MEMBER_PASSWORD, status_of
from domo_core.accounts.models import Account
from domo_core.accounts.passkey_models import PasskeyCredential
from domo_core.accounts.passkeys import PasskeySite
from domo_core.conversation.channel import Abilities
from domo_core.proofs.kinds import PROOF_KINDS, ProofPlace
from domo_core.proofs.models import ProofMean
from domo_core.proofs.service import agree, means_of, offers, remove
from sqlalchemy import select
from test_proof import (
    WRONG_PASSWORD,
    added_passkey,
    by_name,
    proof_request,
    prove,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
SITE = PasskeySite("localhost", "https://localhost:8443")


@pytest.fixture
async def adult_by_name(client_factory, chat_server, adult_member):
    """Alex's browser on the household's address (passkeys work there), logged in."""
    async with by_name(client_factory, chat_server, adult_member) as client:
        yield client


def abilities(*kinds: str) -> Abilities:
    return Abilities(
        buttons=False,
        shows_once=False,
        safe_screen=False,
        hears_unasked=False,
        proof_kinds=frozenset(kinds),
    )


async def kinds_of(session, person) -> list[str]:
    return sorted(mean.kind for mean in await means_of(session, person, NOW))


async def add_passkey_row(session, account: Account) -> None:
    session.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id=uuid.uuid4().bytes,
            public_key=b"key",
            sign_count=0,
            transports=[],
            label="phone",
        )
    )
    await session.flush()


# --- the rows -----------------------------------------------------------------------------------


def test_the_kinds_the_code_knows_are_the_two_proofs_of_before():
    assert list(PROOF_KINDS) == ["passkey", "password"]
    assert PROOF_KINDS["password"].throttled is True
    assert PROOF_KINDS["passkey"].throttled is False


async def test_a_new_account_has_the_password_means(session, add_person, add_account):
    person = await add_person("Alex", "adult")
    assert await kinds_of(session, person) == []
    await add_account(person, "alex")
    [mean] = await means_of(session, person, NOW)
    assert (mean.kind, mean.proposed_by, mean.removed_at) == ("password", "code", None)


async def test_agreeing_twice_keeps_one_row_and_removing_then_agreeing_again_makes_a_new_one(
    session, add_person
):
    person = await add_person("Alex", "adult")
    await agree(session, person.id, "password", proposed_by="code", now=NOW)
    await agree(session, person.id, "password", proposed_by="person", now=LATER)
    [mean] = await means_of(session, person, LATER)
    assert (mean.proposed_by, mean.agreed_at) == ("code", NOW)

    await remove(session, person.id, "password", LATER)
    assert await kinds_of(session, person) == []
    # Removing what is not agreed is nothing.
    await remove(session, person.id, "password", LATER)
    await agree(session, person.id, "password", proposed_by="person", now=LATER)
    assert await kinds_of(session, person) == ["password"]
    found = await session.execute(
        select(ProofMean).order_by(ProofMean.agreed_at).execution_options(populate_existing=True)
    )
    rows = found.scalars().all()
    assert [(row.proposed_by, row.removed_at) for row in rows] == [
        ("code", LATER),
        ("person", None),
    ]


async def test_a_kind_the_code_does_not_know_is_not_agreed(session, add_person):
    person = await add_person("Alex", "adult")
    with pytest.raises(ValueError, match="unknown"):
        await agree(session, person.id, "telepathy", proposed_by="person", now=NOW)
    with pytest.raises(ValueError, match="unknown"):
        await agree(session, person.id, "password", proposed_by="nobody", now=NOW)
    assert await kinds_of(session, person) == []


async def test_the_means_of_a_person_are_not_those_of_another(session, add_person, add_account):
    alex = await add_person("Alex", "adult")
    sam = await add_person("Sam", "adult")
    await add_account(alex, "alex")
    await add_account(sam, "sam")
    await remove(session, sam.id, "password", NOW)
    assert await kinds_of(session, alex) == ["password"]
    assert await kinds_of(session, sam) == []


# --- what a place offers ------------------------------------------------------------------------


async def test_outside_the_page_the_password_is_offered_and_not_the_passkey(
    session, add_person, add_account
):
    person = await add_person("Alex", "adult")
    account = await add_account(person, "alex")
    await add_passkey_row(session, account)
    await agree(session, person.id, "passkey", proposed_by="person", now=NOW)
    # No site for passkeys, and a place that only carries the password.
    place = ProofPlace(abilities("password"))
    assert await offers(session, person, place, NOW) == ("password",)


async def test_a_kind_the_place_does_not_hold_is_never_offered(session, add_person, add_account):
    person = await add_person("Alex", "adult")
    account = await add_account(person, "alex")
    await add_passkey_row(session, account)
    await agree(session, person.id, "passkey", proposed_by="person", now=NOW)
    # The site for passkeys is there, but the place does not list the kind.
    assert await offers(session, person, ProofPlace(abilities("password"), SITE), NOW) == (
        "password",
    )
    assert await offers(session, person, ProofPlace(abilities(), SITE), NOW) == ()
    assert await offers(session, person, ProofPlace(abilities("passkey"), None), NOW) == ()
    both = ProofPlace(abilities("passkey", "password"), SITE)
    assert await offers(session, person, both, NOW) == ("passkey", "password")


async def test_a_means_not_agreed_or_removed_is_not_offered(session, add_person, add_account):
    person = await add_person("Alex", "adult")
    account = await add_account(person, "alex")
    # A passkey that exists, but that nobody agreed as a means.
    await add_passkey_row(session, account)
    place = ProofPlace(abilities("passkey", "password"), SITE)
    assert await offers(session, person, place, NOW) == ("password",)
    await agree(session, person.id, "passkey", proposed_by="person", now=NOW)
    assert await offers(session, person, place, NOW) == ("passkey", "password")
    await remove(session, person.id, "password", LATER)
    assert await offers(session, person, place, LATER) == ("passkey",)


# --- the passkey comes and goes with the person's passkeys --------------------------------------


async def means_in_database(chat_server, member) -> list[str]:
    async with chat_server.session_factory() as reader:
        return sorted(
            kind
            for kind in (
                await reader.execute(
                    select(ProofMean.kind).where(
                        ProofMean.person_id == member.person.id, ProofMean.removed_at.is_(None)
                    )
                )
            ).scalars()
        )


async def test_the_first_passkey_adds_the_means_and_the_last_one_removed_takes_it_away(
    adult_by_name,
    adult_member,
    soft_authenticator,
    chat_server,  # noqa: F811
):
    assert await means_in_database(chat_server, adult_member) == ["password"]
    passkey = await added_passkey(adult_by_name, soft_authenticator, chat_server, adult_member)
    assert await means_in_database(chat_server, adult_member) == ["passkey", "password"]

    # A second one changes nothing; taking one of two away keeps the means.
    options = await adult_by_name.post(
        "/account/passkeys/options", data={"current": adult_member.password}
    )
    payload = soft_authenticator.create(options.json())
    added = await adult_by_name.post(
        "/account/passkeys", data={"label": "laptop", "credential": json.dumps(payload)}
    )
    assert added.status_code == 303
    assert await means_in_database(chat_server, adult_member) == ["passkey", "password"]
    gone = await adult_by_name.post(f"/account/passkeys/{passkey.id}/delete")
    assert gone.status_code == 303
    assert await means_in_database(chat_server, adult_member) == ["passkey", "password"]

    async with chat_server.session_factory() as reader:
        [last] = (await reader.execute(select(PasskeyCredential))).scalars().all()
    gone = await adult_by_name.post(f"/account/passkeys/{last.id}/delete")
    assert gone.status_code == 303
    assert await means_in_database(chat_server, adult_member) == ["password"]


# --- proving ------------------------------------------------------------------------------------


async def test_a_proof_by_a_removed_means_is_refused_like_a_means_that_is_not_offered(
    adult_chat, adult_member, chat_server, confirmation_status, display_name_of
):
    rid = await proof_request(adult_chat)
    async with chat_server.session_factory() as writer:
        await remove(writer, adult_member.person.id, "password", NOW)
        await writer.commit()
    refused = await prove(adult_chat.client, rid, means="password", password=MEMBER_PASSWORD)
    assert status_of(refused) == "not_yours"
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(adult_chat.person) == "Alex"


async def test_a_wrong_password_counts_in_the_throttle_of_the_account_page(
    adult_chat,
    adult_member,
    adult_by_name,
    confirmation_status,
):
    rid = await proof_request(adult_chat)
    for _ in range(3):
        r = await prove(adult_chat.client, rid, means="password", password=WRONG_PASSWORD)
        assert status_of(r) == "proof_failed"
    # Two wrong passwords asked before a new passkey are counted with them (one count per account).
    for _ in range(2):
        r = await adult_by_name.post("/account/passkeys/options", data={"current": WRONG_PASSWORD})
        assert r.status_code == 400
    held = await prove(adult_chat.client, rid, means="password", password=MEMBER_PASSWORD)
    assert held.status_code == 429
    assert await confirmation_status(rid) == "pending"
