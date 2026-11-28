"""Voice prints: a speaker-embedding model (ONNX Runtime) over Kaldi filter banks, and the
intonation measured beside it."""

from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from domo_speech.engines import VoicePrint
from domo_speech.real.device import onnx_providers, runtime
from domo_speech.real.fbank import fbank
from domo_speech.real.intonation import intonation
from domo_speech.real.sound import floats_of

_BYTES_PER_SECOND: Final = 32_000


def unit_vector(embedding: npt.NDArray[np.floating]) -> npt.NDArray[np.float32]:
    """``embedding`` at length 1; ``ValueError`` when it is empty, not finite or zero (a model
    that failed silently)."""
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vector.size == 0 or not bool(np.isfinite(vector).all()):
        raise ValueError("bad_print")
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("bad_print")
    return vector / norm


class EmbeddingPrint:
    """One embedding model (its single ``.onnx`` file). Blocking; several sounds may be
    printed at once from several threads (an ONNX Runtime session may be run concurrently)."""

    def __init__(self, weights: Path, model_id: str, *, gpu: bool) -> None:
        ort = runtime("onnxruntime")
        (onnx,) = sorted(weights.glob("*.onnx"))
        self._session: Any = ort.InferenceSession(str(onnx), providers=onnx_providers(gpu))
        self._input: str = self._session.get_inputs()[0].name
        self._model_id = model_id

    def voice_print(self, pcm: bytes) -> VoicePrint:
        features = fbank(floats_of(pcm))
        if len(features) == 0:
            raise ValueError("too_short")
        (embedding,) = self._session.run(None, {self._input: features[None, :, :]})[:1]
        unit = unit_vector(np.asarray(embedding, dtype=np.float32))
        pitch, spread, rate = intonation(pcm)
        return VoicePrint(
            vector=tuple(float(x) for x in unit),
            model=self._model_id,
            seconds=len(pcm) / _BYTES_PER_SECOND,
            pitch_hz=pitch,
            pitch_range_hz=spread,
            rate=rate,
        )

    def close(self) -> None:
        self._session = None
