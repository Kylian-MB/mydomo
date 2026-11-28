"""The program: its three commands and the conversation, against a Domo made with ``respx``."""

import io
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from domo_talk import config, talk
from domo_talk.__main__ import main
from domo_talk.client import Client
from domo_talk.config import Saved

URL = "https://domo.test"
API = f"{URL}/api/channel"
SECRET = "s3cr3t-of-this-device"
PASSWORD = "correct horse battery"
REPLY = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"
SAVED = Saved(url=URL, secret=SECRET, ca=None)
REAL_DRAIN = talk.drain_console


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def terminal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The input is a terminal, and emptying the console is noted (``drained``) instead of done."""
    noted: list[str] = []
    monkeypatch.setattr(talk, "_is_terminal", lambda: True)
    monkeypatch.setattr(talk, "drain_console", lambda: noted.append("drained"))
    return noted


def typed(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """What the person types at the prompt, one line after the other, then the end of input."""
    remaining = list(lines)

    def read(prompt: str = "") -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", read)


def sse(*events: tuple[str, dict[str, object]]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events
    ).encode()


def nothing_heard() -> None:
    respx.get(f"{API}/listen").respond(204)
    # An older server: it has no texts for the program (``in_the_persons_words`` gives it some).
    respx.get(f"{API}/texts", name="texts").respond(404, json={"error": "not_found"})


# What the server writes for a person who reads French: the program says these, and no other.
FRENCH = {
    "talk.no_key": "L'assistant ne peut pas encore répondre.",
    "talk.too_long": "C'est trop long.",
    "talk.not_sayable": "L'assistant ne peut pas prendre ce message.",
    "talk.held_back": "Trop d'essais.",
    "talk.refused": "L'assistant a refusé cela ({error}).",
    "talk.proof.prompt": "Mot de passe :",
    "talk.proof.nothing_waits": "Aucune preuve n'est attendue ici.",
    "talk.proof.busy": "Trop de mots de passe à la fois.",
    "talk.unknown_command": "Cette commande n'existe pas ici.",
    "talk.unreachable": "L'assistant est injoignable à {url}.",
    "talk.not_trusted": "Son certificat n'est pas reconnu.",
    "talk.unpaired_now": "L'assistant ne connaît plus cet ordinateur. Appuie sur Entrée.",
}


def in_the_persons_words(texts: dict[str, str] | None = None) -> None:
    """The server has texts for the program, in French (after ``nothing_heard``)."""
    respx.routes["texts"].respond(json={"language": "fr", "texts": texts or FRENCH})


def wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("not there in time")


def acted(sentence: str, **more: object) -> tuple[str, dict[str, object]]:
    data: dict[str, object] = {
        "status": "done",
        "sentence": sentence,
        "once": None,
        "request": None,
    }
    return "acted", {**data, **more}


# --- pair and forget ----------------------------------------------------------------------------


@respx.mock
def test_pairing_asks_the_code_and_keeps_the_secret_without_showing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.post(f"{API}/pair").respond(
        json={"device_id": "d", "secret": SECRET, "person": "Alex"}
    )
    typed(monkeypatch, " 123456 ")
    assert main(["pair", URL + "/", "--name", "desk"]) == 0
    assert json.loads(route.calls.last.request.content) == {"code": "123456", "name": "desk"}
    assert config.load() == SAVED
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert out.out.strip()


@respx.mock
def test_pairing_keeps_where_the_authority_is(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str | None] = []

    def pair(url: str, code: str, name: str, ca: str | None) -> Saved:
        asked.append(ca)
        return Saved(url=url, secret=SECRET, ca=ca)

    monkeypatch.setattr(Client, "pair", staticmethod(pair))
    typed(monkeypatch, "1")
    assert main(["pair", URL, "--ca", "ca.crt"]) == 0
    loaded = config.load()
    assert loaded is not None
    assert loaded.ca is not None
    assert Path(loaded.ca).is_absolute()
    assert asked == [loaded.ca]


@respx.mock
def test_a_code_that_is_refused_is_said_and_nothing_is_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(f"{API}/pair").respond(404, json={"error": "unknown_code"})
    typed(monkeypatch, "000000")
    assert main(["pair", URL]) == 1
    assert config.load() is None
    assert "code" in capsys.readouterr().err.lower()


def test_forgetting_deletes_the_saved_secret(capsys: pytest.CaptureFixture[str]) -> None:
    config.save(SAVED)
    assert main(["forget"]) == 0
    assert config.load() is None
    assert SECRET not in capsys.readouterr().out


def test_without_a_pairing_the_program_says_how_to_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    assert "pair" in capsys.readouterr().err


# --- the conversation ---------------------------------------------------------------------------


@respx.mock
def test_the_recent_conversation_is_shown_then_the_words_as_they_come(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(
        json={
            "messages": [
                {"id": "a", "role": "user", "text": "Good morning", "cards": []},
                {"id": "b", "role": "assistant", "text": "Hello!", "cards": ["Lights on."]},
            ]
        }
    )
    post = respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(
        content=sse(
            ("words", {"text": "Sure, "}),
            ("words", {"text": "ça marche"}),
            acted("Lamp turned on."),
            ("ended", {"status": "done"}),
        )
    )
    typed(monkeypatch, "", "Turn on the lamp")
    assert main([]) == 0
    assert json.loads(post.calls.last.request.content) == {"text": "Turn on the lamp"}
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines.index("> Good morning") < lines.index("Hello!") < lines.index("· Lights on.")
    assert any(line.endswith("Sure, ça marche") for line in lines)
    assert "· Lamp turned on." in lines
    assert out.index("Sure, ") < out.index("· Lamp turned on.")


@respx.mock
def test_the_name_the_person_gave_domo_is_written_before_his_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 6.7: where the program writes Domo's words, the name the person calls him by comes
    first, once per reply; the prompt stays "> ", and the name is shown as text, never acted on."""
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(
        json={
            "messages": [
                {"id": "a", "role": "user", "text": "Call yourself Brenda", "cards": []},
                {"id": "b", "role": "assistant", "text": "Done!", "cards": []},
            ],
            "domo_name": "Bren\x1b[31mda",
        }
    )
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(
        content=sse(
            ("words", {"text": "Sure, "}),
            ("words", {"text": "on it.\nIt is "}),
            ("words", {"text": "done."}),
            acted("Lamp turned on."),
            ("ended", {"status": "done"}),
        )
    )
    typed(monkeypatch, "Turn on the lamp")
    assert main([]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "> Call yourself Brenda" in lines
    assert "Brenda: Done!" in lines
    assert "Brenda: Sure, on it." in lines and "It is done." in lines
    assert out.count("Brenda:") == 2
    assert "\x1b" not in out


@respx.mock
def test_a_name_given_during_the_conversation_is_followed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The person names Domo in the middle of the conversation: his next reply comes after that
    name, without the program being opened again."""
    config.save(SAVED)
    nothing_heard()
    since = respx.get(f"{API}/since")
    since.side_effect = [
        httpx.Response(200, json={"messages": [], "domo_name": None}),
        httpx.Response(200, json={"messages": [], "domo_name": "Brenda"}),
        httpx.Response(200, json={"messages": [], "domo_name": "Brenda"}),
    ]
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(
        content=sse(("words", {"text": "Okay."}), ("ended", {"status": "done"}))
    )
    typed(monkeypatch, "Call yourself Brenda", "Hello")
    assert main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines.count("Okay.") == 1 and lines.count("Brenda: Okay.") == 1
    assert lines.index("Okay.") < lines.index("Brenda: Okay.")
    # The name is read after the reply it may have come with.
    assert since.calls[1].request.url.params["after"] == REPLY


@respx.mock
def test_without_a_name_domo_words_come_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(
        json={
            "messages": [{"id": "b", "role": "assistant", "text": "Hello!", "cards": []}],
            "domo_name": None,
        }
    )
    typed(monkeypatch)
    assert main([]) == 0
    assert "Hello!" in capsys.readouterr().out.splitlines()


@respx.mock
def test_what_is_shown_once_is_written_once_and_not_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    once = {
        "kind": "link",
        "value": "https://domo.test/join/abc",
        "sentence": "Open it now, it shows only once.",
    }
    respx.get(f"{API}/stream/{REPLY}").respond(
        content=sse(acted("Here is a link for Sam.", once=once), ("ended", {"status": "done"}))
    )
    typed(monkeypatch, "invite Sam")
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.count("https://domo.test/join/abc") == 1
    assert "Open it now, it shows only once." in out
    assert "https://domo.test/join/abc" not in config.config_path().read_text("utf-8")


def waiting(proofs: list[str], answer_with: str = "proof") -> bytes:
    request = {"id": REQUEST, "answer_with": answer_with, "proofs": proofs}
    return sse(
        acted("This waits for your proof.", status="pending", request=request),
        ("ended", {"status": "done"}),
    )


@respx.mock
def test_the_proof_input_opens_by_itself_when_one_is_awaited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "approved", "sentence": "Thank you, it is done."}
    )
    asked: list[str] = []

    def getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return PASSWORD

    monkeypatch.setattr("getpass.getpass", getpass)
    typed(monkeypatch, "delete my files")  # nothing else is typed: no /prove
    assert main([]) == 0
    assert asked == ["Mot de passe : "]  # the server's sentence, once, for this request
    assert json.loads(prove.calls.last.request.content) == {
        "means": "password",
        "answer": PASSWORD,
    }
    out = capsys.readouterr()
    assert "Thank you, it is done." in out.out
    assert "/prove" not in out.out + out.err
    assert PASSWORD not in out.out + out.err
    assert PASSWORD not in config.config_path().read_text("utf-8")


@respx.mock
def test_the_hidden_line_is_opened_once_for_a_request_and_again_when_a_reply_shows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    other = sse(("words", {"text": "Fine."}), ("ended", {"status": "done"}))
    respx.get(f"{API}/stream/{REPLY}").mock(
        side_effect=[
            httpx.Response(200, content=waiting(["password"])),
            httpx.Response(200, content=other),
            httpx.Response(200, content=waiting(["password"])),
        ]
    )
    opened: list[str] = []

    def getpass(prompt: str = "") -> str:
        opened.append(prompt)
        return ""  # gives up each time

    monkeypatch.setattr("getpass.getpass", getpass)
    typed(monkeypatch, "delete my files", "and the weather?", "delete them")
    assert main([]) == 0
    # Asked after the first reply; not after the second (it does not show the request); asked
    # again after the third, which shows it again.
    assert opened == ["Mot de passe : ", "Mot de passe : "]


@respx.mock
def test_an_empty_proof_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    typed(monkeypatch, "delete my files", "/prove")
    assert main([]) == 0
    assert not prove.called
    assert FRENCH["talk.proof.nothing_waits"] not in capsys.readouterr().out  # it still waits


@respx.mock
def test_a_hidden_line_never_opens_by_itself_where_the_input_is_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    monkeypatch.setattr(talk, "_is_terminal", lambda: False)

    def refuse(prompt: str = "") -> str:
        raise AssertionError("getpass would echo here")

    monkeypatch.setattr("getpass.getpass", refuse)
    typed(monkeypatch, "delete my files")
    assert main([]) == 0


@respx.mock
def test_what_was_typed_ahead_is_thrown_away_before_the_hidden_line_opens(
    monkeypatch: pytest.MonkeyPatch, terminal: list[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))

    def getpass(prompt: str = "") -> str:
        terminal.append("hidden line")
        return ""

    monkeypatch.setattr("getpass.getpass", getpass)
    typed(monkeypatch, "delete my files")
    assert main([]) == 0
    assert terminal == ["drained", "hidden line"]


def test_the_console_is_emptied_on_windows_and_elsewhere() -> None:
    class Keys:
        def __init__(self) -> None:
            self.waiting = list("abc")
            self.read: list[str] = []

        def kbhit(self) -> bool:
            return bool(self.waiting)

        def getwch(self) -> str:
            self.read.append(self.waiting.pop(0))
            return self.read[-1]

    keys = Keys()
    REAL_DRAIN(windows=True, msvcrt=keys)
    assert keys.read == ["a", "b", "c"] and not keys.waiting

    flushed: list[tuple[int, int]] = []

    class Tty:
        TCIFLUSH = 0

        @staticmethod
        def tcflush(fd: int, queue: int) -> None:
            flushed.append((fd, queue))

    class Input:
        @staticmethod
        def fileno() -> int:
            return 7

    REAL_DRAIN(windows=False, termios=Tty, stdin=Input)
    assert flushed == [(7, 0)]
    # Where nothing can be done, nothing fails.
    REAL_DRAIN(windows=False, termios=object(), stdin=Input)


@respx.mock
def test_prove_still_works_and_is_mentioned_nowhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "approved", "sentence": "Done."}
    )
    answers = iter(["", PASSWORD])  # the first line, opened by itself, is given up
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    typed(monkeypatch, "delete my files", "/prove", "/weird")
    assert main([]) == 0
    assert prove.call_count == 1
    out = capsys.readouterr()
    assert FRENCH["talk.unknown_command"] in out.out
    assert "/prove" not in out.out + out.err
    for text in FRENCH.values():
        assert "/prove" not in text


@respx.mock
def test_prove_is_not_offered_when_no_proof_can_be_given_here(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting([]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove")

    def refuse(prompt: str = "") -> str:
        raise AssertionError("no password should be asked")

    monkeypatch.setattr("getpass.getpass", refuse)
    typed(monkeypatch, "delete my files", "/prove")
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "This waits for your proof." in out
    assert "type /prove" not in out
    assert not prove.called


@respx.mock
def test_a_device_that_is_unknown_says_to_pair_again_and_exits_with_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(401, json={"error": "unknown_device"})
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "pair" in err.lower()
    assert SECRET not in err


@respx.mock
def test_a_device_taken_back_while_talking_exits_with_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(401, json={"error": "unknown_device"})
    typed(monkeypatch, "hello")
    assert main([]) == 2
    assert "pair" in capsys.readouterr().err.lower()


@respx.mock
def test_a_message_that_is_refused_is_said_and_the_talk_goes_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    post = respx.post(f"{API}/messages").respond(409, json={"error": "no_key"})
    typed(monkeypatch, "one", "two")
    assert main([]) == 0
    assert post.call_count == 2
    assert capsys.readouterr().err.strip()


@respx.mock
def test_a_refusal_is_said_with_the_servers_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").mock(
        side_effect=[
            httpx.Response(409, json={"error": "no_key"}),
            httpx.Response(413, json={"error": "too_long"}),
            httpx.Response(400, json={"error": "something_new"}),
        ]
    )
    typed(monkeypatch, "one", "two", "three")
    assert main([]) == 0
    assert capsys.readouterr().err.splitlines() == [
        FRENCH["talk.no_key"],
        FRENCH["talk.too_long"],
        FRENCH["talk.refused"].replace("{error}", "something_new"),
    ]


@respx.mock
def test_without_texts_the_program_says_nothing_of_its_own_and_never_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An older server (no texts) or one whose texts lack a key."""
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").mock(
        side_effect=[
            httpx.Response(409, json={"error": "no_key"}),
            httpx.Response(200, json={"reply_id": REPLY}),
        ]
    )
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    asked: list[str] = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": asked.append(prompt) or "")
    typed(monkeypatch, "one", "two", "/nothing", "/prove")
    assert main([]) == 0
    out = capsys.readouterr()
    assert out.err.strip() == "[no_key]"
    # Not by itself (nothing would say why the typing hides): only the person's own /prove opens it.
    assert asked == [""]
    assert "/" not in out.out.replace("·", "")


