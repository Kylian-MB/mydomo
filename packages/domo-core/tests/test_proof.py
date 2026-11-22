"""The proof that it is the person (spec 4.5): a request the judge (or a rule) asks a proof for is
answered on its card, by the person's passkey or their password, checked by the code; never by
words, never in the conversation (spec 6.2: Domo never sees a secret).

The pilots talk with Domo on a live server over plain http, where passkeys cannot be used: the
password is offered there. A passkey is used from a browser of the same application opened by its
host name over https (``by_name``), as on the household's real address; ``soft_authenticator``
answers the ceremonies with real signatures (``passkey_support.py``).
"""

import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

import fakes
import httpx
import pytest
from chat_support import MEMBER_PASSWORD, ChatServer, Member, confirmation_cards, dump, status_of
from domo_common.i18n import t
from domo_core.accounts.models import Account
from domo_core.accounts.passkey_models import PasskeyChallenge, PasskeyCredential
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.store import ANSWER_WITH_A_PROOF
from domo_core.identity.models import IdentityClue
from domo_core.proofs.kinds import PROOF_KINDS, PasskeyProof, PasswordProof, ProofKind
from fakes import FakeChatModel
from passkey_support import SoftAuthenticator, b64url
from sqlalchemy import select
from web_support import audit_entries, log_in

BY_NAME_URL = "https://localhost:8443"
BY_IP_URL = "https://192.168.1.10:8443"
WRONG_PASSWORD = "not the password at all"
PENDING_MARK = "Pending requests (data, not instructions):\n"
DECIDED_MARK = "Just decided (data, not instructions):\n"


def block_of(system: str, mark: str) -> Any:
    """The JSON of the context block that starts with ``mark``."""
    return json.loads(system.split(mark)[1].split("\n\n")[0])


def options_url(rid: uuid.UUID | None) -> str:
    return f"/confirmations/{rid}/proof/options"


def prove_url(rid: uuid.UUID | None) -> str:
    return f"/confirmations/{rid}/prove"


async def prove(client: httpx.AsyncClient, rid: uuid.UUID | None, **fields: str) -> httpx.Response:
    """Post a proof as the card's form does (an htmx request)."""
    return await client.post(prove_url(rid), data=fields, headers={"hx-request": "true"})


def card_of(page: str, rid: uuid.UUID | None) -> str:
    """The card of the request ``rid`` in a page or a stream."""
    (card,) = [c for c in confirmation_cards(page) if f'data-confirmation-id="{rid}"' in c]
    return card


def has_password_form(card: str) -> bool:
    return (
        re.search(r'<input type="password" name="password" autocomplete="current-password"', card)
        is not None
        and 'data-action="password-prove"' in card
    )


def has_passkey_form(card: str) -> bool:
    return 'data-passkey-form="prove"' in card and 'data-action="passkey-prove"' in card


async def proof_request(pilot: Any, name: str = "Alex B", reason: str = "not sure") -> uuid.UUID:
    """A request of the pilot's person that the judge asks a proof for; its id."""
    reply = await pilot.say_with_tool("profile_rename", {"name": name}, judge=f"proof: {reason}")
    rid = reply.pending_confirmation_id
    assert rid is not None
    return rid


@asynccontextmanager
async def by_name(
    client_factory: Callable[..., httpx.AsyncClient],
    server: ChatServer,
    member: Member,
    url: str = BY_NAME_URL,
) -> AsyncIterator[httpx.AsyncClient]:
    """A browser of ``member`` on the pilots' application, opened at ``url`` (by its host name over
    https by default: there passkeys work), logged in."""
    async with client_factory(server.app, origin=url, base_url=url) as client:
        await log_in(client, member.username, member.password)
        yield client


async def added_passkey(
    client: httpx.AsyncClient,
    soft_authenticator: SoftAuthenticator,
    server: ChatServer,
    member: Member,
) -> PasskeyCredential:
    """A passkey added from the account page, as ``passkeys.js`` does it; the stored passkey."""
    r = await client.post("/account/passkeys/options", data={"current": member.password})
    assert r.status_code == 200, r.text
    payload = soft_authenticator.create(r.json())
    r = await client.post(
        "/account/passkeys", data={"label": "phone", "credential": json.dumps(payload)}
    )
    assert r.status_code == 303, r.text
    async with server.session_factory() as reader:
        found = await reader.execute(
            select(PasskeyCredential)
            .join(Account, Account.id == PasskeyCredential.account_id)
            .where(Account.person_id == member.person.id)
        )
        return found.scalar_one()


