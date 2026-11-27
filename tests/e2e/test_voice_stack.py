"""The voice tried through the whole stack, in a driven browser (Task 29, second half).

The stack runs with the real speech service (its weights in a folder of this machine,
``compose.voice.yaml``) and the fake model (``compose.test.yaml``): the thinking model is the
scripted one, so what is measured is everything but a real model's thinking. Each browser has a
fake microphone that plays a file built from the recorded set (``voice_support.microphone``) once,
from the moment the page opens it; ``voice_page_probe.js`` records, on the page's clock, the
socket's frames and when the speakers really played something.

What the trials measure is written beside the recorded set (``DOMO_VOICE_TRIALS_RESULTS``,
``stack-<device>.json``) and copied by hand into
``docs/superpowers/followups/2026-11-24-voice-trials.md``.

Opt-in (heavy: a few gigabytes of models, a graphics card when there is one):
``DOMO_SPEECH_TEST_MODELS=1``, ``DOMO_VOICE_MODELS_DIR`` (the folder that holds the weights), the
recorded set built; ``DOMO_VOICE_TRIALS_DEVICE`` is ``gpu`` (default) or ``cpu``.
"""

import contextlib
import json
import os
import re
import statistics
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from domo_core.llm.speech.home_key import HOME_SPEECH_ID
from playwright.sync_api import Browser, BrowserType, Locator, Page, expect

from tests.stack.stack_support import StackInfo

from .support import (
    EXPECT_TIMEOUT_MS,
    Member,
    database_text,
    finish_the_first_launch,
    invitation_link,
    join_with_invitation,
    judged,
    log_in,
    reply_text,
    send_and_wait,
    tool_message,
)
from .voice_support import (
    PAGE_SILENCE_S,
    Microphone,
    Probe,
    browser_with,
    device,
    install_probe,
    microphone,
    results_dir,
    speech_health,
    speech_models,
    trials_dir,
    voice_stack,
)

pytestmark = [
    pytest.mark.stack,
    pytest.mark.speech_models,
    pytest.mark.skipif(
        os.environ.get("DOMO_SPEECH_TEST_MODELS") != "1"
        or "DOMO_VOICE_MODELS_DIR" not in os.environ
        or not (trials_dir() / "manifest.json").is_file(),
        reason="real speech models are opt-in (DOMO_SPEECH_TEST_MODELS=1, DOMO_VOICE_MODELS_DIR, "
        "the recorded set built)",
    ),
]

DANA = Member("Dana", "adult", "dana", "dana-test-passphrase")
RUNS = int(os.environ.get("DOMO_VOICE_TRIALS_RUNS", "5"))
# Long enough for the page to load its detector before the first sentence.
LEAD_S = 4.0
# How long a heard sentence may take to come back as sound, on a slow machine.
ANSWER_TIMEOUT_S = 60.0
# The silence between two sentences that should overlap his answer to the first.
OVER_HIM_GAP_S = float(os.environ.get("DOMO_VOICE_TRIALS_OVER_HIM_GAP", "6.5"))
# How long the core may take to read the speech service's whole list after the first launch.
CORE_LISTS_S = 120.0
FOUND: dict[str, Any] = {"device": device()}


