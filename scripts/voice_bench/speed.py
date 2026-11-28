"""Speed and capacity of a running speech service, measured from outside, the way the core calls
it (HTTP on this machine, no stack).

For each model the service lists: hearing (real-time factor on the sentences of the recorded set
and on 40 s without a pause), saying (time to the FIRST chunk of sound of a short reply and of a
longer one, and the real-time factor), voice prints (time for a sentence and for 40 s). Then two
at once, for real: two hearings together, two sayings together, a hearing while a saying, each
against the same call alone. Then the memory of the service's process (and its graphics memory,
when ``nvidia-smi`` knows it). Errors are counted, never hidden.

    python -m scripts.voice_bench.speed --url http://127.0.0.1:18500 --pid <service pid> \
        --label gpu

The sounds are read from the recorded set (``DOMO_VOICE_TRIALS_DIR``); the result goes to
``DOMO_VOICE_TRIALS_RESULTS/speed-<label>.json``. Nothing is written inside the repository.
"""

import argparse
import json
import os
import statistics
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Final

import httpx

TRIALS_DIR: Final = Path(
    os.environ.get("DOMO_VOICE_TRIALS_DIR", "D:/domo-voice-models/trials/voices")
)
RESULTS_DIR: Final = Path(
    os.environ.get("DOMO_VOICE_TRIALS_RESULTS", "D:/domo-voice-models/trials/results")
)
SENTENCES: Final = (
    "alex.test0.fr.mic1.wav",
    "dana.test1.fr.mic1.wav",
    "paul.test2.en.mic1.wav",
    "sam.test3.en.mic1.wav",
)
LONG: Final = "alex.long.fr.mic1.wav"
REPLIES: Final = {
    "short": ("fr", "D'accord, j'allume la lumière du salon."),
    "long": (
        "fr",
        "Bien sûr. Demain il fera beau le matin, avec quelques nuages l'après-midi. "
        "Pense à prendre une veste si tu sors le soir, il fera plus frais.",
    ),
}
REPEAT: Final = 3


def seconds_of(wav: bytes) -> float:
    return (len(wav) - 44) / 32_000


class Service:
    def __init__(self, url: str) -> None:
        self.client = httpx.Client(base_url=url, timeout=600)
        self.errors: list[str] = []

    def models(self) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self.client.get("/v1/models").json()["data"]
        return data

    def _post_sound(self, route: str, wav: bytes, model: str) -> float:
        start = time.perf_counter()
        answer = self.client.post(
            route, files={"file": ("u.wav", wav, "audio/wav")}, data={"model": model}
        )
        took = time.perf_counter() - start
        if answer.status_code != 200:
            self.errors.append(f"{route} {model}: {answer.status_code} {answer.text[:80]}")
        return took

    def hear(self, wav: bytes, model: str) -> float:
        return self._post_sound("/v1/audio/transcriptions", wav, model)

    def print_(self, wav: bytes, model: str) -> float:
        return self._post_sound("/v1/voice/print", wav, model)

    def say(self, text: str, language: str, model: str) -> tuple[float, float, float]:
        """(time to the first chunk, total time, seconds of sound)."""
        start = time.perf_counter()
        first = 0.0
        size = 0
        body = {"model": model, "input": text, "language": language}
        try:
            with self.client.stream("POST", "/v1/audio/speech", json=body) as answer:
                if answer.status_code != 200:
                    answer.read()
                    self.errors.append(f"speech {model}: {answer.status_code} {answer.text[:80]}")
                for chunk in answer.iter_bytes():
                    if not first and chunk:
                        first = time.perf_counter() - start
                    size += len(chunk)
        except httpx.HTTPError as error:
            self.errors.append(f"speech {model}: {type(error).__name__}")
        return first, time.perf_counter() - start, size / 48_000


def median(values: list[float]) -> float:
    return round(statistics.median(values), 3) if values else float("nan")


