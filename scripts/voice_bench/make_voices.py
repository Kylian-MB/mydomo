"""Builds the recorded set of the voice trials, by synthesis, OUTSIDE the repository.

Nobody is recorded: every voice is one of the speech service's own voice styles (the imported
floor voice, its ``F*`` and ``M*`` styles), changed by plain signal processing. The people are
Alex (the owner), Dana, Paul, Sam and Tom (a child-like voice: a female style raised in pitch and
formants). Each sentence goes through TWO microphone chains:

* ``mic1`` ("laptop"): recorded at 48 kHz, band-pass 90-7000 Hz, faint white noise and mains hum
  (30 dB under the voice);
* ``mic2`` ("webcam"): recorded at 22.05 kHz, band-pass 300-3800 Hz with a tilt, a small room
  (an exponential reverb tail of about 0.25 s) and pink noise 18 dB under the voice.

Both end as what the page sends: 16 kHz, mono, 16-bit WAV.

Beside the people's sentences (four to learn from, four to read, French and English):

* ``cold``: Alex with a cold: the voice lowered by 1.5 semitones and slowed by the same factor
  (resampled), and a nasal filter (a resonance at 250 Hz, a dip at 1 kHz, the top rolled off);
* ``imitation``: Paul imitating Alex: Paul's sentences moved to Alex's median pitch and to Alex's
  length for the same sentence (resampled, then stretched back by overlap-add);
* ``short``: a 0.5 s "Oui." of Alex and of Dana;
* ``long``: about 40 s of Alex without a pause (the silences between sentences cut out);
* ``room``: Dana and Sam talking to each other (six turns, one overlap), through mic1 with a room.

Deterministic: the voice's random draw and every noise come from fixed seeds (a CRC of the file's
name), so a second run writes the same samples (to one least significant bit now and then: the
voice model's threaded sums on the CPU are not always in the same order). ``manifest.json``
beside the sounds says who speaks in each file, on which microphone, the sentence and its language.

Run it with the speech service's real engines installed (the extra ``models``), from the root of
the repository::

    D:/domo-voice-models/venv/Scripts/python.exe -m scripts.voice_bench.make_voices

``DOMO_VOICE_TRIALS_DIR`` (default ``D:/domo-voice-models/trials/voices``) is where the set goes;
``DOMO_SPEECH_MODEL_DIR`` (default ``D:/domo-voice-models/models``) where the weights are. No
sound is ever written inside the repository.
"""

import json
import os
import wave
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

Samples = npt.NDArray[np.float32]
Mic = Literal["mic1", "mic2"]

VOICE_RATE: Final = 24_000
OUT_RATE: Final = 16_000
MICS: Final[tuple[Mic, ...]] = ("mic1", "mic2")
TRIALS_DIR: Final = Path(
    os.environ.get("DOMO_VOICE_TRIALS_DIR", "D:/domo-voice-models/trials/voices")
)
MODEL_DIR: Final = Path(os.environ.get("DOMO_SPEECH_MODEL_DIR", "D:/domo-voice-models/models"))
SEED: Final = 2911
FLOOR_VOICE: Final = "supertonic-3"


@dataclass(frozen=True)
class Speaker:
    name: str
    style: str
    # Pitch (and formant) factor applied by resampling; the length is put back by stretching.
    raise_by: float = 1.0
    # The length after the pitch change, as a part of the original (a child speaks a bit faster).
    length: float = 1.0


PEOPLE: Final = (
    Speaker("Alex", "M1"),
    Speaker("Dana", "F1"),
    Speaker("Paul", "M4"),
    Speaker("Sam", "F3"),
    Speaker("Tom", "F2", raise_by=2 ** (4 / 12), length=0.92),
)

