"""A voice that is a separate program: started with its arguments, fed the text, read as PCM."""

import sys
import textwrap
import time
from pathlib import Path

import numpy as np
import pytest
from domo_speech.real.says import ProgramFailed, ProgramVoice
from domo_speech.real.sound import floats_of

PROGRAM = textwrap.dedent(
    """
    import math, struct, sys
    text = sys.stdin.readline().strip()
    model = sys.argv[sys.argv.index("--model") + 1]
    open(sys.argv[sys.argv.index("--seen") + 1], "w", encoding="utf-8").write(model + "|" + text)
    rate = 22050
    for i in range(rate):  # one second of 440 Hz
        value = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
        sys.stdout.buffer.write(struct.pack("<h", value))
    sys.stdout.flush()
    sys.exit(3 if "fail" in text else 0)
    """
)


def _voice(tmp_path: Path) -> tuple[ProgramVoice, Path]:
    script = tmp_path / "voice_program.py"
    script.write_text(PROGRAM, encoding="utf-8")
    seen = tmp_path / "seen.txt"
    voice = ProgramVoice(
        Path(sys.executable),
        ("-I", str(script), "--model", "{weights}/voice.onnx", "--seen", str(seen)),
        tmp_path / "weights",
        22_050,
    )
    return voice, seen


async def test_the_program_hears_the_text_and_answers_24k_pcm(tmp_path: Path) -> None:
    voice, seen = _voice(tmp_path)

    pcm = b"".join([chunk async for chunk in voice.say("hello\nthere  you")])

    model, text = seen.read_text(encoding="utf-8").split("|")
    assert text == "hello there you"
    assert model == f"{tmp_path / 'weights'}/voice.onnx"
    samples = floats_of(pcm)
    assert abs(len(samples) - 24_000) <= 1
    assert abs(np.argmax(np.abs(np.fft.rfft(samples))) * 24_000 / len(samples) - 440) < 2
    assert "voice_program" not in sys.modules


async def test_a_program_that_fails_is_an_error_even_after_sound(tmp_path: Path) -> None:
    voice, _ = _voice(tmp_path)
    got: list[bytes] = []

    with pytest.raises(ProgramFailed):
        async for chunk in voice.say("please fail"):
            got.append(chunk)

    assert got  # sound came first: the route cuts the answer instead of completing it


async def test_a_program_that_hangs_is_stopped_at_its_deadline(tmp_path: Path) -> None:
    script = tmp_path / "hangs.py"
    script.write_text(
        "import sys, time\nsys.stdin.readline()\nsys.stdout.buffer.write(bytes(8820))\n"
        "sys.stdout.flush()\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    voice = ProgramVoice(
        Path(sys.executable), ("-I", str(script)), tmp_path, 22_050, base_seconds=0.5
    )
    started = time.perf_counter()

    with pytest.raises(ProgramFailed, match="program_timeout"):
        async for _ in voice.say("hi"):
            pass

    assert time.perf_counter() - started < 10
