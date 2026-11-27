"""The chat page's voice: static checks only (no browser here; what the page does in a real browser
is tried in the browser trials). The fragment and the scripts are read as files, the page is
rendered through the app, the texts are the assistant's."""

import hashlib
import html
import re
from datetime import UTC, datetime
from pathlib import Path

import domo_core
import pytest
from domo_common.meanings import default_text, meaning, meaning_note
from domo_core.texts.store import Reader, save_written
from web_support import in_language, reads_in

STATIC = Path(domo_core.__file__).parent / "web" / "static"
TEMPLATES = Path(domo_core.__file__).parent / "web" / "templates"
FRAGMENT = TEMPLATES / "fragments" / "voice.html"
KEYS = (
    "voice.turn_on",
    "voice.turn_off",
    "voice.listening",
    "voice.on_gesture",
    "voice.talk",
    "voice.off",
    "voice.preparing",
    "voice.unavailable",
    "voice.no_microphone",
    "voice.refused_by_browser",
    "voice.moved",
    "voice.reload",
    "voice.not_taken_elsewhere",
    "voice.not_taken_now",
    "voice.failed",
    "voice.not_told",
)
SCRIPTS = [STATIC / "voice.js", *sorted((STATIC / "voice").glob("*.js"))]
# What the detector's runtime and model are, as listed in VENDORED.md.
VENDORED_FILES = (
    "voice/ort/ort.wasm.min.mjs",
    "voice/ort/ort-wasm-simd-threaded.mjs",
    "voice/ort/ort-wasm-simd-threaded.wasm",
    "voice/silero_vad.onnx",
)


def string_literals(source: str) -> list[str]:
    """The string literals of a script, comments left out (a small scanner: the scripts of the voice
    keep to quotes that open and close on one line and write no regular expression holding a quote
    or two slashes)."""
    found: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
        elif source.startswith("/*", i):
            i = source.find("*/", i)
            i = n if i < 0 else i + 2
        elif c in "\"'`":
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            found.append(source[i + 1 : j])
            i = j + 1
        else:
            i += 1
    return found


def test_the_scanner_finds_literals_and_skips_comments():
    code = "a = 'one two'; // not a 'string here'\n/* nor 'this one' */ b = \"x\" + `y`;"
    assert string_literals(code) == ["one two", "x", "y"]


# --- the page ---------------------------------------------------------------------------------


async def test_the_chat_page_carries_the_voice_fragment_and_its_script(adult_client):
    r = await adult_client.get("/chat")
    assert r.status_code == 200
    assert 'data-voice="toggle"' in r.text
    assert 'data-voice="state"' in r.text
    assert re.search(r'<script src="/static/voice\.js\?v=[^"]+" type="module"></script>', r.text)
    # chat.js is untouched by the voice: the page keeps its own script.
    assert "/static/chat.js" in r.text
    # The voice is announced where the page can say it has no sentence of its own: the person it
    # was rendered for is already on the body, the voice socket reads it there.
    assert "data-page-person=" in r.text


def test_the_fragment_and_the_script_files_exist():
    assert FRAGMENT.is_file()
    assert (STATIC / "voice.js").is_file()
    assert (STATIC / "voice" / "detector.js").is_file()
    assert (STATIC / "voice" / "capture-worklet.js").is_file()


def test_every_voice_text_has_a_meaning_a_french_default_and_a_note():
    for key in KEYS:
        assert meaning(key).strip(), key
        assert default_text(key, "fr") != meaning(key), key
        assert (meaning_note(key) or "").strip(), key


async def test_every_voice_text_is_read_from_the_texts(adult_client, adult_account, session):
    await reads_in(session, adult_account.person_id, "fr")
    page = html.unescape((await adult_client.get("/chat")).text)
    # The page holds each text, whatever the reader's version of it is.
    for key in KEYS:
        assert in_language("fr", key) in page, key
    # A person whose recueil rewrote one reads theirs.
    await save_written(
        session,
        Reader(adult_account.person_id, "fr"),
        {"voice.listening": "Je suis tout ouïe"},
        model_id="m",
        now=datetime.now(UTC),
    )
    page = html.unescape((await adult_client.get("/chat")).text)
    assert "Je suis tout ouïe" in page
    assert in_language("fr", "voice.listening") not in page