@respx.mock
def test_the_texts_are_read_again_after_a_reply_when_the_language_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    texts = respx.routes["texts"]
    texts.mock(
        side_effect=[
            httpx.Response(200, json={"language": "fr", "texts": FRENCH}),
            httpx.Response(
                200,
                json={"language": "en", "texts": {**FRENCH, "talk.too_long": "Too long."}},
            ),
        ]
    )
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").mock(
        side_effect=[
            httpx.Response(200, json={"reply_id": REPLY}),
            httpx.Response(413, json={"error": "too_long"}),
        ]
    )
    respx.get(f"{API}/stream/{REPLY}").respond(
        content=sse(("words", {"text": "Hi."}), ("ended", {"status": "done"}))
    )
    typed(monkeypatch, "hello", "a very long one")
    assert main([]) == 0
    assert capsys.readouterr().err.strip() == "Too long."


@respx.mock
def test_a_line_typed_during_a_reply_is_sent_and_nothing_is_said_about_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Spec 11.3: what answers it comes as what Domo says on his own, on the listening stream.
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    post = respx.post(f"{API}/messages").respond(202, json={"reply_id": None, "kept": True})
    typed(monkeypatch, "and also this")
    assert main([]) == 0
    assert post.call_count == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == ""  # nothing to complain about
    assert "kept" not in captured.out and "busy" not in captured.out.lower()


