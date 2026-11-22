import pytest
from domo_core.llm.models import Provider, ProviderUser
from domo_core.llm.spending_models import SpendingLimit
from sqlalchemy.exc import IntegrityError


async def _key(session, owner, *, general=False):
    provider = Provider(
        kind="anthropic",
        base_url="https://api.example",
        given_by=owner.id,
        is_general=general,
    )
    session.add(provider)
    await session.flush()
    return provider


async def test_one_general_key_at_most(session, add_person):
    owner = await add_person(role="owner")
    await _key(session, owner, general=True)
    await _key(session, owner)
    await _key(session, owner)
    with pytest.raises(IntegrityError):
        await _key(session, owner, general=True)


async def test_a_person_access_names_its_person(session, add_person):
    owner = await add_person(role="owner")
    key = await _key(session, owner)
    session.add(
        ProviderUser(
            provider_id=key.id, scope="person", person_id=None, allowed=True, set_by=owner.id
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_limit_is_more_than_nothing(session, add_person):
    owner = await add_person(role="owner")
    key = await _key(session, owner)
    session.add(
        SpendingLimit(provider_id=key.id, amount=0, currency="USD", period="month", set_by=owner.id)
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_limit_per_key_and_person(session, add_person):
    owner = await add_person(role="owner")
    key = await _key(session, owner)
    for _ in range(2):
        session.add(
            SpendingLimit(
                provider_id=key.id, amount=5, currency="USD", period="month", set_by=owner.id
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()
