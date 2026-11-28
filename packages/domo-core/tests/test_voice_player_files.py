"""The page plays the assistant's voice: static checks of the files, the wiring and the texts (no
browser here; what a real browser does with the sound is tried in the browser trials), and the
logic of the player run for real in Node against a fake audio context
(``voice_player_check.mjs``, skipped where Node is not installed)."""

import re
import shutil
import subprocess
from pathlib import Path

import domo_core
import pytest
from domo_common.meanings import default_text, meaning, meaning_note

HERE = Path(__file__).parent
WEB = Path(domo_core.__file__).parent / "web"
STATIC = WEB / "static"
FRAGMENT = WEB / "templates" / "fragments" / "voice.html"
PLAYER = STATIC / "voice" / "player.js"
WORKLET = STATIC / "voice" / "player-worklet.js"
VOICE = STATIC / "voice.js"
KEYS = (
    "voice.speaking",
    "voice.cannot_speak",
    "voice.cannot_speak_preparing",
    "voice.cannot_speak_limit",
    "voice.cannot_speak_no_model",
    "voice.cannot_play",
)
PLAYER_NAMES = ("begin", "push", "end", "duck", "stop", "resume", "playing")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def between(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def test_the_player_files_exist():
    assert PLAYER.is_file() and WORKLET.is_file()


def test_the_player_exports_what_the_voice_script_uses():
    player = read(PLAYER)
    assert "export function createPlayer(" in player
    assert "export function playbackHooks(" in player
    for name in PLAYER_NAMES:
        assert re.search(rf"^    {name}\(", player, re.MULTILINE), name
    voice = read(VOICE)
    assert re.search(r'import \{[^}]*\bcreatePlayer\b[^}]*\} from "\./voice/player\.js"', voice)
    assert re.search(r'import \{[^}]*\bplaybackHooks\b[^}]*\} from "\./voice/player\.js"', voice)
    assert "createPlayer(context" in voice
    assert 'registerProcessor("domo-player"' in read(WORKLET)
    assert 'new AudioWorkletNode(context, "domo-player"' in player


def test_the_player_hooks_are_the_hooks_of_the_voice_script():
    voice = read(VOICE)
    assert re.search(r"onMessage:\s*playback\.onMessage", voice)
    assert re.search(r"onSound:\s*playback\.onSound", voice)
    assert "playbackHooks(() => speaker" in voice


def test_the_player_holds_no_sentence_and_loads_nothing_from_elsewhere():
    from test_voice_page import string_literals

    for path in (PLAYER, WORKLET):
        source = read(path)
        for literal in string_literals(source):
            assert not re.search(r"[A-Za-z] [A-Za-z]", literal), (path.name, literal)
            assert "http:" not in literal and "https:" not in literal, (path.name, literal)
            assert not literal.startswith(("//", "blob:", "data:")), (path.name, literal)
        for forbidden in ("eval(", "new Function", "fetch(", "XMLHttpRequest", "importScripts"):
            assert forbidden not in source, (path.name, forbidden)


def test_no_sound_is_stored_or_logged():
    for path in (PLAYER, WORKLET, VOICE):
        source = read(path)
        for forbidden in ("indexedDB", "console.", "createObjectURL", "new Blob", "new Audio("):
            assert forbidden not in source, (path.name, forbidden)
    assert "Storage" not in read(PLAYER)


def test_the_content_security_policy_is_not_widened_for_the_player():
    assert "blob:" not in read(VOICE)
    policies = [read(p) for p in WEB.rglob("*.py") if "script-src" in read(p)]
    assert policies, "the policy was not found"
    assert all("media-src" not in s for s in policies)


def test_ducking_is_wired_to_speech_start_not_to_a_server_message():
    voice = read(VOICE)
    begin = between(voice, "function beginMoment", "function endMoment")
    assert re.search(r"speaker\.duck\(\)", begin)
    assert voice.count(".duck(") == 1  # the server's `duck` goes through the hooks, to the player
    # The end of the moment starts the short wait for the house's word.
    assert "speaker.speechEnded()" in between(voice, "function abandonMoment", "// --- the socket")


def test_the_player_ducks_mechanically_and_waits_for_the_house():
    player = read(PLAYER)
    for line in (
        "const DUCK_LEVEL = 0.25;",
        "const DUCK_MS = 80;",
        "const STOP_FADE_MS = 30;",
        "const RESUME_MS = 150;",
        "const DUCK_BACK_MS = 1500;",
    ):
        assert line in player, line
    duck = between(player, "    duck() {", "    // The person stopped speaking")
    assert "stop(" not in duck  # lowering never stops: that is the house's judgement


def test_the_socket_going_or_the_voice_turned_off_cuts_the_sound():
    voice = read(VOICE)
    assert "speaker.stop()" in between(voice, "function onSocketClosed", "function reloadOnce")
    assert "speaker.stop()" in between(voice, "function stopAll", "async function startCapture")


def test_every_new_voice_text_is_the_assistants_and_reaches_the_page():
    fragment = read(FRAGMENT)
    player = read(PLAYER)
    for key in KEYS:
        assert meaning(key).strip(), key
        assert default_text(key, "fr") != meaning(key), key
        assert (meaning_note(key) or "").strip(), key
        attribute = "data-text-" + key.removeprefix("voice.").replace("_", "-")
        assert f"{attribute}=\"{{{{ t('{key}') }}}}\"" in fragment, key
    # The player names its notices (the page writes none): each name has its attribute.
    for name in (
        "cannot_play",
        "cannot_speak",
        "cannot_speak_preparing",
        "cannot_speak_limit",
        "cannot_speak_no_model",
    ):
        assert f'"{name}"' in player, name
    render = between(read(VOICE), "function render()", "function setPlaying")
    assert "voice.speaking" in KEYS
    assert 'if (voicePlaying && current === "listening") {\n    shown = "speaking";' in render


def test_the_state_line_says_the_voice_plays_only_while_it_does():
    assert "onPlaying: setPlaying" in read(VOICE)


def test_the_reconnect_counter_resets_only_after_a_socket_that_lived():
    voice = read(VOICE)
    assert "attempt" not in between(voice, 'case "state":', 'case "listening":')
    assert "if (lived)" in between(voice, "function onSocketClosed", "function reloadOnce")


def test_turning_off_ends_the_moment_before_it_says_off():
    off = between(read(VOICE), "function turnOff", "const playback = ")
    assert off.index('"speech_end"') < off.index('type: "off"')
    assert off.index("speaker.stop()") < off.index('type: "off"')


def test_the_hook_comment_no_longer_lists_a_message_the_page_consumes():
    voice = read(VOICE)
    comment = re.search(r"// say, say_end[^\n]*", voice)
    assert comment and "failed" not in comment.group(0)
    assert voice.count('case "failed":') == 1 and voice.count('case "not_taken":') == 1


# --- the player run for real, in Node ------------------------------------------------------------

SCENARIOS = re.findall(
    r"^  (?:async )?([a-z]+_[a-z_]+)\(\) \{", read(HERE / "voice_player_check.mjs"), re.M
)


def test_the_node_scenarios_were_found():
    assert len(SCENARIOS) >= 27


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_player_logic_in_node(scenario, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    # Copies with the .mjs extension: Node reads them as modules whatever its version.
    player, worklet = tmp_path / "player.mjs", tmp_path / "player-worklet.mjs"
    player.write_text(read(PLAYER), encoding="utf-8")
    worklet.write_text(read(WORKLET), encoding="utf-8")
    shutil.copy(HERE / "voice_player_check.mjs", tmp_path / "check.mjs")
    done = subprocess.run(
        [node, str(tmp_path / "check.mjs"), str(player), str(worklet), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_houses_duck_goes_to_the_idempotent_duck_and_the_texts_say_the_answer():
    player = read(PLAYER)
    assert "player.houseDuck()" in player
    for locale in ("en", "fr"):
        text = read(
            Path(domo_core.__file__).parents[3]
            / "domo-common/src/domo_common/locales"
            / f"{locale}.json"
        )
        for line in text.splitlines():
            if line.strip().startswith('"voice.cannot_'):
                assert (
                    " his " not in line and " sa réponse" not in line and " sa réponse" not in line
                )
