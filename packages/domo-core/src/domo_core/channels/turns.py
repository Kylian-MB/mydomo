"""What a turn needs of the application, for every channel: the page's routes
(``channels.page.routes``) and the API's (``channels.api.routes``) read it alike, and so does what
is no request's turn (``confirmations.routes``: the decisions; the upkeep). And the core told that
someone is there, whatever channel they are on (``person_is_there``).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from domo_core.accounts.models import Person
from domo_core.config import CoreSettings
from domo_core.conversation.prepare import TurnDeps, made_tool_finder
from domo_core.conversation.service import Conversation
from domo_core.gatekeeper_client import GatekeeperClient
from domo_core.llm.factory import ModelFactory
from domo_core.llm.listing import ModelLister
from domo_core.memory.embedder import NO_EMBEDDER
from domo_core.policy.catalog import PolicyTables
from domo_core.texts.card_heads import card_head_writing
from domo_core.texts.scheduler import TextWriting
from domo_core.tools.registry import InternalTool
from domo_core.web.deps import (
    SessionFactory,
    gatekeeper_of,
    get_clock,
    get_gatekeeper,
    get_model_factory,
    get_model_lister,
    get_policy,
    get_session_factory,
    get_settings,
    get_streaming_replies,
    get_tool_registry,
    model_factory_of,
    model_lister_of,
    sandbox_of,
    tools_base_of,
)

logger = logging.getLogger(__name__)


def turn_deps_of(
    app: FastAPI,
    *,
    session_factory: SessionFactory,
    model_factory: ModelFactory,
    model_lister: ModelLister,
    streaming_replies: set[uuid.UUID],
    settings: CoreSettings,
    gatekeeper: GatekeeperClient,
    clock: Callable[[], datetime],
    registry: Mapping[str, InternalTool],
    policy: PolicyTables,
) -> TurnDeps:
    """What every turn needs of ``app``, with these: the writing of people's texts and of the heads
    of the cards (``app.state.text_writing``), the tasks the turns run in (``app.state.wakes``),
    the arrival turns that run (``app.state.arrival_turns``: none where it is ``None``), whoever
    listens (``app.state.listeners``), what people wrote while a turn of theirs ran
    (``app.state.kept``), and the local model of the meanings of memories
    (``app.state.embedder``: none where the lifespan made none), the sandbox
    (``web.deps.sandbox_of``: one that is always down where the lifespan made none) and the tools'
    own database (``web.deps.tools_base_of``: none where the lifespan made none), and the channels
    told of what happens to the records (``app.state.record_listeners``: the application's one
    instance, set with the other application-wide objects)."""
    state = app.state
    text_writing: TextWriting | None = getattr(state, "text_writing", None)
    return TurnDeps(
        session_factory=session_factory,
        model_factory=model_factory,
        model_lister=model_lister,
        streaming_replies=streaming_replies,
        settings=settings,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=registry,
        policy=policy,
        text_writing=text_writing,
        wakes=state.wakes,
        kept=state.kept,
        card_heads=card_head_writing(text_writing, session_factory),
        arrival_turns=getattr(state, "arrival_turns", None),
        listeners=getattr(state, "listeners", None),
        embedder=getattr(state, "embedder", NO_EMBEDDER),
        sandbox=sandbox_of(app),
        tools_base=tools_base_of(app),
        record_listeners=state.record_listeners,
    )


def get_turn_deps(
    request: Request,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    model_factory: Annotated[ModelFactory, Depends(get_model_factory)],
    model_lister: Annotated[ModelLister, Depends(get_model_lister)],
    streaming_replies: Annotated[set[uuid.UUID], Depends(get_streaming_replies)],
    settings: Annotated[CoreSettings, Depends(get_settings)],
    gatekeeper: Annotated[GatekeeperClient, Depends(get_gatekeeper)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    registry: Annotated[Mapping[str, InternalTool], Depends(get_tool_registry)],
    policy: Annotated[PolicyTables, Depends(get_policy)],
) -> TurnDeps:
    """What every turn needs of the application, read as each request reads its dependencies (a
    test may replace them while the application runs)."""
    return turn_deps_of(
        request.app,
        session_factory=session_factory,
        model_factory=model_factory,
        model_lister=model_lister,
        streaming_replies=streaming_replies,
        settings=settings,
        gatekeeper=gatekeeper,
        clock=clock,
        registry=registry,
        policy=policy,
    )


def get_conversation(deps: Annotated[TurnDeps, Depends(get_turn_deps)]) -> Conversation:
    """The conversation of the application (``turn_deps_of``)."""
    return Conversation(deps)


def app_turn_deps(app: FastAPI) -> TurnDeps:
    """What every turn needs of ``app`` (``turn_deps_of``), read as a request reads its
    dependencies (a test may replace them while the application runs), for what is not a request's
    turn: the core told that someone is there, the upkeep's resume of a chain. The gatekeeper is
    read only now."""
    overrides = app.dependency_overrides
    state = app.state
    return turn_deps_of(
        app,
        session_factory=state.session_factory,
        model_factory=model_factory_of(app),
        model_lister=model_lister_of(app),
        streaming_replies=state.streaming_replies,
        settings=state.settings,
        gatekeeper=gatekeeper_of(app),
        clock=overrides.get(get_clock, get_clock)(),
        registry=overrides.get(get_tool_registry, get_tool_registry)(),
        policy=overrides.get(get_policy, get_policy)(),
    )


def app_made_tool_finder(app: FastAPI) -> Callable[[str], Awaitable[InternalTool | None]]:
    """What finds a tool of Domo's for a yes given outside a turn (``made_tool_finder``), with
    the application's deps read when it is called (``app_turn_deps``)."""

    async def find(name: str) -> InternalTool | None:
        return await made_tool_finder(app_turn_deps(app))(name)

    return find


def person_is_there(
    app: FastAPI, person: Person, now: datetime, *, came_back: bool = False
) -> None:
    """Tell the core that ``person`` is there, or came back (``Conversation.seen``), for any
    channel: the page's asks, a stream of the API. Nothing where the application offers Domo no
    arrival turns (``app.state.arrival_turns``): there is nothing to weigh then, and what a turn
    needs (the gatekeeper) is never read. It never fails what the channel answers: what goes wrong
    is logged, by its kind alone (its message may hold what the database said)."""
    if getattr(app.state, "arrival_turns", None) is None:
        return
    try:
        Conversation(app_turn_deps(app)).seen(person, now=now, came_back=came_back)
    except Exception as error:
        logger.error("The core could not be told that someone is there (%s)", type(error).__name__)
