"""A software authenticator for the passkey tests: it answers the options of the core as a browser
and its authenticator would, with real ES256 keys and signatures (``cryptography``), so that the
tests run py_webauthn's real verification instead of a mock of it.

It is not a conformant authenticator: it does what the tests need. It makes credentials with a
"none" attestation (the core asks for none), keeps their private keys, and signs assertions with
them. Every value the core checks can be made wrong on purpose (the origin, the RP id, the
counter, the user handle, the signature), to see that the core refuses it.

The fixture ``soft_authenticator`` (in ``conftest.py``) gives a new one to each test. The name of
this module is unique across the repository, as pytest's import mode requires (see
``web_support.py``).
"""

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from domo_core.accounts.passkey_models import PasskeyCredential

# The origin of the page in the tests: the stack's default address.
DEFAULT_ORIGIN = "https://localhost:8443"

# The flags of the authenticator data (WebAuthn §6.1).
USER_PRESENT = 0x01
USER_VERIFIED = 0x04
ATTESTED_CREDENTIAL_DATA = 0x40


def b64url(data: bytes) -> str:
    """Base64url without padding, as WebAuthn's JSON forms write bytes."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def from_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftCredential:
    """What the authenticator remembers of a credential it made."""

    credential_id: bytes
    private_key: ec.EllipticCurvePrivateKey
    rp_id: str
    user_handle: bytes
    # The signature counter: ``None`` for an authenticator that does not count (it always says 0).
    counter: int | None


def _cose_public_key(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    """The public half as a COSE_Key: EC2 (1: 2), ES256 (3: -7), P-256 (-1: 1), x and y."""
    numbers = private_key.public_key().public_numbers()
    return cbor2.dumps(
        {
            1: 2,
            3: -7,
            -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }
    )


def _client_data(kind: str, challenge: str, origin: str, **extra: Any) -> bytes:
    return json.dumps(
        {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False, **extra}
    ).encode()


class SoftAuthenticator:
    """Answers ``navigator.credentials.create()`` and ``.get()`` for the options the core gives.

    ``create`` and ``get`` return what the page posts back: the JSON form of the credential
    (``PublicKeyCredential.toJSON()``).
    """

    def __init__(self, origin: str = DEFAULT_ORIGIN) -> None:
        self.origin = origin
        self.credentials: dict[bytes, SoftCredential] = {}

    def create(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        counting: bool = True,
        user_verified: bool = True,
        credential_id: bytes | None = None,
        client_data_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A new credential for the registration ``options``. ``rp_id`` and ``origin`` default to
        the options' RP id and to this authenticator's origin; give others to lie about them.
        ``counting=False`` makes an authenticator whose counter is always 0."""
        private_key = ec.generate_private_key(ec.SECP256R1())
        cred_id = credential_id if credential_id is not None else os.urandom(32)
        used_rp_id = rp_id if rp_id is not None else options["rp"]["id"]
        user_handle = from_b64url(options["user"]["id"])
        counter = 0 if counting else None
        self.credentials[cred_id] = SoftCredential(
            cred_id, private_key, used_rp_id, user_handle, counter
        )
        flags = USER_PRESENT | ATTESTED_CREDENTIAL_DATA | (USER_VERIFIED if user_verified else 0)
        auth_data = (
            hashlib.sha256(used_rp_id.encode()).digest()
            + bytes([flags])
            + struct.pack(">I", 0)
            + bytes(16)  # AAGUID: none
            + struct.pack(">H", len(cred_id))
            + cred_id
            + _cose_public_key(private_key)
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = _client_data(
            "webauthn.create",
            options["challenge"],
            origin or self.origin,
            **(client_data_extra or {}),
        )
        return {
            "id": b64url(cred_id),
            "rawId": b64url(cred_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["internal", "hybrid"],
            },
        }

    def get(
        self,
        options: dict[str, Any],
        credential: PasskeyCredential | bytes,
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        sign_count: int | None = None,
        user_handle: bytes | None | str = "own",
        user_verified: bool = True,
        tamper: bool = False,
        client_data_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """An assertion of ``credential`` (a stored passkey, or a credential id) for the login
        ``options``. The counter goes up by one each time unless ``sign_count`` says otherwise (or
        the authenticator does not count). ``user_handle`` is the one of the registration by
        default; ``None`` leaves it out. ``tamper`` breaks the signature."""
        cred_id = credential if isinstance(credential, bytes) else credential.credential_id
        soft = self.credentials[cred_id]
        if sign_count is not None:
            count = sign_count
        elif soft.counter is None:
            count = 0
        else:
            soft.counter += 1
            count = soft.counter
        used_rp_id = rp_id if rp_id is not None else options.get("rpId", soft.rp_id)
        flags = USER_PRESENT | (USER_VERIFIED if user_verified else 0)
        auth_data = (
            hashlib.sha256(used_rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
        )
        client_data = _client_data(
            "webauthn.get",
            options["challenge"],
            origin or self.origin,
            **(client_data_extra or {}),
        )
        signed = auth_data + hashlib.sha256(client_data).digest()
        signature = soft.private_key.sign(signed, ec.ECDSA(hashes.SHA256()))
        if tamper:
            signature = soft.private_key.sign(signed + b"x", ec.ECDSA(hashes.SHA256()))
        response: dict[str, Any] = {
            "clientDataJSON": b64url(client_data),
            "authenticatorData": b64url(auth_data),
            "signature": b64url(signature),
        }
        handle = soft.user_handle if user_handle == "own" else user_handle
        if isinstance(handle, bytes):
            response["userHandle"] = b64url(handle)
        return {
            "id": b64url(cred_id),
            "rawId": b64url(cred_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": response,
        }
