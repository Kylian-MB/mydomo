"""A name means the right record, through links, with or without an account.

``resolve_person(among="records")`` looks among every record the assistant may name, whatever its
state, and tells two records of the same name apart by what is confirmed of them: a link, or where
they stand. ``among="household"`` stays among those who have access now.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from domo_core.accounts.models import Person
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.service import records_meant, resolve_person
from domo_core.llm.types import ToolCall
from domo_core.people import links, records
from domo_core.tools.context import InvalidArguments
from sqlalchemy import update
from test_concerned_person import _judge, _people, _turn

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
WHY = "Paul says his friend Tom comes on Saturday"


async def _known(session, name: str, *, standing: str = "visiting") -> Person:
    """A record with no access and no account: someone the assistant only knows."""
    return await records.open_record(
        session,
        name=name,
        standing=standing,  # type: ignore[arg-type]
        is_child=False,
        by="domo",
        reason=WHY,
        turn_of=None,
        now=NOW,
    )


async def _link(session, start: Person, relation: str, end: Person, said_by: Person | None):
    return await links.note_link(
        session,
        from_person=start,
        relation=relation,
        to_person=end,
        said_by=said_by,
        # What a person says is their own request, which can make a link stand; his idea cannot.
        by="domo" if said_by is None else f"person:{said_by.id}",
        reason=WHY if said_by is None else None,
        turn_of=None,
        now=NOW,
    )


async def _refused(session, name: str, **more) -> InvalidArguments:
    with pytest.raises(InvalidArguments) as error:
        await resolve_person(session, name, now=NOW, **more)
    return error.value


async def test_my_mother_means_the_record_the_link_says(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await _known(session, "Dana Roy")
    await _known(session, "Mina")
    await _link(session, dana, "mother of", alex, said_by=alex)
    for words in ("my mother", "My  Mother", "mother"):
        found = await resolve_person(session, words, now=NOW, speaker=alex.id, among="records")
        assert found.id == dana.id, words
    # In French the relation reads "mère de": "ma mère" is that record too.
    mere = await _known(session, "Lou")
    await _link(session, mere, "mère de", alex, said_by=alex)
    found = await resolve_person(session, "ma mère", now=NOW, speaker=alex.id, among="records")
    assert found.id == mere.id
    # Without a speaker, or among the household, a relation word means nobody.
    assert (await _refused(session, "my mother", among="records")).code == "not_found"
    assert (await _refused(session, "my mother", speaker=alex.id)).code == "not_found"


async def test_a_guest_whose_stay_is_over_is_still_found_by_name_and_has_no_access(
    session, add_person
):
    await add_person("Alex", "owner")
    sam = await add_person("Sam", "guest", state="has_access", expires_at=NOW - timedelta(days=1))
    found = await resolve_person(session, "Sam", now=NOW, among="records")
    assert found.id == sam.id
    assert (await _refused(session, "Sam")).code == "not_found"
    # The same for a record that never had an access.
    tom = await _known(session, "Tom")
    assert (await resolve_person(session, "Tom", now=NOW, among="records")).id == tom.id
    assert (await _refused(session, "Tom")).code == "not_found"


async def test_two_toms_are_told_apart_and_nobody_is_picked(session, add_person):
    paul = await add_person("Paul", "child")
    dana = await add_person("Dana", "adult")
    friend = await _known(session, "Tom")
    neighbour = await _known(session, "Tom", standing="elsewhere")
    await _link(session, friend, "friend of", paul, said_by=paul)
    await _link(session, neighbour, "neighbour of", dana, said_by=dana)
    error = await _refused(session, "Tom", among="records", detailed=True)
    assert error.code == "ambiguous"
    assert "friend of Paul" in error.detail and "neighbour" in error.detail
    assert str(friend.id) not in error.detail and str(neighbour.id) not in error.detail
    # A first name or a beginning that fits two records tells them apart the same way.
    error = await _refused(session, "To", among="records", detailed=True)
    assert error.code == "ambiguous" and "friend of Paul" in error.detail


async def test_an_unconfirmed_link_never_resolves_a_name(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await _known(session, "Sam")
    # Dana says Sam is Alex's brother: it is her word, not a fact, until Alex or Sam confirms.
    link = await _link(session, sam, "brother of", alex, said_by=dana)
    assert link.confirmed_at is None
    assert (await _refused(session, "my brother", speaker=alex.id, among="records")).code == (
        "not_found"
    )
    # Nor does it tell two records apart.
    await _known(session, "Sam")
    error = await _refused(session, "Sam", among="records", detailed=True)
    assert error.code == "ambiguous" and "brother" not in error.detail
    await links.confirm(session, link, by_person=alex, now=NOW)
    found = await resolve_person(session, "my brother", now=NOW, speaker=alex.id, among="records")
    assert found.id == sam.id


async def test_my_parents_gives_both_records(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await _known(session, "Dana")
    sam = await _known(session, "Sam")
    pal = await _known(session, "Tom")
    await _link(session, dana, "mother of", alex, said_by=alex)
    await _link(session, sam, "father of", alex, said_by=alex)
    await _link(session, pal, "friend of", alex, said_by=alex)
    unconfirmed = await _known(session, "Mina")
    await _link(session, unconfirmed, "sister of", alex, said_by=dana)
    # The assistant chooses among the records linked to the speaker by a confirmed link.
    meant = await records_meant(session, "my parents", speaker=alex.id, now=NOW)
    assert {p.id for p in meant} == {dana.id, sam.id, pal.id}
    # A word that fits a relation narrows them down.
    friends = await records_meant(session, "my friends", speaker=alex.id, now=NOW)
    assert [p.id for p in friends] == [pal.id]
    # Through a named person: "Dana's friends" and "the friends of Dana".
    await _link(session, sam, "friend of", dana, said_by=alex)
    for words in ("Dana's friends", "the friends of Dana", "Dana's friends and children"):
        found = await records_meant(session, words, speaker=alex.id, now=NOW)
        assert [p.id for p in found] == [sam.id], words
    # Nobody linked: nothing.
    assert await records_meant(session, "my children", speaker=pal.id, now=NOW) == []


async def test_a_forgotten_or_merged_record_is_never_meant(session, add_person):
    alex = await add_person("Alex", "owner")
    gone = await _known(session, "Tom")
    kept = await _known(session, "Lou")
    twin = await _known(session, "Mina")
    for record in (gone, twin):
        await _link(session, record, "friend of", alex, said_by=alex)
    await session.execute(update(Person).where(Person.id == gone.id).values(forgotten_at=NOW))
    await session.execute(update(Person).where(Person.id == twin.id).values(merged_into_id=kept.id))
    await session.refresh(gone)
    await session.refresh(twin)
    for name in ("Tom", "Mina"):
        error = await _refused(session, name, among="records")
        assert error.code == "not_found", name
    assert (await resolve_person(session, "Lou", now=NOW, among="records")).id == kept.id
    assert await records_meant(session, "my friends", speaker=alex.id, now=NOW) == []


def _list():
    return ToolCall("l", "household_list", json.dumps({}))


async def test_the_household_list_shows_records_only_to_the_owner_and_the_assistant(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        await records.open_record(
            writer,
            name="Tom",
            standing="visiting",
            is_child=False,
            by="domo",
            reason=WHY,
            turn_of=None,
            now=NOW,
        )
        await writer.commit()

    def names(result):
        return {p["name"]: p for p in result.model_result["people"]}  # type: ignore[index]

    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    owner = await handle_tool_call(_list(), ctx=ctx, reply_id=reply_id)
    shown = names(owner)
    assert owner.status == "done" and set(shown) == {"Alex", "Dana", "Tom"}
    assert (shown["Tom"]["state"], shown["Tom"]["standing"], shown["Tom"]["has_account"]) == (
        "known",
        "visiting",
        False,
    )
    assert shown["Dana"]["has_account"] is True and shown["Dana"]["role"] == "adult"
    # Anyone else: those who have access, as before, without states.
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    other = await handle_tool_call(_list(), ctx=ctx, reply_id=reply_id)
    shown = names(other)
    assert set(shown) == {"Alex", "Dana"} and "state" not in shown["Dana"]
    # A turn the assistant acts in on his own sees the records too.
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"], on_his_own=True)
    his = await handle_tool_call(_list(), ctx=ctx, reply_id=reply_id)
    assert set(names(his)) == {"Alex", "Dana", "Tom"}


async def test_two_toms_are_told_apart_only_to_the_owner_and_the_assistant(
    session_factory, monkeypatch
):
    from domo_core.tools.people_tools import record_named

    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Sam", "guest"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        paul = await records.open_record(
            writer, name="Paul", standing="lives_here", is_child=True,
            by="domo", reason=WHY, turn_of=None, now=NOW,
        )  # fmt: skip
        toms = [
            await records.open_record(
                writer,
                name="Tom",
                standing=standing,
                is_child=False,
                by="domo",
                reason=WHY,
                turn_of=None,
                now=NOW,
            )  # fmt: skip
            for standing in ("visiting", "elsewhere")
        ]
        await _link(writer, toms[0], "friend of", paul, said_by=people["Alex"])
        await writer.commit()

    async def detail(person, **changes) -> str:
        ctx, _, _ = await _turn(session_factory, person, **changes)
        async with session_factory() as reader:
            with pytest.raises(InvalidArguments) as error:
                await record_named(reader, "Tom", ctx)
        assert error.value.code == "ambiguous"
        return error.value.detail or ""

    for asked in (people["Dana"], people["Sam"]):
        plain = await detail(asked)
        for secret in ("friend of", "Paul", "visiting", "elsewhere", "person:"):
            assert secret not in plain, (asked.display_name, secret)
    # The assistant's own turn is told them apart, as the owner's request is.
    for told in (await detail(people["Alex"]), await detail(people["Dana"], on_his_own=True)):
        assert "friend of Paul" in told and "elsewhere" in told and "person:" in told
