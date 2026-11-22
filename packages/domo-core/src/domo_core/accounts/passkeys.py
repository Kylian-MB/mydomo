"""Passkeys (WebAuthn, with py_webauthn): adding one to an account, and logging in with it.

Where they work (D5)
--------------------
A browser only uses a passkey on a page opened by a host name (``localhost``, ``domo.local``, a
DNS name): WebAuthn refuses IP addresses. The relying party id (the RP id) is the host name of the
request, without its port (``rp_id_for``), and the origin the browser signs is ``https://``, that
name and the page's port. ``passkey_availability`` only gives them when:

* the request came over HTTPS. The edge serves the page over HTTPS and says so to the core in
  ``X-Forwarded-Proto``, which the web server believes from the edge only
  (``DOMO_TRUSTED_PROXIES``): the request's scheme is what that header said;
* the host name is one of the site addresses (``DOMO_SITE_ADDRESSES``, the edge's own list, see
  ``site_names``). The edge serves no other name, and the core does not take a ``Host`` header's
  word for one;
* the request names the page's port (``DOMO_HTTPS_PORT``, the edge's, which it publishes the
  same inside and outside; none when it is 443). The origin is built from that setting, never
  from the request: the edge's site matching ignores the port, so a ``Host`` (and an ``Origin``)
  naming another port would otherwise make the core expect an assertion made on another service
  of the same name.

Elsewhere the pages say why (``unavailable``: an IP address, or another address) and the
password is there as always.

The ceremonies
--------------
Each one starts with options that carry a challenge: random bytes (64, py_webauthn's own) that the
core keeps in ``webauthn_challenges`` for five minutes (``CHALLENGE_LIFETIME``), good for one
answer. An answer is matched to its challenge by the challenge it signed, and the challenge is
spent (``used_at``) *before* the answer is checked: a wrong answer spends it too, and the same
answer cannot be played twice, not even by two requests at once (the update is conditional). A
registration challenge belongs to its account; a login challenge to nobody (the passkey says whose
it is). The challenges that ended (spent or expired) are deleted whenever new options are made.

* Adding a passkey (``registration_options``, ``finish_registration``): a resident
  (discoverable) key is required, so that logging in needs no username; user verification
  (fingerprint, face, PIN) is required too, so that a passkey that is kept can always sign in;
  no attestation is asked for. The user
  handle is the account's id (16 bytes, nothing personal). The passkeys the account already has
  are left out of the options; an account has at most ``MAX_PASSKEYS_PER_ACCOUNT``, and a newer
  request of an account replaces its older one. The label (the name the person gives the passkey)
  is one plain line of 1 to ``MAX_PASSKEY_LABEL_LENGTH`` characters (``text.is_plain_line``). A
  credential id is ``MAX_CREDENTIAL_ID_LENGTH`` bytes at most (WebAuthn's own limit, which
  py_webauthn does not check). A credential belongs to exactly one account: one whose id is
  already stored, on this account or another, is refused.
* Logging in (``login_options``, ``finish_login``): the passkey is found by the credential id the
  browser sends, and the user handle, when there is one, must be its account's id. py_webauthn
  checks the type, the challenge, the origin, the RP id, the presence and the verification of the
  user (a passkey stands in for the password: a key that was only touched, with no PIN or
  fingerprint, does not sign in), the signature and the counter: the counter must go up, unless
  it is 0 before and after (an authenticator that does not count, as synced passkeys do); a
  counter that goes back is the sign of a copied key.
  The stored counter and ``last_used_at`` are then updated, under a lock of the passkey's row.
  Someone who is no longer in the household (removed, or a guest whose stay is over, at ``now``:
  ``accounts.sessions.active_at``) is refused like any failed login (R77). Anyone can ask for
  login options: at most ``MAX_PENDING_LOGIN_CHALLENGES`` of them wait at once (counted under an
  advisory lock, so that requests at the same moment cannot overshoot it; reaching it is logged).
  The routes also hold back one address that asks too often (``accounts.passkey_routes``).
* Proving it is the person (``proof_options``, ``finish_proof``), for a request that asks a proof
  (``proofs.service``, spec 4.5): as a login, but the challenge belongs to the account of the
  person who proves it (``proof``), the options name that account's passkeys (an account with
  none gets ``no_passkey``), and the passkey that answers must be one of them: a login's answer,
  a registration's, another account's passkey or another account's challenge prove nothing.
  The user is verified (fingerprint, face, PIN), as at a login. The counter and ``last_used_at``
  are updated the same way. Who may prove what is the caller's to check (the request's
  ``may_decide``).

Every refusal raises ``PasskeyRejected``. Its ``reason`` is for the code, the logs and the tests,
never for the person: the pages give one answer for all of them.

None of these functions commits: the caller owns the transaction (and writes the audit entry last,
just before committing). A refused answer has spent its challenge: the caller commits that too.
"""