async def account_id_of(server: ChatServer, member: Member) -> uuid.UUID:
    async with server.session_factory() as reader:
        found = await reader.execute(
            select(Account.id).where(Account.person_id == member.person.id)
        )
        return found.scalar_one()


async def proof_challenges(server: ChatServer) -> list[PasskeyChallenge]:
    async with server.session_factory() as reader:
        found = await reader.execute(
            select(PasskeyChallenge).where(PasskeyChallenge.purpose == "proof")
        )
        return list(found.scalars())


async def entries(server: ChatServer, action: str) -> list[Any]:
    async with server.session_factory() as reader:
        return [entry for entry in await audit_entries(reader) if entry.action == action]


async def decided_via(server: ChatServer, rid: uuid.UUID) -> str | None:
    async with server.session_factory() as reader:
        found = await reader.execute(select(Confirmation.decided_via).where(Confirmation.id == rid))
        return found.scalar_one()


@pytest.fixture
async def adult_by_name(
    client_factory: Callable[..., httpx.AsyncClient], chat_server: ChatServer, adult_member: Member
) -> AsyncIterator[httpx.AsyncClient]:
    """Alex's browser on the household's address (passkeys work there), logged in."""
    async with by_name(client_factory, chat_server, adult_member) as client:
        yield client


async def clues_kept(server: ChatServer) -> list[tuple[str, str, str | None]]:
    async with server.session_factory() as reader:
        found = await reader.execute(
            select(IdentityClue.kind, IdentityClue.channel, IdentityClue.means)
        )
        return [tuple(row) for row in found.all()]


async def test_a_proof_that_holds_is_kept_as_a_clue_and_the_judge_reads_it(adult_chat, chat_server):
    rid = await proof_request(adult_chat)
    await prove(adult_chat.client, rid, means="password", password=WRONG_PASSWORD)
    assert await clues_kept(chat_server) == []
    adult_chat.use_model(FakeChatModel(["C'est fait."]))
    await prove(adult_chat.client, rid, means="password", password=MEMBER_PASSWORD)
    assert await clues_kept(chat_server) == [("proof", "page", "password")]
    await adult_chat.say_with_tool("profile_rename", {"name": "Alex C"}, judge="do")
    [judged] = [call for call in chat_server.calls if fakes.is_judge_call(call.system)][-1:]
    clues = json.loads(judged.messages[0].content)["speaker"]["clues"]
    assert [(clue["kind"], clue["strength"], clue["source"]) for clue in clues] == [
        ("session", "strong", "code"),
        ("proof", "strong", "code"),
    ]


# --- the password -------------------------------------------------------------------------------


async def test_a_proof_by_password_runs_the_action_and_a_wrong_one_does_nothing(
    adult_chat, chat_server, display_name_of, confirmation_status
):
    rid = await proof_request(adult_chat)
    wrong = await prove(adult_chat.client, rid, means="password", password=WRONG_PASSWORD)
    assert wrong.status_code == 200
    assert status_of(wrong) == "proof_failed"
    assert t("confirmation.proof.failed", "en") in wrong.text
    # Nothing was done, and the card still offers the proof: the person may try again.
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(adult_chat.person) == "Alex"
    assert has_password_form(card_of(wrong.text, rid))
    [failed] = await entries(chat_server, "confirmation.proof_failed")
    assert failed.details == {"confirmation_id": str(rid), "means": "password"}
    assert failed.actor == f"person:{adult_chat.person.id}"

    adult_chat.use_model(FakeChatModel(["C'est fait."]))
    right = await prove(adult_chat.client, rid, means="password", password=MEMBER_PASSWORD)
    assert status_of(right) == "executed"
    assert await display_name_of(adult_chat.person) == "Alex B"
    assert await decided_via(chat_server, rid) == "proof"
    [decided] = await entries(chat_server, "confirmation.decided")
    assert decided.details["means"] == "password"
    assert (decided.details["via"], decided.details["outcome"]) == ("proof", "executed")
    # No separate entry for the proof that worked: the decision says how it was given.
    assert await entries(chat_server, "confirmation.proved") == []
    # A proof wakes Domo as a button does, and he is told it was a proof.
    await chat_server.wait_for_wakes()
    decided_told = block_of(chat_server.calls[-1].system, DECIDED_MARK)
    assert (decided_told["outcome"], decided_told["via"]) == ("executed", "proof")
    # Domo never sees the password, nor any model call: the secret stays on the card.
    for call in chat_server.calls:
        assert MEMBER_PASSWORD not in call.system
        assert MEMBER_PASSWORD not in json.dumps([dump(item) for item in call.messages])
        assert WRONG_PASSWORD not in call.system
    for entry in await entries(chat_server, "confirmation.decided") + [failed]:
        assert MEMBER_PASSWORD not in json.dumps(entry.details)


