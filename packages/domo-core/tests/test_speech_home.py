"""The speech service of the house is a provider like the others, without a key."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from domo_core.accounts.models import Person
from domo_core.channels.voice.deps import MeteredSpeechOpener
from domo_core.llm import choice, router
from domo_core.llm.catalog import UNKNOWN_PRICES, shipped_catalog
from domo_core.llm.models import Provider, ProviderModel, ProviderUser
from domo_core.llm.speech.at_home import AtHome
from domo_core.llm.speech.catalog import FREE_SPEECH, does_of
from domo_core.llm.speech.client import SpeechError
from domo_core.llm.speech.home import (
    HOME_SPEECH_ID,
    RELIST_AT_MOST_EVERY,
    RELIST_SOON_EVERY,
    RELIST_SOON_FOR,
    SPEECH_LIST_WANTED,
    SpeechListWanted,
    ensure_home_speech,
    speech_home_pass,
)
from domo_core.llm.speech.metered import MeteredSpeech
from domo_core.llm.speech.routes import speech_route
from domo_core.policy.settings import PolicySettings
from fakes import FakeModelLister
from sqlalchemy import func, select

URL = "http://speech:8083/v1"
HEARING = "whisper-1"
SAYING = "tts-1"
PRINTING = "speaker-embedding-1"
# The candidates the service of the house keeps (``domo-speech``), read when its data file is there.
SERVICE_DATA = (
    Path(__file__).parents[2] / "domo-speech" / "src" / "domo_speech" / "data" / "models.json"
)


class AtHomeZone:
    """The gatekeeper's judgement of where an address leads, as one answer for every key."""

    def __init__(self, zone: str) -> None:
        self.zone = zone

    async def provider_zone(self, provider_id: str) -> str:
        return self.zone


@pytest.fixture
async def owner(add_member):
    return await add_member("Alex", "owner")


@pytest.fixture
def lister() -> FakeModelLister:
    found = FakeModelLister()
    found.models = (HEARING, SAYING)
    return found


async def providers(session_factory) -> list[Provider]:
    async with session_factory() as reader:
        return list((await reader.execute(select(Provider))).scalars())


async def model_ids(session_factory, provider_id: str) -> set[str]:
    async with session_factory() as reader:
        found = await reader.execute(
            select(ProviderModel.model_id).where(ProviderModel.provider_id == provider_id)
        )
        return set(found.scalars())


