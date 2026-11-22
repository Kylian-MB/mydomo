"""Running the database migrations (Alembic) from Python.

The migrations ship inside the package, in ``domo_core/migrations``, and not next to
``pyproject.toml``: an installed core (the container has no source tree) can only reach what the
wheel contains. ``alembic.ini`` at the root of the ``domo-core`` project points the ``alembic``
command line at the same directory for development.

Alembic's async environment starts its own event loop, so ``upgrade_to_head`` must not be called
from running async code (from a coroutine, run it with ``asyncio.to_thread``).
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(database_url: str) -> Config:
    """An Alembic configuration for the migrations of this package, on ``database_url``.

    The URL is handed over as an attribute, not as an ini option: ini values go through
    ``%`` interpolation, which would mangle a URL-encoded password.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_to_head(database_url: str) -> None:
    """Bring the database up to the latest revision (nothing happens when it already is)."""
    command.upgrade(alembic_config(database_url), "head")
