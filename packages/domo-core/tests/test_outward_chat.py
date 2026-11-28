"""Opening an address through a conversation (step 2, spec 13.10; rulings R17 to R19, R23, R24):
in the owner's own turn an opening that takes his yes is judged on his words ("do" opens it, "ask"
shows him a request his button or his words answer); out of it, reopening what he closed is
Domo's judgement too, and a yes asked there is the owner's, as Domo's request; a port of the
machine that hosts Domo without that right always waits for the owner's yes there. The yes-judge
of an opening in a person's turn reads what the person said, while the right stays Domo's own.

The turns run on the chat pilots' live server (``chat_support``); the yes-judge is the fake's."""

import json
import uuid
from datetime import UTC
from typing import Any

from chat_support import ChatPilot, ChatServer
from domo_core.accounts.actors import actor_of
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import Message
from domo_core.confirmations.models import Confirmation
from domo_core.llm.types import ToolCall
from domo_core.outward.models import OpenAddress
from domo_core.outward.service import open_for
from domo_core.tools.context import ToolContext
from fakes import FakeChatModel, is_judge_call
from sqlalchemy import func, select
from test_his_own_requests import his_turn
from test_madetools_in_turn import decisions_on

ON_HOST = {"host": "host.docker.internal", "port": 8123, "tool": "lights", "on_host": True}
ADDRESS = {"host": "192.168.1.20", "port": 8123, "tool": "lights"}
ASKED = "Ouvre l'adresse 192.168.1.20 sur le port 8123 pour l'outil des lumières, s'il te plaît."


async def live(pilot: ChatPilot) -> list[str]:
    async with pilot.server.session_factory() as session:
        return [f"{a.host}:{a.port}" for a in await open_for(session, "lights")]


async def confirmation(pilot: ChatPilot, confirmation_id: uuid.UUID) -> Confirmation:
    async with pilot.server.session_factory() as session:
        found = await session.get(Confirmation, confirmation_id)
        assert found is not None
        return found


def shows_confirm(html: str, confirmation_id: uuid.UUID) -> bool:
    return f"/confirmations/{confirmation_id}/approve" in html


async def waits_for_the_owner(owner_chat: ChatPilot, arguments: dict[str, Any]) -> uuid.UUID:
    """The owner asks for ``arguments`` and the judge asks him first (R23): his own request waits
    for his yes, and its card has the button that confirms it."""
    reply = await owner_chat.say_with_tool("address_open", arguments, judge="yes")
    assert [a["status"] for a in reply.actions] == ["waiting"], reply.actions
    waiting = reply.pending_confirmation_id
    assert waiting is not None and shows_confirm(reply.cards_html, waiting)
    stored = await confirmation(owner_chat, waiting)
    assert (stored.by_domo, stored.approver_kind) == (False, "self")
    return waiting


async def closes_as_the_owner(owner_chat: ChatPilot) -> None:
    """``ADDRESS`` opened, then closed by the owner."""
    opened = await owner_chat.say_with_tool("address_open", ADDRESS, judge="do")
    assert [a["status"] for a in opened.actions] == ["done"]
    await owner_chat.say_with_tool("address_close", ADDRESS, judge="do")
    assert await live(owner_chat) == []


async def opened_lines(pilot: ChatPilot) -> list[dict[str, Any]]:
    async with pilot.server.session_factory() as session:
        found = await session.scalars(
            select(AuditEntry)
            .where(AuditEntry.action == "outward.address_opened")
            .order_by(AuditEntry.id)
        )
        return [dict(entry.details) for entry in found.all()]


async def test_the_owner_confirms_a_port_of_the_host_by_the_button(owner_chat: ChatPilot):
    waiting = await waits_for_the_owner(owner_chat, ON_HOST)
    card = await owner_chat.post(f"/confirmations/{waiting}/approve")
    assert card.status_code == 200 and 'data-outcome="executed"' in card.text, card.text
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    async with owner_chat.server.session_factory() as session:
        (row,) = (await session.scalars(select(OpenAddress))).all()
    assert (row.on_host, row.opened_by) == (True, str(owner_chat.person.id))
    async with owner_chat.server.session_factory() as session:
        (opened,) = (
            await session.scalars(
                select(AuditEntry).where(AuditEntry.action == "outward.address_opened")
            )
        ).all()
    assert opened.details["under"] == "owner_yes" and opened.details["on_host"] is True


