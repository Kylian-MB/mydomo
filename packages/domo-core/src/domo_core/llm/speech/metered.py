"""A speech client whose every call is counted on the account of the assistant, like a chat
model's (``llm.metered``; spec 4.7, 4.8).

For each call: the most it can cost is reserved first (from the length of the sound, at 32 000
bytes a second, or of the text; ``spending.reserve``: the same lock, the same limits, the same pass
over stale reservations as for a chat call), a limit that would be passed raises
``SpeechError("limit_reached")`` and nothing is sent, and once the call is over the row is settled
with what it cost (``spending.settle_speech``). A route at home has nothing to pay
(``FREE_SPEECH``) and is counted all the same: one row with its seconds or characters. A call that
failed before any answer costs and counts nothing.

``say`` is an async generator: a caller that may stop reading early (a person who speaks over the
voice, a cancelled turn) reads it as ``async with aclosing(speech.say(...))``, so that the stream
to the service is closed and the call settled at once (a bare ``break`` leaves both to the garbage
collector, and the reservation to the stale pass, at its maximum).

One ``MeteredSpeech`` is for one route, so for one kind of task: it refuses a call of another kind.
"""

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import anyio

from domo_core.db import SessionFactory
from domo_core.llm.catalog import Catalog
from domo_core.llm.costs import speech_cost_usd
from domo_core.llm.provider_kinds import PROVIDER_KINDS
from domo_core.llm.router import Route
from domo_core.llm.speech import catalog as speech_catalog
from domo_core.llm.speech.catalog import FREE_SPEECH, SpeechPrices
from domo_core.llm.speech.client import (
    BYTES_PER_SECOND,
    Heard,
    Print,
    SpeechClient,
    SpeechError,
)
from domo_core.llm.speech.home import SPEECH_LIST_WANTED
from domo_core.llm.spending import LimitReached, reserve, settle_speech
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import ModelCallError
from domo_core.policy.settings import load_policy_settings

logger = logging.getLogger(__name__)

_SETTLE_SECONDS: Final = 10.0


def _list_again_after(error: SpeechError) -> None:
    """A call to the service failed: its list of models is to be read again (it may have started
    again, or measured its models anew: ``llm.speech.home``), unless a spending limit stopped it."""
    if error.reason != "limit_reached":
        SPEECH_LIST_WANTED.want()


def prices_of(route: Route, *, at_home: bool) -> SpeechPrices:
    """What a call on ``route`` costs: nothing when the key is at home (``at_home``, what
    ``speech.at_home`` says of it), else the price the file of speech models lists, else the
    highest it lists."""
    if at_home:
        return FREE_SPEECH
    for kind in PROVIDER_KINDS:
        if kind == route.provider.kind:
            found = speech_catalog.speech_prices(kind, route.model_id)
            if found is not None:
                return found
    return speech_catalog.UNKNOWN_SPEECH_PRICES


class MeteredSpeech:
    """The ``client`` for ``route``, each call counted for ``person_id`` at ``prices``."""

    def __init__(
        self,
        client: SpeechClient,
        *,
        route: Route,
        person_id: UUID | None,
        session_factory: SessionFactory,
        clock: Callable[[], datetime],
        catalog: Catalog,
        prices: SpeechPrices,
    ) -> None:
        self._client = client
        self._route = route
        self._person_id = person_id
        self._session_factory = session_factory
        self._clock = clock
        self._catalog = catalog
        self._prices = prices
        self._cost = Decimal(0)

    @property
    def route(self) -> Route:
        return self._route

    @property
    def cost_usd(self) -> Decimal:
        """What its calls cost so far, in dollars."""
        return self._cost

    def _expects(self, task: TaskKind) -> None:
        if self._route.task != task:
            raise ValueError("a call of sound on a route of another kind of task")

    async def transcribe(
        self, pcm: bytes, *, language: str | None = None, hint: str | None = None
    ) -> Heard:
        self._expects("hear")
        seconds = len(pcm) / BYTES_PER_SECOND
        call_id = await self._reserve(heard_seconds=seconds, said_chars=0)
        answered = False
        try:
            heard = await self._client.transcribe(self._route, pcm, language=language, hint=hint)
            answered = True
            return heard
        except SpeechError as error:
            _list_again_after(error)
            raise
        finally:
            await self._settle(call_id, seconds, 0, answered=answered)

    async def voice_print(self, pcm: bytes) -> Print:
        self._expects("voice_print")
        seconds = len(pcm) / BYTES_PER_SECOND
        call_id = await self._reserve(heard_seconds=seconds, said_chars=0)
        answered = False
        try:
            result = await self._client.voice_print(self._route, pcm)
            answered = True
            return result
        except SpeechError as error:
            _list_again_after(error)
            raise
        finally:
            await self._settle(call_id, seconds, 0, answered=answered)

    async def say(
        self, text: str, *, voice: str | None, language: str | None
    ) -> AsyncGenerator[bytes]:
        self._expects("say")
        call_id = await self._reserve(heard_seconds=0.0, said_chars=len(text))
        answered = False
        try:
            inner = self._client.say(self._route, text, voice=voice, language=language)
            async with aclosing(inner) as chunks:
                async for chunk in chunks:
                    answered = True
                    yield chunk
        except SpeechError as error:
            _list_again_after(error)
            raise
        finally:
            await self._settle(call_id, 0.0, len(text), answered=answered)

    async def _reserve(self, *, heard_seconds: float, said_chars: int) -> UUID:
        """The reservation, in a transaction of its own; ``SpeechError`` when the call may not be
        made (``limit_reached``, ``no_model``)."""
        most = speech_cost_usd(self._prices, heard_seconds=heard_seconds, said_chars=said_chars)
        async with self._session_factory() as session:
            try:
                zone = (await load_policy_settings(session)).time_zone
                reserved = await reserve(
                    session,
                    route=self._route,
                    person_id=self._person_id,
                    max_usd=most,
                    now=self._clock(),
                    catalog=self._catalog,
                    zone=zone,
                )
            except ModelCallError:
                await session.commit()
                raise SpeechError("no_model") from None
            await session.commit()
        if isinstance(reserved, LimitReached):
            logger.info("A call of sound was not made: it would pass a spending limit")
            raise SpeechError("limit_reached")
        return reserved

    async def _settle(
        self, call_id: UUID, heard_seconds: float, said_chars: int, *, answered: bool
    ) -> None:
        """Write what the call cost, once it is over, shielded from a cancellation and within a
        time limit; a settlement that fails is logged by the kind of error only (the upkeep then
        settles the reservation at its maximum)."""
        with anyio.move_on_after(_SETTLE_SECONDS, shield=True):
            try:
                async with self._session_factory() as session:
                    cost = await settle_speech(
                        session,
                        call_id,
                        heard_seconds=heard_seconds,
                        said_chars=said_chars,
                        prices=self._prices,
                        failed_before_any_answer=not answered,
                        now=self._clock(),
                    )
                    await session.commit()
                self._cost += cost
            except Exception as error:
                logger.error("A call of sound could not be settled (%s)", type(error).__name__)
            return
        logger.error("Settling a call of sound took too long")
