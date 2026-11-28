"""The memory follows the record: what was said of someone before their record existed can be
attached to it, and memory, trust and nicknames work for a record with no account, through the same
tables and functions as for anyone."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.household.nicknames import PersonNickname
from domo_core.memory.models import Memory
from domo_core.memory.service import free_text_about, keep, unconfirmed
from domo_core.people import directives, records
from domo_core.people.models import PersonAct
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.policy.roles import domo_actor
from domo_core.tools.context import InvalidArguments, ToolContext
from domo_core.tools.registry import TOOLS
from domo_core.trust.models import TrustFact
from sqlalchemy import select

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
LASAGNE = "Tom loves lasagne."


async def household(session_factory: Any) -> tuple[Person, Person]:
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        sam = Person(display_name="Sam", role="adult")
        writer.add_all([alex, sam])
        await writer.commit()
    return alex, sam


def turn(person: Person, session_factory: Any) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=actor_of(person),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
    )


async def run(tool: str, ctx: ToolContext, /, **arguments: Any) -> dict[str, Any]:
    return dict((await TOOLS[tool].execute(arguments, ctx)).model_result)


def free_memory(**more: Any) -> dict[str, Any]:
    return {
        "content": LASAGNE,
        "subject": "Tom",
        "kind": "preference",
        "private": False,
        "core": False,
        **more,
    }


async def stored(session_factory: Any, memory_id: str) -> Memory:
    async with session_factory() as reader:
        return (
            await reader.execute(
                select(Memory)
                .where(Memory.id == uuid.UUID(memory_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()


async def open_tom(ctx: ToolContext) -> dict[str, Any]:
    return await run("record_open", ctx, name="Tom", standing="visiting", ground="comes")


async def test_what_was_said_of_tom_before_his_record_can_be_attached_to_it(session_factory):
    alex, _ = await household(session_factory)
    ctx = turn(alex, session_factory)
    said = await run("memory_keep", ctx, **free_memory())
    other = await run("memory_keep", ctx, **free_memory(subject="the boiler", content="Noisy."))

    opened = await open_tom(ctx)

    # The candidates are listed, never attached by themselves: which speak of him is for the
    # assistant to judge.
    assert opened["free_memories"] == [{"id": said["id"], "subject": "Tom"}]
    assert (await stored(session_factory, said["id"])).about_id is None
    await run("record_complete", ctx, person=f"person:{opened['id']}", memories=[said["id"]])
    assert (await stored(session_factory, said["id"])).about_id == uuid.UUID(opened["id"])
    assert (await stored(session_factory, other["id"])).about_id is None
    found = await run("memory_search", ctx, words="lasagne")
    assert [m["id"] for m in found["memories"]] == [said["id"]]
    # Attached, it is no longer a candidate for any record.
    async with session_factory() as reader:
        assert await free_text_about(reader, "Tom") == []


async def test_free_text_is_found_by_name_first_word_and_nickname_only(session_factory):
    alex, _ = await household(session_factory)
    async with session_factory() as session:
        for subject in ("Tom", "tom lee", "Tommy", "TOM", "me"):
            await keep(
                session,
                said_by=alex.id,
                subject=subject,
                content=f"About {subject}.",
                kind="fact",
                origin="own_words",
                kept_private=False,
                core=False,
                now=NOW,
            )
        await session.commit()
    async with session_factory() as reader:
        by_name = {m.subject for m in await free_text_about(reader, "Tom")}
        by_whole = {m.subject for m in await free_text_about(reader, "Tom Lee")}
        by_nickname = {m.subject for m in await free_text_about(reader, "Tom", nicknames=["Tommy"])}
        limited = await free_text_about(reader, "Tom", limit=1)
    assert by_name == {"Tom", "TOM"}
    assert by_whole == {"tom lee", "Tom", "TOM"}
    assert by_nickname == {"Tom", "TOM", "Tommy"}
    assert len(limited) == 1


async def test_a_memory_about_someone_else_is_never_repointed(session_factory):
    alex, sam = await household(session_factory)
    ctx = turn(alex, session_factory)
    about_sam = await run("memory_keep", ctx, **free_memory(about="Sam"))
    free = await run("memory_keep", ctx, **free_memory())
    opened = await open_tom(ctx)
    async with session_factory() as session:
        tom = await session.get(Person, uuid.UUID(opened["id"]))
        assert tom is not None
        attached = await records.attach(
            session, tom, [uuid.UUID(about_sam["id"]), uuid.UUID(free["id"]), uuid.uuid4()]
        )
        await session.commit()
    assert attached == 1
    assert (await stored(session_factory, about_sam["id"])).about_id == sam.id
    assert (await stored(session_factory, free["id"])).about_id == tom.id


async def test_nothing_is_attached_to_a_record_merged_away_or_forgotten(session_factory):
    alex, sam = await household(session_factory)
    ctx = turn(alex, session_factory)
    free = await run("memory_keep", ctx, **free_memory())
    opened = await open_tom(ctx)
    async with session_factory() as session:
        tom = await session.get(Person, uuid.UUID(opened["id"]))
        assert tom is not None
        tom.merged_into_id = sam.id
        assert await records.attach(session, tom, [uuid.UUID(free["id"])]) == 0
        tom.merged_into_id = None
        tom.forgotten_at = NOW
        assert await records.attach(session, tom, [uuid.UUID(free["id"])]) == 0
        await session.commit()
    assert (await stored(session_factory, free["id"])).about_id is None


async def test_a_memory_about_someone_without_an_account_is_the_sayers_word(session_factory):
    alex, sam = await household(session_factory)
    opened = await open_tom(turn(alex, session_factory))
    said = await run("memory_keep", turn(sam, session_factory), **free_memory(about="Tom"))
    by_sam = await stored(session_factory, said["id"])
    assert by_sam.about_id == uuid.UUID(opened["id"])
    assert by_sam.said_by_id == sam.id
    # Tom has no account to confirm it: it stays Sam's word, as for anyone.
    assert unconfirmed(by_sam)


async def test_a_memory_names_one_of_two_toms_by_identifier(session_factory):
    alex, _ = await household(session_factory)
    ctx = turn(alex, session_factory)
    first, second = await open_tom(ctx), await open_tom(ctx)
    with pytest.raises(InvalidArguments) as refused:
        await run("memory_keep", ctx, **free_memory(about="Tom"))
    assert refused.value.code == "ambiguous"
    kept = await run("memory_keep", ctx, **free_memory(about=f"person:{second['id']}"))
    assert (await stored(session_factory, kept["id"])).about_id == uuid.UUID(second["id"])
    assert first["id"] != second["id"]


async def test_trust_and_nicknames_work_for_someone_without_an_account(session_factory):
    alex, _ = await household(session_factory)
    ctx = turn(alex, session_factory)
    tom_id = uuid.UUID((await open_tom(ctx))["id"])

    # The same tables and the same functions as for anyone.
    await run("nickname_keep", ctx, nickname="Tommy", person="Tom")
    await run(
        "trust_note",
        ctx,
        person="Tom",
        outcome="held",
        how="reported",
        about="Tom said the shop closes at six and it did.",
    )
    async with session_factory() as reader:
        nicknamed = (
            await reader.scalars(
                select(PersonNickname.target_id).where(PersonNickname.person_id == alex.id)
            )
        ).all()
        facts = (await reader.scalars(select(TrustFact.person_id))).all()
    assert list(nicknamed) == [tom_id]
    assert list(facts) == [tom_id]
    # The nickname finds him in the speaker's turns.
    kept = await run("memory_keep", ctx, **free_memory(about="Tommy"))
    assert (await stored(session_factory, kept["id"])).about_id == tom_id


async def test_no_keeping_refuses_a_memory_about_a_record_opened_since(session_factory):
    alex, sam = await household(session_factory)
    async with session_factory() as session:
        dana = Person(display_name="Dana", role="guest", state="known")
        session.add(dana)
        await session.commit()
        # Just after Dana's row, read from the database's own clock: the test does not depend on
        # the two clocks agreeing.
        ruled_at = dana.created_at + timedelta(microseconds=1)
    async with session_factory() as session:
        await directives.set_directive(
            session,
            kind="no_keeping",
            person=None,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=ruled_at,
        )
        await session.commit()
    async with session_factory() as session:
        session.add(Person(display_name="Paul", role="guest", state="known"))
        await session.commit()
    ctx = turn(sam, session_factory)
    with pytest.raises(InvalidArguments) as refused:
        await run("memory_keep", ctx, **free_memory(subject="Paul", about="Paul"))
    assert refused.value.code == "not_possible"
    # A record that was there when the owner ruled is kept as ever, and the owner's own words are
    # never refused.
    await run("memory_keep", ctx, **free_memory(subject="Dana", about="Dana"))
    await run(
        "memory_keep", turn(alex, session_factory), **free_memory(subject="Paul", about="Paul")
    )
    async with session_factory() as reader:
        kept = {m.subject for m in await reader.scalars(select(Memory))}
    assert kept == {"Dana", "Paul"}


async def _rule(session_factory: Any, alex: Person, *, except_words: str | None = None) -> Any:
    """The owner's ``no_keeping``, ruled just after the last row written (the database's clock)."""
    async with session_factory() as session:
        last = await session.scalar(select(Person.created_at).order_by(Person.created_at.desc()))
        assert last is not None
        directive = await directives.set_directive(
            session,
            kind="no_keeping",
            person=None,
            description=None,
            except_words=except_words,
            until=None,
            owner=alex,
            now=last + timedelta(microseconds=1),
        )
        await session.commit()
        return directive


async def _new_paul(session_factory: Any) -> Person:
    async with session_factory() as session:
        paul = Person(display_name="Paul", role="guest", state="known")
        session.add(paul)
        await session.commit()
        return paul


async def test_an_exception_the_assistant_names_lets_a_memory_through_and_is_traced(
    session_factory,
):
    alex, sam = await household(session_factory)
    directive = await _rule(session_factory, alex, except_words="except the family")
    paul = await _new_paul(session_factory)
    his = replace(turn(sam, session_factory), judged_as=domo_actor())
    memory = free_memory(subject="Paul", about="Paul")
    # Not named, or named wrongly: refused.
    for given in ({}, {"exception_of": str(uuid.uuid4())}):
        with pytest.raises(InvalidArguments) as refused:
            await run("memory_keep", his, **memory, **given)
        assert refused.value.code == "not_possible"
    # A person's own call naming it changes nothing: the exception is the assistant's to judge.
    with pytest.raises(InvalidArguments):
        await run(
            "memory_keep", turn(sam, session_factory), **memory, exception_of=str(directive.id)
        )
    kept = await run("memory_keep", his, **memory, exception_of=str(directive.id))
    assert (await stored(session_factory, kept["id"])).about_id == paul.id
    async with session_factory() as reader:
        [used] = (
            await reader.scalars(select(PersonAct).where(PersonAct.act == "exception_used"))
        ).all()
    assert (used.person_id, used.directive_id, used.by) == (paul.id, directive.id, "domo")


async def test_an_exception_needs_words_in_the_rule_of_the_owner(session_factory):
    alex, sam = await household(session_factory)
    directive = await _rule(session_factory, alex)
    await _new_paul(session_factory)
    his = replace(turn(sam, session_factory), judged_as=domo_actor())
    with pytest.raises(InvalidArguments) as refused:
        await run(
            "memory_keep",
            his,
            **free_memory(subject="Paul", about="Paul"),
            exception_of=str(directive.id),
        )
    assert refused.value.code == "not_possible"


async def test_the_rule_also_covers_a_new_person_on_themselves_and_his_own_idea(session_factory):
    # Deliberate: "nobody new is kept" holds for the new person's own words about themselves and
    # for what the assistant concludes alone about a new record; only the owner's stated
    # exception lets one through.
    alex, _ = await household(session_factory)
    await _rule(session_factory, alex)
    paul = await _new_paul(session_factory)
    arguments = free_memory(subject="Paul", about="Paul")
    with pytest.raises(InvalidArguments) as of_themselves:
        await run("memory_keep", turn(paul, session_factory), **arguments)
    assert of_themselves.value.code == "not_possible"
    alone = replace(turn(alex, session_factory), on_his_own=True)
    with pytest.raises(InvalidArguments) as on_his_own:
        await run("memory_keep", alone, **arguments)
    assert on_his_own.value.code == "not_possible"
    # A turn of the owner that answers no message of his is the assistant's too.
    unasked = replace(turn(alex, session_factory), reply_message_id=None)
    with pytest.raises(InvalidArguments) as unasked_refused:
        await run("memory_keep", unasked, **arguments)
    assert unasked_refused.value.code == "not_possible"


async def test_a_record_is_dated_by_the_time_of_its_opening_act(session_factory):
    alex, sam = await household(session_factory)
    async with session_factory() as session:
        await directives.set_directive(
            session,
            kind="no_keeping",
            person=None,
            description=None,
            except_words=None,
            until=None,
            owner=alex,
            now=NOW,
        )
        await session.commit()
    before = replace(turn(alex, session_factory), clock=lambda: NOW - timedelta(days=1))
    after = replace(turn(alex, session_factory), clock=lambda: NOW + timedelta(days=1))
    early = await run("record_open", before, name="Dana", standing="visiting", ground="comes")
    late = await run("record_open", after, name="Paul", standing="visiting", ground="comes")
    sams = replace(turn(sam, session_factory), clock=lambda: NOW + timedelta(days=2))
    await run("memory_keep", sams, **free_memory(subject="Dana", about=f"person:{early['id']}"))
    with pytest.raises(InvalidArguments):
        await run("memory_keep", sams, **free_memory(subject="Paul", about=f"person:{late['id']}"))


async def test_a_private_memory_of_someone_else_is_neither_listed_nor_attached(session_factory):
    alex, sam = await household(session_factory)
    private = await run("memory_keep", turn(sam, session_factory), **free_memory(private=True))
    shared = await run("memory_keep", turn(sam, session_factory), **free_memory())
    async with session_factory() as reader:
        seen_by_alex = {m.id for m in await free_text_about(reader, "Tom", seen_by=alex.id)}
        seen_by_sam = {m.id for m in await free_text_about(reader, "Tom", seen_by=sam.id)}
        seen_alone = {m.id for m in await free_text_about(reader, "Tom")}
    assert seen_by_alex == {uuid.UUID(shared["id"])}
    assert seen_by_sam == seen_alone == {uuid.UUID(private["id"]), uuid.UUID(shared["id"])}

    opened = await open_tom(turn(alex, session_factory))
    tom = f"person:{opened['id']}"
    ids = [private["id"], shared["id"]]
    # In Alex's turn the private one is skipped, not refused; the record is completed.
    await run("record_complete", turn(alex, session_factory), person=tom, memories=ids)
    assert (await stored(session_factory, private["id"])).about_id is None
    assert (await stored(session_factory, shared["id"])).about_id == uuid.UUID(opened["id"])
    async with session_factory() as session:
        row = await session.get(Person, uuid.UUID(opened["id"]))
        assert row is not None
        assert await records.attach(session, row, [uuid.UUID(private["id"])], seen_by=alex.id) == 0
        # A turn the assistant acts in alone sees all of them.
        assert await records.attach(session, row, [uuid.UUID(private["id"])]) == 1
        await session.commit()