async def test_the_home_service_becomes_a_provider_without_a_key(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    key = await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert key is not None
    (row,) = await providers(session_factory)
    assert (row.id, row.kind, row.base_url, row.secret_ref) == (
        HOME_SPEECH_ID,
        "openai_compatible",
        URL,
        None,
    )
    assert (row.given_by, row.is_general, row.admin_secret_ref) == (owner.id, False, None)
    # Declared to the gatekeeper, both routes and no secret; its models listed through the relay.
    assert {(route.id, route.base_url, route.secret_ref) for route in gk_mock.routes()} == {
        (row.id, URL, None),
        (f"{row.id}-admin", URL, None),
    }
    assert lister.listed == [row.id]
    assert await model_ids(session_factory, row.id) == {HEARING, SAYING}


async def test_declared_twice_is_one_provider(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    for _ in range(2):
        await ensure_home_speech(
            session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
        )
    assert len(await providers(session_factory)) == 1
    async with session_factory() as reader:
        lines = await reader.scalar(select(func.count()).select_from(ProviderUser))
    assert lines == 1
    assert await model_ids(session_factory, HOME_SPEECH_ID) == {HEARING, SAYING}


async def test_a_changed_address_updates_the_same_provider(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    other = "http://192.168.1.30:8083/v1"
    await ensure_home_speech(
        session_factory, gk_client, lister, url=other, owner_id=owner.id, now=clock()
    )
    (row,) = await providers(session_factory)
    assert (row.id, row.base_url) == (HOME_SPEECH_ID, other)
    assert gk_mock.routes()[-1].base_url == other


async def test_everyone_of_the_household_may_use_it(
    owner, add_member, session_factory, gk_client, gk_mock, lister, clock
):
    guest = await add_member("Sam", "guest")
    await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    at_home = AtHome(AtHomeZone("host"))  # type: ignore[arg-type]
    async with session_factory() as session:
        person = await session.get(Person, guest.id)
        assert person is not None
        usable = await router.usable_keys(session, person)
        assert [key.id for key in usable] == [HOME_SPEECH_ID]
        # It hears and says for them, but it is no key to think with.
        assert await router.text_keys(session, person) == []
        for task, model in (("hear", HEARING), ("say", SAYING)):
            route = await speech_route(
                session,
                person,
                task,  # type: ignore[arg-type]
                catalog=shipped_catalog(),
                settings=PolicySettings({}),
                at_home=at_home,
            )
            assert route is not None
            assert (route.provider.id, route.model_id) == (HOME_SPEECH_ID, model)


async def test_no_url_or_no_owner_declares_nothing(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    for url, owner_id in ((None, owner.id), ("", owner.id), (URL, None)):
        key = await ensure_home_speech(
            session_factory, gk_client, lister, url=url, owner_id=owner_id, now=clock()
        )
        assert key is None
    assert await providers(session_factory) == []
    assert gk_mock.routes() == [] and lister.listed == []


async def test_a_service_still_preparing_is_tried_again_by_the_upkeep(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    lister.error = "unreachable"
    upkeep = speech_home_pass(session_factory, lambda: gk_client, lambda: lister, url=URL)
    await upkeep(clock())  # no crash: the key is there, with no model
    (row,) = await providers(session_factory)
    assert await model_ids(session_factory, row.id) == set()
    lister.error = None
    await upkeep(clock())
    assert await model_ids(session_factory, row.id) == {HEARING, SAYING}
    # Once it has its models, the pass leaves the key to the upkeep of the lists.
    asked = len(lister.listed)
    await upkeep(clock())
    assert len(lister.listed) == asked


async def test_a_service_that_gains_a_use_later_is_read_again_when_asked_at_most_once_a_minute(
    owner, session_factory, gk_client, gk_mock, lister
):
    # Final fix wave C: the service's first measure held only its voice; once it holds its hearing
    # too (it started again, or finished measuring later), the core uses it without a restart.
    wanted = SpeechListWanted()
    start = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    lister.models = (SAYING,)
    upkeep = speech_home_pass(
        session_factory, lambda: gk_client, lambda: lister, url=URL, wanted=wanted
    )
    await upkeep(start)
    assert await model_ids(session_factory, HOME_SPEECH_ID) == {SAYING}
    lister.models = (HEARING, SAYING, PRINTING)
    asked = len(lister.listed)
    # Nobody missed a use: nothing is read.
    await upkeep(start + timedelta(minutes=5))
    assert len(lister.listed) == asked
    # A use was missing: read again at the next round, and the hearing is there.
    wanted.want()
    await upkeep(start + timedelta(minutes=5, seconds=10))
    assert len(lister.listed) == asked + 1
    assert await model_ids(session_factory, HOME_SPEECH_ID) == {HEARING, SAYING, PRINTING}
    assert not wanted.wanted
    # Asked again within the minute, nothing missing now: it waits for the minute to pass.
    wanted.want()
    await upkeep(start + timedelta(minutes=5, seconds=40))
    assert len(lister.listed) == asked + 1
    await upkeep(start + timedelta(minutes=6, seconds=10))
    assert len(lister.listed) == asked + 2


async def test_a_use_still_missing_at_a_fresh_start_is_read_again_sooner_for_a_while(
    owner, session_factory, gk_client, gk_mock, lister
):
    # A fresh start: the service listed its hearing and its voice but is still measuring its
    # prints. A use asked for and missing is read again within seconds, not a minute, for as
    # long as the service may still be preparing; never more than once per short interval.
    wanted = SpeechListWanted()
    start = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    lister.models = (HEARING, SAYING)
    upkeep = speech_home_pass(
        session_factory, lambda: gk_client, lambda: lister, url=URL, wanted=wanted
    )
    await upkeep(start)
    asked = len(lister.listed)
    wanted.want()
    await upkeep(start + RELIST_SOON_EVERY / 2)
    assert len(lister.listed) == asked
    await upkeep(start + RELIST_SOON_EVERY)
    assert len(lister.listed) == asked + 1
    # Asked again at once: still one read per short interval at most.
    wanted.want()
    await upkeep(start + RELIST_SOON_EVERY + RELIST_SOON_EVERY / 2)
    assert len(lister.listed) == asked + 1
    await upkeep(start + 2 * RELIST_SOON_EVERY)
    assert len(lister.listed) == asked + 2
    # Past the while a service may take to prepare, a use still missing is one it does not have
    # here: back to once a minute.
    late = start + RELIST_SOON_FOR
    wanted.want()
    await upkeep(late)
    assert len(lister.listed) == asked + 3
    wanted.want()
    await upkeep(late + RELIST_SOON_EVERY)
    assert len(lister.listed) == asked + 3
    await upkeep(late + RELIST_AT_MOST_EVERY)
    assert len(lister.listed) == asked + 4


async def test_a_complete_list_is_read_again_once_a_minute_at_most(
    owner, session_factory, gk_client, gk_mock, lister
):
    wanted = SpeechListWanted()
    start = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    lister.models = (HEARING, SAYING, PRINTING)
    upkeep = speech_home_pass(
        session_factory, lambda: gk_client, lambda: lister, url=URL, wanted=wanted
    )
    await upkeep(start)
    asked = len(lister.listed)
    # A call that failed asks again: nothing is missing, the minute holds.
    wanted.want()
    await upkeep(start + RELIST_SOON_EVERY)
    assert len(lister.listed) == asked
    await upkeep(start + RELIST_AT_MOST_EVERY)
    assert len(lister.listed) == asked + 1


def test_the_reread_waits_a_minute_at_most():
    assert timedelta(minutes=1) == RELIST_AT_MOST_EVERY
    # Sooner while a use is missing, but never one read per sentence.
    assert timedelta(seconds=5) <= RELIST_SOON_EVERY < RELIST_AT_MOST_EVERY < RELIST_SOON_FOR


async def test_a_use_missing_asks_for_the_list_again(owner, session_factory, clock):
    SPEECH_LIST_WANTED.clear()
    opener = MeteredSpeechOpener(
        cast(Any, object()),
        session_factory=session_factory,
        clock=clock,
        at_home=AtHome(AtHomeZone("host")),  # type: ignore[arg-type]
    )
    assert await opener.open(owner, "hear") is None
    assert SPEECH_LIST_WANTED.wanted
    SPEECH_LIST_WANTED.clear()


async def test_a_failed_call_of_sound_asks_for_the_list_again(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    key = await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert key is not None
    route = router.Route(
        provider=key,
        model_id=HEARING,
        level="small",
        prices=UNKNOWN_PRICES,
        task="hear",
        price_known=True,
    )

    class Failing:
        def __init__(self, reason: str) -> None:
            self.reason = reason

        async def transcribe(self, route, pcm, *, language=None):
            raise SpeechError(self.reason)  # type: ignore[arg-type]

    for reason, asks in (("limit_reached", False), ("unreachable", True)):
        SPEECH_LIST_WANTED.clear()
        speech = MeteredSpeech(
            cast(Any, Failing(reason)),
            route=route,
            person_id=owner.id,
            session_factory=session_factory,
            clock=clock,
            catalog=shipped_catalog(),
            prices=FREE_SPEECH,
        )
        with pytest.raises(SpeechError):
            await speech.transcribe(b"\x00\x00" * 16000)
        assert SPEECH_LIST_WANTED.wanted is asks, reason
    SPEECH_LIST_WANTED.clear()


async def test_the_upkeep_waits_for_an_owner(session_factory, gk_client, gk_mock, lister, clock):
    upkeep = speech_home_pass(session_factory, lambda: gk_client, lambda: lister, url=URL)
    await upkeep(clock())
    assert await providers(session_factory) == []
    assert lister.listed == []


async def test_a_gatekeeper_that_refuses_leaves_the_key_for_the_next_round(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    gk_mock.fail_next("register_provider", status=500)
    key = await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert key is not None and lister.listed == []
    await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert await model_ids(session_factory, HOME_SPEECH_ID) == {HEARING, SAYING}


async def test_it_is_local(owner, session_factory, gk_client, gk_mock, lister, clock):
    key = await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert key is not None and choice.is_local(key)


def test_every_model_the_home_service_lists_is_known_to_the_core():
    """Each candidate of the service's data file is classified by the core's file of speech
    models the way the service says (else the voice is silently never chosen). Skipped while the
    service has no data file."""
    if not SERVICE_DATA.exists():
        pytest.skip("the candidates data file of the speech service is not there yet")
    data = json.loads(SERVICE_DATA.read_text(encoding="utf-8"))
    entries = data["models"] if isinstance(data, dict) else data
    assert entries
    for entry in entries:
        assert does_of("openai_compatible", entry["id"]) == entry["does"], entry["id"]


async def test_a_row_at_the_same_address_that_the_owner_did_not_give_is_not_the_service(
    owner, add_member, session_factory, gk_client, gk_mock, lister, clock
):
    # A key someone else recorded at the address of the service must not be taken for it: the
    # address would then be rewritten and its household line shared.
    other = await add_member("Sam", "adult")
    async with session_factory() as writer:
        writer.add(
            Provider(
                id="p-bbbbbbbb",
                kind="openai_compatible",
                base_url=URL,
                secret_ref=None,
                given_by=other.id,
            )
        )
        await writer.commit()
    key = await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert key is not None and key.id == HOME_SPEECH_ID
    rows = {row.id: row for row in await providers(session_factory)}
    assert set(rows) == {"p-bbbbbbbb", HOME_SPEECH_ID}
    assert rows["p-bbbbbbbb"].given_by == other.id


async def test_a_row_the_owner_gave_at_the_address_is_found_not_doubled(
    owner, session_factory, gk_client, gk_mock, lister, clock
):
    async with session_factory() as writer:
        writer.add(
            Provider(
                id="p-bbbbbbbb",
                kind="openai_compatible",
                base_url=URL,
                secret_ref=None,
                given_by=owner.id,
            )
        )
        await writer.commit()
    key = await ensure_home_speech(
        session_factory, gk_client, lister, url=URL, owner_id=owner.id, now=clock()
    )
    assert key is not None and key.id == "p-bbbbbbbb"
    assert len(await providers(session_factory)) == 1


def test_every_print_model_of_the_home_service_has_ordered_thresholds():
    """The service lists each print model's thresholds (its data file is the one source); the
    core reads them (``SpeechClient.print_thresholds``). Skipped while there is no data file."""
    if not SERVICE_DATA.exists():
        pytest.skip("the candidates data file of the speech service is not there yet")
    data = json.loads(SERVICE_DATA.read_text(encoding="utf-8"))
    prints = [entry for entry in data["models"] if entry["does"] == "voice_print"]
    assert prints
    for entry in prints:
        low, same, high = (
            entry["thresholds"][key] for key in ("print_low", "same_voice", "print_high")
        )
        assert 0 <= low < same < high <= 1, entry["id"]
