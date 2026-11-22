"""The catalog of models in force: the latest snapshot Domo read, else the shipped one."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.llm.catalog import Catalog, catalog_from_json, shipped_catalog
from domo_core.llm.spending_models import CatalogSnapshot

logger = logging.getLogger(__name__)


async def current_catalog(session: AsyncSession) -> Catalog:
    """The latest row of ``catalog_snapshots``, or ``shipped_catalog()`` when there is none, or
    when the latest one cannot be read (logged)."""
    row = await session.execute(
        select(CatalogSnapshot.data)
        .order_by(CatalogSnapshot.fetched_at.desc(), CatalogSnapshot.id.desc())
        .limit(1)
    )
    data = row.scalar_one_or_none()
    if data is None:
        return shipped_catalog()
    try:
        return catalog_from_json(data)
    except ValueError:
        logger.error("The latest catalog of models cannot be read: the shipped one is used")
        return shipped_catalog()
