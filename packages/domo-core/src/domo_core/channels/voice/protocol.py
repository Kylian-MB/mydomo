"""The messages of the page's voice socket (``/chat/voice``, spec 6.1, 6.2): one definition, read
by the route (``channels.voice.routes``) and written by whoever speaks to the page
(``channels.voice.listener``, the voice session).

From the page, text frames (JSON, at most ``MAX_TEXT_BYTES``), each one exactly:

* ``{"type": "hello", "microphone": "<id>", "person": "<uuid>"}``: first, once. ``microphone`` is
  the id the page made for its microphone (1 to 80 of ``A-Z a-z 0-9 . _ : -``): the page's claim,
  never a proof of a place. ``person`` is the id of the person the page was rendered for (the
  value the page already sends as ``X-Domo-Page-Person`` with its requests, Task 16): a tab left
  open for one person after another signed in, in another tab, shares the other's cookie, and
  its socket is closed with ``CLOSE_ANOTHER_PERSON`` (the page then reloads). It is sent in the
  message, never in the address: the address reaches the access log;
* ``{"type": "speech_start"}``, then the sound as binary frames, then ``{"type": "speech_end"}``:
  one moment of speech; the sound is PCM, 16-bit little-endian, 16 000 Hz, mono, each frame at
  most ``MAX_FRAME_BYTES`` and a whole number of samples;
* ``{"type": "played", "reply": "<uuid>", "whole": true|false}``: whether the page played a reply
  to its end;
* ``{"type": "off"}``: the person turned the voice off.

To the page, text frames: ``state`` (``hearing``: ``ready``, ``preparing``, ``unavailable``, or
``gesture``: the page sends a moment of speech only while the person holds the gesture to talk),
``say`` (``reply``, ``index``, ``rate``) then the sound as binary frames (16-bit, ``SAY_RATE``,
mono) then ``say_end``; ``duck``, ``stop``, ``resume`` (the page raises its sound, and a reply
it was told to stop plays its next sentences); ``speech_unavailable`` (``reason``);
``listening`` (``on``); ``failed`` (``reason``: what was said could not be answered, a key the
page shows as ``chat.error.<reason>``, the assistant's text); ``not_taken`` (``reason``: what
was said is not taken, ``elsewhere`` when a reserved moment of the person belongs to another
microphone, ``not_now`` when a voice nobody known was not answered or the microphone was kept
for another person's words: one notice for both, which tells an imitator nothing).

Anything else from the page is a ``ProtocolError``, whose message never repeats the frame: a frame
may hold anything, and an error message can reach a log.
"""

import json
import re
from dataclasses import dataclass
from typing import Final, Literal, cast
from uuid import UUID

from domo_core.llm.types import JsonObject

# The bounds of what the page sends.
SAMPLE_RATE: Final = 16_000
SAMPLE_BYTES: Final = 2
MAX_TEXT_BYTES: Final = 1024
MAX_FRAME_BYTES: Final = 64 * 1024
# 30 s of sound: at the bound the utterance is closed as if ``speech_end`` came (far below what the
# speech service takes, 16 MB).
MAX_UTTERANCE_BYTES: Final = 30 * SAMPLE_RATE * SAMPLE_BYTES
# 0.3 s: anything shorter is no speech, and is dropped.
MIN_UTTERANCE_BYTES: Final = int(0.3 * SAMPLE_RATE * SAMPLE_BYTES)

# The rate of the sound the page is sent.
SAY_RATE: Final = 24_000

# The codes a socket is closed with. A socket refused before it is accepted (another site, no
# session) is answered 403 by the server whatever the code (ASGI): a browser sees only a failed
# socket (1006), and learns nothing of why. Once accepted, the page reads the code.
CLOSE_NORMAL: Final = 1000
CLOSE_POLICY: Final = 1008  # refused: another site, no session, a message out of the protocol
CLOSE_ERROR: Final = 1011
CLOSE_NOT_YET: Final = 1013  # accepted, then closed: the voice cannot be served yet
CLOSE_ANOTHER_PERSON: Final = 4403  # this tab was rendered for someone else: reload it
CLOSE_REPLACED: Final = 4409  # another socket of the same page or person took its place

_MICROPHONE: Final = re.compile(r"[A-Za-z0-9._:-]{1,80}")


class ProtocolError(Exception):
    """A frame that is not a message of the page. Its message is a fixed reason."""


