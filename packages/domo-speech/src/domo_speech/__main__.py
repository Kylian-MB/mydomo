"""Entry point of the speech service: ``domo-speech``."""

import importlib
import importlib.util
import logging
import sys

import uvicorn
from pydantic import ValidationError

from domo_speech.app import create_app
from domo_speech.config import SpeechSettings
from domo_speech.engines import Engines
from domo_speech.fakes import fake_engines

# What the real engines run on (the extra ``models``).
RUNTIMES = ("ctranslate2", "faster_whisper", "onnxruntime")


def build_engines(settings: SpeechSettings) -> Engines:
    """The fake engines, or the real ones (``domo_speech.real``, loaded only now: they need the
    optional extra ``models``). Exits with one plain line when they are not installed."""
    if settings.engines == "fake":
        return fake_engines()
    if any(importlib.util.find_spec(name) is None for name in RUNTIMES):
        sys.exit("domo-speech: the speech models are not installed (install the 'models' extra).")
    real = importlib.import_module("domo_speech.real")
    engines: Engines = real.real_engines(settings)
    return engines


def server_config(settings: SpeechSettings, engines: Engines) -> uvicorn.Config:
    """No access log: a request line is not worth a trace of who spoke when."""
    return uvicorn.Config(
        create_app(engines),
        host=settings.host,
        port=settings.port,
        access_log=False,
        server_header=False,
        log_config=None,
        log_level="info",
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = SpeechSettings()
    except ValidationError as exc:
        names = ", ".join(
            "DOMO_SPEECH_" + ".".join(str(part) for part in detail["loc"]).upper()
            for detail in exc.errors(include_url=False, include_input=False, include_context=False)
        )
        sys.exit(f"domo-speech: invalid configuration: {names}")
    uvicorn.Server(server_config(settings, build_engines(settings))).run()


if __name__ == "__main__":
    main()
