"""How much longer the FIRST call of a freshly loaded engine takes than the next ones.

The service's measure (``domo_speech.real.trial``) times the first calls of an engine it has just
built; this shows what that first call costs on this machine, model by model.

    python -m scripts.voice_bench.first_call faster-whisper-base --gpu

Sounds come from the recorded set (``DOMO_VOICE_TRIALS_DIR``); weights from
``DOMO_SPEECH_MODEL_DIR``. Nothing is written.
"""

import argparse
import os
import time
import wave
from pathlib import Path
from typing import Final

TRIALS_DIR: Final = Path(
    os.environ.get("DOMO_VOICE_TRIALS_DIR", "D:/domo-voice-models/trials/voices")
)
MODEL_DIR: Final = Path(os.environ.get("DOMO_SPEECH_MODEL_DIR", "D:/domo-voice-models/models"))
SOUND: Final = "alex.test0.fr.mic1.wav"


def main() -> None:
    from domo_speech.data import candidates
    from domo_speech.real.build import build, uses_gpu
    from domo_speech.real.hears import Hearing
    from domo_speech.real.prints import EmbeddingPrint
    from domo_speech.real.weights import ensure_weights

    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--calls", type=int, default=4)
    args = parser.parse_args()
    entry = next(e for e in candidates() if e.id == args.model)
    with wave.open(str(TRIALS_DIR / SOUND), "rb") as source:
        pcm = source.readframes(source.getnframes())
    seconds = len(pcm) / 32_000
    start = time.perf_counter()
    on_gpu = uses_gpu(entry, args.gpu)
    engine = build(entry, ensure_weights(entry, MODEL_DIR), gpu=on_gpu, program=None)
    print(f"{args.model} on {'gpu' if on_gpu else 'cpu'}: load {time.perf_counter() - start:.2f} s")
    for call in range(args.calls):
        start = time.perf_counter()
        if isinstance(engine, Hearing):
            engine.transcribe(pcm, None)
        elif isinstance(engine, EmbeddingPrint):
            engine.voice_print(pcm)
        else:
            raise SystemExit("hearing and print models only")
        took = time.perf_counter() - start
        print(f"call {call + 1}: {took:.3f} s, real-time factor {took / seconds:.3f}")


if __name__ == "__main__":
    main()
