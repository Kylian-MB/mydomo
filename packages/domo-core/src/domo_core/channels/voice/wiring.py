"""The voice sessions of the application (``app.state.voice_sessions``, read by the route
``channels.voice.routes``): each socket's session is made with what a turn needs of the
application as it is when the socket opens (``channels.turns.app_turn_deps``), the speech client
of the house's relay (``app.state.speech``) behind the one shared ``AtHome``
(``app.state.at_home``), and the count of the password checks of the account page."""

from datetime import datetime
from uuid import UUID

from fastapi import FastAPI

from domo_core.channels.turns import app_turn_deps
from domo_core.channels.voice.deps import MeteredSpeechOpener, Told, VoiceDeps
from domo_core.channels.voice.listener import VoiceSink
from domo_core.channels.voice.routes import VoiceSessionFactory
from domo_core.channels.voice.session import VoiceSession


def voice_sessions_of(app: FastAPI) -> VoiceSessionFactory:
    """The factory of the voice sessions of ``app``. The facts the owner was told once (no model
    may hear, or weigh what is heard), or is being told, are shared by its sessions, for the life
    of the process."""
    told = Told()

    async def open_session(
        *,
        person_id: UUID,
        microphone: str,
        origin: str,
        sink: VoiceSink,
        signed_in_at: datetime | None,
    ) -> VoiceSession:
        turn = app_turn_deps(app)
        state = app.state
        speech = MeteredSpeechOpener(
            state.speech,
            session_factory=turn.session_factory,
            clock=turn.clock,
            at_home=state.at_home,
        )
        deps = VoiceDeps(
            turn=turn,
            speech=speech,
            throttle=state.password_check_throttle,
            told=told,
        )
        return VoiceSession(
            deps,
            person_id=person_id,
            microphone=microphone,
            origin=origin,
            sink=sink,
            signed_in_at=signed_in_at,
        )

    return open_session
