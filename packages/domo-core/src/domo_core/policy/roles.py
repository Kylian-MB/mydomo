"""The household roles, and who an action is done for: a person, or Domo himself."""

from dataclasses import dataclass
from typing import Final, Literal, get_args
from uuid import UUID

# The same four values as the ``persons.role`` constraint. What each role may do is in
# ``policy.catalog``.
Role = Literal["owner", "adult", "child", "guest"]

ROLES: tuple[Role, ...] = get_args(Role)


def as_role(value: str) -> Role:
    """The stored ``persons.role`` as a ``Role``.

    The column's constraint keeps any other value out of the database, so a ``ValueError`` here
    means a bug (or a database that was edited by hand), not something a person can cause.
    """
    for role in ROLES:
        if role == value:
            return role
    raise ValueError(f"Not a household role: {value!r}")


# Who an actor is: a person of the household, or Domo himself, for what he does on his own for
# the house (the owner, 2026-11-18). Domo is never a person row nor a fifth role.
Subject = Literal["person", "domo"]

# The id Domo carries as an actor: nobody's (no person has it), so that what reads a person by
# the id of an actor finds nobody rather than someone.
DOMO_SUBJECT_ID: Final = UUID(int=0)
# How the audit log and the facts of a limit name Domo (``actor_label``).
DOMO_LABEL: Final = "domo"


@dataclass(frozen=True)
class Actor:
    """Who asks for an action, as the barrier sees them.

    ``trusted`` says whether the identity is certain: someone logged in on the web page is; a
    caller known only by what they claim (a channel without a strong link to the person, later)
    is not. ``subject`` says whether it is a person or Domo (``domo_actor``): Domo's rights
    are his own (``policy.rights``), and his role is only what a rule on roles reads of him.
    """

    person_id: UUID
    role: Role
    trusted: bool = True
    subject: Subject = "person"


def domo_actor() -> Actor:
    """Domo as an actor, for what he does on his own for the house: the role of an adult for
    what reads a role (whose agreement is needed, a limit), certain of who he is, and his own
    rights."""
    return Actor(DOMO_SUBJECT_ID, "adult", trusted=True, subject="domo")


def actor_label(actor: Actor) -> str:
    """How the audit log and the facts of a limit name ``actor``: ``person:<id>``, or
    ``domo``."""
    return DOMO_LABEL if actor.subject == "domo" else f"person:{actor.person_id}"
