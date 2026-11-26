"""Who speaks on the channel of the API: the device whose secret the request carries, and its
person; what the channel can do; and its refusals, always JSON.

A request names its device by ``Authorization: Bearer <secret>`` and by nothing else: the secret
is checked by the code (``devices.service.device_of``: only its hash is kept), never logged, never
kept in a message, the audit or a prompt. A secret that is missing, unknown, of a device taken
back or of someone who is no longer in the household is refused alike, 401 ``unknown_device``,
never by a redirection: a program reads the answer, no browser follows it. A device known is a
strong clue, made by the code, that it is its person who speaks (spec 6.5): as strong as a session
of the page.
"""

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Final

from fastapi import Depends, Request
from starlette.responses import JSONResponse

from domo_core.accounts.models import Person
from domo_core.conversation.channel import Abilities
from domo_core.conversation.speaker import Clue, Speaker
from domo_core.devices.models import Device
from domo_core.devices.service import device_of
from domo_core.web.deps import SessionFactory, get_clock, get_session_factory

# What the channel can do: no button, something shown once to the person alone (the program shows
# it on their screen), no vault form (the key of a model is given on a page), what Domo says on
# his own (the stream of ``GET /listen``), and a password as a proof.
API: Final = Abilities(
    buttons=False,
    shows_once=True,
    safe_screen=False,
    hears_unasked=True,
    proof_kinds=frozenset({"password"}),
)
# The name of the channel, for the judge and the audit (never branched on).
API_CHANNEL: Final = "api"
# The longest secret read: a device's is 43 characters.
_MAX_SECRET_LENGTH: Final = 256
_BEARER: Final = "bearer"


class ApiRefusal(Exception):
    """A request the channel refuses: answered ``status`` with ``{"error": error}`` (and
    ``headers``) by ``refusal_response``."""

    def __init__(self, status: int, error: str, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.headers = dict(headers or {})


def refusal(status: int, error: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    """The JSON answer of a refusal: ``{"error": <code>}``."""
    return JSONResponse({"error": error}, status_code=status, headers=dict(headers or {}))


async def refusal_response(request: Request, exc: Exception) -> JSONResponse:
    """The exception handler of ``ApiRefusal``."""
    assert isinstance(exc, ApiRefusal)
    return refusal(exc.status, exc.error, exc.headers)


def api_speaker(person_id: uuid.UUID, device_id: uuid.UUID, paired_at: datetime) -> Speaker:
    """Who speaks through the device ``device_id`` of ``person_id``: the known device is a strong
    clue, made by the code, that it is them, dated when it was paired (``paired_at``)."""
    return Speaker(
        person_id,
        API_CHANNEL,
        device_id=device_id,
        clues=(Clue("device", "for", "strong", "code", paired_at),),
    )


def secret_of(request: Request) -> str | None:
    """The device's secret the request carries (``Authorization: Bearer <secret>``), if it carries
    one that could be one."""
    found = request.headers.getlist("authorization")
    if len(found) != 1:
        return None
    scheme, _, secret = found[0].partition(" ")
    secret = secret.strip()
    if scheme.lower() != _BEARER or not secret or len(secret) > _MAX_SECRET_LENGTH:
        return None
    return secret


async def current_device(
    request: Request,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> tuple[Device, Person]:
    """The device the request's secret belongs to and its person (``device_of``, which notes
    when it was last seen: committed). ``ApiRefusal`` 401 ``unknown_device`` otherwise."""
    secret = secret_of(request)
    if secret is None:
        raise ApiRefusal(401, "unknown_device", {"WWW-Authenticate": "Bearer"})
    async with session_factory() as session:
        found = await device_of(session, secret, clock())
        await session.commit()
    if found is None:
        raise ApiRefusal(401, "unknown_device", {"WWW-Authenticate": "Bearer"})
    return found
