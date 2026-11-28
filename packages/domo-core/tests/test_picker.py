"""The models of a turn (``ModelPicker``): each call takes the route of its kind of task on the key
of the request, the protected calls never take a model under medium, the key is read again before
each call, and a model too small for the history is left out."""

import uuid
from collections.abc import Sequence
from dataclasses import replace

import pytest
from domo_core.accounts.actors import actor_of
from domo_core.chat.agent import TurnFailed, run_turn
from domo_core.confirmations.judge import RightsSay
from domo_core.conversation.execution import consult_the_judge
from domo_core.llm.models import Provider, ProviderModel, new_provider_id
from domo_core.llm.picker import ModelPicker
from domo_core.llm.spending_models import CatalogSnapshot
from domo_core.llm.types import ChatMessage, ModelCallError
from domo_core.policy.actions import DescribedAction
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from fakes import FakeChatModel, fake_factory
from sqlalchemy import delete

PILOT_MODELS = ("example-haiku-1", "example-sonnet-1", "example-opus-1")


async def add_key(
    session_factory,
    giver: uuid.UUID,
    models: Sequence[str] = PILOT_MODELS,
    *,
    kind: str = "openai_compatible",
    base_url: str = "https://llm.example.com/v1",
) -> Provider:
    """A key of ``giver`` with a key in the vault, listing ``models`` (committed)."""
    provider_id = new_provider_id()
    row = Provider(
        id=provider_id,
        kind=kind,
        base_url=base_url,
        secret_ref=f"provider:{provider_id}:api_key",
        given_by=giver,
        is_general=True,
    )
    async with session_factory() as writer:
        writer.add(row)
        await writer.flush()
        writer.add_all(ProviderModel(provider_id=provider_id, model_id=m) for m in models)
        await writer.commit()
    return row


def picker_on(key: Provider, session_factory, clock, factory, **options) -> ModelPicker:
    return ModelPicker(
        session_factory=session_factory,
        model_factory=factory,
        provider=key,
        person_id=key.given_by,
        clock=clock,
        **options,
    )


@pytest.fixture
async def owner(add_member):
    return await add_member("Owner", "owner")


async def test_the_judge_never_takes_a_model_below_medium(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, ["x-haiku"])
    factory = fake_factory(FakeChatModel(["ok"], judge="do"))
    picker = picker_on(key, session_factory, clock, factory)

    assert await picker.model_for("judge") is None
    assert await picker.model_for("intent") is None
    assert await picker.model_for("rights_writer") is None
    assert factory.requested == []

    ctx = ToolContext(
        session_factory=session_factory,
        actor=actor_of(owner),
        person=owner,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        # The turn answers a message of theirs: what is judged is a request they made.
        reply_message_id=uuid.uuid4(),
        models=picker,
    )
    rename = DescribedAction("core", "profile.rename", None, "self_profile", "household")
    allowed = RightsSay(allowed=True, needs=None, question=None)
    judgement = await consult_the_judge(
        ctx, actor=ctx.actor, action=rename, says="rename themselves", rights=allowed
    )
    # As when the judge does not answer: nothing is asked (2026-11-15). The model was never called.
    assert (judgement.verdict, judgement.source) == ("do", "fallback")
    assert factory.requested == []
    # In a turn that answers no message of theirs, nobody is known to have asked: a yes.
    on_his_own = await consult_the_judge(
        replace(ctx, reply_message_id=None),
        actor=ctx.actor,
        action=rename,
        says="rename themselves",
        rights=allowed,
    )
    assert (on_his_own.verdict, on_his_own.source) == ("yes", "fallback")
    assert factory.requested == []
    # The conversation is no protected task: it takes the small model.
    await picker.conversation()
    assert factory.requested == [(key.id, "x-haiku")]


async def test_each_task_gets_its_route(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id)
    factory = fake_factory(FakeChatModel(["ok"]))
    picker = picker_on(key, session_factory, clock, factory)

    await picker.conversation()
    for task in ("judge", "intent", "rights_writer"):
        assert await picker.model_for(task) is not None
    assert factory.requested == [(key.id, "example-sonnet-1")] * 4
    route = picker.conversation_route
    assert route is not None
    assert (route.provider.id, route.model_id, route.level, route.task) == (
        key.id,
        "example-sonnet-1",
        "medium",
        "conversation",
    )
    assert picker.provider.id == key.id


async def test_a_conversation_without_a_route_fails_with_no_model(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id, [])
    picker = picker_on(key, session_factory, clock, fake_factory(FakeChatModel(["ok"])))

    assert await picker.model_for("conversation") is None
    with pytest.raises(ModelCallError) as raised:
        await picker.conversation()
    assert raised.value.kind == "no_model"
    assert picker.conversation_route is None
    # A turn asks the picker before its first round: it fails at once, with nothing said.
    events = [
        event
        async for event in run_turn(
            models=picker,
            system_prompt="S",
            history=[ChatMessage("user", "hi")],
            max_output_tokens=32,
        )
    ]
    assert events == [TurnFailed("no_model", "")]


