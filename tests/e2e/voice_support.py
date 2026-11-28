"""What the voice trials through the stack share (``test_voice_stack.py``): the stack with the
real speech service, a microphone fed from a file, and what the page did, read on its own clock.

* ``voice_stack``: the stack of ``compose.yaml`` with the fake model (``compose.test.yaml``), the
  graphics card when asked (``compose.gpu.yaml``) and the speech weights of a folder of this
  machine (``compose.voice.yaml``). Started and taken down with its volumes by the test run, or,
  with ``DOMO_VOICE_STACK_ATTACH=1``, one already running under the same project (started by
  whoever runs the trials, taken down by them).
* ``Microphone``: the sound a browser's fake microphone plays, built from the recorded set
  (``scripts/voice_bench/make_voices.py``): silences and sentences one after the other, written
  OUTSIDE the repository (``DOMO_VOICE_TRIALS_DIR``'s ``stack`` folder), played once (Chromium's
  ``--use-file-for-fake-audio-capture=<file>%noloop``) from the moment the page opens it.
* ``Probe``: what ``voice_page_probe.js`` saw in a page: the socket's frames each way, and when
  the page's speakers played something (a peak above 0.01), all in milliseconds of the page.
"""

import io
import json
import os
import re
import secrets
import time
import wave
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Browser, BrowserType, Page

from tests.stack.stack_support import (
    FAKE_LLM_COMPOSE_FILES,
    FAKE_LLM_KEY_VARIABLE,
    REPO_ROOT,
    StackInfo,
    docker_environment,
    read_env_file,
    running_stack,
)

VOICE_PROJECT = "domo-e2e-voice"
VOICE_ENV_FILE = "tests/e2e/voice_stack.env"
VOICE_COMPOSE = "tests/e2e/compose.voice.yaml"
GPU_COMPOSE = "compose.gpu.yaml"
PROBE_SCRIPT = Path(__file__).with_name("voice_page_probe.js")
RATE = 16_000
# The page ends a moment of speech after 700 ms of silence (voice.js: 22 frames of 32 ms).
PAGE_SILENCE_S = 22 * 512 / RATE
# How long the speech service may take to be ready once the stack is up (it measures its models
# at a first start on a machine).
SPEECH_READY_S = 1200


def device() -> str:
    """``gpu`` or ``cpu``: what the speech service runs on in these trials."""
    return os.environ.get("DOMO_VOICE_TRIALS_DEVICE", "gpu")


def voice_files() -> tuple[str, ...]:
    gpu = (GPU_COMPOSE,) if device() == "gpu" else ()
    return (*FAKE_LLM_COMPOSE_FILES, *gpu, VOICE_COMPOSE)


def voice_env(key: str) -> dict[str, str]:
    return {
        FAKE_LLM_KEY_VARIABLE: key,
        "DOMO_VOICE_MODELS_DIR": os.environ["DOMO_VOICE_MODELS_DIR"],
        "DOMO_SPEECH_DEVICE": "auto" if device() == "gpu" else "cpu",
    }


@contextmanager
def voice_stack() -> Iterator[StackInfo]:
    """The stack of the voice trials, its speech service ready (see the module's docstring)."""
    if os.environ.get("DOMO_VOICE_STACK_ATTACH") == "1":
        key = os.environ[FAKE_LLM_KEY_VARIABLE]
        env = docker_environment(VOICE_ENV_FILE, voice_env(key))
        settings = read_env_file(REPO_ROOT / VOICE_ENV_FILE)
        compose = ["docker", "compose", "-p", VOICE_PROJECT, "--env-file", VOICE_ENV_FILE]
        for name in voice_files():
            compose += ["-f", name]
        info = StackInfo(
            base_url=f"https://localhost:{settings['DOMO_HTTPS_PORT']}",
            http_url=f"http://localhost:{settings['DOMO_HTTP_PORT']}",
            project=VOICE_PROJECT,
            compose_command=tuple(compose),
            env=env,
        )
        wait_for_speech(info)
        yield info
        return
    key = "sk-test-" + secrets.token_hex(16)
    with running_stack(
        files=voice_files(),
        extra_env=voice_env(key),
        project=VOICE_PROJECT,
        env_file=VOICE_ENV_FILE,
    ) as info:
        wait_for_speech(info)
        yield info


def speech_health(stack: StackInfo) -> dict[str, Any]:
    """What the speech service's ``/health`` answers, asked from inside its container (it
    publishes no port)."""
    script = (
        "import json,urllib.request,urllib.error\n"
        "try:\n"
        "    r=urllib.request.urlopen('http://127.0.0.1:8083/health',timeout=5); b=r.read()\n"
        "except urllib.error.HTTPError as e:\n"
        "    b=e.read()\n"
        "print(b.decode())\n"
    )
    result = stack.exec("speech", ["python", "-c", script])
    try:
        return json.loads(result.stdout.strip() or "{}")
    except ValueError:
        return {"status": "unreadable", "out": result.stdout[-200:], "err": result.stderr[-200:]}


def speech_models(stack: StackInfo) -> list[dict[str, Any]]:
    script = (
        "import urllib.request\n"
        "print(urllib.request.urlopen('http://127.0.0.1:8083/v1/models',timeout=5).read().decode())\n"
    )
    result = stack.exec("speech", ["python", "-c", script])
    return json.loads(result.stdout)["data"]


# The lines of the speech service's output shown when it will not be ready.
SPEECH_LOG_LINES = 60


