"""Who may say yes for a child: the single spot for this rule.

A child's request on a type that waits for an adult's agreement
(``catalog.ADULT_AGREES_FOR_A_CHILD``), and any request a right sends to an adult, is answered by
someone who may agree for the one who asked. The confirmations' check of who may answer such a
request (``store.approves``, ``store.awaiting_approval``) and the case of the yes-judge, which
marks the lines said by such a person (``case.gather``), all ask this module, so that they never
disagree. Whether that person is still in the household is checked by each caller, as for every
approver.
"""

from typing import Final
from uuid import UUID

# The roles whose members may agree for a child today: there is no right for it yet, so the
# role is the only fact the code has. A role added later agrees for nobody until it is named here.
_AGREES_FOR_A_CHILD: Final = frozenset({"adult", "owner"})


def agrees_for_children(role: str) -> bool:
    """Whether a person of ``role`` may agree for a child at all (an adult, the owner): what a
    list of the requests that wait for an adult's yes reads before any child is known
    (``store.awaiting_approval``). Part of the rule of ``may_agree_for_a_child``."""
    return role in _AGREES_FOR_A_CHILD


def may_agree_for_a_child(role: str, person_id: UUID, child_id: UUID) -> bool:
    """Whether a person of ``role`` (``person_id``) may give an adult's agreement to a request
    of ``child_id``: an adult or the owner, never the one who asked. The single spot for this
    rule: both the check of who may answer (``store.approves``) and the mark of the judge's case
    (``case.gather``) call it."""
    return agrees_for_children(role) and person_id != child_id