@pytest.fixture(scope="module")
def stack() -> Iterator[StackInfo]:
    with voice_stack() as info:
        FOUND["models"] = [
            {key: model.get(key) for key in ("id", "does", "at_once", "measured")}
            for model in speech_models(info)
        ]
        FOUND["memory_idle"] = memory(info)
        yield info
    results_dir().mkdir(parents=True, exist_ok=True)
    (results_dir() / f"stack-{device()}.json").write_text(
        json.dumps(FOUND, indent=1), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def ready(stack: StackInfo, browser: Browser, browser_context_args: dict[str, Any]) -> StackInfo:
    finish_the_first_launch(stack, browser, browser_context_args)
    the_core_lists_what_the_service_does(stack)
    return stack


def the_core_lists_what_the_service_does(stack: StackInfo) -> None:
    """Wait until the core's list of the house's speech key (read once the owner exists) holds
    every model the service lists: before, the voice turned on would find hearing missing. A
    core that never reads the whole list fails here, by name, not as "no answer"."""
    served = {model["id"] for model in speech_models(stack)}
    query = (
        "SELECT string_agg(model_id, ',' ORDER BY model_id) FROM provider_models "
        f"WHERE provider_id = '{HOME_SPEECH_ID}'"
    )
    listed: set[str] = set()
    deadline = time.monotonic() + CORE_LISTS_S
    while time.monotonic() < deadline:
        listed = set(filter(None, database_text(stack, query).split(",")))
        if served <= listed:
            return
        time.sleep(2)
    # Whether the key itself is there tells a key never recorded (or dropped) from a list unread.
    keys = database_text(stack, f"SELECT count(*) FROM providers WHERE id = '{HOME_SPEECH_ID}'")
    raise AssertionError(
        f"the core lists {sorted(listed)} of the service's {sorted(served)} "
        f"(home speech key recorded: {keys})"
    )


def voice_page(
    browser_type: BrowserType,
    context_args: dict[str, Any],
    stack: StackInfo,
    mic: Microphone,
    *,
    username: str = "owner",
    password: str = "a-long-test-passphrase",
) -> tuple[Browser, Page]:
    """A browser of its own (its microphone plays ``mic``), signed in, on the chat page, the
    probe in place. The microphone is granted to the site as a browser keeps it once the person
    allowed it: ``--use-fake-ui-for-media-stream`` alone accepts each request but leaves the
    permission at ``prompt``, so a reloaded page would never listen by itself."""
    browser = browser_with(browser_type, mic)
    page = browser.new_context(**context_args, permissions=["microphone"]).new_page()
    install_probe(page)
    log_in(page, stack, username=username, password=password)
    return browser, page


def finished_replies(page: Page) -> Locator:
    """The assistant's bubbles (``assistant_replies``) whose words are all there: not the empty one
    shown while a reply is still being written elsewhere (``data-writing``, the voice's turns),
    nor one still streamed (``aria-busy``)."""
    return page.locator('.message.assistant:not([data-writing]):not([aria-busy="true"])')


def wait_for(page: Page, condition: str, timeout_s: float = ANSWER_TIMEOUT_S) -> None:
    # A function, never a string the page would evaluate: the page's policy forbids eval.
    page.wait_for_function(f"() => ({condition})", timeout=timeout_s * 1000, polling=100)


SAID_AFTER_END = (
    "(() => { const p = window.__voiceProbe; const ends = p.sent.filter(f => "
    "f[1].includes('speech_end')); if (ends.length < {n}) return false; const end = ends[{n}-1][0];"
    " return p.loud.some(l => l[0] > end); })()"
)


def answered(page: Page, n: int, timeout_s: float = ANSWER_TIMEOUT_S) -> None:
    """Wait until the page played sound after its ``n``-th end of speech; else fail with what the
    page did (its voice's state, the socket's frames by type, whether its audio ran)."""
    try:
        wait_for(page, SAID_AFTER_END.replace("{n}", str(n)), timeout_s)
    except Exception as error:
        done = what_the_page_did(page)
        raise AssertionError(f"no answer heard ({type(error).__name__}): {done}") from error


def what_the_page_did(page: Page) -> dict[str, Any]:
    probe = Probe.of(page)
    kinds: dict[str, int] = {}
    for _, frame in probe.received:
        kind = "binary" if frame.startswith("binary:") else frame[:40]
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "voice": page.evaluate("() => window.DomoVoice ? window.DomoVoice.state() : null"),
        "notice": page.locator('[data-voice="notice"]').inner_text(),
        "line": page.locator('[data-voice="state"]').inner_text(),
        "sent": sorted({frame[:40] for _, frame in probe.sent if not frame.startswith("binary:")}),
        "sound_frames_sent": sum(frame.startswith("binary:") for _, frame in probe.sent),
        "received": kinds,
        "loud": len(probe.loud),
        "contexts": probe.contexts,
    }


