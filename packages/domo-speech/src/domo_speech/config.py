"""The speech service's configuration, from ``DOMO_SPEECH_*`` environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SpeechSettings(BaseSettings):
    """Settings of the speech service."""

    # A setting left empty is unset: the compose file passes each one, empty when not set.
    model_config = SettingsConfigDict(
        env_prefix="DOMO_SPEECH_", extra="ignore", frozen=True, env_ignore_empty=True
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8083, ge=1, le=65535)
    # The only place the service writes: the weights it fetches and its measure of this machine.
    # The owner picks the disk (it holds a few gigabytes once a graphics card is found).
    model_dir: Path = Path("/models")
    device: Literal["auto", "cpu", "gpu"] = "auto"
    engines: Literal["real", "fake"] = "real"
    # A voice that runs as a separate program (a GPL one, for one): the program to start. Its
    # candidates are tried only when it is set.
    voice_program: Path | None = None
    # Whether candidates whose licence restricts their use (``restricted`` in the data file) are
    # offered at all: ``no`` hides them from the measure and from ``/v1/models``.
    restricted_models: bool = True
    # The most requests one model serves at once. The service measures how many it holds on
    # this machine; this can only lower that number, never raise it. Unset: what was measured.
    at_once: int | None = Field(default=None, ge=1)
