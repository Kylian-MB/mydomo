"""Where the tasks of sound go: hearing stays at home unless the owner says otherwise, saying goes
wherever a voice is, and a chat model never hears nor a speech model converses."""

from collections.abc import Sequence

from domo_core.accounts.models import Person
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.models import ModelChoices, Provider, ProviderModel, new_provider_id
from domo_core.llm.router import choose_route
from domo_core.llm.speech.at_home import AtHome
from domo_core.llm.speech.catalog import does_of, speech_prices
from domo_core.llm.speech.routes import speech_route
from domo_core.llm.tasks import HOME_ONLY, PROTECTED, TASKS
from domo_core.policy.settings import HEAR_OUTSIDE_KEY, PolicySettings
from sqlalchemy.ext.asyncio import AsyncSession

# Where each key leads, as the gatekeeper would say (``key`` records it): the routes only ask.
ZONES: dict[str, str] = {}


class FakeGatekeeper:
    async def provider_zone(self, provider_id: str) -> str:
        if provider_id not in ZONES:
            raise GatekeeperError("GET /internal/providers/{id}/zone", status=404)
        return ZONES[provider_id]


AT_HOME = AtHome(FakeGatekeeper())  # type: ignore[arg-type]
HEARING = "whisper-1"
CHAT = "example-haiku-1"
NO = PolicySettings({})
YES = PolicySettings({HEAR_OUTSIDE_KEY: "yes"})


async def owner_of(session: AsyncSession) -> Person:
    someone = Person(display_name="Owner", role="owner")
    session.add(someone)
    await session.flush()
    return someone


async def key(
    session: AsyncSession,
    giver: Person,
    models: Sequence[str],
    *,
    local: bool = False,
    keyless_at: str | None = None,
    zone: str | None = None,
) -> Provider:
    """A key of ``giver`` that lists ``models``: a server of the house with no key (``local``), or
    one reached over https with a key in the vault. ``zone`` is where the gatekeeper says its
    address leads (the house for a ``local`` one, else the internet)."""
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind="openai_compatible",
        base_url=keyless_at
        or ("http://192.168.1.10:8000/v1" if local else "https://speech.example.com/v1"),
        secret_ref=None if local or keyless_at else f"provider:{provider_id}:api_key",
        given_by=giver.id,
    )
    session.add(row)
    await session.flush()
    ZONES[provider_id] = zone or ("house" if local else "internet")
    session.add_all(ProviderModel(provider_id=provider_id, model_id=m) for m in models)
    await session.flush()
    return row


async def test_hearing_goes_home_only_by_default(session):
    owner = await owner_of(session)
    paid = await key(session, owner, [HEARING, CHAT])
    assert (
        await speech_route(
            session, owner, "hear", catalog=shipped_catalog(), settings=NO, at_home=AT_HOME
        )
        is None
    )
    home = await key(session, owner, [HEARING], local=True)
    route = await speech_route(
        session, owner, "hear", catalog=shipped_catalog(), settings=NO, at_home=AT_HOME
    )
    assert route is not None
    assert (route.provider.id, route.model_id, route.task) == (home.id, HEARING, "hear")
    assert paid.id != home.id


async def test_the_owner_may_let_hearing_go_outside(session):
    owner = await owner_of(session)
    paid = await key(session, owner, [HEARING])
    route = await speech_route(
        session, owner, "hear", catalog=shipped_catalog(), settings=YES, at_home=AT_HOME
    )
    assert route is not None and route.provider.id == paid.id


async def test_a_phrase_awaited_as_proof_is_heard_at_home_whatever_the_setting(session):
    owner = await owner_of(session)
    await key(session, owner, [HEARING])
    assert (
        await speech_route(
            session,
            owner,
            "hear",
            catalog=shipped_catalog(),
            settings=YES,
            at_home=AT_HOME,
            for_a_proof=True,
        )
        is None
    )
    home = await key(session, owner, [HEARING], local=True)
    route = await speech_route(
        session,
        owner,
        "hear",
        catalog=shipped_catalog(),
        settings=YES,
        at_home=AT_HOME,
        for_a_proof=True,
    )
    assert route is not None and route.provider.id == home.id


async def test_saying_goes_where_a_voice_is(session):
    owner = await owner_of(session)
    paid = await key(session, owner, ["tts-1", CHAT])
    route = await speech_route(
        session, owner, "say", catalog=shipped_catalog(), settings=NO, at_home=AT_HOME
    )
    assert route is not None
    assert (route.provider.id, route.model_id) == (paid.id, "tts-1")


async def test_attention_and_sorting_are_home_only_and_take_a_chat_model(session):
    owner = await owner_of(session)
    await key(session, owner, [CHAT, HEARING])
    for task in ("attention", "heard_sorting"):
        assert task in HOME_ONLY
        assert (
            await speech_route(
                session, owner, task, catalog=shipped_catalog(), settings=NO, at_home=AT_HOME
            )
            is None
        )
    home = await key(session, owner, [HEARING, CHAT], local=True)
    for task in ("attention", "heard_sorting"):
        route = await speech_route(
            session, owner, task, catalog=shipped_catalog(), settings=NO, at_home=AT_HOME
        )
        assert route is not None
        assert (route.provider.id, route.model_id) == (home.id, CHAT)


