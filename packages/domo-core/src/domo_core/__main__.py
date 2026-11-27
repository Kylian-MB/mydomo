"""Entry point of the core container: ``python -m domo_core``.

The start-up order matters:

1. The database is migrated to the latest revision. Alembic runs an event loop of its own, so
   this happens synchronously, before this process has one.
2. The tools' own database is made, or brought back to what it must be (``toolsdb.setup``).
3. A new one-time setup code is issued, in its own committed transaction, when the instance has
   no owner yet. It is written to the logs (``docker compose logs core``) as
   ``Domo setup code: XXXX-XXXX-XXXX``; nowhere else. Each start replaces the previous code.
4. The web server starts. It writes an access log, without the container's health checks, and
   believes the ``X-Forwarded-*`` headers only from ``DOMO_TRUSTED_PROXIES``. The core's URLs
   carry no secret but one: the token of an invitation link (``/invite/<token>``), which the log
   hides (``InvitationLinkFilter``). The other secrets travel in form bodies.
"""

import asyncio
import logging
import sys

import uvicorn
from domo_common.startup import describe_settings_error, pin_loggers_at_warning
from pydantic import ValidationError

from domo_core.channels.voice.protocol import MAX_FRAME_BYTES
from domo_core.config import ConfigurationError, CoreSettings
from domo_core.db import create_engine, make_session_factory
from domo_core.gatekeeper_client import load_core_token
from domo_core.migrate import upgrade_to_head
from domo_core.setup_flow.code import rotate_setup_code
from domo_core.toolsdb.setup import (
    PREPARE_ERRORS,
    asyncpg_dsn,
    database_of,
    describe_failure,
    prepare_tools_database,
)
from domo_core.web.app import create_app

HOST = "0.0.0.0"  # inside the container: only the edge proxy's network reaches it
PORT = 8000
# The biggest WebSocket message the server assembles: a little above the frame the voice socket
# takes (``channels.voice.protocol.MAX_FRAME_BYTES``), so that a bigger one is refused before it
# is held whole (the server's own default is 16 MiB).
WS_MAX_SIZE = MAX_FRAME_BYTES + 4 * 1024

# Not ``__main__``: the name shows in every line of the log.
logger = logging.getLogger("domo_core.startup")


# The HTTP libraries and the model SDKs log requests, and at DEBUG their headers: the core's calls
# carry its token in one. They stay at WARNING or above, whatever the root says.
HTTP_LOGGERS = ("httpcore", "httpcore2", "httpx", "httpx2", "openai", "anthropic", "google_genai")


def pin_http_loggers() -> None:
    """Keep the loggers of the HTTP libraries and SDKs at WARNING or above (a logger that is
    already stricter is left alone)."""
    pin_loggers_at_warning(HTTP_LOGGERS)


# The path the container's health check calls (see the Dockerfile).
HEALTH_PATH = "/healthz"


class HealthCheckFilter(logging.Filter):
    """Keeps the health checks (a request to ``/healthz`` every few seconds) out of the access
    log, where they would bury the lines that matter, the setup code among them.

    It reads the records uvicorn's access logger writes, whose arguments are the client, the
    method, the path with its query string, the HTTP version and the status. Any other record goes
    through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            return args[2].partition("?")[0] != HEALTH_PATH
        return True


# The path of an invitation link, whose last part is the invitation's token: a secret until it is
# used (see ``household.routes``).
INVITE_PREFIX = "/invite/"
HIDDEN_INVITE_PATH = INVITE_PREFIX + "[hidden]"


class InvitationLinkFilter(logging.Filter):
    """Keeps the token of an invitation link out of the access log: a request to
    ``/invite/<token>`` is logged as ``/invite/[hidden]`` (its query string too is left out).

    It reads the records uvicorn's access logger writes, like ``HealthCheckFilter``, and rewrites
    their path; any other record goes through as it is. It never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and args[2].startswith(INVITE_PREFIX)
        ):
            record.args = (*args[:2], HIDDEN_INVITE_PATH, *args[3:])
        return True


def configure_logging() -> None:
    """Log to stderr (where ``docker compose logs`` finds it), at INFO and above, without the
    health checks in the access log, nor the token of an invitation link."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    pin_http_loggers()
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, HealthCheckFilter) for existing in access.filters):
        access.addFilter(HealthCheckFilter())
    if not any(isinstance(existing, InvitationLinkFilter) for existing in access.filters):
        access.addFilter(InvitationLinkFilter())


async def prepare_tools(settings: CoreSettings) -> None:
    """The tools' own database, made or brought back to what it must be (``toolsdb.setup``), on
    the server of the core's database, as the core's own user."""
    url = settings.database_url
    await prepare_tools_database(
        asyncpg_dsn(url), core_database=database_of(url), name=settings.tools_database
    )


async def issue_setup_code(settings: CoreSettings) -> str | None:
    """A new setup code, stored (committed) for whoever claims the instance; ``None`` when the
    instance already has its owner."""
    engine = create_engine(settings)
    try:
        async with make_session_factory(engine)() as session:
            code = await rotate_setup_code(session)
            await session.commit()
            return code
    finally:
        await engine.dispose()


def announce_setup_code(code: str) -> None:
    """Write the code to the log, at WARNING so that no log level setting hides it. The end-to-end
    test, and the README, look for this exact line."""
    logger.warning("The assistant setup code: %s", code)


def main() -> None:
    configure_logging()
    try:
        settings = CoreSettings()
    except ValidationError as exc:
        sys.exit(f"domo-core: invalid configuration: {describe_settings_error(exc)}")
    try:
        # Checked first, for a clear message instead of a failed start-up after the migration (the
        # application reads the token again when it starts).
        load_core_token(settings.core_token_file)
        upgrade_to_head(settings.database_url)
        try:
            asyncio.run(prepare_tools(settings))
        except PREPARE_ERRORS as exc:
            sys.exit(
                "domo-core: the tools' database could not be prepared "
                f"({describe_failure(exc)}). Check that the database server is up and that the "
                "core's database user is a superuser of it."
            )
        code = asyncio.run(issue_setup_code(settings))
    except ConfigurationError as exc:
        sys.exit(f"domo-core: {exc}")
    if code is not None:
        announce_setup_code(code)
    uvicorn.run(
        create_app(settings),
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        access_log=True,
        ws_max_size=WS_MAX_SIZE,
        # The root logger's setup (see ``configure_logging``) applies to uvicorn's records.
        log_config=None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
