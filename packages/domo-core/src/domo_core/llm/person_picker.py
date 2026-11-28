"""The models a person's calls may use outside a turn: one ``ModelPicker`` on the key
``choose_key`` takes among the keys they may use, a key past one of its limits passed over."""

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.db import SessionFactory
from domo_core.llm.catalog_store import current_catalog
from domo_core.llm.factory import ModelFactory
from domo_core.llm.models import Provider
from domo_core.llm.picker import ModelPicker
from domo_core.llm.router import Route, choose_key, text_keys
from domo_core.llm.spending import at_limit
from domo_core.llm.tasks import TaskKind
from domo_core.policy.settings import load_policy_settings


async def person_picker(
    session: AsyncSession,
    person: Person,
    *,
    session_factory: SessionFactory,
    model_factory: ModelFactory,
    clock: Callable[[], datetime],
    now: datetime,
    main_task: TaskKind = "conversation",
    tool_id: uuid.UUID | None = None,
    build_id: uuid.UUID | None = None,
) -> ModelPicker | None:
    """A ``ModelPicker`` whose calls count for ``person``, for the task ``main_task`` (naming the
    tool and the build they are made for, when given), read on ``session`` at ``now``; ``None``
    when no key they may use has room."""
    keys = await text_keys(session, person)
    if not keys:
        return None
    catalog = await current_catalog(session)
    zone = (await load_policy_settings(session)).time_zone

    async def at_its_limit(key: Provider, route: Route) -> bool:
        return await at_limit(
            session,
            provider=key,
            person_id=person.id,
            need=Decimal(0),
            now=now,
            catalog=catalog,
            zone=zone,
        )

    choice = await choose_key(session, person, keys, catalog=catalog, at_limit=at_its_limit)
    if choice.key is None:
        return None
    return ModelPicker(
        session_factory=session_factory,
        model_factory=model_factory,
        provider=choice.key,
        person_id=person.id,
        clock=clock,
        main_task=main_task,
        catalog=catalog,
        tool_id=tool_id,
        build_id=build_id,
    )