def wait_for_speech(
    stack: StackInfo,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Waits until the speech service hears, says and prints (a use it marks ``slow`` counts)
    with nothing being measured. Gives up at once, with its last lines of output, when it says it
    will not get there in this start: ``failed``, or ``ready`` with a use missing, while it
    neither measures nor will measure again (``retrying``)."""
    deadline = clock() + SPEECH_READY_S
    health: dict[str, Any] = {}
    while clock() < deadline:
        health = speech_health(stack)
        # A fresh service lists each use as it is measured, and says ``measuring`` until the
        # round is over: until then a use may be missing from its list without being named.
        measuring = health.get("measuring")
        if health.get("status") == "ready" and not health.get("missing") and not measuring:
            return
        if health.get("status") in ("ready", "failed") and not (
            measuring or health.get("retrying")
        ):
            raise AssertionError(
                f"the speech service will not hear and say in this start: {health}\n"
                f"--- its last lines ---\n{_last_lines(stack)}"
            )
        sleep(5)
    raise AssertionError(
        f"the speech service is not ready to hear and say: {health}\n"
        f"--- its last lines ---\n{_last_lines(stack)}"
    )


def _last_lines(stack: StackInfo) -> str:
    try:
        return "\n".join(stack.logs("speech").splitlines()[-SPEECH_LOG_LINES:])
    except AssertionError as error:
        return str(error)


# --- the microphone -------------------------------------------------------------------------------


def trials_dir() -> Path:
    return Path(os.environ.get("DOMO_VOICE_TRIALS_DIR", "D:/domo-voice-models/trials/voices"))


def results_dir() -> Path:
    return Path(os.environ.get("DOMO_VOICE_TRIALS_RESULTS", "D:/domo-voice-models/trials/results"))


def _samples(path: Path) -> bytes:
    with wave.open(str(path), "rb") as sound:
        assert sound.getframerate() == RATE and sound.getnchannels() == 1
        return sound.readframes(sound.getnframes())


@dataclass(frozen=True)
class Microphone:
    """A file a fake microphone plays once: ``path``, and where each sentence lies in it
    (seconds from the start of the file)."""

    path: Path
    spoken: tuple[tuple[str, float, float], ...]

    @property
    def argument(self) -> str:
        return f"--use-file-for-fake-audio-capture={self.path.as_posix()}%noloop"


def microphone(name: str, pieces: Sequence[str | float]) -> Microphone:
    """A sound made of ``pieces``: a number is that many seconds of silence, a name a file of the
    recorded set (``alex.test0.fr.mic1.wav``)."""
    out = bytearray()
    spoken: list[tuple[str, float, float]] = []
    for piece in pieces:
        if isinstance(piece, str):
            start = len(out) / 2 / RATE
            out += _samples(trials_dir() / piece)
            spoken.append((piece, start, len(out) / 2 / RATE))
        else:
            out += b"\0\0" * int(piece * RATE)
    folder = trials_dir().parent / "stack"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.wav"
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(RATE)
        sound.writeframes(bytes(out))
    path.write_bytes(buffer.getvalue())
    return Microphone(path, tuple(spoken))


def browser_with(browser_type: BrowserType, mic: Microphone, *, autoplay: bool = False) -> Browser:
    """A browser whose microphone plays ``mic`` once it is opened, which grants the microphone
    without asking. ``autoplay``: sound may play with no gesture of the person."""
    args = [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        mic.argument,
    ]
    if autoplay:
        args.append("--autoplay-policy=no-user-gesture-required")
    # The headless shell by default (Playwright's own); ``DOMO_VOICE_TRIALS_CHANNEL=chromium`` for
    # the full browser, whose autoplay rules are a real one's, where this machine lets it run.
    channel = os.environ.get("DOMO_VOICE_TRIALS_CHANNEL")
    return browser_type.launch(args=args, channel=channel or None)


def text_of(path: Path) -> str:
    """The sentence a file of the recorded set says (its manifest)."""
    items = json.loads((trials_dir() / "manifest.json").read_text(encoding="utf-8"))["items"]
    return next(item["text"] for item in items if item["file"] == path.name)


# --- what the page did ----------------------------------------------------------------------------


@dataclass
class Probe:
    sent: list[tuple[float, str]] = field(default_factory=list[tuple[float, str]])
    received: list[tuple[float, str]] = field(default_factory=list[tuple[float, str]])
    loud: list[tuple[float, float]] = field(default_factory=list[tuple[float, float]])
    contexts: list[str] = field(default_factory=list[str])

    @classmethod
    def of(cls, page: Page) -> "Probe":
        data = page.evaluate("() => window.__voiceProbe")
        return cls(
            sent=[(a, b) for a, b in data["sent"]],
            received=[(a, b) for a, b in data["received"]],
            loud=[(a, b) for a, b in data["loud"]],
            contexts=list(data["contexts"]),
        )

    def sent_types(self, kind: str) -> list[float]:
        return [at for at, frame in self.sent if _type(frame) == kind]

    def received_types(self, kind: str) -> list[tuple[float, dict[str, Any]]]:
        found: list[tuple[float, dict[str, Any]]] = []
        for at, frame in self.received:
            if not frame.startswith("binary:") and _type(frame) == kind:
                found.append((at, json.loads(frame)))
        return found

    def first_sound_after(self, at: float) -> float | None:
        """When the page's speakers first played something after ``at``."""
        return next((t for t, _ in self.loud if t > at), None)

    def first_binary_after(self, at: float) -> float | None:
        return next((t for t, f in self.received if t > at and f.startswith("binary:")), None)


def _type(frame: str) -> str:
    match = re.search(r'"type":\s*"([a-z_]+)"', frame)
    return match.group(1) if match else ""


def install_probe(page: Page) -> None:
    page.add_init_script(path=str(PROBE_SCRIPT))


def get_json(url: str) -> Any:
    return httpx.get(url, verify=False, timeout=10).json()
