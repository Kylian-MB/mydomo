"""The writing of a reader's texts in the background (spec 4.2, 5.2; 0.7.1 spec 5.1).

A page shows at once the best text there is of each key (the reader's own, else their language's,
else the default phrase of their language or the English one: ``view.recueil_for``). When Domo
has a reason to (``has_a_reason``: a language without default phrases of its own, or a writing
note he keeps for the person), the writer writes in the background the texts of that page for
them: the groups of the keys the page showed that are not yet their own
(``web.texts_middleware``). They show from the next display on. Without a reason nothing is asked:
writing again, with nothing to write from, what the default phrases of their language say would
cost without a reason. What a reader asks to have written again (``rewrite``) and the heads of
cards (``write_head``) are written whatever it says.

``TextWriting`` (``app.state.text_writing``) runs these writings in the tasks of the application's
wakes (``conversation.starts.Wakes``: they outlive the request that asked for them and are stopped
with the application), one at a time for a reader and a group: a group asked again while it is being
written is not asked twice, and a group whose texts were written meanwhile is not written again
(only the keys still missing are). What a page asks is one task, on one choice of models; each of
its groups is one writing (``writer.write_texts``: one call of the writer for each
``writer.WRITER_KEYS_AT_ONCE`` texts), ``GROUPS_AT_ONCE`` groups at once,
whoever they are for. What it wrote and the checker accepted is kept at once
(``store.save_written``), with a line of the journal (``texts.written``: for whom, the language,
the group, how many, the model; never a text), and each text refused in the end is journaled with
why (``texts.refused``). A text left unwritten (refused, not given, or whose writing failed: a
writer that did not answer, a writing that broke off, or no key that may write it) is not asked
again for that reader before ``upkeep.GAP_RETRY``: a page shown again meanwhile (the chat's polls
come every few seconds) shows the best text there is, without a call, and a group whose texts were
all left so is not even looked at again.

The texts are written for someone on the keys they may use (``reader_models``: ``choose_key``,
then a ``ModelPicker`` whose calls count for them): nothing is written for someone without a key
they may use. A writer that does not answer, or a writing that breaks off, is logged. A person's
texts are written, and kept, in the language they read when they are kept: a writing asked in
another one (by a page shown before their language changed) keeps nothing.

Texts a reader asks to have written again (``rewrite``, spec 4.7: a person's own, through
``tools.text_tools``, or the version of a language) are written the same way, a writer's call for
each of their groups, whatever texts the reader has already, with the indication of the rewrite
(``ForWhom.indication``) and the text the reader reads today to start from; once the writings of
those groups running for the reader have ended, so that what was asked last is written last. Its
``Rewriting`` tells, once it is over, what was kept and what was refused in the end, with why.
What must see the texts as those writings leave them (an earlier version brought back,
``tools.text_tools``) runs after them in the same way (``after_writings``), and what is asked of
those groups next waits for it in turn.

The head of a card (``texts.cards``) is written here too, for one reader, on their keys
(``write_head``), and kept with the card: a head kept from the same facts is not written again (it
is looked for before any key is chosen); one writing at a time for a card and a reader, a writing
asked from other facts beginning once the one before has ended, so that the newest facts are
always written last; a head left unwritten (refused, or nobody answered) is not asked again for
that reader, from the same facts, before ``GAP_RETRY``. What asks for heads in the background
(``texts.card_heads``) runs here too (``in_background``).

The version of a language is written the same way, for nobody in particular (spec 4.6, 4.12): on
the keys of the one who invited the person it is written for (``host``: the page of a valid link,
``household.routes``), whose calls count for them; else on the house's key (``install.house_key``:
the general key, else one of the owner's), whose calls count for nobody. A new language is begun
with ``begin_language``, which keeps to the house's ``languages.NEW_LANGUAGES_PER_DAY``: one
decision at a time, and a language it let through counts for that day whatever its writing keeps
(a writer that fails, texts all refused), as do the languages being written that have no version
yet (a version written again is no language begun). The whole version is asked then, the texts
of the page first: none of it is asked later, outside that decision. A language with default
phrases of its own is read at once: it begins nothing, and takes no slot.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Coroutine, Hashable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import anyio
from domo_common.meanings import (
    group_of,
    has_defaults,
    has_meaning,
    keys_of_group,
    written_groups,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.db import SessionFactory
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.factory import ModelFactory
from domo_core.llm.models import Provider
from domo_core.llm.picker import ModelPicker
from domo_core.llm.router import Route, choose_key, usable_keys
from domo_core.llm.spending import at_limit
from domo_core.policy.settings import load_policy_settings
from domo_core.texts import cards
from domo_core.texts.cards import CardFacts, CardOf, head_of, write_card_head
from domo_core.texts.install import house_models
from domo_core.texts.languages import (
    NEW_LANGUAGES_PER_DAY,
    known_languages,
    languages_begun_today,
)
from domo_core.texts.store import Reader, save_written, written
from domo_core.texts.upkeep import GAP_RETRY
from domo_core.texts.writer import ForWhom, TextModels, WriterUnavailable, Written, write_texts

if TYPE_CHECKING:
    from domo_core.conversation.starts import Wakes

logger = logging.getLogger(__name__)

# How many groups are written at once, whoever they are for.
GROUPS_AT_ONCE: Final = 4
# How long a language let through by ``begin_language`` is remembered: more than the longest day
# of any time zone, back from its midnight.
_BEGUN_KEPT: Final = timedelta(days=2)

# Where a reader's texts are kept: a person's id, or a language's tag.
type _Where = UUID | str
# The models a reader's texts are written on: for ``reader``, the version of a language on the keys
# of ``host`` (``None``: the house's); ``None`` when nothing can be written.
type ModelsFor = Callable[[Reader, UUID | None], Awaitable[TextModels | None]]


def _where(reader: Reader) -> _Where:
    return reader.language if reader.person_id is None else reader.person_id


@dataclass
class Rewriting:
    """Texts a reader asked to have written again (``TextWriting.rewrite``): ``keys``, with the
    ``indication`` of the rewrite (``None``: none). ``task`` is its writing (``None`` when there
    was nothing to write); once it has ended, ``texts`` holds the texts kept, by key, and
    ``refused`` those refused in the end, each with why. A key in neither was not written: the
    reader has no key that may write it, or the writer did not answer."""

    keys: frozenset[str]
    indication: str | None = None
    texts: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    refused: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    task: asyncio.Task[None] | None = None


async def reads_another_language(
    session: AsyncSession, reader: Reader, *, hold: bool = False
) -> bool:
    """Whether the person ``reader`` is for reads another language than the reader's now (it
    changed since the writing was asked): their texts are not kept then. Never for the version
    of a language, nor for someone who has no language kept yet (they read the house's). With
    ``hold``, the person's row is held (``FOR SHARE``) until the transaction ends: a change of
    their language, which locks it first (``tools.text_tools``, ``web.texts_middleware``), waits
    for what is kept in it, or it is kept after the change and seen here."""
    if reader.person_id is None:
        return False
    read = select(Person.language).where(Person.id == reader.person_id)
    kept = await session.scalar(read.with_for_update(read=True) if hold else read)
    return kept is not None and kept != reader.language


def has_a_reason(reader: Reader, *, writing_note: str | None) -> bool:
    """Whether Domo has a reason to write the texts of ``reader`` in the background, unasked:
    their language has no default phrases of its own (``meanings.has_defaults``), or the reader is
    a person for whom he keeps a writing note (``writing_note``, the person's)."""
    if not has_defaults(reader.language):
        return True
    return reader.person_id is not None and bool(writing_note)


def for_whom_of(person: Person | None, language: str) -> ForWhom:
    """Who the writer writes for: ``person`` (their name, their role and the writing note Domo
    keeps for them), reading ``language``; the version of a language for ``None``."""
    if person is None:
        return ForWhom(language)
    return ForWhom(
        language,
        name=person.display_name,
        role=person.role,
        writing_note=person.writing_note,
    )


class TextWriting:
    """The writings of readers' texts in the background (see the module docstring): in tasks of
    ``wakes``, on the models ``models_for`` gives for a reader and a host (``None``: nothing is
    written), kept in the database of ``session_factory`` at the time ``clock`` gives.

    ``host`` is the one on whose keys the version of a language is written, and for whom it
    counts: the one who invited the person it is written for. Without one, the version of a
    language is the house's to write. A person's own texts are always written on their keys."""

    def __init__(
        self,
        wakes: "Wakes",
        session_factory: SessionFactory,
        models_for: ModelsFor,
        clock: Callable[[], datetime],
    ) -> None:
        self._wakes = wakes
        self._session_factory = session_factory
        self._models_for = models_for
        self._clock = clock
        self._groups = frozenset(written_groups())
        self._all_groups = tuple(written_groups())
        self._running: dict[tuple[_Where, str], asyncio.Task[None]] = {}
        # When each text left unwritten for a reader (refused, not given, or whose writing
        # failed) was asked last.
        self._left: dict[tuple[_Where, str], datetime] = {}
        self._at_once = asyncio.Semaphore(GROUPS_AT_ONCE)
        # One decision at a time to begin a new language (``begin_language``), and the languages
        # it let through, with when (the last time).
        self._beginning = asyncio.Lock()
        self._begun: dict[str, datetime] = {}
        # The writing of the head of a card for a reader, with what it is written from (the
        # fingerprint of its facts); what a head left unwritten was asked from, and when (the last
        # time); and what asks for heads in the background, by what it asks.
        self._heads: dict[tuple[CardOf, UUID], tuple[str, asyncio.Task[None]]] = {}
        self._heads_left: dict[tuple[CardOf, UUID], tuple[str, datetime]] = {}
        self._asks: dict[Hashable, asyncio.Task[None]] = {}

    def ask(
        self,
        reader: Reader,
        groups: Iterable[str],
        *,
        host: UUID | None = None,
        writing_note: str | None = None,
    ) -> None:
        """Have the texts of ``groups`` written for ``reader`` in the background, those that are
        missing (the version of a language on the keys of ``host``, see the class docstring), if
        Domo has a reason to (``has_a_reason``, with the person's ``writing_note``). A group that
        is never written (``setup``), or that is being written for the reader already, is not
        asked again. It returns at once."""
        self._ask(reader, groups, host=host, writing_note=writing_note)

    def _ask(
        self,
        reader: Reader,
        groups: Iterable[str],
        *,
        host: UUID | None,
        writing_note: str | None = None,
        after: asyncio.Task[None] | None = None,
    ) -> asyncio.Task[None] | None:
        """``ask``, the writing beginning once ``after`` (a writing asked before) has ended; the
        task of the writing, ``None`` when nothing new was asked. Nothing is asked without a reason
        (``has_a_reason``). A group each of whose texts was left unwritten for the reader less than
        ``GAP_RETRY`` ago is not asked: its writing would find nothing to write, after reading the
        database (a reader nothing can write for, whose page polls every few seconds)."""
        if not has_a_reason(reader, writing_note=writing_note):
            return None
        where, now = _where(reader), self._clock()
        new = [
            group
            for group in dict.fromkeys(groups)
            if group in self._groups
            and (where, group) not in self._running
            and not all(self._left_lately(where, key, now) for key in keys_of_group(group))
        ]
        if not new:
            return None
        return self._start(reader, new, host, () if after is None else (after,))

    def _start(
        self,
        reader: Reader,
        groups: list[str],
        host: UUID | None,
        after: Collection[asyncio.Task[None]],
        rewriting: Rewriting | None = None,
    ) -> asyncio.Task[None]:
        """The writing of ``groups`` for ``reader`` in a task of the wakes, once the writings
        ``after`` have ended, held as the one running for each of them."""
        slots = [(_where(reader), group) for group in groups]
        return self._hold(
            slots, self._wakes.spawn(self._write(reader, groups, host, after, rewriting))
        )

    def _hold(
        self, slots: list[tuple[_Where, str]], task: asyncio.Task[None]
    ) -> asyncio.Task[None]:
        """``task`` held as the one running for each of ``slots``, until it ends."""
        for slot in slots:
            self._running[slot] = task
        task.add_done_callback(lambda done: self._forget(slots, done))
        return task

    def after_writings(
        self, reader: Reader, groups: Iterable[str], then: Callable[[], Awaitable[None]]
    ) -> asyncio.Task[None]:
        """Run ``then()`` in a task of the wakes once the writings of ``groups`` running for
        ``reader`` have ended, held meanwhile as the one running for each of them: a rewrite asked
        next waits for it, and a page does not ask those groups twice. It returns at once: its
        task. What fails in it is logged by its kind."""
        where = _where(reader)
        slots = [(where, group) for group in dict.fromkeys(groups) if group in self._groups]
        before = {self._running[slot] for slot in slots if slot in self._running}

        async def run() -> None:
            try:
                if before:
                    await asyncio.wait(before)
                await then()
            except Exception as error:
                logger.error(
                    "What waited for the writing of texts failed (%s)", type(error).__name__
                )

        return self._hold(slots, self._wakes.spawn(run()))

    def rewrite(
        self, reader: Reader, keys: Iterable[str], *, indication: str | None = None
    ) -> Rewriting:
        """Have ``keys`` written again for ``reader`` in the background, whatever texts they have
        (a person's own on their keys, the version of a language on the house's), with
        ``indication``: a writer's call for each of their groups, once the writings of those
        groups running for the reader have ended. A key that is not a text that is written is left
        out. It returns at once: the ``Rewriting``, which tells what came of it once its task has
        ended."""
        wanted = [
            key for key in dict.fromkeys(keys) if has_meaning(key) and group_of(key) in self._groups
        ]
        rewriting = Rewriting(frozenset(wanted), indication)
        groups = list(dict.fromkeys(group_of(key) for key in wanted))
        if groups:
            where = _where(reader)
            before = {
                self._running[(where, group)] for group in groups if (where, group) in self._running
            }
            rewriting.task = self._start(reader, groups, None, before, rewriting)
        return rewriting

    def _forget(self, slots: list[tuple[_Where, str]], task: asyncio.Task[None]) -> None:
        for slot in slots:
            if self._running.get(slot) is task:
                del self._running[slot]

    async def write_now(
        self, reader: Reader, groups: Iterable[str], *, within: float, host: UUID | None = None
    ) -> bool:
        """Have the texts of ``groups`` written for ``reader`` (``ask``, on the keys of
        ``host``), and wait up to ``within`` seconds for those writings to end: ``True`` when they
        did, ``False`` when some still run (they go on in the background)."""
        wanted = list(dict.fromkeys(groups))
        self.ask(reader, wanted, host=host)
        where = _where(reader)
        tasks = {
            task for (at, group), task in self._running.items() if at == where and group in wanted
        }
        if not tasks:
            return True
        _, still = await asyncio.wait(tasks, timeout=within)
        return not still

    async def begin_language(
        self, language: str, groups: Iterable[str], *, host: UUID | None
    ) -> bool:
        """Begin the version of ``language``, a language that has none yet, in the background, on
        the keys of ``host``: the texts of ``groups`` (a page's) first, then the rest of the
        version, both asked now. Only if the house may begin a new language today: fewer than
        ``NEW_LANGUAGES_PER_DAY`` were begun since its midnight (``languages_begun_today``),
        counting those this let through, whatever their writing kept, and those being written that
        have no version yet (a version written again begins nothing); one decision at a time, so
        two at once never pass the limit together. A language begun today already takes no new
        slot. A language with default phrases of its own (``has_defaults``) is read at once: nothing
        is written, and no slot is taken. ``True`` when it is being written or read at once,
        ``False`` when the house may not begin it."""
        if has_defaults(language):
            return True
        async with self._beginning:
            now = self._clock()
            self._begun = {
                begun: when for begun, when in self._begun.items() if when > now - _BEGUN_KEPT
            }
            async with self._session_factory() as session:
                # A version being written again (``rewrite``) is no language begun: only those
                # being written that have no version yet count.
                known = await known_languages(session)
                writing = [where for where in self._languages_written() if where not in known]
                begun = {**self._begun, **dict.fromkeys(writing, now)}
                today = await languages_begun_today(session, now=now, begun=begun)
            if language not in today and len(today) >= NEW_LANGUAGES_PER_DAY:
                return False
            self._begun[language] = now
            reader = Reader(None, language)
            page = self._ask(reader, groups, host=host)
            self._ask(reader, self._all_groups, host=host, after=page)
            return True

    def write_head(
        self, card: CardOf, reader: Person, facts: CardFacts
    ) -> asyncio.Task[None] | None:
        """Have the head of ``card`` written for ``reader`` from ``facts`` in the background, on
        their keys (``cards.write_card_head``; nothing for someone without a key they may use),
        and kept: the task of the writing, the one already running for that card, reader and
        facts if there is one. A writing from other facts begins once the one running for that
        card and reader has ended: the newest facts are written last, and never given way to.
        ``None`` when nothing is asked: a head left unwritten for them, from the same facts, less
        than ``GAP_RETRY`` ago. It returns at once."""
        slot, asked = (card, reader.id), facts.fingerprint("")
        running = self._heads.get(slot)
        if running is not None and running[0] == asked:
            return running[1]
        left = self._heads_left.get(slot)
        if left is not None and left[0] == asked and self._clock() < left[1] + GAP_RETRY:
            return None
        before = None if running is None else running[1]
        task = self._wakes.spawn(self._write_head(card, reader.id, facts, before))
        self._heads[slot] = (asked, task)

        def forget(done: asyncio.Task[None]) -> None:
            current = self._heads.get(slot)
            if current is not None and current[1] is done:
                del self._heads[slot]

        task.add_done_callback(forget)
        return task

    async def head_now(self, card: CardOf, reader: Person, facts: CardFacts) -> None:
        """``write_head``, waiting ``cards.CARD_HEAD_SECONDS`` at most for the writing to end:
        past that, it goes on in the background, and what it writes is kept."""
        task = self.write_head(card, reader, facts)
        if task is not None:
            await asyncio.wait({task}, timeout=cards.CARD_HEAD_SECONDS)

    def asked_lately(self, card: CardOf, reader_id: UUID) -> bool:
        """Whether the head of ``card`` is being written for the reader ``reader_id``, or was
        left unwritten for them, from whatever facts, less than ``GAP_RETRY`` ago: a page that
        shows the card does not ask it again (``texts.card_heads.ask_missing_heads``)."""
        slot = (card, reader_id)
        if slot in self._heads:
            return True
        left = self._heads_left.get(slot)
        return left is not None and self._clock() < left[1] + GAP_RETRY

    def in_background(self, key: Hashable, ask: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Run ``ask()`` in a task of the wakes, unless one asked under ``key`` still runs: what
        asks for heads (``texts.card_heads``), which reads the database, never makes a page or a
        decision wait. Logged by the kind of its error when it fails. It returns at once."""
        if key in self._asks:
            return

        async def logged() -> None:
            try:
                await ask()
            except Exception as error:
                logger.error("Asking for heads failed (%s)", type(error).__name__)

        task = self._wakes.spawn(logged())
        self._asks[key] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._asks.get(key) is done:
                del self._asks[key]

        task.add_done_callback(forget)

    async def _write_head(
        self,
        card: CardOf,
        reader_id: UUID,
        facts: CardFacts,
        before: asyncio.Task[None] | None,
    ) -> None:
        """The writing of a head (``write_head``), once the writing ``before`` it for that card and
        reader has ended, for the reader as the database holds them now, in the language they
        read: nothing when the head kept was written from the same facts, else written on their
        keys. Logged by the kind of its error when it fails."""
        if before is not None:
            await asyncio.wait({before})
        slot, asked, now = (card, reader_id), facts.fingerprint(""), self._clock()
        head: str | None = None
        try:
            async with self._session_factory() as session:
                person = await session.get(Person, reader_id)
                house = (await load_policy_settings(session)).house_language
                kept = await head_of(
                    session,
                    confirmation_id=card.confirmation_id,
                    message_action_id=card.message_action_id,
                    reader_id=reader_id,
                )
            if person is None:
                return
            language = person.language or house
            if kept is not None and kept.facts_hash == facts.fingerprint(language):
                # Kept already, from these facts: no key is chosen, nothing is written.
                head = kept.text
                return
            models = await self._models_for(Reader(person.id, language), None)
            if models is not None:
                head = await write_card_head(
                    models,
                    self._session_factory,
                    card=card,
                    reader=person,
                    for_whom=for_whom_of(person, language),
                    facts=facts,
                    clock=self._clock,
                )
        except Exception as error:
            # The kind of error only, as everywhere in the core.
            logger.error("Writing the head of a card failed (%s)", type(error).__name__)
        finally:
            if head is None:
                # Those whose hour is over are asked again anyway: they are not kept.
                self._heads_left = {
                    left: (facts_of, when)
                    for left, (facts_of, when) in self._heads_left.items()
                    if now < when + GAP_RETRY
                }
                self._heads_left[slot] = (asked, now)
            else:
                self._heads_left.pop(slot, None)

    def _languages_written(self) -> frozenset[str]:
        """The languages whose version is being written."""
        return frozenset(where for where, _ in self._running if isinstance(where, str))

    def _tasks(self) -> set[asyncio.Task[None]]:
        """The writings that run, of texts and of heads, and what asks for heads."""
        heads = {task for _, task in self._heads.values()}
        return {*self._running.values(), *heads, *self._asks.values()}

    @property
    def running(self) -> int:
        """How many writings run, of texts and of heads, with what asks for heads (each a task
        of the wakes; for the tests)."""
        return len(self._tasks())

    async def idle(self) -> None:
        """Wait until every writing asked so far, of texts and of heads, and what asks for
        heads, has ended (for the tests)."""
        while tasks := self._tasks():
            await asyncio.wait(tasks)

    async def _write(
        self,
        reader: Reader,
        groups: list[str],
        host: UUID | None,
        after: Collection[asyncio.Task[None]],
        rewriting: Rewriting | None,
    ) -> None:
        try:
            if after:
                await asyncio.wait(after)
            await self._write_groups(reader, groups, host, rewriting)
        except Exception as error:
            # The kind of error only, as everywhere in the core: its message may hold what the
            # database said.
            logger.error("Writing the texts of a page failed (%s)", type(error).__name__)

    def _left_lately(self, where: _Where, key: str, now: datetime) -> bool:
        """Whether ``key`` was left unwritten for the reader at ``where`` less than
        ``GAP_RETRY`` before ``now``."""
        when = self._left.get((where, key))
        return when is not None and now < when + GAP_RETRY

    async def _write_groups(
        self,
        reader: Reader,
        groups: list[str],
        host: UUID | None,
        rewriting: Rewriting | None = None,
    ) -> None:
        """The keys of ``groups`` still missing for ``reader`` (but those left unwritten lately),
        or those of ``rewriting`` whatever texts the reader has, written on the models for the
        reader and ``host``, each group kept with its lines of the journal as it comes. Nothing
        for a person who reads another language now."""
        where, now = _where(reader), self._clock()
        language = Reader(None, reader.language)
        async with self._session_factory() as session:
            person = None
            if reader.person_id is not None:
                person = await session.get(Person, reader.person_id)
                if person is None or await reads_another_language(session, reader):
                    return
            found = await written(session, list(dict.fromkeys([reader, language])))
        have = found[reader]

        def asked(key: str) -> bool:
            if rewriting is not None:
                return key in rewriting.keys
            return key not in have and not self._left_lately(where, key, now)

        missing = {group: [key for key in keys_of_group(group) if asked(key)] for group in groups}
        missing = {group: keys for group, keys in missing.items() if keys}
        if not missing:
            return
        models = await self._models_for(reader, host)
        if models is None:
            # Nothing can write for them (no key they may use, a key at its limit, a host gone):
            # their keys wait ``GAP_RETRY`` too, or each poll of their page would look again.
            for keys in missing.values():
                for key in keys:
                    self._left[(where, key)] = now
            return
        for_whom = for_whom_of(person, reader.language)
        if rewriting is not None:
            for_whom = replace(for_whom, indication=rewriting.indication)
        # What the reader reads today, for the writer to start from: their own text, else their
        # language's.
        today = {**found[language], **have}

        async def one(group: str, keys: list[str]) -> None:
            written: frozenset[str] = frozenset()
            async with self._at_once:
                try:
                    current = {key: today[key] for key in keys if key in today}
                    result = await write_texts(models, keys, for_whom, current=current)
                    if await self._keep(reader, group, result) and rewriting is not None:
                        rewriting.texts.update(result.texts)
                        rewriting.refused.update(result.refused)
                    written = frozenset(result.texts)
                except WriterUnavailable as unavailable:
                    # A fixed sentence: the writer did not answer, and how.
                    logger.info("The texts of a group could not be written: %s", unavailable)
                except Exception as error:
                    logger.error("Writing the texts of a group failed (%s)", type(error).__name__)
            # What was not kept waits ``GAP_RETRY``, whatever kept it from being written: a key
            # that fails is never asked again at each page.
            for key in keys:
                if key in written:
                    self._left.pop((where, key), None)
                else:
                    self._left[(where, key)] = now

        async with anyio.create_task_group() as tasks:
            for group, keys in missing.items():
                tasks.start_soon(one, group, keys)

    async def _keep(self, reader: Reader, group: str, result: Written) -> bool:
        """Keep what ``result`` wrote for ``reader``, with a line of the journal for the group, and
        one for each text refused in the end; whether it was kept: nothing is, for a person who
        reads another language now (it changed while it was written)."""
        if not result.texts and not result.refused:
            return True
        async with self._session_factory() as session:
            if await reads_another_language(session, reader, hold=True):
                return False
            if result.texts:
                await save_written(
                    session, reader, result.texts, model_id=result.model_id, now=self._clock()
                )
                # Last, just before the commit: the audit log holds a lock until the transaction
                # ends.
                await append_audit(
                    session,
                    actor="system",
                    action="texts.written",
                    details={
                        "person": None if reader.person_id is None else str(reader.person_id),
                        "language": reader.language,
                        "group": group,
                        "count": len(result.texts),
                        "model": result.model_id,
                    },
                )
            for key in sorted(result.refused):
                await append_audit(
                    session,
                    actor="system",
                    action="texts.refused",
                    details={"key": key, "why": result.refused[key]},
                )
            await session.commit()
        return True


def reader_models(
    session_factory: SessionFactory,
    model_factory: Callable[[], ModelFactory],
    clock: Callable[[], datetime],
) -> ModelsFor:
    """The models a reader's texts are written on, as they are at each call, with the model
    factory ``model_factory`` gives then: a person's on the keys they may use; the version of a
    language on the keys of its host, the one who invited the person it is written for (as if it
    were theirs: it counts for them), else on the house's key (``install.house_models``: the
    general key, else one of the owner's, counting for nobody). ``None`` when nothing can be
    written: for someone without a key they may use, or no longer in the database, and for a
    house without a key."""
    house = house_models(session_factory, model_factory, clock)

    async def models(reader: Reader, host: UUID | None) -> TextModels | None:
        payer = reader.person_id if reader.person_id is not None else host
        if payer is None:
            return await house()
        return await _person_models(session_factory, model_factory, clock, payer)

    return models


async def _person_models(
    session_factory: SessionFactory,
    model_factory: Callable[[], ModelFactory],
    clock: Callable[[], datetime],
    person_id: UUID,
) -> TextModels | None:
    """A ``ModelPicker`` on the key ``choose_key`` takes among the keys the person of
    ``person_id`` may use (a key past one of its limits is passed over), whose calls count for
    them; ``None`` for someone without a key they may use, or no longer in the database."""
    now = clock()
    async with session_factory() as session:
        person = await session.get(Person, person_id)
        if person is None:
            return None
        keys = await usable_keys(session, person)
        if not keys:
            return None
        catalog = await current_catalog(session)
        zone = (await load_policy_settings(session)).time_zone

        async def at_its_limit(key: Provider, route: Route) -> bool:
            return await at_limit(
                session,
                provider=key,
                person_id=person.id,
                need=Decimal(0),
                now=now,
                catalog=catalog,
                zone=zone,
            )

        choice = await choose_key(session, person, keys, catalog=catalog, at_limit=at_its_limit)
    if choice.key is None:
        return None
    return ModelPicker(
        session_factory=session_factory,
        model_factory=model_factory(),
        provider=choice.key,
        person_id=person_id,
        clock=clock,
        catalog=catalog,
    )
