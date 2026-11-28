"""What a voice session needs of the application (spec 6.4): the turn's dependencies, the METERED
speech clients of a person, the count of the password checks a proof shares, and how the
assistant is woken with a fact of the channel.

Every call of sound goes through a ``MeteredSpeech`` (ruling T14): ``MeteredSpeechOpener.open``
finds the route of a task for a person (``llm.speech.routes.speech_route``: what is heard stays at
home unless the owner said otherwise, a phrase awaited as a proof is heard at home whatever the
setting; where a key is is the one shared ``AtHome`` of the application) and wraps the client in
the meter of that route, so that each call is reserved, limited and settled like a chat call. The
thresholds of a print model are read from the service at home that makes the prints
(``SpeechClient.print_thresholds``), kept for ``THRESHOLDS_KEPT_S``; a service elsewhere gives none,
and the voice is then "not learnt yet".
"""

import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from domo_core.accounts.models import Person
from domo_core.accounts.throttle import LoginThrottle
from domo_core.conversation.prepare import TurnDeps
from domo_core.conversation.tool_wakes import WakeEnd
from domo_core.db import SessionFactory
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.speech.at_home import HomeCheck
from domo_core.llm.speech.client import PrintThresholds, SpeechClient
from domo_core.llm.speech.home import SPEECH_LIST_WANTED
from domo_core.llm.speech.metered import MeteredSpeech, prices_of
from domo_core.llm.speech.routes import speech_route
from domo_core.policy.settings import load_policy_settings
from domo_core.prompts import ToolWake

# The tasks of sound a voice session asks for.
SpeechTask = Literal["hear", "say", "voice_print"]
# How long the thresholds of a print model are kept, in seconds.
THRESHOLDS_KEPT_S: Final = 300.0

# Wakes the assistant in the conversation of a person with what tools say (as data):
# ``conversation.tool_wakes.tool_wake_end``; how the turn ended (``spoke``: words were written).
Wake = Callable[[uuid.UUID, Sequence[ToolWake]], Awaitable[WakeEnd]]
# After a tell that did not write words, how long the same fact waits before it is tried again,
# in seconds, however often the voice is turned on meanwhile.
RETRY_TELL_AFTER_S: Final = 60.0


class MeteredSpeechOpener:
    """The metered speech clients of the application (see the module docstring)."""

    def __init__(
        self,
        client: SpeechClient,
        *,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
        at_home: HomeCheck,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._clock = clock
        self._at_home = at_home
        self._monotonic = monotonic
        self._thresholds: dict[str, tuple[float, dict[str, PrintThresholds]]] = {}

    @property
    def at_home(self) -> HomeCheck:
        """The check of where a key leads that the routes are chosen with."""
        return self._at_home

    async def open(
        self, person: Person, task: SpeechTask, *, for_a_proof: bool = False
    ) -> MeteredSpeech | None:
        """The metered client of ``task`` for ``person``, or ``None`` when no key they may use
        has a model that does it where it may go."""
        async with self._session_factory() as session:
            settings = await load_policy_settings(session)
            catalog = await current_catalog(session)
            route = await speech_route(
                session,
                person,
                task,
                catalog=catalog,
                settings=settings,
                at_home=self._at_home,
                for_a_proof=for_a_proof,
            )
        if route is None:
            # The speech service may have gained this use since its list was read.
            SPEECH_LIST_WANTED.want()
            return None
        home = await self._at_home(route.provider)
        return MeteredSpeech(
            self._client,
            route=route,
            person_id=person.id,
            session_factory=self._session_factory,
            clock=self._clock,
            catalog=catalog,
            prices=prices_of(route, at_home=home),
        )

    async def hears_outside(self) -> bool:
        """Whether the owner lets what is heard go to a model outside the house."""
        async with self._session_factory() as session:
            return (await load_policy_settings(session)).hear_outside

    async def thresholds(self, printing: MeteredSpeech, model: str) -> PrintThresholds | None:
        """The thresholds of the print model ``model`` of the service ``printing`` reaches, when
        it is a service of the house; ``None`` otherwise, or when it lists none for that model."""
        provider = printing.route.provider
        if not await self._at_home(provider):
            return None
        now = self._monotonic()
        kept = self._thresholds.get(provider.id)
        if kept is None or now >= kept[0]:
            listed = await self._client.print_thresholds(provider, at_home=True)
            kept = (now + THRESHOLDS_KEPT_S, listed)
            self._thresholds[provider.id] = kept
        return kept[1].get(model)


@dataclass
class Told:
    """The facts told once (``told``), those being told now (``telling``), and when a fact whose
    tell wrote nothing was last tried (``tried_at``). A fact is told once the assistant's turn that
    tells it kept words in the conversation; one whose turn could not run, ran silently or failed
    stays to tell, and is tried again ``RETRY_TELL_AFTER_S`` after its last try at the soonest.
    Never two tells of one fact at once. A fact forgotten while it is told (``forgotten``) is
    neither told nor tried when that tell ends."""

    told: set[str] = field(default_factory=lambda: set[str]())
    telling: set[str] = field(default_factory=lambda: set[str]())
    forgotten: set[str] = field(default_factory=lambda: set[str]())
    tried_at: dict[str, datetime] = field(default_factory=lambda: dict[str, datetime]())

    def begin(self, fact: str, now: datetime) -> bool:
        """Whether ``fact`` is to be told now (neither told, nor being told, nor tried in vain
        too recently): it is then being told until ``end``."""
        if fact in self.told or fact in self.telling:
            return False
        tried = self.tried_at.get(fact)
        if tried is not None and (now - tried).total_seconds() < RETRY_TELL_AFTER_S:
            return False
        self.telling.add(fact)
        return True

    def end(self, fact: str, *, told: bool, now: datetime) -> None:
        """``fact`` is no longer being told; ``told``: words saying it were written, and it is
        never told again (until ``forget``). Nothing is kept of a fact forgotten meanwhile."""
        self.telling.discard(fact)
        if fact in self.forgotten:
            self.forgotten.discard(fact)
            return
        if told:
            self.told.add(fact)
            self.tried_at.pop(fact, None)
        else:
            self.tried_at[fact] = now

    def forget(self, fact: str) -> None:
        """What made ``fact`` true is over: told again, and at once, should it become true again
        (a tell of it under way still ends first, and keeps nothing)."""
        self.told.discard(fact)
        self.tried_at.pop(fact, None)
        if fact in self.telling:
            self.forgotten.add(fact)

    def keep_only(self, facts: Iterable[str]) -> None:
        """Every fact but ``facts`` is forgotten (``forget``)."""
        kept = set(facts)
        for fact in [f for f in self.told | self.telling | set(self.tried_at) if f not in kept]:
            self.forget(fact)


@dataclass(frozen=True)
class VoiceDeps:
    """What a voice session needs: what every turn needs (``turn``), the metered speech clients
    (``speech``), the count of the password checks (``throttle``: the account page's, which a
    proof shares; a spoken phrase is counted by the gatekeeper), how the assistant is woken with a
    fact of the channel (``wake``; ``None``: ``conversation.tool_wakes.tool_wake_end`` on
    ``turn``), and the facts the owner was already told in this process, or is being told
    (``told``). The voice sessions live in the process and the moments they share are
    ``live.LIVE_VOICES``, the one object the sessions and the tool that stops the listening both
    reach."""

    turn: TurnDeps
    speech: MeteredSpeechOpener
    throttle: LoginThrottle
    wake: Wake | None = None
    told: Told = field(default_factory=Told)