LEARN: Final = (
    ("fr", "Ce soir, je voudrais manger des pâtes avec un peu de fromage râpé."),
    ("fr", "Peux-tu me rappeler d'appeler le garage demain matin vers neuf heures ?"),
    ("en", "I think the weather will be much better tomorrow afternoon, after the rain."),
    ("en", "Please put some quiet music on in the kitchen while I am cooking dinner."),
)
TEST: Final = (
    ("fr", "Bonjour, peux-tu allumer la lumière du salon, s'il te plaît ?"),
    ("fr", "Il faut acheter du pain, du lait et trois pommes vertes au marché."),
    ("en", "Good morning, could you turn on the light in the living room, please?"),
    ("en", "What time does the train to the city leave on Sunday morning?"),
)
SHORT: Final = ("fr", "Oui.")
LONG: Final = (
    "Alors voilà, demain matin il faudra que tu me réveilles vers sept heures, "
    "parce que je dois passer à la boulangerie avant d'emmener les enfants à l'école. "
    "Ensuite je prendrai la voiture pour aller au bureau, mais il y aura sûrement des bouchons "
    "sur la grande route à cause des travaux qui durent depuis le mois dernier. "
    "À midi je mangerai avec des collègues, puis l'après-midi sera occupé par deux réunions "
    "assez longues avec des clients qui viennent de loin. "
    "Le soir, je voudrais que la maison soit chaude quand je rentre, que la lumière de "
    "l'entrée soit allumée et qu'une musique douce passe dans le salon. "
    "Après le dîner, rappelle-moi de payer la facture d'électricité et de répondre au message "
    "de mon frère au sujet des vacances d'été à la montagne."
)
ROOM: Final = (
    ("Dana", "Tu as vu le match hier soir à la télévision ?"),
    ("Sam", "Non, je travaillais tard, c'était bien au moins ?"),
    ("Dana", "Oui, très serré jusqu'à la dernière minute, tu aurais adoré."),
    ("Sam", "Dommage, je le regarderai en rediffusion ce week-end alors."),
    ("Dana", "Tu veux venir dîner samedi soir avec Tom et les autres ?"),
    ("Sam", "Avec plaisir, j'apporterai le dessert et une bonne bouteille."),
)


# --- Plain signal processing ----------------------------------------------------------------------


def resample(samples: Samples, rate_in: int, rate_out: int) -> Samples:
    """All of ``samples`` at another rate, pushed a chunk at a time (the resampler's work arrays
    grow with the chunk)."""
    from domo_speech.real.sound import Resampler

    resampler = Resampler(rate_in, rate_out)
    pieces = [resampler.push(samples[i : i + 8192]) for i in range(0, len(samples), 8192)]
    pieces.append(resampler.flush())
    return np.concatenate(pieces).astype(np.float32)


def shape(samples: Samples, rate: int, gain_db: npt.NDArray[np.float64]) -> Samples:
    """``samples`` filtered with no phase change by a gain curve (dB) given for every bin of a
    real FFT of the whole sound (``len(samples)`` points)."""
    spectrum = np.fft.rfft(samples)
    return np.fft.irfft(spectrum * 10 ** (gain_db / 20), n=len(samples)).astype(np.float32)


def freqs(samples: Samples, rate: int) -> npt.NDArray[np.float64]:
    return np.fft.rfftfreq(len(samples), 1 / rate)


def band_pass(
    samples: Samples, rate: int, low: float, high: float, tilt_db: float = 0.0
) -> Samples:
    """A smooth band-pass (12 dB per octave on each side) and a tilt across the band."""
    f = np.maximum(freqs(samples, rate), 1.0)
    gain = -12 * np.maximum(0, np.log2(low / f)) - 12 * np.maximum(0, np.log2(f / high))
    gain = gain + tilt_db * np.clip(np.log2(f / low) / np.log2(high / low), 0, 1)
    return shape(samples, rate, gain)


Curve = npt.NDArray[np.float64]


def bell(f: Curve, centre: float, width: float, db: float) -> Curve:
    return db * np.exp(-0.5 * ((f - centre) / width) ** 2)


def nasal(samples: Samples, rate: int) -> Samples:
    """A blocked nose: a resonance at 250 Hz, a dip at 1 kHz, the top rolled off."""
    f = freqs(samples, rate)
    gain = bell(f, 250, 80, 7.0) + bell(f, 1000, 250, -12.0) - 8 * np.clip((f - 3000) / 3000, 0, 1)
    return shape(samples, rate, gain)


