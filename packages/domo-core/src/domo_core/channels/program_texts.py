"""The sentences of the text program (``domo-talk``), written by Domo like every text a person
reads (spec 6.12, finding WEB-8): ``GET /api/channel/texts`` gives, in the language of the person
whose device asks, the texts of the keys of ``TALK_KEYS`` as they read for that person (their own
version, else their language's, else the default phrase), their ``{name}`` holes left for the
program to fill.

It is a route of the channel of the API, authenticated as every route of it: the device's secret
(``channels.api.auth.current_device``), a browser session refused alike. It lives here, not in
``channels.api.routes``, which it only imports from. Asking for the texts also has the group
``talk`` written for the reader when Domo has a reason to, as a page does (the channel is outside
the pages' middleware, ``web.texts_middleware``); the texts show from the next fetch on.
"""

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from domo_core.accounts.models import Person
from domo_core.channels.api import API_PATH
from domo_core.channels.api.auth import current_device
from domo_core.devices.models import Device
from domo_core.texts.store import Reader
from domo_core.texts.view import RecueilTexts, person_texts
from domo_core.web.deps import SessionFactory, get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix=API_PATH)

# What the program says on its own, once the person is known: the keys it reads.
TALK_KEYS: Final[tuple[str, ...]] = (
    "talk.no_key",
    "talk.too_long",
    "talk.not_sayable",
    "talk.held_back",
    "talk.refused",
    "talk.proof.prompt",
    "talk.proof.nothing_waits",
    "talk.proof.busy",
    "talk.unknown_command",
    "talk.unreachable",
    "talk.not_trusted",
    "talk.unpaired_now",
)
TALK_GROUP: Final = "talk"


@router.get("/texts")
async def program_texts(
    request: Request,
    found: Annotated[tuple[Device, Person], Depends(current_device)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> JSONResponse:
    """``{"language", "texts": {key: sentence}}``: what the program says, as the person reads it."""
    _, person = found
    async with session_factory() as session:
        texts = await person_texts(session, person)
    said = {key: texts.template(key) for key in TALK_KEYS}
    _ask_to_write(request, person, texts)
    return JSONResponse({"language": texts.language, "texts": said})


def _ask_to_write(request: Request, person: Person, texts: RecueilTexts) -> None:
    """Have the group written for the person in the background, as a page does; never a reason to
    fail the answer."""
    try:
        writing = request.app.state.text_writing
        writing.ask(
            Reader(person.id, texts.language), [TALK_GROUP], writing_note=person.writing_note
        )
    except Exception as error:
        logger.error("Asking for the texts of the program failed (%s)", type(error).__name__)
