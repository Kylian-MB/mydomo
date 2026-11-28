"""Which graphics card the runtimes see, the libraries they need to use it, and how much of its
memory is used.

The runtimes are imported only here and in the engines, by name (``importlib``): they come with
the optional extra ``models`` and the service starts without them.
"""

import ctypes
import importlib
import os
import sys
from functools import cache
from pathlib import Path
from typing import Any


def runtime(name: str) -> Any:
    """A runtime of the extra ``models``, imported now."""
    return importlib.import_module(name)


@cache
def cuda_libraries() -> None:
    """The CUDA libraries installed as Python packages (``nvidia-*``) are on no system's search
    path. On Windows their folders are added to it; elsewhere (Linux) each library is loaded now,
    shared with what loads later (``RTLD_GLOBAL``), so the runtime that asks for it by name finds
    it already there. A library that cannot be loaded is left: the runtime then says what it
    misses."""
    if sys.platform != "win32":
        for folder in map(Path, sys.path):
            for library in sorted((folder / "nvidia").glob("*/lib/lib*.so*")):
                try:
                    ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    continue
        return
    for folder in map(Path, sys.path):
        nvidia = folder / "nvidia"
        if nvidia.is_dir():
            for library in nvidia.glob("*/bin"):
                os.add_dll_directory(str(library))
                os.environ["PATH"] = f"{library}{os.pathsep}{os.environ.get('PATH', '')}"


def cuda_devices() -> int:
    """How many graphics cards CTranslate2 can use (0 without the runtime or a card)."""
    try:
        cuda_libraries()
        return int(runtime("ctranslate2").get_cuda_device_count())
    except (ImportError, OSError, RuntimeError):
        return 0


def onnx_providers(gpu: bool) -> list[str]:
    """The ONNX Runtime providers to use: the CUDA one first when asked for and present."""
    available = set(runtime("onnxruntime").get_available_providers())
    if gpu and "CUDAExecutionProvider" in available:
        cuda_libraries()
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


class _Memory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


@cache
def _management() -> Any:
    """The graphics card driver's management library (part of the driver, beside every CUDA
    card), set up once; ``None`` without it."""
    name = "nvml.dll" if sys.platform == "win32" else "libnvidia-ml.so.1"
    try:
        library: Any = ctypes.CDLL(name)
        if library.nvmlInit_v2() != 0:
            return None
    except (OSError, AttributeError):
        return None
    return library


def gpu_memory_mb() -> tuple[float, float] | None:
    """(used, total) memory of the CUDA graphics cards together, in megabytes, as the driver
    reports it; ``None`` when it cannot be read (no card, no driver)."""
    library = _management()
    if library is None:
        return None
    try:
        count = ctypes.c_uint()
        if library.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
            return None
        used = total = 0
        for index in range(count.value):
            handle = ctypes.c_void_p()
            memory = _Memory()
            if library.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
                return None
            if library.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) != 0:
                return None
            used += memory.used
            total += memory.total
    except (OSError, AttributeError):
        return None
    return used / 2**20, total / 2**20