def rms(samples: Samples) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0


def with_noise(samples: Samples, noise: Samples, snr_db: float) -> Samples:
    level = rms(samples) / (10 ** (snr_db / 20)) / max(rms(noise), 1e-9)
    return (samples + noise * level).astype(np.float32)


def pink(rng: np.random.Generator, count: int) -> Samples:
    white = np.fft.rfft(rng.standard_normal(count))
    scale = 1 / np.sqrt(np.maximum(np.arange(len(white)), 1))
    return np.fft.irfft(white * scale, n=count).astype(np.float32)


def reverb(samples: Samples, rate: int, rng: np.random.Generator, seconds: float) -> Samples:
    """A small room: the direct sound plus a decaying noise tail (``seconds`` to -60 dB)."""
    length = int(seconds * rate)
    t = np.arange(length) / rate
    tail = rng.standard_normal(length) * np.exp(-6.9 * t / seconds) * 0.25
    tail[0] = 1.0
    out = np.fft.irfft(
        np.fft.rfft(samples, n=len(samples) + length) * np.fft.rfft(tail, n=len(samples) + length),
        n=len(samples) + length,
    )[: len(samples)]
    return (out * rms(samples) / max(rms(out.astype(np.float32)), 1e-9)).astype(np.float32)


def stretch(samples: Samples, factor: float, window: int = 1024) -> Samples:
    """``samples`` made ``factor`` times longer at the same pitch (WSOLA: overlap-add of windows
    taken where they best continue the previous one)."""
    if abs(factor - 1.0) < 1e-3 or len(samples) < 2 * window:
        return samples
    hop_out = window // 2
    hop_in = hop_out / factor
    tolerance = window // 4
    hann = np.hanning(window).astype(np.float32)
    padded = np.concatenate(
        [np.zeros(tolerance, np.float32), samples, np.zeros(window + 2 * tolerance, np.float32)]
    )
    frames = int((len(samples) - window) / hop_in) + 1
    out = np.zeros(frames * hop_out + window, dtype=np.float32)
    weight = np.zeros_like(out)
    previous = tolerance
    for k in range(frames):
        target = tolerance + int(k * hop_in)
        if k == 0:
            start = target
        else:
            natural = padded[previous + hop_out : previous + hop_out + window]
            lo = max(0, target - tolerance)
            region = padded[lo : target + tolerance + window]
            scores = np.correlate(region, natural, mode="valid")
            start = lo + int(np.argmax(scores))
        out[k * hop_out : k * hop_out + window] += padded[start : start + window] * hann
        weight[k * hop_out : k * hop_out + window] += hann
        previous = start
    return (out / np.maximum(weight, 1e-3))[: int(len(samples) * factor)].astype(np.float32)


def pitch_shift(samples: Samples, factor: float, length: float = 1.0) -> Samples:
    """Pitch and formants times ``factor`` (resampled), then the length put back to ``length``
    times the original's."""
    if abs(factor - 1.0) < 1e-3:
        return stretch(samples, length)
    moved = resample(samples, round(VOICE_RATE * factor), VOICE_RATE)
    return stretch(moved, length * len(samples) / max(len(moved), 1))


def trim(samples: Samples, rate: int, keep: float = 0.03) -> Samples:
    """The sound without its silence before and after (keeping ``keep`` seconds)."""
    frame = rate // 100
    count = len(samples) // frame
    if count == 0:
        return samples
    energy = np.sqrt((samples[: count * frame].reshape(count, frame) ** 2).mean(axis=1))
    loud = np.flatnonzero(energy > 0.02 * float(energy.max()))
    if len(loud) == 0:
        return samples
    pad = int(keep * rate)
    return samples[max(0, loud[0] * frame - pad) : min(len(samples), (loud[-1] + 1) * frame + pad)]


def seed_of(name: str) -> int:
    return SEED ^ zlib.crc32(name.encode("utf-8"))


