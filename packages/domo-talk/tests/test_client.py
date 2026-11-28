"""The client of the channel of the API: the saved secret, the reading of a stream cut anywhere,
and each call with ``respx``."""

import json
import os
import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from domo_talk import config
from domo_talk.client import (
    Client,
    Event,
    Message,
    Past,
    Proved,
    Refused,
    SseParser,
    TalkError,
    Unpaired,
    Unreachable,
)
from domo_talk.config import Saved

URL = "https://domo.test"
API = f"{URL}/api/channel"
SECRET = "s3cr3t-of-this-device"
SAVED = Saved(url=URL, secret=SECRET, ca=None)
REPLY = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's configuration folder, in a temporary place: never the real one."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- the saved secret ---------------------------------------------------------------------------


def test_what_is_saved_is_loaded_again_and_forgotten() -> None:
    assert config.load() is None
    config.save(Saved(url=URL, secret=SECRET, ca="/some/ca.crt"))
    assert config.load() == Saved(url=URL, secret=SECRET, ca="/some/ca.crt")
    config.forget()
    assert config.load() is None
    config.forget()  # nothing left to forget is not an error


def test_the_file_is_in_the_configuration_folder_of_the_user(home: Path) -> None:
    config.save(SAVED)
    assert config.config_path().is_relative_to(home)
    assert json.loads(config.config_path().read_text("utf-8"))["secret"] == SECRET


def test_the_file_is_created_with_the_mode_0600_from_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the system has no modes to read back, the mode asked for is still checked."""
    asked: list[int] = []
    real = os.open

    def spy(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        asked.append(mode)
        return real(path, flags, mode)

    monkeypatch.setattr(os, "open", spy)
    config.save(SAVED)
    assert asked == [0o600]


@pytest.mark.skipif(os.name == "nt", reason="this system has no file modes")
def test_the_file_that_is_kept_has_the_mode_0600() -> None:
    config.save(SAVED)
    assert config.config_path().stat().st_mode & 0o777 == 0o600
    config.save(SAVED)  # saved again: still tight
    assert config.config_path().stat().st_mode & 0o777 == 0o600


def test_a_file_that_cannot_be_read_is_not_a_saved_secret() -> None:
    config.save(SAVED)
    config.config_path().write_text("not json", "utf-8")
    assert config.load() is None
    config.config_path().write_text('{"url": "x"}', "utf-8")
    assert config.load() is None


def test_the_secret_is_not_in_what_a_saved_one_says_of_itself() -> None:
    assert SECRET not in repr(SAVED)


# --- the reading of a stream --------------------------------------------------------------------

STREAM = (
    'event: words\ndata: {"text":"Bonjour, été ☃ \U0001f600"}\n\n'
    ": still here\n\n"
    'event: acted\r\ndata: {"sentence":"a"}\r\n\r\n'
    'event: words\ndata: {"text":\ndata: "two lines"}\n\n'
    "data: no name\n\n"
    'event: ended\ndata: {"status":"done"}\n\n'
).encode()
EVENTS = [
    ("words", '{"text":"Bonjour, été ☃ \U0001f600"}'),
    ("acted", '{"sentence":"a"}'),
    ("words", '{"text":\n"two lines"}'),
    ("message", "no name"),
    ("ended", '{"status":"done"}'),
]


def raw_events(*chunks: bytes) -> list[tuple[str, str]]:
    parser = SseParser()
    return [item for chunk in chunks for item in parser.feed(chunk)]


def test_a_stream_is_read_as_its_events() -> None:
    assert raw_events(STREAM) == EVENTS


def test_a_stream_cut_anywhere_is_read_the_same() -> None:
    for cut in range(len(STREAM) + 1):
        assert raw_events(STREAM[:cut], STREAM[cut:]) == EVENTS, cut


def test_a_stream_given_byte_by_byte_is_read_the_same() -> None:
    assert raw_events(*(bytes([b]) for b in STREAM)) == EVENTS


def test_a_stream_cut_in_three_is_read_the_same() -> None:
    for first in range(0, len(STREAM), 7):
        for second in range(first, len(STREAM), 11):
            assert raw_events(STREAM[:first], STREAM[first:second], STREAM[second:]) == EVENTS


def test_an_event_that_is_not_finished_is_not_given() -> None:
    assert raw_events(b'event: words\ndata: {"text":"a"}\n') == []
    assert raw_events(b'event: words\ndata: {"text":"a"}') == []


def test_a_data_field_keeps_what_follows_one_space() -> None:
    assert raw_events(b"data:  two spaces\n\ndata:none\n\ndata\n\n") == [
        ("message", " two spaces"),
        ("message", "none"),
        ("message", ""),
    ]


def test_a_lone_carriage_return_ends_a_line_too() -> None:
    # A final CR waits for the next byte: it may be the first half of CRLF.
    assert raw_events(b"event: a\rdata: b\r\r") == []
    assert raw_events(b"event: a\rdata: b\r\rx") == [("a", "b")]
    assert raw_events(b"event: a\r", b"\ndata: b\r", b"\n\r", b"\n") == [("a", "b")]


def test_the_name_of_an_event_does_not_go_on_to_the_next() -> None:
    assert raw_events(b"event: a\ndata: 1\n\ndata: 2\n\n") == [("a", "1"), ("message", "2")]


def test_a_byte_that_is_not_text_does_not_stop_the_reading() -> None:
    assert raw_events(b"event: a\ndata: \xff\n\n") == [("a", "�")]


class ByteByByte(httpx.SyncByteStream):
    """A response body that arrives one byte at a time."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __iter__(self) -> Iterator[bytes]:
        for b in self._body:
            yield bytes([b])


