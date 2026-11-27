"""Reading what is uploaded, and the bounds."""

import io
import wave
from typing import Final

MAX_SOUND_BYTES: Final = 16 * 1024 * 1024
MAX_TEXT_CHARS: Final = 4000

_SAMPLE_RATE: Final = 16_000


def pcm_of(upload: bytes) -> bytes:
    """The PCM s16le 16 kHz mono of an upload: a WAV file (header stripped and checked) or raw PCM.

    Raises ``ValueError`` (a message that never holds the sound) for a WAV file that is not
    16 kHz mono s16le, or that cannot be read.
    """
    if not (upload[:4] == b"RIFF" and upload[8:12] == b"WAVE"):
        return upload
    try:
        with wave.open(io.BytesIO(upload), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != _SAMPLE_RATE
                or wav.getcomptype() != "NONE"
            ):
                raise ValueError("the sound is not 16 kHz mono 16-bit PCM")
            return wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        raise ValueError("the sound is not a readable WAV file") from None