async def test_the_picker_reads_the_key_again_at_each_call(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id)
    factory = fake_factory(FakeChatModel(["ok"]))
    picker = picker_on(key, session_factory, clock, factory)
    await picker.conversation()

    # The key is removed while the turn goes on.
    async with session_factory() as writer:
        await writer.execute(delete(Provider).where(Provider.id == key.id))
        await writer.commit()

    assert await picker.model_for("judge") is None
    with pytest.raises(ModelCallError) as raised:
        await picker.conversation()
    assert raised.value.kind == "no_model"
    assert factory.requested == [(key.id, "example-sonnet-1")]


def entry(context: int, price: int) -> dict[str, object]:
    return {
        "input": str(price),
        "output": str(price),
        "cache_read": str(price),
        "cache_write": str(price),
        "context": context,
        "output_tokens": 1000,
        "tools": True,
        "vision": False,
    }


async def test_a_model_too_small_for_the_history_is_dropped(owner, session_factory, clock):
    # The catalog of now is the latest snapshot: two models of the same level, the cheaper one
    # with a small context.
    async with session_factory() as writer:
        writer.add(
            CatalogSnapshot(
                data={
                    "source": "models.dev",
                    "fetched": "2026-11-08",
                    "usd_per_eur": "1.1",
                    "providers": {
                        "anthropic": {
                            "claude-sonnet-short": entry(context=2_000, price=1),
                            "claude-sonnet-long": entry(context=200_000, price=3),
                        },
                        "openai": {},
                        "google": {},
                    },
                }
            )
        )
        await writer.commit()
    key = await add_key(
        session_factory,
        owner.id,
        ["claude-sonnet-short", "claude-sonnet-long"],
        kind="anthropic",
        base_url="https://api.anthropic.com",
    )

    short = fake_factory(FakeChatModel(["ok"]))
    await picker_on(key, session_factory, clock, short).conversation()
    assert short.requested == [(key.id, "claude-sonnet-short")]

    long = fake_factory(FakeChatModel(["ok"]))
    picker = picker_on(key, session_factory, clock, long, min_context=5_000)
    await picker.conversation()
    # The judge is not sent the history: its call is small, it takes the cheaper one.
    await picker.model_for("judge")
    assert long.requested == [(key.id, "claude-sonnet-long"), (key.id, "claude-sonnet-short")]


async def test_a_picker_takes_its_main_task(owner, session_factory, clock):
    key = await add_key(session_factory, owner.id)
    factory = fake_factory(FakeChatModel(["ok"]))
    picker = picker_on(key, session_factory, clock, factory, main_task="upkeep")

    await picker.conversation()
    # The upkeep starts small.
    assert factory.requested == [(key.id, "example-haiku-1")]
    route = picker.conversation_route
    assert route is not None and (route.task, route.level) == ("upkeep", "small")


async def test_at_least_raises_the_floor_of_a_task_that_is_not_the_main_one(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id)
    factory = fake_factory(FakeChatModel(["ok"]))
    picker = picker_on(key, session_factory, clock, factory)

    # The writing of texts starts small; asked for at least a level, it takes that one.
    await picker.model_for("writing")
    await picker.model_for("writing", at_least="medium")
    await picker.model_for("writing", at_least="strong")
    # A level under the task's own changes nothing: the checking starts at medium.
    await picker.model_for("checking", at_least="small")
    assert factory.requested == [
        (key.id, "example-haiku-1"),
        (key.id, "example-sonnet-1"),
        (key.id, "example-opus-1"),
        (key.id, "example-sonnet-1"),
    ]
    # The main calls are not raised by it.
    await picker.conversation()
    assert factory.requested[-1] == (key.id, "example-sonnet-1")


async def test_at_least_and_a_step_up_of_the_main_task_take_the_higher(
    owner, session_factory, clock
):
    key = await add_key(session_factory, owner.id)
    factory = fake_factory(FakeChatModel(["ok"]))
    picker = picker_on(key, session_factory, clock, factory, main_task="upkeep")

    # The upkeep starts small; a step up makes its floor medium.
    assert picker.step_up() == "medium"
    await picker.model_for("upkeep", at_least="small")
    await picker.model_for("upkeep", at_least="strong")
    await picker.model_for("upkeep")
    assert factory.requested == [
        (key.id, "example-sonnet-1"),
        (key.id, "example-opus-1"),
        (key.id, "example-sonnet-1"),
    ]
