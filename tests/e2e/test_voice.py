"""The voice tried with the recorded set and real models, no stack (Task 29, first half).

The recorded set is synthetic (``scripts/voice_bench/make_voices.py``, built outside the
repository): Alex, Dana, Paul, Sam and Tom on two microphones, a cold Alex, Paul imitating Alex,
0.5 s utterances, 40 s without a pause, Dana and Sam talking to each other. Every sound goes
through the real speech service (a plain local process, see ``voice.env``) and the core's own
``read_voice`` and ``learn`` on a throwaway PostgreSQL (the core tests' image; no stack). What is
measured is written beside the set (``DOMO_VOICE_TRIALS_RESULTS``) and copied by hand into
``docs/superpowers/followups/2026-11-24-voice-trials.md``.

Opt-in like every ``speech_models`` test: ``DOMO_SPEECH_TEST_MODELS=1``, the set built, the
service ready at ``DOMO_VOICE_TRIALS_URL``. Skipped otherwise (the build machine has no models).
"""

import asyncio
import json
import os
import statistics
import uuid
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.heard_words import heard_back_said, misheard_at_most

ENV_FILE = Path(__file__).with_name("voice.env")


def _settings() -> dict[str, str]:
    found: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            found[name.strip()] = value.strip()
    return {name: os.environ.get(name, value) for name, value in found.items()}


SETTINGS = _settings()
VOICES = Path(SETTINGS["DOMO_VOICE_TRIALS_DIR"])
RESULTS = Path(SETTINGS["DOMO_VOICE_TRIALS_RESULTS"])
LABEL = SETTINGS["DOMO_VOICE_TRIALS_LABEL"]
PEOPLE = (
    ("Alex", "owner"),
    ("Dana", "adult"),
    ("Paul", "child"),
    ("Sam", "adult"),
    ("Tom", "child"),
)
MICS = ("mic1", "mic2")
T0 = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
# Learning: two utterances on a first visit, two more on a second one two hours later.
LEARN_AT = (
    T0,
    T0 + timedelta(seconds=30),
    T0 + timedelta(hours=2),
    T0 + timedelta(hours=2, seconds=30),
)
READ_AT = T0 + timedelta(days=1)
POSTGRES_IMAGE = "pgvector/pgvector:pg17"

pytestmark = [
    pytest.mark.speech_models,
    pytest.mark.skipif(
        os.environ.get("DOMO_SPEECH_TEST_MODELS") != "1"
        or not (VOICES / "manifest.json").is_file(),
        reason="real speech models are opt-in (DOMO_SPEECH_TEST_MODELS=1, the recorded set built)",
    ),
]


@dataclass(frozen=True)
class Sound:
    file: str
    who: str
    kind: str
    mic: str
    language: str
    text: str
    seconds: float
    segments: tuple[tuple[float, float, str], ...] = ()


def _sounds() -> list[Sound]:
    items = json.loads((VOICES / "manifest.json").read_text(encoding="utf-8"))["items"]
    return [
        Sound(**{**item, "segments": tuple(tuple(s) for s in item["segments"])}) for item in items
    ]


@dataclass
class Row:
    """One sound read against one person's prints."""

    model: str
    learnt_on: str
    file: str
    who: str
    kind: str
    mic: str
    person: str
    weighs: str
    degree: float
    core_degree: float
    best_other: str | None
    other_raw: float
    short: bool
    via_passing: bool
    resembles_nobody: bool
    cosine: float | None
    window: tuple[float, float] | None = None
    # Who the product takes the speaker for at this person's own signed-in page
    # (``channels.voice.speaker.voice_speaker``): "page" (that person, sure or unsure), "nobody"
    # (a voice like nobody known: the whose-voice question X21 is asked, then the stranger's
    # path if the quick model does not say "theirs") or "other" (another known person).
    taken_for: str = "page"


@dataclass
class Trial:
    models: list[dict[str, Any]]
    learning: dict[str, list[str]] = field(default_factory=dict)
    # Per model and microphone, per person: the cosines between their four learn sentences one
    # after the other (FR-FR, FR-EN, EN-EN), what decides whether a candidate goes on.
    learn_cosines: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    rows: list[Row] = field(default_factory=list)
    heard: list[dict[str, Any]] = field(default_factory=list)


