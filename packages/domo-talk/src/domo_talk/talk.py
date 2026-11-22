"""The conversation in the terminal. It shows the sentences the server gives and sends what the
person types; it decides nothing. The only thing it offers on its own is ``/prove``, and only when
the server said that a proof can be given here.
"""

import contextlib
import getpass
import platform
import re
import sys
import threading
from typing import Any, Final, TextIO

from domo_talk import config
from domo_talk.client import Client, Event, Message, Refused, TalkError, Unpaired, Unreachable
from domo_talk.config import Saved

# The mark of a card, written on its own line.
CARD = "·"
PROMPT = "> "
# How long the listening waits before it tries again, in seconds.
RETRY_SECONDS = 5.0

# What a terminal would act on rather than show: the sequences an escape begins (a control
# sequence, an operating system command, a string, or one character after the escape), their C1
# forms, then every other control character but the line feed and the tab.
_ESCAPES: Final = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[ -~]?"
)
_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


# Domo named in a sentence of the program: the word, never the program's own name.
_DOMO: Final = re.compile(r"\b[Tt]he assistant\b")


def plain(text: str) -> str:
    """``text`` as the terminal may be given it: nothing in it that the terminal would act on
    (``_ESCAPES``, ``_CONTROLS``), so that what the server says is only ever shown."""
    return _CONTROLS.sub("", _ESCAPES.sub("", text))


