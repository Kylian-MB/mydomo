"""Entrusting a device to Domo, and knowing it again (spec 6.5).

A person asks for a code (``begin_pairing``): twelve characters in three groups, like the setup
code, valid ``PAIRING_FOR`` and served once. They give it to the program on their device, which
trades it for a secret of its own (``finish_pairing``); from then on the device is known by that
secret (``device_of``). Both the code and the secret are only ever held in clear by the one who
shows them: the database keeps their SHA-256 (both are random, 59 bits and 256 bits, so a plain
hash is enough, as for a session's token), and neither is in an audit entry. A wrong, an expired
and a used code are refused alike, without saying which.

A device is known until it is taken back (``remove_device``). It stops being known when its person
is no longer in the household (``device_of`` checks it, as a session does), and is forgotten for
good (``forget_devices_of``) when they leave or are removed, when a guest's stay ends (the
upkeep, ``forget_devices_of_those_who_left``) and when they are brought back. Changing a password
does not touch a device.

None of the functions commits: the caller owns the transaction (an audit entry commits with the
change it describes). Nothing here knows the web.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import active_at
from domo_core.audit.log import append_audit
from domo_core.devices.models import Device, DevicePairing
from domo_core.setup_flow.code import generate_setup_code, normalize_setup_code
from domo_core.text import is_plain_line

# How long a code lasts once made.
PAIRING_FOR: Final = timedelta(minutes=10)
# The longest name of a device, in characters.
MAX_DEVICE_NAME: Final = 60
# How often the moment a device was last seen is written down, at most.
LAST_SEEN_EVERY: Final = timedelta(minutes=1)


def _hash(value: str) -> str:
    # ``surrogatepass``: whatever was typed can be hashed, and so can only fail to match.
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def clean_device_name(name: str) -> str:
    """The name of a device without the spaces around it: ``ValueError`` unless it is 1 to
    ``MAX_DEVICE_NAME`` characters that make one plain line."""
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_DEVICE_NAME or not is_plain_line(cleaned):
        raise ValueError(f"a device name is one plain line of 1 to {MAX_DEVICE_NAME} characters")
    return cleaned


def _in_the_household(now: datetime) -> Select[uuid.UUID]:
    """The people who are in the household at ``now``, for a sub-query."""
    return select(Person.id).where(active_at(now))


async def begin_pairing(session: AsyncSession, person_id: uuid.UUID, now: datetime) -> str:
    """A new code for ``person_id`` to entrust a device with, valid ``PAIRING_FOR`` from ``now``.
    The code that waited for them is replaced (it no longer works), and the ones that ended are
    dropped. The code is returned and kept nowhere (only its hash); the audit entry
    ``device.entrusted`` names the person, never the code. Flushed, not committed."""
    await session.execute(
        delete(DevicePairing)
        .where(
            or_(
                DevicePairing.expires_at < now,
                (DevicePairing.person_id == person_id) & DevicePairing.used_at.is_(None),
            )
        )
        .execution_options(synchronize_session=False)
    )
    code = generate_setup_code()
    session.add(
        DevicePairing(person_id=person_id, code_hash=_hash(code), expires_at=now + PAIRING_FOR)
    )
    await session.flush()
    await append_audit(
        session,
        actor=f"person:{person_id}",
        action="device.entrusted",
        details={"person_id": str(person_id)},
    )
    return code


async def finish_pairing(
    session: AsyncSession, code: str, name: str, now: datetime
) -> tuple[Device, str] | None:
    """Trade a code for a device: the new device (called ``name``) and its raw secret, which
    nothing keeps but its hash. ``None`` when the code is wrong, ended or used, or its person is
    no longer in the household, without saying which. The code is spent in the same statement that
    checks it, so that it serves once even when two programs give it together. ``ValueError`` for
    a name that is not one plain line of 1 to ``MAX_DEVICE_NAME`` characters, before the code is
    looked at (it is not spent). Flushed, not committed."""
    device_name = clean_device_name(name)
    spent = await session.execute(
        update(DevicePairing)
        .where(
            DevicePairing.code_hash == _hash(normalize_setup_code(code)),
            DevicePairing.used_at.is_(None),
            DevicePairing.expires_at >= now,
            DevicePairing.person_id.in_(_in_the_household(now)),
        )
        .values(used_at=now)
        .returning(DevicePairing.person_id)
        .execution_options(synchronize_session=False)
    )
    person_id = spent.scalar_one_or_none()
    if person_id is None:
        return None
    secret = secrets.token_urlsafe(32)
    device = Device(
        person_id=person_id, name=device_name, secret_hash=_hash(secret), created_at=now
    )
    session.add(device)
    await session.flush()
    await append_audit(
        session,
        actor=f"person:{person_id}",
        action="device.paired",
        details={"device_id": str(device.id), "person_id": str(person_id)},
    )
    return device, secret


async def device_of(
    session: AsyncSession, raw_secret: str, now: datetime
) -> tuple[Device, Person] | None:
    """The device a secret belongs to and its person at ``now``, or ``None``: an unknown secret, a
    device that was taken back, or a person who is no longer in the household. A device that
    counts gets its ``last_seen_at`` set to ``now`` when it was not set in the last
    ``LAST_SEEN_EVERY`` (a device that speaks often is not written down at every request); one
    that does not count is left untouched. Flushed, not committed."""
    found = await session.execute(
        select(Device, Person)
        .join(Person, Person.id == Device.person_id)
        .where(
            Device.secret_hash == _hash(raw_secret),
            Device.removed_at.is_(None),
            Device.person_id.in_(_in_the_household(now)),
        )
        .execution_options(populate_existing=True)
    )
    row = found.one_or_none()
    if row is None:
        return None
    device, person = row[0], row[1]
    if device.last_seen_at is None or now - device.last_seen_at >= LAST_SEEN_EVERY:
        device.last_seen_at = now
        await session.flush()
    return device, person


async def devices_of(session: AsyncSession, person_id: uuid.UUID) -> list[Device]:
    """The devices of the person that were not taken back, the oldest first."""
    found = await session.execute(
        select(Device)
        .where(Device.person_id == person_id, Device.removed_at.is_(None))
        .order_by(Device.created_at, Device.id)
    )
    return list(found.scalars())


async def remove_device(
    session: AsyncSession, device_id: uuid.UUID, now: datetime, *, actor: str = "system"
) -> bool:
    """Take a device back at ``now``, ``actor`` being who does it (``person:<id>``): its secret no
    longer works. ``False`` when there is no such device, or it was taken back already. The audit
    entry ``device.removed`` names the device and its person, never its name. Flushed, not
    committed."""
    removed = await session.execute(
        update(Device)
        .where(Device.id == device_id, Device.removed_at.is_(None))
        .values(removed_at=now)
        .returning(Device.person_id)
        .execution_options(synchronize_session=False)
    )
    person_id = removed.scalar_one_or_none()
    if person_id is None:
        return False
    await append_audit(
        session,
        actor=actor,
        action="device.removed",
        details={"device_id": str(device_id), "person_id": str(person_id)},
    )
    return True


async def forget_devices_of(
    session: AsyncSession, person_id: uuid.UUID, now: datetime, *, actor: str = "system"
) -> int:
    """Forget every device of the person at ``now``, and the codes that waited for them (they left
    the household, or came back to it). Returns how many devices were forgotten; each one has its
    audit entry ``device.removed``. Flushed, not committed."""
    await session.execute(
        delete(DevicePairing)
        .where(DevicePairing.person_id == person_id)
        .execution_options(synchronize_session=False)
    )
    forgotten = await session.execute(
        update(Device)
        .where(Device.person_id == person_id, Device.removed_at.is_(None))
        .values(removed_at=now)
        .returning(Device.id)
        .execution_options(synchronize_session=False)
    )
    device_ids = sorted(forgotten.scalars().all(), key=str)
    for device_id in device_ids:
        await append_audit(
            session,
            actor=actor,
            action="device.removed",
            details={"device_id": str(device_id), "person_id": str(person_id)},
        )
    return len(device_ids)


async def forget_devices_of_those_who_left(session: AsyncSession, now: datetime) -> int:
    """Forget the devices of everyone who is no longer in the household at ``now`` (a guest whose
    stay ended, or someone removed before devices were forgotten at removal). Returns how many
    devices were forgotten, by the system. Nothing is committed."""
    departed = await session.execute(
        select(Device.person_id)
        .join(Person, Person.id == Device.person_id)
        .where(Device.removed_at.is_(None), ~active_at(now))
        .distinct()
    )
    forgotten = 0
    for person_id in sorted(departed.scalars().all(), key=str):
        forgotten += await forget_devices_of(session, person_id, now)
    return forgotten