def together(*calls: Callable[[], Any]) -> list[Any]:
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def memory_mb(pid: int) -> float | None:
    """The working set of ``pid`` (Windows ``tasklist``, or ``/proc`` elsewhere)."""
    try:
        if os.name == "nt":
            line = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh", "/fi", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            kilobytes = "".join(ch for ch in line.split('","')[-1] if ch.isdigit())
            return round(int(kilobytes) / 1024, 1)
        for row in Path(f"/proc/{pid}/status").read_text().splitlines():
            if row.startswith("VmRSS:"):
                return round(int(row.split()[1]) / 1024, 1)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def gpu_memory_mb(pid: int) -> float | None:
    """The graphics memory ``nvidia-smi`` gives ``pid`` (Windows often reports none per process:
    then the card's whole use is given instead, marked as such by the caller)."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in out.splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) == 2 and parts[0] == str(pid) and parts[1].isdigit():
            return float(parts[1])
    return None


def gpu_used_mb() -> float | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return float(out[0]) if out and out[0].isdigit() else None


def measure(url: str, pid: int | None, only: set[str]) -> dict[str, Any]:
    service = Service(url)
    sounds = {name: (TRIALS_DIR / name).read_bytes() for name in (*SENTENCES, LONG)}
    found: dict[str, Any] = {"models": {}, "at_once": {}, "memory": {}}
    listed = [m for m in service.models() if not only or m["id"] in only]
    found["gpu_used_before_mb"] = gpu_used_mb()
    for entry in listed:
        model, does = entry["id"], entry["does"]
        found["at_once"][model] = entry.get("at_once")
        result: dict[str, Any] = {"does": does, "measures": entry.get("measures", {})}
        if does == "hear":
            service.hear(sounds[SENTENCES[0]], model)  # loads it
            rtf = [
                service.hear(sounds[name], model) / seconds_of(sounds[name])
                for name in SENTENCES
                for _ in range(REPEAT)
            ]
            one = [service.hear(sounds[SENTENCES[0]], model) for _ in range(REPEAT)]
            long = service.hear(sounds[LONG], model)
            result |= {
                "rtf_sentences": median(rtf),
                "sentence_s": round(seconds_of(sounds[SENTENCES[0]]), 2),
                "hear_sentence_s": median(one),
                "hear_40s_s": round(long, 2),
                "rtf_40s": round(long / seconds_of(sounds[LONG]), 3),
            }
            a, b = sounds[SENTENCES[0]], sounds[SENTENCES[2]]
            alone_a = [service.hear(a, model) for _ in range(REPEAT)]
            pairs = [
                together(partial(service.hear, a, model), partial(service.hear, b, model))
                for _ in range(REPEAT)
            ]
            result["two_hearings"] = {
                "alone_s": median(alone_a),
                "together_s": [median([p[0] for p in pairs]), median([p[1] for p in pairs])],
            }
        elif does == "say":
            service.say("Bonjour.", "fr", model)  # loads it
            for name, (language, text) in REPLIES.items():
                runs = [service.say(text, language, model) for _ in range(REPEAT)]
                result[f"reply_{name}"] = {
                    "first_chunk_s": median([r[0] for r in runs]),
                    "total_s": median([r[1] for r in runs]),
                    "sound_s": median([r[2] for r in runs]),
                    "rtf": median([r[1] / r[2] for r in runs if r[2]]),
                }
            language, text = REPLIES["short"]
            pairs = [
                together(
                    partial(service.say, text, language, model),
                    partial(service.say, text, language, model),
                )
                for _ in range(REPEAT)
            ]
            result["two_sayings"] = {
                "alone_first_chunk_s": result["reply_short"]["first_chunk_s"],
                "alone_total_s": result["reply_short"]["total_s"],
                "together_first_chunk_s": [median([p[i][0] for p in pairs]) for i in (0, 1)],
                "together_total_s": [median([p[i][1] for p in pairs]) for i in (0, 1)],
            }
        else:
            service.print_(sounds[SENTENCES[0]], model)  # loads it
            result |= {
                "print_sentence_s": median(
                    [service.print_(sounds[SENTENCES[0]], model) for _ in range(REPEAT)]
                ),
                "print_40s_s": round(service.print_(sounds[LONG], model), 3),
            }
        found["models"][model] = result
        if pid is not None:
            found["memory"][f"after {model}"] = {
                "ram_mb": memory_mb(pid),
                "gpu_mb_of_process": gpu_memory_mb(pid),
                "gpu_mb_card": gpu_used_mb(),
            }
    hearing = next((m["id"] for m in listed if m["does"] == "hear"), None)
    voice = next((m["id"] for m in listed if m["does"] == "say"), None)
    if hearing and voice:
        a = sounds[SENTENCES[0]]
        language, text = REPLIES["short"]
        hear_alone = [service.hear(a, hearing) for _ in range(REPEAT)]
        say_alone = [service.say(text, language, voice) for _ in range(REPEAT)]
        mixed = [
            together(partial(service.hear, a, hearing), partial(service.say, text, language, voice))
            for _ in range(REPEAT)
        ]
        found["hearing_while_saying"] = {
            "hearing": hearing,
            "voice": voice,
            "hear_alone_s": median(hear_alone),
            "hear_together_s": median([m[0] for m in mixed]),
            "say_first_chunk_alone_s": median([s[0] for s in say_alone]),
            "say_first_chunk_together_s": median([m[1][0] for m in mixed]),
            "say_total_alone_s": median([s[1] for s in say_alone]),
            "say_total_together_s": median([m[1][1] for m in mixed]),
        }
    if pid is not None:
        found["memory"]["end"] = {
            "ram_mb": memory_mb(pid),
            "gpu_mb_of_process": gpu_memory_mb(pid),
            "gpu_mb_card": gpu_used_mb(),
        }
    found["errors"] = service.errors
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18500")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--label", default="run")
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    found = measure(args.url, args.pid, set(args.only))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"speed-{args.label}.json"
    path.write_text(json.dumps(found, indent=1), encoding="utf-8")
    print(json.dumps(found, indent=1))


if __name__ == "__main__":
    main()