class Screen:
    """What is written for the person. Several threads write (the reply being read, and what
    Domo says on his own), so each write holds a lock; a line that is being written (the words
    of a reply) is ended before anything else is put after it."""

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self._out = out
        self._err = err
        self._lock = threading.Lock()
        self._mid_line = False
        self._speaking: str | None = None
        self._proof: tuple[str, str] | None = None
        # The prompt is waiting for the person's line, and whether it was broken off by a line
        # written meanwhile (it is then shown again once that line is done).
        self._at_prompt = False
        self._broken = False
        # The name the person calls Domo by, written before his words (``None``: nothing is
        # written, he is Domo), and whether the reply being written has it already.
        self.domo_name: str | None = None
        self._named = False

    @property
    def out(self) -> TextIO:
        return self._out if self._out is not None else sys.stdout

    @property
    def err(self) -> TextIO:
        if self._err is not None:
            return self._err
        return sys.stderr if self._out is None else self._out

    @property
    def proof(self) -> tuple[str, str] | None:
        """The request that waits for a proof which can be given here: its identifier and the
        means to give it with."""
        return self._proof

    def forget_proof(self) -> None:
        self._proof = None

    def waiting_at_prompt(self, waiting: bool) -> None:
        """Tell that the prompt waits (or no longer waits) for the person's line."""
        with self._lock:
            self._at_prompt = waiting
            self._broken = False

    def ask(self) -> str:
        """Show the prompt and read the person's line."""
        self.waiting_at_prompt(True)
        try:
            return input(PROMPT)
        finally:
            self.waiting_at_prompt(False)

    def line(self, text: str) -> None:
        with self._lock:
            self._begin()
            self._line(text)
            self._settle()

    def problem(self, text: str) -> None:
        """A sentence of the program itself, on the error output. Where it names Domo, it says
        the name the person calls him by, when they gave him one (2026-11-16, the owner's trial of
        0.9.0); the program's own name (``domo-talk``) stays."""
        name = self.domo_name
        if name:
            text = _DOMO.sub(lambda _: name, text)
        with self._lock:
            self._begin()
            self._end_line()
            self._write(f"{text}\n", self.err)
            self._settle()

    def shown_once(self, once: object) -> None:
        """Write what is shown only once (``{"kind", "value", "sentence"}``), as an ``acted``
        event does: after the answer to a proof, whose request made it."""
        with self._lock:
            self._begin()
            self._once(once)
            self._settle()

    def show(self, event: Event) -> None:
        """Write an event as the server gave it."""
        with self._lock:
            self._begin()
            speaker = event.data.get("message_id")
            if isinstance(speaker, str) and speaker != self._speaking:
                self._end_line()
                self._speaking = speaker
                self._named = False
            match event.name:
                case "words":
                    self._words(_text(event.data, "text"))
                case "acted":
                    self._acted(event.data)
                case "failed":
                    self._line(_text(event.data, "sentence"))
                case "ended":
                    self._end_line()
                    self._speaking = None
                    self._named = False
                case _:
                    pass
            self._settle()

    def end(self) -> None:
        with self._lock:
            self._end_line()

    def _begin(self) -> None:
        """What is written while the prompt waits starts on a line of its own."""
        if self._at_prompt and not self._broken and not self._mid_line:
            self.out.write("\n")
            self.out.flush()
            self._broken = True

    def _settle(self) -> None:
        """Once what was written has ended its line, the prompt is shown again."""
        if self._at_prompt and self._broken and not self._mid_line:
            self.out.write(PROMPT)
            self.out.flush()
            self._broken = False

    def _write(self, text: str, to: TextIO) -> None:
        """Every text the screen is given is written here, made plain (``plain``): what comes from
        the server is only ever shown."""
        to.write(plain(text))
        to.flush()

    def _words(self, text: str) -> None:
        text = plain(text)
        if text:
            if self.domo_name and not self._named:
                # His words begin: the name the person calls him by, once per reply.
                self._end_line()
                text = f"{self.domo_name}: {text}"
                self._named = True
            self._write(text, self.out)
            self._mid_line = not text.endswith("\n")

    def _end_line(self) -> None:
        if self._mid_line:
            self.out.write("\n")
            self.out.flush()
            self._mid_line = False

    def _line(self, text: str) -> None:
        self._end_line()
        if text:
            self._write(f"{text}\n", self.out)

    def _once(self, once: object) -> None:
        """What is shown once is written once, here, and kept nowhere: its sentence, then its
        value on a line of its own unless the sentence already holds it (a code)."""
        if not isinstance(once, dict):
            return
        shown: dict[str, Any] = once  # pyright: ignore[reportUnknownVariableType]
        sentence, value = _text(shown, "sentence"), _text(shown, "value")
        if sentence:
            self._line(f"{CARD} {sentence}")
        if value and value not in sentence:
            self._line(f"  {value}")

    def _acted(self, data: dict[str, Any]) -> None:
        sentence = _text(data, "sentence")
        if sentence:
            self._line(f"{CARD} {sentence}")
        self._once(data.get("once"))
        request = data.get("request")
        if isinstance(request, dict):
            asked: dict[str, Any] = request  # pyright: ignore[reportUnknownVariableType]
            proofs = asked.get("proofs")
            kinds = proofs if isinstance(proofs, list) else []  # pyright: ignore[reportUnknownVariableType]
            if asked.get("answer_with") == "proof" and "password" in kinds and asked.get("id"):
                self._proof = (str(asked["id"]), "password")
                self._line("(type /prove to give your password here)")
            elif self._proof is not None and asked.get("id") == self._proof[0]:
                self._proof = None  # the server says this request has no proof to give any more


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def show_past(screen: Screen, messages: list[Message]) -> None:
    """The recent conversation, as Domo keeps it: his words after the name the person calls him
    by, when they gave him one (``Screen.domo_name``)."""
    for message in messages:
        if message.role == "user":
            screen.line(f"{PROMPT}{message.text}")
        elif screen.domo_name and message.text:
            screen.line(f"{screen.domo_name}: {message.text}")
        else:
            screen.line(message.text)
        for card in message.cards:
            screen.line(f"{CARD} {card}")


