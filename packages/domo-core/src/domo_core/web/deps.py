"""Dependencies shared by the pages (FastAPI ``Depends``). Tests override them through
``app.dependency_overrides``."""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domo_core.accounts.models import Person
from domo_core.accounts.sessions import SESSION_COOKIE, resolve_session
from domo_core.config import CoreSettings

# Re-exported (``as``): the pages import the type of the session factory from here.
from domo_core.db import SessionFactory as SessionFactory
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.factory import ModelFactory, build_chat_model
from domo_core.llm.listing import ModelLister, list_models
from domo_core.llm.models import Provider
from domo_core.llm.types import ChatModel
from domo_core.policy.catalog import DEFAULT_POLICY, PolicyTables
from domo_core.policy.open_catalog import load_policy
from domo_core.policy.rights import load_rights
from domo_core.policy.roles import Actor, as_role
from domo_core.sandbox_client import NO_SANDBOX, Sandbox
from domo_core.tools.registry import TOOLS, InternalTool
from domo_core.toolsdb.store import ToolsBase


class LoginRequired(Exception):
    """The request needs a logged-in person and has none. The application answers it with a
    redirect to the login page (``web.app``)."""


# The header a page sends with each of its requests: the id of the person it was rendered for
# (``base.html`` sets it once, for htmx; ``chat.js`` and ``passkeys.js`` send it with their own
# requests).
PAGE_PERSON_HEADER: Final = "X-Domo-Page-Person"


class AnotherPersonsPage(Exception):
    """The request comes from a page that was rendered for someone else than the person of the
    session: the browser's session changed since (someone joined or logged in, in another tab).
    Nothing is done, and the application answers so that the tab reloads in full (``web.app``):
    it then shows the session's person."""


def get_settings(request: Request) -> CoreSettings:
    """The settings the application was created with."""
    settings: CoreSettings = request.app.state.settings
    return settings


def get_gatekeeper(request: Request) -> GatekeeperClient:
    """The client of the gatekeeper, built when the application started."""
    gatekeeper: GatekeeperClient = request.app.state.gatekeeper
    return gatekeeper


def get_sandbox(request: Request) -> Sandbox:
    """The client of the sandbox, built when the application started (``NO_SANDBOX`` where the
    lifespan did not run: a call then fails as a sandbox that is down)."""
    sandbox: Sandbox = getattr(request.app.state, "sandbox", NO_SANDBOX)
    return sandbox


def sandbox_of(app: FastAPI) -> Sandbox:
    """The sandbox of ``app``, as a request reads it (a test may replace it), for what is not a
    request."""
    override = app.dependency_overrides.get(get_sandbox)
    sandbox: Sandbox = getattr(app.state, "sandbox", NO_SANDBOX) if override is None else override()
    return sandbox


def tools_base_of(app: FastAPI) -> ToolsBase | None:
    """The tools' own database of ``app``, as the lifespan made it (``None`` where it did not
    run)."""
    base: ToolsBase | None = getattr(app.state, "tools_base", None)
    return base


def get_model_factory(
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
) -> ModelFactory:
    """Builds the model of a provider, talking to the gatekeeper's relay. Tests replace it with
    ``fake_factory`` through ``app.dependency_overrides``."""

    def factory(provider: Provider, model_id: str) -> ChatModel:
        return build_chat_model(provider, model_id, gatekeeper)

    return factory


def get_model_lister(
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
) -> ModelLister:
    """Lists the models a key opens, through the gatekeeper's relay. Tests replace it with a
    scripted lister through ``app.dependency_overrides``."""

    def lister(provider: Provider) -> Awaitable[tuple[str, ...]]:
        return list_models(gatekeeper, provider)

    return lister


def gatekeeper_of(app: FastAPI) -> GatekeeperClient:
    """The gatekeeper's client of ``app``, as a request reads it (a test may replace it), for the
    code that needs it outside a request's dependencies (the upkeep, an arrival turn)."""
    override = app.dependency_overrides.get(get_gatekeeper)
    gatekeeper: GatekeeperClient = app.state.gatekeeper if override is None else override()
    return gatekeeper


def model_lister_of(app: FastAPI) -> ModelLister:
    """The lister of the models of a key of ``app``, as a request reads it (a test may replace
    it)."""
    override = app.dependency_overrides.get(get_model_lister)
    lister: ModelLister = get_model_lister(gatekeeper_of(app)) if override is None else override()
    return lister


