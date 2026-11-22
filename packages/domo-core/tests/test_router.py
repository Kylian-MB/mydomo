"""The keys of a person, in order, and the route of a call: who may use a key (only with the
agreement of the one who gave it), what a line of access gives or takes away, and the choices of
Domo that a key may have of its own."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from domo_core.accounts.models import Person
from domo_core.llm.catalog import shipped_catalog
from domo_core.llm.choices import (
    NO_CHOICES,
    Choices,
    TaskChoice,
    choices_as_json,
    choices_from_json,
    load_choices,
    merged,
)
from domo_core.llm.models import (
    ModelChoices,
    Provider,
    ProviderModel,
    ProviderUser,
    new_provider_id,
)
from domo_core.llm.router import allowed, choose_route, key_label, usable_keys, wanted_level
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

PILOT_MODELS = ("example-haiku-1", "example-sonnet-1", "example-opus-1")


async def person(
    session: AsyncSession, name: str, role: str, *, invited_by: Person | None = None
) -> Person:
    someone = Person(
        display_name=name, role=role, invited_by=None if invited_by is None else invited_by.id
    )
    session.add(someone)
    await session.flush()
    return someone


async def key(
    session: AsyncSession,
    giver: Person,
    *,
    general: bool = False,
    models: Sequence[str] = PILOT_MODELS,
) -> Provider:
    """A key of ``giver`` (an OpenAI-compatible server reached over https, with a key in the
    vault) that lists ``models``."""
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind="openai_compatible",
        base_url="https://llm.example.com/v1",
        secret_ref=f"provider:{provider_id}:api_key",
        given_by=giver.id,
        is_general=general,
    )
    session.add(row)
    await session.flush()
    session.add_all(ProviderModel(provider_id=provider_id, model_id=m) for m in models)
    await session.flush()
    return row


async def access(
    session: AsyncSession,
    provider: Provider,
    scope: str,
    *,
    allow: bool,
    set_by: Person,
    who: Person | None = None,
) -> None:
    session.add(
        ProviderUser(
            provider_id=provider.id,
            scope=scope,
            person_id=None if who is None else who.id,
            allowed=allow,
            set_by=set_by.id,
        )
    )
    await session.flush()


def ids(keys: Sequence[Provider]) -> list[str]:
    return [k.id for k in keys]


# --- the keys of a person ------------------------------------------------------------------------


async def test_the_keys_of_a_person_come_in_order(session):
    owner = await person(session, "Owner", "owner")
    dana = await person(session, "Dana", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=dana)
    general = await key(session, owner, general=True)
    danas = await key(session, dana)
    await access(session, danas, "invitees", allow=True, set_by=dana)
    robins = await key(session, robin)

    # Theirs, then the one of who invited them, then the general key.
    assert ids(await usable_keys(session, robin)) == [robins.id, danas.id, general.id]
    # The one who invited them uses their own key first, and never the guest's.
    assert ids(await usable_keys(session, dana)) == [danas.id, general.id]
    assert ids(await usable_keys(session, owner)) == [general.id]


async def test_the_general_key_is_for_the_whole_household_by_default(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    sam = await person(session, "Sam", "child", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    general = await key(session, owner, general=True)
    alexs = await key(session, alex)

    for someone in (owner, sam, robin):
        assert ids(await usable_keys(session, someone)) == [general.id], someone.display_name
    # A key that is not the general one is only its giver's.
    assert ids(await usable_keys(session, alex)) == [alexs.id, general.id]
    everyone = {owner.id: owner, alex.id: alex, sam.id: sam, robin.id: robin}
    assert allowed(alexs, [], alex, people=everyone)
    assert not allowed(alexs, [], owner, people=everyone)


async def test_a_key_of_someone_else_is_never_used_without_its_giver(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    general = await key(session, owner, general=True)
    alexs = await key(session, alex)

    # The owner gives himself the key of Alex, for himself or for the whole household: nothing.
    await access(session, alexs, "person", allow=True, set_by=owner, who=owner)
    await access(session, alexs, "household", allow=True, set_by=owner)
    assert ids(await usable_keys(session, owner)) == [general.id]

    # Alex says yes (his line replaces the owner's, as sharing a key does): now he may.
    await session.execute(
        update(ProviderUser)
        .where(ProviderUser.provider_id == alexs.id, ProviderUser.scope == "person")
        .values(set_by=alex.id)
    )
    assert ids(await usable_keys(session, owner)) == [general.id, alexs.id]


async def test_a_person_row_takes_away_the_general_key(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    sam = await person(session, "Sam", "child", invited_by=owner)
    general = await key(session, owner, general=True)

    # A rule of the house: Alex brings his own.
    await access(session, general, "person", allow=False, set_by=owner, who=alex)
    assert await usable_keys(session, alex) == []
    assert ids(await usable_keys(session, sam)) == [general.id]


async def test_a_person_row_wins_over_the_household_row(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    sam = await person(session, "Sam", "child", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    dana = await person(session, "Dana", "guest", invited_by=owner)
    general = await key(session, owner, general=True)

    await access(session, general, "household", allow=False, set_by=owner)
    await access(session, general, "person", allow=True, set_by=owner, who=alex)
    assert ids(await usable_keys(session, alex)) == [general.id]
    assert await usable_keys(session, sam) == []

    # "My invitees" comes between the two: before the household, after the person.
    await access(session, general, "invitees", allow=True, set_by=owner)
    assert ids(await usable_keys(session, sam)) == [general.id]
    assert await usable_keys(session, robin) == []  # invited by Alex, not by the giver
    await access(session, general, "person", allow=False, set_by=owner, who=dana)
    assert await usable_keys(session, dana) == []


async def test_on_the_general_key_a_line_counts_whoever_wrote_it(session):
    """The lines of the general key are written by the rules of the house (``keys_house``), whose
    right is checked when they are written: one that someone given that right wrote counts. On
    any other key, only its giver's word lets someone use it."""
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    paul = await person(session, "Paul", "adult", invited_by=owner)
    general = await key(session, owner, general=True)
    alexs = await key(session, alex)
    await access(session, general, "household", allow=False, set_by=owner)
    assert await usable_keys(session, paul) == []

    await access(session, general, "person", allow=True, set_by=alex, who=paul)
    assert ids(await usable_keys(session, paul)) == [general.id]
    await access(session, alexs, "person", allow=True, set_by=owner, who=paul)
    assert ids(await usable_keys(session, paul)) == [general.id]


