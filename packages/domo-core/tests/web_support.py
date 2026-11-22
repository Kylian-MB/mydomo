"""Constants and helpers shared by the web tests (fixtures live in ``conftest.py``).

They are here, and not in ``conftest.py``, because test modules cannot reliably import from a
module called ``conftest``: every tests folder of the repository has one, and with several of
them loaded in one run the name refers to whichever was imported last. The name of this module is
unique across the repository, which the import mode of pytest requires.
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from concurrent.futures import Executor, Future
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from argon2 import PasswordHasher
from domo_common.i18n import t
from domo_common.meanings import all_keys, group_of, meaning, meaning_hash, written_groups
from domo_core.accounts.models import Person
from domo_core.accounts.passkey_routes import new_passkey_options_throttle
from domo_core.accounts.passwords import hash_password
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.accounts.throttle import LoginThrottle
from domo_core.audit.models import AuditEntry
from domo_core.config import CoreSettings
from domo_core.household.service import accept_invitation, check_joining
from domo_core.policy.settings import HOUSE_LANGUAGE, update_policy_setting
from domo_core.setup_flow.owner_routes import new_setup_code_throttle
from domo_core.texts.formatting import local_date
from domo_core.texts.models import WrittenText
from domo_core.texts.store import Reader
from domo_core.web.app import create_app
from domo_core.web.deps import current_person, get_settings, require_action, require_owner
from fastapi import Depends, FastAPI
from sqlalchemy import Row, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# The owner account of the web tests.
OWNER_USERNAME = "owner"
OWNER_PASSWORD = "correct horse battery staple"
# The adult account of the account-page tests (``adult_account`` in ``conftest.py``).
ADULT_USERNAME = "adult"
ADULT_PASSWORD = OWNER_PASSWORD


def in_words(moment: date | datetime, *, time: bool = True) -> str:
    """A date as the pages of the tests show it: in English, at the time of the house (UTC, the
    house of the tests choosing no time zone), a day alone as that day; its spaces as a person
    reads them (the narrow space before "PM" is a space)."""
    return " ".join(local_date(moment, language="en", zone=ZoneInfo("UTC"), time=time).split())


def in_language(language: str, key: str, /, **params: str) -> str:
    """The text of ``key`` in the version of ``language`` the tests write
    (``write_language_version``): its meaning, marked with the language, its holes filled."""
    return f"({language}) {t(key, 'en', **params)}"


async def write_language_version(session: AsyncSession, language: str) -> None:
    """The version of ``language`` of every text, as if Domo had written it: each text its
    meaning marked with the language (``in_language``). Flushed: the caller commits when its
    session needs it."""
    groups = frozenset(written_groups())
    await session.execute(
        insert(WrittenText),
        [
            {
                "language": language,
                "key": key,
                "text": f"({language}) {meaning(key)}",
                "meaning_hash": meaning_hash(key),
                "model_id": "language-version-of-the-tests",
                "written_at": datetime.now(UTC),
            }
            for key in all_keys()
            if group_of(key) in groups
        ],
    )
    await session.flush()


async def reads_in(session: AsyncSession, person: Person | UUID, language: str) -> None:
    """``person`` reads ``language`` from now on, whose version holds every text
    (``write_language_version``); the texts written for them in the language they read before
    are gone (their own version is written again, in their new language, as they see pages).
    Flushed: the caller commits when its session needs it."""
    person_id = person if isinstance(person, UUID) else person.id
    await session.execute(update(Person).where(Person.id == person_id).values(language=language))
    await session.execute(delete(WrittenText).where(WrittenText.person_id == person_id))
    await write_language_version(session, language)


async def house_reads(session: AsyncSession, language: str) -> None:
    """The house's language becomes ``language`` (what a visitor reads), whose version holds every
    text (``write_language_version``): the choice of the first person of the household, who must
    exist. Flushed: the caller commits when its session needs it."""
    by = (
        await session.execute(select(Person.id).order_by(Person.created_at).limit(1))
    ).scalar_one()
    await update_policy_setting(
        session, key=HOUSE_LANGUAGE, value=language, by=by, now=datetime.now(UTC)
    )
    await write_language_version(session, language)


def add_protected_routes(app: FastAPI) -> None:
    """``/whoami`` (any logged-in person), ``/owner-only`` and ``/can-invite`` (whoever may invite
    someone: the owner's by default, and given like any right): pages of later tasks depend on
    ``current_person``, ``require_owner`` and ``require_action`` in the same way."""

    async def whoami(person: Annotated[Person, Depends(current_person)]) -> dict[str, str]:
        return {"id": str(person.id), "name": person.display_name, "role": person.role}

    async def owner_only(person: Annotated[Person, Depends(require_owner)]) -> dict[str, str]:
        return {"id": str(person.id)}

    async def can_invite(
        person: Annotated[Person, Depends(require_action("household.invite"))],
    ) -> dict[str, str]:
        return {"id": str(person.id)}

    app.add_api_route("/whoami", whoami, methods=["GET", "POST"])
    app.add_api_route("/owner-only", owner_only)
    app.add_api_route("/can-invite", can_invite)


async def log_in(
    client: httpx.AsyncClient, username: str = OWNER_USERNAME, password: str = OWNER_PASSWORD
) -> None:
    """Log ``client`` in (it keeps the session cookie, as a browser does)."""
    response = await client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303, response.text


async def audit_entries(session: AsyncSession) -> Sequence[Row[Any]]:
    """The audit log, oldest first, as rows with ``id``, ``actor``, ``action`` and ``details``
    (read as columns, so that what the database holds is what the test sees)."""
    result = await session.execute(
        select(AuditEntry.id, AuditEntry.actor, AuditEntry.action, AuditEntry.details).order_by(
            AuditEntry.id
        )
    )
    return result.all()


async def audit_actions(session: AsyncSession) -> list[str]:
    """The actions of the audit log, oldest first."""
    return [entry.action for entry in await audit_entries(session)]


def cookie_attributes(response: httpx.Response) -> dict[str, str]:
    """The attributes of the session cookie that ``response`` sets (flags have an empty value)."""
    header = response.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE}=")
    parts = [part.strip() for part in header.split(";")]
    return {name.lower(): value for name, _, value in (part.partition("=") for part in parts)}


def shared_session_factory(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Stands in for the application's session factory with one session that is never closed:
    the rolled-back ``session`` fixture. The pages and the setup gate then all see (and share
    the transaction of) what the test did."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


class NoWriting:
    """Stands in for ``app.state.text_writing`` (``texts.scheduler.TextWriting``) where nobody's
    texts may be written in the background: what it is asked to write is left unwritten, and no
    new language is begun."""

    def ask(
        self,
        reader: Reader,
        groups: Iterable[str],
        *,
        host: UUID | None = None,
        writing_note: str | None = None,
    ) -> None:
        return None

    async def write_now(
        self, reader: Reader, groups: Iterable[str], *, within: float, host: UUID | None = None
    ) -> bool:
        return False

    async def begin_language(
        self, language: str, groups: Iterable[str], *, host: UUID | None
    ) -> bool:
        return False

    async def idle(self) -> None:
        return None


def build_test_app(
    settings: CoreSettings, session: AsyncSession, clock: Callable[[], datetime]
) -> FastAPI:
    """The web app on the test database, using the shared rolled-back ``session`` for every
    request (and for the setup gate), with its throttles on the test clock. The first launch is
    *not* over: the gate is active (the ``app`` fixture of ``conftest.py`` marks it done).

    Nobody's texts are written in the background (``NoWriting``): a writing would use the one
    session while the test's next request does. The pages show the house's version; the tests of
    the writing run on a live server (``chat_server``). Nor does opening the chat page offer Domo
    an arrival turn (``app.state.arrival_turns``, ``conversation.starts``), for the same reason: the
    tests of the arrival run on a live server too."""
    app = create_app(settings)
    app.state.login_throttle = LoginThrottle(clock=clock)
    app.state.password_check_throttle = LoginThrottle(clock=clock)
    app.state.setup_code_throttle = new_setup_code_throttle(clock=clock)
    app.state.invitation_throttle = LoginThrottle(clock=clock)
    app.state.passkey_login_throttle = LoginThrottle(clock=clock)
    app.state.passkey_options_throttle = new_passkey_options_throttle(clock=clock)
    app.state.session_factory = shared_session_factory(session)
    app.state.text_writing = NoWriting()
    app.state.arrival_turns = None
    add_protected_routes(app)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def join_with_password(
    session: AsyncSession, *, token: str, username: str, password: str, now: datetime
) -> Person:
    """Join the household with the invitation of ``token`` as the page does, in ``session``:
    ``check_joining``, the hash of the password, then ``accept_invitation`` (with the exceptions
    of either)."""
    joining = await check_joining(
        session, token=token, username=username, password=password, now=now
    )
    return await accept_invitation(session, joining, hash_password(password), now=now)


async def count_rows(session: AsyncSession, model: Any) -> int:
    """How many rows the table of ``model`` has."""
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def count_owners(session: AsyncSession) -> int:
    """How many persons have the owner role (there is at most one)."""
    result = await session.execute(
        select(func.count()).select_from(Person).where(Person.role == "owner")
    )
    return result.scalar_one()


# --- a slow argon2, to see how requests wait for it (``slow_hash`` in ``conftest.py``) ----------

# How long a held hash waits for ``HashProbe.release`` before it gives up (a test that fails must
# not leave the threads of argon2 blocked for the next ones).
_GATE_TIMEOUT_S = 30.0


class HashProbe:
    """What a slowed argon2 saw: how many hashes were handed to its threads (``queued``: the
    limited path of ``accounts.passwords``, and only it), how many started (``started``), how many
    run now (``running``) and the most that ran at once (``max_concurrent``). Each takes
    ``seconds``; a ``gated`` probe also holds every hash until ``release()``."""

    def __init__(self, seconds: float, *, gated: bool) -> None:
        self.seconds = seconds
        self.queued = 0
        self.started = 0
        self.running = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()
        self._gate = threading.Event()
        if not gated:
            self._gate.set()

    def release(self) -> None:
        """Let the held hashes (and the next ones) end."""
        self._gate.set()

    def count_queued(self) -> None:
        with self._lock:
            self.queued += 1

    def run[T](self, work: Callable[[], T]) -> T:
        with self._lock:
            self.started += 1
            self.running += 1
            self.max_concurrent = max(self.max_concurrent, self.running)
        try:
            time.sleep(self.seconds)
            if not self._gate.wait(timeout=_GATE_TIMEOUT_S):
                raise TimeoutError("the probe of argon2 was never released")
            return work()
        finally:
            with self._lock:
                self.running -= 1


class SlowHasher:
    """Stands in for the hasher of ``accounts.passwords``: every hash and verification goes
    through ``probe`` before the real one."""

    def __init__(self, real: PasswordHasher, probe: HashProbe) -> None:
        self._real = real
        self._probe = probe

    def hash(self, password: str) -> str:
        return self._probe.run(lambda: self._real.hash(password))

    def verify(self, password_hash: str, password: str) -> bool:
        return self._probe.run(lambda: self._real.verify(password_hash, password))


class CountingExecutor(Executor):
    """Stands in for the threads of argon2 of ``accounts.passwords``: counts the work handed to
    them (``HashProbe.queued``), then hands it on."""

    def __init__(self, real: Executor, probe: HashProbe) -> None:
        self._real = real
        self._probe = probe

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self._probe.count_queued()
        return self._real.submit(fn, *args, **kwargs)


async def wait_until(condition: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    """Wait until ``condition()`` holds (checked every 10 ms); fail the test with ``what`` if it
    does not within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting until {what}"
        await asyncio.sleep(0.01)