def timing(probe: Probe, n: int) -> dict[str, float | None]:
    """For the ``n``-th moment of speech (from 1): its end as the page sent it, then the first
    sound frame received and the first sound played, in seconds from the END OF THE SPOKEN
    SENTENCE (the page sends its end after ``PAGE_SILENCE_S`` of silence)."""
    end = probe.sent_types("speech_end")[n - 1]
    spoken_end = end - PAGE_SILENCE_S * 1000
    frame = probe.first_binary_after(end)
    sound = probe.first_sound_after(end)
    return {
        "page_sent_end_s": round(PAGE_SILENCE_S, 3),
        "first_frame_s": None if frame is None else round((frame - spoken_end) / 1000, 3),
        "first_sound_s": None if sound is None else round((sound - spoken_end) / 1000, 3),
    }


def memory(stack: StackInfo) -> dict[str, Any]:
    """The speech container's memory (``docker stats``) and the graphics card's (its whole use:
    Windows gives no figure per process)."""
    container = stack.container_id("speech")
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}|{{.CPUPerc}}", container],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    card = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return {"container": stats, "card_used_mib": card, "at": time.time()}


def summary(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "worst": round(max(values), 3),
        "best": round(min(values), 3),
    }


# --- (1) and (7): turned on once, it answers aloud; after a reload it listens by itself ---------


def test_turned_on_once_he_answers_aloud_and_listens_again_after_a_reload(
    ready: StackInfo, browser_type: BrowserType, browser_context_args: dict[str, Any]
) -> None:
    mic = microphone("one-sentence", [LEAD_S, "alex.test0.fr.mic1.wav", 30.0])
    browser, page = voice_page(browser_type, browser_context_args, ready, mic)
    try:
        before = finished_replies(page).count()
        page.click('[data-voice="toggle"]')
        answered(page, 1)
        probe = Probe.of(page)
        # (7) the socket passed the edge: the page is the edge's https address.
        assert page.url.startswith(ready.base_url)
        assert probe.received_types("say"), "no sound was sent to the page"
        expect(finished_replies(page)).to_have_count(before + 1, timeout=EXPECT_TIMEOUT_MS)
        heard = reply_text(finished_replies(page).nth(before)).inner_text()
        FOUND["first_reply"] = {"text": heard, **timing(probe, 1), "contexts": probe.contexts}
        FOUND["memory_after_one"] = memory(ready)
        # The first turn of the page (no key pressed): the sentence came back through the model.
        assert re.search(r"lumi[eè]re", heard, re.IGNORECASE), heard
        # Reload: no gesture, the page listens by itself, and hears again (the file starts again
        # when the page opens the microphone again).
        reloads: list[dict[str, Any]] = []
        for _ in range(RUNS):
            page.reload()
            wait_for(page, "window.DomoVoice && window.DomoVoice.state().state === 'listening'", 30)
            try:
                answered(page, 1)
            except Exception:
                probe = Probe.of(page)
                reloads.append(
                    {"answered": False, "contexts": probe.contexts, "sent": len(probe.sent)}
                )
                continue
            probe = Probe.of(page)
            reloads.append({"answered": True, **timing(probe, 1), "contexts": probe.contexts})
        FOUND["after_reload"] = reloads
        assert all(item["answered"] for item in reloads), reloads
        sounds = [float(item["first_sound_s"]) for item in reloads if item["first_sound_s"]]
        FOUND["end_of_speech_to_first_sound"] = summary(sounds)
    finally:
        browser.close()


# --- (4): speaking while he speaks --------------------------------------------------------------