async def test_the_household_keys_serve_when_nobody_is_named(session):
    owner = await owner_of(session)
    general = await key(session, owner, ["tts-1"])
    general.is_general = True
    await session.flush()
    route = await speech_route(
        session, None, "say", catalog=shipped_catalog(), settings=NO, at_home=AT_HOME
    )
    assert route is not None and route.provider.id == general.id


async def test_a_chat_model_is_never_picked_to_hear(session):
    owner = await owner_of(session)
    home = await key(session, owner, [CHAT], local=True)
    assert (
        await choose_route(session, provider=home, task="hear", catalog=shipped_catalog()) is None
    )
    assert (
        await speech_route(
            session, owner, "hear", catalog=shipped_catalog(), settings=YES, at_home=AT_HOME
        )
        is None
    )


async def test_a_hearing_model_is_never_picked_to_converse(session):
    owner = await owner_of(session)
    speech = await key(session, owner, [HEARING, "tts-1"])
    assert (
        await choose_route(session, provider=speech, task="conversation", catalog=shipped_catalog())
        is None
    )


def test_the_five_new_kinds_stay_out_of_the_protected_ones():
    assert not PROTECTED & {"hear", "say", "voice_print", "attention", "heard_sorting"}
    assert [TASKS[k].does for k in ("hear", "say", "voice_print", "attention")] == [
        "hear",
        "say",
        "voice_print",
        "chat",
    ]


def test_the_speech_file_says_what_a_model_does_and_costs():
    assert does_of("openai", "whisper-1") == "hear"
    assert does_of("openai_compatible", "Systran/faster-whisper-small") == "hear"
    assert does_of("openai", "gpt-4o-mini-tts") == "say"
    assert does_of("openai", "gpt-4o") == "chat"
    prices = speech_prices("openai", "whisper-1")
    assert prices is not None and str(prices.per_minute_heard) == "0.006"
    assert speech_prices("openai", "gpt-4o") is None


async def test_a_keyless_server_at_a_public_address_is_not_at_home(session):
    owner = await owner_of(session)
    await key(session, owner, [HEARING, CHAT], keyless_at="https://speech.example.com/v1")
    catalog = shipped_catalog()
    for task in ("hear", "attention", "heard_sorting", "voice_print"):
        assert (
            await speech_route(session, owner, task, catalog=catalog, settings=NO, at_home=AT_HOME)
            is None
        )
    assert (
        await speech_route(
            session, owner, "hear", catalog=catalog, settings=YES, at_home=AT_HOME, for_a_proof=True
        )
        is None
    )
    # Saying may go there; a keyless server at a private address is at home.
    private = await key(
        session, owner, [HEARING], keyless_at="http://10.0.0.5:8000/v1", zone="house"
    )
    route = await speech_route(
        session, owner, "hear", catalog=catalog, settings=NO, at_home=AT_HOME
    )
    assert route is not None and route.provider.id == private.id


async def test_a_pinned_hearing_model_on_an_outside_key_is_not_routed_there(session):
    owner = await owner_of(session)
    paid = await key(session, owner, [HEARING, "whisper-large"])
    session.add(
        ModelChoices(
            scope=f"provider:{paid.id}", choices={"tasks": {"hear": {"model": "whisper-large"}}}
        )
    )
    await session.flush()
    catalog = shipped_catalog()
    assert (
        await speech_route(session, owner, "hear", catalog=catalog, settings=NO, at_home=AT_HOME)
        is None
    )
    route = await speech_route(
        session, owner, "hear", catalog=catalog, settings=YES, at_home=AT_HOME
    )
    assert route is not None and route.model_id == "whisper-large"


async def test_a_proof_with_a_home_key_that_cannot_hear_does_not_go_outside(session):
    owner = await owner_of(session)
    await key(session, owner, [CHAT], local=True)
    await key(session, owner, [HEARING])
    assert (
        await speech_route(
            session,
            owner,
            "hear",
            catalog=shipped_catalog(),
            settings=YES,
            at_home=AT_HOME,
            for_a_proof=True,
        )
        is None
    )


async def test_the_host_and_the_house_are_home_the_rest_is_not(session):
    owner = await owner_of(session)
    catalog = shipped_catalog()
    outside = {}
    for zone in ("internet", "never"):
        outside[zone] = await key(
            session, owner, [HEARING], keyless_at="http://x.test/v1", zone=zone
        )
    assert (
        await speech_route(session, owner, "hear", catalog=catalog, settings=NO, at_home=AT_HOME)
        is None
    )
    host = await key(
        session, owner, [HEARING], keyless_at="http://host.docker.internal:8083/v1", zone="host"
    )
    route = await speech_route(
        session, owner, "hear", catalog=catalog, settings=NO, at_home=AT_HOME
    )
    assert route is not None and route.provider.id == host.id


async def test_a_gatekeeper_that_does_not_answer_puts_nothing_at_home(session):
    owner = await owner_of(session)
    unknown = await key(session, owner, [HEARING], local=True)
    del ZONES[unknown.id]  # the gatekeeper does not know the key: it answers an error
    catalog = shipped_catalog()
    for for_a_proof in (False, True):
        found = await speech_route(
            session,
            owner,
            "hear",
            catalog=catalog,
            settings=YES,
            at_home=AT_HOME,
            for_a_proof=for_a_proof,
        )
        assert (found is None) == for_a_proof
