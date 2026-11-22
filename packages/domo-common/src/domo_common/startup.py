"""Start-up helpers shared by the core and the gatekeeper."""

import logging
from collections.abc import Iterable

from pydantic import ValidationError


def describe_settings_error(error: ValidationError) -> str:
    """What is wrong with the settings, by variable name (``DOMO_*``), without echoing the
    values: one of them may be a credential."""
    problems: list[str] = []
    for detail in error.errors(include_url=False, include_input=False, include_context=False):
        field = ".".join(str(part) for part in detail["loc"])
        name = f"DOMO_{field.upper()}" if field else "settings"
        problems.append(f"{name}: {detail['msg']}")
    return "; ".join(problems)


def pin_loggers_at_warning(names: Iterable[str]) -> None:
    """Keep the named loggers at WARNING or above, whatever the root logger says: at DEBUG, the
    HTTP libraries and the model SDKs log headers, and so tokens and keys. A logger that is
    already stricter is left alone."""
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(max(logger.level, logging.WARNING))
