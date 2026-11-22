"""Who speaks on the page: the person of the session, which the code is sure of."""

import uuid
from datetime import datetime
from typing import Final

from domo_core.conversation.speaker import Clue, Speaker

# The name of the page's channel, for the judge, the audit and what reached whom.
PAGE_CHANNEL: Final = "page"


def page_speaker(person_id: uuid.UUID, now: datetime) -> Speaker:
    """The speaker of a request that came with the session of ``person_id``: the session is a
    strong clue, made by the code, that it is them."""
    return Speaker(person_id, PAGE_CHANNEL, clues=(Clue("session", "for", "strong", "code", now),))