def microphone(samples: Samples, mic: Mic, name: str) -> Samples:
    """``samples`` (at 24 kHz) as recorded by ``mic``, at 16 kHz."""
    rng = np.random.default_rng(seed_of(f"{mic}:{name}"))
    if mic == "mic1":
        rate = 48_000
        sound = resample(samples, VOICE_RATE, rate)
        sound = band_pass(sound, rate, 90, 7000)
        t = np.arange(len(sound)) / rate
        hum = (np.sin(2 * np.pi * 50 * t) + 0.3 * np.sin(2 * np.pi * 150 * t)).astype(np.float32)
        noise = (rng.standard_normal(len(sound)) + 0.5 * hum).astype(np.float32)
        sound = with_noise(sound, noise, 30)
    else:
        rate = 22_050
        sound = resample(samples, VOICE_RATE, rate)
        sound = reverb(sound, rate, rng, 0.25)
        sound = band_pass(sound, rate, 300, 3800, tilt_db=4)
        sound = with_noise(sound, pink(rng, len(sound)), 18)
    sound = resample(sound, rate, OUT_RATE)
    peak = float(np.max(np.abs(sound))) if len(sound) else 0.0
    return (sound * (0.7 / peak) if peak > 0 else sound).astype(np.float32)


# --- The voice ------------------------------------------------------------------------------------


class Voice:
    """The service's floor voice, loaded once; every sentence is drawn from a fixed seed."""

    def __init__(self) -> None:
        from domo_speech.data import candidates
        from domo_speech.real.build import build
        from domo_speech.real.says import FlowVoice
        from domo_speech.real.weights import ensure_weights

        entry = next(e for e in candidates() if e.id == FLOOR_VOICE)
        engine = build(entry, ensure_weights(entry, MODEL_DIR), gpu=False, program=None)
        assert isinstance(engine, FlowVoice)
        self._engine = engine

    def say(self, text: str, style: str, language: str, name: str) -> Samples:
        """``text`` in ``style`` at 24 kHz, as floats, from the seed of ``name``."""
        from domo_speech.real.sound import floats_of

        # The engine's own random draw (unseeded in the service) is replaced by a seeded one.
        setattr(self._engine, "_random", np.random.default_rng(seed_of(name)))  # noqa: B010
        pcm = b"".join(self._engine.say(text, style, language))
        return floats_of(pcm)


def spoken(voice: Voice, who: Speaker, text: str, language: str, name: str) -> Samples:
    return pitch_shift(voice.say(text, who.style, language, name), who.raise_by, who.length)


def median_pitch(samples: Samples) -> float:
    from domo_speech.real.intonation import intonation
    from domo_speech.real.sound import pcm_of

    pitch, _, _ = intonation(pcm_of(resample(samples, VOICE_RATE, OUT_RATE)))
    if pitch is None:
        raise ValueError("no pitch")
    return pitch


# --- The set --------------------------------------------------------------------------------------


@dataclass
class Item:
    file: str
    who: str
    kind: str
    mic: str
    language: str
    text: str
    seconds: float
    segments: Sequence[tuple[float, float, str]] = ()


def write(path: Path, samples: Samples) -> None:
    from domo_speech.real.sound import pcm_of

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OUT_RATE)
        wav.writeframes(pcm_of(samples))


def both_mics(
    out: Path, base: str, samples: Samples, *, who: str, kind: str, language: str, text: str
) -> Iterator[Item]:
    for mic in MICS:
        file = f"{base}.{mic}.wav"
        recorded = microphone(samples, mic, base)
        write(out / file, recorded)
        yield Item(file, who, kind, mic, language, text, len(recorded) / OUT_RATE)