async def test_wrong_passwords_for_a_proof_are_held_back_like_logins(
    adult_chat, display_name_of, confirmation_status
):
    rid = await proof_request(adult_chat)
    for _ in range(5):
        r = await prove(adult_chat.client, rid, means="password", password=WRONG_PASSWORD)
        assert status_of(r) == "proof_failed"
    held = await prove(adult_chat.client, rid, means="password", password=MEMBER_PASSWORD)
    assert held.status_code == 429
    assert int(held.headers["retry-after"]) > 0
    # Said in the notice area of the page.
    assert held.headers["hx-retarget"] == "#chat-notice"
    assert t("account.password.error.throttled", "en", minutes="15") in held.text
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(adult_chat.person) == "Alex"


# --- the passkey --------------------------------------------------------------------------------


async def test_a_proof_by_passkey_runs_the_action(
    adult_chat, adult_member, adult_by_name, soft_authenticator, chat_server, display_name_of
):
    passkey = await added_passkey(adult_by_name, soft_authenticator, chat_server, adult_member)
    rid = await proof_request(adult_chat)
    r = await adult_by_name.post(options_url(rid))
    assert r.status_code == 200, r.text
    options = r.json()
    # The passkeys of this account, and only them; the person is verified (a touch is not enough).
    assert [item["id"] for item in options["allowCredentials"]] == [b64url(passkey.credential_id)]
    assert (options["rpId"], options["userVerification"]) == ("localhost", "required")
    assertion = soft_authenticator.get(options, passkey.credential_id)
    done = await prove(adult_by_name, rid, means="passkey", credential=json.dumps(assertion))
    assert status_of(done) == "executed"
    assert await display_name_of(adult_chat.person) == "Alex B"
    [challenge] = await proof_challenges(chat_server)
    assert challenge.used_at is not None
    assert challenge.account_id == await account_id_of(chat_server, adult_member)
    [decided] = await entries(chat_server, "confirmation.decided")
    assert (decided.details["via"], decided.details["means"]) == ("proof", "passkey")
    async with chat_server.session_factory() as reader:
        used = await reader.get(PasskeyCredential, passkey.id)
        assert used is not None and used.last_used_at is not None
    await chat_server.wait_for_wakes()


async def test_a_proof_given_twice_runs_the_action_once(  # Review Focus 3
    adult_chat, adult_member, adult_by_name, soft_authenticator, chat_server, display_name_of
):
    passkey = await added_passkey(adult_by_name, soft_authenticator, chat_server, adult_member)
    rid = await proof_request(adult_chat)
    options = (await adult_by_name.post(options_url(rid))).json()
    answer = json.dumps(soft_authenticator.get(options, passkey.credential_id))
    first = await prove(adult_by_name, rid, means="passkey", credential=answer)
    # The second tab: the request is read before the means, so it is already answered.
    second = await prove(adult_by_name, rid, means="passkey", credential=answer)
    assert (status_of(first), status_of(second)) == ("executed", "already_decided")
    assert await display_name_of(adult_chat.person) == "Alex B"
    decided = await entries(chat_server, "confirmation.decided")
    assert [entry.details["outcome"] for entry in decided] == ["executed"]
    [challenge] = await proof_challenges(chat_server)
    assert challenge.used_at is not None
    await chat_server.wait_for_wakes()


async def test_a_passkey_of_someone_else_or_another_ceremony_proves_nothing(
    adult_chat,
    adult_member,
    owner_member,
    adult_by_name,
    client_factory,
    soft_authenticator,
    chat_server,
    display_name_of,
    confirmation_status,
):
    own = await added_passkey(adult_by_name, soft_authenticator, chat_server, adult_member)
    async with by_name(client_factory, chat_server, owner_member) as owner_by_name:
        others = await added_passkey(owner_by_name, soft_authenticator, chat_server, owner_member)
        rid = await proof_request(adult_chat)
        # Only the one who may answer gets the options of its proof.
        assert (await owner_by_name.post(options_url(rid))).status_code == 404
    # The assertion of a login ceremony is not a proof.
    login = (await adult_by_name.post("/login/passkey/options")).json()
    r = await prove(
        adult_by_name,
        rid,
        means="passkey",
        credential=json.dumps(soft_authenticator.get(login, own.credential_id)),
    )
    assert status_of(r) == "proof_failed"
    # The passkey of someone else, on Alex's own proof, neither.
    options = (await adult_by_name.post(options_url(rid))).json()
    r = await prove(
        adult_by_name,
        rid,
        means="passkey",
        credential=json.dumps(soft_authenticator.get(options, others.credential_id)),
    )
    assert status_of(r) == "proof_failed"
    # Nor what is not an answer of a browser at all.
    r = await prove(adult_by_name, rid, means="passkey", credential="[1, 2]")
    assert status_of(r) == "proof_failed"
    assert await confirmation_status(rid) == "pending"
    assert await display_name_of(adult_chat.person) == "Alex"
    failures = await entries(chat_server, "confirmation.proof_failed")
    assert [entry.details["means"] for entry in failures] == ["passkey"] * 3


