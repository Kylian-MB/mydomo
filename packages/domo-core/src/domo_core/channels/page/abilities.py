"""What the page can do as a channel."""

from typing import Final

from domo_core.conversation.channel import Abilities

# What the page can do: a card with a button, something shown once to the person alone, the
# vault's forms; it receives nothing it did not ask for, and a person proves it is them there with
# a passkey or their password.
PAGE: Final = Abilities(
    buttons=True,
    shows_once=True,
    safe_screen=True,
    hears_unasked=False,
    proof_kinds=frozenset({"passkey", "password"}),
)
