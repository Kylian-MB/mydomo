"""The name a reader sees for the assistant: the person's own (``Person.calls_domo``), else the
house's (``house.domo_name``), else ``DEFAULT_NAME``.

This module sits below ``tools`` and ``policy``: it imports from neither.
"""

from typing import Final

from domo_core.text import cleaned_name, is_plain_line

DEFAULT_NAME: Final = "Domo"
# The longest name a person or the house gives the assistant (the ``persons`` table says the same).
MAX_DOMO_NAME_LENGTH: Final = 40


def name_shown(calls_domo: str | None, house_name: str | None) -> str:
    """The name to show: the person's own, else the house's, else ``DEFAULT_NAME``."""
    return calls_domo or house_name or DEFAULT_NAME


def is_domo_name(value: object) -> bool:
    """Whether ``value`` is a name as it is kept: a text of one plain line, already cleaned
    (``text.cleaned_name`` changes nothing), 1 to ``MAX_DOMO_NAME_LENGTH`` characters."""
    return (
        isinstance(value, str)
        and is_plain_line(value)
        and cleaned_name(value) == value
        and 1 <= len(value) <= MAX_DOMO_NAME_LENGTH
    )