async def test_a_disabled_person_has_no_key(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    await key(session, owner, general=True)
    await key(session, alex)
    alex.disabled_at = datetime(2030, 1, 1, tzinfo=UTC)
    await session.flush()

    assert await usable_keys(session, alex) == []


async def test_an_invitee_follows_their_host_on_the_general_key(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    general = await key(session, owner, general=True)

    assert ids(await usable_keys(session, robin)) == [general.id]
    # Alex brings his own: Robin, who has the general key through Alex, loses it too.
    await access(session, general, "person", allow=False, set_by=owner, who=alex)
    assert await usable_keys(session, alex) == []
    assert await usable_keys(session, robin) == []
    # Alex has it again, and Robin follows.
    await session.execute(
        update(ProviderUser)
        .where(ProviderUser.provider_id == general.id, ProviderUser.person_id == alex.id)
        .values(allowed=True)
    )
    assert ids(await usable_keys(session, robin)) == [general.id]


async def test_a_line_for_the_invitee_beats_their_host(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    general = await key(session, owner, general=True)

    # Alex is off the general key, and a line says yes to Robin: Robin has it.
    await access(session, general, "person", allow=False, set_by=owner, who=alex)
    await access(session, general, "person", allow=True, set_by=owner, who=robin)
    assert await usable_keys(session, alex) == []
    assert ids(await usable_keys(session, robin)) == [general.id]
    # Alex is back on it, and a line says no to Robin: Robin does not.
    await session.execute(
        update(ProviderUser).where(ProviderUser.person_id == alex.id).values(allowed=True)
    )
    await session.execute(
        update(ProviderUser).where(ProviderUser.person_id == robin.id).values(allowed=False)
    )
    assert ids(await usable_keys(session, alex)) == [general.id]
    assert await usable_keys(session, robin) == []


async def test_an_invitee_never_gets_more_than_their_host_through_the_house_lines(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    general = await key(session, owner, general=True)

    await access(session, general, "household", allow=False, set_by=owner)
    await access(session, general, "person", allow=True, set_by=owner, who=alex)
    assert ids(await usable_keys(session, alex)) == [general.id]
    assert await usable_keys(session, robin) == []


async def test_an_invitee_follows_the_rules_for_a_host_who_left_as_if_they_were_there(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    general = await key(session, owner, general=True)
    assert ids(await usable_keys(session, robin)) == [general.id]

    # Alex left and has no line: Alex has no key, but Robin keeps the general key.
    alex.disabled_at = datetime(2030, 1, 1, tzinfo=UTC)
    await session.flush()
    assert await usable_keys(session, alex) == []
    assert ids(await usable_keys(session, robin)) == [general.id]

    # A line takes the general key away from Alex who left: Robin does not follow.
    await access(session, general, "person", allow=False, set_by=owner, who=alex)
    assert await usable_keys(session, robin) == []


async def test_whoever_the_owner_invited_keeps_the_general_key_after_a_hand_over(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=owner)
    sam = await person(session, "Sam", "guest", invited_by=alex)
    general = await key(session, owner, general=True)

    # The role goes to Alex (it does not matter here) and the former owner is removed.
    owner.disabled_at = datetime(2030, 1, 1, tzinfo=UTC)
    await session.flush()

    assert await usable_keys(session, owner) == []
    for someone in (alex, robin, sam):
        assert ids(await usable_keys(session, someone)) == [general.id]


async def test_following_a_host_stops_on_a_loop_or_a_missing_host(session):
    owner = await person(session, "Owner", "owner")
    a = await person(session, "Alex", "adult", invited_by=owner)
    b = await person(session, "Sam", "adult", invited_by=a)
    c = await person(session, "Robin", "guest", invited_by=b)
    general = await key(session, owner, general=True)
    everyone = {someone.id: someone for someone in (owner, a, b, c)}
    assert allowed(general, [], c, people=everyone)

    # The chain A -> B -> C, with A off the general key: C does not have it.
    await access(session, general, "person", allow=False, set_by=owner, who=a)
    lines = list((await session.execute(select(ProviderUser))).scalars())
    assert not allowed(general, lines, c, people=everyone)

    # A forged loop (A was invited by C): no key by that road, and no RecursionError.
    a.invited_by = c.id
    assert not allowed(general, [], c, people=everyone)
    assert not allowed(general, [], a, people=everyone)

    # A host who is not among the people: no.
    c.invited_by = uuid4()
    assert not allowed(general, [], c, people=everyone)
    c.invited_by = b.id
    assert not allowed(general, [], c, people={})


async def test_following_a_host_is_only_for_the_general_key(session):
    owner = await person(session, "Owner", "owner")
    alex = await person(session, "Alex", "adult", invited_by=owner)
    robin = await person(session, "Robin", "guest", invited_by=alex)
    general = await key(session, owner, general=True)
    alexs = await key(session, alex)

    assert ids(await usable_keys(session, robin)) == [general.id]
    assert ids(await usable_keys(session, alex)) == [alexs.id, general.id]


# --- the choices of Domo and the route ---------------------------------------------------------


async def choose(session: AsyncSession, scope: str, choices: dict[str, object]) -> None:
    session.add(ModelChoices(scope=scope, choices=choices))
    await session.flush()


async def test_choices_of_a_key_win_over_the_household(session):
    owner = await person(session, "Owner", "owner")
    general = await key(session, owner, general=True)
    other = await key(session, owner)
    await choose(
        session,
        "household",
        {"tasks": {"conversation": {"level": "small"}}, "output_tokens": 500},
    )
    await choose(
        session, f"provider:{general.id}", {"tasks": {"conversation": {"level": "strong"}}}
    )

    on_general = await load_choices(session, general.id)
    assert on_general.tasks["conversation"] == TaskChoice(level="strong")
    assert on_general.output_tokens == 500  # what the key does not say stays the household's
    assert (await load_choices(session, other.id)).tasks["conversation"] == TaskChoice("small")

    catalog = shipped_catalog()
    route = await choose_route(session, provider=general, task="conversation", catalog=catalog)
    assert route is not None and (route.model_id, route.level) == ("example-opus-1", "strong")
    route = await choose_route(session, provider=other, task="conversation", catalog=catalog)
    assert route is not None and route.model_id == "example-haiku-1"


async def test_a_route_says_its_key_its_model_and_its_task(session):
    owner = await person(session, "Owner", "owner")
    general = await key(session, owner, general=True)

    route = await choose_route(session, provider=general, task="judge", catalog=shipped_catalog())
    assert route is not None
    assert (route.provider.id, route.model_id, route.level, route.task) == (
        general.id,
        "example-sonnet-1",
        "medium",
        "judge",
    )
    # A key the catalog does not know is priced at the highest price, and says so.
    assert route.price_known is False
    # One step up: the strongest.
    raised = await choose_route(
        session, provider=general, task="conversation", catalog=shipped_catalog(), raised=1
    )
    assert raised is not None and raised.model_id == "example-opus-1"


def test_a_key_label_names_its_giver_and_the_general_key():
    general = Provider(
        id="p-00000001",
        kind="openai_compatible",
        base_url="https://llm.example.com/v1",
        is_general=True,
    )
    alexs = Provider(id="p-00000002", kind="openai", base_url="https://api.openai.com/v1")
    assert key_label(general, "Owner") == "openai_compatible key of Owner, general"
    assert key_label(alexs, "Alex") == "openai key of Alex"


async def test_a_key_without_models_has_no_route(session):
    owner = await person(session, "Owner", "owner")
    empty = await key(session, owner, general=True, models=())

    route = await choose_route(
        session, provider=empty, task="conversation", catalog=shipped_catalog()
    )
    assert route is None


def test_the_level_wanted_starts_from_the_choice_and_never_goes_under_the_minimum():
    small = Choices({"conversation": TaskChoice(level="small"), "judge": TaskChoice("small")})
    assert wanted_level("conversation", NO_CHOICES, raised=0) == "medium"
    assert wanted_level("conversation", small, raised=0) == "small"
    assert wanted_level("conversation", small, raised=1) == "medium"
    assert wanted_level("conversation", small, raised=5) == "strong"
    # A protected task never goes under its minimum, whatever Domo chose.
    assert wanted_level("judge", small, raised=0) == "medium"
    assert wanted_level("upkeep", NO_CHOICES, raised=0) == "small"


def test_choices_read_from_json_drop_what_is_bad():
    read = choices_from_json(
        {
            "tasks": {
                "conversation": {"level": "strong", "model": "example-opus-1"},
                "judge": {"level": "tiny", "model": 3},
                "nope": {"level": "small"},
                "intent": "medium",
            },
            "history_chars": 12000,
            "output_tokens": "many",
            "warn_at_percent": 250,
            "unknown": True,
        }
    )
    assert read == Choices({"conversation": TaskChoice("strong", "example-opus-1")}, 12000)
    assert choices_from_json(choices_as_json(read)) == read
    assert choices_from_json({"tasks": [], "history_chars": True}) == NO_CHOICES
    assert choices_from_json(["not", "an", "object"]) == NO_CHOICES  # type: ignore[arg-type]


def test_the_choices_of_a_key_are_merged_field_by_field():
    household = Choices(
        {
            "conversation": TaskChoice("small"),
            "judge": TaskChoice("strong"),
            "upkeep": TaskChoice("medium", "example-sonnet-1"),
        },
        history_chars=8000,
        warn_at_percent=80,
    )
    of_the_key = Choices(
        {
            # A key that pins a model only keeps the household's level for when it is gone.
            "conversation": TaskChoice(model="example-opus-1"),
            # A key that chooses a level only keeps the household's pinned model.
            "upkeep": TaskChoice("small"),
            "intent": TaskChoice("strong"),
        },
        output_tokens=600,
    )
    assert merged(household, of_the_key) == Choices(
        {
            "conversation": TaskChoice("small", "example-opus-1"),
            "judge": TaskChoice("strong"),
            "upkeep": TaskChoice("small", "example-sonnet-1"),
            "intent": TaskChoice("strong"),
        },
        history_chars=8000,
        output_tokens=600,
        warn_at_percent=80,
    )
    assert merged(household, None) == household


async def test_a_step_up_leaves_a_pinned_model_under_it(session):
    owner = await person(session, "Owner", "owner")
    general = await key(session, owner, general=True)
    session.add(
        ModelChoices(
            scope="household", choices={"tasks": {"conversation": {"model": "example-sonnet-1"}}}
        )
    )
    await session.flush()
    catalog = shipped_catalog()

    pinned = await choose_route(session, provider=general, task="conversation", catalog=catalog)
    assert pinned is not None and pinned.model_id == "example-sonnet-1"
    # The levels the key has for the task, which a step up looks at.
    assert pinned.fit_levels == frozenset({"small", "medium", "strong"})
    # Once the request steps up, a pinned model under the level it needs is left.
    up = await choose_route(
        session, provider=general, task="conversation", catalog=catalog, at_least="strong"
    )
    assert up is not None and up.model_id == "example-opus-1"
    # A pinned model at that level or above stays.
    same = await choose_route(
        session, provider=general, task="conversation", catalog=catalog, at_least="medium"
    )
    assert same is not None and same.model_id == "example-sonnet-1"


async def test_no_choice_without_a_row(session):
    assert await load_choices(session, None) == NO_CHOICES
    assert await load_choices(session, "p-00000000") == NO_CHOICES


def test_the_choices_about_writing_have_their_defaults_and_are_read_and_saved():
    assert (NO_CHOICES.write_now, NO_CHOICES.arrival) == (("requests",), "every_visit")
    read = choices_from_json({"write_now": ["done", "requests", "done"], "arrival": "when_new"})
    assert (read.write_now, read.arrival) == (("requests", "done"), "when_new")
    assert choices_from_json(choices_as_json(read)) == read
    nothing = choices_from_json({"write_now": [], "arrival": "never"})
    assert (nothing.write_now, nothing.arrival) == ((), "never")
    assert choices_from_json(choices_as_json(nothing)) == nothing
    # What is not a known moment is dropped: the default applies.
    odd = choices_from_json({"write_now": ["later"], "arrival": "sometimes"})
    assert odd == NO_CHOICES
    assert choices_from_json({"write_now": "done", "arrival": 3}) == NO_CHOICES
    assert choices_as_json(NO_CHOICES) == {"tasks": {}}
    # They are the house's: a key's choices keep them.
    household = Choices(write_now=(), arrival="never")
    assert merged(household, Choices(history_chars=9000)).arrival == "never"
    assert merged(household, Choices(history_chars=9000)).write_now == ()
