"""What the voice can do as a channel."""

from typing import Final

from domo_core.conversation.channel import Abilities

# What the voice can do: no button, nothing shown once (no screen), no vault form; it hears what
# Domo says on his own, and a person proves it is them there with a spoken phrase. Its words are
# said aloud.
VOICE: Final = Abilities(
    buttons=False,
    shows_once=False,
    safe_screen=False,
    hears_unasked=True,
    proof_kinds=frozenset({"spoken_phrase"}),
    spoken=True,
)
# The name of the channel, for the judge and the audit (never branched on).
VOICE_CHANNEL: Final = "voice"