def test_speaking_over_him_lowers_his_voice_at_once(
    ready: StackInfo, browser_type: BrowserType, browser_context_args: dict[str, Any]
) -> None:
    # A long sentence (a long reply to say), then, while he says it, another one: his voice
    # begins some 4.5 to 6 s after the end of the first (the stack's figure on this machine), so
    # the second begins OVER_HIM_GAP_S after it.
    mic = microphone(
        "over-him",
        [LEAD_S, "alex.learn1.fr.mic1.wav", OVER_HIM_GAP_S, "alex.learn0.fr.mic1.wav", 30.0],
    )
    browser, page = voice_page(browser_type, browser_context_args, ready, mic)
    try:
        page.click('[data-voice="toggle"]')
        answered(page, 1)
        # Speak again once he speaks: the microphone file goes on; wait for the second end.
        wait_for(
            page,
            "window.__voiceProbe.sent.filter(f => f[1].includes('speech_end')).length >= 1",
            60,
        )
        page.wait_for_timeout(20_000)
        probe = Probe.of(page)
        FOUND["over_him"] = {
            "speech_starts": [round(t) for t in probe.sent_types("speech_start")],
            "speech_ends": [round(t) for t in probe.sent_types("speech_end")],
            "duck": [round(t) for t, _ in probe.received_types("duck")],
            "stop": [round(t) for t, _ in probe.received_types("stop")],
            "resume": [round(t) for t, _ in probe.received_types("resume")],
            "played": [f for _, f in probe.sent if "played" in f],
            "loud_first_last": [probe.loud[0], probe.loud[-1]] if probe.loud else [],
            # The loudest peak of each quarter second the speakers played: whether his voice was
            # still heard when the second sentence began, and how low it went then.
            "loud_by_quarter": loud_by_quarter(probe),
        }
    finally:
        browser.close()


def loud_by_quarter(probe: Probe) -> list[tuple[int, float]]:
    quarters: dict[int, float] = {}
    for at, peak in probe.loud:
        quarter = int(at // 250) * 250
        quarters[quarter] = max(quarters.get(quarter, 0.0), peak)
    return sorted(quarters.items())


# --- (c): two people at once ----------------------------------------------------------------------


def test_two_people_at_once_each_get_their_own_answer(
    ready: StackInfo,
    browser: Browser,
    browser_type: BrowserType,
    browser_context_args: dict[str, Any],
) -> None:
    if not can_log_in(ready, browser, browser_context_args, DANA):
        # A stack attached again (DOMO_VOICE_STACK_ATTACH) may already have her.
        owner = browser.new_context(**browser_context_args).new_page()
        log_in(owner, ready)
        invitation = {"display_name": DANA.display_name, "role": DANA.role}
        reply = send_and_wait(owner, judged("do", tool_message("household_invite", invitation)))
        link = invitation_link(reply, ready)
        joining = browser.new_context(**browser_context_args).new_page()
        join_with_invitation(joining, link, DANA)
        owner.context.close()
        joining.context.close()

    runs: list[dict[str, Any]] = []
    peaks: list[dict[str, Any]] = []
    for run in range(RUNS):
        alex_mic = microphone(f"two-alex-{run}", [LEAD_S, "alex.test0.fr.mic1.wav", 40.0])
        dana_mic = microphone(f"two-dana-{run}", [LEAD_S, "dana.test1.fr.mic2.wav", 40.0])
        first, alex = voice_page(browser_type, browser_context_args, ready, alex_mic)
        second, dana = voice_page(
            browser_type,
            browser_context_args,
            ready,
            dana_mic,
            username=DANA.username,
            password=DANA.password,
        )
        sampling = Sampler(ready)
        try:
            sampling.start()
            before = {
                "alex": finished_replies(alex).count(),
                "dana": finished_replies(dana).count(),
            }
            # As close together as two clicks of one driver can be.
            alex.click('[data-voice="toggle"]')
            dana.click('[data-voice="toggle"]')
            failures: list[str] = []
            for who, page in (("alex", alex), ("dana", dana)):
                try:
                    answered(page, 1)
                except Exception as error:
                    failures.append(f"{who}: {type(error).__name__}")
            entry: dict[str, Any] = {"failures": failures}
            for who, page in (("alex", alex), ("dana", dana)):
                probe = Probe.of(page)
                if probe.sent_types("speech_end"):
                    entry[who] = timing(probe, 1)
                entry[f"{who}_failed"] = [f for _, f in probe.received_types("failed")]
                entry[f"{who}_not_taken"] = [f for _, f in probe.received_types("not_taken")]
                replies = finished_replies(page)
                # The sound may come before the written reply shows in the page, and the reply
                # shows as being written (an empty bubble) until its sound has all been sent.
                with contextlib.suppress(AssertionError):
                    expect(replies).to_have_count(before[who] + 1, timeout=EXPECT_TIMEOUT_MS)
                entry[f"{who}_reply"] = (
                    reply_text(replies.last).inner_text() if replies.count() > before[who] else None
                )
            runs.append(entry)
        finally:
            peaks.append(sampling.stop())
            first.close()
            second.close()
    FOUND["two_at_once"] = runs
    FOUND["memory_two_at_once"] = peaks
    for entry in runs:
        assert not entry["failures"], entry
        assert entry["alex_reply"] and "lumi" in entry["alex_reply"].lower(), entry
        assert entry["dana_reply"] and "pain" in entry["dana_reply"].lower(), entry


def can_log_in(
    stack: StackInfo, browser: Browser, context_args: dict[str, Any], member: Member
) -> bool:
    page = browser.new_context(**context_args).new_page()
    try:
        log_in(page, stack, username=member.username, password=member.password)
        return True
    except Exception:
        return False
    finally:
        page.context.close()


class Sampler:
    """The speech container's memory and the card's, read every half second while two speak."""

    def __init__(self, stack: StackInfo) -> None:
        self._stack = stack
        self._stop = threading.Event()
        self._samples: list[dict[str, Any]] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._samples.append(memory(self._stack))
            self._stop.wait(0.5)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=30)
        return {"samples": self._samples}