# --- The service ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=SETTINGS["DOMO_VOICE_TRIALS_URL"], timeout=600) as client:
        health = client.get("/health")
        assert health.status_code == 200, f"the speech service is not ready: {health.text}"
        yield client


def _pcm(path: Path) -> bytes:
    return path.read_bytes()


def _print(client: httpx.Client, wav: bytes, model: str) -> dict[str, Any]:
    answer = client.post(
        "/v1/voice/print", files={"file": ("u.wav", wav, "audio/wav")}, data={"model": model}
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def _cut(wav: bytes, start: float, end: float) -> bytes:
    """A window of a 16 kHz mono WAV file, as a WAV file."""
    import io
    import wave

    with wave.open(io.BytesIO(wav), "rb") as source:
        frames = source.readframes(source.getnframes())
    piece = frames[int(start * 16_000) * 2 : int(end * 16_000) * 2]
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(piece)
    return out.getvalue()


# --- The database ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    from domo_core.migrate import upgrade_to_head
    from testcontainers.community.postgres import PostgresContainer

    database = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg").with_command(
        "postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    )
    with database as container:
        url = container.get_connection_url()
        upgrade_to_head(url)
        yield url


def _in_a_thread(work: Any) -> Any:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, work).result()


async def _read_all(
    url: str,
    model: str,
    learnt_on: str,
    thresholds: Any,
    prints: dict[str, Any],
    sounds: list[Sound],
    windows: list[tuple[Sound, tuple[float, float], Any]],
) -> tuple[dict[str, list[str]], list[Row]]:
    """Everyone learnt from their four ``learn`` sentences on ``learnt_on``, then every other
    sound read against everyone; all in one transaction, rolled back."""
    from domo_core.accounts.models import Person
    from domo_core.channels.voice.models import VoicePrint, VoiceRef
    from domo_core.channels.voice.prints import learn, read_voice, similarity
    from domo_core.channels.voice.speaker import voice_speaker
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine(url)
    learning: dict[str, list[str]] = {}
    rows: list[Row] = []
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
            people: dict[str, uuid.UUID] = {}
            persons: dict[str, Person] = {}
            for name, role in PEOPLE:
                person = Person(display_name=name, role=role)
                session.add(person)
                await session.flush()
                people[name] = person.id
                persons[name] = person
            for name, person_id in people.items():
                mine = [
                    s for s in sounds if s.who == name and s.kind == "learn" and s.mic == learnt_on
                ]
                mine.sort(key=lambda s: s.file)
                learning[name] = [
                    await learn(
                        session,
                        person_id=person_id,
                        microphone=learnt_on,
                        print_=prints[sound.file],
                        thresholds=thresholds,
                        sure_otherwise=True,
                        visit_at=at,
                        now=at,
                    )
                    for sound, at in zip(mine, LEARN_AT, strict=True)
                ]
            bases: dict[str, list[float]] = {}
            for name, person_id in people.items():
                base = await session.scalar(
                    select(VoicePrint)
                    .join(VoiceRef, VoicePrint.ref_id == VoiceRef.id)
                    .where(VoiceRef.person_id == person_id, VoicePrint.kind == "base")
                )
                if base is not None:
                    bases[name] = list(base.vector)
            names = {person_id: name for name, person_id in people.items()}
            targets: list[tuple[Sound, tuple[float, float] | None, Any]] = [
                (s, None, prints[s.file]) for s in sounds if s.kind != "learn"
            ] + [(s, w, p) for s, w, p in windows]
            for sound, window, print_ in targets:
                for name, person_id in people.items():
                    reading = await read_voice(
                        session,
                        person_id=person_id,
                        microphone=sound.mic,
                        print_=print_,
                        thresholds=thresholds,
                        now=READ_AT,
                    )
                    base = bases.get(name)
                    speaker = voice_speaker(
                        page_person=persons[name],
                        reading=reading,
                        print_=print_,
                        microphone=sound.mic,
                        signed_in_at=READ_AT,
                        proofs=(),
                        now=READ_AT,
                    )
                    taken_for = (
                        "nobody"
                        if speaker.person_id is None
                        else "page"
                        if speaker.person_id == person_id
                        else "other"
                    )
                    rows.append(
                        Row(
                            model=model,
                            learnt_on=learnt_on,
                            file=sound.file,
                            who=sound.who,
                            kind=sound.kind,
                            mic=sound.mic,
                            person=name,
                            weighs=reading.weighs,
                            degree=round(reading.degree, 3),
                            core_degree=round(reading.core_degree, 3),
                            best_other=names.get(reading.best_other)
                            if reading.best_other
                            else None,
                            other_raw=round(reading.other_raw, 3),
                            short=reading.short,
                            via_passing=reading.via_passing,
                            resembles_nobody=reading.resembles_nobody,
                            cosine=round(similarity(print_.vector, base), 3) if base else None,
                            window=window,
                            taken_for=taken_for,
                        )
                    )
            await session.close()
            await outer.rollback()
    finally:
        await engine.dispose()
    return learning, rows


