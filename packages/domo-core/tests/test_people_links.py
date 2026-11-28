"""Links between people: who is what to whom, noted by the assistant, confirmed only by who can
know (the owner, or one of the two while they have access), never by a newcomer's word about
themselves, and kept with their trace."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from domo_core.accounts.models import Person
from domo_core.confirmations.judge import Judgement
from domo_core.conversation.execution import handle_tool_call
from domo_core.llm.types import ToolCall
from domo_core.people import links, records
from domo_core.people.acts import why
from domo_core.people.models import PersonAct, PersonLink
from domo_core.policy.catalog import ACTION_FACTS
from domo_core.tools.registry import TOOLS
from sqlalchemy import func, select
from test_concerned_person import _judge, _people, _turn
from test_own_idea import mark

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
WHY = "Dana says Sam is Alex's brother"


async def _known(session, name: str, *, is_child: bool = False) -> Person:
    """A record with no access: someone the assistant only knows."""
    return await records.open_record(
        session,
        name=name,
        standing="visiting",
        is_child=is_child,
        by="domo",
        reason=WHY,
        turn_of=None,
        now=NOW,
    )


async def _note(session, start, relation, end, *, said_by, by=None, reason=None):
    """Noted as the assistant's own act (``by="domo"``, with its reason), or, by default, as the
    own request of the one who said it (nobody said it: the assistant's)."""
    if by is None:
        by = "domo" if said_by is None else f"person:{said_by.id}"
    if by == "domo" and reason is None:
        reason = WHY
    return await links.note_link(
        session,
        from_person=start,
        relation=relation,
        to_person=end,
        said_by=said_by,
        by=by,
        reason=reason,
        turn_of=None,
        now=NOW,
    )


async def test_what_the_owner_says_of_his_own_family_is_confirmed(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    link = await _note(session, dana, "mother of", alex, said_by=alex)
    assert (link.confirmed_at, link.said_by_id) == (NOW, alex.id)
    # One of the two, with access now, is believed on their own link too.
    sibling = await _note(session, alex, "partner of", dana, said_by=dana)
    assert sibling.confirmed_at == NOW


async def test_a_link_someone_else_says_is_their_word(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await _known(session, "Sam")
    link = await _note(session, sam, "brother of", alex, said_by=dana)
    assert link.confirmed_at is None and link.said_by_id == dana.id
    deduced = await _note(session, sam, "friend of", dana, said_by=None)
    assert deduced.confirmed_at is None and deduced.said_by_id is None


async def test_saying_one_is_the_owners_brother_confirms_nothing(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await _known(session, "Sam")
    link = await _note(session, sam, "brother of", alex, said_by=sam)
    assert sam.state == "known" and link.confirmed_at is None
    # Only the owner (or one of the two with access) makes it stand.
    assert await links.confirm(session, link, by_person=sam, now=NOW) is False
    assert link.confirmed_at is None
    assert await links.confirm(session, link, by_person=alex, now=NOW) is True
    assert link.confirmed_at == NOW


async def test_only_the_owner_or_one_of_the_two_with_access_confirms(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    paul = await add_person("Paul", "child")
    tom = await _known(session, "Tom")
    link = await _note(session, tom, "friend of", paul, said_by=None)
    assert await links.confirm(session, link, by_person=dana, now=NOW) is False
    assert await links.confirm(session, link, by_person=tom, now=NOW) is False
    assert await links.confirm(session, link, by_person=paul, now=NOW) is True
    assert link.confirmed_at == NOW
    other = await _note(session, tom, "cousin of", dana, said_by=None)
    assert await links.confirm(session, other, by_person=alex, now=NOW) is True


async def test_the_same_link_is_kept_once(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await _known(session, "Sam")
    first = await _note(session, sam, "brother of", alex, said_by=dana)
    again = await _note(session, sam, " Brother of ", alex, said_by=dana)
    assert again.id == first.id
    assert await session.scalar(select(func.count()).select_from(PersonLink)) == 1
    # The owner saying it later confirms the one that is there.
    confirmed = await _note(session, sam, "brother of", alex, said_by=alex)
    assert confirmed.id == first.id and confirmed.confirmed_at == NOW


async def test_a_link_goes_with_its_records(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await _known(session, "Sam")
    await _note(session, sam, "brother of", alex, said_by=alex)
    await session.delete(sam)
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(PersonLink)) == 0


async def test_a_relation_is_one_plain_line_and_two_different_records(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await _known(session, "Sam")
    for relation in ("", "   ", "a\nb", "x" * 61):
        with pytest.raises(records.NotPossible) as refused:
            await _note(session, sam, relation, alex, said_by=alex)
        assert refused.value.code == "bad_relation"
    with pytest.raises(records.NotPossible) as refused:
        await _note(session, sam, "twin of", sam, said_by=alex)
    assert refused.value.code == "same_record"
    with pytest.raises(ValueError):
        await _note(session, sam, "friend of", alex, said_by=None, by="domo", reason="")


async def test_a_link_leaves_its_trace_with_its_reason_and_drop_too(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await _known(session, "Sam")
    link = await _note(session, sam, "brother of", alex, said_by=alex)
    await links.drop(
        session, link, by="domo", reason="Alex says it is wrong", turn_of=None, now=NOW
    )
    assert await session.scalar(select(func.count()).select_from(PersonLink)) == 0
    acts = await session.scalars(select(PersonAct))
    assert sorted((a.act, a.person_id, a.other_id) for a in acts) == sorted(
        [
            ("opened", sam.id, None),
            ("linked", sam.id, alex.id),
            ("unlinked", sam.id, alex.id),
        ]
    )
    assert {a.reason for a in await why(session, sam.id) if a.act == "unlinked"} == {
        "Alex says it is wrong"
    }


async def test_who_is_what_to_a_person_and_their_links_both_ways(session, add_person):
    alex = await add_person("Alex", "owner")
    dana = await add_person("Dana", "adult")
    sam = await _known(session, "Sam")
    await _note(session, dana, "mother of", alex, said_by=alex)
    await _note(session, sam, "friend of", alex, said_by=dana)
    assert {
        (p.display_name, r) for p, r in await links.related(session, alex.id, confirmed_only=False)
    } == {
        ("Dana", "mother of"),
        ("Sam", "friend of"),
    }
    assert [
        (p.display_name, r) for p, r in await links.related(session, alex.id, confirmed_only=True)
    ] == [("Dana", "mother of")]
    assert len(await links.links_of(session, alex.id)) == 2
    assert len(await links.links_of(session, dana.id)) == 1


# --- the tools -----------------------------------------------------------------------------------


def call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall("c", name, json.dumps(arguments))


def test_the_link_tools_describe_exactly_the_facts_of_the_catalog():
    assert dict(ACTION_FACTS["people.link"]) == {"from": "person", "to": "person"}
    assert dict(ACTION_FACTS["people.unlink"]) == {"from": "person", "to": "person"}
    assert TOOLS["link_note"].action_type == "people.link"
    assert TOOLS["link_drop"].action_type == "people.unlink"
    for name in ("link_note", "link_drop"):
        properties = TOOLS[name].definition.parameters["properties"]
        assert "reason" in properties  # type: ignore[operator]


async def test_the_assistant_notes_a_link_on_his_own_idea_and_it_is_silent(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    judged = _judge(monkeypatch)
    async with session_factory() as writer:
        sam = await _known(writer, "Sam")
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    noted = await handle_tool_call(
        call(
            "link_note",
            {
                "person": f"person:{sam.id}",
                "relation": "brother of",
                "to": "Alex",
                "said_by": "Dana",
                "reason": WHY,
            },
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert noted.status == "done"
    assert judged.cases == []
    async with session_factory() as reader:
        [link] = list(await reader.scalars(select(PersonLink)))
        assert (link.from_id, link.to_id, link.said_by_id) == (
            sam.id,
            people["Alex"].id,
            people["Dana"].id,
        )
        assert link.confirmed_at is None
        [act] = [a for a in await why(reader, sam.id) if a.act == "linked"]
        assert (act.by, act.turn_of, act.reason) == ("domo", people["Dana"].id, WHY)


async def test_the_owner_asking_himself_notes_his_own_word_and_a_reason_is_refused(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch, Judgement("do"))
    async with session_factory() as writer:
        sam = await _known(writer, "Sam")
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    arguments = {"person": f"person:{sam.id}", "relation": "brother of", "to": "Alex"}
    refused = await handle_tool_call(
        call("link_note", {**arguments, "reason": WHY}), ctx=ctx, reply_id=reply_id
    )
    assert refused.status == "invalid"
    outcome = await handle_tool_call(call("link_note", arguments), ctx=ctx, reply_id=reply_id)
    assert outcome.status == "done"
    async with session_factory() as reader:
        link = await reader.scalar(select(PersonLink))
        assert link is not None and link.said_by_id == people["Alex"].id
        assert link.confirmed_at is not None


async def test_dropping_a_link_by_the_tool(session_factory, monkeypatch):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        sam = await _known(writer, "Sam")
        await _note(writer, sam, "brother of", people["Alex"], said_by=people["Dana"])
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark("Alex says it is not true"), ctx=ctx, reply_id=reply_id)
    dropped = await handle_tool_call(
        call(
            "link_drop",
            {
                "person": f"person:{sam.id}",
                "relation": "brother of",
                "to": "Alex",
                "reason": "Alex says it is not true",
            },
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert dropped.status == "done"
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(PersonLink)) == 0
    missing = await handle_tool_call(mark("again"), ctx=ctx, reply_id=reply_id)
    assert missing.status == "done"
    gone = await handle_tool_call(
        call(
            "link_drop",
            {"person": f"person:{sam.id}", "relation": "brother of", "to": "Alex", "reason": "x"},
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert gone.status == "invalid" and gone.model_result["reason"] == "not_found"


async def test_an_own_idea_never_confirms_even_naming_the_owner_and_his_own_word_does(
    session_factory, monkeypatch
):
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        sam = await _known(writer, "Sam")
        await writer.commit()
    arguments = {"person": f"person:{sam.id}", "relation": "brother of", "to": "Alex"}
    ctx, reply_id, _ = await _turn(session_factory, people["Dana"])
    await handle_tool_call(mark(WHY), ctx=ctx, reply_id=reply_id)
    claimed = await handle_tool_call(
        call("link_note", {**arguments, "said_by": "Alex", "reason": WHY}),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert claimed.status == "done" and claimed.model_result["stands"] is False
    async with session_factory() as reader:
        link = await reader.scalar(select(PersonLink))
        assert link is not None and link.confirmed_at is None
        assert link.said_by_id == people["Alex"].id
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    said = await handle_tool_call(call("link_note", arguments), ctx=ctx, reply_id=reply_id)
    assert said.status == "done" and said.model_result["stands"] is True
    async with session_factory() as reader:
        assert await reader.scalar(select(func.count()).select_from(PersonLink)) == 1
        link = await reader.scalar(select(PersonLink))
        assert link is not None and link.confirmed_at is not None


async def test_the_one_who_said_it_is_a_live_record(session, add_person):
    alex = await add_person("Alex", "owner")
    sam = await _known(session, "Sam")
    gone = await add_person("Tom", "guest", forgotten_at=NOW)
    with pytest.raises(records.NotPossible) as refused:
        await _note(session, sam, "friend of", alex, said_by=gone, by="domo")
    assert refused.value.code == "not_a_record"


async def test_a_word_heard_from_someone_else_is_noted_as_the_speaker_s(
    session_factory, monkeypatch
):
    """Alex: "Sam told me he is Paul's uncle". Not refused: it is Alex's word, heard from Sam,
    and the result says from whom (final review B M9)."""
    people = await _people(session_factory, ("Alex", "owner"), ("Dana", "adult"), ("Paul", "guest"))
    _judge(monkeypatch)
    async with session_factory() as writer:
        sam = await _known(writer, "Sam")
        await writer.commit()
    ctx, reply_id, _ = await _turn(session_factory, people["Alex"])
    noted = await handle_tool_call(
        call(
            "link_note",
            {"person": f"person:{sam.id}", "relation": "uncle of", "to": "Paul", "said_by": "Sam"},
        ),
        ctx=ctx,
        reply_id=reply_id,
    )
    assert noted.status == "done", noted.model_result
    assert noted.model_result["heard_from"] == "Sam"
    async with session_factory() as reader:
        [link] = list(await reader.scalars(select(PersonLink)))
    assert link.said_by_id == people["Alex"].id