import logging
import re
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    generate_challenge,
    options_to_json_dict,
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    CollectedClientData,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from domo_core.accounts.models import Account, Person
from domo_core.accounts.passkey_models import PasskeyChallenge, PasskeyCredential
from domo_core.accounts.sessions import active_at
from domo_core.config import CoreSettings
from domo_core.llm.types import JsonObject
from domo_core.text import is_plain_line

if TYPE_CHECKING:
    # Only for the type of ``passkey_availability_of``: the rest of the module is read by code that
    # knows no web (the conversation).
    from starlette.requests import Request

CHALLENGE_LIFETIME: Final = timedelta(minutes=5)
# The name of the relying party, which an authenticator may show next to the passkey.
RP_NAME: Final = "Domo"
MAX_PASSKEY_LABEL_LENGTH: Final = 60
# WebAuthn's limit on a credential id (registration: "credentialId" is 1023 bytes at most).
MAX_CREDENTIAL_ID_LENGTH: Final = 1023
MAX_PASSKEYS_PER_ACCOUNT: Final = 20
# How many login challenges may wait at once: anyone can ask for one. A household signs in a few
# times a day; past this, passkey logins wait (the password still works).
MAX_PENDING_LOGIN_CHALLENGES = 100
# How long the browser waits for the person, as a hint: no longer than the challenge lives.
_TIMEOUT_MS: Final = int(CHALLENGE_LIFETIME / timedelta(milliseconds=1))
# The key of the advisory lock that serialises the counting of the login challenges that wait
# (next to the audit log's 4242 and the display names' 4343).
LOGIN_CHALLENGES_LOCK_KEY: Final = 4444

logger = logging.getLogger(__name__)

RejectReason = Literal[
    "malformed",
    "label",
    "challenge",
    "verification",
    "unknown_credential",
    "not_in_household",
    "taken",
    "too_many",
    "busy",
    "no_passkey",
]


class PasskeyRejected(Exception):
    """A passkey ceremony was refused. ``reason`` says why, for the code and the logs:

    * ``malformed``: the answer is not what a browser sends;
    * ``label``: the name given to a new passkey is not one plain line of 1 to 60 characters;
    * ``challenge``: no challenge waits for this answer (unknown, spent, expired, of another
      account or of the other ceremony);
    * ``verification``: the answer does not check out (origin, RP id, signature, counter, user
      handle, a cross-origin frame...);
    * ``unknown_credential``: no passkey has this credential id;
    * ``not_in_household``: the passkey's person was removed, or their stay is over;
    * ``taken``: a passkey with this credential id is already stored;
    * ``too_many``: the account has ``MAX_PASSKEYS_PER_ACCOUNT`` passkeys already;
    * ``busy``: ``MAX_PENDING_LOGIN_CHALLENGES`` login challenges wait already;
    * ``no_passkey``: the account has no passkey to prove it is its person with.

    It never carries what the browser sent."""

    def __init__(self, reason: RejectReason) -> None:
        super().__init__(reason)
        self.reason: RejectReason = reason


# --- where passkeys work ----------------------------------------------------------------------