async def replies(pilot: ChatPilot) -> int:
    """How many replies of Domo there are, once no wake of his runs any more."""
    await pilot.server.wait_for_wakes()
    async with pilot.server.session_factory() as session:
        found = await session.scalar(
            select(func.count()).select_from(Message).where(Message.role == "assistant")
        )
    return int(found or 0)


async def test_the_owner_says_yes_in_words_to_a_port_of_the_host(owner_chat: ChatPilot):
    # Fix round 4: answered in words in the conversation that showed it, Domo's request is told
    # by the reply of that turn alone; nobody is woken for it.
    await waits_for_the_owner(owner_chat, ON_HOST)
    before = await replies(owner_chat)
    owner_chat.use_model(FakeChatModel(["?"]))
    await owner_chat.say("oui")
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    assert await replies(owner_chat) == before + 1


async def test_the_owner_says_no_in_words_and_nothing_opens(owner_chat: ChatPilot):
    await waits_for_the_owner(owner_chat, ON_HOST)
    before = await replies(owner_chat)
    owner_chat.use_model(FakeChatModel(["?"]))
    await owner_chat.say("non")
    assert await live(owner_chat) == []
    assert await replies(owner_chat) == before + 1


async def test_the_button_gives_one_reply_too(owner_chat: ChatPilot):
    waiting = await waits_for_the_owner(owner_chat, ON_HOST)
    before = await replies(owner_chat)
    card = await owner_chat.post(f"/confirmations/{waiting}/approve")
    assert card.status_code == 200
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    assert await replies(owner_chat) == before + 1


