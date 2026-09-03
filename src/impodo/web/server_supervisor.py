"""Supervise the local web-server child without changing its browser origin."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
from pathlib import Path
import socket
from typing import Callable, Protocol
from urllib.parse import quote
import webbrowser

import h11
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .app import create_local_app
from .diagnostics import LocalDiagnosticRecorder


MAX_AUTOMATIC_RESTARTS = 1


class ClosedConnectionSafeH11Protocol(H11Protocol):
    """Ignore transport bytes delivered after h11 has completed a local close.

    Windows can deliver already-queued bytes after the transport starts closing.
    Uvicorn otherwise tries to answer them with a 400 that h11 cannot send from
    its CLOSED state, turning a successful shutdown into a noisy traceback.
    """

    def data_received(self, data: bytes) -> None:
        if self.conn.our_state is h11.CLOSED:
            self._unset_keepalive_if_required()
            self.transport.close()
            return
        super().data_received(data)


@dataclass(frozen=True, slots=True)
class ServerChildSettings:
    """Hold only the local runtime values required after a safe process spawn."""

    project_root: Path
    expected_host: str
    launch_token: str
    session_secret: str
    diagnostics_root: Path | None
    development_mode: bool


@dataclass(frozen=True, slots=True)
class ServerSupervisionResult:
    """Describe why the launcher stopped supervising its server child."""

    exit_code: int
    reason: str
    restart_attempts: int


class ServerChildProcess(Protocol):
    """Describe the bounded process surface used by the supervisor."""

    pid: int | None
    exitcode: int | None

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...


ServerProcessFactory = Callable[
    [socket.socket, ServerChildSettings, int],
    ServerChildProcess,
]


def supervise_server(
    listener: socket.socket,
    settings: ServerChildSettings,
    *,
    process_factory: ServerProcessFactory | None = None,
    browser_opener: Callable[..., bool] | None = None,
) -> ServerSupervisionResult:
    """Run one child, restart one unexpected exit, then open the circuit."""

    spawn = process_factory or spawn_server_process
    open_browser = browser_opener or webbrowser.open
    port = listener.getsockname()[1]
    active_listener = listener
    restart_attempt = 0
    child: ServerChildProcess | None = None
    browser_opened = False
    try:
        while True:
            child = spawn(active_listener, settings, restart_attempt)
            child.start()
            active_listener.close()
            if not browser_opened:
                url = (
                    f"http://{settings.expected_host}/launch?"
                    f"token={quote(settings.launch_token, safe='')}"
                )
                try:
                    open_browser(url, new=1)
                except OSError:
                    pass
                browser_opened = True
            child.join()
            child_exit_code = (
                int(child.exitcode) if child.exitcode is not None else 1
            )
            record_launcher_event(
                settings.diagnostics_root,
                "server_process_exited",
                port=port,
                reason=(
                    "normal_shutdown"
                    if child_exit_code == 0
                    else "unexpected_exit"
                ),
                exit_code=child_exit_code,
                child_process_id=child.pid,
                restart_attempt=restart_attempt,
            )
            if child_exit_code == 0:
                return ServerSupervisionResult(
                    exit_code=0,
                    reason="normal_shutdown",
                    restart_attempts=restart_attempt,
                )
            replacement_listener = None
            if restart_attempt < MAX_AUTOMATIC_RESTARTS:
                replacement_listener = bind_loopback_listener(port)
            if replacement_listener is None:
                record_launcher_event(
                    settings.diagnostics_root,
                    "server_restart_circuit_open",
                    port=port,
                    reason=(
                        "repeated_exit"
                        if restart_attempt >= MAX_AUTOMATIC_RESTARTS
                        else "port_ownership_lost"
                    ),
                    exit_code=child_exit_code,
                    child_process_id=child.pid,
                    restart_attempt=restart_attempt,
                )
                return ServerSupervisionResult(
                    exit_code=child_exit_code,
                    reason="restart_circuit_open",
                    restart_attempts=restart_attempt,
                )
            restart_attempt += 1
            active_listener = replacement_listener
            record_launcher_event(
                settings.diagnostics_root,
                "server_restart_attempted",
                port=port,
                reason="unexpected_exit",
                exit_code=child_exit_code,
                child_process_id=child.pid,
                restart_attempt=restart_attempt,
            )
    except KeyboardInterrupt:
        if child is not None and child.is_alive():
            child.terminate()
            child.join(timeout=5)
        record_launcher_event(
            settings.diagnostics_root,
            "server_supervision_interrupted",
            port=port,
            reason="keyboard_interrupt",
            child_process_id=child.pid if child is not None else None,
            restart_attempt=restart_attempt,
        )
        return ServerSupervisionResult(
            exit_code=0,
            reason="keyboard_interrupt",
            restart_attempts=restart_attempt,
        )


def spawn_server_process(
    listener: socket.socket,
    settings: ServerChildSettings,
    restart_attempt: int,
) -> ServerChildProcess:
    """Create one spawn-safe server child for Windows and macOS."""

    process = multiprocessing.get_context("spawn").Process(
        target=_serve_child_process,
        args=(listener, settings, restart_attempt),
        name="impodo-web-server",
    )
    return process


def listener_is_owned_loopback(listener: socket.socket, port: int) -> bool:
    """Confirm that the parent still owns the exact loopback listener."""

    try:
        host, observed_port = listener.getsockname()[:2]
        accepting = listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        return (
            listener.fileno() >= 0
            and host == "127.0.0.1"
            and int(observed_port) == int(port)
            and accepting == 1
        )
    except OSError:
        return False


def bind_loopback_listener(port: int) -> socket.socket | None:
    """Acquire one fresh loopback listener, exclusively where supported."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", int(port)))
        listener.listen(128)
        observed_port = listener.getsockname()[1]
        if not listener_is_owned_loopback(listener, observed_port):
            listener.close()
            return None
        return listener
    except OSError:
        listener.close()
        return None