def sse_response(body: bytes, *, byte_by_byte: bool = True) -> httpx.Response:
    headers = {"content-type": "text/event-stream; charset=utf-8"}
    if byte_by_byte:
        return httpx.Response(200, headers=headers, stream=ByteByByte(body))
    return httpx.Response(200, headers=headers, content=body)


# --- the calls ----------------------------------------------------------------------------------


@respx.mock
def test_pairing_gives_the_secret_and_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.post(f"{API}/pair").respond(
        json={"device_id": "d", "secret": SECRET, "person": "Alex"}
    )
    saved = Client.pair(URL + "/", "123456", "my computer", None)
    assert saved == Saved(url=URL, secret=SECRET, ca=None)
    assert json.loads(route.calls.last.request.content) == {
        "code": "123456",
        "name": "my computer",
    }
    assert "authorization" not in route.calls.last.request.headers
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


@respx.mock
def test_pairing_with_a_code_that_is_refused_says_so() -> None:
    respx.post(f"{API}/pair").respond(404, json={"error": "unknown_code"})
    with pytest.raises(Refused) as caught:
        Client.pair(URL, "000000", "x", None)
    assert caught.value.error == "unknown_code"


@respx.mock
def test_pairing_with_an_answer_that_is_not_one_is_refused() -> None:
    respx.post(f"{API}/pair").respond(200, json={"nothing": 1})
    with pytest.raises(TalkError):
        Client.pair(URL, "1", "x", None)


@respx.mock
def test_saying_posts_with_the_secret_then_reads_the_stream() -> None:
    post = respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    stream = respx.get(f"{API}/stream/{REPLY}").mock(return_value=sse_response(STREAM))
    events = list(Client(SAVED).say("Hello"))
    assert json.loads(post.calls.last.request.content) == {"text": "Hello"}
    assert post.calls.last.request.headers["authorization"] == f"Bearer {SECRET}"
    assert stream.calls.last.request.headers["authorization"] == f"Bearer {SECRET}"
    assert [e.name for e in events] == ["words", "acted", "words", "ended"]
    assert events[0] == Event("words", {"text": "Bonjour, été ☃ \U0001f600"})
    assert events[2].data == {"text": "two lines"}


@respx.mock
def test_a_reply_that_is_over_or_written_elsewhere_gives_no_event() -> None:
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").respond(204)
    assert list(Client(SAVED).say("Hello")) == []


@respx.mock
def test_an_event_whose_data_is_not_an_object_is_skipped() -> None:
    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    body = b"event: words\ndata: nope\n\nevent: words\ndata: [1]\n\nevent: ended\ndata: {}\n\n"
    respx.get(f"{API}/stream/{REPLY}").mock(return_value=sse_response(body, byte_by_byte=False))
    assert list(Client(SAVED).say("x")) == [Event("ended", {})]


@respx.mock
@pytest.mark.parametrize(
    ("status", "error"),
    [(409, "no_key"), (413, "too_long"), (400, "not_sayable")],
)
def test_a_message_that_is_refused_says_why(status: int, error: str) -> None:
    respx.post(f"{API}/messages").respond(status, json={"error": error})
    with pytest.raises(Refused) as caught:
        list(Client(SAVED).say("x"))
    assert caught.value.error == error
    assert caught.value.exit_status == 1


@respx.mock
def test_a_device_that_is_not_known_any_more_is_told_to_pair_again() -> None:
    respx.post(f"{API}/messages").respond(401, json={"error": "unknown_device"})
    with pytest.raises(Unpaired) as caught:
        list(Client(SAVED).say("x"))
    assert caught.value.exit_status == 2
    assert "pair" in str(caught.value).lower()
    assert SECRET not in str(caught.value)