@respx.mock
def test_a_failure_is_written_with_the_sentence_of_the_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(
        content=sse(
            ("failed", {"reason": "model", "sentence": "I could not think just now."}),
            ("ended", {"status": "failed"}),
        )
    )
    typed(monkeypatch, "hello")
    assert main([]) == 0
    assert "I could not think just now." in capsys.readouterr().out


# --- what Domo says on his own ----------------------------------------------------------------


def then_nothing(*answers: httpx.Response | Exception) -> Callable[[httpx.Request], httpx.Response]:
    """Each call of the route gets the next answer; once they are used, nothing is heard."""
    remaining = list(answers)

    def answer(request: httpx.Request) -> httpx.Response:
        if not remaining:
            return httpx.Response(204)
        given = remaining.pop(0)
        if isinstance(given, Exception):
            raise given
        return given

    return answer


def listening_in(out: io.StringIO) -> talk.Listening:
    return talk.Listening(Client(SAVED), talk.Screen(out), retry=0.01)


@respx.mock
def test_what_domo_says_on_his_own_is_written_on_its_own_lines() -> None:
    body = sse(
        ("words", {"text": "Welcome ", "message_id": "m1"}),
        ("words", {"text": "home.", "message_id": "m1"}),
        ("ended", {"status": "done", "message_id": "m1"}),
        ("words", {"text": "News.", "message_id": "m2"}),
        ("ended", {"status": "done", "message_id": "m2"}),
    )
    respx.get(f"{API}/listen").mock(side_effect=then_nothing(httpx.Response(200, content=body)))
    respx.get(f"{API}/since").respond(json={"messages": [], "domo_name": None})
    respx.get(f"{API}/texts").respond(404)
    out = io.StringIO()
    listening = listening_in(out)
    listening.start()
    try:
        wait_for(lambda: "News." in out.getvalue())
    finally:
        listening.stop()
    assert out.getvalue().splitlines() == ["Welcome home.", "News."]