def _serve_child_process(
    listener: socket.socket,
    settings: ServerChildSettings,
    restart_attempt: int,
) -> None:
    diagnostics = None
    if settings.diagnostics_root is not None:
        try:
            diagnostics = LocalDiagnosticRecorder(settings.diagnostics_root)
        except OSError:
            # Optional support evidence must not make the local server fail.
            diagnostics = None
    port = listener.getsockname()[1]
    stop_reason = "server_returned"
    if diagnostics is not None:
        diagnostics.record_lifecycle(
            "server_process_started",
            port=port,
            development_mode=settings.development_mode,
            restart_attempt=restart_attempt,
        )
    try:
        app = create_local_app(
            settings.project_root,
            expected_host=settings.expected_host,
            launch_token=settings.launch_token,
            session_secret=settings.session_secret,
            diagnostic_recorder=diagnostics,
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            http=ClosedConnectionSafeH11Protocol,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            limit_concurrency=20,
            timeout_keep_alive=5,
        )
        server = uvicorn.Server(config)
        app.state.server = server
        server.run(sockets=[listener])
    except BaseException as error:
        stop_reason = "unhandled_exception"
        if diagnostics is not None:
            diagnostics.record_lifecycle(
                "server_process_failed",
                port=port,
                reason=stop_reason,
                exit_code=1,
                exception_class=type(error).__name__,
                restart_attempt=restart_attempt,
            )
        raise
    finally:
        if diagnostics is not None:
            diagnostics.record_lifecycle(
                "server_process_stopped",
                port=port,
                reason=stop_reason,
                exit_code=0 if stop_reason == "server_returned" else 1,
                restart_attempt=restart_attempt,
            )
            diagnostics.close()


def record_launcher_event(
    diagnostics_root: Path | None,
    event: str,
    **fields,
) -> None:
    if diagnostics_root is None:
        return
    try:
        recorder = LocalDiagnosticRecorder(diagnostics_root)
        recorder.record_lifecycle(event, **fields)
        recorder.close()
    except OSError:
        return
