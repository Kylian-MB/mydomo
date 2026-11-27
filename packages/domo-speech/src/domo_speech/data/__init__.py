"""The candidates of the speech service (``models.json``): open models it may run, with their
licences and where their weights come from. Never a choice and never a machine's measure: the
service measures them where it runs (``domo_speech.measure``) and the assistant chooses."""

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Literal, cast

Does = Literal["hear", "say", "voice_print"]
Runs = Literal["imported", "separate_program"]
Needs = Literal["cpu", "gpu"]


@dataclass(frozen=True)
class FileData:
    """One file of a model's weights, pinned by its digest."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelEntryData:
    """One candidate of the data file."""

    id: str
    does: Does
    # Which code of ``domo_speech.real`` runs it: ``faster_whisper``, ``flow_voice``,
    # ``speaker_embedding`` or ``program`` (a separate program the service calls).
    engine: str
    source: str
    licence: str
    licence_url: str
    # True when the licence restricts how the model may be used (beyond attribution or sharing
    # alike): the owner may hide such candidates (``DOMO_SPEECH_RESTRICTED_MODELS=no``).
    restricted: bool
    code: str
    code_licence: str
    runs: Runs
    languages: tuple[str, ...]
    size_mb: int
    needs: Needs
    floor: bool
    repo: str
    revision: str
    files: tuple[FileData, ...]
    voices: tuple[str, ...] = ()
    # For a separate program: its arguments, ``{weights}`` standing for the model's directory.
    program_args: tuple[str, ...] = ()
    # For a voice print: the cosine similarities (print_low, same_voice, print_high).
    thresholds: tuple[float, float, float] | None = None


def _entry(raw: dict[str, object]) -> ModelEntryData:
    files = cast(list[dict[str, object]], raw["files"])
    thresholds = cast(dict[str, float] | None, raw.get("thresholds"))
    return ModelEntryData(
        id=str(raw["id"]),
        does=cast(Does, raw["does"]),
        engine=str(raw["engine"]),
        source=str(raw["source"]),
        licence=str(raw["licence"]),
        licence_url=str(raw["licence_url"]),
        restricted=bool(raw.get("restricted", False)),
        code=str(raw["code"]),
        code_licence=str(raw["code_licence"]),
        runs=cast(Runs, raw["runs"]),
        languages=tuple(cast(list[str], raw["languages"])),
        size_mb=int(cast(int, raw["size_mb"])),
        needs=cast(Needs, raw["needs"]),
        floor=bool(raw["floor"]),
        repo=str(raw["repo"]),
        revision=str(raw["revision"]),
        files=tuple(
            FileData(path=str(f["path"]), size=int(cast(int, f["size"])), sha256=str(f["sha256"]))
            for f in files
        ),
        voices=tuple(cast(list[str], raw.get("voices", []))),
        program_args=tuple(cast(list[str], raw.get("program_args", []))),
        thresholds=None
        if thresholds is None
        else (thresholds["print_low"], thresholds["same_voice"], thresholds["print_high"]),
    )


def offered(*, restricted: bool) -> tuple[ModelEntryData, ...]:
    """The candidates the service may try: all, or those whose licence restricts no use."""
    return tuple(entry for entry in candidates() if restricted or not entry.restricted)


@cache
def candidates() -> tuple[ModelEntryData, ...]:
    """Every candidate of the data file, in its order (floor first within a use)."""
    text = (resources.files("domo_speech.data") / "models.json").read_text(encoding="utf-8")
    raw = cast(dict[str, list[dict[str, object]]], json.loads(text))
    return tuple(_entry(item) for item in raw["models"])
