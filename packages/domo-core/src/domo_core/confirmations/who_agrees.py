"""Who may say yes for a child: the single spot for this rule.

A child's request on a type that waits for an adult's agreement
(``catalog.ADULT_AGREES_FOR_A_CHILD``), and any request a right sends to an adult, is answered by
someone who may agree for the one who asked. The confirmations' check of who may answer such a
request (``store.approves``, ``store.awaiting_approval``), the yes of a launch
(``madetools.offered``) and the case of the yes-judge, which marks the lines said by such a person
(``case.gather``), all ask this module, so that they never disagree. Whether that person is still
in the household and can answer is checked by each caller, as for every approver.

It is a fact about the person, never the name of a role: the assistant makes roles, so no check
reads one. The right to agree for a child is the type ``catalog.CHILD_AGREE``, read like any right;
the built-in base of the person's role is read here, and in ``accounts.actors``, only.
"""

from uuid import UUID

from domo_core.policy.catalog import CHILD_AGREE, PolicyTables
from domo_core.policy.rights import RightsTable
from domo_core.policy.roles import Actor


def agrees_for_children(actor: Actor, *, rights: RightsTable, policy: PolicyTables) -> bool:
    """Whether ``actor`` may agree for a child at all, under the ``rights`` in force and the
    catalog ``policy``: what a list of the requests that wait for an adult's yes reads before any
    child is known (``store.awaiting_approval``). In order:

    1. never the assistant, an actor whose identity is not trusted, nor a child-marked person,
       whatever their role or their rights;
    2. always the owner;
    3. the owner's own right for that person on ``CHILD_AGREE``, when there is one;
    4. else the access the role the assistant made for them gives or denies on it;
    5. else a person not child-marked whose role's base is an adult, whatever the role is called,
       whether they live here or only pass by: whether one passing by is the adult looking after
       the child then is the judge's to weigh, who is told where they stand (the owner's answer of
       2026-11-27, "Domo juge")."""
    if actor.subject == "domo" or not actor.trusted or actor.child:
        return False
    if actor.role == "owner":
        return True
    if CHILD_AGREE not in policy.known:
        return False
    said = rights.said_for(actor, CHILD_AGREE)
    if said is not None:
        return said == "allow"
    return actor.role == "adult"


def may_agree_for_a_child(
    actor: Actor, child_id: UUID, *, rights: RightsTable, policy: PolicyTables
) -> bool:
    """Whether ``actor`` may give an adult's agreement to a request of ``child_id``: never the
    one who asked, then the rule of ``agrees_for_children``. The single spot for this rule: the
    check of who may answer (``store.approves``) and the mark of the judge's case
    (``case.gather``) both call it."""
    return actor.person_id != child_id and agrees_for_children(actor, rights=rights, policy=policy)