# A host as browsers send it in ``Host``: a name or an IPv4 address, or a bracketed IPv6 address,
# then an optional port. No user information, no path.
_HOST: Final = re.compile(r"(?P<name>[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}))?")
# A last label that browsers read as a number makes the whole host an IPv4 address: "127.1" and
# "0x7f.1" are addresses too, for the URL standard.
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")
# The scheme Caddy accepts in front of a site address (``https://domo.local``).
_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def _split_host(host: str) -> tuple[str, int | None] | None:
    """The name (lower case) and the port of a ``Host`` value; ``None`` when it is not one."""
    match = _HOST.fullmatch(host)
    if match is None:
        return None
    port = match.group("port")
    if port is not None and int(port) > 65535:
        return None
    return match.group("name").lower(), None if port is None else int(port)


def _is_ip_address(name: str) -> bool:
    return name.startswith("[") or _NUMERIC_LABEL.fullmatch(name.rsplit(".", 1)[-1]) is not None


def rp_id_for(host: str) -> str | None:
    """The RP id for a page opened at ``host`` (a ``Host`` value): its name, in lower case and
    without the port. ``None`` for an IPv4 or IPv6 address, which WebAuthn refuses, and for
    anything that is not a valid name (empty, an empty label, a stray character)."""
    parts = _split_host(host)
    if parts is None:
        return None
    name = parts[0]
    if _is_ip_address(name) or any(not label for label in name.split(".")):
        return None
    return name


def site_names(site_addresses: str) -> frozenset[str]:
    """The host names among the site addresses (``DOMO_SITE_ADDRESSES``, separated by commas
    or spaces as the edge reads them): the names passkeys may use. The addresses are left out; a
    scheme or a path in front of or after an entry is ignored, as is a port."""
    names: set[str] = set()
    for entry in re.split(r"[,\s]+", site_addresses):
        authority = _SCHEME.sub("", entry, count=1).split("/", 1)[0]
        name = rp_id_for(authority) if authority else None
        if name is not None:
            names.add(name)
    return frozenset(names)


class PasskeySite(NamedTuple):
    """Where passkeys are used for a request: the RP id, and the origin the browser signs."""

    rp_id: str
    origin: str


class PasskeyAvailability(NamedTuple):
    """Whether a request can use passkeys: ``site`` when it can; otherwise ``unavailable`` says
    why: ``ip`` (the page was opened by an IP address, which WebAuthn refuses) or ``address``
    (not over HTTPS, or a name that is not one of the site addresses)."""

    site: PasskeySite | None
    unavailable: Literal["ip", "address"] | None


def passkey_availability(
    host: str | None, scheme: str, site_addresses: str, https_port: int
) -> PasskeyAvailability:
    """Whether passkeys can be used by a request to ``host`` (its ``Host`` header) over
    ``scheme`` (its scheme, as the trusted edge's ``X-Forwarded-Proto`` set it), given the site
    addresses and the page's port. A ``Host`` without a port names 443. The origin is built from
    ``https_port`` (left out when it is 443, as browsers do), never from the request."""
    parts = _split_host(host) if host else None
    if parts is not None and _is_ip_address(parts[0]):
        return PasskeyAvailability(None, "ip")
    rp_id = rp_id_for(host) if host else None
    if (
        parts is None
        or rp_id is None
        or scheme != "https"
        or rp_id not in site_names(site_addresses)
        or (443 if parts[1] is None else parts[1]) != https_port
    ):
        return PasskeyAvailability(None, "address")
    origin = f"https://{rp_id}" if https_port == 443 else f"https://{rp_id}:{https_port}"
    return PasskeyAvailability(PasskeySite(rp_id, origin), None)


def passkey_availability_of(request: "Request", settings: CoreSettings) -> PasskeyAvailability:
    """``passkey_availability`` for ``request``: its ``Host`` header (exactly one, or there is
    none to speak of) and its scheme, with the site addresses and the port of ``settings``."""
    hosts = request.headers.getlist("host")
    host = hosts[0] if len(hosts) == 1 else None
    return passkey_availability(
        host, request.url.scheme, settings.site_addresses, settings.https_port
    )


# --- challenges ---------------------------------------------------------------------------------


