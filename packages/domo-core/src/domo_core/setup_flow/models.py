"""The first-launch state: which setup steps are done."""

from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base


class SetupState(Base):
    """The one row (``id`` is always 1) that tells how far the first launch has got.

    The migration creates the row, empty: code that reads or updates it can rely on it being
    there. Stage rules (what "owner created" means for the gate) live in ``setup_flow``.
    """

    __tablename__ = "setup_state"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    # SHA-256 of the current one-time setup code; cleared once the owner exists.
    setup_code_hash: Mapped[str | None]
    code_created_at: Mapped[datetime | None]
    owner_created_at: Mapped[datetime | None]
    model_configured_at: Mapped[datetime | None]
