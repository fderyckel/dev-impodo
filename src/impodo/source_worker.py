"""Resource-bounded workers for untrusted source validation and inspection."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing
import os
from pathlib import Path
import sys
import time
from typing import Any

import psutil

from .inspection import (
    SourceFileCatalog,
    SourceInspectionError,
    SourceInspectionOptions,
    inspect_source_file,
)
from .workspace_state import SourceFile
from .source import SourceLoadError, validate_source_file


VALIDATION_TIMEOUT_SECONDS = 30
INSPECTION_TIMEOUT_SECONDS = 60
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
        if not _poll_with_memory_limit(
            receiver,
            process,
            timeout_seconds=VALIDATION_TIMEOUT_SECONDS,
            operation="validation",
            error_type=SourceLoadError,
        ):
            process.terminate()
            process.join(timeout=5)
            raise SourceLoadError("Source validation exceeded its time limit")
        try:
            status, message = receiver.recv()
        except EOFError as error:
            raise SourceLoadError(
                "Source validation worker stopped unexpectedly"
            ) from error
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


def inspect_source_file_isolated(
    path: str | Path,
    *,
    source_file: SourceFile,
    options: SourceInspectionOptions | None,
) -> SourceFileCatalog:
    """Inspect an accepted file in a spawned, resource-bounded process."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    start_event = context.Event()
    process = context.Process(
        target=_inspection_worker,
        args=(str(Path(path)), source_file, options, sender, start_event),
        name="impodo-source-inspector",
        daemon=True,
    )
    process.start()
    sender.close()
    job_handle: int | None = None
    try:
        if os.name == "nt":
            job_handle = _assign_windows_job(process.pid)
        start_event.set()
        if not _poll_with_memory_limit(
            receiver,
            process,
            timeout_seconds=INSPECTION_TIMEOUT_SECONDS,
            operation="inspection",
            error_type=SourceInspectionError,
        ):
            process.terminate()
            process.join(timeout=5)
            raise SourceInspectionError("Source inspection exceeded its time limit")
        try:
            status, payload = receiver.recv()
        except EOFError as error:
            raise SourceInspectionError(
                "Source inspection worker stopped unexpectedly"
            ) from error
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            raise SourceInspectionError("Source inspection worker did not exit")
        if status != "ok":
            raise SourceInspectionError(str(payload))
        if process.exitcode != 0:
            raise SourceInspectionError("Source inspection worker failed")
        return SourceFileCatalog.from_json(str(payload))
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if job_handle is not None:
            _windows_kernel32().CloseHandle(job_handle)


def _worker(path: str, sender: Any, start_event: Any) -> None:
    try:
        if os.name != "nt" and sys.platform != "darwin":
            _limit_unix_memory()
        start_event.wait()
        validate_source_file(path)
    except Exception as error:
        sender.send(("error", str(error)))
    else:
        sender.send(("ok", ""))
    finally:
        sender.close()


def _inspection_worker(
    path: str,
    source_file: SourceFile,
    options: SourceInspectionOptions | None,
    sender: Any,
    start_event: Any,
) -> None:
    try:
        if os.name != "nt" and sys.platform != "darwin":
            _limit_unix_memory()
        start_event.wait()
        catalog = inspect_source_file(
            path,
            source_file=source_file,
            options=options,
        )
    except Exception as error:
        sender.send(("error", str(error)))
    else:
        sender.send(("ok", catalog.to_json()))
    finally:
        sender.close()


def _limit_unix_memory() -> None:
    try:
        import resource
    except ImportError as error:
        raise SourceLoadError(
            "This platform cannot enforce the source-validator memory limit"
        ) from error
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = min(VALIDATION_MEMORY_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def _poll_with_memory_limit(
    receiver: Any,
    process: multiprocessing.Process,
    *,
    timeout_seconds: int,
    operation: str,
    error_type: type[Exception],
) -> bool:
    """Wait for a worker while enforcing macOS resident-memory bounds.

    macOS cannot reliably lower ``RLIMIT_AS`` after Python and its native
    libraries are loaded.  The parent therefore watches the spawned process's
    resident set and fails closed if it crosses the same 512 MiB boundary.
    Linux keeps the kernel-enforced address-space limit and Windows uses a Job
    Object configured by the caller.
    """

    if sys.platform != "darwin":
        return receiver.poll(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    monitored = psutil.Process(process.pid)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if receiver.poll(min(0.05, remaining)):
            return True
        try:
            resident = monitored.memory_info().rss
        except psutil.NoSuchProcess:
            return receiver.poll(0)
        if resident > VALIDATION_MEMORY_BYTES:
            process.terminate()
            process.join(timeout=5)
            raise error_type(
                f"Source {operation} exceeded its memory limit"
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