async def _forget_ended_challenges(
    session: AsyncSession, now: datetime, *, replaced_for: uuid.UUID | None = None
) -> None:
    """Delete the challenges that were spent or have expired, and, for ``replaced_for``, that
    account's registration challenge that waits (a new one replaces it)."""
    ended = or_(PasskeyChallenge.expires_at <= now, PasskeyChallenge.used_at.is_not(None))
    if replaced_for is not None:
        ended = or_(
            ended,
            and_(
                PasskeyChallenge.purpose == "register",
                PasskeyChallenge.account_id == replaced_for,
            ),
        )
    await session.execute(delete(PasskeyChallenge).where(ended))


async def _new_challenge(
    session: AsyncSession, purpose: str, account_id: uuid.UUID | None, now: datetime
) -> bytes:
    challenge = generate_challenge()
    session.add(
        PasskeyChallenge(
            purpose=purpose,
            account_id=account_id,
            challenge=challenge,
            expires_at=now + CHALLENGE_LIFETIME,
        )
    )
    await session.flush()
    return challenge


async def _spend_challenge(
    session: AsyncSession,
    purpose: str,
    challenge: bytes,
    account_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Mark the challenge an answer signed as used, or refuse the answer (``challenge``) when no
    such challenge waits: unknown, spent, expired, or of another account or ceremony. The update
    is conditional, so two answers of one challenge cannot both spend it."""
    owner = (
        PasskeyChallenge.account_id.is_(None)
        if account_id is None
        else PasskeyChallenge.account_id == account_id
    )
    spent = await session.execute(
        update(PasskeyChallenge)
        .where(
            PasskeyChallenge.purpose == purpose,
            PasskeyChallenge.challenge == challenge,
            owner,
            PasskeyChallenge.used_at.is_(None),
            PasskeyChallenge.expires_at > now,
        )
        .values(used_at=now)
        .returning(PasskeyChallenge.id)
    )
    if spent.first() is None:
        raise PasskeyRejected("challenge")


def _client_data(raw: bytes) -> CollectedClientData:
    """The client data of an answer. A page framed by another site (``crossOrigin``) is refused:
    the page itself forbids framing, this is the second lock."""
    try:
        client_data = parse_client_data_json(raw)
    except Exception as exc:  # whatever the parser raises for bytes it cannot read
        raise PasskeyRejected("malformed") from exc
    if client_data.cross_origin:
        raise PasskeyRejected("verification")
    return client_data


# --- the passkeys of an account -----------------------------------------------------------------


async def passkeys_of(session: AsyncSession, account_id: uuid.UUID) -> Sequence[PasskeyCredential]:
    """The passkeys of an account, oldest first."""
    result = await session.execute(
        select(PasskeyCredential)
        .where(PasskeyCredential.account_id == account_id)
        .order_by(PasskeyCredential.created_at, PasskeyCredential.id)
    )
    return result.scalars().all()


async def _count_passkeys(session: AsyncSession, account_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(PasskeyCredential)
        .where(PasskeyCredential.account_id == account_id)
    )
    return result.scalar_one()


async def remove_passkey(
    session: AsyncSession, account_id: uuid.UUID, passkey_id: uuid.UUID
) -> bool:
    """Delete the passkey ``passkey_id`` of the account. ``False`` when the account has no such
    passkey (someone else's, or gone): nothing is deleted then."""
    removed = await session.execute(
        delete(PasskeyCredential)
        .where(PasskeyCredential.id == passkey_id, PasskeyCredential.account_id == account_id)
        .returning(PasskeyCredential.id)
    )
    return removed.first() is not None


def _transports(values: Sequence[str]) -> list[AuthenticatorTransport]:
    """The transports WebAuthn knows among ``values`` (others are dropped)."""
    known: list[AuthenticatorTransport] = []
    for value in values:
        try:
            known.append(AuthenticatorTransport(value))
        except ValueError:
            continue
    return known


# --- adding a passkey ---------------------------------------------------------------------------


async def registration_options(
    session: AsyncSession, account: Account, rp_id: str, now: datetime
) -> JsonObject:
    """The options for ``navigator.credentials.create()`` that add a passkey to ``account`` on
    ``rp_id``, in their JSON form; the challenge is stored. ``PasskeyRejected("too_many")`` when
    the account has ``MAX_PASSKEYS_PER_ACCOUNT`` already."""
    await _forget_ended_challenges(session, now, replaced_for=account.id)
    existing = await passkeys_of(session, account.id)
    if len(existing) >= MAX_PASSKEYS_PER_ACCOUNT:
        raise PasskeyRejected("too_many")
    challenge = await _new_challenge(session, "register", account.id, now)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_name=account.username,
        user_id=account.id.bytes,
        user_display_name=account.username,
        challenge=challenge,
        timeout=_TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(
                id=passkey.credential_id, transports=_transports(passkey.transports) or None
            )
            for passkey in existing
        ],
    )
    return options_to_json_dict(options)


