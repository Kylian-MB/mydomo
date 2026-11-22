"""The house's version of the texts, for a house whose language has no default phrases of its own
(spec 4.1; 0.7.1 spec 5.1).

The installation writes nothing: once the first key works, it is over, and the house's language is
the main language of the owner's browser, whatever it is (``house.language``,
``setup_flow.model_routes``). A language with default phrases of its own (English, French:
``meanings.has_defaults``) reads them. For another one, the upkeep writes every text people read
(``write_house_version``), in the house's language, for no one in particular
(``Reader(None, language)``), and keeps it whole. The texts of the installation itself (the group
``setup``) are never written: they stay fixed.

How the texts are written
-------------------------

Each group of the catalog of meanings is one writing (``writer.write_texts``: one call of the writer
for each ``writer.WRITER_KEYS_AT_ONCE`` texts), ``GROUPS_AT_ONCE`` of them at once. What a writing
wrote and the checker accepted is kept at once (``store.save_written``), with a line of the journal
(``texts.written``: the group, the language, how many, the model; never a text). A text refused, not
given, or whose writer gave no answer is asked again of a stronger model: one level above where
writing starts, then the strongest (``LEVELS_ASKED``). A text still left then is a gap: when it was
refused at some level, the journal says why (``texts.refused``); the upkeep asks for the gaps again
(``texts.upkeep.text_gaps_pass``). A writer that never answered at all, for any group at any level,
raises ``WriterUnavailable``.

The house's models (``house_models``) are on the household's general key, else on a key of the
owner. Their calls count for nobody (``person_id`` is ``None``), under the key's limits for
everyone.

When the texts are written
--------------------------

``HouseWriting`` (``app.state.house_writing``) runs these writings in the background, one at a
time, in the tasks of the application's wakes: the upkeep's, at once when the core starts and then
at most every ``texts.upkeep.GAP_RETRY`` (``rewrite_changed_meanings``: the texts of the house's
language that are missing, never written, refused, or written for a meaning a new version of
Domo changed; none for a language with default phrases of its own).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

import anyio
from domo_common.meanings import all_keys, group_of, has_defaults, meaning, written_groups
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.db import SessionFactory
from domo_core.llm.factory import ModelFactory
from domo_core.llm.levels import LEVELS, Level, level_rank
from domo_core.llm.models import Provider
from domo_core.llm.picker import ModelPicker
from domo_core.llm.tasks import TASKS
from domo_core.policy.settings import load_policy_settings
from domo_core.texts.store import Reader, missing_keys, save_written
from domo_core.texts.writer import ForWhom, TextModels, WriterUnavailable, Written, write_texts

logger = logging.getLogger(__name__)

# How many groups are written at once.
GROUPS_AT_ONCE: Final = 4
# The least level of the writer's model at each try of a text: the writer's own (``None``), one
# above where writing starts, then the strongest.
LEVELS_ASKED: Final[tuple[Level | None, ...]] = (
    None,
    LEVELS[min(level_rank(TASKS["writing"].start) + 1, len(LEVELS) - 1)],
    LEVELS[-1],
)


@dataclass(frozen=True)
class HouseVersion:
    """What a writing of the house's version did: how many texts it ``written``, and the keys it
    left unwritten after the strongest model (``gaps``), sorted."""

    written: int
    gaps: tuple[str, ...]


def _by_group(keys: Sequence[str] | None) -> dict[str, list[str]]:
    """``keys`` (every key of the catalog when ``None``) by group, the groups and their keys
    sorted, a key once: those of the groups that are written (never one of ``setup``).
    ``KeyError`` for a key that is not in the catalog."""
    groups = frozenset(written_groups())
    by_group: dict[str, set[str]] = {}
    for key in all_keys() if keys is None else keys:
        meaning(key)  # in the catalog, or KeyError
        group = group_of(key)
        if group in groups:
            by_group.setdefault(group, set()).add(key)
    return {group: sorted(by_group[group]) for group in sorted(by_group)}


async def _write_group(
    session_factory: SessionFactory,
    models: TextModels,
    group: str,
    keys: Sequence[str],
    *,
    reader: Reader,
    at_least: Level | None,
    now: Callable[[], datetime],
) -> Written | WriterUnavailable:
    """The texts of ``keys`` (one group) written for ``reader`` and kept, with their line of the
    journal; or why the writer gave no answer."""
    try:
        written = await write_texts(models, keys, ForWhom(reader.language), at_least=at_least)
    except WriterUnavailable as unavailable:
        return unavailable
    if written.texts:
        async with session_factory() as session:
            await save_written(session, reader, written.texts, model_id=written.model_id, now=now())
            # Last, just before the commit: the audit log holds a lock until the transaction ends.
            await append_audit(
                session,
                actor="system",
                action="texts.written",
                details={
                    "person": None,
                    "language": reader.language,
                    "group": group,
                    "count": len(written.texts),
                    "model": written.model_id,
                },
            )
            await session.commit()
    return written


async def _write_groups(
    session_factory: SessionFactory,
    models: TextModels,
    groups: Mapping[str, Sequence[str]],
    *,
    reader: Reader,
    at_least: Level | None,
    now: Callable[[], datetime],
) -> dict[str, Written | WriterUnavailable]:
    """Each of ``groups`` written (``_write_group``), ``GROUPS_AT_ONCE`` at once."""
    outcomes: dict[str, Written | WriterUnavailable] = {}
    limiter = anyio.CapacityLimiter(GROUPS_AT_ONCE)

    async def one(group: str) -> None:
        async with limiter:
            outcomes[group] = await _write_group(
                session_factory,
                models,
                group,
                groups[group],
                reader=reader,
                at_least=at_least,
                now=now,
            )

    async with anyio.create_task_group() as tasks:
        for group in groups:
            tasks.start_soon(one, group)
    return outcomes


async def write_house_version(
    session_factory: SessionFactory,
    models: TextModels,
    *,
    language: str,
    now: Callable[[], datetime],
    keys: Sequence[str] | None = None,
) -> HouseVersion:
    """Write the house's version of the texts in ``language``: every key of the groups that are
    written (or those of ``keys``; a key of ``setup`` never), one writer's call per group,
    ``GROUPS_AT_ONCE`` at once, each group kept as it comes, at the time ``now`` gives (see the
    module docstring). The keys left are asked again at each level of ``LEVELS_ASKED``; those
    still left are the gaps, and each one refused at some level (then refused again, or not
    answered, at the next ones) is journaled once, with why it was refused last.
    When there was something to write and the writer gave no answer to any call, the writing
    fails: ``WriterUnavailable`` (the last reason). ``KeyError`` for a key not in the catalog,
    before any call."""
    left = _by_group(keys)
    reader = Reader(None, language)
    count = 0
    answered = False
    unavailable: WriterUnavailable | None = None
    refused: dict[str, str] = {}
    for at_least in LEVELS_ASKED:
        if not left:
            break
        outcomes = await _write_groups(
            session_factory, models, left, reader=reader, at_least=at_least, now=now
        )
        still: dict[str, list[str]] = {}
        for group, keys_left in left.items():
            outcome = outcomes[group]
            if isinstance(outcome, WriterUnavailable):
                unavailable = outcome
                still[group] = keys_left
                continue
            answered = True
            count += len(outcome.texts)
            # A key refused at a level and written at a later one is no refusal any more.
            for key in outcome.texts:
                refused.pop(key, None)
            # A key asked and not given is in neither ``texts`` nor ``refused``: it is left too.
            unwritten = [key for key in keys_left if key not in outcome.texts]
            refused.update(
                {key: outcome.refused[key] for key in unwritten if key in outcome.refused}
            )
            if unwritten:
                still[group] = unwritten
        left = still
    if not answered and unavailable is not None:
        raise unavailable
    if refused:
        async with session_factory() as session:
            for key in sorted(refused):
                await append_audit(
                    session,
                    actor="system",
                    action="texts.refused",
                    details={"key": key, "why": refused[key]},
                )
            await session.commit()
    gaps = tuple(sorted(key for keys_left in left.values() for key in keys_left))
    if gaps:
        logger.info("%d text(s) of the house's version are left to write", len(gaps))
    return HouseVersion(written=count, gaps=gaps)


async def rewrite_changed_meanings(
    session_factory: SessionFactory, models: TextModels, *, now: Callable[[], datetime]
) -> None:
    """Write again the texts of the house's language that are missing: never written, refused
    (the gaps), or written for a meaning that changed since (a new version of Domo: the old
    text is no longer shown, ``store.written``). Nothing is asked when none is missing, nor for a
    language with default phrases of its own (``has_defaults``: it reads them). A writer that
    never answers (``WriterUnavailable``) is logged, never raised: the upkeep asks again later."""
    async with session_factory() as session:
        language = (await load_policy_settings(session)).house_language
        if has_defaults(language):
            return
        missing = await missing_keys(
            session,
            Reader(None, language),
            [key for keys in _by_group(None).values() for key in keys],
        )
    if not missing:
        return
    try:
        await write_house_version(session_factory, models, language=language, now=now, keys=missing)
    except WriterUnavailable as unavailable:
        logger.warning("The missing texts of the house could not be written: %s", unavailable)


# --- the house's models -------------------------------------------------------------------------


async def house_key(session: AsyncSession) -> Provider | None:
    """The key the house's texts are written on: the household's general key, else the oldest
    key the owner gave; ``None`` when there is neither."""
    general = (await session.execute(select(Provider).where(Provider.is_general))).scalars().first()
    if general is not None:
        return general
    owner = select(Person.id).where(Person.role == "owner").scalar_subquery()
    found = await session.execute(
        select(Provider)
        .where(Provider.given_by == owner)
        .order_by(Provider.created_at, Provider.id)
        .limit(1)
    )
    return found.scalars().first()


def house_models(
    session_factory: SessionFactory,
    model_factory: Callable[[], ModelFactory],
    clock: Callable[[], datetime],
) -> Callable[[], Awaitable[TextModels | None]]:
    """The models of the house's texts, as they are at each call: a ``ModelPicker`` on the house's
    key (``house_key``) whose calls count for nobody, with the model factory ``model_factory``
    gives then; ``None`` when the house has no key."""

    async def models() -> TextModels | None:
        async with session_factory() as session:
            key = await house_key(session)
        if key is None:
            return None
        return ModelPicker(
            session_factory=session_factory,
            model_factory=model_factory(),
            provider=key,
            person_id=None,
            clock=clock,
        )

    return models


# --- one writing at a time ----------------------------------------------------------------------


def _kinds(error: Exception) -> str:
    """The kind of ``error``; of each error inside it, for the errors of a task group (the groups
    are written in one)."""
    if isinstance(error, ExceptionGroup):
        inside = cast(ExceptionGroup[Exception], error).exceptions
        return ", ".join(sorted({type(each).__name__ for each in inside}))
    return type(error).__name__


@dataclass(frozen=True)
class HouseRun:
    """A writing of the house's version (``HouseWriting``): when it ``began``; how it ``ended``
    (``None`` while it runs): ``written`` (whatever gaps it left), or ``failed`` (the writer never
    answered, or the writing broke off)."""

    began: datetime
    ended: Literal["written", "failed"] | None = None


class HouseWriting:
    """The writings of the house's version, one at a time (``app.state.house_writing``): each runs
    in a task ``spawn`` makes (the application's wakes, which outlive the request that started
    one and are stopped with the application). ``last`` is the last one begun."""

    def __init__(self, spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]) -> None:
        self._spawn = spawn
        self._last: HouseRun | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def last(self) -> HouseRun | None:
        """The last writing begun, as it stands (``None`` before any)."""
        return self._last

    @property
    def running(self) -> bool:
        """Whether a writing runs."""
        return self._last is not None and self._last.ended is None

    def due(self, when: datetime, every: timedelta) -> bool:
        """Whether a writing may begin at ``when``, coming ``every`` after the last one began:
        none runs, and none began less than ``every`` before."""
        return not self.running and (self._last is None or when >= self._last.began + every)

    def start(self, write: Callable[[], Awaitable[object]], *, began: datetime) -> bool:
        """Begin ``write`` in the background, as a writing begun at ``began``; ``False`` (and
        nothing begins) while another runs."""
        if self.running:
            return False
        run = HouseRun(began=began)
        self._last = run
        self._task = self._spawn(self._run(run, write))
        return True

    async def _run(self, run: HouseRun, write: Callable[[], Awaitable[object]]) -> None:
        ended: Literal["written", "failed"] = "failed"
        try:
            await write()
            ended = "written"
        except WriterUnavailable as unavailable:
            # A fixed sentence: the writer did not answer, and how.
            logger.warning("The house's texts could not be written: %s", unavailable)
        except Exception as error:
            # The kind of error only, as everywhere in the core: its message may hold what the
            # database said.
            logger.error("Writing the house's texts failed (%s)", _kinds(error))
        finally:
            self._last = replace(run, ended=ended)

    async def wait(self) -> None:
        """Wait for the writing that runs, if one does, to end."""
        task = self._task
        if task is not None:
            await asyncio.wait({task})
