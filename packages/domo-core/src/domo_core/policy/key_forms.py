"""Who may have a form for a key, from a page: rules, not routes (the routes are
``channels.page.key_routes``; the chat page reads ``may_add_a_new_key`` too, to offer the form of a
key of one's own).

A form for a key given again, or for an admin key (``may_open_a_form``, rulings R20 and R22), and
the form of a new key (``may_add_a_key``), open in one of these cases, and nowhere else:

* without a request named: the barrier (``policy.decision.decide_now``, for the person, on the
  action the tool describes) lets it through: a click on the page is the person's own act, and the
  judge is not consulted for it. A refusal, or an agreement that is an adult's or the owner's, is a
  no;
* after someone else's yes: the request is named (the card's button carries it); it is the
  person's, of the right type and for the right target, executed after someone else's yes; that
  yes is not older than the confirmations' lifetime; the barrier does not refuse the action now (a
  limit the owner added since closes it; a yes still asked for is fine, it was given), read as the
  limits stood at the yes (ruling R23); and the yes is not spent;
* from the button of a step of a chain (spec 4.5): the request named is a chain of the person's
  with a step done that left that form behind its button; the lifetime counts from when the step
  was done; the rest is as above.

One yes, one key: a yes is spent once a key was saved under it, whatever its check said
(``REKEYED``, ``ADMIN_ADDED``, recorded by the routes), or once the gatekeeper says a form opened
under it was filled in. A request and those a replacement links it to are one yes
(``linked_requests``). A gatekeeper that cannot say closes it (fail closed).
"""

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.confirmations.models import Confirmation, ConfirmationStep
from domo_core.confirmations.store import action_type_of, confirmations_by_id
from domo_core.gatekeeper_client import GatekeeperClient, GatekeeperError
from domo_core.llm.key_flow import started_under
from domo_core.llm.models import Provider
from domo_core.policy.actions import DescribedAction
from domo_core.policy.barrier import Allow, Refuse
from domo_core.policy.catalog import PolicyTables
from domo_core.policy.decision import decide_now
from domo_core.policy.roles import Actor, as_role
from domo_core.policy.settings import load_policy_settings
from domo_core.tools.key_tools import KEY_ADD_ACTION_TYPE, key_add_action
from domo_core.tools.model_key import REKEY_ACTION_TYPE, rekey_action
from domo_core.tools.spending_tools import KEY_ADMIN_ACTION_TYPE, admin_key_action

# A request's id, as an address names it.
CONFIRMATION_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The gatekeeper's request ids (``secrets.token_urlsafe``).
GATEKEEPER_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
# What records a key given again, and an admin key given, once its form was filled in: a key saved
# under a yes, which spends it.
REKEYED: Final = "provider.rekeyed"
ADMIN_ADDED: Final = "key.admin_added"


async def linked_requests(session: AsyncSession, request: Confirmation) -> list[str]:
    """``request`` and the requests a replacement links it to, both ways, one after the other
    (``replaced_by``: a change, or a chain taken up again, makes a new request that holds the steps
    done of the old one, copied, ``confirmations.replan.replace_chain_request``), as texts, in no
    particular order. They are one yes for the form: a key saved under any of them spends them all
    (ruling R22)."""
    found = {request.id}
    edge = {request.id}
    while edge:
        rows = await session.execute(
            select(Confirmation.id, Confirmation.replaced_by).where(
                or_(Confirmation.id.in_(edge), Confirmation.replaced_by.in_(edge))
            )
        )
        linked: set[uuid.UUID] = set()
        for linked_id, replaced_by in rows.all():
            linked.add(linked_id)
            if replaced_by is not None:
                linked.add(replaced_by)
        edge = linked - found
        found |= linked
    return [str(linked_id) for linked_id in found]