async def finish_registration(
    session: AsyncSession,
    account: Account,
    rp_id: str,
    origin: str,
    payload: JsonObject,
    label: str,
    now: datetime,
) -> PasskeyCredential:
    """Check the browser's answer (``payload``, the JSON form of the new credential) to the
    options of ``registration_options``, and store the passkey under ``label`` (without the
    spaces around it). Raises ``PasskeyRejected``; a bad label is refused before the challenge is
    spent, so the same answer can come back with a better one."""
    name = label.strip()
    if not name or len(name) > MAX_PASSKEY_LABEL_LENGTH or not is_plain_line(name):
        raise PasskeyRejected("label")
    try:
        credential = parse_registration_credential_json(payload)
    except Exception as exc:  # whatever py_webauthn raises for a shape it does not know
        raise PasskeyRejected("malformed") from exc
    client_data = _client_data(credential.response.client_data_json)
    await _spend_challenge(session, "register", client_data.challenge, account.id, now)
    if await _count_passkeys(session, account.id) >= MAX_PASSKEYS_PER_ACCOUNT:
        raise PasskeyRejected("too_many")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=client_data.challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:  # anything the browser sent that does not check out is a refusal
        raise PasskeyRejected("verification") from exc
    if len(verified.credential_id) > MAX_CREDENTIAL_ID_LENGTH:
        raise PasskeyRejected("malformed")
    transports = [transport.value for transport in credential.response.transports or []]
    # A credential belongs to one account: a credential id that is already stored is refused,
    # whoever has it (and two registrations of one id at once cannot both get in).
    inserted = await session.execute(
        insert(PasskeyCredential)
        .values(
            account_id=account.id,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=transports,
            label=name,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[PasskeyCredential.credential_id])
        .returning(PasskeyCredential.id)
    )
    passkey_id = inserted.scalar_one_or_none()
    if passkey_id is None:
        raise PasskeyRejected("taken")
    stored = await session.execute(
        select(PasskeyCredential).where(PasskeyCredential.id == passkey_id)
    )
    return stored.scalar_one()


# --- logging in ---------------------------------------------------------------------------------