async def test_the_owner_s_words_open_again_what_he_closed_when_the_judge_lets_it_through(
    owner_chat: ChatPilot,
):
    # R23: in his own turn, the judge weighs his words; "do" opens it at once, no card, one reply.
    await closes_as_the_owner(owner_chat)
    before = await replies(owner_chat)
    reply = await owner_chat.say_with_tool("address_open", ADDRESS, text=ASKED, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"], reply.actions
    assert reply.pending_confirmation_id is None
    assert await live(owner_chat) == ["192.168.1.20:8123"]
    assert await replies(owner_chat) == before + 1
    last = (await opened_lines(owner_chat))[-1]
    assert (last["under"], last["reopened"]) == ("owner_asked", True)
    assert last["turn_of"] == f"person:{owner_chat.person.id}"


async def test_the_owner_s_words_open_a_port_of_the_host_when_the_judge_lets_it_through(
    owner_chat: ChatPilot,
):
    reply = await owner_chat.say_with_tool("address_open", ON_HOST, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"], reply.actions
    assert reply.pending_confirmation_id is None
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    (line,) = await opened_lines(owner_chat)
    assert (line["under"], line["reopened"], line["zone"]) == ("owner_asked", False, "house")


async def test_opening_again_what_the_owner_closed_waits_for_his_yes_when_the_judge_asks(
    owner_chat: ChatPilot,
):
    await closes_as_the_owner(owner_chat)
    waiting = await waits_for_the_owner(owner_chat, ADDRESS)
    assert await live(owner_chat) == []
    card = await owner_chat.post(f"/confirmations/{waiting}/approve")
    assert card.status_code == 200 and 'data-outcome="executed"' in card.text, card.text
    assert await live(owner_chat) == ["192.168.1.20:8123"]


async def test_in_an_adult_s_turn_a_port_of_the_host_waits_for_the_owner_s_yes(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    # R24 (B): a right, not a closing: never opened without the owner's yes, which Domo asks.
    reply = await adult_chat.say_with_tool("address_open", ON_HOST, judge="do")
    assert [a["status"] for a in reply.actions] == ["waiting"], reply.actions
    waiting = reply.pending_confirmation_id
    assert waiting is not None and not shows_confirm(reply.cards_html, waiting)
    stored = await confirmation(adult_chat, waiting)
    assert (stored.by_domo, stored.approver_kind) == (True, "owner")
    assert await live(adult_chat) == []
    card = await owner_chat.post(f"/confirmations/{waiting}/approve")
    assert card.status_code == 200 and 'data-outcome="executed"' in card.text, card.text
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    (line,) = await opened_lines(owner_chat)
    assert line["under"] == "owner_yes"


async def test_in_an_adult_s_turn_what_the_owner_closed_is_reopened_on_domo_s_judgement(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    # R24 (A): judged like any request of the turn, the judge told of the owner's closing.
    await closes_as_the_owner(owner_chat)
    first = len(adult_chat.server.calls)
    reply = await adult_chat.say_with_tool("address_open", ADDRESS, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"], reply.actions
    assert await live(adult_chat) == ["192.168.1.20:8123"]
    (judged,) = [c for c in adult_chat.server.calls[first:] if is_judge_call(c.system)]
    case = json.loads(judged.messages[0].content)  # type: ignore[union-attr]
    facts = case["action"]["facts"]
    assert facts["closed_by_owner"] is True
    assert facts["closed_on"] == adult_chat.clock().astimezone(UTC).date().isoformat()
    last = (await opened_lines(adult_chat))[-1]
    assert (last["under"], last["reopened"]) == ("person_asked", True)
    assert last["turn_of"] == f"person:{adult_chat.person.id}"


async def test_in_an_adult_s_turn_a_reopening_the_judge_doubts_waits_for_the_owner(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    await closes_as_the_owner(owner_chat)
    reply = await adult_chat.say_with_tool("address_open", ADDRESS, judge="yes")
    assert [a["status"] for a in reply.actions] == ["waiting"], reply.actions
    waiting = reply.pending_confirmation_id
    assert waiting is not None
    stored = await confirmation(adult_chat, waiting)
    assert (stored.by_domo, stored.approver_kind) == (True, "owner")
    assert await live(adult_chat) == []
    card = await owner_chat.post(f"/confirmations/{waiting}/approve")
    assert card.status_code == 200 and 'data-outcome="executed"' in card.text, card.text
    assert await live(owner_chat) == ["192.168.1.20:8123"]
    last = (await opened_lines(owner_chat))[-1]
    assert (last["under"], last["reopened"]) == ("owner_yes", True)


async def test_a_rule_of_the_owner_makes_his_closing_absolute(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    # R24: a deny of the owner's on Domo's opening holds before any judgement, in every turn.
    from domo_core.policy.models import PolicyRight

    await closes_as_the_owner(owner_chat)
    async with owner_chat.server.session_factory() as writer:
        writer.add(
            PolicyRight(
                for_domo=True,
                action_type="net.open",
                access="deny",
                summary="The assistant opens no address.",
                owner_words="n'ouvre plus aucune adresse",
                created_by=owner_chat.person.id,
            )
        )
        await writer.commit()
    for pilot in (adult_chat, owner_chat):
        reply = await pilot.say_with_tool("address_open", ADDRESS, judge="do")
        assert [a["status"] for a in reply.actions] == ["refused"], reply.actions
    assert await live(owner_chat) == []


# --- a port of the host the owner closed: the right comes first ----------------------------------


async def closes_the_host_port_as_the_owner(owner_chat: ChatPilot) -> None:
    """``ON_HOST`` opened on the owner's words, then closed by him."""
    opened = await owner_chat.say_with_tool("address_open", ON_HOST, judge="do")
    assert [a["status"] for a in opened.actions] == ["done"], opened.actions
    closing = {key: ON_HOST[key] for key in ("host", "port", "tool")}
    await owner_chat.say_with_tool("address_close", closing, judge="do")
    assert await live(owner_chat) == []


def address_call(arguments: dict[str, Any]) -> ToolCall:
    return ToolCall("o1", "address_open", json.dumps(arguments))


async def test_in_an_adult_s_turn_a_host_port_the_owner_closed_still_waits_for_his_yes(
    owner_chat: ChatPilot, adult_chat: ChatPilot
):
    # Without the right, a port of the host is the right's case (B) first, closed or not: the
    # judge's "do" never opens it; Domo's request waits for the owner's yes.
    await closes_the_host_port_as_the_owner(owner_chat)
    reply = await adult_chat.say_with_tool("address_open", ON_HOST, judge="do")
    assert [a["status"] for a in reply.actions] == ["waiting"], reply.actions
    waiting = reply.pending_confirmation_id
    assert waiting is not None
    stored = await confirmation(adult_chat, waiting)
    assert (stored.by_domo, stored.approver_kind) == (True, "owner")
    assert await live(adult_chat) == []
    card = await owner_chat.post(f"/confirmations/{waiting}/approve")
    assert card.status_code == 200 and 'data-outcome="executed"' in card.text, card.text
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    last = (await opened_lines(owner_chat))[-1]
    assert (last["under"], last["on_host"], last["reopened"]) == ("owner_yes", True, True)
    # Run on the owner's yes, it is still the adult's turn it came from.
    assert last["turn_of"] == f"person:{adult_chat.person.id}"


async def test_on_his_own_a_host_port_the_owner_closed_waits_for_his_yes(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory: Any
):
    await closes_the_host_port_as_the_owner(owner_chat)
    await his_turn(chat_server, owner_chat, session_factory, address_call(ON_HOST))
    assert await live(owner_chat) == []
    async with session_factory() as session:
        (asked,) = (
            await session.scalars(
                select(Confirmation).where(Confirmation.tool_name == "address_open")
            )
        ).all()
    assert (asked.status, asked.by_domo, asked.approver_kind) == ("pending", True, "owner")
    card = await owner_chat.post(f"/confirmations/{asked.id}/approve")
    assert card.status_code == 200 and 'data-outcome="executed"' in card.text, card.text
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    last = (await opened_lines(owner_chat))[-1]
    assert (last["under"], last["reopened"], last["turn_of"]) == ("owner_yes", True, "domo")


async def test_the_owner_s_words_open_a_host_port_he_closed(owner_chat: ChatPilot):
    await closes_the_host_port_as_the_owner(owner_chat)
    reply = await owner_chat.say_with_tool("address_open", ON_HOST, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"], reply.actions
    assert reply.pending_confirmation_id is None
    assert await live(owner_chat) == ["host.docker.internal:8123"]
    last = (await opened_lines(owner_chat))[-1]
    assert (last["under"], last["on_host"], last["zone"], last["reopened"]) == (
        "owner_asked",
        True,
        "house",
        True,
    )


async def test_with_the_right_a_host_port_the_owner_closed_is_domo_s_judgement(
    owner_chat: ChatPilot, adult_chat: ChatPilot, grant: Any
):
    await grant("net.open_host", for_domo=True, access="allow")
    await closes_the_host_port_as_the_owner(owner_chat)
    first = len(adult_chat.server.calls)
    reply = await adult_chat.say_with_tool("address_open", ON_HOST, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"], reply.actions
    assert await live(adult_chat) == ["host.docker.internal:8123"]
    (judged,) = [c for c in adult_chat.server.calls[first:] if is_judge_call(c.system)]
    case = json.loads(judged.messages[0].content)  # type: ignore[union-attr]
    assert case["action"]["facts"]["closed_by_owner"] is True
    last = (await opened_lines(adult_chat))[-1]
    assert (last["under"], last["on_host"], last["reopened"]) == ("person_asked", True, True)


async def test_a_rule_of_the_owner_to_ask_him_first_holds_against_the_judge_s_do(
    owner_chat: ChatPilot, adult_chat: ChatPilot, grant: Any
):
    await grant("net.open", for_domo=True, ask="yes")
    for pilot in (owner_chat, adult_chat):
        reply = await pilot.say_with_tool("address_open", ADDRESS, judge="do")
        assert [a["status"] for a in reply.actions] == ["waiting"], reply.actions
    assert await live(owner_chat) == []


async def test_a_deny_of_the_owner_refuses_an_opening_on_his_own(
    chat_server: ChatServer, owner_chat: ChatPilot, session_factory: Any, grant: Any
):
    await grant("net.open", for_domo=True, access="deny")
    await his_turn(chat_server, owner_chat, session_factory, address_call(ADDRESS))
    assert await live(owner_chat) == []
    [decided] = await decisions_on(session_factory, "net.open")
    assert (decided.actor, decided.details["decision"]) == ("domo", "refuse")


async def test_the_judge_of_an_opening_reads_what_the_owner_said(owner_chat: ChatPilot):
    # Ruling R18: a "do" of the judge, who read the owner asking for it, lets it through, no card.
    first = len(owner_chat.server.calls)
    reply = await owner_chat.say_with_tool("address_open", ADDRESS, text=ASKED, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"]
    assert reply.pending_confirmation_id is None
    assert await live(owner_chat) == ["192.168.1.20:8123"]
    (judged,) = [c for c in owner_chat.server.calls[first:] if is_judge_call(c.system)]
    case = json.dumps([getattr(m, "content", "") for m in judged.messages], ensure_ascii=False)
    assert "192.168.1.20 sur le port 8123" in case


# The judge reads the words of the turn for every act of his in a person's turn, and his own case
# on his own: ``test_judge.test_an_act_of_his_in_a_person_s_turn_reads_their_words`` and
# ``test_judge.test_his_own_act_reads_what_he_knows_and_why``.


# --- ruling R19: never on the judge's "do", never inside a chain --------------------------------


async def test_a_chain_never_holds_an_opening_that_takes_the_owner_s_yes(owner_chat: ChatPilot):
    chain = {"steps": [{"tool": "address_open", "arguments": ON_HOST}]}
    reply = await owner_chat.say_with_tool("do_in_order", chain, judge="do")
    (action,) = reply.actions
    assert action["status"] == "invalid", reply.actions
    assert "on its own" in reply.model_result_text
    assert reply.pending_confirmation_id is None and await live(owner_chat) == []
    # What the owner closed, the same.
    await owner_chat.say_with_tool("address_open", ADDRESS, judge="do")
    await owner_chat.say_with_tool("address_close", ADDRESS, judge="do")
    closed = {"steps": [{"tool": "address_open", "arguments": ADDRESS}]}
    again = await owner_chat.say_with_tool("do_in_order", closed, judge="do")
    assert [a["status"] for a in again.actions] == ["invalid"]
    assert await live(owner_chat) == []


async def test_an_ordinary_opening_stays_a_step_of_a_chain(owner_chat: ChatPilot):
    chain = {"steps": [{"tool": "address_open", "arguments": ADDRESS}]}
    reply = await owner_chat.say_with_tool("do_in_order", chain, judge="do")
    assert [a["status"] for a in reply.actions] == ["done"], reply.actions
    assert await live(owner_chat) == ["192.168.1.20:8123"]


async def test_an_opening_that_takes_the_owner_s_yes_runs_only_on_his_real_yes(
    owner_chat: ChatPilot,
):
    # The reviewer's scenario, at the tool: run as after a chain the judge let through (the
    # requester recorded as its decider, ``decided_via`` judge), resumed in the owner's turn or on
    # his own, or as a step of a chain, nothing opens; his yes on that very request does.
    from dataclasses import replace

    import pytest
    from domo_core.policy.catalog import DEFAULT_POLICY
    from domo_core.tools.context import InvalidArguments
    from domo_core.tools.registry import TOOLS
    from sqlalchemy import update

    waiting = await waits_for_the_owner(owner_chat, ON_HOST)
    owner = owner_chat.person
    tool = TOOLS["address_open"]
    base = ToolContext(
        session_factory=owner_chat.server.session_factory,
        actor=actor_of(owner),  # type: ignore[arg-type]
        person=owner,
        conversation_id=uuid.uuid4(),
        origin="",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=owner_chat.clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=uuid.uuid4(),
        approved_by=owner.id,
    )

    async def refused(ctx: ToolContext) -> None:
        with pytest.raises(InvalidArguments) as error:
            await tool.execute(ON_HOST, ctx)
        assert error.value.code == "not_possible"
        assert await live(owner_chat) == []

    # No request at all, a request nobody decided, and one the judge let through.
    await refused(base)
    await refused(replace(base, request_id=waiting))
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(
            update(Confirmation)
            .where(Confirmation.id == waiting)
            .values(
                status="executing",
                decided_by=owner.id,
                decided_at=owner_chat.clock(),
                decided_via="judge",
            )
        )
        await writer.commit()
    for ctx in (
        replace(base, request_id=waiting),
        replace(base, request_id=waiting, on_his_own=True, reply_message_id=None),
    ):
        await refused(ctx)
    # Given by his button, it opens; as a step of a chain, never.
    async with owner_chat.server.session_factory() as writer:
        await writer.execute(
            update(Confirmation).where(Confirmation.id == waiting).values(decided_via="button")
        )
        await writer.commit()
    await refused(replace(base, request_id=waiting, in_a_chain=True))
    await tool.execute(ON_HOST, replace(base, request_id=waiting))
    assert await live(owner_chat) == ["host.docker.internal:8123"]