async def _is_spent(
    session: AsyncSession, gatekeeper: GatekeeperClient, request_ids: Sequence[str]
) -> bool:
    """Whether a key was saved under the yes of the requests ``request_ids`` (one yes: a request
    and those a replacement links it to, ``linked_requests``; ruling R22): the entry
    ``provider.rekeyed`` (or ``key.admin_added``, for an admin key) carries one of them, or the
    gatekeeper says that a form opened under one of them (the entry ``secret.requested`` carries
    it, with the form's request) was filled in, even if nobody asked the page where that form
    stood. A gatekeeper that cannot say closes it (fail closed). The reads of the database end
    before the vault is asked: no transaction waits for it."""
    under_one_of_them = or_(
        *(AuditEntry.details.contains({"confirmation": request_id}) for request_id in request_ids)
    )
    saved = await session.execute(
        select(AuditEntry.id)
        .where(AuditEntry.action.in_((REKEYED, ADMIN_ADDED)), under_one_of_them)
        .limit(1)
    )
    if saved.first() is not None:
        return True
    opened = await session.execute(
        select(AuditEntry.details).where(AuditEntry.action == "secret.requested", under_one_of_them)
    )
    forms = [details.get("request_id") for details in opened.scalars()]
    await session.commit()
    for form in forms:
        if not isinstance(form, str) or GATEKEEPER_REQUEST_ID.fullmatch(form) is None:
            continue
        try:
            view = await gatekeeper.get_secret_request(form)
        except GatekeeperError as error:
            if error.status == 404:
                continue  # the vault lost it: no key was saved under it
            return True
        if view.status == "fulfilled":
            return True
    return False


async def may_open_a_form(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    person: Person,
    provider: Provider,
    request_id: str | None,
    policy: PolicyTables,
    now: datetime,
    *,
    admin: bool = False,
) -> bool:
    """Whether ``person`` may have a new form for the key of ``provider`` (rulings R20 and R22, see
    the module docstring), or for its admin key (``admin``: on the action ``key_admin`` describes,
    its step's button, and its record, all the same way). ``request_id`` is the request the card's
    button names, if it does.

    Without one: the barrier, asked for ``person`` on the action ``model_rekey`` describes, lets it
    through (their click on the page is their own act: the judge is not consulted for it, as
    ``require_action`` reads only the right); a refusal, or an agreement that is an adult's or the
    owner's, is a no. With one, a request of this person's: the yes of someone else to
    their ``model.rekey`` request, or, for a chain, a step of it that left the form behind its
    button (whoever said yes, their own yes included: a chain never shows the form at once), as
    long as that yes, or that step, is recent; the barrier does not refuse the action now (a yes
    still asked for is fine: it was given); and no key was saved under it yet."""
    actor = Actor(person_id=person.id, role=as_role(person.role))
    action = admin_key_action(provider, person.display_name) if admin else rekey_action(provider)
    if request_id is None:
        decision = await decide_now(session, actor, action, policy, now)
        return isinstance(decision, Allow)
    if CONFIRMATION_ID.fullmatch(request_id) is None:
        return False
    wanted = uuid.UUID(request_id)
    request = (await confirmations_by_id(session, [wanted])).get(wanted)
    if request is None or request.requester_id != person.id:
        return False
    since = (
        await _form_step_done_at(session, request, provider, admin=admin)
        if request.chain_basis is not None
        else _someone_elses_yes_at(request, action)
    )
    if since is None:
        return False
    # The limits are read as they stood at the yes: this request is not counted in the history
    # (it was not, at its yes), so that "once a day" lets the one request through.
    decision = await decide_now(session, actor, action, policy, now, not_counting=wanted)
    if isinstance(decision, Refuse):
        return False
    settings = await load_policy_settings(session)
    if now >= since + settings.max_lifetime:
        return False
    return not await _is_spent(session, gatekeeper, await linked_requests(session, request))


def _someone_elses_yes_at(request: Confirmation, action: DescribedAction) -> datetime | None:
    """When someone else's yes was given to ``request``, a request of the type and for the target
    of ``action`` that it ran; ``None`` for any other request (the person's own yes showed the form
    at once)."""
    if (
        request.status != "executed"
        or action_type_of(request) != action.action_type
        or request.action.get("target") != action.target
        or request.decided_by is None
        or request.decided_by == request.requester_id
    ):
        return None
    return request.decided_at