async def login_options(session: AsyncSession, rp_id: str, now: datetime) -> JsonObject:
    """The options for ``navigator.credentials.get()`` on ``rp_id``, in their JSON form: no
    credential is named (the browser offers the passkeys it has for this site); the challenge is
    stored. ``PasskeyRejected("busy")`` when ``MAX_PENDING_LOGIN_CHALLENGES`` wait already."""
    await _forget_ended_challenges(session, now)
    # Counted under a lock held until the caller commits: two requests at once cannot both see
    # room for one more.
    await session.execute(select(func.pg_advisory_xact_lock(LOGIN_CHALLENGES_LOCK_KEY)))
    waiting = await session.execute(
        select(func.count())
        .select_from(PasskeyChallenge)
        .where(
            PasskeyChallenge.purpose == "login",
            PasskeyChallenge.used_at.is_(None),
            PasskeyChallenge.expires_at > now,
        )
    )
    if waiting.scalar_one() >= MAX_PENDING_LOGIN_CHALLENGES:
        # Nobody signs in this often: the owner should know that someone keeps asking.
        logger.warning(
            "%d login challenges are waiting: passkey sign-in is refused until some expire",
            MAX_PENDING_LOGIN_CHALLENGES,
        )
        raise PasskeyRejected("busy")
    challenge = await _new_challenge(session, "login", None, now)
    options = generate_authentication_options(
        rp_id=rp_id,
        challenge=challenge,
        timeout=_TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json_dict(options)


async def finish_login(
    session: AsyncSession, rp_id: str, origin: str, payload: JsonObject, now: datetime
) -> Account:
    """Check the browser's answer (``payload``, the JSON form of the assertion) to the options of
    ``login_options``, and return the account of the passkey. Its counter and ``last_used_at``
    are updated. Raises ``PasskeyRejected``."""
    try:
        credential = parse_authentication_credential_json(payload)
    except Exception as exc:  # whatever py_webauthn raises for a shape it does not know
        raise PasskeyRejected("malformed") from exc
    client_data = _client_data(credential.response.client_data_json)
    await _spend_challenge(session, "login", client_data.challenge, None, now)
    found = await session.execute(
        select(PasskeyCredential)
        .where(PasskeyCredential.credential_id == credential.raw_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    passkey = found.scalar_one_or_none()
    if passkey is None:
        raise PasskeyRejected("unknown_credential")
    handle = credential.response.user_handle
    if handle is not None and handle != passkey.account_id.bytes:
        raise PasskeyRejected("verification")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=client_data.challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # anything the browser sent that does not check out is a refusal
        raise PasskeyRejected("verification") from exc
    holder = await session.execute(
        select(Account)
        .join(Person, Person.id == Account.person_id)
        .where(Account.id == passkey.account_id, active_at(now))
    )
    account = holder.scalar_one_or_none()
    if account is None:
        raise PasskeyRejected("not_in_household")
    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = now
    await session.flush()
    return account


# --- proving it is the person -------------------------------------------------------------------


async def proof_options(
    session: AsyncSession, account_id: uuid.UUID, rp_id: str, now: datetime
) -> JsonObject:
    """The options for ``navigator.credentials.get()`` that prove the person of ``account_id`` is
    the one at the page, on ``rp_id``, in their JSON form: the account's passkeys are named
    (``allowCredentials``), the user is verified, and the challenge is stored as a ``proof`` of
    that account. ``PasskeyRejected("no_passkey")`` when the account has none."""
    await _forget_ended_challenges(session, now)
    existing = await passkeys_of(session, account_id)
    if not existing:
        raise PasskeyRejected("no_passkey")
    challenge = await _new_challenge(session, "proof", account_id, now)
    options = generate_authentication_options(
        rp_id=rp_id,
        challenge=challenge,
        timeout=_TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[
            PublicKeyCredentialDescriptor(
                id=passkey.credential_id, transports=_transports(passkey.transports) or None
            )
            for passkey in existing
        ],
    )
    return options_to_json_dict(options)


async def finish_proof(
    session: AsyncSession,
    rp_id: str,
    origin: str,
    payload: JsonObject,
    account_id: uuid.UUID,
    now: datetime,
) -> None:
    """Check the browser's answer (``payload``, the JSON form of the assertion) to the options of
    ``proof_options`` for ``account_id``: the ``proof`` challenge of that account is spent (first,
    as for a login: a wrong answer spends it too), and the passkey that answers must be one of
    that account's (``verification`` otherwise). Its counter and ``last_used_at`` are updated.
    Raises ``PasskeyRejected``."""
    try:
        credential = parse_authentication_credential_json(payload)
    except Exception as exc:  # whatever py_webauthn raises for a shape it does not know
        raise PasskeyRejected("malformed") from exc
    client_data = _client_data(credential.response.client_data_json)
    await _spend_challenge(session, "proof", client_data.challenge, account_id, now)
    found = await session.execute(
        select(PasskeyCredential)
        .where(
            PasskeyCredential.credential_id == credential.raw_id,
            PasskeyCredential.account_id == account_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    passkey = found.scalar_one_or_none()
    if passkey is None:
        # Someone else's passkey, or none at all: it proves nothing of this person.
        raise PasskeyRejected("verification")
    handle = credential.response.user_handle
    if handle is not None and handle != account_id.bytes:
        raise PasskeyRejected("verification")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=client_data.challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # anything the browser sent that does not check out is a refusal
        raise PasskeyRejected("verification") from exc
    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = now
    await session.flush()
