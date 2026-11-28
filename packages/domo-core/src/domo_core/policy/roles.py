"""The household's built-in roles, and who an action is done for: a person, or Domo himself.

The four built-in roles are also the bases a role the assistant makes for the household starts
from (``people.made_roles``; the owner's role is never made nor copied): an actor carries that
role's id (``Actor.made_role``), and a protection reads the actor's marks, never a role's name."""

from dataclasses import dataclass, replace
from typing import Final, Literal, get_args
from uuid import UUID

# The same four values as the ``persons.role`` constraint. What each role may do is in
# ``policy.catalog``; what a role made for the household gives on top of its base is in
# ``people.made_roles``.
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
    # The marks of the person's record (``accounts.actors.actor_of``): a protection reads these,
    # never the name of the role. ``made_role`` is the role the assistant made for them.
    child: bool = False
    visiting: bool = False
    resident: bool = True
    made_role: UUID | None = None


def asked_as(actor: Actor, role: Role) -> Actor:
    """``actor`` with the role they had when they asked (a chain judges its steps by it); the
    marks of their record stay those of now."""
    return replace(actor, role=role)


def domo_actor() -> Actor:
    """Domo as an actor, for what he does on his own for the house: the role of an adult for
    what reads a role (whose agreement is needed, a limit), certain of who he is, and his own
    rights."""
    return Actor(DOMO_SUBJECT_ID, "adult", trusted=True, subject="domo")


def actor_label(actor: Actor) -> str:
    """How the audit log and the facts of a limit name ``actor``: ``person:<id>``, or
    ``domo``."""
    return DOMO_LABEL if actor.subject == "domo" else f"person:{actor.person_id}"
