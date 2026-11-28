"""The CUDA libraries a graphics card needs, installed as Python packages (``nvidia-*``, the extra
``gpu``), made findable by the runtimes on every system, not only on Windows."""

import ctypes
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from domo_speech.real import device


@pytest.fixture(autouse=True)
def fresh_search() -> Iterator[None]:
    # The search is done once per process (``@cache``): each test does it again.
    device.cuda_libraries.cache_clear()
    yield
    device.cuda_libraries.cache_clear()


def test_on_linux_the_libraries_of_the_nvidia_packages_are_loaded_for_the_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Linux's loader never looks in site-packages: CTranslate2 then fails at its first work with
    # "libcublas.so.12 is not found" (seen in the GPU image of the stack).
    for name in ("cublas/lib/libcublas.so.12", "cudnn/lib/libcudnn.so.9", "cudnn/lib/README"):
        path = tmp_path / "nvidia" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    loaded: list[tuple[str, int]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    monkeypatch.setattr(ctypes, "RTLD_GLOBAL", 256, raising=False)
    monkeypatch.setattr(ctypes, "CDLL", lambda name, mode=0: loaded.append((name, mode)))

    device.cuda_libraries()

    assert sorted(Path(name).name for name, _ in loaded) == ["libcublas.so.12", "libcudnn.so.9"]
    assert all(mode == 256 for _, mode in loaded)


def test_a_library_that_cannot_be_loaded_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nvidia" / "cublas" / "lib" / "libnvblas.so.12"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    tried: list[str] = []

    def refuse(name: str, mode: int = 0) -> None:
        tried.append(Path(name).name)
        raise OSError("cannot load")

    monkeypatch.setattr(ctypes, "CDLL", refuse)

    device.cuda_libraries()

    assert tried == ["libnvblas.so.12"]
