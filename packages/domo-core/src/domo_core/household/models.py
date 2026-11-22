"""The household: invitations to join it, links to come back to it, and the owner's successors."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class Invitation(Base):
    """A one-time invitation to join the household, made by the owner or whoever has the right.

    Only the hash of its token is stored: a database leak does not leak working links. The hash
    is empty while the invitation waits for its link: when someone other than the one who asked
    said yes (``created_by``), the link is made, and shown once, when the one who asked asks for it
    (``household.service.reveal_link``); a link given again after someone else's yes waits for
    the one who asked for it (``link_for_id``, when it is not the one who made it). The
    invitation is open until it is accepted (``accepted_at``, with the person it created in
    ``accepted_person_id``), cancelled
    (``cancelled_at``) or past ``expires_at``. ``guest_days`` is how long a guest may stay once
    they have joined; only a guest has one.

    An invitation with a ``person_id`` is a return link: it does not make a new person, it brings
    that person, who left the household, back (``household.service.accept_return``). The role,
    ``guest_days`` and display name are then the ones they come back with, and
    ``accepted_person_id`` is that same person once the link is used.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint("role IN ('adult', 'child', 'guest')", name="role_values"),
        # The technical ceiling of a stay (``household.service.GUEST_DAYS_CEILING``): the
        # household's rule, 30 days unless the owner chose otherwise, is a limit of the catalog.
        CheckConstraint("guest_days BETWEEN 1 AND 365", name="guest_days_range"),
        CheckConstraint("guest_days IS NULL OR role = 'guest'", name="guest_days_only_for_guests"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    token_hash: Mapped[str | None] = mapped_column(unique=True)
    display_name: Mapped[str]
    role: Mapped[str]
    guest_days: Mapped[int | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    accepted_person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    cancelled_at: Mapped[datetime | None]
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    # Whether the invitee may use the inviter's key (null: not said yet).
    uses_inviter_key: Mapped[bool | None]
    # For whom a link that waits is (``household.service.give_link_again``): ``None`` for the one
    # who made the invitation.
    link_for_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )


class Successor(Base):
    """One place on the owner's list of successors: who takes over the owner's role if the owner
    can no longer, in the order the owner chose (``rank``, from 1, each used once).

    A place is held by a person of the household (``person_id``) or by someone outside it (``name``,
    at most 60 characters, and ``relation``: what the owner says of them, such as "my brother"):
    exactly one of the two. A person is on the list once. ``access_way`` is how the door is opened
    the day it comes: a personal access that only claims the role (``personal_access``), or a paper
    code kept with the will (``paper_code``); it is only noted for now.

    Someone who died (``deceased_at``) stays on the list, out of the running. There is one list,
    the household's: it belongs to whoever is the owner, and passes to the next one by itself
    (``household.succession``), minus the new owner, who is never on it.
    """

    __tablename__ = "successors"
    __table_args__ = (
        CheckConstraint("(person_id IS NULL) <> (name IS NULL)", name="one_who"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 60", name="name_length"),
        CheckConstraint("char_length(relation) BETWEEN 1 AND 200", name="relation_length"),
        CheckConstraint(
            "access_way IN ('personal_access', 'paper_code')", name="access_way_values"
        ),
        CheckConstraint("rank >= 1", name="rank_from_one"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    rank: Mapped[int] = mapped_column(unique=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"), unique=True)
    name: Mapped[str | None]
    relation: Mapped[str | None]
    access_way: Mapped[str | None]
    deceased_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