def _consecutive(vectors: list[tuple[float, ...]]) -> list[float]:
    from domo_core.channels.voice.prints import similarity

    return [round(similarity(a, b), 3) for a, b in zip(vectors, vectors[1:], strict=False)]


# --- The trial: run once, read by every test ------------------------------------------------------


@pytest.fixture(scope="module")
def trial(service: httpx.Client, database_url: str) -> Iterator[Trial]:
    from domo_core.llm.speech.client import Print, PrintThresholds

    sounds = _sounds()
    models = service.get("/v1/models").json()["data"]
    found = Trial(models=models)
    for entry in [m for m in models if m["does"] == "voice_print"]:
        model = entry["id"]
        bounds = entry["thresholds"]
        thresholds = PrintThresholds(
            print_low=bounds["print_low"],
            same_voice=bounds["same_voice"],
            print_high=bounds["print_high"],
        )

        def as_print(answer: dict[str, Any], model: str = model) -> Print:
            return Print(
                vector=tuple(answer["vector"]),
                model=answer.get("model", model),
                seconds=answer["seconds"],
                pitch_hz=answer.get("pitch_hz"),
                pitch_range_hz=answer.get("pitch_range_hz"),
                rate=answer.get("rate"),
            )

        prints = {s.file: as_print(_print(service, _pcm(VOICES / s.file), model)) for s in sounds}
        for mic in MICS:
            found.learn_cosines[f"{model}@{mic}"] = {
                name: _consecutive(
                    [
                        prints[s.file].vector
                        for s in sorted(sounds, key=lambda s: s.file)
                        if s.who == name and s.kind == "learn" and s.mic == mic
                    ]
                )
                for name, _ in PEOPLE
            }
        windows: list[tuple[Sound, tuple[float, float], Any]] = []
        for room in (s for s in sounds if s.kind == "room"):
            wav = _pcm(VOICES / room.file)
            start = 0.0
            while start + 3.0 <= room.seconds:
                piece = _print(service, _cut(wav, start, start + 3.0), model)
                windows.append((room, (start, start + 3.0), as_print(piece)))
                start += 1.5
        for learnt_on in MICS:
            learning, rows = _in_a_thread(
                _read_all(database_url, model, learnt_on, thresholds, prints, sounds, windows)
            )
            found.learning[f"{model}@{learnt_on}"] = [
                f"{name}: {' '.join(steps)}" for name, steps in learning.items()
            ]
            found.rows += rows
    for entry in [m for m in models if m["does"] == "hear"]:
        for sound in sounds:
            if sound.kind == "learn" and sound.mic == "mic2":
                continue
            answer = service.post(
                "/v1/audio/transcriptions",
                files={"file": ("u.wav", _pcm(VOICES / sound.file), "audio/wav")},
                data={"model": entry["id"]},
            )
            assert answer.status_code == 200, answer.text
            heard = answer.json()
            found.heard.append(
                {
                    "model": entry["id"],
                    "file": sound.file,
                    "kind": sound.kind,
                    "language": sound.language,
                    "detected": heard["language"],
                    "heard_back": round(
                        heard_back_said(sound.text, heard["text"], sound.language), 2
                    ),
                    "heard": heard["text"],
                    "elapsed_s": round(answer.elapsed.total_seconds(), 2),
                    "seconds": round(sound.seconds, 2),
                }
            )
    yield found
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"voice-{LABEL}.json").write_text(
        json.dumps(
            {
                "models": found.models,
                "learning": found.learning,
                "learn_cosines": found.learn_cosines,
                "rows": [asdict(r) for r in found.rows],
                "heard": found.heard,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (RESULTS / f"voice-{LABEL}.md").write_text(tables(found), encoding="utf-8")


# --- Tables for the trials file -------------------------------------------------------------------


def _span(values: list[float]) -> str:
    if not values:
        return "-"
    low, high = min(values), max(values)
    return f"{low:.2f}" if low == high else f"{low:.2f}-{high:.2f}"


def _tally(rows: list[Row]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.weighs[0].upper() if row.weighs != "unknown" else "u"] += 1
    return "".join(f"{n}{k}" for k, n in sorted(counts.items()))


def tables(found: Trial) -> str:
    """Per print model and microphone learnt on: each group of sounds against each person
    (degree span, then the readings: F for, A against, u unsure), and the cosines."""
    out: list[str] = []
    groups = [
        ("test", "same"),
        ("test", "cross"),
        ("cold", "same"),
        ("cold", "cross"),
        ("imitation", "same"),
        ("imitation", "cross"),
        ("short", "same"),
        ("short", "cross"),
        ("long", "same"),
        ("long", "cross"),
    ]
    by: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in found.rows:
        by[(row.model, row.learnt_on)].append(row)
    for (model, learnt_on), rows in sorted(by.items()):
        out.append(f"\n#### {model}, learnt on {learnt_on}\n")
        out.append("| sounds | " + " | ".join(name for name, _ in PEOPLE) + " |")
        out.append("|---|" + "---|" * len(PEOPLE))
        for kind, mic_kind in groups:
            for who in sorted({r.who for r in rows if r.kind == kind and r.window is None}):
                chosen = [
                    r
                    for r in rows
                    if r.kind == kind
                    and r.who == who
                    and r.window is None
                    and (r.mic == learnt_on) == (mic_kind == "same")
                ]
                if not chosen:
                    continue
                cells = []
                for name, _ in PEOPLE:
                    mine = [r for r in chosen if r.person == name]
                    cells.append(f"{_span([r.degree for r in mine])} {_tally(mine)}")
                out.append(f"| {who} {kind} ({mic_kind} mic) | " + " | ".join(cells) + " |")
        same = [
            r.cosine
            for r in rows
            if r.kind == "test"
            and r.who == r.person
            and r.mic == learnt_on
            and r.cosine is not None
        ]
        cross = [
            r.cosine
            for r in rows
            if r.kind == "test"
            and r.who == r.person
            and r.mic != learnt_on
            and r.cosine is not None
        ]
        other = [
            r.cosine
            for r in rows
            if r.kind == "test" and r.who != r.person and r.cosine is not None
        ]
        cold = [
            r.cosine
            for r in rows
            if r.kind == "cold" and r.person == "Alex" and r.cosine is not None
        ]
        mimic = [
            r.cosine
            for r in rows
            if r.kind == "imitation" and r.person == "Alex" and r.cosine is not None
        ]
        out.append("")
        for mic_kind in ("same", "cross"):
            colds = [
                r
                for r in rows
                if r.kind == "cold"
                and r.person == "Alex"
                and r.window is None
                and (r.mic == learnt_on) == (mic_kind == "same")
            ]
            if colds:
                counts = {
                    k: sum(r.taken_for == k for r in colds) for k in ("page", "nobody", "other")
                }
                out.append(
                    f"- cold Alex at his page, {mic_kind} mic: {counts['page']} Alex, "
                    f"{counts['nobody']} to be judged (whose-voice question), {counts['other']} "
                    f"another known person (n={len(colds)})"
                )
        for title, values in (
            ("genuine, same mic", same),
            ("genuine, cross mic", cross),
            ("another person", other),
            ("cold Alex vs Alex", cold),
            ("imitation vs Alex", mimic),
        ):
            if values:
                out.append(
                    f"- cosine to the base, {title}: min {min(values):.2f}, median "
                    f"{statistics.median(values):.2f}, max {max(values):.2f} (n={len(values)})"
                )
        room = [r for r in rows if r.window is not None and r.person in ("Dana", "Sam")]
        if room:
            out.append("\nRoom windows (3 s, every 1.5 s), read against Dana / Sam:\n")
            out.append("| window | truth | Dana | Sam |")
            out.append("|---|---|---|---|")
            segments = next(s for s in _sounds() if s.kind == "room").segments
            for window in sorted({r.window for r in room if r.window is not None}):
                truth = _truth(window, segments)
                cells = []
                for name in ("Dana", "Sam"):
                    r = next(r for r in room if r.window == window and r.person == name)
                    cells.append(f"{r.degree:.2f} {r.weighs}")
                out.append(
                    f"| {window[0]:.1f}-{window[1]:.1f} s | {truth} | " + " | ".join(cells) + " |"
                )
        out.append("\nLearning (what each of the four learn calls returned):\n")
        cosines = found.learn_cosines.get(f"{model}@{learnt_on}", {})
        for line in found.learning.get(f"{model}@{learnt_on}", []):
            name = line.split(":", 1)[0]
            steps = ", ".join(f"{c:.2f}" for c in cosines.get(name, []))
            out.append(f"- {line} (cosines one sentence to the next: {steps})")
    if found.heard:
        out.append("\n#### Hearing\n")
        out.append(
            "| model | sounds | right language | heard back >= 0.8 | mean heard back | RTF |"
        )
        out.append("|---|---|---|---|---|---|")
        by_heard: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in found.heard:
            mic = item["file"].rsplit(".", 2)[-2]
            by_heard[(item["model"], f"{item['kind']} {item['language']} {mic}")].append(item)
        for (model, group), items in sorted(by_heard.items()):
            right = sum(i["detected"] == i["language"] for i in items)
            good = sum(i["heard_back"] >= 0.8 for i in items)
            mean = statistics.mean(i["heard_back"] for i in items)
            rtf = statistics.mean(i["elapsed_s"] / i["seconds"] for i in items)
            out.append(
                f"| {model} | {group} | {right}/{len(items)} | {good}/{len(items)} "
                f"| {mean:.2f} | {rtf:.3f} |"
            )
    return "\n".join(out) + "\n"


def _truth(window: tuple[float, float], segments: tuple[tuple[float, float, str], ...]) -> str:
    """Who speaks in ``window`` (the segments are in seconds of the voice at 24 kHz: the same
    seconds after the microphone)."""
    spoken: dict[str, float] = defaultdict(float)
    for start, end, name in segments:
        spoken[name] += max(0.0, min(end, window[1]) - max(start, window[0]))
    present = [f"{name} {seconds:.1f}s" for name, seconds in spoken.items() if seconds > 0.2]
    return " + ".join(present) or "silence"


# --- What the spec expects ------------------------------------------------------------------------


def _rows(found: Trial, **where: Any) -> list[Row]:
    return [
        r
        for r in found.rows
        if r.window is None and all(getattr(r, key) == value for key, value in where.items())
    ]


def test_every_voice_is_learnt_on_each_microphone(trial: Trial) -> None:
    unlearnt = [line for lines in trial.learning.values() for line in lines if "learnt" not in line]
    assert unlearnt == []


def test_a_genuine_voice_on_the_microphone_it_was_learnt_on_weighs_for(trial: Trial) -> None:
    missed = [
        (r.model, r.learnt_on, r.file, r.weighs, r.degree)
        for r in _rows(trial, kind="test")
        if r.who == r.person and r.mic == r.learnt_on and r.weighs != "for"
    ]
    assert missed == []


def test_no_voice_weighs_for_somebody_else(trial: Trial) -> None:
    taken = [
        (r.model, r.learnt_on, r.file, r.person, r.degree)
        for r in trial.rows
        if r.window is None and r.kind != "room" and r.who != r.person and r.weighs == "for"
    ]
    assert taken == []


def test_a_voice_learnt_on_one_microphone_is_not_a_stranger_on_the_other(trial: Trial) -> None:
    lost = [
        (r.model, r.learnt_on, r.file, r.weighs, r.degree)
        for r in _rows(trial, kind="test")
        if r.who == r.person
        and r.mic != r.learnt_on
        and (r.weighs == "against" or r.resembles_nobody)
    ]
    assert lost == []


def test_the_cold_voice_is_unsure_and_still_alex(trial: Trial) -> None:
    """On the microphone Alex was learnt on: strict, a cold Alex is never against him nor a voice
    like nobody (still fails: trials defect 2, five readings on the home server)."""
    wrong = [
        (r.model, r.learnt_on, r.file, r.weighs, r.degree, r.best_other)
        for r in _rows(trial, kind="cold", person="Alex")
        if r.mic == r.learnt_on and (r.weighs == "against" or r.resembles_nobody)
    ]
    assert wrong == []


def test_the_cold_voice_on_another_microphone_is_alex_or_judged(trial: Trial) -> None:
    """On a microphone where Alex has no base print, the owner (2026-11-28): "Domo juge à chaque
    fois". A cold read like nobody known is not a failure of the reading: at his own signed-in
    page the product asks the whose-voice question (X21: the quick model, with this visit's last
    lines) before any stranger's path; the trial has no quick model, so it counts those readings
    as judged. A cold taken for another known person is a failure."""
    cold = [r for r in _rows(trial, kind="cold", person="Alex") if r.mic != r.learnt_on]
    judged = [r for r in cold if r.taken_for == "nobody"]
    other = [
        (r.model, r.learnt_on, r.file, r.weighs, r.degree, r.best_other)
        for r in cold
        if r.taken_for == "other"
    ]
    # Never a silent stranger by score alone: a speaker who is nobody, said to him at a signed-in
    # page, is first asked about (``session._theirs_after_all``), then the stranger's exchange.
    print(
        f"cold Alex on another microphone: {len(cold) - len(judged) - len(other)} Alex, "
        f"{len(judged)} judged, {len(other)} another known person (n={len(cold)})"
    )
    assert cold and other == []


def test_the_imitation_is_never_sure(trial: Trial) -> None:
    sure = [
        (r.model, r.learnt_on, r.file, r.degree)
        for r in _rows(trial, kind="imitation", person="Alex")
        if r.weighs == "for"
    ]
    assert sure == []


def test_a_short_utterance_is_never_against(trial: Trial) -> None:
    short = _rows(trial, kind="short")
    assert short and all(r.short for r in short)
    assert [r for r in short if r.weighs == "against" or r.resembles_nobody] == []


def test_forty_seconds_without_a_pause_are_alex_on_his_microphone(trial: Trial) -> None:
    long = [r for r in _rows(trial, kind="long", person="Alex") if r.mic == r.learnt_on]
    assert long and all(r.weighs == "for" for r in long)


def test_every_sentence_is_heard_back_in_its_language(trial: Trial) -> None:
    """Hours and numbers in figures count as the words said; each hearing model may mishear at
    most its own share of the sentences (``heard_words.misheard_at_most``)."""
    sentences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i in trial.heard:
        if i["kind"] in ("learn", "test", "cold", "imitation", "long"):
            sentences[i["model"]].append(i)
    over = {
        model: wrong
        for model, items in sentences.items()
        if len(
            wrong := [
                (i["file"], i["detected"], i["heard_back"], i["heard"])
                for i in items
                if i["detected"] != i["language"] or i["heard_back"] < 0.8
            ]
        )
        > misheard_at_most(model, len(items))
    }
    assert sentences and over == {}
