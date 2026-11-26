"""Warned facts and the verdict (final review of the judge-first branch): two verdict lines that
differ are read as the more demanding (A4); a fact a judge must weigh first (the last key) asks a
plain yes when the judge lets it go under the owner's "never ask me" with conditions (A6), and
when a change of a request is met by a silent judge (B4)."""

from dataclasses import replace

import pytest
from domo_core.confirmations.judge import read_judgement
from domo_core.confirmations.models import Confirmation
from domo_core.confirmations.service import amend_confirmation
from domo_core.llm.models import ProviderModel
from sqlalchemy import delete
from test_key_tools import all_keys, facts_judged
from test_spending import add_key


@pytest.mark.parametrize(
    ("answer", "verdict", "reason"),
    [
        ("do: they asked\nwarn: it cannot be undone", "warn", "it cannot be undone"),
        ("warn: it cannot be undone\ndo: they asked", "warn", "it cannot be undone"),
        ("yes: are you sure?\ndo_not: not this", "do_not", "not this"),
        ("do: they asked\ndo: really", "do", "they asked"),
    ],
)
def test_two_verdicts_that_differ_read_as_the_more_demanding(answer, verdict, reason):
    judged = read_judgement(answer)
    assert (judged.verdict, judged.reason) == (verdict, reason)


async def test_a_do_under_a_conditional_never_ask_on_the_last_key_still_asks_a_yes(
    owner_chat, grant, gk_mock, confirmation_ask
):
    await grant("key.remove", ask="judge", ask_when="when I am at home")
    general = owner_chat.server.provider_id
    reply = await owner_chat.say_with_tool("key_remove", {"key": general}, judge="do")
    assert facts_judged(owner_chat)["last_key"] is True
    assert reply.actions == [{"tool": "key_remove", "status": "waiting"}]
    assert await confirmation_ask(reply.pending_confirmation_id) == ("yes", "judge", None)
    assert [key.id for key in await all_keys(owner_chat)] == [general]


async def test_a_change_to_the_general_key_on_a_silent_judge_is_asked_again(
    owner_chat, gk_mock, tool_context, confirmation_ask, confirmation_status
):
    factory = owner_chat.server.session_factory
    general = owner_chat.server.provider_id
    other = await add_key(factory, owner_chat.person.id)
    reply = await owner_chat.say_with_tool("key_remove", {"key": other.id}, judge="yes: sure?")
    rid = reply.pending_confirmation_id
    assert rid is not None
    async with factory() as writer:
        # The other key lists no model any more: the general key is the last that works.
        await writer.execute(delete(ProviderModel).where(ProviderModel.provider_id == other.id))
        await writer.commit()
    async with factory() as reader:
        request = await reader.get(Confirmation, rid)
        assert request is not None
    # "Yes, but the general key instead", in his own words: the judge does not answer.
    ctx = replace(
        tool_context,
        conversation_id=request.conversation_id,
        change_grant=rid,
        # A turn that answers him: a silent judge's fallback is a do (no model to ask).
        reply_message_id=reply.message_id,
        models=None,
    )
    outcome, _, new = await amend_confirmation(
        session_factory=factory,
        confirmation_id=rid,
        new_arguments={"key": general},
        ask_again=False,
        ctx=ctx,
        now=owner_chat.server.clock(),
    )
    assert outcome == "asked_again", outcome
    assert new is not None
    assert await confirmation_status(rid) == "replaced"
    assert await confirmation_ask(new.id) == ("yes", "judge", None)
    assert general in {key.id for key in await all_keys(owner_chat)}
