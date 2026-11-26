"""The program: its three commands and the conversation, against a Domo made with ``respx``."""

import io
import json
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


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


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
def test_a_request_that_waits_for_a_proof_offers_prove_and_the_password_is_never_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
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
    typed(monkeypatch, "delete my files", "/prove")
    assert main([]) == 0
    assert len(asked) == 1
    assert json.loads(prove.calls.last.request.content) == {
        "means": "password",
        "answer": PASSWORD,
    }
    out = capsys.readouterr()
    assert "type /prove" in out.out
    assert "Thank you, it is done." in out.out
    assert PASSWORD not in out.out + out.err
    assert PASSWORD not in config.config_path().read_text("utf-8")


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
def test_a_message_kept_while_a_reply_is_written_is_no_problem(
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
    assert capsys.readouterr().err.strip() == ""  # nothing to complain about


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
    post = respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})

    def read(prompt: str = "") -> str:
        wait_for(lambda: listen.called)
        time.sleep(0.3)  # the thread has said it by then
        return "hello"

    monkeypatch.setattr("builtins.input", read)
    assert main([]) == 2
    assert not post.called
    err = capsys.readouterr().err.lower()
    assert "pair" in err
    assert "enter" in err


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
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "delete my files", "and the weather?", "/prove", "/prove")
    assert main([]) == 0
    assert prove.call_count == 1  # then the request is over: nothing waits any more
    out = capsys.readouterr().out
    assert "Done." in out
    assert "Nothing waits for a proof here." in out


@respx.mock
def test_a_proof_that_fails_can_be_given_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save(SAVED)
    nothing_heard()
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
    typed(monkeypatch, "delete my files", "/prove", "/prove")
    assert main([]) == 0
    assert prove.call_count == 2


@respx.mock
def test_an_event_saying_the_request_has_no_proof_to_give_ends_the_offer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
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
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "delete my files", "what now", "/prove")
    assert main([]) == 0
    assert "Nothing waits for a proof here." in capsys.readouterr().out


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
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    prove = respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "whatever", "sentence": "Said.", "request": request_now}
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "delete my files", "/prove", "/prove")
    assert main([]) == 0
    assert prove.call_count == 1


@respx.mock
def test_what_a_proof_runs_shows_once_is_written_once_and_not_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save(SAVED)
    nothing_heard()
    respx.get(f"{API}/since").respond(json={"messages": []})
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(content=waiting(["password"]))
    link = "https://domo.test/join/xyz"
    once = {"kind": "link", "value": link, "sentence": "The link works once."}
    respx.post(f"{API}/confirmations/{REQUEST}/prove").respond(
        json={"outcome": "executed", "sentence": "Invited.", "once": once, "request": None}
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    typed(monkeypatch, "invite someone", "/prove")
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
