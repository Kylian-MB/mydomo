"""How far the first launch has got.

There are two steps after the installation itself: create the owner, then give Domo a model.
The stage is read from the database, where the steps record themselves, and only ever moves
forward: ``owner`` (nobody has claimed the instance yet), ``model`` (the owner exists, no model
works yet) and ``done``.

An owner exists when a person has the owner role or when the setup state says the owner was
created. The two are written together, in one transaction; either one is enough to close the
owner step, and the unique index on the owner role is what makes a second owner impossible.
"""

from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.setup_flow.models import SetupState

SetupStage = Literal["owner", "model", "done"]

# Where a setup page lives. Everything under ``SETUP_PATH`` belongs to the first launch.
SETUP_PATH: Final = "/setup"
STEP_PATHS: Final[dict[Literal["owner", "model"], str]] = {
    "owner": f"{SETUP_PATH}/owner",
    "model": f"{SETUP_PATH}/model",
}


@dataclass(frozen=True)
class SetupProgress:
    """What the database says has been done."""

    owner_created: bool
    model_configured: bool


async def read_progress(session: AsyncSession) -> SetupProgress:
    """The steps done so far, read in one query.

    The migration creates the one ``setup_state`` row, so a missing row means the migrations did
    not run: that is an error, not a fresh instance.
    """
    owner_role_exists = exists(select(Person.id).where(Person.role == "owner"))
    result = await session.execute(
        select(
            SetupState.owner_created_at.is_not(None) | owner_role_exists,
            SetupState.model_configured_at.is_not(None),
        ).where(SetupState.id == 1)
    )
    row = result.one_or_none()
    if row is None:
        raise RuntimeError("the setup_state row is missing: the database migrations have not run")
    owner_created, model_configured = row
    return SetupProgress(owner_created=bool(owner_created), model_configured=bool(model_configured))


async def owner_exists(session: AsyncSession) -> bool:
    """Whether the instance already has its owner."""
    return (await read_progress(session)).owner_created


async def setup_stage(session: AsyncSession) -> SetupStage:
    """The step the first launch is at (``done`` once the owner exists and a model works)."""
    progress = await read_progress(session)
    if not progress.owner_created:
        return "owner"
    if not progress.model_configured:
        return "model"
    return "done"