@respx.mock
def test_a_name_given_in_what_domo_said_on_his_own_is_followed() -> None:
    """Once something Domo said on his own is over, the name is read again after it."""
    body = sse(
        ("words", {"text": "Call me Brenda from now on?", "message_id": "m1"}),
        ("ended", {"status": "done", "message_id": "m1"}),
        ("words", {"text": "News.", "message_id": "m2"}),
        ("ended", {"status": "done", "message_id": "m2"}),
    )
    respx.get(f"{API}/listen").mock(side_effect=then_nothing(httpx.Response(200, content=body)))
    since = respx.get(f"{API}/since").respond(json={"messages": [], "domo_name": "Brenda"})
    respx.get(f"{API}/texts").respond(404)
    out = io.StringIO()
    listening = listening_in(out)
    listening.start()
    try:
        wait_for(lambda: "News." in out.getvalue())
    finally:
        listening.stop()
    assert out.getvalue().splitlines() == ["Call me Brenda from now on?", "Brenda: News."]
    assert since.calls[0].request.url.params["after"] == "m1"


@respx.mock
def test_listening_stops_for_good_when_the_device_is_not_known() -> None:
    route = respx.get(f"{API}/listen").respond(401, json={"error": "unknown_device"})
    out = io.StringIO()
    listening = listening_in(out)
    listening.start()
    wait_for(lambda: not listening.running)
    time.sleep(0.05)
    assert route.call_count == 1
    assert "pair" in out.getvalue().lower()