async def test_the_card_offers_the_passkey_only_where_passkeys_work_and_the_password_always(
    adult_chat, adult_member, adult_by_name, client_factory, soft_authenticator, chat_server
):
    rid = await proof_request(adult_chat)
    # On the household's address, without a passkey: the password only.
    page = (await adult_by_name.get("/chat")).text
    card = card_of(page, rid)
    assert has_password_form(card) and not has_passkey_form(card)
    assert (await adult_by_name.post(options_url(rid))).status_code == 404
    # With a passkey: both, the passkey first; the page has the script that speaks to it.
    await added_passkey(adult_by_name, soft_authenticator, chat_server, adult_member)
    page = (await adult_by_name.get("/chat")).text
    card = card_of(page, rid)
    assert has_passkey_form(card) and has_password_form(card)
    assert card.index('data-action="passkey-prove"') < card.index('data-action="password-prove"')
    assert f'data-options-url="{options_url(rid)}"' in card
    assert f'hx-post="{prove_url(rid)}"' in card
    assert re.search(r'<button [^>]*data-action="passkey-prove"[^>]*data-passkey-only', card)
    assert "/static/passkeys.js" in page
    assert 'data-action="confirm"' not in card and 'data-action="refuse"' in card
    # On an IP address, where browsers refuse passkeys: the password only.
    async with by_name(client_factory, chat_server, adult_member, BY_IP_URL) as by_ip:
        card = card_of((await by_ip.get("/chat")).text, rid)
        assert has_password_form(card) and not has_passkey_form(card)
        assert (await by_ip.post(options_url(rid))).status_code == 404


# --- where the proof is offered -----------------------------------------------------------------


async def test_the_proof_form_is_on_the_reply_card_and_in_to_approve(  # F12
    adult_chat, live_owner_client, grant, confirmation_ask
):
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"}, judge="proof: x")
    rid = reply.pending_confirmation_id
    card = card_of(reply.cards_html, rid)
    assert has_password_form(card) and 'data-action="confirm"' not in card
    # Plain http: no passkey here.
    assert not has_passkey_form(card)
    assert t("confirmation.proof.title", "en") in card
    # The chat page, read again, shows the same form.
    assert has_password_form(card_of((await adult_chat.get("/chat")).text, rid))
    # A request of Alex's that waits for the owner, who must prove it is him.
    await grant("profile.rename", person=adult_chat.person, approver="owner")
    waiting = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex C"}, judge="proof: unusual"
    )
    wid = waiting.pending_confirmation_id
    assert (await confirmation_ask(wid))[0] == "proof"
    # Alex cannot answer it: no form on Alex's card.
    assert not has_password_form(card_of(waiting.cards_html, wid))
    zone = (await live_owner_client.get("/approvals")).text
    assert has_password_form(card_of(zone, wid))


async def test_the_zone_that_reloads_itself_waits_while_a_proof_is_given_in_it(
    adult_chat, live_owner_client, grant
):
    """The approvals zone asks for itself every 15 seconds; the proof form it holds must not be
    replaced while someone uses it (a password typed, the focus in it, a passkey's ceremony or
    the proof's own post under way). The page runs htmx without eval, so no trigger filter can
    hold it: ``passkeys.js`` cancels the zone's request instead. What a browser does with it is
    the end-to-end test's; here, what the page and the script declare."""
    await grant("profile.rename", person=adult_chat.person, approver="owner")
    waiting = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"}, judge="proof: x")
    page = (await live_owner_client.get("/chat")).text
    zone = re.search(r'<div id="approvals"[^>]*>', page)
    assert zone is not None and 'hx-trigger="load, every 15s"' in zone.group(0)
    assert "/static/passkeys.js" in page
    # The forms the script watches are the ones the zone's card holds.
    zone_page = (await live_owner_client.get("/approvals")).text
    card = card_of(zone_page, waiting.pending_confirmation_id)
    assert re.search(r'<div class="confirmation-proof"[^>]*>\s*<p[^>]*>[^<]*</p>\s*<form', card)
    script = (
        resources.files("domo_core").joinpath("web", "static", "passkeys.js").read_text("utf-8")
    )
    watch_at = script.index('addEventListener("htmx:beforeRequest"')
    assert "event.preventDefault()" in script[watch_at:].split("});")[0]
    in_use = script[script.index("function proofInUse") : watch_at]
    for sign in (
        '".confirmation-proof form"',
        '"data-passkey-running"',
        '"htmx-request"',
        "document.activeElement",
        "password.value",
    ):
        assert sign in in_use, sign