@respx.mock
def test_every_call_tells_a_device_that_is_not_known_any_more() -> None:
    respx.get(f"{API}/since").respond(401, json={"error": "unknown_device"})
    respx.get(f"{API}/listen").respond(401, json={"error": "unknown_device"})
    respx.post(f"{API}/confirmations/{REPLY}/prove").respond(401, json={"error": "unknown_device"})
    client = Client(SAVED)
    with pytest.raises(Unpaired):
        client.since(None)
    with pytest.raises(Unpaired):
        list(client.listen())
    with pytest.raises(Unpaired):
        client.prove(REPLY, "password", "pw")


@respx.mock
def test_since_gives_the_messages_after_one() -> None:
    route = respx.get(f"{API}/since").respond(
        json={
            "messages": [
                {"id": "a", "role": "user", "text": "Hi", "cards": []},
                {"id": "b", "role": "assistant", "text": "Hello", "cards": ["Done."]},
            ]
        }
    )
    messages = Client(SAVED).since(None)
    assert messages == [
        Message("a", "user", "Hi", ()),
        Message("b", "assistant", "Hello", ("Done.",)),
    ]
    assert "after" not in route.calls.last.request.url.params
    Client(SAVED).since("a")
    assert route.calls.last.request.url.params["after"] == "a"


@respx.mock
def test_the_past_says_the_name_the_person_calls_domo_by() -> None:
    route = respx.get(f"{API}/since")
    route.respond(json={"messages": [], "domo_name": "Brenda"})
    assert Client(SAVED).past(None) == Past([], "Brenda")
    for none in (None, "", 3):
        route.respond(json={"messages": [], "domo_name": none})
        assert Client(SAVED).past(None).domo_name is None
    route.respond(json={"messages": []})
    assert Client(SAVED).past(None).domo_name is None


@respx.mock
def test_listening_reads_what_domo_says_on_his_own() -> None:
    body = b'event: words\ndata: {"text":"Hi","message_id":"m"}\n\n: still here\n\n'
    respx.get(f"{API}/listen").mock(return_value=sse_response(body))
    assert list(Client(SAVED).listen()) == [Event("words", {"text": "Hi", "message_id": "m"})]


@respx.mock
def test_a_proof_is_sent_for_its_request_and_the_password_goes_nowhere_else() -> None:
    route = respx.post(f"{API}/confirmations/{REPLY}/prove").respond(
        json={"outcome": "approved", "sentence": "Done."}
    )
    proved = Client(SAVED).prove(REPLY, "password", "hunter2")
    assert proved == Proved(outcome="approved", sentence="Done.")
    assert json.loads(route.calls.last.request.content) == {
        "means": "password",
        "answer": "hunter2",
    }


@respx.mock
def test_what_a_proof_runs_shows_once_is_read_and_never_in_a_representation() -> None:
    once = {"kind": "link", "value": "https://domo.test/join/xyz", "sentence": "Once."}
    respx.post(f"{API}/confirmations/{REPLY}/prove").respond(
        json={"outcome": "executed", "sentence": "Done.", "once": once, "request": None}
    )
    proved = Client(SAVED).prove(REPLY, "password", "hunter2")
    assert proved.once == once
    assert "join/xyz" not in repr(proved)


@respx.mock
def test_a_proof_that_is_refused_never_says_the_password() -> None:
    respx.post(f"{API}/confirmations/{REPLY}/prove").respond(429, json={"error": "held_back"})
    with pytest.raises(Refused) as caught:
        Client(SAVED).prove(REPLY, "password", "hunter2")
    assert caught.value.error == "held_back"
    assert "hunter2" not in str(caught.value) + repr(caught.value)


@respx.mock
def test_a_server_that_cannot_be_reached_is_said_plainly() -> None:
    respx.post(f"{API}/messages").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(Unreachable) as caught:
        list(Client(SAVED).say("x"))
    assert URL in str(caught.value)
    assert SECRET not in str(caught.value)


