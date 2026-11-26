"""The core's web application: ``create_app`` builds it, the other tasks plug their routers in.

The database engine, the session factory and the gatekeeper client are created when the
application starts (the lifespan) and closed when it stops, and are kept on ``app.state``;
``get_session`` hands out sessions from that factory, and so does the setup gate. The core's token
is read from its file at that moment: without it the application does not start. Tests that bypass
the lifespan put a session factory of their own on ``app.state``.

The lifespan also runs the core's periodic upkeep (``upkeep.run_upkeep``, the scheduler the
architecture plans) in a task of the application, every ``CoreSettings.upkeep_seconds``: its
passes erase the notes of the guests who left (``notes.upkeep``), pause the rights of the people
who left (``household.upkeep``, spec 5.7), drop the corners of the tools' database of the people
who were removed (``toolsdb.spaces.corners_pass``, the database ``app.state.tools_base`` reaches,
made by the lifespan and closed with it), resume, or stop, the chains paused until someone's
arrival (``confirmations.arrivals``, spec 4.3b), settle the reservations of the calls to a
model that never said what they cost (``upkeep.stale_reservations_pass``), and read again, through
the gatekeeper, the models of the keys in use (``upkeep.model_lists_pass``) and the real spending
of the keys with an admin key (``upkeep.real_spend_pass``), compute the vectors the memories
lack with the local model (``upkeep.memory_vectors_pass``, ``app.state.embedder``, made by the
lifespan; a turn finds it in ``TurnDeps.embedder``), start going back over the conversations that
are quiet (``memory.review.review_pass``, with what a turn needs of the application as the lifespan
finds it), wake Domo for the plans whose time came (``plans.upkeep.plans_pass``, the same) and
deliver the runs of his tools a restart left (``conversation.tool_wakes.tool_wakes_pass``, the
same), build the tools he opened a build of (``madetools.upkeep.toolmaking_pass``, the same) and
launch his tools whose time came or that wait for someone to join
(``madetools.triggers.triggers_pass``, the same), give the sandbox how many programs it runs at
once (``madetools.places.places_pass``, Domo's setting);
the catalog of models and the euro's
rate are read again only when Domo judges it useful (``models_refresh``); and, for a house whose
language has no default phrases of its own, it writes again the texts missing from the house's
version (``texts.upkeep.text_gaps_pass``): at once when the core starts (the texts whose meaning a
new version of Domo changed among them), then at most every hour, once the installation is
over. It starts once the session factory exists, reads the
time the way the pages do (the ``get_clock`` dependency, so a test that replaces it moves the
upkeep's time too), and is cancelled, and waited for, before the wakes are stopped and the HTTP
client and the engine are closed. Nothing it meets ends it, and it cannot hold the shutdown for
more than a few seconds. What a resumed chain needs of the application is read again at each
pass, as a request reads its dependencies (``arrival_deps``), and so are the gatekeeper, the
lister and the factory of the models that the reads of the world and the writing of the texts use
(``web.deps.gatekeeper_of``, ``model_lister_of``, ``model_factory_of``).

The turns Domo starts himself (``conversation.starts``: a wake after a button, an arrival, a news
turn) are tasks of the application too, kept on ``app.state.wakes`` (created with the application,
so that the tests that bypass the lifespan have them), and so are the resume of a chain after an
arrival, the writings of the house's texts, one at a time (``app.state.house_writing``,
``texts.install.HouseWriting``: the upkeep's), and the writings of a person's texts after a page
showed them (when Domo has a reason to), and of the version of a new language
(``app.state.text_writing``, ``texts.scheduler.TextWriting``, on the person's keys, on those of the
one who invited, or on the house's key: with what the application's database, factory of models and
clock are when each one runs). The arrival turns that run are kept on ``app.state.arrival_turns``,
so that a message of the person stops theirs, whoever listens to a person on
``app.state.listeners`` (``conversation.listeners``): what Domo says on his own goes to them, and
what people wrote while a turn of theirs ran on ``app.state.kept`` (``conversation.kept``). The
lifespan stops the tasks that still run in the same way, before the HTTP client (a wake may be
talking to a model through the gatekeeper) and the engine are closed.

Every request has its texts before its route runs (``web.texts_middleware``), and so has an error
page. The channel of the API (``channels.api``) has no page: its errors, a route it does not have
and an error of the server included, are JSON (``{"error": <code>}``). The manifest of the web
app (``MANIFEST_PATH``) is served here, its description in the house's language.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Final

import anyio
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response

from domo_core.accounts import account_routes as account_page_routes
from domo_core.accounts import passkey_routes
from domo_core.accounts import routes as account_routes
from domo_core.accounts.throttle import LoginThrottle
from domo_core.audit import routes as audit_routes
from domo_core.channels import turns
from domo_core.channels.api import is_api_path
from domo_core.channels.api import routes as api_routes
from domo_core.channels.api.auth import ApiRefusal, refusal, refusal_response
from domo_core.channels.page import key_routes as page_key_routes
from domo_core.channels.page import routes as page_routes
from domo_core.config import CoreSettings
from domo_core.confirmations import routes as confirmation_routes
from domo_core.confirmations.arrivals import arrivals_pass
from domo_core.confirmations.expiring import expiring_pass
from domo_core.conversation.kept import KeptMessages
from domo_core.conversation.listeners import Listeners
from domo_core.conversation.prepare import TurnDeps
from domo_core.conversation.starts import Wakes
from domo_core.conversation.tool_wakes import tool_wakes_pass
from domo_core.conversation.visits import ArrivalTurns
from domo_core.db import SessionFactory, create_engine, load_models, make_session_factory
from domo_core.deliveries.service import DeliveryNotes
from domo_core.gatekeeper_client import GatekeeperClient, load_core_token, new_http_client
from domo_core.household import routes as household_routes
from domo_core.household.upkeep import departures_pass
from domo_core.madetools.places import places_pass
from domo_core.madetools.triggers import triggers_pass
from domo_core.madetools.upkeep import toolmaking_pass
from domo_core.memory.embedder import LocalEmbedder
from domo_core.memory.review import review_pass
from domo_core.naming import DEFAULT_NAME, name_shown
from domo_core.notes.upkeep import notes_pass
from domo_core.plans.upkeep import plans_pass
from domo_core.policy.settings import load_policy_settings
from domo_core.sandbox_client import SandboxClient
from domo_core.sandbox_client import new_http_client as new_sandbox_http_client
from domo_core.setup_flow import model_routes, owner_routes
from domo_core.setup_flow.middleware import SetupGateMiddleware, setup_is_done
from domo_core.texts.install import HouseWriting, house_models
from domo_core.texts.scheduler import TextWriting, reader_models
from domo_core.texts.upkeep import text_gaps_pass
from domo_core.texts.view import recueil_for
from domo_core.toolsdb.setup import ToolsAdmin, asyncpg_dsn
from domo_core.toolsdb.spaces import corners_pass
from domo_core.toolsdb.store import ToolsBase
from domo_core.upkeep import (
    memory_vectors_pass,
    model_lists_pass,
    real_spend_pass,
    run_upkeep,
    stale_reservations_pass,
)
from domo_core.web.deps import (
    AnotherPersonsPage,
    LoginRequired,
    gatekeeper_of,
    get_clock,
    model_factory_of,
    model_lister_of,
    sandbox_of,
)
from domo_core.web.language import request_texts
from domo_core.web.redirects import redirect_to
from domo_core.web.security import (
    SecurityMiddleware,
    apply_cache_control,
    apply_security_headers,
)
from domo_core.web.templating import create_templates, render
from domo_core.web.texts_middleware import TextsMiddleware, ensure_texts

STATIC_DIR = Path(__file__).parent / "static"

# Where the manifest of the web app is served, and what it says besides its description.
MANIFEST_PATH: Final = "/manifest.webmanifest"
MANIFEST: Final[Mapping[str, object]] = {
    "name": "Domo",
    "short_name": "Domo",
    "start_url": "/chat",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f4f6f9",
    "theme_color": "#1f5fbf",
    "icons": [
        {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
    ],
}

# How long the lifespan waits for the upkeep to stop once it is cancelled, in seconds. It stops at
# once unless it is in the middle of a database call; past that, it is left behind rather than
# holding up the shutdown.
UPKEEP_STOP_SECONDS: Final = 5.0
# How long the lifespan waits for the wakes of Domo to stop once they are cancelled, in seconds:
# what a turn stores at its end is shielded; past that, it is left behind.
WAKE_STOP_SECONDS: Final = 5.0


def _error_page(
    request: Request, status_code: int, headers: Mapping[str, str] | None = None
) -> Response:
    if status_code == 403:
        kind = "forbidden"
    elif status_code == 404:
        kind = "not_found"
    elif status_code >= 500:
        kind = "server"
    else:
        kind = "generic"
    return render(request, "error.html", status_code=status_code, headers=headers, kind=kind)


# The codes of the errors of the channel of the API, by status.
_API_ERRORS: Final[Mapping[int, str]] = {
    401: "unknown_device",
    403: "forbidden",
    404: "not_found",
    405: "not_allowed",
}


def _api_error(status_code: int, headers: Mapping[str, str] | None = None) -> Response:
    """An error of the channel of the API, as JSON."""
    error = "server" if status_code >= 500 else _API_ERRORS.get(status_code, "bad_request")
    return refusal(status_code, error, headers)


async def _http_error(request: Request, exc: Exception) -> Response:
    if is_api_path(request.url.path):
        if isinstance(exc, StarletteHTTPException):
            return _api_error(exc.status_code, exc.headers)
        return _api_error(500)
    # A static file not found has no texts yet: they are read now.
    await ensure_texts(request)
    if isinstance(exc, StarletteHTTPException):
        return _error_page(request, exc.status_code, exc.headers)
    return _error_page(request, 500)


async def _unexpected_error(request: Request, exc: Exception) -> Response:
    # This handler runs outside the security middleware, so the headers are set here too.
    # Starlette re-raises the exception afterwards, for the server to log; its message is not
    # shown to the visitor.
    if is_api_path(request.url.path):
        response = _api_error(500)
    else:
        await ensure_texts(request)
        response = _error_page(request, 500)
    apply_security_headers(response.headers)
    apply_cache_control(response.headers, request.url.path)
    return response


async def _login_required(request: Request, exc: Exception) -> Response:
    return redirect_to(request.headers, account_routes.LOGIN_PATH)


async def _another_persons_page(request: Request, exc: Exception) -> Response:
    # Nothing was done. ``HX-Refresh`` makes htmx reload the tab in full, whatever the status;
    # the page's own requests read it too (``chat.js``).
    return Response(status_code=409, headers={"HX-Refresh": "true"})


def arrival_deps(app: FastAPI) -> Callable[[], TurnDeps]:
    """What the resume of a chain paused until someone's arrival needs of ``app``
    (``confirmations.arrivals.arrivals_pass``), read at each call as a request reads its
    dependencies (a test may replace them while the application runs,
    ``channels.turns.app_turn_deps``): its database, the model's factory, its lister of models and
    the gatekeeper, the tools, the catalog and the clock. No address of a request: no link is made
    in a resumed chain (each step leaves what it makes behind its button,
    ``ToolContext.in_a_chain``)."""

    def deps() -> TurnDeps:
        return turns.app_turn_deps(app)

    return deps


async def _manifest(request: Request) -> Response:
    """The manifest of the web app: its description is the house's version of it once Domo is
    installed (the fixed catalog's during the installation), in the house's language."""
    app = request.app
    name = DEFAULT_NAME
    if not await setup_is_done(app):
        texts = request_texts(request)
        description, language = texts("manifest.description"), texts.language
    else:
        session_factory: SessionFactory = app.state.session_factory
        async with session_factory() as session:
            settings = await load_policy_settings(session)
            language = settings.house_language
            name = name_shown(None, settings.house_domo_name)
            house = await recueil_for(session, person=None, language=language, settings=settings)
        description = house("manifest.description")
    return JSONResponse(
        {
            **MANIFEST,
            "name": name,
            "short_name": name,
            "lang": language,
            "description": description,
        },
        media_type="application/manifest+json",
    )


def create_app(settings: CoreSettings) -> FastAPI:
    """The web application for ``settings``. The database is only opened when it starts.

    Every module that declares tables is imported first: a table refers to another by its name
    (``message_actions.confirmation_id`` to ``confirmations``, say), and SQLAlchemy can only
    resolve the name of a table whose module was imported.
    """
    load_models()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # The token first: without it the application cannot start, and nothing is open yet.
        token = load_core_token(settings.core_token_file)
        engine = create_engine(settings)
        http = new_http_client()
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.gatekeeper = GatekeeperClient(settings.gatekeeper_url, token, http)
        # Nothing connects to the sandbox now: the core serves whether or not it is up.
        sandbox_http = new_sandbox_http_client(settings.sandbox_socket)
        app.state.sandbox = SandboxClient(sandbox_http)
        # The local model of the meanings, loaded in the background from now on: no turn ever
        # waits for it (its searches go by the words until it is there).
        app.state.embedder = LocalEmbedder(settings.embedding_model_dir)
        app.state.embedder.warm()
        # The tools' own database: nothing connects to it until a program asks.
        tools_admin = ToolsAdmin(asyncpg_dsn(settings.database_url), settings.tools_database)
        app.state.tools_base = ToolsBase(tools_admin)

        def now() -> datetime:
            # Read at each pass: the application's clock may be replaced while it runs (tests).
            return app.dependency_overrides.get(get_clock, get_clock)()()

        passes = [
            notes_pass(app.state.session_factory),
            departures_pass(app.state.session_factory),
            corners_pass(app.state.session_factory, tools_admin),
            arrivals_pass(arrival_deps(app), app.state.wakes),
            # The requests past their lifetime are expired as they lapse, and the assistant
            # is woken once for those about to: each wake runs in a task of the wakes.
            expiring_pass(arrival_deps(app), app.state.wakes),
            stale_reservations_pass(app.state.session_factory),
            # It only starts the reviews due: each one runs in a task of the wakes.
            review_pass(turns.app_turn_deps(app)),
            # It only starts the turns of the plans due: each one runs in a task of the wakes.
            plans_pass(turns.app_turn_deps(app)),
            # The runs of Domo's tools a restart left undelivered: each wake runs in a task of
            # the wakes.
            tool_wakes_pass(turns.app_turn_deps(app)),
            # It only starts the builds of Domo's tools: each one's rounds run in a task of the
            # wakes.
            toolmaking_pass(turns.app_turn_deps(app)),
            # It only claims the triggers of Domo's tools that are due: their launches run in a
            # task of the wakes.
            triggers_pass(turns.app_turn_deps(app)),
            # Domo's ``places`` to the sandbox, which takes it without a restart: at once as
            # the core starts, and again each round for a sandbox that started again since.
            places_pass(app.state.session_factory, lambda: sandbox_of(app)),
            # After the reviews and the plans, which it must never hold back: a bounded number of
            # vectors per round (``upkeep.VECTORS_PER_PASS``).
            memory_vectors_pass(app.state.session_factory, app.state.embedder),
            # It only begins the writing: the texts are written in the background.
            text_gaps_pass(
                app.state.session_factory,
                house_models(app.state.session_factory, lambda: model_factory_of(app), now),
                writing=app.state.house_writing,
                now=now,
            ),
            # The reads of the world come last: they wait on the network.
            model_lists_pass(
                app.state.session_factory,
                lambda: gatekeeper_of(app),
                lambda: model_lister_of(app),
            ),
            real_spend_pass(app.state.session_factory, lambda: gatekeeper_of(app), http),
        ]
        upkeep = asyncio.create_task(run_upkeep(passes, now, every=settings.upkeep_seconds))
        try:
            yield
        finally:
            upkeep.cancel()
            # Waited for, within a limit; the shield lets this finish even when it is the
            # lifespan itself that is being cancelled.
            with anyio.move_on_after(UPKEEP_STOP_SECONDS, shield=True):
                await asyncio.wait({upkeep})
            await app.state.wakes.stop(WAKE_STOP_SECONDS)
            await app.state.embedder.stop()
            await app.state.tools_base.close()
            await http.aclose()
            await sandbox_http.aclose()
            await engine.dispose()

    # No API documentation or schema: the interface is the pages.
    app = FastAPI(title="Domo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.templates = create_templates()
    app.state.login_throttle = LoginThrottle()
    # The password a signed-in person types again (account page, adding a passkey) is counted per
    # account, apart from the login (see ``accounts.account_routes``).
    app.state.password_check_throttle = LoginThrottle()
    app.state.setup_code_throttle = owner_routes.new_setup_code_throttle()
    # Joining with an invitation is held back like a login (see ``household.routes``).
    app.state.invitation_throttle = LoginThrottle()
    # Passkey logins have the login's limits but a throttle of their own, and the login options
    # are limited per client address (see ``accounts.passkey_routes``).
    app.state.passkey_login_throttle = LoginThrottle()
    app.state.passkey_options_throttle = passkey_routes.new_passkey_options_throttle()
    # Trading a code for a device is held back like a login, per client address
    # (``channels.api.routes``).
    app.state.pairing_throttle = LoginThrottle()
    app.state.streaming_replies = set()
    # What people wrote while a turn of theirs ran, for that turn or the one after it.
    app.state.kept = KeptMessages()
    app.state.wakes = Wakes()
    # The arrival turns that run, by their reply: a message the person says stops theirs.
    app.state.arrival_turns = ArrivalTurns()
    app.state.house_writing = HouseWriting(app.state.wakes.spawn)

    def now() -> datetime:
        # Read at each call: the application's clock may be replaced while it runs (tests).
        return app.dependency_overrides.get(get_clock, get_clock)()()

    def session() -> AbstractAsyncContextManager[AsyncSession]:
        # The application's database as it is at each call: the lifespan makes it, a test may put
        # its own.
        return app.state.session_factory()

    app.state.text_writing = TextWriting(
        app.state.wakes, session, reader_models(session, lambda: model_factory_of(app), now), now
    )
    # Whoever listens to a person: what Domo says on his own goes to them, and what reached
    # them is noted apart (``deliveries``).
    app.state.listeners = Listeners(DeliveryNotes(app.state.wakes.spawn, session, now))

    # The last one added is the outermost: the security checks (headers, Origin) come first, then
    # the setup gate, then the texts of the request, then the routes.
    app.add_middleware(TextsMiddleware)
    app.add_middleware(SetupGateMiddleware)
    app.add_middleware(SecurityMiddleware)
    app.add_exception_handler(LoginRequired, _login_required)
    app.add_exception_handler(AnotherPersonsPage, _another_persons_page)
    app.add_exception_handler(ApiRefusal, refusal_response)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return RedirectResponse(account_routes.HOME_PATH, status_code=303)

    app.add_api_route(MANIFEST_PATH, _manifest, methods=["GET"], include_in_schema=False)

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness, for the container's health check: the process answers. It does not look at
        the database, and the setup gate lets it through."""
        return {"status": "ok"}

    app.include_router(account_routes.router)
    app.include_router(account_page_routes.router)
    app.include_router(passkey_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(page_routes.router)
    app.include_router(page_key_routes.router)
    app.include_router(confirmation_routes.router)
    app.include_router(household_routes.router)
    app.include_router(owner_routes.router)
    app.include_router(model_routes.router)
    app.include_router(api_routes.router)
    return app
