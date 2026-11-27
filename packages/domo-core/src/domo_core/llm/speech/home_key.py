"""The id of the key of the house's speech service, in a module that imports nothing, so that the
router can tell that key apart (it hears and says, it never thinks) without importing the upkeep
that records it (``llm.speech.home``)."""

import hashlib
from typing import Final

# Fixed, so that the key is found whatever its address.
HOME_SPEECH_ID: Final = "p-" + hashlib.sha256(b"home-speech-service").hexdigest()[:8]