@respx.mock
def test_listening_tries_again_when_the_server_cannot_be_reached() -> None:
    body = sse(
        ("words", {"text": "Back.", "message_id": "m"}),
        ("ended", {"status": "done", "message_id": "m"}),
    )
    respx.get(f"{API}/listen").mock(
        side_effect=then_nothing(httpx.ConnectError("boom"), httpx.Response(200, content=body))
    )
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.get(f"{API}/texts").respond(404)
    out = io.StringIO()
    listening = listening_in(out)
    listening.start()
    try:
        wait_for(lambda: "Back." in out.getvalue())
    finally:
        listening.stop()
    assert out.getvalue().strip() == "Back."


# --- fix round 1 --------------------------------------------------------------------------------


@respx.mock
def test_a_device_found_unknown_while_listening_ends_the_talk_at_the_next_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    listen = respx.get(f"{API}/listen").respond(401, json={"error": "unknown_device"})
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.get(f"{API}/texts").respond(json={"language": "fr", "texts": FRENCH})
    post = respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})

    def read(prompt: str = "") -> str:
        wait_for(lambda: listen.called)
        time.sleep(0.3)  # the thread has said it by then
        return "hello"

    monkeypatch.setattr("builtins.input", read)
    assert main([]) == 2
    assert not post.called
    assert FRENCH["talk.unpaired_now"] in capsys.readouterr().err