@respx.mock
def test_a_stream_that_breaks_is_said_plainly() -> None:
    class Breaking(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'event: words\ndata: {"text":"a"}\n\n'
            raise httpx.ReadError("cut")

    respx.post(f"{API}/messages").respond(json={"reply_id": REPLY})
    respx.get(f"{API}/stream/{REPLY}").mock(return_value=httpx.Response(200, stream=Breaking()))
    seen: list[Event] = []
    with pytest.raises(Unreachable):
        for event in Client(SAVED).say("x"):
            seen.append(event)
    assert seen == [Event("words", {"text": "a"})]


# --- the verification of the certificate --------------------------------------------------------


def test_the_certificate_is_always_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    real = httpx.Client

    def spy(**kwargs: object) -> httpx.Client:
        seen.append(kwargs.get("verify", True))
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", spy)
    Client(SAVED)
    assert seen == [True]


def test_the_authority_of_domo_is_the_one_that_is_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[str | None] = []
    made = ssl.create_default_context()

    def fake(*, cafile: str | None = None) -> ssl.SSLContext:
        asked.append(cafile)
        return made

    monkeypatch.setattr(ssl, "create_default_context", fake)
    ca = str(tmp_path / "ca.crt")
    Client(Saved(url=URL, secret=SECRET, ca=ca))
    assert asked == [ca]


def test_a_certificate_file_that_cannot_be_read_is_said_plainly(tmp_path: Path) -> None:
    with pytest.raises(TalkError) as caught:
        Client(Saved(url=URL, secret=SECRET, ca=str(tmp_path / "nothing.crt")))
    assert "certificate" in str(caught.value).lower()


def test_a_secret_is_not_sent_over_plain_http_to_another_machine() -> None:
    with pytest.raises(TalkError):
        Client(Saved(url="http://domo.example", secret=SECRET, ca=None))
    with pytest.raises(TalkError):
        Client.pair("http://domo.example", "1", "x", None)
    Client(Saved(url="http://localhost:8000", secret=SECRET, ca=None))


# --- fix round 1 --------------------------------------------------------------------------------


@respx.mock
def test_a_message_kept_while_a_reply_is_written_gives_no_event() -> None:
    # Spec 11.3: no stream of its own; what answers it comes on the listening stream.
    respx.post(f"{API}/messages").respond(202, json={"reply_id": None, "kept": True})
    stream = respx.get(url__startswith=f"{API}/stream/")
    client = Client(SAVED)
    assert list(client.say("x")) == []
    assert not stream.called and client.last_reply is None


@respx.mock
def test_busy_on_a_proof_says_too_many_passwords_are_being_checked() -> None:
    respx.post(f"{API}/confirmations/{REPLY}/prove").respond(503, json={"error": "busy"})
    with pytest.raises(Refused) as caught:
        Client(SAVED).prove(REPLY, "password", "hunter2")
    text = str(caught.value)
    assert text == "[busy]"  # no texts held: nothing of its own
    assert "hunter2" not in text


@respx.mock
def test_busy_on_a_proof_says_the_servers_sentence() -> None:
    respx.get(f"{API}/texts").respond(
        json={"language": "fr", "texts": {"talk.proof.busy": "Trop de mots de passe."}}
    )
    respx.post(f"{API}/confirmations/{REPLY}/prove").respond(503, json={"error": "busy"})
    client = Client(SAVED)
    assert client.texts() == {"talk.proof.busy": "Trop de mots de passe."}
    assert client.language == "fr"
    with pytest.raises(Refused) as caught:
        client.prove(REPLY, "password", "hunter2")
    assert str(caught.value) == "Trop de mots de passe."


@respx.mock
def test_the_texts_are_fetched_with_the_secret_and_never_fail() -> None:
    route = respx.get(f"{API}/texts").mock(
        side_effect=[
            httpx.Response(404, json={"error": "not_found"}),
        ]
    )
    client = Client(SAVED)
    assert client.texts() == {}
    assert client.texts() == {}  # an older server is not asked again
    assert route.call_count == 1
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SECRET}"
    unreachable = respx.get(f"{API}/texts").mock(side_effect=httpx.ConnectError("boom"))
    other = Client(SAVED)
    assert other.texts() == {}
    assert unreachable.called
    assert other.text("talk.too_long") is None


@respx.mock
def test_cannot_reach_says_the_servers_sentence_once_it_was_fetched() -> None:
    respx.get(f"{API}/texts").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "language": "fr",
                    "texts": {
                        "talk.unreachable": "Injoignable à {url}.",
                        "talk.not_trusted": "Non.",
                    },
                },
            ),
            httpx.ConnectError("CERTIFICATE_VERIFY_FAILED"),
        ]
    )
    client = Client(SAVED)
    client.texts()
    assert str(Unreachable(URL, ssl.SSLError("CERTIFICATE"), client.known_texts)) == (
        f"Injoignable à {URL}. Non."
    )
    assert str(Unreachable(URL, OSError("x"))).startswith("Cannot reach the assistant")


def test_a_failed_write_leaves_no_temporary_file_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        config.save(SAVED)
    assert list(config.config_dir().iterdir()) == []


@respx.mock
def test_new_texts_replace_the_old_whole_and_both_connections_read_them() -> None:
    respx.get(f"{API}/texts").mock(
        side_effect=[
            httpx.Response(200, json={"language": "fr", "texts": {"talk.too_long": "Long."}}),
            httpx.Response(200, json={"language": "en", "texts": {"talk.too_long": "Too long."}}),
        ]
    )
    client = Client(SAVED)
    hearing = Client(SAVED, client.held)
    client.texts()
    before = hearing.known_texts
    client.texts()
    # The dictionary a reader holds is never emptied or changed: the new one is put in its place.
    assert before == {"talk.too_long": "Long."}
    assert hearing.known_texts == {"talk.too_long": "Too long."}
    assert hearing.known_texts is not before
