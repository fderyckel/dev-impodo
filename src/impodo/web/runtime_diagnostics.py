"""Capture and sanitize bounded runtime facts without host names or paths.

Static facts are observed once per process. Available memory is a separate
sample, and missing probes remain explicit rather than becoming zero capacity.
No subprocess, security-product probe, or privileged configuration is used.
"""

from __future__ import annotations

import ctypes
import math
import platform
import re
import struct
import sys
from collections.abc import Callable, Mapping
from functools import lru_cache

import duckdb
import psutil

_ARCHITECTURES = frozenset({"arm64", "aarch64", "x86_64", "amd64", "i386", "i686"})
_VERSION = re.compile(
    r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}(?:[a-zA-Z][a-zA-Z0-9.+-]{0,20})?(?:[-+][a-zA-Z0-9.-]{1,24})?"
)
_VERSIONS = (
    "operating_system_release",
    "operating_system_build",
    "python_version",
    "duckdb_version",
    "polars_version",
    "psutil_version",
)
_NUMBERS = (
    "logical_cpu_count",
    "physical_cpu_count",
    "total_memory_bytes",
    "available_memory_bytes",
    "process_bitness",
)
_FIELDS = (
    *_VERSIONS,
    *_NUMBERS,
    "architecture",
    "process_architecture",
    "native_architecture",
)


def _probe(probe: Callable[[], object]) -> object:
    """Isolate each optional observation without retaining exception details."""
    try:
        return probe()
    except Exception:  # noqa: BLE001 - runtime observations cannot change job truth
        return None


def _windows_architectures() -> tuple[str | None, str | None]:
    """Read actual process/native machine types using a non-privileged API."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    current_process = kernel.GetCurrentProcess
    current_process.restype = ctypes.c_void_p
    observe = kernel.IsWow64Process2
    observe.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ushort),
    ]
    observe.restype = ctypes.c_int
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not observe(current_process(), ctypes.byref(process), ctypes.byref(native)):
        return None, None
    names = {0x014C: "i386", 0x8664: "amd64", 0xAA64: "arm64"}
    native_name = names.get(native.value)
    return names.get(process.value) if process.value else native_name, native_name


@lru_cache(maxsize=1)
def _static_runtime_metadata() -> dict[str, object]:
    """Observe process-lifetime facts once without opening application files."""
    operating_system = _probe(platform.system)
    architecture = _probe(lambda: platform.machine().lower())
    native: object = None
    process: object = architecture
    architecture_source = "process_reported"
    if operating_system == "Windows":
        observed = _probe(_windows_architectures)
        if isinstance(observed, tuple) and len(observed) == 2:
            process, native = observed
            if process is not None and native is not None:
                architecture_source = "windows_api"
    memory = _probe(psutil.virtual_memory)
    return {
        "operating_system": operating_system,
        "operating_system_release": _probe(platform.release),
        "operating_system_build": _probe(platform.version),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "duckdb_version": _probe(lambda: duckdb.__version__),
        "polars_version": _probe(
            lambda: getattr(sys.modules.get("polars"), "__version__", None)
        ),
        "psutil_version": _probe(lambda: psutil.__version__),
        "architecture": architecture,
        "process_architecture": process,
        "native_architecture": native,
        "architecture_source": architecture_source,
        "process_bitness": struct.calcsize("P") * 8,
        "logical_cpu_count": _probe(lambda: psutil.cpu_count(logical=True)),
        "physical_cpu_count": _probe(lambda: psutil.cpu_count(logical=False)),
        "total_memory_bytes": getattr(memory, "total", None),
    }


def runtime_performance_metadata() -> dict[str, object]:
    """Return detached runtime capacity with explicit probe availability."""
    runtime = dict(_static_runtime_metadata())
    runtime["polars_version"] = _probe(
        lambda: getattr(sys.modules.get("polars"), "__version__", None)
    )
    memory = _probe(psutil.virtual_memory)
    runtime["available_memory_bytes"] = getattr(memory, "available", None)
    return sanitize_runtime_metadata(runtime)


def sanitize_runtime_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """Apply a closed schema both to captured facts and untrusted log exports."""
    operating_system = value.get("operating_system")
    result: dict[str, object] = {
        "operating_system": operating_system
        if isinstance(operating_system, str)
        and operating_system in {"Windows", "Darwin", "Linux"}
        else "Unknown",
        "memory_scope": "system_wide",
    }
    for name in _VERSIONS:
        candidate = value.get(name)
        result[name] = (
            candidate
            if isinstance(candidate, str) and _VERSION.fullmatch(candidate)
            else None
        )
    for name in ("architecture", "process_architecture", "native_architecture"):
        candidate = value.get(name)
        result[name] = (
            candidate
            if isinstance(candidate, str) and candidate in _ARCHITECTURES
            else None
        )
    for name in _NUMBERS:
        candidate = value.get(name)
        result[name] = (
            candidate
            if type(candidate) is int
            and (0 <= candidate if name == "available_memory_bytes" else 0 < candidate)
            and candidate <= 2**53 - 1
            and math.isfinite(candidate)
            else None
        )
    if result["process_bitness"] not in {32, 64}:
        result["process_bitness"] = None
    source = value.get("architecture_source")
    result["architecture_source"] = (
        source
        if isinstance(source, str) and source in {"process_reported", "windows_api"}
        else "unavailable"
    )
    process, native = result["process_architecture"], result["native_architecture"]
    result["emulation_status"] = (
        ("native" if process == native else "emulated")
        if process is not None and native is not None
        else "unavailable"
    )
    result["coverage"] = {
        "available_fields": [name for name in _FIELDS if result[name] is not None],
        "unavailable_fields": [name for name in _FIELDS if result[name] is None],
        "measurement_scope": "capture_process",
        "security_scan_attribution": "unavailable",
        "storage_and_power_context": "unavailable",
    }
    if any(
        result[name] is None
        for name in (
            "logical_cpu_count",
            "total_memory_bytes",
            "available_memory_bytes",
        )
    ):
        result["resource_observation"] = "unavailable"
    return result
