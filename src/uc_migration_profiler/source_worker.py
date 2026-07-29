"""Resource-bounded worker for untrusted source-container validation."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing
import os
from pathlib import Path
from typing import Any

from .source import SourceLoadError, validate_source_file


VALIDATION_TIMEOUT_SECONDS = 30
VALIDATION_MEMORY_BYTES = 512 * 1024 * 1024


def validate_source_file_isolated(path: str | Path) -> None:
    """Validate an untrusted file in a spawned, resource-bounded process."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    start_event = context.Event()
    process = context.Process(
        target=_worker,
        args=(str(Path(path)), sender, start_event),
        name="impodo-source-validator",
        daemon=True,
    )
    process.start()
    sender.close()
    job_handle: int | None = None
    try:
        if os.name == "nt":
            job_handle = _assign_windows_job(process.pid)
        start_event.set()
        if not receiver.poll(VALIDATION_TIMEOUT_SECONDS):
            process.terminate()
            process.join(timeout=5)
            raise SourceLoadError("Source validation exceeded its time limit")
        status, message = receiver.recv()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            raise SourceLoadError("Source validation worker did not exit")
        if status != "ok":
            raise SourceLoadError(message)
        if process.exitcode != 0:
            raise SourceLoadError("Source validation worker failed")
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if job_handle is not None:
            _windows_kernel32().CloseHandle(job_handle)


def _worker(path: str, sender: Any, start_event: Any) -> None:
    try:
        if os.name != "nt":
            _limit_unix_memory()
        start_event.wait()
        validate_source_file(path)
    except Exception as error:
        sender.send(("error", str(error)))
    else:
        sender.send(("ok", ""))
    finally:
        sender.close()


def _limit_unix_memory() -> None:
    try:
        import resource
    except ImportError as error:
        raise SourceLoadError(
            "This platform cannot enforce the source-validator memory limit"
        ) from error
    resource.setrlimit(
        resource.RLIMIT_AS,
        (VALIDATION_MEMORY_BYTES, VALIDATION_MEMORY_BYTES),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _assign_windows_job(process_id: int) -> int:
    kernel32 = _windows_kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise SourceLoadError("Could not create the source-validator job")
    process_handle = None
    try:
        information = _JobExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
        information.ProcessMemoryLimit = VALIDATION_MEMORY_BYTES
        configured = kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            raise SourceLoadError(
                "Could not configure the source-validator memory limit"
            )
        process_handle = kernel32.OpenProcess(
            0x0001 | 0x0100 | 0x1000,
            False,
            process_id,
        )
        if not process_handle:
            raise SourceLoadError("Could not open the source-validator process")
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise SourceLoadError(
                "Could not isolate the source-validator process"
            )
        return int(job)
    except Exception:
        kernel32.CloseHandle(job)
        raise
    finally:
        if process_handle:
            kernel32.CloseHandle(process_handle)


def _windows_kernel32() -> Any:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
