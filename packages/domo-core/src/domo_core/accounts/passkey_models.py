"""Passkeys (WebAuthn): the credentials of an account and the challenges in flight."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class PasskeyCredential(Base):
    """A passkey registered on an account: the public half only, never a private key."""

    __tablename__ = "webauthn_credentials"
    __table_args__ = (
        # "The passkeys of this account": the account page, the options of a new registration.
        Index("ix_webauthn_credentials_account_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    # What the authenticator calls this credential; the browser sends it back at login.
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # The authenticator's counter: a value that goes back is a sign of a cloned key.
    sign_count: Mapped[int] = mapped_column(BigInteger)
    transports: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # A name the person gave it ("Alex's phone"), shown on their account page.
    label: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    last_used_at: Mapped[datetime | None]


class PasskeyChallenge(Base):
    """A challenge the core sent for a registration, a login or a proof that it is the person
    (``proofs.service``), good for one answer.

    ``account_id`` is the account being registered, logged in or proving it is its person, when it
    is known; it is not a foreign key (a challenge is short-lived, and one for an account that has
    since gone is simply never answered).
    """

    __tablename__ = "webauthn_challenges"
    __table_args__ = (
        CheckConstraint("purpose IN ('register', 'login', 'proof')", name="purpose_values"),
        # An answer is matched to its challenge by the challenge itself.
        Index("ix_webauthn_challenges_challenge", "challenge"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    purpose: Mapped[str]
    account_id: Mapped[uuid.UUID | None]
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