def test_the_fragment_holds_its_texts_only_through_t():
    source = FRAGMENT.read_text(encoding="utf-8")
    for key in KEYS:
        assert f'"{key}"' in source or f"'{key}'" in source, key
    # No text of its own outside the {{ t(...) }} calls and the markup.
    stripped = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)
    stripped = re.sub(r"\{\{.*?\}\}", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"\{%.*?%\}", "", stripped, flags=re.DOTALL)
    text = re.sub(r"<[^>]*>", "", stripped)
    assert text.strip() == ""


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_the_script_holds_no_sentence(path):
    for literal in string_literals(path.read_text(encoding="utf-8")):
        assert not re.search(r"[A-Za-z] [A-Za-z]", literal), (path.name, literal)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_the_script_has_no_inline_code_from_elsewhere(path):
    source = path.read_text(encoding="utf-8")
    for literal in string_literals(source):
        assert not re.search(r"(https?:)?//[A-Za-z0-9]", literal), (path.name, literal)
    assert "eval(" not in source and "new Function" not in source


def test_nothing_is_loaded_from_elsewhere():
    fragment = FRAGMENT.read_text(encoding="utf-8")
    assert not re.search(r"https?://|\s(src|href)=[\"']//", fragment)
    for path in SCRIPTS:
        for literal in string_literals(path.read_text(encoding="utf-8")):
            assert "http://" not in literal and "https://" not in literal, path.name
            assert not literal.startswith("//"), path.name


def test_the_detector_files_are_listed_in_vendored_md_with_a_licence():
    notes = (STATIC / "VENDORED.md").read_text(encoding="utf-8")
    for name in VENDORED_FILES:
        path = STATIC / name
        assert path.is_file(), name
        line = next((ln for ln in notes.splitlines() if f"`{name}`" in ln), None)
        assert line is not None, f"{name}: not listed"
        assert "MIT" in line, f"{name}: no licence"
        assert "https://" in line, f"{name}: no source"
        assert hashlib.sha256(path.read_bytes()).hexdigest() in line, name


async def test_the_detector_files_are_served_with_types_a_browser_runs(adult_client):
    for name, kind in (
        ("voice.js", "javascript"),
        ("voice/detector.js", "javascript"),
        ("voice/capture-worklet.js", "javascript"),
        ("voice/ort/ort.wasm.min.mjs", "javascript"),
        ("voice/ort/ort-wasm-simd-threaded.mjs", "javascript"),
        ("voice/ort/ort-wasm-simd-threaded.wasm", "application/wasm"),
    ):
        r = await adult_client.get(f"/static/{name}")
        assert r.status_code == 200, name
        assert kind in r.headers["content-type"], (name, r.headers["content-type"])
    assert (await adult_client.get("/static/voice/silero_vad.onnx")).status_code == 200


def test_the_script_tells_the_two_messages_that_say_what_was_not_taken_or_failed():
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    for name in (
        "not_taken",
        "failed",
        "not_taken_elsewhere",
        "not_taken_now",
        "not_told",
    ):
        assert f'"{name}"' in source, name
    fragment = FRAGMENT.read_text(encoding="utf-8")
    assert 'data-voice="notice"' in fragment
    assert "chat.error.no_key" in fragment  # the reason `no_key` is the assistant's existing text


def test_the_page_lets_go_of_everything_when_it_is_left():
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    assert '"pagehide"' in source and '"visibilitychange"' in source


def test_nothing_is_said_of_a_sentence_too_late_any_more():
    """No reason ``too_late`` is ever sent (a sentence said during a reply is kept): the page
    holds no notice for it, and no text."""
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    assert "too_late" not in source
    assert "too_late" not in FRAGMENT.read_text(encoding="utf-8")


def test_the_page_talks_only_while_the_gesture_is_held_when_the_house_asks_it():
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    assert '"gesture"' in source and 'data-voice="talk"' in source
    handle = source[source.index("async function handle") : source.index("function beginMoment")]
    assert "held" in handle
    fragment = FRAGMENT.read_text(encoding="utf-8")
    assert 'data-voice="talk" hidden' in fragment


def test_a_failed_reconnect_never_resets_the_back_off():
    """The back-off is reset only by a socket that really opened and lived: ``openedAt`` is
    cleared at each close, and read before it is."""
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    closed = source[source.index("function onSocketClosed") : source.index("function reloadOnce")]
    assert closed.index("const lived") < closed.index("openedAt = 0")
    assert "attempt = 0" in closed and "if (lived)" in closed


def test_a_microphone_opened_is_stopped_when_the_detector_fails():
    source = (STATIC / "voice.js").read_text(encoding="utf-8")
    start = source[
        source.index("async function startCapture") : source.index("function resumeOnTouch")
    ]
    assert "allSettled" in start and "Promise.all(" not in start