def build_set(out: Path) -> list[Item]:
    voice = Voice()
    items: list[Item] = []
    clean: dict[tuple[str, str], Samples] = {}
    for who in PEOPLE:
        for kind, sentences in (("learn", LEARN), ("test", TEST)):
            for index, (language, text) in enumerate(sentences):
                base = f"{who.name.lower()}.{kind}{index}.{language}"
                samples = spoken(voice, who, text, language, base)
                clean[(who.name, text)] = samples
                items += both_mics(
                    out, base, samples, who=who.name, kind=kind, language=language, text=text
                )
    alex = PEOPLE[0]
    for index, (language, text) in enumerate(TEST):
        lowered = resample(clean[("Alex", text)], VOICE_RATE, round(VOICE_RATE * 2 ** (1.5 / 12)))
        cold = nasal(lowered, VOICE_RATE)
        items += both_mics(
            out, f"alex.cold{index}.{language}", cold,
            who="Alex", kind="cold", language=language, text=text,
        )  # fmt: skip
        paul = clean[("Paul", text)]
        target = clean[("Alex", text)]
        factor = median_pitch(target) / median_pitch(paul)
        moved = pitch_shift(paul, factor, len(target) / len(paul))
        items += both_mics(
            out, f"paul.imitation{index}.{language}", moved,
            who="Paul", kind="imitation", language=language, text=text,
        )  # fmt: skip
    for who in (alex, PEOPLE[1]):
        language, text = SHORT
        base = f"{who.name.lower()}.short.{language}"
        said = trim(spoken(voice, who, text, language, base), VOICE_RATE, keep=0.0)
        half = int(0.5 * VOICE_RATE)
        said = np.concatenate([said, np.zeros(max(0, half - len(said)), np.float32)])[:half]
        items += both_mics(
            out, base, said, who=who.name, kind="short", language=language, text=text
        )
    from domo_speech.real.says import sentences as cut

    parts = [
        trim(spoken(voice, alex, piece, "fr", f"alex.long.{i}"), VOICE_RATE, keep=0.01)
        for i, piece in enumerate(cut(LONG, longest=1))
    ]
    long = np.concatenate(parts)
    file = "alex.long.fr.mic1.wav"
    recorded = microphone(long, "mic1", "alex.long")
    write(out / file, recorded)
    items.append(Item(file, "Alex", "long", "mic1", "fr", LONG, len(recorded) / OUT_RATE))
    items.append(room(voice, out))
    return items


def room(voice: Voice, out: Path) -> Item:
    """Dana and Sam in turn, a pause of 0.3 s between turns but the fourth turn starting 0.4 s
    before the third ends; through a room, then mic1."""
    by_name = {who.name: who for who in PEOPLE}
    mix = np.zeros(0, dtype=np.float32)
    segments: list[tuple[float, float, str]] = []
    at = 0
    for index, (name, text) in enumerate(ROOM):
        said = trim(spoken(voice, by_name[name], text, "fr", f"room.{index}"), VOICE_RATE)
        start = at + int((-0.4 if index == 3 else 0.3) * VOICE_RATE) if index else 0
        end = start + len(said)
        if end > len(mix):
            mix = np.concatenate([mix, np.zeros(end - len(mix), np.float32)])
        mix[start:end] += said
        segments.append((start / VOICE_RATE, end / VOICE_RATE, name))
        at = end
    rng = np.random.default_rng(seed_of("room"))
    mix = reverb(mix, VOICE_RATE, rng, 0.4)
    recorded = microphone(mix, "mic1", "room")
    file = "room.dana-sam.fr.mic1.wav"
    write(out / file, recorded)
    text = " ".join(t for _, t in ROOM)
    return Item(file, "Dana+Sam", "room", "mic1", "fr", text, len(recorded) / OUT_RATE, segments)


def main() -> None:
    out = TRIALS_DIR.resolve()
    repository = Path(__file__).resolve().parents[2]
    if out == repository or repository in out.parents:
        raise SystemExit("the recorded set never goes inside the repository")
    out.mkdir(parents=True, exist_ok=True)
    items = build_set(out)
    manifest = [item.__dict__ | {"segments": list(item.segments)} for item in items]
    (out / "manifest.json").write_text(
        json.dumps({"seed": SEED, "voice": FLOOR_VOICE, "items": manifest}, indent=1),
        encoding="utf-8",
    )
    print(f"{len(items)} files in {out}")


if __name__ == "__main__":
    main()
