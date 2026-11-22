"""``delivery_look``: Domo looks at what reached someone of his replies (spec 6.10, task 11).

For the person who speaks: his last replies, an extract of his own words, how each ended and why,
and where it reached them. For someone else: only the fact, never a word.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.chat.store import ConversationStore
from domo_core.db import SessionFactory
from domo_core.deliveries.service import delivered
from domo_core.devices.models import Device
from domo_core.policy.catalog import DEFAULT_POLICY, is_silent, needs_the_judge
from domo_core.policy.roles import Actor
from domo_core.tools.context import NamesPeople, ToolContext
from domo_core.tools.registry import TOOLS
from sqlalchemy import select

NOW = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)


def context(person: Person, session_factory: SessionFactory) -> ToolContext:
    return ToolContext(
        session_factory=session_factory,
        actor=Actor(person.id, person.role),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=lambda: NOW,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
    )


async def look(who: Person, session_factory: SessionFactory, **arguments: Any) -> dict[str, Any]:
    """Describe then execute ``delivery_look`` in ``who``'s turn; what the model reads."""
    ctx = context(who, session_factory)
    tool = TOOLS["delivery_look"]
    await tool.describe(arguments, ctx.actor, ctx)
    return dict((await tool.execute(arguments, ctx)).model_result)


async def household(session_factory: SessionFactory) -> tuple[Person, Person]:
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner")
        samuel = Person(display_name="Samuel Martin", role="adult")
        writer.add_all([alex, samuel])
        await writer.commit()
    return alex, samuel


async def reply_to(
    session_factory: SessionFactory,
    person: Person,
    words: str,
    content: str,
    *,
    status: str = "complete",
    failure: str | None = None,
    at: datetime = NOW - timedelta(hours=1),
) -> uuid.UUID:
    """``person`` said ``words`` and Domo answered ``content`` (committed): the reply's id."""
    async with session_factory() as session:
        store = ConversationStore(session)
        conversation = await store.active_conversation(person.id)
        await store.add_message(
            conversation.id, role="user", content=words, status="complete", provenance="person"
        )
        reply = await store.add_message(
            conversation.id, role="assistant", content=content, status=status, provenance="model"
        )
        reply.failure = failure
        reply.created_at = at
        await session.commit()
        return reply.id


def test_the_look_is_everyones_never_judged_nor_shown():
    tool = TOOLS["delivery_look"]
    assert tool.action_type == "delivery.look"
    assert is_silent("delivery.look") and not needs_the_judge("delivery.look")
    assert isinstance(tool, NamesPeople) and tool.person_arguments == ("person",)
    for role in ("owner", "adult", "child", "guest"):
        assert "delivery.look" in DEFAULT_POLICY.allowed[role]  # type: ignore[index]


async def test_domo_sees_why_a_reply_was_empty(session_factory: SessionFactory):
    alex, _ = await household(session_factory)
    said = await reply_to(
        session_factory, alex, "Hi", "Hello Alex, " + "x" * 200, at=NOW - timedelta(hours=2)
    )
    failed = await reply_to(session_factory, alex, "And?", "", status="error", failure="upstream")
    async with session_factory() as session:
        device = Device(person_id=alex.id, name="Laptop", secret_hash="h")
        session.add(device)
        await session.flush()
        await delivered(
            session,
            message_id=said,
            person_id=alex.id,
            channel="api",
            device_id=device.id,
            how="live",
            extent="cut",
            now=NOW - timedelta(hours=2),
        )
        await session.commit()
    result = await look(alex, session_factory)
    latest, earlier = result["replies"]
    assert latest == {
        "said_at": (NOW - timedelta(hours=1)).isoformat(),
        "extract": "",
        "status": "error",
        "empty": True,
        "failure": "upstream",
        "reached": [],
    }
    assert earlier["extract"] == ("Hello Alex, " + "x" * 200)[:80]
    assert (earlier["status"], earlier["empty"], earlier["failure"]) == ("complete", False, None)
    assert earlier["reached"] == [
        {
            "channel": "api",
            "device": "Laptop",
            "how": "live",
            "extent": "cut",
            "at": (NOW - timedelta(hours=2)).isoformat(),
        }
    ]
    assert str(failed) not in json.dumps(result)  # no identifier, only what happened


async def test_the_reply_being_written_is_not_among_them(session_factory: SessionFactory):
    alex, _ = await household(session_factory)
    await reply_to(session_factory, alex, "Hi", "", status="streaming")
    assert (await look(alex, session_factory))["replies"] == []


async def test_about_someone_else_only_the_fact_never_the_words(session_factory: SessionFactory):
    alex, samuel = await household(session_factory)
    seen = await reply_to(session_factory, samuel, "my secret recipe?", "Flour, then saffron.")
    await reply_to(session_factory, samuel, "and the cake?", "Butter, and patience.")
    await reply_to(session_factory, samuel, "long ago", "Old words.", at=NOW - timedelta(days=8))
    async with session_factory() as session:
        for channel, extent in (("page", "whole"), ("api", "cut")):
            await delivered(
                session,
                message_id=seen,
                person_id=samuel.id,
                channel=channel,
                device_id=None,
                how="live",
                extent=extent,
                now=NOW,
            )
        await session.commit()
    # A first name alone finds them (``household.service.resolve_person``).
    result = await look(alex, session_factory, person="Samuel")
    assert result["person"] == "Samuel Martin" and result["days"] == 7
    lines = result["replies"]
    assert len(lines) == 2  # the one of eight days ago is not told
    for line in lines:
        assert set(line) == {"said_at", "reached", "channels", "whole"}
    assert {(line["reached"], tuple(line["channels"]), line["whole"]) for line in lines} == {
        (True, ("api", "page"), True),
        (False, (), False),
    }
    text = json.dumps(result)
    for words in ("recipe", "Flour", "saffron", "cake", "Butter", "patience", str(seen)):
        assert words not in text


async def test_naming_oneself_is_naming_nobody(session_factory: SessionFactory):
    alex, _ = await household(session_factory)
    await reply_to(session_factory, alex, "Hi", "Hello.")
    mine = await look(alex, session_factory)
    assert await look(alex, session_factory, person="Alex") == mine
    assert mine["replies"][0]["extract"] == "Hello."
    # Looking at one's own replies leaves nothing in the audit log.
    assert await audit_entries(session_factory) == []


async def audit_entries(session_factory: SessionFactory) -> list[AuditEntry]:
    async with session_factory() as session:
        found = await session.scalars(
            select(AuditEntry).where(AuditEntry.action == "delivery.looked")
        )
        return list(found.all())


async def test_a_look_at_someone_else_is_kept_in_the_audit_log(session_factory: SessionFactory):
    # Final review I6: it tells when someone was there.
    alex, samuel = await household(session_factory)
    await reply_to(session_factory, samuel, "my secret recipe?", "Flour, then saffron.")
    await look(alex, session_factory, person="Samuel")
    (entry,) = await audit_entries(session_factory)
    assert entry.actor == f"person:{alex.id}"
    assert entry.details == {"about": str(samuel.id), "count": 1}
    # The description asks him to judge whether the one asking should know.
    description = TOOLS["delivery_look"].definition.description
    assert "judge whether the person asking should know it" in description