@dataclass(frozen=True)
class Hello:
    microphone: str
    person: UUID
    type: Literal["hello"] = "hello"


@dataclass(frozen=True)
class SpeechStart:
    type: Literal["speech_start"] = "speech_start"


@dataclass(frozen=True)
class SpeechEnd:
    type: Literal["speech_end"] = "speech_end"


@dataclass(frozen=True)
class Played:
    reply: UUID
    whole: bool
    type: Literal["played"] = "played"


@dataclass(frozen=True)
class Off:
    type: Literal["off"] = "off"


type PageMessage = Hello | SpeechStart | SpeechEnd | Played | Off


def parse(frame: str) -> PageMessage:
    """The message of a text frame of the page; ``ProtocolError`` for anything else."""
    if len(frame.encode()) > MAX_TEXT_BYTES:
        raise ProtocolError("too long")
    try:
        data = json.loads(frame)
    except ValueError:
        raise ProtocolError("not json") from None
    if not isinstance(data, dict):
        raise ProtocolError("not an object")
    fields = cast(dict[str, object], data)
    kind = fields.get("type")
    keys = set(fields) - {"type"}
    match kind:
        case "hello" if keys == {"microphone", "person"}:
            microphone = fields["microphone"]
            if not isinstance(microphone, str) or _MICROPHONE.fullmatch(microphone) is None:
                raise ProtocolError("bad microphone")
            return Hello(microphone=microphone, person=_uuid(fields["person"], "bad person"))
        case "speech_start" if not keys:
            return SpeechStart()
        case "speech_end" if not keys:
            return SpeechEnd()
        case "played" if keys == {"reply", "whole"}:
            whole = fields["whole"]
            if not isinstance(whole, bool):
                raise ProtocolError("bad played")
            return Played(reply=_uuid(fields["reply"], "bad reply"), whole=whole)
        case "off" if not keys:
            return Off()
        case _:
            raise ProtocolError("unknown message")


def _uuid(value: object, why: str) -> UUID:
    if not isinstance(value, str):
        raise ProtocolError(why)
    try:
        return UUID(value)
    except ValueError:
        raise ProtocolError(why) from None


def dump(message: PageMessage) -> str:
    """The text frame of a message of the page (what the page sends)."""
    match message:
        case Hello(microphone=microphone, person=person):
            return json.dumps({"type": "hello", "microphone": microphone, "person": str(person)})
        case Played(reply=reply, whole=whole):
            return json.dumps({"type": "played", "reply": str(reply), "whole": whole})
        case SpeechStart() | SpeechEnd() | Off():
            return json.dumps({"type": message.type})


# --- to the page ------------------------------------------------------------------------------

type Hearing = Literal["ready", "preparing", "unavailable", "gesture"]


def duck() -> JsonObject:
    """Someone speaks over the voice: the page lowers its sound."""
    return {"type": "duck"}


def stop() -> JsonObject:
    """Stop playing now."""
    return {"type": "stop"}


def resume() -> JsonObject:
    """The page raises its sound again; a reply it was told to stop plays its next sentences."""
    return {"type": "resume"}


def state(hearing: Hearing) -> JsonObject:
    """Whether the assistant can hear."""
    return {"type": "state", "hearing": hearing}


def say(reply: UUID, index: int) -> JsonObject:
    """The sound of sentence ``index`` of ``reply`` follows, as binary frames at ``SAY_RATE``."""
    return {"type": "say", "reply": str(reply), "index": index, "rate": SAY_RATE}


def say_end(reply: UUID, index: int) -> JsonObject:
    """The sound of sentence ``index`` of ``reply`` is over."""
    return {"type": "say_end", "reply": str(reply), "index": index}


def speech_unavailable(reason: str) -> JsonObject:
    """The voice cannot speak (``reason`` is a key the page puts in words)."""
    return {"type": "speech_unavailable", "reason": reason}


def failed(reason: str) -> JsonObject:
    """What was said could not be answered (``reason``: ``no_key``); the page shows the
    assistant's text of ``chat.error.<reason>``, nothing is said aloud."""
    return {"type": "failed", "reason": reason}


def not_taken(reason: str) -> JsonObject:
    """What was said here is not taken (``reason``: ``elsewhere``, ``not_now``); the page puts it
    in the assistant's words."""
    return {"type": "not_taken", "reason": reason}


def listening(*, on: bool) -> JsonObject:
    """Whether the page keeps its microphone open."""
    return {"type": "listening", "on": on}