def test_what_arrives_while_the_prompt_waits_is_written_on_its_own_lines() -> None:
    out = io.StringIO()
    screen = talk.Screen(out)
    screen.waiting_at_prompt(True)
    screen.show(talk.Event("words", {"text": "News.", "message_id": "m"}))
    screen.show(talk.Event("ended", {"status": "done", "message_id": "m"}))
    assert out.getvalue() == "\nNews.\n> "


def test_nothing_is_added_when_the_prompt_does_not_wait() -> None:
    out = io.StringIO()
    screen = talk.Screen(out)
    screen.show(talk.Event("words", {"text": "Hi", "message_id": "m"}))
    screen.show(talk.Event("ended", {"status": "done", "message_id": "m"}))
    assert out.getvalue() == "Hi\n"


@respx.mock
def test_a_request_that_waits_keeps_its_proof_through_other_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").mock(
        side_effect=[
            httpx.Response(200, content=waiting(["password"])),
            httpx.Response(
                200, content=sse(("words", {"text": "Fine."}), ("ended", {"status": "done"}))
            ),
        ]
    )
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "executed", "sentence": "Done.", "request": None}
    )
    answers = iter(["", PASSWORD])  # the line opened by itself is given up: the request waits on
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    typed(monkeypatch, "delete my files", "and the weather?", "/prove", "/prove")
    assert main([]) == 0
    assert prove.call_count == 1  # then the request is over: nothing waits any more
    out = capsys.readouterr().out
    assert "Done." in out
    assert FRENCH["talk.proof.nothing_waits"] in out


@respx.mock
def test_a_proof_that_fails_can_be_given_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={
            "outcome": "wrong",
            "sentence": "That is not it.",
            "request": {"id": REQUEST, "answer_with": "proof", "proofs": ["password"]},
        }
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "delete my files", "/prove")  # the first line opens by itself
    assert main([]) == 0
    assert prove.call_count == 2


@respx.mock
def test_an_event_saying_the_request_has_no_proof_to_give_ends_the_offer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    over = sse(
        acted(
            "It was decided elsewhere.",
            request={"id": REQUEST, "answer_with": "yes", "proofs": []},
        ),
        ("ended", {"status": "done"}),
    )
    respx.get(f"{API}/stream/{REPLY}").mock(
        side_effect=[
            httpx.Response(200, content=waiting(["password"])),
            httpx.Response(200, content=over),
        ]
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")  # given up
    typed(monkeypatch, "delete my files", "what now", "/prove")
    assert main([]) == 0
    assert FRENCH["talk.proof.nothing_waits"] in capsys.readouterr().out


@respx.mock
@pytest.mark.parametrize(
    "request_now",
    [
        None,
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "answer_with": "proof",
            "proofs": ["password"],
        },
        {"id": REQUEST, "answer_with": "proof", "proofs": ["passkey"]},
        {"id": REQUEST, "answer_with": "yes", "proofs": []},
    ],
)
def test_the_offer_ends_unless_the_server_says_this_request_still_waits_for_a_proof_given_here(
    monkeypatch: pytest.MonkeyPatch, request_now: dict[str, object] | None
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "whatever", "sentence": "Said.", "request": request_now}
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "delete my files", "/prove")
    assert main([]) == 0
    assert prove.call_count == 1