class Listening:
    """What Domo says on his own, written between two prompts while the program is open. A
    thread of its own keeps the connection; it tries again when the server cannot be reached, and
    stops for good when Domo no longer knows this computer."""

    def __init__(self, client: Client, screen: Screen, *, retry: float = RETRY_SECONDS) -> None:
        self._client = client
        self._screen = screen
        self._retry = retry
        self._stopped = threading.Event()
        self._unpaired = threading.Event()
        self._thread = threading.Thread(target=self._run, name="listening", daemon=True)

    @property
    def unpaired(self) -> bool:
        """Domo told this thread that he does not know this computer."""
        return self._unpaired.is_set()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                for event in self._client.listen():
                    if self._stopped.is_set():
                        return
                    self._screen.show(event)
                    if event.name == "ended":
                        # What he said on his own may have come with a new name.
                        reply = event.data.get("message_id")
                        if isinstance(reply, str):
                            follow_the_name(self._client, self._screen, reply)
            except Unpaired as unpaired:
                self._unpaired.set()
                self._screen.problem(f"{unpaired} Press Enter to leave.")
                return
            except TalkError:
                pass  # the server could not be reached, or refused: try again
            self._stopped.wait(self._retry)


def pair(url: str, ca: str | None, name: str | None, screen: Screen) -> int:
    """Ask the code Domo showed, trade it for the secret of this computer, keep the secret."""
    code = input("Code from the assistant: ").strip()
    saved = Client.pair(url, code, name or platform.node() or "computer", ca)
    config.save(saved)
    screen.line("This computer is now entrusted to the assistant. Run domo-talk to talk to him.")
    return 0


def forget(screen: Screen) -> int:
    config.forget()
    screen.line("The saved secret is deleted from this computer.")
    return 0


def prove(client: Client, screen: Screen) -> None:
    """Give the proof the server asked for: the password is read without echo, sent in the body of
    the proof, and written nowhere."""
    waiting = screen.proof
    if waiting is None:
        screen.line("Nothing waits for a proof here.")
        return
    confirmation_id, means = waiting
    answer = getpass.getpass("Password: ")
    proved = client.prove(confirmation_id, means, answer)
    # The server says how the request is answered now; the offer stays only while it says that
    # this very request still waits for a proof that can be given here.
    if not proved.waits_for(confirmation_id, means):
        screen.forget_proof()
    screen.line(proved.sentence or proved.outcome)
    screen.shown_once(proved.once)


def follow_the_name(client: Client, screen: Screen, after: str | None = None) -> None:
    """Once a reply is over (the last one ``client`` read, or ``after``: one Domo said on his
    own), read again the name the person calls Domo by (they may have just given him one, or
    called him Domo again): the next replies follow it. A failure keeps the name as it was; the
    next reply tries again."""
    with contextlib.suppress(TalkError):
        screen.domo_name = client.past(after or client.last_reply).domo_name


def converse(saved: Saved, screen: Screen) -> int:
    """The conversation: the recent discussion, then a prompt, until the person leaves."""
    with Client(saved) as client, Client(saved) as hearing:
        past = client.past(None)
        screen.domo_name = past.domo_name
        show_past(screen, past.messages)
        listening = Listening(hearing, screen)
        listening.start()
        try:
            return _talk(client, screen, listening)
        finally:
            listening.stop()


def _talk(client: Client, screen: Screen, listening: Listening) -> int:
    while True:
        try:
            line = screen.ask().strip()
        except (EOFError, KeyboardInterrupt):
            screen.line("")
            return 0
        if listening.unpaired:
            return Unpaired.exit_status  # nothing is sent: Domo would refuse it
        if not line:
            continue
        try:
            if line == "/quit":
                return 0
            if line == "/prove":
                prove(client, screen)
                follow_the_name(client, screen)
            elif line.startswith("/"):
                screen.line("Unknown command. There are /prove and /quit.")
            else:
                for event in client.say(line):
                    screen.show(event)
                screen.end()
                follow_the_name(client, screen)
        except (Refused, Unreachable) as problem:
            screen.problem(str(problem))