def model_factory_of(app: FastAPI) -> ModelFactory:
    """The factory of the models of ``app``, as a request reads it (a test may replace it)."""
    override = app.dependency_overrides.get(get_model_factory)
    factory: ModelFactory = (
        get_model_factory(gatekeeper_of(app)) if override is None else override()
    )
    return factory


def get_session_factory(request: Request) -> SessionFactory:
    """The application's session factory, for the code that opens sessions of its own: a stream
    that lasts minutes must not keep the request's session (and a connection) for all that time."""
    factory: SessionFactory = request.app.state.session_factory
    return factory


def get_streaming_replies(request: Request) -> set[UUID]:
    """The replies this process is writing right now: the chat claims a reply here while a
    connection streams it, so that a second one (the browser reconnecting) does not start a second
    generation. It is a set of the application, not of the database: the core is one process."""
    streaming: set[UUID] = request.app.state.streaming_replies
    return streaming


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A database session for the request, closed at its end. Nothing is committed on its own:
    the code that changes something commits."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Callable[[], datetime]:
    """The clock the pages read the time from: a function that returns the current time, in UTC.
    Tests replace it to move time by hand."""
    return _utc_now


def get_tool_registry() -> Mapping[str, InternalTool]:
    """The tools Domo may call, by the name the model uses (``tools.registry.TOOLS``). Tests
    replace it to add a tool of their own."""
    return TOOLS


def get_policy() -> PolicyTables:
    """The tables the policy barrier decides from (``policy.catalog.DEFAULT_POLICY``). Tests
    replace it to try other levels or rights."""
    return DEFAULT_POLICY


async def policy_now(
    session: Annotated[AsyncSession, Depends(get_session)],
    base: Annotated[PolicyTables, Depends(get_policy)],
) -> PolicyTables:
    """The tables of ``get_policy`` with the kinds of the open catalog laid over them, as the
    database holds them now (``policy.open_catalog.load_policy``): for a page whose request may
    meet one of them (a right on one of Domo's own tools). Nothing is written: the read ends
    its transaction, so that the request's session holds no connection while it goes on."""
    policy = await load_policy(session, base)
    await session.commit()
    return policy


async def current_person(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> Person:
    """The person whose valid session cookie the request carries, and who is still in the
    household (not removed, not a guest whose stay is over, at the time of ``get_clock``);
    otherwise ``LoginRequired`` (303 to ``/login``, or 401 with ``HX-Redirect`` for htmx
    requests). So someone removed, or whose stay ended, is stopped at their next request.

    A request that says which person its page was rendered for (``PAGE_PERSON_HEADER``) is served
    for that person only: when the session is someone else's now, ``AnotherPersonsPage`` (a page
    left open would otherwise show one person's conversation and act as another). A request that
    does not say it (a direct call, a stream) is served for the session's person."""
    raw_token = request.cookies.get(SESSION_COOKIE)
    person = await resolve_session(session, raw_token, clock()) if raw_token else None
    if person is None:
        raise LoginRequired
    rendered_for = request.headers.get(PAGE_PERSON_HEADER)
    if rendered_for is not None and rendered_for != str(person.id):
        raise AnotherPersonsPage
    # Commit now: it makes the ``last_seen_at`` update durable, whatever the endpoint does after.
    await session.commit()
    return person


async def require_owner(person: Annotated[Person, Depends(current_person)]) -> Person:
    """The logged-in person, who must be the owner (403 otherwise)."""
    if person.role != "owner":
        raise HTTPException(status_code=403)
    return person


def require_action(action_type: str) -> Callable[..., Awaitable[Person]]:
    """A dependency for a page that is for whoever may do ``action_type``: the logged-in person,
    if the owner's rights, read now, resolve the action as allowed for them (``RightsTable.resolve``
    on top of the catalog ``get_policy`` gives: the owner by default, whoever the owner gave the
    right to, and not someone it was taken away from); 403 otherwise. It is the same reading as
    the tools a person is shown (``tools.registry.tools_for``), which is also what the barrier
    starts from. Only the right is read, not what is asked or who says yes: the page is the person's
    own act in the page, as the owner's was, not an action of Domo."""

    async def dependency(
        person: Annotated[Person, Depends(current_person)],
        session: Annotated[AsyncSession, Depends(get_session)],
        policy: Annotated[PolicyTables, Depends(get_policy)],
    ) -> Person:
        rights = await load_rights(session)
        actor = Actor(person_id=person.id, role=as_role(person.role))
        if not rights.resolve(actor, action_type, policy).allowed:
            raise HTTPException(status_code=403)
        return person

    return dependency