@respx.mock
def test_what_a_proof_runs_shows_once_is_written_once_and_not_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    in_the_persons_words()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    link = "https://domo.test/join/xyz"
    once = {"kind": "link", "value": link, "sentence": "The link works once."}
    respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "executed", "sentence": "Invited.", "once": once, "request": None}
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "invite someone")
    assert main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    shown = [line for line in lines if link in line]
    assert shown == [f"  {link}"]
    assert lines.index("Invited.") < lines.index("· The link works once.") < lines.index(shown[0])
    assert link not in config.config_path().read_text("utf-8")


def test_a_code_the_sentence_holds_already_is_written_once() -> None:
    out = io.StringIO()
    code = "ABCD-EFGH-JKMN"
    once = {"kind": "code", "value": code, "sentence": f"Your code is {code}, once."}
    talk.Screen(out).show(talk.Event(*acted("Entrusted.", once=once)))
    assert out.getvalue().count(code) == 1
    assert f"· Your code is {code}, once." in out.getvalue().splitlines()


def test_what_the_server_says_reaches_the_terminal_without_control_characters() -> None:
    out = io.StringIO()
    screen = talk.Screen(out)
    screen.show(
        talk.Event("words", {"text": "\x1b[31mred\x1b[0m\rover\x07\ttab\n", "message_id": "m"})
    )
    screen.show(talk.Event(*acted("\x1b]0;a title\x07Lamp\x9b2J on\x1b7.\x00")))
    screen.line("past\x1b[2Kline\x85")
    written = out.getvalue()
    assert written == "redover\ttab\n· Lamp on.\npastline\n"


def test_the_program_knows_no_outcome_of_the_server() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("proof_failed", "needs_proof"):
        for path in [*(root / "src").rglob("*.py"), *(root / "tests").rglob("*.py")]:
            if path == Path(__file__).resolve():
                continue
            assert name not in path.read_text("utf-8"), (name, path.name)


ALLOWED_ENGLISH = {
    # The line that is not a sentence of the conversation: command-line help (``--help``).
    "Talk with the assistant from a terminal.",
    "entrust this computer to the assistant (asks for a code)",
    "the https address of the assistant",
    "the ca.crt of the assistant, to trust his certificate",
    "the name of this computer, as the assistant will show it",
    "delete the secret saved on this computer",
    # Pairing and forgetting, before there is a person, a language or a secret (ruling R9).
    "This computer is not paired yet. Run: domo-talk pair <url>",
    "That code is not valid: it may be wrong, already used, or too old.",
    "That name cannot be used.",
    "Too many tries. Wait a little, then try again.",
    "Give the https address of the assistant, for example https://domo.example",
    "The assistant does not know this computer (any more). Pair it again: domo-talk pair <url>",
    "Cannot read the certificate file ",
    "This computer is now entrusted to the assistant. Run domo-talk to talk to him.",
    "The saved secret is deleted from this computer.",
    "Code from the assistant: ",
    # Said when no texts were ever fetched (nothing can be asked of the server).
    "Cannot reach the assistant at ",
    " His certificate is not trusted here: pair with --ca <path to his ca.crt>.",
}


def test_no_english_sentence_is_left_in_the_code_but_the_pairing_ones() -> None:
    import ast

    found: set[str] = set()
    for path in sorted((Path(__file__).resolve().parents[1] / "src" / "domo_talk").glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and re.search(r"[A-Za-z]{2,} [A-Za-z]{2,}", node.value)
                and not node.value.startswith("\\b")  # the pattern that finds Domo's name
            ):
                found.add(node.value)
    assert found == ALLOWED_ENGLISH


def test_a_sentence_of_the_program_says_the_name_the_person_gave_domo() -> None:
    # F8 (the owner's trial of 0.9.0): where the program's own sentence names Domo.
    out, err = io.StringIO(), io.StringIO()
    screen = talk.Screen(out, err)
    screen.problem("The assistant cannot take that message.")
    screen.domo_name = "Friday"
    screen.problem("The assistant does not know this computer. Pair it again: domo-talk pair <url>")
    assert err.getvalue().splitlines() == [
        "The assistant cannot take that message.",
        "Friday does not know this computer. Pair it again: domo-talk pair <url>",
    ]
