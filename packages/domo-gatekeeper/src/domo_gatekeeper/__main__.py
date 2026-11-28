"""Entry point of the gatekeeper container: ``python -m domo_gatekeeper``.

One process runs two web servers in the same event loop: the internal API (for the core, on the
internal network only) and the public app (the one-time vault form, reached through the HTTPS
edge proxy). Both write no access log: the path of the vault form carries a one-time token.

The startup order matters. The core's token is read first, so that a missing file stops the
start before anything is created; then the master key is loaded (created only for an empty
vault, see ``load_or_create_master_key``), then the stores are opened, and what a program may
reach is read once (``reach``), for the relay of programs (``out``).
"""

import asyncio
import contextlib
import logging
import signal
import sys
import threading
from collections.abc import Callable, Generator, Sequence
from typing import Any, Protocol

import httpx
import uvicorn
from domo_common.startup import describe_settings_error, pin_loggers_at_warning
from fastapi import FastAPI
from pydantic import ValidationError

from domo_gatekeeper.config import GatekeeperSettings, load_core_token
from domo_gatekeeper.errors import (
    ConfigurationError,
    MasterKeyCorruptError,
    MasterKeyMissingError,
    ServerStartupError,
)
from domo_gatekeeper.internal_api import create_internal_app
from domo_gatekeeper.master_key import load_or_create_master_key
from domo_gatekeeper.out import ProgramRelay, create_out_ssl_context
from domo_gatekeeper.phrases import PhraseStore
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.public_app import create_public_app
from domo_gatekeeper.reach import describe, reach_from
from domo_gatekeeper.secret_requests import SecretRequestStore
from domo_gatekeeper.sessions import Sessions
from domo_gatekeeper.vault import Vault

logger = logging.getLogger("domo_gatekeeper")

# httpcore logs the response headers of every call at DEBUG, before they are scrubbed, and
# httpx logs every URL at INFO: neither belongs in the logs of the one component that holds keys.
HTTP_LOGGERS = ("httpcore", "httpx")
# uvicorn waits for open connections when it stops; Docker kills the container after 10 s anyway.
_GRACEFUL_SHUTDOWN_SECONDS = 8


def pin_http_loggers() -> None:
    """Keep the ``httpcore`` and ``httpx`` loggers at WARNING or above, whatever the root says.

    A logger that is already stricter is left alone.
    """
    pin_loggers_at_warning(HTTP_LOGGERS)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    pin_http_loggers()


def create_http_client() -> httpx.AsyncClient:
    """The client the relay calls providers with.

    Long streamed completions need a long read timeout; the other timeouts keep a dead provider
    from holding the gatekeeper. No redirects (they would carry the injected key along) and
    nothing from the environment: no proxy, no ``.netrc`` credentials, no extra CA bundle.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
        follow_redirects=False,
        trust_env=False,
    )


def open_stores(
    settings: GatekeeperSettings,
) -> tuple[Vault, SecretRequestStore, ProviderRegistry, PhraseStore]:
    """Load (or, for an empty vault, create) the master key and open the three databases, and
    the spoken phrases, kept in the vault's file under the vault's key."""
    stored = Vault.count_secrets(settings.vault_db_path)
    key = load_or_create_master_key(settings.master_key_path, vault_has_secrets=stored > 0)
    return (
        Vault(settings.vault_db_path, key),
        SecretRequestStore(settings.requests_db_path),
        ProviderRegistry(settings.providers_db_path),
        PhraseStore(settings.vault_db_path, key),
    )


class _Servable(Protocol):
    """What ``run_servers`` needs of a server (``uvicorn.Server`` provides it)."""

    should_exit: bool

    async def serve(self) -> None: ...


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the process signals alone.

    uvicorn installs its own handlers around ``serve``. With two servers in one process each
    would replace the other's, and only the last one would hear a stop request: ``stop_on_signals``
    stops them all instead.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def _server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    return _Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            access_log=False,
            server_header=False,
            proxy_headers=False,
            # The root logger's setup (see ``configure_logging``) applies to uvicorn's records.
            log_config=None,
            log_level="info",
            lifespan="off",
            http="h11",
            ws="none",
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
        )
    )


def build_servers(
    internal: FastAPI, public: FastAPI, *, host: str, internal_port: int, public_port: int
) -> list[uvicorn.Server]:
    """The internal and the public servers, in that order."""
    return [_server(internal, host, internal_port), _server(public, host, public_port)]


async def _run(server: _Servable) -> None:
    try:
        await server.serve()
    except SystemExit:
        # uvicorn ends the interpreter when it cannot start (a port in use, say), after
        # logging why: report it as an error instead, so the other server is stopped cleanly.
        raise ServerStartupError("A web server could not start: see the log above.") from None


async def run_servers(servers: Sequence[_Servable]) -> None:
    """Run the servers until one of them stops, then stop the others.

    A server that stops by itself, fails or cannot start brings the others down: a gatekeeper
    with only one of its two servers is not one that should keep running. Being cancelled stops
    them all too. The first failure, if any, is raised once every server is down.
    """
    tasks = [asyncio.ensure_future(_run(server)) for server in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


@contextlib.contextmanager
def stop_on_signals(stop: Callable[[], None]) -> Generator[None, None, None]:
    """Call ``stop`` (in the event loop) when the process is asked to stop; undone on exit.

    Does nothing outside the main thread, the only one that can handle signals.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    names = ["SIGINT", "SIGTERM", "SIGBREAK"]  # SIGBREAK exists on Windows only
    signals = [sig for name in names if (sig := getattr(signal, name, None)) is not None]
    previous: dict[int, Any] = {}
    through_loop: set[int] = set()
    try:
        for sig in signals:
            previous[sig] = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, stop)
                through_loop.add(sig)
            except NotImplementedError:  # the Windows event loops cannot do it
                signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(stop))
        yield
    finally:
        for sig, handler in previous.items():
            if sig in through_loop:
                loop.remove_signal_handler(sig)
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)


async def serve(settings: GatekeeperSettings) -> None:
    """Start both servers and run them until they are asked to stop."""
    token = load_core_token(settings.core_token_file)
    vault, requests, providers, phrases = open_stores(settings)
    reach = reach_from(settings)
    logger.info("%s", describe(reach))
    out_relay = ProgramRelay(
        vault=vault, sessions=Sessions(), reach=reach, ssl_context=create_out_ssl_context()
    )
    async with create_http_client() as http_client:
        servers = build_servers(
            create_internal_app(
                vault=vault,
                requests=requests,
                providers=providers,
                http_client=http_client,
                core_token=token,
                phrases=phrases,
                out_relay=out_relay,
                reach=reach,
            ),
            create_public_app(requests, vault),
            host=settings.gatekeeper_host,
            internal_port=settings.gatekeeper_internal_port,
            public_port=settings.gatekeeper_public_port,
        )

        def stop() -> None:
            for server in servers:
                server.should_exit = True

        with stop_on_signals(stop):
            await run_servers(servers)


def main() -> None:
    configure_logging()
    try:
        settings = GatekeeperSettings()
    except ValidationError as exc:
        sys.exit(f"domo-gatekeeper: invalid configuration: {describe_settings_error(exc)}")
    try:
        asyncio.run(serve(settings))
    except (
        ConfigurationError,
        MasterKeyMissingError,
        MasterKeyCorruptError,
        ServerStartupError,
    ) as exc:
        sys.exit(f"domo-gatekeeper: {exc}")


if __name__ == "__main__":
    main()
