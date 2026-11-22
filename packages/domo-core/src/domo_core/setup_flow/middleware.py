"""The setup gate: until the first launch is over, every page leads to the current setup step.

A fresh instance has no owner and no model. Whatever the visitor asks for, they are sent to the
step that is next (``/setup/owner``, then ``/setup/model``); once both are done the setup pages
are closed and send the visitor to the chat.

Reachable at any stage, without asking the database:

* ``/static`` (the pages need their styles and scripts), ``/manifest.webmanifest`` (what a
  browser reads to install the page as an app), ``/healthz`` (the container's health check);
* ``/login`` and ``/logout``: an owner whose session ended during the model step must be able to
  sign in again and finish.

The channel of the API (``channels.api``) is answered 503 ``{"error": "not_set_up"}`` instead: a
program reads that answer, it follows no redirection.

Setup pages are only reachable at their own stage: the owner page once an owner exists, and the
model page before there is one, both lead to the current step. Other paths under ``/setup`` are
left to the router (a 404 while the setup is going on).

The stage only moves forward, so once the database has said ``done`` the answer is kept on the
application (``app.state.setup_done``) and the database is not asked again. What comes after the
gate and needs to know whether the first launch is over (the texts of the request) asks
``setup_is_done``: it reads the stage the gate read for the request, if it read one
(``request.state.setup_stage``), else the database.

This is a plain ASGI middleware so that it also covers the paths no route answers (``/`` and the
pages of later tasks), and it sits inside the security middleware: a cross-site request is
refused before it is redirected.
"""

from typing import Any, Final

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from domo_core.accounts.routes import HOME_PATH
from domo_core.channels.api import is_api_path
from domo_core.setup_flow.state import SETUP_PATH, STEP_PATHS, SetupStage, setup_stage
from domo_core.web.redirects import redirect_to

# The paths the gate leaves alone at any stage (and each one's sub-paths).
ALWAYS_REACHABLE: Final = ("/static", "/manifest.webmanifest", "/healthz", "/login", "/logout")

_DONE_ATTRIBUTE: Final = "setup_done"
# Where the gate leaves the stage it read for a request.
STAGE_STATE_KEY: Final = "setup_stage"


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def mark_setup_done(app: FastAPI) -> None:
    """Record that the first launch is over, so that the gate stops asking the database.

    The gate does this itself the first time it reads ``done``; this is for an application that
    is known to be installed (tests that have no setup to go through).
    """
    setattr(app.state, _DONE_ATTRIBUTE, True)


async def _read_stage(app: Any) -> SetupStage:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        return await setup_stage(session)


async def setup_is_done(app: Any, scope: Scope | None = None) -> bool:
    """Whether the first launch is over: once the database said so, it is not asked again. For a
    request (``scope``) whose stage the gate read, the stage it read."""
    if getattr(app.state, _DONE_ATTRIBUTE, False):
        return True
    read: object = None if scope is None else scope.get("state", {}).get(STAGE_STATE_KEY)
    stage = read if read is not None else await _read_stage(app)
    if stage != "done":
        return False
    mark_setup_done(app)
    return True


async def _redirect_target(app: Any, path: str, state: dict[str, Any]) -> str | None:
    """Where ``path`` must be redirected to, or ``None`` when the request goes through. The stage
    read for it is left in ``state`` (``STAGE_STATE_KEY``)."""
    if getattr(app.state, _DONE_ATTRIBUTE, False):
        return HOME_PATH if _is_under(path, SETUP_PATH) else None
    if any(_is_under(path, prefix) for prefix in ALWAYS_REACHABLE):
        return None

    stage = await _read_stage(app)
    state[STAGE_STATE_KEY] = stage
    if stage == "done":
        mark_setup_done(app)
        return HOME_PATH if _is_under(path, SETUP_PATH) else None

    current = STEP_PATHS[stage]
    if not _is_under(path, SETUP_PATH):
        return current
    for step, step_path in STEP_PATHS.items():
        if _is_under(path, step_path):
            return None if step == stage else current
    return None  # some other path under /setup: no such page, the router says so


class SetupGateMiddleware:
    """Redirects every request to the current setup step until the first launch is over (the
    channel of the API is told it is not set up)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        target = await _redirect_target(scope["app"], scope["path"], scope.setdefault("state", {}))
        if target is None:
            await self.app(scope, receive, send)
            return
        if is_api_path(scope["path"]):
            await JSONResponse({"error": "not_set_up"}, status_code=503)(scope, receive, send)
            return
        await redirect_to(Headers(scope=scope), target)(scope, receive, send)
