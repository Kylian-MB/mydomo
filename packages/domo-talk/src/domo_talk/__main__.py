"""``domo-talk``: pair this computer with Domo, talk to him, or forget the pairing."""

import argparse
import contextlib
import io
import sys
from collections.abc import Sequence
from pathlib import Path

from domo_talk import config, talk
from domo_talk.client import TalkError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domo-talk", description="Talk with the assistant from a terminal."
    )
    commands = parser.add_subparsers(dest="command", metavar="command")
    pair = commands.add_parser(
        "pair", help="entrust this computer to the assistant (asks for a code)"
    )
    pair.add_argument("url", help="the https address of the assistant")
    pair.add_argument(
        "--ca", metavar="PATH", help="the ca.crt of the assistant, to trust his certificate"
    )
    pair.add_argument("--name", help="the name of this computer, as the assistant will show it")
    commands.add_parser("forget", help="delete the secret saved on this computer")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the program; the value is the exit code (2 when Domo does not know this computer)."""
    arguments = _parser().parse_args(argv)
    stream: object = sys.stdout
    if isinstance(stream, io.TextIOWrapper):
        # A terminal that cannot show a character must not stop the program.
        with contextlib.suppress(OSError, ValueError):
            stream.reconfigure(errors="replace")
    screen = talk.Screen()
    try:
        if arguments.command == "pair":
            ca = None if arguments.ca is None else str(Path(arguments.ca).resolve())
            return talk.pair(arguments.url, ca, arguments.name, screen)
        if arguments.command == "forget":
            return talk.forget(screen)
        saved = config.load()
        if saved is None:
            screen.problem("This computer is not paired yet. Run: domo-talk pair <url>")
            return 1
        return talk.converse(saved, screen)
    except TalkError as problem:
        screen.problem(str(problem))
        return problem.exit_status
    except (EOFError, KeyboardInterrupt):
        return 130


if __name__ == "__main__":
    sys.exit(main())
