"""The actor of a person: the only place an ``Actor`` is built from a ``Person``.

The marks of an actor (``child``, ``visiting``, ``resident``, ``made_role``) are read from the
person's record, so a protection never depends on the name of a role.
"""

from sqlalchemy import ColumnElement, or_

from domo_core.accounts.models import Person
from domo_core.policy.roles import Actor, as_role


def is_child(person: Person) -> bool:
    """Whether the protections of a child apply to ``person``: the record's mark, or the
    built-in child role. Never the owner's: he carries no child mark."""
    if person.role == "owner":
        return False
    return person.is_child or person.role == "child"


def is_visiting(person: Person) -> bool:
    """Whether ``person`` is a visitor: the record says so, or it holds the guest role."""
    return person.standing == "visiting" or person.role == "guest"


def marks_of(person: Person) -> dict[str, object]:
    """The four marks of an actor, for a ``dataclasses.replace`` of an existing actor."""
    return {
        "child": is_child(person),
        "visiting": is_visiting(person),
        "resident": person.standing == "lives_here" and person.role != "guest",
        "made_role": person.made_role_id,
    }


def actor_of(person: Person, *, trusted: bool = True) -> Actor:
    """``person`` as the barrier sees them."""
    return Actor(
        person.id,
        as_role(person.role),
        trusted=trusted,
        child=is_child(person),
        visiting=is_visiting(person),
        resident=person.standing == "lives_here" and person.role != "guest",
        made_role=person.made_role_id,
    )


# The same as ``is_visiting``, for a query: who is a visitor.
VISITING: ColumnElement[bool] = or_(Person.standing == "visiting", Person.role == "guest")