async def _form_step_done_at(
    session: AsyncSession, request: Confirmation, provider: Provider, *, admin: bool
) -> datetime | None:
    """When a step of the chain ``request`` that left the form for the key of ``provider`` behind
    its button was done (``model.rekey``, ``done``, its card naming the provider: spec 4.5), or
    the form of its admin key (``admin``: ``key.admin``, its card's ``admin_key``); ``None`` when
    it has none (``_step_done_at``)."""
    action_type, button = (
        (KEY_ADMIN_ACTION_TYPE, "admin_key") if admin else (REKEY_ACTION_TYPE, "rekey")
    )
    return await _step_done_at(
        session, request, action_type, ConfirmationStep.result[button].astext == provider.id
    )


async def _step_done_at(
    session: AsyncSession, request: Confirmation, action_type: str, button: ColumnElement[bool]
) -> datetime | None:
    """When a step of the chain ``request`` of ``action_type`` that left its button (``button``,
    on its card) was done; ``None`` when it has none. A step of a chain never shows a form at once,
    whoever said yes, and the yes is not kept on a chain that waits again (``decided_*`` are
    emptied): the step's own time counts, whatever the state of the request now."""
    found = await session.execute(
        select(ConfirmationStep.done_at)
        .where(
            ConfirmationStep.confirmation_id == request.id,
            ConfirmationStep.action_type == action_type,
            ConfirmationStep.status == "done",
            button,
            ConfirmationStep.done_at.is_not(None),
        )
        .order_by(ConfirmationStep.done_at.desc())
        .limit(1)
    )
    return found.scalar_one_or_none()


async def request_named(
    session: AsyncSession, person: Person, request_id: str
) -> Confirmation | None:
    """The request ``request_id`` names, if it is a request of ``person``'s."""
    if CONFIRMATION_ID.fullmatch(request_id) is None:
        return None
    wanted = uuid.UUID(request_id)
    request = (await confirmations_by_id(session, [wanted])).get(wanted)
    if request is None or request.requester_id != person.id:
        return None
    return request


async def may_add_a_new_key(
    session: AsyncSession, person: Person, policy: PolicyTables, now: datetime
) -> bool:
    """Whether ``person`` may have the form of a new key of their own now, with no request
    behind it: the barrier, asked for them on the action ``key_add`` describes, lets it through
    (``may_add_a_key`` without a request). Asks nothing of the gatekeeper."""
    actor = Actor(person_id=person.id, role=as_role(person.role))
    return isinstance(await decide_now(session, actor, key_add_action(), policy, now), Allow)


async def may_add_a_key(
    session: AsyncSession,
    gatekeeper: GatekeeperClient,
    person: Person,
    request_id: str | None,
    policy: PolicyTables,
    now: datetime,
    *,
    key: Provider | None = None,
) -> bool:
    """Whether ``person`` may have the form of a new key (see the module docstring), as
    ``may_open_a_form`` for a key given again. ``request_id`` is the request the card's button
    names, if it does. Without one: the barrier, asked for ``person`` on the action ``key_add``
    describes, lets it through. With one, a request of this person's: someone else's yes to their
    ``key.add`` request, or a step of a chain of theirs that left the page behind its button, as
    long as that yes, or that step, is recent; the barrier does not refuse the action now; and the
    yes is the yes of one key. For a new key (``key`` is ``None``): no key was started under it
    yet. For the key ``key`` (its vault form again): it is the key started under that yes, and no
    key was saved under it yet (``_is_spent``: whatever the check said, as for a key given
    again)."""
    actor = Actor(person_id=person.id, role=as_role(person.role))
    action = key_add_action()
    if request_id is None:
        return await may_add_a_new_key(session, person, policy, now)
    request = await request_named(session, person, request_id)
    if request is None:
        return False
    since = (
        await _step_done_at(
            session,
            request,
            KEY_ADD_ACTION_TYPE,
            ConfirmationStep.result["key_new"].astext == "true",
        )
        if request.chain_basis is not None
        else _someone_elses_yes_at(request, action)
    )
    if since is None:
        return False
    decision = await decide_now(session, actor, action, policy, now, not_counting=request.id)
    if isinstance(decision, Refuse):
        return False
    settings = await load_policy_settings(session)
    if now >= since + settings.max_lifetime:
        return False
    linked = await linked_requests(session, request)
    started = await started_under(session, linked)
    if key is None:
        return not started
    if key.id not in started:
        return False
    return not await _is_spent(session, gatekeeper, linked)