async def test_a_proof_is_asked_of_whoever_may_decide(
    adult_chat, owner_chat, grant, display_name_of, confirmation_status
):
    await grant("profile.rename", person=adult_chat.person, approver="owner")
    reply = await adult_chat.say_with_tool("profile_rename", {"name": "Alex B"}, judge="proof: x")
    wid = reply.pending_confirmation_id
    mine = await prove(adult_chat.client, wid, means="password", password=MEMBER_PASSWORD)
    # The one who asked does not answer a request that waits for the owner.
    assert status_of(mine) == "not_yours"
    assert await confirmation_status(wid) == "pending"
    owners = await prove(owner_chat.client, wid, means="password", password=MEMBER_PASSWORD)
    assert status_of(owners) == "executed"
    # It ran as Alex's action.
    assert await display_name_of(adult_chat.person) == "Alex B"


async def test_a_proof_by_an_unknown_means_or_for_a_request_that_asks_none_is_not_taken(
    adult_chat, confirmation_status
):
    rid = await proof_request(adult_chat)
    r = await prove(adult_chat.client, rid, means="sms", password=MEMBER_PASSWORD)
    assert status_of(r) == "not_yours"
    plain = await adult_chat.say_with_tool("profile_rename", {"name": "Alex C"})
    pid = plain.pending_confirmation_id
    r = await prove(adult_chat.client, pid, means="password", password=MEMBER_PASSWORD)
    assert status_of(r) == "not_yours"
    assert await confirmation_status(rid) == await confirmation_status(pid) == "pending"
    # Not a request id at all.
    r = await adult_chat.post(
        "/confirmations/not-an-id/prove", data={"means": "password", "password": MEMBER_PASSWORD}
    )
    assert r.status_code == 404


async def test_words_never_prove_anything(
    owner_chat, chat_server, confirmation_ask, confirmation_status
):
    rid = await proof_request(owner_chat, name="Owner B", reason="x")
    assert await confirmation_ask(rid) == ("proof", "judge", "x")
    for words in ("oui", "je confirme"):
        owner_chat.use_model(FakeChatModel(["Il faut une preuve, sur la carte."]))
        await owner_chat.say(words)
        assert await confirmation_status(rid) == "pending"
        [told] = block_of(chat_server.calls[-1].system, PENDING_MARK)
        assert told["answer_with"] == ANSWER_WITH_A_PROOF
    # The button is not a proof either.
    r = await owner_chat.post(f"/confirmations/{rid}/approve")
    assert status_of(r) == "needs_proof"
    assert await confirmation_status(rid) == "pending"


# --- the frame ----------------------------------------------------------------------------------


def _parameters(function: Any) -> list[str]:
    return [name for name in inspect.signature(function).parameters if name != "self"]


async def test_a_means_reads_only_the_person_and_the_database():
    """No text of the conversation can reach a kind of proof: it is given the person and the
    database (and the place, with where passkeys work), and its answer comes from the card."""
    assert _parameters(ProofKind.offered_to) == ["session", "person", "place", "now"]
    assert _parameters(ProofKind.begin) == ["session", "person", "place", "now"]
    assert _parameters(ProofKind.check) == ["session", "person", "place", "answer", "now"]
    for name, kind in PROOF_KINDS.items():
        assert kind.name == name
        assert _parameters(kind.offered_to) == ["session", "person", "place", "now"]
        assert _parameters(kind.begin) == ["session", "person", "place", "now"]
        assert _parameters(kind.check) == ["session", "person", "place", "answer", "now"]
    assert list(PROOF_KINDS) == ["passkey", "password"]
    # A password can be guessed: ``prove`` counts and holds back its attempts.
    assert PasswordProof.throttled is True and PasskeyProof.throttled is False