# --- (8): nothing heard is kept -------------------------------------------------------------------


MARKER_WORDS = ("lumière", "lumiere", "salon", "fromage", "garage")


def test_nothing_heard_is_written_to_disk_or_logs(ready: StackInfo) -> None:
    """After the scenarios above: no sound file in the speech service or the core, and no word of
    what was said in their logs (the discussion keeps the words, which is the point of it)."""
    found_files: dict[str, str] = {}
    for service in ("speech", "core", "gatekeeper"):
        listing = ready.exec(
            service,
            [
                "sh",
                "-c",
                "find / -xdev \\( -path /proc -o -path /sys -o -path /models \\) -prune -o "
                "-type f \\( -iname '*.wav' -o -iname '*.pcm' -o -iname '*.webm' -o -iname "
                "'*.ogg' -o -iname '*.opus' -o -iname '*.mp3' -o -iname '*.flac' \\) "
                "-newer /etc/hostname -print 2>/dev/null; find /tmp -type f 2>/dev/null | head",
            ],
        )
        found_files[service] = listing.stdout.strip()
    logs = {service: ready.logs(service) for service in ("speech", "core", "gatekeeper", "edge")}
    in_logs = {
        service: [word for word in MARKER_WORDS if word in text.lower()]
        for service, text in logs.items()
    }
    models_dir = os.environ["DOMO_VOICE_MODELS_DIR"]
    new_sound = [
        str(path) for path in Path(models_dir).rglob("*") if path.suffix.lower() in (".wav", ".pcm")
    ]
    FOUND["nothing_kept"] = {
        "files": found_files,
        "words_in_logs": in_logs,
        "sound_in_models": new_sound,
        "speech_health": speech_health(ready),
    }
    assert all(not words for words in in_logs.values()), in_logs
    assert not new_sound, new_sound
    assert all(not listing for listing in found_files.values()), found_files
